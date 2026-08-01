from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import mmap
import os
import shutil
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
from orbbec_femto_provider.shared_memory_access import (
    FRAME_METADATA_NAMES,
    FRAME_METADATA_VALUES_V2,
    FRAME_PREFIX_V2,
    HEADER_BYTES,
    MAGIC,
    SLOT_HEADER_BYTES_V2,
    STREAM_DESCRIPTOR,
    STREAM_KIND_NAMES,
    CameraSharedMemory,
    _HEADER_PREFIX,
    _HEADER_V2_STREAM_TABLE_OFFSET,
)
from spatial_registration_rgbd import register_rgbd_point

from .phase4_policy import (
    await_with_progress_heartbeat,
    report_operation_progress,
)


BUNDLE_SCHEMA = "physical_agent.phase5_replay_bundle"
BUNDLE_SCHEMA_VERSION = 1
REPLAY_TRANSPORT = "windows_named_shared_memory"
DEFAULT_MAX_TOTAL_PAYLOAD_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_METADATA_BYTES = 8 * 1024 * 1024
DEFAULT_RETENTION_REVIEW_DAYS = 30
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "cookie",
    "set-cookie",
}


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _redact(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            output[str(key)] = (
                "[REDACTED]"
                if normalized in _SECRET_KEYS
                else _redact(item, parent_key=normalized)
            )
        return output
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, parent_key=parent_key) for item in value]
    return value


def _safe_label(label: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in label.strip()
    )
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"invalid replay record label: {label!r}")
    return normalized[:120]


def _write_fixed_text(target: bytearray, offset: int, size: int, value: str) -> None:
    encoded = value.encode("utf-8")[: max(0, size - 1)]
    target[offset : offset + len(encoded)] = encoded


def _replace_mapping_names(value: Any, mapping_names: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _replace_mapping_names(item, mapping_names)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_mapping_names(item, mapping_names) for item in value]
    if isinstance(value, str):
        return mapping_names.get(value, value)
    return value


@dataclass(frozen=True)
class ReplayMaterialization:
    bundle_id: str
    route_set: dict[str, Any]
    references: dict[str, dict[str, Any]]
    records: dict[str, Any]
    artifacts: dict[str, Path]
    replay_policy: dict[str, Any]


@dataclass(frozen=True)
class ReplayScenario:
    name: str
    expected_outcome: str
    materialization: ReplayMaterialization
    injected_state: dict[str, Any]


class Phase5ReplayScenarioRunner:
    """Inject deterministic control-plane and BufferRef failures in replay."""

    SCENARIOS = {
        "success",
        "stale_rgb",
        "stale_depth",
        "independent_channel_rates",
        "flexible_channel_geometry",
        "recycled_bufferref",
        "alignment_revision_change",
        "provider_restart",
        "vio_epoch_change",
        "stale_world_transform",
        "rejected_controller_preview",
        "lease_loss",
        "stale_fencing_generation",
        "audit_failure",
        "manager_loss",
        "fabric_loss",
    }

    @classmethod
    def apply(
        cls,
        materialization: ReplayMaterialization,
        scenario_name: str,
    ) -> ReplayScenario:
        name = str(scenario_name).strip().lower()
        if name not in cls.SCENARIOS:
            raise ValueError(f"unsupported Phase 5 replay scenario: {name}")
        references = copy.deepcopy(materialization.references)
        records = copy.deepcopy(materialization.records)
        injected: dict[str, Any] = {"scenario": name}
        expected = "PASS"

        if name == "stale_rgb":
            reference = references.get("rgb")
            if not isinstance(reference, dict):
                raise ValueError("stale_rgb scenario requires an RGB reference")
            current = max(
                int(item.get("global_timestamp_us") or 0)
                for item in references.values()
            ) + 10_000_000
            stale = max(1, current - 10_000_000)
            reference["global_timestamp_us"] = stale
            reference["system_timestamp_us"] = stale
            injected["rgb_timestamp_us"] = stale
            injected["current_timestamp_us"] = current
            injected["maximum_channel_skew_us"] = 250_000
            expected = "REJECT_STALE_SENSOR_DATA"
        elif name == "stale_depth":
            reference = references.get("registered_depth")
            if not isinstance(reference, dict):
                raise ValueError(
                    "stale_depth scenario requires a registered-depth reference"
                )
            current = max(
                int(item.get("global_timestamp_us") or 0)
                for item in references.values()
            ) + 10_000_000
            stale = max(1, current - 10_000_000)
            reference["global_timestamp_us"] = stale
            reference["system_timestamp_us"] = stale
            injected["depth_timestamp_us"] = stale
            injected["current_timestamp_us"] = current
            injected["maximum_channel_skew_us"] = 250_000
            expected = "REJECT_STALE_SENSOR_DATA"
        elif name == "independent_channel_rates":
            rgb = references.get("rgb")
            depth = references.get("registered_depth")
            if not isinstance(rgb, dict) or not isinstance(depth, dict):
                raise ValueError(
                    "independent_channel_rates requires RGB and registered depth"
                )
            depth_timestamp = int(
                depth.get("global_timestamp_us")
                or depth.get("system_timestamp_us")
                or depth.get("device_timestamp_us")
                or 0
            )
            rgb["frame_number"] = int(depth.get("frame_number") or 0) + 4
            rgb["global_timestamp_us"] = depth_timestamp + 25_000
            rgb["system_timestamp_us"] = depth_timestamp + 25_000
            injected["maximum_channel_skew_us"] = 50_000
            expected = "PASS"
        elif name == "flexible_channel_geometry":
            rgb = references.get("rgb")
            native_depth = references.get("native_depth")
            registered_depth = references.get("registered_depth")
            if (
                not isinstance(rgb, dict)
                or not isinstance(native_depth, dict)
                or not isinstance(registered_depth, dict)
            ):
                raise ValueError(
                    "flexible_channel_geometry requires RGB, native depth, "
                    "and registered depth"
                )
            native_depth["width"] = 2
            native_depth["height"] = 4
            native_depth["stride_bytes"] = 4
            injected["geometry_policy"] = {
                "same_resolution_not_required": True,
                "same_aspect_ratio_not_required": True,
                "same_boundary_not_required": True,
                "registered_depth_matches_rgb_grid": True,
            }
            expected = "PASS"
        elif name == "recycled_bufferref":
            label = "rgb" if "rgb" in references else next(iter(references))
            references[label]["generation"] = (
                int(references[label]["generation"]) + 2
            )
            injected["reference_label"] = label
            expected = "REJECT_RECYCLED_BUFFERREF"
        elif name == "alignment_revision_change":
            records["alignment_state"] = {
                "validity": "STALE",
                "reason": "ALIGNMENT_REVISION_CHANGED",
                "selected_revision": "replay-alignment-before",
                "observed_revision": "replay-alignment-after",
            }
            expected = "RECAPTURE_REQUIRED"
        elif name == "provider_restart":
            records["binding_state"] = {
                "validity": "STALE",
                "reason": "PROVIDER_BOOT_ID_CHANGED",
                "selected_boot_id": "replay-boot-before",
                "observed_boot_id": "replay-boot-after",
            }
            expected = "REBIND_REQUIRED"
        elif name == "vio_epoch_change":
            records["transform_state"] = {
                "validity": "STALE",
                "reason": "VIO_EPOCH_CHANGED",
                "selected_epoch": "replay-epoch-before",
                "observed_epoch": "replay-epoch-after",
            }
            expected = "RECALIBRATION_REQUIRED"
        elif name == "stale_world_transform":
            records["transform_state"] = {
                "validity": "STALE",
                "reason": "WORLD_TRANSFORM_EXPIRED",
            }
            expected = "RECALIBRATION_REQUIRED"
        elif name == "rejected_controller_preview":
            records["controller_preview_state"] = {
                "planning_valid": False,
                "collision_free": False,
                "physical_execution_blockers": [
                    "REPLAY_INJECTED_COLLISION",
                ],
                "preview_id": "replay-rejected-preview",
            }
            expected = "AUTHORIZATION_NOT_CREATED"
        elif name == "lease_loss":
            records["lease_state"] = {
                "active": False,
                "state": "EXPIRED",
                "reason": "REPLAY_INJECTED_LEASE_LOSS",
            }
            expected = "MOTION_INHIBITED"
        elif name == "stale_fencing_generation":
            records["lease_state"] = {
                "active": True,
                "state": "ACTIVE",
                "selected_fencing_generation": 7,
                "observed_fencing_generation": 8,
                "reason": "REPLAY_INJECTED_STALE_FENCING_GENERATION",
            }
            expected = "MOTION_INHIBITED"
        elif name == "audit_failure":
            records["control_audit_state"] = {
                "mode": "STRICT_LOCAL",
                "submitted_persisted": False,
                "reason": "REPLAY_INJECTED_LOCAL_WRITE_FAILURE",
            }
            expected = "CONTROLLED_TARGET_NOT_CALLED"
        elif name == "manager_loss":
            records["manager_state"] = {
                "available": False,
                "reason": "REPLAY_INJECTED_MANAGER_LOSS",
            }
            expected = "BINDING_OR_AUTHORITY_UNAVAILABLE"
        elif name == "fabric_loss":
            records["fabric_state"] = {
                "available": False,
                "reason": "REPLAY_INJECTED_FABRIC_LOSS",
            }
            expected = "OBSERVATION_UNAVAILABLE_BASIC_SUPPORT_UNCHANGED"

        injected["expected_outcome"] = expected
        records["scenario_injection"] = copy.deepcopy(injected)
        scenario_materialization = ReplayMaterialization(
            bundle_id=materialization.bundle_id,
            route_set=copy.deepcopy(materialization.route_set),
            references=references,
            records=records,
            artifacts=copy.deepcopy(materialization.artifacts),
            replay_policy=copy.deepcopy(materialization.replay_policy),
        )
        return ReplayScenario(
            name=name,
            expected_outcome=expected,
            materialization=scenario_materialization,
            injected_state=injected,
        )

    @staticmethod
    def evaluate(scenario: ReplayScenario) -> dict[str, Any]:
        """Evaluate replay-only safety gates from the injected observable state."""

        name = scenario.name
        materialization = scenario.materialization
        references = materialization.references
        records = materialization.records
        evidence: dict[str, Any] = {}
        outcome = "PASS"

        if name in {"stale_rgb", "stale_depth"}:
            label = "rgb" if name == "stale_rgb" else "registered_depth"
            reference = references[label]
            observed = int(
                reference.get("global_timestamp_us")
                or reference.get("system_timestamp_us")
                or reference.get("device_timestamp_us")
                or 0
            )
            current = int(scenario.injected_state["current_timestamp_us"])
            maximum_skew = int(
                scenario.injected_state["maximum_channel_skew_us"]
            )
            age_us = current - observed
            evidence = {
                "reference_label": label,
                "observed_timestamp_us": observed,
                "current_timestamp_us": current,
                "age_us": age_us,
                "maximum_channel_skew_us": maximum_skew,
            }
            if age_us > maximum_skew:
                outcome = "REJECT_STALE_SENSOR_DATA"
        elif name == "independent_channel_rates":
            rgb = references["rgb"]
            depth = references["registered_depth"]
            rgb_timestamp = int(
                rgb.get("global_timestamp_us")
                or rgb.get("system_timestamp_us")
                or rgb.get("device_timestamp_us")
                or 0
            )
            depth_timestamp = int(
                depth.get("global_timestamp_us")
                or depth.get("system_timestamp_us")
                or depth.get("device_timestamp_us")
                or 0
            )
            skew_us = abs(rgb_timestamp - depth_timestamp)
            maximum_skew = int(
                scenario.injected_state["maximum_channel_skew_us"]
            )
            independent_frames = int(rgb["frame_number"]) != int(
                depth["frame_number"]
            )
            evidence = {
                "rgb_frame_number": int(rgb["frame_number"]),
                "depth_frame_number": int(depth["frame_number"]),
                "independent_frames": independent_frames,
                "timestamp_skew_us": skew_us,
                "maximum_channel_skew_us": maximum_skew,
            }
            if not independent_frames or skew_us > maximum_skew:
                outcome = "REJECT_CHANNEL_SYNCHRONIZATION"
        elif name == "flexible_channel_geometry":
            rgb = references["rgb"]
            native_depth = references["native_depth"]
            registered_depth = references["registered_depth"]
            policy = scenario.injected_state["geometry_policy"]
            rgb_grid = (int(rgb["width"]), int(rgb["height"]))
            native_grid = (
                int(native_depth["width"]),
                int(native_depth["height"]),
            )
            registered_grid = (
                int(registered_depth["width"]),
                int(registered_depth["height"]),
            )
            resolution_differs = rgb_grid != native_grid
            aspect_differs = (
                rgb_grid[0] * native_grid[1]
                != native_grid[0] * rgb_grid[1]
            )
            registered_matches_rgb = registered_grid == rgb_grid
            accepted = (
                bool(policy["same_resolution_not_required"])
                and bool(policy["same_aspect_ratio_not_required"])
                and bool(policy["same_boundary_not_required"])
                and bool(policy["registered_depth_matches_rgb_grid"])
                and resolution_differs
                and aspect_differs
                and registered_matches_rgb
            )
            evidence = {
                "rgb_grid": list(rgb_grid),
                "native_depth_grid": list(native_grid),
                "registered_depth_grid": list(registered_grid),
                "resolution_differs": resolution_differs,
                "aspect_ratio_differs": aspect_differs,
                "registered_depth_matches_rgb": registered_matches_rgb,
                "policy": copy.deepcopy(policy),
            }
            if not accepted:
                outcome = "REJECT_CHANNEL_GEOMETRY"
        elif name == "alignment_revision_change":
            state = records["alignment_state"]
            evidence = copy.deepcopy(state)
            if (
                state.get("selected_revision")
                != state.get("observed_revision")
            ):
                outcome = "RECAPTURE_REQUIRED"
        elif name == "provider_restart":
            state = records["binding_state"]
            evidence = copy.deepcopy(state)
            if state.get("selected_boot_id") != state.get("observed_boot_id"):
                outcome = "REBIND_REQUIRED"
        elif name in {"vio_epoch_change", "stale_world_transform"}:
            state = records["transform_state"]
            evidence = copy.deepcopy(state)
            if state.get("validity") != "CURRENT":
                outcome = "RECALIBRATION_REQUIRED"
        elif name == "rejected_controller_preview":
            state = records["controller_preview_state"]
            evidence = copy.deepcopy(state)
            if (
                not bool(state.get("planning_valid"))
                or not bool(state.get("collision_free"))
                or bool(state.get("physical_execution_blockers"))
            ):
                outcome = "AUTHORIZATION_NOT_CREATED"
        elif name == "lease_loss":
            state = records["lease_state"]
            evidence = copy.deepcopy(state)
            if not bool(state.get("active")) or state.get("state") != "ACTIVE":
                outcome = "MOTION_INHIBITED"
        elif name == "stale_fencing_generation":
            state = records["lease_state"]
            evidence = copy.deepcopy(state)
            if state.get("selected_fencing_generation") != state.get(
                "observed_fencing_generation"
            ):
                outcome = "MOTION_INHIBITED"
        elif name == "audit_failure":
            state = records["control_audit_state"]
            evidence = copy.deepcopy(state)
            if (
                state.get("mode") == "STRICT_LOCAL"
                and not bool(state.get("submitted_persisted"))
            ):
                outcome = "CONTROLLED_TARGET_NOT_CALLED"
        elif name == "manager_loss":
            state = records["manager_state"]
            evidence = copy.deepcopy(state)
            if not bool(state.get("available")):
                outcome = "BINDING_OR_AUTHORITY_UNAVAILABLE"
        elif name == "fabric_loss":
            state = records["fabric_state"]
            evidence = copy.deepcopy(state)
            if not bool(state.get("available")):
                outcome = "OBSERVATION_UNAVAILABLE_BASIC_SUPPORT_UNCHANGED"

        return {
            "outcome": outcome,
            "rejected": outcome != "PASS",
            "evidence": evidence,
            "physical_controller_called": False,
            "physical_lease_acquired": False,
            "hardware_provider_started": False,
        }


class ReplaySharedMemoryMapping:
    """Own a replay-only CameraHost-compatible named shared-memory mapping."""

    def __init__(
        self,
        *,
        mapping_name: str,
        records: Iterable[tuple[str, dict[str, Any], bytes]],
    ):
        if os.name != "nt":
            raise OSError("Phase 5 shared-memory replay currently requires Windows")
        self.mapping_name = mapping_name
        self._records = list(records)
        if not self._records:
            raise ValueError("replay mapping requires at least one BufferRef")

        stream_kinds: set[int] = set()
        for label, reference, _payload in self._records:
            stream_kind = int(reference["stream_kind"])
            if stream_kind in stream_kinds:
                raise ValueError(
                    "a point-in-time replay mapping cannot contain duplicate "
                    f"stream kind {stream_kind}; duplicate label={label!r}"
                )
            stream_kinds.add(stream_kind)

        self._mm: mmap.mmap | None = None
        self.references: dict[str, dict[str, Any]] = {}
        self._create()

    def _create(self) -> None:
        stream_layouts: list[dict[str, Any]] = []
        next_offset = HEADER_BYTES
        for label, reference, payload in self._records:
            capacity = max(64, ((len(payload) + 63) // 64) * 64)
            stride = SLOT_HEADER_BYTES_V2 + capacity
            stream_layouts.append(
                {
                    "label": label,
                    "reference": reference,
                    "payload": payload,
                    "capacity": capacity,
                    "stride": stride,
                    "base_offset": next_offset,
                }
            )
            next_offset += stride

        total_bytes = next_offset
        image = bytearray(total_bytes)
        now_us = int(time.time() * 1_000_000)
        _HEADER_PREFIX.pack_into(
            image,
            0,
            MAGIC,
            2,
            len(stream_layouts),
            total_bytes,
            HEADER_BYTES,
            0,
            1_000_000_000.0,
            now_us,
            os.getpid(),
        )
        offset = _HEADER_PREFIX.size
        _write_fixed_text(image, offset, 128, self.mapping_name)
        offset += 128
        _write_fixed_text(image, offset, 256, "Midbrain Phase 5 Replay")
        offset += 256
        _write_fixed_text(image, offset, 256, "REPLAY")
        offset += 256
        _write_fixed_text(image, offset, 64, "phase5-replay-v1")
        offset += 64
        _write_fixed_text(image, offset, 64, "REPLAY")
        offset += 64
        _write_fixed_text(image, offset, 32, "REPLAY_ONLY")
        offset += 32
        _write_fixed_text(image, offset, 128, f"replay:{self.mapping_name}")
        offset += 128
        struct.pack_into("<IIII", image, offset, 0, 0, 0, 0)

        for index, layout in enumerate(stream_layouts):
            reference = layout["reference"]
            stream_kind = int(reference["stream_kind"])
            stream_name = str(
                reference.get("stream_name")
                or STREAM_KIND_NAMES.get(stream_kind, f"stream_{stream_kind}")
            )
            name_bytes = stream_name.encode("utf-8")[:31]
            name_field = name_bytes + b"\x00" * (32 - len(name_bytes))
            STREAM_DESCRIPTOR.pack_into(
                image,
                _HEADER_V2_STREAM_TABLE_OFFSET + index * STREAM_DESCRIPTOR.size,
                name_field,
                stream_kind,
                0,
                1,
                0,
                int(layout["stride"]),
                int(layout["capacity"]),
                int(layout["base_offset"]),
                0,
                int(reference.get("frame_number") or 0),
                0,
            )

            slot_offset = int(layout["base_offset"])
            payload_offset = slot_offset + SLOT_HEADER_BYTES_V2
            generation = 2
            metadata = reference.get("frame_metadata")
            metadata_values = tuple(
                int((metadata or {}).get(name, 0))
                for name in FRAME_METADATA_NAMES
            )
            metadata_mask = int(reference.get("metadata_mask") or 0)
            FRAME_PREFIX_V2.pack_into(
                image,
                slot_offset,
                generation,
                int(reference.get("frame_number") or 0),
                int(reference.get("host_qpc") or 0),
                int(reference.get("device_timestamp_us") or 0),
                int(reference.get("system_timestamp_us") or 0),
                int(reference.get("global_timestamp_us") or 0),
                stream_kind,
                0,
                int(reference.get("frame_type") or 0),
                int(reference.get("format") or 0),
                int(reference.get("width") or 0),
                int(reference.get("height") or 0),
                int(reference.get("stride_bytes") or 0),
                int(reference.get("bytes_per_pixel") or 0),
                len(layout["payload"]),
                float(reference.get("depth_value_scale_mm") or 0.0),
                int(reference.get("flags") or 0),
                metadata_mask,
            )
            FRAME_METADATA_VALUES_V2.pack_into(
                image,
                slot_offset + FRAME_PREFIX_V2.size,
                *metadata_values,
            )
            _write_fixed_text(
                image,
                slot_offset + 376,
                32,
                str(reference.get("format_name") or ""),
            )
            _write_fixed_text(
                image,
                slot_offset + 408,
                64,
                f"REPLAY:{reference.get('note') or ''}",
            )
            image[
                payload_offset : payload_offset + len(layout["payload"])
            ] = layout["payload"]
            self.references[str(layout["label"])] = {
                **copy.deepcopy(reference),
                "transport": REPLAY_TRANSPORT,
                "mapping_name": self.mapping_name,
                "pool_id": f"{self.mapping_name}:{stream_name}",
                "slot_id": 0,
                "generation": generation,
                "slot_offset": slot_offset,
                "payload_offset": payload_offset,
                "payload_bytes": len(layout["payload"]),
                "payload_capacity_bytes": int(layout["capacity"]),
                "note": f"REPLAY:{reference.get('note') or ''}",
                "replay_original_mapping_name": reference.get("mapping_name"),
                "replay_original_generation": reference.get("generation"),
            }

        mapped = mmap.mmap(
            -1,
            total_bytes,
            tagname=self.mapping_name,
            access=mmap.ACCESS_WRITE,
        )
        mapped.seek(0)
        mapped.write(image)
        mapped.flush()
        self._mm = mapped

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None

    def __enter__(self) -> "ReplaySharedMemoryMapping":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class Phase5ReplayBundle:
    def __init__(self, bundle_directory: Path, manifest: dict[str, Any]):
        self.bundle_directory = bundle_directory
        self.manifest = manifest
        self._mappings: list[ReplaySharedMemoryMapping] = []

    @classmethod
    def load(cls, bundle_directory: Path) -> "Phase5ReplayBundle":
        directory = bundle_directory.resolve()
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"replay manifest is missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != BUNDLE_SCHEMA:
            raise ValueError("unsupported replay bundle schema")
        if int(manifest.get("schema_version") or 0) != BUNDLE_SCHEMA_VERSION:
            raise ValueError("unsupported replay bundle schema version")
        policy = manifest.get("replay_policy")
        if not isinstance(policy, dict) or any(
            bool(policy.get(field))
            for field in (
                "hardware_provider_start_allowed",
                "physical_lease_allowed",
                "physical_controller_call_allowed",
                "agent_physical_execution_allowed",
            )
        ):
            raise ValueError("replay bundle does not enforce hardware isolation")

        for entry in manifest.get("payloads", {}).values():
            cls._verify_file_entry(directory, entry)
        for entry in manifest.get("artifacts", {}).values():
            cls._verify_file_entry(directory, entry)
        return cls(directory, manifest)

    @staticmethod
    def _verify_file_entry(directory: Path, entry: Any) -> None:
        if not isinstance(entry, dict):
            raise ValueError("invalid replay file entry")
        relative = Path(str(entry.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("replay file path escapes the bundle")
        path = directory / relative
        payload = path.read_bytes()
        if len(payload) != int(entry.get("bytes") or -1):
            raise ValueError(f"replay file size mismatch: {relative}")
        if _sha256_bytes(payload) != entry.get("sha256"):
            raise ValueError(f"replay file hash mismatch: {relative}")

    def materialize(self) -> ReplayMaterialization:
        if self._mappings:
            raise RuntimeError("replay bundle is already materialized")
        payload_entries = self.manifest.get("payloads", {})
        by_mapping: dict[str, list[tuple[str, dict[str, Any], bytes]]] = {}
        for label, entry in payload_entries.items():
            reference = copy.deepcopy(entry["original_reference"])
            original_mapping = str(reference.get("mapping_name") or "")
            if not original_mapping:
                raise ValueError(f"BufferRef {label!r} has no original mapping")
            payload = (self.bundle_directory / entry["path"]).read_bytes()
            by_mapping.setdefault(original_mapping, []).append(
                (str(label), reference, payload)
            )

        bundle_id = str(self.manifest["bundle_id"])
        mapping_names: dict[str, str] = {}
        replay_refs: dict[str, dict[str, Any]] = {}
        for index, (original_name, records) in enumerate(
            sorted(by_mapping.items())
        ):
            replay_name = (
                "Local\\MidbrainPhase5Replay_"
                f"{_safe_label(bundle_id)}_{index}_{uuid.uuid4().hex[:8]}"
            )
            mapping = ReplaySharedMemoryMapping(
                mapping_name=replay_name,
                records=records,
            )
            self._mappings.append(mapping)
            mapping_names[original_name] = replay_name
            replay_refs.update(mapping.references)

        route_set = _replace_mapping_names(
            copy.deepcopy(self.manifest.get("route_set") or {}),
            mapping_names,
        )
        route_set["replay"] = {
            "active": True,
            "bundle_id": bundle_id,
            "namespace": "REPLAY",
            "hardware_access_allowed": False,
            "original_mapping_names": sorted(mapping_names),
            "mapping_names": sorted(mapping_names.values()),
        }
        artifacts = {
            str(label): self.bundle_directory / str(entry["path"])
            for label, entry in self.manifest.get("artifacts", {}).items()
        }
        return ReplayMaterialization(
            bundle_id=bundle_id,
            route_set=route_set,
            references=replay_refs,
            records=copy.deepcopy(self.manifest.get("records") or {}),
            artifacts=artifacts,
            replay_policy=copy.deepcopy(self.manifest["replay_policy"]),
        )

    def provenance_summary(self) -> dict[str, Any]:
        records = self.manifest.get("records")
        records = records if isinstance(records, dict) else {}
        fabric = records.get("fabric")
        fabric = fabric if isinstance(fabric, dict) else {}
        optional_streams = fabric.get("optional_streams")
        optional_streams = (
            optional_streams if isinstance(optional_streams, dict) else {}
        )
        route_set = self.manifest.get("route_set")
        route_set = route_set if isinstance(route_set, dict) else {}
        routes = route_set.get("routes")
        routes = routes if isinstance(routes, list) else []

        route_summaries = []
        provider_identities: set[tuple[str, str, str]] = set()
        for route in routes:
            if not isinstance(route, dict):
                continue
            provider_identity = (
                str(route.get("provider_id") or ""),
                str(route.get("provider_instance_id") or ""),
                str(route.get("boot_id") or ""),
            )
            if any(provider_identity):
                provider_identities.add(provider_identity)
            route_summaries.append(
                {
                    "route_id": str(route.get("route_id") or ""),
                    "capability": str(route.get("capability") or ""),
                    "available": bool(route.get("available", True)),
                    "hardware_specific": bool(route.get("hardware_specific")),
                    "provider_id": provider_identity[0],
                    "provider_instance_id": provider_identity[1],
                    "boot_id": provider_identity[2],
                }
            )

        payload_summaries: dict[str, dict[str, Any]] = {}
        for label, entry in self.manifest.get("payloads", {}).items():
            reference = entry.get("original_reference")
            reference = reference if isinstance(reference, dict) else {}
            payload_summaries[str(label)] = {
                "bytes": int(entry.get("bytes") or 0),
                "sha256": str(entry.get("sha256") or ""),
                "format_name": str(reference.get("format_name") or ""),
                "grid": {
                    "width": int(reference.get("width") or 0),
                    "height": int(reference.get("height") or 0),
                    "stride_bytes": int(reference.get("stride_bytes") or 0),
                },
                "frame_number": int(reference.get("frame_number") or 0),
                "global_timestamp_us": int(
                    reference.get("global_timestamp_us") or 0
                ),
                "generation": int(reference.get("generation") or 0),
            }

        calibration_observation = optional_streams.get("camera.calibration")
        calibration_data = (
            calibration_observation.get("data")
            if isinstance(calibration_observation, dict)
            else None
        )
        calibration_data = (
            calibration_data if isinstance(calibration_data, dict) else {}
        )
        vio_observation = optional_streams.get("localization.vio.status")
        vio_data = (
            vio_observation.get("data")
            if isinstance(vio_observation, dict)
            else None
        )
        vio_data = vio_data if isinstance(vio_data, dict) else {}
        body_observation = optional_streams.get("localization.body.pose")
        body_data = (
            body_observation.get("data")
            if isinstance(body_observation, dict)
            else None
        )
        body_data = body_data if isinstance(body_data, dict) else {}

        retention = self.manifest.get("retention")
        retention = retention if isinstance(retention, dict) else {
            "policy": "UNSPECIFIED_LEGACY_BUNDLE",
            "automatic_deletion_allowed": False,
            "deletion_requires_explicit_operator_action": True,
            "review_after_us": None,
        }
        review_after_us = retention.get("review_after_us")
        review_due = (
            review_after_us is not None
            and int(time.time() * 1_000_000) >= int(review_after_us)
        )
        manifest_bytes = (self.bundle_directory / "manifest.json").read_bytes()
        authorization_records = records.get("authorizations")
        if authorization_records is None:
            authorization_records = records.get("authorization")

        return {
            "schema": "physical_agent.phase5_replay_provenance_summary",
            "schema_version": 1,
            "bundle_id": str(self.manifest.get("bundle_id") or ""),
            "bundle_schema": str(self.manifest.get("schema") or ""),
            "bundle_schema_version": int(
                self.manifest.get("schema_version") or 0
            ),
            "created_at_us": int(self.manifest.get("created_at_us") or 0),
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "secret_redaction_applied": True,
            "routes": route_summaries,
            "provider_identities": [
                {
                    "provider_id": provider_id,
                    "provider_instance_id": instance_id,
                    "boot_id": boot_id,
                }
                for provider_id, instance_id, boot_id in sorted(
                    provider_identities
                )
            ],
            "payloads": payload_summaries,
            "artifacts": {
                str(label): {
                    "bytes": int(entry.get("bytes") or 0),
                    "sha256": str(entry.get("sha256") or ""),
                    "source_name": str(entry.get("source_name") or ""),
                }
                for label, entry in self.manifest.get("artifacts", {}).items()
                if isinstance(entry, dict)
            },
            "calibration_revision": (
                calibration_data.get("calibration_revision")
                or (
                    calibration_observation.get("calibration_revision")
                    if isinstance(calibration_observation, dict)
                    else None
                )
            ),
            "vio_session_epoch": vio_data.get("session_epoch"),
            "world_frame": body_data.get("world_frame"),
            "record_presence": {
                "fabric": bool(fabric),
                "manager_binding": any(
                    key in records
                    for key in ("capability_binding", "binding", "binding_state")
                ),
                "workcell_transform": any(
                    key in records
                    for key in ("workcell_transform", "transform_state")
                ),
                "vlm": any(
                    key in records
                    for key in (
                        "vlm",
                        "vlm_requests",
                        "rgbd_alignment_validation",
                    )
                ),
                "controller_preview": any(
                    key in records
                    for key in ("controller_preview", "controller_preview_state")
                ),
                "lease": any(
                    key in records for key in ("lease", "lease_state")
                ),
                "authorization": authorization_records is not None,
                "control_audit": (
                    "control_audit_state" in records
                    or optional_streams.get(
                        "robot_arm.integrated.control_audit"
                    )
                    is not None
                ),
            },
            "replay_isolation": copy.deepcopy(
                self.manifest.get("replay_policy") or {}
            ),
            "retention": {
                **copy.deepcopy(retention),
                "review_due": review_due,
            },
        }

    def close(self) -> None:
        while self._mappings:
            self._mappings.pop().close()

    def __enter__(self) -> "Phase5ReplayBundle":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class Phase5ReplayCaptureService:
    """Capture and validate current Fabric RGB-D state without hardware control."""

    def __init__(self, fabric: Any, replay_root: Path):
        self.fabric = fabric
        self.replay_root = replay_root.resolve()

    async def capture_current(
        self,
        *,
        bundle_id: str | None,
        additional_records: Mapping[str, Any] | None = None,
        artifacts: Mapping[str, Path] | None = None,
    ) -> dict[str, Any]:
        safe_bundle_id = _safe_label(bundle_id or uuid.uuid4().hex)
        destination = self.replay_root / safe_bundle_id

        # Read slower, non-payload metadata before taking the short-lived
        # BufferRef snapshot from the camera's advancing shared-memory ring.
        optional_streams: dict[str, Any] = {}
        for stream in (
            "camera.calibration",
            "localization.vio.status",
            "localization.body.pose",
            "skills.initialize_space_cognition.status",
            "robot_arm.integrated.control_audit",
        ):
            report_operation_progress(f"REPLAY_READ_{stream.upper()}")
            optional_streams[stream] = await self.fabric.latest_optional(stream)

        report_operation_progress("REPLAY_READ_RGBD_ROUTE_SET")
        route_observation = await self.fabric.latest("camera.rgbd.data_routes")
        report_operation_progress("REPLAY_READ_RGBD_BUNDLE")
        bundle_observation = await self.fabric.latest("camera.rgbd.bundle")
        bundle = bundle_observation.get("data")
        if not isinstance(bundle, dict):
            raise RuntimeError("camera.rgbd.bundle has no data object")

        references: dict[str, dict[str, Any]] = {}
        for label, field in (
            ("rgb", "rgb"),
            ("native_depth", "depth"),
            ("infrared", "ir"),
            ("registered_depth", "depth_aligned_to_rgb"),
        ):
            reference = bundle.get(field)
            if isinstance(reference, dict):
                references[label] = reference
        missing = {
            "rgb",
            "native_depth",
            "registered_depth",
        } - set(references)
        if missing:
            raise RuntimeError(
                "current RGB-D bundle is missing replay references: "
                + ", ".join(sorted(missing))
            )

        observed_provider = str(bundle_observation.get("provider_id") or "")
        route_provider = str(route_observation.get("provider_id") or "")
        if (
            observed_provider
            and route_provider
            and observed_provider != route_provider
        ):
            raise RuntimeError(
                "RGB-D route and bundle provider mismatch: "
                f"{route_provider} != {observed_provider}"
            )

        route_set = route_observation.get("data")
        if not isinstance(route_set, dict):
            route_set = route_observation
        records = {
            "fabric": {
                "route_observation": route_observation,
                "rgbd_bundle_observation": bundle_observation,
                "optional_streams": optional_streams,
            },
            "capture": {
                "provider_id": observed_provider or route_provider,
                "reference_labels": sorted(references),
                "hardware_provider_started_by_capture": False,
                "physical_controller_called_by_capture": False,
            },
            **copy.deepcopy(dict(additional_records or {})),
        }
        report_operation_progress("REPLAY_COPY_CURRENT_BUFFERREFS")
        manifest_path = await await_with_progress_heartbeat(
            asyncio.to_thread(
                capture_phase5_replay_bundle,
                bundle_directory=destination,
                route_set=route_set,
                references=references,
                records=records,
                artifacts=artifacts,
                bundle_id=safe_bundle_id,
            ),
            stage="REPLAY_COPY_CURRENT_BUFFERREFS",
        )
        report_operation_progress("REPLAY_VERIFY_CAPTURE")
        loaded = await asyncio.to_thread(Phase5ReplayBundle.load, destination)
        return {
            "status": "CAPTURED",
            "bundle_id": safe_bundle_id,
            "manifest_path": str(manifest_path),
            "payload_count": len(loaded.manifest.get("payloads", {})),
            "artifact_count": len(loaded.manifest.get("artifacts", {})),
            "replay_policy": copy.deepcopy(loaded.manifest["replay_policy"]),
        }

    async def validate_bundle(self, bundle_id: str) -> dict[str, Any]:
        safe_bundle_id = _safe_label(bundle_id)
        directory = self.replay_root / safe_bundle_id
        report_operation_progress("REPLAY_LOAD_AND_HASH_VALIDATE")
        bundle = await asyncio.to_thread(Phase5ReplayBundle.load, directory)
        try:
            report_operation_progress("REPLAY_MATERIALIZE_SHARED_MEMORY")
            replay = await asyncio.to_thread(bundle.materialize)
            verified: dict[str, dict[str, Any]] = {}
            for label, reference in replay.references.items():
                report_operation_progress(f"REPLAY_READ_{label.upper()}")
                camera = CameraSharedMemory(str(reference["mapping_name"])).open()
                try:
                    payload = camera.read_ref(reference)
                finally:
                    camera.close()
                expected = bundle.manifest["payloads"][label]
                digest = _sha256_bytes(payload)
                if digest != expected["sha256"]:
                    raise RuntimeError(
                        f"materialized replay payload hash mismatch: {label}"
                    )
                verified[label] = {
                    "bytes": len(payload),
                    "sha256": digest,
                    "mapping_name": reference["mapping_name"],
                    "generation": reference["generation"],
                }
            return {
                "status": "VALID",
                "bundle_id": replay.bundle_id,
                "hardware_access_allowed": False,
                "physical_controller_call_allowed": False,
                "verified_payloads": verified,
            }
        finally:
            bundle.close()

    def bundle_provenance(self, bundle_id: str) -> dict[str, Any]:
        safe_bundle_id = _safe_label(bundle_id)
        bundle = Phase5ReplayBundle.load(self.replay_root / safe_bundle_id)
        return bundle.provenance_summary()

    async def compare_rgbd_routes(
        self,
        bundle_id: str,
        *,
        pixel_yx: tuple[float, float],
        depth_policy: str,
    ) -> dict[str, Any]:
        safe_bundle_id = _safe_label(bundle_id)
        directory = self.replay_root / safe_bundle_id
        report_operation_progress("REPLAY_ROUTE_COMPARISON_LOAD")
        bundle = await asyncio.to_thread(Phase5ReplayBundle.load, directory)
        try:
            report_operation_progress("REPLAY_ROUTE_COMPARISON_MATERIALIZE")
            replay = await asyncio.to_thread(bundle.materialize)
            report_operation_progress("REPLAY_ROUTE_COMPARISON_REGISTER")
            return await asyncio.to_thread(
                self._compare_materialized_rgbd_routes,
                bundle.manifest,
                replay,
                pixel_yx,
                depth_policy,
            )
        finally:
            bundle.close()

    @staticmethod
    def _compare_materialized_rgbd_routes(
        manifest: dict[str, Any],
        replay: ReplayMaterialization,
        pixel_yx: tuple[float, float],
        depth_policy: str,
    ) -> dict[str, Any]:
        routes = replay.route_set.get("routes")
        if not isinstance(routes, list):
            raise RuntimeError("replay bundle has no RGB-D route set")
        by_capability = {
            str(route.get("capability") or ""): route
            for route in routes
            if isinstance(route, dict) and bool(route.get("available"))
        }
        required = {
            "generic": "camera.rgbd.route.generic_shared_memory",
            "direct": "camera.rgbd.route.direct_shared_memory",
        }
        missing = [
            label
            for label, capability in required.items()
            if capability not in by_capability
        ]
        if missing:
            raise RuntimeError(
                "replay route comparison is missing: " + ", ".join(missing)
            )

        rgb_reference = replay.references.get("rgb")
        depth_reference = replay.references.get("registered_depth")
        if not isinstance(rgb_reference, dict) or not isinstance(
            depth_reference, dict
        ):
            raise RuntimeError("replay bundle lacks RGB or registered depth")
        mapping_name = str(depth_reference.get("mapping_name") or "")
        camera = CameraSharedMemory(mapping_name).open()
        try:
            payload = camera.read_ref(depth_reference)
        finally:
            camera.close()

        width = int(depth_reference.get("width") or 0)
        height = int(depth_reference.get("height") or 0)
        format_name = str(depth_reference.get("format_name") or "").upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(
                f"unsupported replay registered-depth format: {format_name}"
            )
        expected = width * height
        values = np.frombuffer(payload, dtype="<u2", count=expected)
        if values.size != expected:
            raise RuntimeError("replay registered-depth payload is truncated")
        scale_mm = float(depth_reference.get("depth_value_scale_mm") or 1.0)
        registered_depth_m = (
            values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0
        )

        records = manifest.get("records")
        fabric_records = (
            records.get("fabric") if isinstance(records, dict) else None
        )
        optional_streams = (
            fabric_records.get("optional_streams")
            if isinstance(fabric_records, dict)
            else None
        )
        calibration_observation = (
            optional_streams.get("camera.calibration")
            if isinstance(optional_streams, dict)
            else None
        )
        calibration = (
            calibration_observation.get("data")
            if isinstance(calibration_observation, dict)
            else None
        )
        intrinsics = (
            calibration.get("rgb_intrinsic")
            if isinstance(calibration, dict)
            else None
        )
        if not isinstance(intrinsics, dict):
            raise RuntimeError("replay bundle lacks RGB intrinsics")

        source_frame = str(
            (calibration_observation or {}).get("coordinate_frame")
            or "replay_camera_frame"
        )
        timestamp_us = int(
            depth_reference.get("global_timestamp_us")
            or depth_reference.get("system_timestamp_us")
            or depth_reference.get("device_timestamp_us")
            or 0
        )
        calibration_revision = (
            str(
                calibration.get("calibration_revision")
                or calibration_observation.get("calibration_revision")
            )
            if isinstance(calibration, dict)
            and (
                calibration.get("calibration_revision") is not None
                or calibration_observation.get("calibration_revision") is not None
            )
            else None
        )

        results: dict[str, dict[str, Any]] = {}
        for label, capability in required.items():
            route = by_capability[capability]
            products = route.get("products")
            channels = (
                products.get("channels") if isinstance(products, dict) else None
            )
            registered_channel = (
                channels.get("depth_registered_to_rgb")
                if isinstance(channels, dict)
                else None
            )
            valid_region = (
                registered_channel.get("valid_region")
                if isinstance(registered_channel, dict)
                else None
            )
            route_provenance = {
                "route_id": str(route.get("route_id") or ""),
                "capability": capability,
                "provider_id": str(route.get("provider_id") or ""),
                "provider_instance_id": str(
                    route.get("provider_instance_id") or ""
                ),
                "boot_id": str(route.get("boot_id") or ""),
                "hardware_specific": bool(route.get("hardware_specific")),
                "selection_reason": (
                    "GENERIC_ROUTE_PREFERRED"
                    if label == "generic"
                    else "EXPLICIT_PROVIDER_COMPATIBILITY_FALLBACK"
                ),
            }
            results[label] = register_rgbd_point(
                rgb_pixel_yx=pixel_yx,
                rgb_grid=(
                    int(rgb_reference.get("height") or 0),
                    int(rgb_reference.get("width") or 0),
                ),
                registered_depth_m=registered_depth_m,
                registered_depth_grid=(height, width),
                intrinsics=intrinsics,
                target_from_camera=np.eye(4, dtype=np.float64),
                observed_at_us=timestamp_us,
                source_frame=source_frame,
                target_frame=source_frame,
                calibration_revision=calibration_revision,
                route_provenance=route_provenance,
                depth_policy=depth_policy,
                valid_region=(
                    dict(valid_region)
                    if isinstance(valid_region, dict)
                    else None
                ),
            )

        camera_axis_names = (
            "camera_system_x",
            "camera_system_y",
            "camera_system_z",
        )
        generic_point = np.asarray(
            [
                results["generic"]["camera_system_point_m"][axis]
                for axis in camera_axis_names
            ],
            dtype=np.float64,
        )
        direct_point = np.asarray(
            [
                results["direct"]["camera_system_point_m"][axis]
                for axis in camera_axis_names
            ],
            dtype=np.float64,
        )
        maximum_absolute_delta_m = float(
            np.max(np.abs(generic_point - direct_point))
        )
        same_depth_pixel = (
            results["generic"]["registered_depth_pixel_yx"]
            == results["direct"]["registered_depth_pixel_yx"]
        )
        depth_delta_m = abs(
            float(results["generic"]["depth_selection"]["depth_m"])
            - float(results["direct"]["depth_selection"]["depth_m"])
        )
        equivalent = (
            maximum_absolute_delta_m <= 1e-12
            and depth_delta_m <= 1e-12
            and same_depth_pixel
        )
        return {
            "schema": "physical_agent.phase5_rgbd_route_comparison",
            "schema_version": 1,
            "status": "PASS" if equivalent else "FAIL",
            "equivalent": equivalent,
            "bundle_id": replay.bundle_id,
            "pixel_yx": [float(pixel_yx[0]), float(pixel_yx[1])],
            "depth_policy": str(depth_policy),
            "maximum_absolute_point_delta_m": maximum_absolute_delta_m,
            "depth_delta_m": depth_delta_m,
            "same_registered_depth_pixel": same_depth_pixel,
            "results": results,
            "replay_policy": copy.deepcopy(replay.replay_policy),
            "hardware_access_allowed": False,
            "physical_action_submitted": False,
            "physical_controller_call_allowed": False,
        }

    async def run_scenario(
        self,
        bundle_id: str,
        scenario_name: str,
    ) -> dict[str, Any]:
        safe_bundle_id = _safe_label(bundle_id)
        directory = self.replay_root / safe_bundle_id
        report_operation_progress("REPLAY_SCENARIO_LOAD")
        bundle = await asyncio.to_thread(Phase5ReplayBundle.load, directory)
        try:
            materialized = await asyncio.to_thread(bundle.materialize)
            scenario = Phase5ReplayScenarioRunner.apply(
                materialized,
                scenario_name,
            )
            observed: dict[str, Any] = {}
            if scenario.name == "recycled_bufferref":
                label = str(scenario.injected_state["reference_label"])
                reference = scenario.materialization.references[label]
                camera = CameraSharedMemory(
                    str(reference["mapping_name"])
                ).open()
                try:
                    try:
                        camera.read_ref(reference)
                    except RuntimeError as error:
                        observed = {
                            "rejected": True,
                            "error": str(error),
                            "outcome": "REJECT_RECYCLED_BUFFERREF",
                            "physical_controller_called": False,
                            "physical_lease_acquired": False,
                            "hardware_provider_started": False,
                        }
                    else:
                        raise RuntimeError(
                            "recycled BufferRef scenario was unexpectedly readable"
                        )
                finally:
                    camera.close()
            else:
                observed = Phase5ReplayScenarioRunner.evaluate(scenario)
            if observed.get("outcome") != scenario.expected_outcome:
                raise RuntimeError(
                    "replay scenario outcome mismatch: "
                    f"expected {scenario.expected_outcome}, "
                    f"observed {observed.get('outcome')}"
                )
            return {
                "status": "SCENARIO_COMPLETED",
                "bundle_id": scenario.materialization.bundle_id,
                "scenario": scenario.name,
                "expected_outcome": scenario.expected_outcome,
                "observed": observed,
                "hardware_access_allowed": False,
                "physical_controller_call_allowed": False,
            }
        finally:
            bundle.close()

    def list_bundles(self) -> list[dict[str, Any]]:
        if not self.replay_root.exists():
            return []
        output: list[dict[str, Any]] = []
        for directory in sorted(self.replay_root.iterdir()):
            if not directory.is_dir() or not (directory / "manifest.json").is_file():
                continue
            try:
                bundle = Phase5ReplayBundle.load(directory)
                output.append(
                    {
                        "bundle_id": bundle.manifest["bundle_id"],
                        "created_at_us": bundle.manifest["created_at_us"],
                        "payload_count": len(bundle.manifest.get("payloads", {})),
                        "artifact_count": len(bundle.manifest.get("artifacts", {})),
                        "status": "VALID",
                        "provenance": bundle.provenance_summary(),
                    }
                )
            except Exception as error:
                output.append(
                    {
                        "bundle_id": directory.name,
                        "status": "INVALID",
                        "error": str(error),
                    }
                )
        return output


def capture_phase5_replay_bundle(
    *,
    bundle_directory: Path,
    route_set: dict[str, Any],
    references: Mapping[str, dict[str, Any]],
    records: Mapping[str, Any],
    artifacts: Mapping[str, Path] | None = None,
    bundle_id: str | None = None,
    max_total_payload_bytes: int = DEFAULT_MAX_TOTAL_PAYLOAD_BYTES,
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
    retention_review_days: int = DEFAULT_RETENTION_REVIEW_DAYS,
) -> Path:
    """Copy current BufferRefs and metadata into a hardware-incapable bundle."""

    directory = bundle_directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"replay bundle directory is not empty: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    payload_directory = directory / "payloads"
    artifact_directory = directory / "artifacts"
    payload_directory.mkdir(exist_ok=True)
    artifact_directory.mkdir(exist_ok=True)

    safe_bundle_id = _safe_label(bundle_id or uuid.uuid4().hex)
    retention_days = int(retention_review_days)
    if retention_days <= 0:
        raise ValueError("replay retention review days must be positive")
    redacted_route_set = _redact(copy.deepcopy(route_set))
    redacted_records = _redact(copy.deepcopy(dict(records)))
    metadata_probe = _canonical_json_bytes(
        {"route_set": redacted_route_set, "records": redacted_records}
    )
    if len(metadata_probe) > max_metadata_bytes:
        raise ValueError("replay metadata exceeds the configured size limit")

    cameras: dict[str, CameraSharedMemory] = {}
    captured_payloads: dict[str, tuple[str, dict[str, Any], bytes]] = {}
    payload_manifest: dict[str, dict[str, Any]] = {}
    total_payload_bytes = 0
    try:
        for label, original_reference in sorted(references.items()):
            safe_name = _safe_label(str(label))
            reference = copy.deepcopy(original_reference)
            mapping_name = str(reference.get("mapping_name") or "")
            if not mapping_name:
                raise ValueError(f"BufferRef {label!r} has no mapping_name")
            camera = cameras.get(mapping_name)
            if camera is None:
                camera = CameraSharedMemory(mapping_name).open()
                cameras[mapping_name] = camera
            payload = camera.read_ref(reference)
            if len(payload) != int(reference.get("payload_bytes") or -1):
                raise RuntimeError(
                    f"BufferRef {label!r} changed size while being captured"
                )
            total_payload_bytes += len(payload)
            if total_payload_bytes > max_total_payload_bytes:
                raise ValueError(
                    "replay payloads exceed the configured aggregate size limit"
                )
            captured_payloads[str(label)] = (
                safe_name,
                reference,
                payload,
            )
    finally:
        for camera in cameras.values():
            camera.close()

    # Persist only after every BufferRef has been copied. Disk latency must not
    # consume the remaining validity window for references not yet read.
    for label, (safe_name, reference, payload) in captured_payloads.items():
        digest = _sha256_bytes(payload)
        relative = Path("payloads") / f"{safe_name}-{digest[:16]}.bin"
        (directory / relative).write_bytes(payload)
        payload_manifest[label] = {
            "path": relative.as_posix(),
            "bytes": len(payload),
            "sha256": digest,
            "original_reference": _redact(reference),
        }

    artifact_manifest: dict[str, dict[str, Any]] = {}
    for label, source in sorted((artifacts or {}).items()):
        source_path = source.resolve()
        payload = source_path.read_bytes()
        total_payload_bytes += len(payload)
        if total_payload_bytes > max_total_payload_bytes:
            raise ValueError(
                "replay payloads and artifacts exceed the configured size limit"
            )
        digest = _sha256_bytes(payload)
        suffix = source_path.suffix[:20]
        relative = Path("artifacts") / (
            f"{_safe_label(str(label))}-{digest[:16]}{suffix}"
        )
        shutil.copyfile(source_path, directory / relative)
        artifact_manifest[str(label)] = {
            "path": relative.as_posix(),
            "bytes": len(payload),
            "sha256": digest,
            "source_name": source_path.name,
        }

    created_at_us = int(time.time() * 1_000_000)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": safe_bundle_id,
        "created_at_us": created_at_us,
        "replay_policy": {
            "namespace": "REPLAY",
            "hardware_provider_start_allowed": False,
            "physical_lease_allowed": False,
            "physical_controller_call_allowed": False,
            "agent_physical_execution_allowed": False,
            "large_payload_transport": "REPLAY_ONLY_SHARED_MEMORY",
        },
        "route_set": redacted_route_set,
        "payloads": payload_manifest,
        "records": redacted_records,
        "artifacts": artifact_manifest,
        "limits": {
            "max_total_payload_bytes": int(max_total_payload_bytes),
            "max_metadata_bytes": int(max_metadata_bytes),
            "captured_payload_and_artifact_bytes": total_payload_bytes,
        },
        "retention": {
            "policy": "MANUAL_REVIEW",
            "review_after_us": (
                created_at_us
                + retention_days * 24 * 60 * 60 * 1_000_000
            ),
            "review_interval_days": retention_days,
            "automatic_deletion_allowed": False,
            "deletion_requires_explicit_operator_action": True,
        },
    }
    encoded = _canonical_json_bytes(manifest)
    if len(encoded) > max_metadata_bytes:
        raise ValueError("replay manifest exceeds the configured metadata limit")
    temporary = directory / "manifest.json.incomplete"
    temporary.write_bytes(encoded)
    temporary.replace(directory / "manifest.json")
    return directory / "manifest.json"
