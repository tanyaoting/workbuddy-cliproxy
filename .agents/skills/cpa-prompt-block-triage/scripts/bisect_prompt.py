#!/usr/bin/env python3
"""Probe which part of a captured request trips the upstream prompt block.

Modes:
    replay     send the captured request as-is
    swap       cross-swap system prompt and tools between an OK and a BLOCKED
               request to see which field decides the verdict
    sentences  test every sentence of the target text on its own
    pairs      test every two-sentence combination
    prefix     binary-search the shortest prefix that still blocks

The API key comes from --key or CPA_API_KEY. The endpoint defaults to a local
CPA at http://127.0.0.1:8317/v1/chat/completions.

Examples:
    python3 bisect_prompt.py replay /tmp/blockshot/01-xxxx-req.json
    python3 bisect_prompt.py swap ok.json blocked.json
    python3 bisect_prompt.py sentences blocked.json --field system
    python3 bisect_prompt.py pairs blocked.json --field system
    python3 bisect_prompt.py prefix blocked.json --field system
"""
import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8317/v1/chat/completions"


def load_request(path):
    """Accept both a raw request body and a capture_proxy wrapper."""
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if isinstance(obj, dict) and "body" in obj:
        return obj["body"]
    return obj


def call(body, url, key, timeout=180):
    """Return (ok, detail); ok is False for any upstream error envelope."""
    payload = copy.deepcopy(body)
    payload["stream"] = False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(data=data, url=url, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - report transport failures as-is
        return False, f"transport error: {exc}"
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return False, raw[:200]
    if isinstance(parsed, dict) and parsed.get("error"):
        return False, str(parsed["error"])[:160]
    try:
        content = parsed["choices"][0]["message"].get("content") or ""
    except Exception:  # noqa: BLE001
        content = ""
    return True, content[:60].replace("\n", " ")


def verdict(ok):
    return "OK     " if ok else "BLOCKED"


def system_text(body):
    for msg in body.get("messages", []):
        if msg.get("role") == "system":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def with_system(body, text):
    out = copy.deepcopy(body)
    for msg in out.get("messages", []):
        if msg.get("role") == "system":
            msg["content"] = text
            return out
    out.setdefault("messages", []).insert(0, {"role": "system", "content": text})
    return out


def split_sentences(text):
    """Split on sentence ends while keeping a usable minimum length."""
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in ".!?。！？" and len(buf.strip()) > 20:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts


def cmd_replay(args, key):
    body = load_request(args.request)
    ok, detail = call(body, args.url, key)
    print(f"{verdict(ok)}  {detail}")
    return 0 if ok else 1


def cmd_swap(args, key):
    ok_body = load_request(args.ok_request)
    bad_body = load_request(args.blocked_request)

    bad_sys = copy.deepcopy(bad_body)
    bad_sys["messages"] = [{"role": "system", "content": system_text(ok_body)}] + [
        m for m in bad_body.get("messages", []) if m.get("role") != "system"
    ]
    ok_sys = copy.deepcopy(ok_body)
    ok_sys["messages"] = [{"role": "system", "content": system_text(bad_body)}] + [
        m for m in ok_body.get("messages", []) if m.get("role") != "system"
    ]
    ok_tools = copy.deepcopy(ok_body)
    ok_tools["tools"] = bad_body.get("tools", [])
    bad_tools = copy.deepcopy(bad_body)
    bad_tools["tools"] = ok_body.get("tools", [])

    cases = [
        ("baseline: passing request", ok_body),
        ("baseline: blocked request", bad_body),
        ("blocked request + passing system prompt", bad_sys),
        ("passing request + blocked system prompt", ok_sys),
        ("passing request + blocked tools", ok_tools),
        ("blocked request + passing tools", bad_tools),
    ]
    for label, body in cases:
        ok, detail = call(body, args.url, key)
        print(f"{verdict(ok)}  {label}")
        time.sleep(args.delay)
    print("-- system prompt decides the verdict => fix the prompt; tools => fix the schemas")
    return 0


def cmd_sentences(args, key):
    body = load_request(args.request)
    base = system_text(body) if args.field == "system" else ""
    ok, _ = call(body, args.url, key)
    print(f"{verdict(ok)}  baseline (untouched)")
    if args.field == "messages":
        msgs = [m for m in body.get("messages", []) if m.get("role") != "system"]
        for i, msg in enumerate(msgs):
            probe = copy.deepcopy(body)
            probe["messages"] = [m for m in body["messages"] if m.get("role") == "system"] + [msg]
            ok, detail = call(probe, args.url, key)
            text = msg.get("content") if isinstance(msg.get("content"), str) else ""
            print(f"{verdict(ok)}  msg[{i}] {text[:60]!r}")
            time.sleep(args.delay)
        return 0

    sents = split_sentences(base)
    print(f"-- {len(sents)} sentences --")
    for i, sent in enumerate(sents, 1):
        ok, detail = call(with_system(body, sent), args.url, key)
        print(f"{verdict(ok)}  s{i}  {sent[:70]}")
        time.sleep(args.delay)
    return 0


def cmd_pairs(args, key):
    body = load_request(args.request)
    sents = split_sentences(system_text(body))
    if len(sents) < 2:
        print("need at least two sentences", file=sys.stderr)
        return 2
    blocked = []
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            combo = sents[i] + " " + sents[j]
            ok, _ = call(with_system(body, combo), args.url, key)
            tag = "BLOCKED" if not ok else "ok     "
            print(f"{tag}  s{i + 1}+s{j + 1}")
            if not ok:
                blocked.append((i + 1, j + 1))
            time.sleep(args.delay)
    print(f"-- blocked pairs: {blocked if blocked else 'none'}")
    return 0


def cmd_prefix(args, key):
    body = load_request(args.request)
    text = system_text(body)
    ok_full, _ = call(body, args.url, key)
    if ok_full:
        print("baseline already OK; nothing to bisect")
        return 0
    lo, hi = 0, len(text)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        ok, _ = call(with_system(body, text[:mid]), args.url, key)
        print(f"{verdict(ok)}  prefix {mid}")
        if ok:
            lo = mid
        else:
            hi = mid
        time.sleep(args.delay)
    print(f"-- shortest blocking prefix: {hi} chars")
    print(f"-- context: ...{text[max(0, hi - 60):hi]!r} | {text[hi:hi + 40]!r}...")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--key", default=os.environ.get("CPA_API_KEY", ""))
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between probes")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replay")
    p.add_argument("request")

    p = sub.add_parser("swap")
    p.add_argument("ok_request")
    p.add_argument("blocked_request")

    for name in ("sentences", "pairs", "prefix"):
        p = sub.add_parser(name)
        p.add_argument("request")
        p.add_argument("--field", choices=["system", "messages"], default="system")

    args = parser.parse_args()
    if not args.key:
        print("missing API key: pass --key or set CPA_API_KEY", file=sys.stderr)
        return 2
    handlers = {
        "replay": cmd_replay,
        "swap": cmd_swap,
        "sentences": cmd_sentences,
        "pairs": cmd_pairs,
        "prefix": cmd_prefix,
    }
    return handlers[args.command](args, args.key)


if __name__ == "__main__":
    sys.exit(main())
