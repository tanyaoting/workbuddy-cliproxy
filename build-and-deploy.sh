#!/usr/bin/env bash
#
# 一条命令:编译 workbuddy 插件并热更新到运行中的 CPA 容器。
#
#   ./build-and-deploy.sh
#
# 流程:容器内编译 -> 校验产物 -> 备份旧插件 -> 拷贝 -> 重启容器 -> 验证模型列表
#
# [workbuddy 2026-09-18] 首次编写:封装交叉编译 + 部署流程
#
set -euo pipefail

# ---------------------------------------------------------------------------
# 配置(按需改这里,或临时用环境变量覆盖)
# ---------------------------------------------------------------------------
CONTAINER="${CONTAINER:-cpa}"
PLUGIN_DEST="${PLUGIN_DEST:-/app/plugins/workbuddy.so}"
BACKUP_DIR="${BACKUP_DIR:-/app/plugins-backup}"
GO_IMAGE="${GO_IMAGE:-golang:1.26-bookworm}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"
CPA_URL="${CPA_URL:-http://localhost:8317}"
CPA_KEY="${CPA_KEY:-}"
MODEL_TO_TEST="${MODEL_TO_TEST:-deepseek-v4.1-flash}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 产物落到 dist/,不覆盖仓库里已有的 workbuddy.so(.dylib 等历史产物)
DIST_DIR="$SRC_DIR/dist"
mkdir -p "$DIST_DIR"
# 容器内输出用相对路径:必须相对于 /src,传宿主机绝对路径会写进容器临时层然后随 --rm 丢失
OUT_REL="dist/workbuddy.so"
OUT_SO="$SRC_DIR/$OUT_REL"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m  错误:\033[0m %s\n' "$*" >&2; exit 1; }

# 把 "1.2GB" / "247MB" 这类字符串转成可比较的整数(单位 KB)
size_to_kb() {
  awk '{
    n = $1 + 0
    u = $1
    sub(/^[0-9.]+/, "", u)
    if      (u ~ /^GB/) n *= 1024 * 1024
    else if (u ~ /^MB/) n *= 1024
    else if (u ~ /^kB/) n *= 1
    else if (u ~ /^B/)  n /= 1024
    printf "%d", n
  }'
}

# 版本号比较:ver_ge "2.34" "2.17" -> 0 表示 >=
ver_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN{
    na = split(a, A, "."); nb = split(b, B, ".")
    n = (na > nb) ? na : nb
    for (i = 1; i <= n; i++) {
      x = (i <= na) ? A[i] + 0 : 0
      y = (i <= nb) ? B[i] + 0 : 0
      if (x > y) exit 0
      if (x < y) exit 1
    }
    exit 0
  }'
}

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
log "前置检查"

command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
docker info >/dev/null 2>&1 || die "docker 守护进程不可用"

docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || die "容器 '$CONTAINER' 不存在。用 CONTAINER=<名字> 指定,或先启动它"
docker inspect -f '{{.State.Running}}' "$CONTAINER" | grep -q true \
  || die "容器 '$CONTAINER' 未在运行"

docker image inspect "$GO_IMAGE" >/dev/null 2>&1 \
  || die "编译镜像 '$GO_IMAGE' 不存在,先执行: docker pull $GO_IMAGE"

# 目标架构决定 GOARCH;容器架构必须与产物一致,否则插件加载不了
CONTAINER_ARCH="$(docker exec "$CONTAINER" uname -m | tr -d '\r')"
case "$CONTAINER_ARCH" in
  aarch64|arm64) GOARCH_VAL=arm64 ;;
  x86_64|amd64)  GOARCH_VAL=amd64 ;;
  *) die "不支持的容器架构: $CONTAINER_ARCH" ;;
esac

# 容器 glibc 版本(用于后面校验产物兼容性)
CONTAINER_GLIBC="$(docker exec "$CONTAINER" sh -c 'ldd --version 2>/dev/null | head -1' \
  | grep -oE '[0-9]+\.[0-9]+$' || true)"
[ -n "$CONTAINER_GLIBC" ] || warn "无法检测容器 glibc 版本,跳过兼容性校验"

ok "容器 $CONTAINER 运行中,架构 $CONTAINER_ARCH (GOARCH=$GOARCH_VAL),glibc ${CONTAINER_GLIBC:-未知}"

# 工作区有未提交改动时提醒(不阻止,编译的就是当前工作区代码)
if [ -n "$(git -C "$SRC_DIR" status --porcelain 2>/dev/null)" ]; then
  warn "工作区有未提交改动,编译的是当前工作区代码(不是 HEAD)"
fi

# ---------------------------------------------------------------------------
# 1. 容器内编译
# ---------------------------------------------------------------------------
log "编译中(容器 $GO_IMAGE,目标 linux/$GOARCH_VAL)"

BUILD_START=$(date +%s)
docker run --rm --platform "linux/$GOARCH_VAL" \
  -v "$SRC_DIR":/src -w /src \
  -e CGO_ENABLED=1 -e GOOS=linux -e GOARCH="$GOARCH_VAL" \
  -e GOPATH=/tmp/gopath -e GOCACHE=/tmp/gocache \
  -e GOPROXY="${GOPROXY:-https://goproxy.cn,direct}" \
  "$GO_IMAGE" \
  go build -buildmode=c-shared -o "$OUT_REL" . \
  || die "编译失败"

BUILD_SEC=$(( $(date +%s) - BUILD_START ))
ok "编译完成,耗时 ${BUILD_SEC}s"

# ---------------------------------------------------------------------------
# 2. 校验产物
# ---------------------------------------------------------------------------
log "校验产物"

[ -f "$OUT_SO" ] || die "没有生成 $OUT_SO"

FILE_INFO="$(file -b "$OUT_SO")"
case "$GOARCH_VAL:$FILE_INFO" in
  arm64:*ARM\ aarch64*) ok "架构 arm64" ;;
  amd64:*x86-64*)       ok "架构 amd64" ;;
  *) die "产物架构不符: $FILE_INFO" ;;
esac

# 导出符号:插件被 CPA 加载的前提
SYMS="$(nm -D --defined-only "$OUT_SO" 2>/dev/null | grep -cE ' T cliproxy' || true)"
[ "$SYMS" -eq 4 ] || die "导出符号数应为 4,实际 $SYMS(插件 ABI 不完整)"
ok "导出符号 4 个"

# glibc 下限必须 <= 容器 glibc,否则加载时会报 version not found
SO_GLIBC="$(strings "$OUT_SO" | grep -oE 'GLIBC_[0-9.]+' | sort -uV | tail -1 | sed 's/GLIBC_//')"
if [ -n "$CONTAINER_GLIBC" ] && [ -n "$SO_GLIBC" ]; then
  ver_ge "$CONTAINER_GLIBC" "$SO_GLIBC" \
    || die "产物要求 glibc >= $SO_GLIBC,容器只有 $CONTAINER_GLIBC(编译镜像基线太新)"
  ok "glibc 要求 $SO_GLIBC <= 容器 $CONTAINER_GLIBC"
fi

SO_SIZE="$(du -h "$OUT_SO" | awk '{print $1}')"
SO_SHA="$(shasum -a 256 "$OUT_SO" | awk '{print $1}')"
ok "大小 $SO_SIZE,sha256 ${SO_SHA:0:16}…"

# ---------------------------------------------------------------------------
# 3. 备份旧插件(放在 plugins 目录外,避免被 CPA 当成第二个插件扫描)
# ---------------------------------------------------------------------------
log "备份旧插件"

OLD_SHA="$(docker exec "$CONTAINER" sha256sum "$PLUGIN_DEST" 2>/dev/null | awk '{print $1}' || true)"
if [ -n "$OLD_SHA" ]; then
  if [ "$OLD_SHA" = "$SO_SHA" ]; then
    ok "容器内插件已是同一份(sha256 相同),仍会重新拷贝以刷新 mtime"
  fi
  STAMP="$(date +%Y%m%d-%H%M%S)"
  docker exec "$CONTAINER" sh -c \
    "mkdir -p '$BACKUP_DIR' && cp '$PLUGIN_DEST' '$BACKUP_DIR/workbuddy.so.$STAMP'" \
    || warn "备份失败,继续部署"
  ok "备份到 $BACKUP_DIR/workbuddy.so.$STAMP"

  # 只保留最近 N 份
  docker exec "$CONTAINER" sh -c \
    "ls -1t '$BACKUP_DIR'/workbuddy.so.* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f" \
    || true
else
  warn "容器内没有 $PLUGIN_DEST,跳过备份(首次部署?)"
fi

# ---------------------------------------------------------------------------
# 4. 拷贝并重启
# ---------------------------------------------------------------------------
log "部署到容器"

docker cp "$OUT_SO" "$CONTAINER:$PLUGIN_DEST" || die "拷贝失败"
ok "已拷入 $PLUGIN_DEST"

docker restart "$CONTAINER" >/dev/null || die "重启容器失败"
ok "容器已重启,等待插件加载"

# 等插件注册日志出现(最多 60s)
LOADED=0
for _ in $(seq 1 30); do
  sleep 2
  if docker logs --since 90s "$CONTAINER" 2>&1 | grep -q "plugin registered plugin_id=workbuddy"; then
    LOADED=1
    break
  fi
done

if [ "$LOADED" -eq 1 ]; then
  ok "插件已加载并注册"
else
  warn "60s 内没看到 plugin registered 日志,请手动检查:"
  docker logs --tail 30 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2
  exit 1
fi

# 注册过程中偶发 deadline 警告(旧版也有),提示但不视为失败
if docker logs --since 90s "$CONTAINER" 2>&1 | grep -q "model registrar workbuddy failed"; then
  warn "日志有 'model registrar ... context deadline exceeded'(该插件既有现象,通常随后重试成功)"
fi

# ---------------------------------------------------------------------------
# 5. 端到端验证
# ---------------------------------------------------------------------------
if [ "$SKIP_VERIFY" = "1" ]; then
  warn "SKIP_VERIFY=1,跳过 API 验证"
else
  log "验证模型列表"

  if [ -z "$CPA_KEY" ]; then
    # 尝试从容器内 config.yaml 读第一个 api-key
    CPA_KEY="$(docker exec "$CONTAINER" sh -c \
      "grep -m1 '^api-keys:' /app/config.yaml 2>/dev/null | sed -E 's/.*\\[\"([^\"]+)\".*/\1/'" || true)"
  fi

  if [ -z "$CPA_KEY" ]; then
    warn "拿不到 API key,跳过 API 验证(可用 CPA_KEY=xxx 指定)"
  else
    MODELS="$(curl -fsS --max-time 15 "$CPA_URL/v1/models" \
      -H "Authorization: Bearer $CPA_KEY" 2>/dev/null \
      | python3 -c 'import sys,json; print("\n".join(m["id"] for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null || true)"

    if [ -z "$MODELS" ]; then
      warn "请求 $CPA_URL/v1/models 失败,跳过验证"
    else
      COUNT="$(printf '%s\n' "$MODELS" | grep -c . || true)"
      ok "模型列表 $COUNT 个"
      if printf '%s\n' "$MODELS" | grep -qx "$MODEL_TO_TEST"; then
        ok "目标模型 $MODEL_TO_TEST 在列"
      else
        warn "$MODEL_TO_TEST 不在模型列表里"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
log "完成"
printf '  插件: %s (%s, glibc>=%s)\n' "$PLUGIN_DEST" "$SO_SIZE" "${SO_GLIBC:-?}"
printf '  回滚: docker cp %s/workbuddy.so.<时间戳> %s:%s && docker restart %s\n' \
  "$BACKUP_DIR" "$CONTAINER" "$PLUGIN_DEST" "$CONTAINER"
