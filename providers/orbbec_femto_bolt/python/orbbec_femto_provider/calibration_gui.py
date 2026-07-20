from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from .accelerometer_calibration import (
    POSE_ORDER,
    STANDARD_GRAVITY_M_S2,
    CaptureSummary,
    build_custom_calibration_document,
    pose_quality,
    solve_six_position_calibration,
    summarize_capture,
)
from .device_calibration import (
    load_or_create_accelerometer_calibration,
    write_accelerometer_calibration_document,
)
from .shared_memory_access import CameraSharedMemory, STREAM_ACCEL


class CalibrationSession:
    def __init__(
        self,
        *,
        mapping_name: str,
        workspace_root: Path,
        capture_seconds: float,
        provider_control_url: str,
    ) -> None:
        self.mapping_name = mapping_name
        self.workspace_root = workspace_root.resolve()
        self.capture_seconds = capture_seconds
        self.provider_control_url = provider_control_url.rstrip("/")
        self.calibration_root = (
            self.workspace_root / "config" / "calibration" / "devices"
        )
        self.lock = threading.RLock()
        self.reader: CameraSharedMemory | None = None
        self.captures: dict[str, CaptureSummary] = {}
        self.last_solution: dict[str, Any] | None = None
        self.last_write: dict[str, Any] | None = None

    def close(self) -> None:
        with self.lock:
            if self.reader is not None:
                self.reader.close()
                self.reader = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            reader = self._ensure_reader()
            header = reader.header or reader.refresh()
            sample = reader.read_imu(STREAM_ACCEL)
            calibration = self._load_calibration(header)
            corrected = None
            if sample is not None:
                corrected_vector = calibration.apply(sample.x, sample.y, sample.z)
                corrected = {
                    "x": corrected_vector[0],
                    "y": corrected_vector[1],
                    "z": corrected_vector[2],
                    "magnitude_m_s2": sum(
                        value * value for value in corrected_vector
                    )
                    ** 0.5,
                }
            return {
                "connected": True,
                "mapping_name": self.mapping_name,
                "device": {
                    "name": header.device_name,
                    "serial_number": header.device_serial,
                    "firmware_version": header.firmware_version,
                    "canonical_device_id": calibration.canonical_device_id,
                },
                "raw_accelerometer": (
                    {
                        "x": sample.x,
                        "y": sample.y,
                        "z": sample.z,
                        "magnitude_m_s2": (
                            sample.x * sample.x
                            + sample.y * sample.y
                            + sample.z * sample.z
                        )
                        ** 0.5,
                        "temperature_c": sample.temperature_c,
                        "frame_number": sample.frame_number,
                    }
                    if sample is not None
                    else None
                ),
                "corrected_accelerometer": corrected,
                "calibration": {
                    "status": calibration.status,
                    "scale": list(calibration.scale),
                    "offset": list(calibration.offset),
                    "revision": calibration.revision,
                    "path": str(calibration.path),
                },
                "capture_seconds": self.capture_seconds,
                "gravity_m_s2": STANDARD_GRAVITY_M_S2,
                "poses": list(POSE_ORDER),
                "captures": {
                    pose: {
                        **capture.to_dict(),
                        "quality": pose_quality(capture),
                    }
                    for pose, capture in self.captures.items()
                },
                "last_solution": self.last_solution,
                "last_write": self.last_write,
            }

    def capture(self, pose: str) -> dict[str, Any]:
        if pose not in POSE_ORDER:
            raise ValueError(f"unsupported pose: {pose}")
        with self.lock:
            reader = self._ensure_reader()
            latest = reader.read_imu(STREAM_ACCEL)
            after_frame = latest.frame_number if latest is not None else -1
            deadline = time.monotonic() + self.capture_seconds
            samples_by_frame: dict[int, Any] = {}

            while time.monotonic() < deadline:
                for sample in reader.recent_imu_samples(
                    STREAM_ACCEL,
                    after_frame_number=after_frame,
                ):
                    samples_by_frame[sample.frame_number] = sample
                    after_frame = max(after_frame, sample.frame_number)
                time.sleep(0.008)

            samples = [samples_by_frame[key] for key in sorted(samples_by_frame)]
            if len(samples) < 20:
                raise RuntimeError(
                    f"only {len(samples)} accelerometer samples arrived in "
                    f"{self.capture_seconds:.1f} seconds"
                )
            capture = summarize_capture(
                pose,
                [(sample.x, sample.y, sample.z) for sample in samples],
                [sample.temperature_c for sample in samples],
                duration_s=self.capture_seconds,
                first_frame_number=samples[0].frame_number,
                last_frame_number=samples[-1].frame_number,
            )
            quality = pose_quality(capture)
            if not quality["accepted"]:
                raise ValueError("; ".join(quality["warnings"]))
            self.captures[pose] = capture
            self.last_solution = None
            self.last_write = None
            return {"capture": capture.to_dict(), "quality": quality}

    def reset(self) -> dict[str, Any]:
        with self.lock:
            self.captures.clear()
            self.last_solution = None
            self.last_write = None
            return {"status": "reset"}

    def solve_and_write(self) -> dict[str, Any]:
        with self.lock:
            missing = [pose for pose in POSE_ORDER if pose not in self.captures]
            if missing:
                raise ValueError(f"capture these poses first: {', '.join(missing)}")
            reader = self._ensure_reader()
            header = reader.header or reader.refresh()
            current = self._load_calibration(header)
            solution = solve_six_position_calibration(self.captures)
            document = build_custom_calibration_document(
                current.document,
                self.captures,
                solution,
            )
            written = write_accelerometer_calibration_document(
                current.path,
                document,
                expected_device_id=current.canonical_device_id,
                backup_existing=True,
            )
            reload_result = self._request_provider_reload()
            self.last_solution = solution.to_dict()
            self.last_write = {
                "status": "written",
                "path": str(written.path),
                "revision": written.revision,
                "calibration_status": written.status,
                "provider_reload": reload_result,
            }
            return {
                "solution": self.last_solution,
                "write": self.last_write,
                "document": written.document,
            }

    def _ensure_reader(self) -> CameraSharedMemory:
        if self.reader is not None:
            try:
                self.reader.refresh()
                return self.reader
            except Exception:
                self.reader.close()
                self.reader = None
        reader = CameraSharedMemory(self.mapping_name)
        reader.open()
        self.reader = reader
        return reader

    def _load_calibration(self, header: Any):
        if not header.device_serial:
            raise RuntimeError("camera did not expose a persistent serial number")
        return load_or_create_accelerometer_calibration(
            self.calibration_root,
            manufacturer="Orbbec",
            model="Femto Bolt",
            serial_number=header.device_serial,
            firmware_version=header.firmware_version or None,
        )

    def _request_provider_reload(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.provider_control_url}/v1/control/reload-calibration",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            return {
                "status": "not_reloaded",
                "error": str(error),
                "note": "The file is saved; restart or reload the camera Provider later.",
            }


class CalibrationHandler(BaseHTTPRequestHandler):
    session: CalibrationSession
    static_root = files("orbbec_femto_provider").joinpath("calibration_web")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/status":
                self._json(HTTPStatus.OK, self.session.status())
                return
            if self.path in {"/", "/index.html"}:
                self._static("index.html", "text/html; charset=utf-8")
                return
            if self.path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
                return
            if self.path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
            if self.path == "/api/capture":
                self._json(HTTPStatus.OK, self.session.capture(str(body.get("pose"))))
                return
            if self.path == "/api/reset":
                self._json(HTTPStatus.OK, self.session.reset())
                return
            if self.path == "/api/solve-and-write":
                self._json(HTTPStatus.OK, self.session.solve_and_write())
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[AccelerometerCalibrationGUI] {format % args}", flush=True)

    def _static(self, name: str, content_type: str) -> None:
        payload = self.static_root.joinpath(name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone six-position Femto Bolt accelerometer calibration GUI."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8111)
    parser.add_argument(
        "--mapping-name",
        default=r"Local\FemtoBoltPipeline_CameraHost_v2",
    )
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--capture-seconds", type=float, default=2.0)
    parser.add_argument(
        "--provider-control-url",
        default="http://127.0.0.1:7101",
    )
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def infer_workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main() -> int:
    args = parse_args()
    if args.capture_seconds < 1.0:
        raise SystemExit("--capture-seconds must be at least 1.0")
    workspace_root = args.workspace_root or infer_workspace_root()
    session = CalibrationSession(
        mapping_name=args.mapping_name,
        workspace_root=workspace_root,
        capture_seconds=args.capture_seconds,
        provider_control_url=args.provider_control_url,
    )
    CalibrationHandler.session = session
    server = ThreadingHTTPServer((args.host, args.port), CalibrationHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Accelerometer calibration GUI: {url}")
    print("Keep this window open. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
