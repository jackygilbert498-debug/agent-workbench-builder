"""Read-only local health and evidence dashboard."""

from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Sequence

from .domain import PROJECT_TITLE, SCENARIO


def status_snapshot(work_root: Path) -> dict[str, Any]:
    outputs = sorted((work_root / "output").glob("*.json")) if (work_root / "output").is_dir() else []
    receipts = sorted((work_root / "receipts").glob("*.json")) if (work_root / "receipts").is_dir() else []
    return {
        "schema": "agent-workbench-status/v1",
        "status": "ok",
        "businessOutputs": len(outputs),
        "runReceipts": len(receipts),
    }


def _handler(work_root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            if self.path == "/api/health":
                self._send_json(
                    200,
                    {
                        "schema": "agent-workbench-health/v1",
                        "status": "ok",
                        "project": PROJECT_TITLE,
                    },
                )
                return
            if self.path == "/api/status":
                self._send_json(200, status_snapshot(work_root))
                return
            if self.path == "/":
                snapshot = status_snapshot(work_root)
                body = f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{escape(PROJECT_TITLE)}</title>
<style>body{{font:16px system-ui;max-width:760px;margin:48px auto;padding:0 20px;color:#17202a}}.card{{border:1px solid #d8dee4;border-radius:16px;padding:24px}}code{{background:#f4f6f8;padding:2px 6px;border-radius:6px}}</style>
</head><body><main class=\"card\"><p>Agent workbench · read-only status</p>
<h1>{escape(PROJECT_TITLE)}</h1><p>{escape(SCENARIO)}</p>
<p>Health: <strong>ok</strong></p>
<p>Business outputs: <code>{snapshot['businessOutputs']}</code></p>
<p>Run receipts: <code>{snapshot['runReceipts']}</code></p>
<p>Writes require explicit approval in the CLI.</p></main></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json(404, {"status": "not_found"})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def create_server(host: str, port: int, work_root: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _handler(work_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--work-dir", type=Path, default=Path("work"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("refusing a non-loopback host")
    server = create_server(args.host, args.port, args.work_dir)
    print(f"{PROJECT_TITLE}: http://{args.host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
