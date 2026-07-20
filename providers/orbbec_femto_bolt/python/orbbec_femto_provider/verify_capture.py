from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from array import array
from pathlib import Path

import httpx
from PIL import Image

from .shared_memory_access import CameraSharedMemory


def _stream_endpoint(fabric_url: str, stream: str) -> str:
    return f"{fabric_url.rstrip('/')}/v1/latest/{stream}"


def read_fresh_payload(
    client: httpx.Client,
    fabric_url: str,
    stream: str,
    deadline: float,
    poll_seconds: float,
) -> tuple[dict, bytes]:
    """Fetch a current BufferRef and copy its payload before slot reuse."""
    endpoint = _stream_endpoint(fabric_url, stream)
    reader: CameraSharedMemory | None = None
    reader_mapping_name: str | None = None
    last_error: Exception | None = None

    try:
        while time.monotonic() < deadline:
            try:
                response = client.get(endpoint, timeout=3.0)
                if response.status_code == 404:
                    last_error = RuntimeError(f"{stream} is not available yet")
                    time.sleep(poll_seconds)
                    continue
                response.raise_for_status()
                observation = response.json()
                ref = observation["data"]
                mapping_name = str(ref["mapping_name"])

                if reader is None or reader_mapping_name != mapping_name:
                    if reader is not None:
                        reader.close()
                    reader = CameraSharedMemory(mapping_name).open()
                    reader_mapping_name = mapping_name
                    continue

                try:
                    return ref, reader.read_ref(ref)
                except RuntimeError as error:
                    last_error = error
                    time.sleep(0.003)
            except httpx.ConnectError as error:
                last_error = RuntimeError(
                    f"World State Fabric is not running at {fabric_url}"
                )
                last_error.__cause__ = error
                time.sleep(poll_seconds)
            except Exception as error:
                last_error = error
                time.sleep(poll_seconds)
    finally:
        if reader is not None:
            reader.close()

    raise RuntimeError(f"timed out reading {stream}: {last_error}")



def latest_observation(
    client: httpx.Client,
    fabric_url: str,
    stream: str,
    required: bool = True,
) -> dict | None:
    response = client.get(_stream_endpoint(fabric_url, stream), timeout=5.0)
    if response.status_code == 404 and not required:
        return None
    response.raise_for_status()
    return response.json()


def provider_capabilities(client: httpx.Client, manager_url: str) -> list[dict]:
    response = client.get(f"{manager_url.rstrip('/')}/v1/capabilities", timeout=5.0)
    response.raise_for_status()
    return [
        item
        for item in response.json()
        if item.get("provider_id") == "camera.femto_bolt"
    ]

def capture_rgb(ref: dict, payload: bytes, output: Path) -> None:
    fmt = str(ref.get("format_name", "")).upper()
    if fmt in {"MJPG", "MJPEG", "JPEG", "JPG"}:
        output.write_bytes(payload)
        return
    width, height = int(ref["width"]), int(ref["height"])
    if fmt == "RGB":
        image = Image.frombytes("RGB", (width, height), payload)
    elif fmt == "BGR":
        image = Image.frombytes("RGB", (width, height), payload, "raw", "BGR")
    else:
        raise RuntimeError(f"unsupported color format: {fmt}")
    image.save(output, format="JPEG", quality=92)


def capture_scalar_image(ref: dict, payload: bytes, output: Path) -> None:
    fmt = str(ref.get("format_name", "")).upper()
    width, height = int(ref["width"]), int(ref["height"])
    if fmt == "Y8":
        expected = width * height
        if len(payload) < expected:
            raise RuntimeError(f"Y8 payload is short: {len(payload)} < {expected}")
        Image.frombytes("L", (width, height), payload[:expected]).save(output, "PNG")
        return
    if fmt != "Y16" or int(ref.get("bytes_per_pixel", 0)) != 2:
        raise RuntimeError(f"unsupported scalar image format: {fmt}")

    expected = width * height * 2
    if len(payload) < expected:
        raise RuntimeError(f"Y16 payload is short: {len(payload)} < {expected}")
    values = array("H")
    values.frombytes(payload[:expected])
    if sys.byteorder != "little":
        values.byteswap()
    valid = [value for value in values if value > 0]
    near = max(min(valid), 100) if valid else 100
    far = min(max(valid), 6000) if valid else 6000
    if far <= near:
        far = near + 1
    scale = 255.0 / float(far - near)
    rendered = bytes(
        0 if value == 0 else max(0, min(255, int((far - value) * scale)))
        for value in values
    )
    Image.frombytes("L", (width, height), rendered).save(output, "PNG")


def capture_point_cloud(
    ref: dict,
    payload: bytes,
    output: Path,
    max_points: int,
) -> dict:
    bytes_per_point = int(ref.get("bytes_per_pixel", 0))
    if bytes_per_point < 12:
        point_count = int(ref.get("width", 0)) * max(int(ref.get("height", 1)), 1)
        bytes_per_point = len(payload) // point_count if point_count else 0
    if bytes_per_point < 12:
        raise RuntimeError(f"invalid point-cloud point size: {bytes_per_point}")

    total_points = len(payload) // bytes_per_point
    step = max(1, math.ceil(total_points / max(max_points, 1)))
    points: list[tuple[float, float, float]] = []
    for index in range(0, total_points, step):
        offset = index * bytes_per_point
        x, y, z = struct.unpack_from("<fff", payload, offset)
        if not all(math.isfinite(value) for value in (x, y, z)):
            continue
        if x == 0.0 and y == 0.0 and z == 0.0:
            continue
        points.append((x, y, z))

    with output.open("w", encoding="ascii", newline="\n") as handle:
        handle.write("ply\n")
        handle.write("format ascii 1.0\n")
        handle.write(f"element vertex {len(points)}\n")
        handle.write("property float x\nproperty float y\nproperty float z\n")
        handle.write("end_header\n")
        for x, y, z in points:
            handle.write(f"{x:.6f} {y:.6f} {z:.6f}\n")

    return {
        "format_name": ref.get("format_name"),
        "bytes_per_point": bytes_per_point,
        "source_points": total_points,
        "written_points": len(points),
        "decimation_step": step,
        "units": "millimeters",
    }


def verify_sync(client: httpx.Client, fabric_url: str) -> dict:
    response = client.get(
        f"{fabric_url.rstrip('/')}/v1/sync",
        params={
            "streams": "camera.rgb.frame_ref,camera.depth.frame_ref,camera.imu.accel,camera.imu.gyro",
            "anchor_stream": "camera.rgb.frame_ref",
            "max_delta_us": 100_000,
            "require_all": "false",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--wait-seconds", type=float, default=45.0)
    parser.add_argument("--poll-seconds", type=float, default=0.2)
    parser.add_argument("--max-ply-points", type=int, default=50_000)
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {}

    def new_deadline() -> float:
        return time.monotonic() + args.wait_seconds

    with httpx.Client() as client:
        captures = (
            ("camera.rgb.frame_ref", "verify_rgb.jpg", capture_rgb),
            ("camera.depth.frame_ref", "verify_depth.png", capture_scalar_image),
            ("camera.ir.frame_ref", "verify_ir.png", capture_scalar_image),
            (
                "camera.depth_aligned_to_rgb.frame_ref",
                "verify_depth_aligned_to_rgb.png",
                capture_scalar_image,
            ),
        )
        for stream, filename, writer in captures:
            ref, payload = read_fresh_payload(
                client,
                args.fabric_url,
                stream,
                new_deadline(),
                args.poll_seconds,
            )
            writer(ref, payload, out / filename)
            frame_metadata = ref.get("frame_metadata") or {}
            summary[stream] = {
                "file": filename,
                "frame_number": ref.get("frame_number"),
                "format": ref.get("format_name"),
                "width": ref.get("width"),
                "height": ref.get("height"),
                "payload_bytes": len(payload),
                "global_timestamp_us": ref.get("global_timestamp_us"),
                "frame_metadata_count": len(frame_metadata),
                "frame_metadata": frame_metadata,
                "flags": ref.get("flags"),
                "note": ref.get("note"),
            }

        point_stream = "camera.point_cloud.xyz.frame_ref"
        try:
            point_ref, point_payload = read_fresh_payload(
                client,
                args.fabric_url,
                point_stream,
                new_deadline(),
                args.poll_seconds,
            )
        except RuntimeError:
            point_stream = "camera.point_cloud.xyzrgb.frame_ref"
            point_ref, point_payload = read_fresh_payload(
                client,
                args.fabric_url,
                point_stream,
                new_deadline(),
                args.poll_seconds,
            )
        summary[point_stream] = capture_point_cloud(
            point_ref,
            point_payload,
            out / "verify_point_cloud.ply",
            args.max_ply_points,
        )
        summary[point_stream]["file"] = "verify_point_cloud.ply"
        summary[point_stream]["global_timestamp_us"] = point_ref.get("global_timestamp_us")
        summary[point_stream]["note"] = point_ref.get("note")

        summary["fabric_sync"] = verify_sync(client, args.fabric_url)
        streams_response = client.get(
            f"{args.fabric_url.rstrip('/')}/v1/streams",
            timeout=5.0,
        )
        streams_response.raise_for_status()
        summary["fabric_stream_catalog"] = [
            item
            for item in streams_response.json()
            if item.get("provider_id") == "camera.femto_bolt"
        ]
        summary["manager_capabilities"] = provider_capabilities(
            client,
            args.manager_url,
        )
        for stream in (
            "camera.calibration",
            "camera.device_info",
            "camera.imu.accel",
            "camera.imu.gyro",
            "camera.imu.bundle",
        ):
            observation = latest_observation(
                client,
                args.fabric_url,
                stream,
                required=False,
            )
            if observation is not None:
                summary[stream] = observation

    summary_path = out / "verify_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for filename in (
        "verify_rgb.jpg",
        "verify_depth.png",
        "verify_ir.png",
        "verify_depth_aligned_to_rgb.png",
        "verify_point_cloud.ply",
        "verify_summary.json",
    ):
        print(out / filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
