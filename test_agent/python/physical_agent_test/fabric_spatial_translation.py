from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Protocol

from .spatial_frames import (
    SpatialFrameResolver,
    SpatialResolutionRequired,
    WORLD_CONVENTION_ID,
    rotation_matrix,
)


_SOURCE_REFERENCES = {
    "ACTIVE_WORLD",
    "ARM_BASE",
    "CONTROLLED_EFFECTOR_FRAME",
}


class FabricTransformProtocol(Protocol):
    async def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int | None = None,
        max_extrapolation_us: int = 500_000,
        session_epoch: str | None = None,
    ) -> dict[str, Any]: ...


class _SpatialTranslationUnavailable(RuntimeError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("message") or "spatial translation unavailable"))
        self.payload = payload


class FabricSpatialTranslator:
    """Translate typed directions and poses into the active Fabric world."""

    def __init__(
        self,
        spatial_resolver: SpatialFrameResolver,
        *,
        controlled_effector_frame: str,
    ) -> None:
        self.spatial_resolver = spatial_resolver
        self.fabric: FabricTransformProtocol = spatial_resolver.fabric
        self.controlled_effector_frame = str(controlled_effector_frame).strip()
        if not self.controlled_effector_frame:
            raise ValueError("controlled_effector_frame must be non-empty")

    async def translate_direction(
        self,
        *,
        direction: list[float],
        source_reference: str,
        source_frame_id: str | None = None,
        source_observed_at_us: int | None = None,
        source_session_epoch: str | None = None,
    ) -> dict[str, Any]:
        source_vector = _normalize_vector3(
            _finite_vector3(direction, "direction"),
            "direction",
        )
        try:
            context = await self._translation_context(
                coordinate_kind="DIRECTION",
                source_reference=source_reference,
                source_frame_id=source_frame_id,
                source_observed_at_us=source_observed_at_us,
                source_session_epoch=source_session_epoch,
            )
        except _SpatialTranslationUnavailable as unavailable:
            return unavailable.payload

        direction_world = _normalize_vector3(
            _rotate_vector(context["source_to_world"], source_vector),
            "direction_world",
        )
        completed_at_us = time.time_ns() // 1000
        derivation = {
            "operation": "ROTATE_DIRECTION_TO_ACTIVE_WORLD",
            "source_reference": context["source_reference"],
            "source_frame_id": context["source_frame_id"],
            "source_direction_unit": list(source_vector),
            "direction_world": list(direction_world),
            "target_world_frame_id": context["world_frame"],
            "target_session_epoch": context["session_epoch"],
            "coordinate_at_us": context["coordinate_at_us"],
            "transform_path": context["transform_path"],
        }
        return {
            "schema": "physical_agent.fabric_translated_world_direction",
            "schema_version": 1,
            "status": "WORLD_DIRECTION_READY",
            "workflow_complete": True,
            "physical_motion_authorized": False,
            "physical_motion_submitted": False,
            "translation_id": _translation_id("world-direction", derivation),
            "convention_id": WORLD_CONVENTION_ID,
            "direction_world": list(direction_world),
            "target_world_frame_id": context["world_frame"],
            "target_session_epoch": context["session_epoch"],
            "coordinate_at_us": context["coordinate_at_us"],
            "translated_at_us": completed_at_us,
            "calibration_revision": context["calibration_revision"],
            "framed_direction_world": {
                "vector": list(direction_world),
                "units": "UNITLESS_UNIT_VECTOR",
                "frame_id": context["world_frame"],
                "convention_id": WORLD_CONVENTION_ID,
                "observed_at_us": context["coordinate_at_us"],
                "session_epoch": context["session_epoch"],
                "calibration_revision": context["calibration_revision"],
                "transform_path": context["transform_path"],
            },
            "source": context["source"],
            "derivation": derivation,
            "message": (
                "Copy direction_world unchanged into the downstream field "
                "whose semantic role requested this direction. This read-only "
                "translation grants no motion or contact authority."
            ),
        }

    async def translate_pose(
        self,
        *,
        position_m: list[float],
        orientation_xyzw: list[float],
        source_reference: str,
        source_frame_id: str | None = None,
        source_observed_at_us: int | None = None,
        source_session_epoch: str | None = None,
    ) -> dict[str, Any]:
        source_position = _finite_vector3(position_m, "position_m")
        source_orientation = _normalize_quaternion_xyzw(
            orientation_xyzw,
            "orientation_xyzw",
        )
        try:
            context = await self._translation_context(
                coordinate_kind="POSE",
                source_reference=source_reference,
                source_frame_id=source_frame_id,
                source_observed_at_us=source_observed_at_us,
                source_session_epoch=source_session_epoch,
            )
        except _SpatialTranslationUnavailable as unavailable:
            return unavailable.payload

        source_to_world = context["source_to_world"]
        position_world = _transform_point(source_to_world, source_position)
        orientation_world = _normalize_quaternion_xyzw(
            _quaternion_multiply_xyzw(
                source_to_world["rotation_xyzw"],
                source_orientation,
            ),
            "orientation_world_xyzw",
        )
        completed_at_us = time.time_ns() // 1000
        derivation = {
            "operation": "RIGID_POSE_TO_ACTIVE_WORLD",
            "source_reference": context["source_reference"],
            "source_frame_id": context["source_frame_id"],
            "source_position_m": list(source_position),
            "source_orientation_xyzw": list(source_orientation),
            "target_position_world_m": list(position_world),
            "target_orientation_world_xyzw": list(orientation_world),
            "target_world_frame_id": context["world_frame"],
            "target_session_epoch": context["session_epoch"],
            "coordinate_at_us": context["coordinate_at_us"],
            "transform_path": context["transform_path"],
        }
        return {
            "schema": "physical_agent.fabric_translated_world_pose",
            "schema_version": 1,
            "status": "WORLD_POSE_READY",
            "workflow_complete": True,
            "physical_motion_authorized": False,
            "physical_motion_submitted": False,
            "translation_id": _translation_id("world-pose", derivation),
            "convention_id": WORLD_CONVENTION_ID,
            "target_position_world_m": list(position_world),
            "target_orientation_world_xyzw": list(orientation_world),
            "target_world_frame_id": context["world_frame"],
            "target_session_epoch": context["session_epoch"],
            "coordinate_at_us": context["coordinate_at_us"],
            "translated_at_us": completed_at_us,
            "calibration_revision": context["calibration_revision"],
            "framed_pose_world": {
                "position_m": list(position_world),
                "orientation_xyzw": list(orientation_world),
                "frame_id": context["world_frame"],
                "convention_id": WORLD_CONVENTION_ID,
                "observed_at_us": context["coordinate_at_us"],
                "session_epoch": context["session_epoch"],
                "calibration_revision": context["calibration_revision"],
                "transform_path": context["transform_path"],
            },
            "source": context["source"],
            "derivation": derivation,
            "message": (
                "The pose is expressed in the active world frame. A physical "
                "consumer must independently validate and authorize its own "
                "operation; this read-only translation grants no authority."
            ),
        }

    async def _translation_context(
        self,
        *,
        coordinate_kind: str,
        source_reference: str,
        source_frame_id: str | None,
        source_observed_at_us: int | None,
        source_session_epoch: str | None,
    ) -> dict[str, Any]:
        selected_reference = str(source_reference or "").strip().upper()
        if selected_reference not in _SOURCE_REFERENCES:
            raise ValueError("source_reference is unsupported")
        normalized_frame_id = str(source_frame_id or "").strip() or None
        normalized_epoch = str(source_session_epoch or "").strip() or None
        normalized_observed_at_us = _optional_positive_timestamp(
            source_observed_at_us,
            "source_observed_at_us",
        )

        try:
            binding = await self.spatial_resolver.resolve_world_point(
                target_position_world_m=[0.0, 0.0, 0.0],
                expected_world_frame=(
                    normalized_frame_id
                    if selected_reference == "ACTIVE_WORLD"
                    else None
                ),
                expected_session_epoch=normalized_epoch,
            )
        except SpatialResolutionRequired as required:
            raise _SpatialTranslationUnavailable(
                _resolution_failure(required.payload, coordinate_kind)
            ) from required

        provenance = binding.provenance
        world_frame = str(provenance.get("world_frame") or "").strip()
        session_epoch = str(provenance.get("session_epoch") or "").strip()
        actual_source_frame = self._source_frame_id(
            selected_reference,
            world_frame,
        )
        if (
            normalized_frame_id is not None
            and normalized_frame_id != actual_source_frame
        ):
            raise _SpatialTranslationUnavailable(
                _failure(
                    f"WORLD_{coordinate_kind}_SOURCE_FRAME_MISMATCH",
                    (
                        f"The supplied source frame {normalized_frame_id} does "
                        f"not match active {selected_reference} frame "
                        f"{actual_source_frame}."
                    ),
                    supplied_source_frame_id=normalized_frame_id,
                    active_source_frame_id=actual_source_frame,
                )
            )

        try:
            source_to_world = await self._source_to_world_transform(
                source_reference=selected_reference,
                world_frame=world_frame,
                session_epoch=session_epoch,
                source_observed_at_us=normalized_observed_at_us,
                binding_provenance=provenance,
            )
        except Exception as error:
            raise _SpatialTranslationUnavailable(
                _failure(
                    f"WORLD_{coordinate_kind}_TRANSFORM_UNAVAILABLE",
                    str(error),
                    source_frame_id=actual_source_frame,
                    target_world_frame_id=world_frame,
                    target_session_epoch=session_epoch,
                )
            ) from error

        try:
            continuity = await self.spatial_resolver.resolve_world_point(
                target_position_world_m=[0.0, 0.0, 0.0],
                expected_world_frame=world_frame,
                expected_session_epoch=session_epoch,
            )
        except SpatialResolutionRequired as required:
            raise _SpatialTranslationUnavailable(
                _resolution_failure(required.payload, coordinate_kind)
            ) from required
        continuity_provenance = continuity.provenance
        if _world_binding_identity(continuity_provenance) != (
            _world_binding_identity(provenance)
        ):
            raise _SpatialTranslationUnavailable(
                _failure(
                    f"WORLD_{coordinate_kind}_FRAME_AUTHORITY_CHANGED",
                    (
                        "World-frame authority changed during coordinate "
                        "translation. Translate the coordinate again in the "
                        "active frame."
                    ),
                    source_world_binding=_world_binding_identity(provenance),
                    active_world_binding=_world_binding_identity(
                        continuity_provenance
                    ),
                )
            )

        activation = provenance.get("workcell_activation")
        activation = activation if isinstance(activation, dict) else {}
        transform_at_us = int(source_to_world.get("at_us") or 0) or None
        coordinate_at_us = (
            normalized_observed_at_us
            or transform_at_us
            or int(provenance.get("resolved_at_us") or 0)
            or time.time_ns() // 1000
        )
        transform_path = list(source_to_world.get("path") or [])
        return {
            "source_reference": selected_reference,
            "source_frame_id": actual_source_frame,
            "world_frame": world_frame,
            "session_epoch": session_epoch,
            "coordinate_at_us": coordinate_at_us,
            "calibration_revision": (
                str(activation.get("calibration_revision") or "") or None
            ),
            "transform_path": transform_path,
            "source_to_world": source_to_world,
            "source": {
                "reference": selected_reference,
                "frame_id": actual_source_frame,
                "convention_id": WORLD_CONVENTION_ID,
                "observed_at_us": normalized_observed_at_us,
                "session_epoch": normalized_epoch,
            },
        }

    def _source_frame_id(
        self,
        source_reference: str,
        world_frame: str,
    ) -> str:
        if source_reference == "ACTIVE_WORLD":
            return world_frame
        if source_reference == "ARM_BASE":
            return self.spatial_resolver.arm_base_frame
        return self.controlled_effector_frame

    async def _source_to_world_transform(
        self,
        *,
        source_reference: str,
        world_frame: str,
        session_epoch: str,
        source_observed_at_us: int | None,
        binding_provenance: dict[str, Any],
    ) -> dict[str, Any]:
        if source_reference == "ACTIVE_WORLD":
            return {
                "from_frame": world_frame,
                "to_frame": world_frame,
                "at_us": (
                    source_observed_at_us
                    or int(binding_provenance.get("resolved_at_us") or 0)
                    or None
                ),
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                "path": [],
            }

        world_from_arm = {
            "from_frame": self.spatial_resolver.arm_base_frame,
            "to_frame": world_frame,
            "at_us": int(binding_provenance.get("resolved_at_us") or 0) or None,
            "translation_m": list(
                _finite_vector3(
                    binding_provenance.get("world_from_arm_translation_m"),
                    "world_from_arm_translation_m",
                )
            ),
            "rotation_xyzw": list(
                _normalize_quaternion_xyzw(
                    binding_provenance.get("world_from_arm_rotation_xyzw"),
                    "world_from_arm_rotation_xyzw",
                )
            ),
            "path": list(binding_provenance.get("transform_path") or []),
        }
        if source_reference == "ARM_BASE":
            return world_from_arm

        arm_from_controlled = await self.fabric.transform(
            from_frame=self.controlled_effector_frame,
            to_frame=self.spatial_resolver.arm_base_frame,
            at_us=source_observed_at_us,
            max_extrapolation_us=(
                self.spatial_resolver.maximum_transform_extrapolation_us
            ),
            session_epoch=session_epoch,
        )
        return _compose_transforms(world_from_arm, arm_from_controlled)


def _failure(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "status": status,
        "workflow_complete": False,
        "physical_motion_authorized": False,
        "physical_motion_submitted": False,
        **details,
        "message": message,
    }


def _resolution_failure(
    payload: dict[str, Any],
    coordinate_kind: str,
) -> dict[str, Any]:
    result = dict(payload)
    status = str(result.get("status") or "WORLD_COORDINATE_UNAVAILABLE")
    if status.startswith("WORLD_POINT_"):
        status = f"WORLD_{coordinate_kind}_{status.removeprefix('WORLD_POINT_')}"
    result["status"] = status
    result["workflow_complete"] = False
    result["physical_motion_authorized"] = False
    result["physical_motion_submitted"] = False
    return result


def _translation_id(prefix: str, derivation: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            derivation,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest}"


def _optional_positive_timestamp(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a positive integer or null") from error
    if timestamp <= 0:
        raise ValueError(f"{field} must be a positive integer or null")
    return timestamp


def _finite_vector3(value: Any, field: str) -> tuple[float, float, float]:
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        vector = ()
    if len(vector) != 3 or not all(math.isfinite(item) for item in vector):
        raise ValueError(f"{field} must contain three finite values")
    return vector


def _normalize_vector3(
    vector: tuple[float, float, float],
    field: str,
) -> tuple[float, float, float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError(f"{field} must be non-zero")
    return tuple(value / norm for value in vector)


def _normalize_quaternion_xyzw(
    value: Any,
    field: str,
) -> tuple[float, float, float, float]:
    try:
        quaternion = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        quaternion = ()
    if len(quaternion) != 4 or not all(
        math.isfinite(item) for item in quaternion
    ):
        raise ValueError(f"{field} must contain four finite XYZW values")
    norm = math.sqrt(sum(item * item for item in quaternion))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError(f"{field} must be non-zero")
    normalized = tuple(item / norm for item in quaternion)
    if normalized[3] < 0.0:
        normalized = tuple(-item for item in normalized)
    return normalized


def _quaternion_multiply_xyzw(
    left: Any,
    right: Any,
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = _normalize_quaternion_xyzw(left, "left quaternion")
    rx, ry, rz, rw = _normalize_quaternion_xyzw(right, "right quaternion")
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def _rotate_vector(
    transform: dict[str, Any],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    rotation = rotation_matrix(transform.get("rotation_xyzw"))
    return tuple(
        sum(rotation[row][column] * vector[column] for column in range(3))
        for row in range(3)
    )


def _transform_point(
    transform: dict[str, Any],
    point: tuple[float, float, float],
) -> tuple[float, float, float]:
    rotated = _rotate_vector(transform, point)
    translation = _finite_vector3(
        transform.get("translation_m"),
        "transform.translation_m",
    )
    return tuple(rotated[index] + translation[index] for index in range(3))


def _compose_transforms(
    outer: dict[str, Any],
    inner: dict[str, Any],
) -> dict[str, Any]:
    inner_translation = _finite_vector3(
        inner.get("translation_m"),
        "inner.translation_m",
    )
    outer_translation = _finite_vector3(
        outer.get("translation_m"),
        "outer.translation_m",
    )
    rotated_inner_translation = _rotate_vector(outer, inner_translation)
    return {
        "from_frame": inner.get("from_frame"),
        "to_frame": outer.get("to_frame"),
        "at_us": inner.get("at_us") or outer.get("at_us"),
        "translation_m": [
            rotated_inner_translation[index] + outer_translation[index]
            for index in range(3)
        ],
        "rotation_xyzw": list(
            _normalize_quaternion_xyzw(
                _quaternion_multiply_xyzw(
                    outer.get("rotation_xyzw"),
                    inner.get("rotation_xyzw"),
                ),
                "composed.rotation_xyzw",
            )
        ),
        "path": [
            *list(inner.get("path") or []),
            *list(outer.get("path") or []),
        ],
    }


def _world_binding_identity(provenance: dict[str, Any]) -> dict[str, Any]:
    activation = provenance.get("workcell_activation")
    activation = activation if isinstance(activation, dict) else {}
    return {
        "world_frame": str(provenance.get("world_frame") or ""),
        "session_epoch": str(provenance.get("session_epoch") or ""),
        "activation_id": str(activation.get("activation_id") or "") or None,
        "calibration_revision": (
            str(activation.get("calibration_revision") or "") or None
        ),
    }
