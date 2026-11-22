"""Demo service deployed into every preview environment (stdlib only).

It reports which pull request and commit it was built from, so the end-to-end test
can prove the Ingress routes each preview host to the right build.
"""

from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def info() -> dict[str, str]:
    return {
        "pr": os.environ.get("PREVIEW_PR", "local"),
        "namespace": os.environ.get("PREVIEW_NAMESPACE", "local"),
        "host": os.environ.get("PREVIEW_HOST", "localhost"),
        "sha": os.environ.get("GIT_SHA", "dev"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "preview-app"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        elif self.path == "/version":
            self._send(200, json.dumps(info()).encode(), "application/json")
        elif self.path == "/":
            raw = info()
            raw["sha"] = raw["sha"][:12]  # truncate before escaping, never after
            data = {k: html.escape(v) for k, v in raw.items()}
            page = (
                "<!doctype html><meta charset=utf-8><title>Preview</title>"
                f"<h1>Preview of PR #{data['pr']}</h1>"
                f"<p>Commit <code>{data['sha']}</code> in namespace <code>{data['namespace']}</code>.</p>"
            )
            self._send(200, page.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 (container)


if __name__ == "__main__":
    main()
