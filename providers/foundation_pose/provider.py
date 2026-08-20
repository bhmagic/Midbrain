from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import threading
from typing import Any


PROVIDER_ROOT = Path(__file__).resolve().parent
PYTHON_ROOT = PROVIDER_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from foundation_pose_provider.service import FoundationPoseProvider  # noqa: E402


DEVELOPER_ROOT = PROVIDER_ROOT / "web"
DEVELOPER_PAGE = (DEVELOPER_ROOT / "developer.html").read_bytes()
DEVELOPER_CSS = (DEVELOPER_ROOT / "developer.css").read_bytes()
DEVELOPER_JS = (DEVELOPER_ROOT / "developer.js").read_bytes()


class ControlHandler(BaseHTTPRequestHandler):
    provider: FoundationPoseProvider

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/v1/status"}:
            self._json(200, self.provider.status_payload())
        elif path == "/dev":
            self._bytes(200, DEVELOPER_PAGE, "text/html; charset=utf-8")
        elif path == "/dev/assets/developer.css":
            self._bytes(200, DEVELOPER_CSS, "text/css; charset=utf-8")
        elif path == "/dev/assets/developer.js":
            self._bytes(200, DEVELOPER_JS, "text/javascript; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/control/hot":
                result = self.provider.start_hot()
            elif self.path == "/v1/control/warm":
                result = self.provider.enter_warm()
            elif self.path == "/v1/control/stop":
                result = self.provider.stop()
            elif self.path == "/v1/control/request":
                result = self.provider.handle_request(self._read_json())
            else:
                self._json(404, {"error": "not found"})
                return
            self._json(200, result)
        except (ValueError, RuntimeError) as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._bytes(status, payload, "application/json")

    def _bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[FoundationPoseControl] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--control-port", type=int, default=7103)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    root = Path(os.environ.get("PHYSICAL_AGENT_ROOT") or PROVIDER_ROOT.parents[1])
    provider = FoundationPoseProvider(
        config,
        root,
        args.manager_url,
        args.fabric_url,
        f"http://127.0.0.1:{args.control_port}",
    )
    ControlHandler.provider = provider
    server = ThreadingHTTPServer(("127.0.0.1", args.control_port), ControlHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        provider.stop()

    if os.name != "nt":
        signal.signal(signal.SIGTERM, request_stop)
    try:
        return provider.run()
    finally:
        server.shutdown()
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
