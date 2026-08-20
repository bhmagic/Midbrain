from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from orbbec_femto_provider.shared_memory_access import BufferRef


def _provider():
    provider_path = Path(__file__).resolve().parents[2] / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "orbbec_femto_bolt_provider_identity_entrypoint",
        provider_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Orbbec Provider from {provider_path}")
    provider_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider_module)
    provider = object.__new__(provider_module.FemtoBoltProvider)
    provider.args = SimpleNamespace(
        rgbd_max_delta_us=50_000,
        disable_frame_sync=False,
    )
    provider.calibration_revision = "calibration-7"
    provider.accelerometer_calibration = SimpleNamespace(
        canonical_device_id="orbbec:femto-bolt:CL8326300SJ"
    )
    return provider


def _reference(stream_name: str, timestamp_us: int) -> BufferRef:
    return BufferRef(
        transport="windows_named_shared_memory",
        mapping_name=r"Local\Test",
        stream_kind=0,
        stream_name=stream_name,
        pool_id=f"pool-{stream_name}",
        slot_id=0,
        generation=7,
        slot_offset=0,
        payload_offset=0,
        payload_bytes=48,
        payload_capacity_bytes=48,
        frame_number=42,
        host_qpc=1,
        device_timestamp_us=timestamp_us,
        system_timestamp_us=timestamp_us,
        global_timestamp_us=timestamp_us,
        frame_type=0,
        format=0,
        format_name="RGB",
        width=4,
        height=4,
        stride_bytes=12,
        bytes_per_pixel=3,
        depth_value_scale_mm=0.0,
        flags=0,
        metadata_mask=0,
        frame_metadata={},
        note="test",
    )


def test_rgbd_bundle_carries_activation_grade_camera_identity() -> None:
    bundle = _provider()._rgbd_bundle(
        _reference("color", 100),
        _reference("depth", 110),
        None,
    )
    assert bundle["canonical_device_id"] == "orbbec:femto-bolt:CL8326300SJ"
    assert bundle["calibration_revision"] == "calibration-7"
