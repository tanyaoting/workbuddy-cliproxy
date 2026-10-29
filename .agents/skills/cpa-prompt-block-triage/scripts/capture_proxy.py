#!/usr/bin/env python3
"""Transparent capture proxy: logs request and response, forwards to CPA.

Usage:
    CPA_API_KEY=<key> [CAPTURE_DIR=/tmp/blockshot] [CAPTURE_PORT=8399] \\
        [UPSTREAM_BASE=http://127.0.0.1:8317] python3 capture_proxy.py

Point the client's baseURL at http://127.0.0.1:<CAPTURE_PORT>/v1, send one
request, then read <CAPTURE_DIR>/NN-stamp-req.json (request body) and
*-resp.json / *.sse (upstream answer).

Each exchange is written as three files:
    NN-<stamp>-req.json    {"path": ..., "body": <original request>}
    NN-<stamp>-resp.json   {"status", "finish_reason", "usage", "content",
                            "reasoning_content", "tool_calls"}
    NN-<stamp>-resp.sse    raw upstream byte stream
"""
import http.client
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

OUT_DIR = os.environ.get("CAPTURE_DIR", "/tmp/blockshot")
UPSTREAM = os.environ.get("UPSTREAM_BASE", "http://127.0.0.1:8317")
CPA_KEY = os.environ.get("CPA_API_KEY", "")
os.makedirs(OUT_DIR, exist_ok=True)
_counter = {"n": 0}
_up = urlparse(UPSTREAM)


def _save_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[capture] " + (fmt % args) + "\n")

    def _forward(self, method, raw=b""):
        conn = http.client.HTTPConnection(_up.hostname, _up.port or 80, timeout=300)
        headers = {
            "Authorization": f"Bearer {CPA_KEY}",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "*/*"),
        }
        conn.request(method, _up.path.rstrip("/") + self.path, body=raw or None, headers=headers)
        return conn, conn.getresponse()

    def do_GET(self):
        try:
            conn, resp = self._forward("GET")
            data = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as exc:  # noqa: BLE001 - surface any upstream failure to the client
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            sys.stderr.write(f"[capture] GET error {exc}\n")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _counter["n"] += 1
        n = _counter["n"]
        stem = os.path.join(OUT_DIR, f"{n:02d}-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 - keep the raw bytes for malformed clients
            payload = {"_raw": raw.decode("utf-8", "replace")}
        _save_json(stem + "-req.json", {"path": self.path, "body": payload})
        sys.stderr.write(f"[capture] #{n} -> upstream ({len(raw)} bytes)\n")

        try:
            conn, resp = self._forward("POST", raw)
        except Exception as exc:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            sys.stderr.write(f"[capture] upstream error {exc}\n")
            return

        ctype = resp.getheader("Content-Type", "application/json")
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")

        chunks = []
        content, reasoning, calls, finish, usage = [], [], {}, None, None
        try:
            if "text/event-stream" in ctype:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    chunks.append(line)
                    self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                    text = line.decode("utf-8", "replace").strip()
                    if not text.startswith("data:"):
                        continue
                    data = text[5:].strip()
                    if data in ("", "[DONE]"):
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:  # noqa: BLE001 - partial frames are normal
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                data = resp.read()
                chunks.append(data)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    obj = json.loads(data)
                    for ch in obj.get("choices", []):
                        msg = ch.get("message") or {}
                        content.append(msg.get("content") or "")
                        reasoning.append(msg.get("reasoning_content") or "")
                        finish = ch.get("finish_reason")
                        for tc in msg.get("tool_calls") or []:
                            calls[tc.get("index", 0)] = {
                                "id": tc.get("id", ""),
                                "name": (tc.get("function") or {}).get("name", ""),
                                "arguments": (tc.get("function") or {}).get("arguments", ""),
                            }
                    usage = obj.get("usage")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            conn.close()

        with open(stem + "-resp.sse", "wb") as fh:
            fh.write(b"".join(chunks))
        _save_json(stem + "-resp.json", {
            "status": resp.status,
            "finish_reason": finish,
            "usage": usage,
            "content": "".join(content),
            "reasoning_content": "".join(reasoning),
            "tool_calls": [calls[k] for k in sorted(calls)],
        })
        sys.stderr.write(
            f"[capture] #{n} <- {resp.status} content={len(''.join(content))}c "
            f"tools={len(calls)}\n"
        )
        self.close_connection = True


if __name__ == "__main__":
    port = int(os.environ.get("CAPTURE_PORT", "8399"))
    if not CPA_KEY:
        sys.stderr.write("[capture] WARNING: CPA_API_KEY is empty; upstream will reject\n")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write(f"[capture] 127.0.0.1:{port} -> {UPSTREAM} (saving to {OUT_DIR})\n")
    srv.serve_forever()
