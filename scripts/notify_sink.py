"""Minimal HTTP sink for HA notify.rest 验证：把 POST 请求体写到 var/notify/payloads.log。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OUT = Path("var/notify")
OUT.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", "replace")
        record = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        }
        with (OUT / "payloads.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # silence default stderr logging
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9999), Handler).serve_forever()