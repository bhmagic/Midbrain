from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Protocol

from .semantic_scene_inspector import SemanticSceneInspector
from .spatial_frames import (
    SpatialFrameResolver,
    SpatialResolutionRequired,
    WORLD_CONVENTION_ID,
    rotation_matrix,
)


_CORNER_NAMES = {
    "right_forward_up",
    "left_forward_up",
    "right_backward_up",
    "left_backward_up",
    "right_forward_down",
    "left_forward_down",
    "right_backward_down",
    "left_backward_down",
}
_OFFSET_REFERENCES = {
    "SOURCE_FRAME",
    "ACTIVE_WORLD",
    "CONTROLLED_EFFECTOR_FRAME",
}
_OFFSET_UNIT_SCALE_M = {
    "METRES": 1.0,
    "CENTIMETRES": 0.01,
    "MILLIMETRES": 0.001,
}
_TEMPORAL_POLICY_ID = "FRESH_FABRIC_SNAPSHOT_AND_STABLE_FRAME_AUTHORITY_V2"


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


class FabricWorldPointComposer:
    """Derive one active-world point from authoritative Fabric geometry."""

    def __init__(
        self,
        scene_inspector: SemanticSceneInspector,
        spatial_resolver: SpatialFrameResolver,
        *,
        controlled_effector_frame: str,
    ) -> None:
        self.scene_inspector = scene_inspector
        self.spatial_resolver = spatial_resolver
        self.fabric: FabricTransformProtocol = spatial_resolver.fabric
        self.controlled_effector_frame = str(
            controlled_effector_frame
        ).strip()
        if not self.controlled_effector_frame:
            raise ValueError("controlled_effector_frame must be non-empty")

    async def run(
        self,
        *,
        object_id: str,
        corner_name: str,
        offset_vector: list[float],
        offset_unit: str,
        offset_reference: str,
        expected_scene_revision: str | None = None,
    ) -> dict[str, Any]:
        selected_object_id = str(object_id or "").strip()
        selected_corner = str(corner_name or "").strip().lower()
        selected_reference = str(offset_reference or "").strip().upper()
        selected_unit = str(offset_unit or "").strip().upper()
        if not selected_object_id:
            raise ValueError("object_id must be non-empty")
        if selected_corner not in _CORNER_NAMES:
            raise ValueError("corner_name is not a canonical AABB corner")
        if selected_reference not in _OFFSET_REFERENCES:
            raise ValueError("offset_reference is unsupported")
        if selected_unit not in _OFFSET_UNIT_SCALE_M:
            raise ValueError("offset_unit is unsupported")
        raw_offset = _finite_vector3(offset_vector, "offset_vector")
        offset = tuple(
            value * _OFFSET_UNIT_SCALE_M[selected_unit]
            for value in raw_offset
        )

        skill_started_at_us = time.time_ns() // 1000
        scene = await self.scene_inspector.run(
            include_spheres=False,
            maximum_spheres=1,
            include_visual_evidence=False,
        )
        if scene.get("status") != "SCENE_READY":
            return {
                "status": "WORLD_POINT_SOURCE_UNAVAILABLE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "source_status": scene,
                "message": (
                    "A fresh contract-valid arm semantic scene is required "
                    "before deriving a world point."
                ),
            }

        scene_revision = str(scene.get("scene_revision") or "").strip()
        normalized_expected_revision = str(
            expected_scene_revision or ""
        ).strip()
        revision_disposition = (
            "NOT_SUPPLIED"
            if not normalized_expected_revision
            else (
                "MATCHED_SELECTED_SNAPSHOT"
                if normalized_expected_revision == scene_revision
                else "SUPERSEDED_BY_SELECTED_FRESH_SNAPSHOT"
            )
        )

        candidates = [
            value
            for value in scene.get("visible_surface_aabbs", [])
            if isinstance(value, dict)
            and str(value.get("object_id") or "").strip()
            == selected_object_id
        ]
        if len(candidates) != 1:
            return {
                "status": (
                    "WORLD_POINT_SOURCE_NOT_FOUND"
                    if not candidates
                    else "WORLD_POINT_SOURCE_AMBIGUOUS"
                ),
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "object_id": selected_object_id,
                "matching_source_count": len(candidates),
                "available_object_ids": sorted(
                    {
                        str(value.get("object_id") or "").strip()
                        for value in scene.get(
                            "visible_surface_aabbs", []
                        )
                        if isinstance(value, dict)
                        and str(value.get("object_id") or "").strip()
                    }
                ),
                "message": (
                    "Exactly one fresh visible-surface AABB must match the "
                    "requested object ID."
                ),
            }

        source = candidates[0]
        source_frame = str(
            source.get("frame_id") or scene.get("frame_id") or ""
        ).strip()
        if source_frame != self.spatial_resolver.arm_base_frame:
            return {
                "status": "WORLD_POINT_SOURCE_FRAME_INVALID",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "source_frame_id": source_frame or None,
                "required_source_frame_id": (
                    self.spatial_resolver.arm_base_frame
                ),
                "message": (
                    "The selected AABB is not expressed in the configured "
                    "arm-base frame."
                ),
            }
        if str(source.get("convention_id") or WORLD_CONVENTION_ID) != (
            WORLD_CONVENTION_ID
        ):
            return {
                "status": "WORLD_POINT_SOURCE_CONVENTION_INVALID",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "message": "The selected AABB uses an incompatible convention.",
            }

        corners = source.get("corners_m")
        corners = corners if isinstance(corners, dict) else {}
        try:
            source_point_arm = _finite_vector3(
                corners.get(selected_corner),
                f"corners_m.{selected_corner}",
            )
        except ValueError as error:
            return {
                "status": "WORLD_POINT_SOURCE_CORNER_INVALID",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "object_id": selected_object_id,
                "corner_name": selected_corner,
                "message": str(error),
            }

        source_observed_at_us = int(source.get("observed_at_us") or 0)
        source_expires_at_us = int(source.get("expires_at_us") or 0)
        now_us = time.time_ns() // 1000
        if (
            source_observed_at_us <= 0
            or source_expires_at_us <= source_observed_at_us
            or now_us > source_expires_at_us
        ):
            return {
                "status": "WORLD_POINT_SOURCE_STALE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "observed_at_us": source_observed_at_us or None,
                "expires_at_us": source_expires_at_us or None,
                "message": "The selected AABB is no longer fresh.",
            }

        try:
            binding = await self.spatial_resolver.resolve_world_point(
                target_position_world_m=[0.0, 0.0, 0.0],
            )
        except SpatialResolutionRequired as required:
            return {
                **required.payload,
                "physical_motion_submitted": False,
                "source_object_id": selected_object_id,
                "source_corner_name": selected_corner,
            }

        binding_provenance = binding.provenance
        world_frame = str(binding_provenance["world_frame"])
        session_epoch = str(binding_provenance["session_epoch"])
        workcell_activation = binding_provenance.get("workcell_activation")
        try:
            if isinstance(workcell_activation, dict):
                world_from_arm = {
                    "from_frame": source_frame,
                    "to_frame": world_frame,
                    "at_us": source_observed_at_us,
                    "translation_m": binding_provenance[
                        "world_from_arm_translation_m"
                    ],
                    "rotation_xyzw": binding_provenance[
                        "world_from_arm_rotation_xyzw"
                    ],
                    "path": list(
                        binding_provenance.get("transform_path") or []
                    ),
                }
            else:
                world_from_arm = await self.fabric.transform(
                    from_frame=source_frame,
                    to_frame=world_frame,
                    at_us=source_observed_at_us,
                    max_extrapolation_us=(
                        self.spatial_resolver
                        .maximum_transform_extrapolation_us
                    ),
                    session_epoch=session_epoch,
                )
            point_world = _transform_point(
                world_from_arm,
                source_point_arm,
            )
            offset_world, offset_provenance = await self._offset_in_world(
                offset=offset,
                offset_reference=selected_reference,
                world_from_arm=world_from_arm,
                world_frame=world_frame,
                session_epoch=session_epoch,
            )
        except Exception as error:
            return {
                "status": "WORLD_POINT_TRANSFORM_UNAVAILABLE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "source_frame_id": source_frame,
                "target_world_frame_id": world_frame,
                "target_session_epoch": session_epoch,
                "message": str(error),
            }

        target_world = tuple(
            point_world[index] + offset_world[index]
            for index in range(3)
        )
        try:
            continuity_binding = (
                await self.spatial_resolver.resolve_world_point(
                    target_position_world_m=[0.0, 0.0, 0.0],
                )
            )
        except SpatialResolutionRequired as required:
            return {
                **required.payload,
                "physical_motion_submitted": False,
                "source_object_id": selected_object_id,
                "source_corner_name": selected_corner,
            }
        continuity_provenance = continuity_binding.provenance
        if _world_binding_identity(continuity_provenance) != (
            _world_binding_identity(binding_provenance)
        ):
            return {
                "status": "WORLD_POINT_FRAME_AUTHORITY_CHANGED",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "source_world_binding": _world_binding_identity(
                    binding_provenance
                ),
                "active_world_binding": _world_binding_identity(
                    continuity_provenance
                ),
                "message": (
                    "World-frame authority changed during coordinate "
                    "derivation. Derive a new target in the active frame."
                ),
            }
        completed_at_us = time.time_ns() // 1000
        if completed_at_us > source_expires_at_us:
            return {
                "status": "WORLD_POINT_SOURCE_EXPIRED_DURING_DERIVATION",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
                "observed_at_us": source_observed_at_us,
                "expires_at_us": source_expires_at_us,
                "message": (
                    "The selected AABB expired during coordinate derivation."
                ),
            }
        source_age_ms = max(
            0.0,
            (completed_at_us - source_observed_at_us) / 1000.0,
        )
        source_identity = {
            "stream": scene.get("stream"),
            "provider_id": scene.get("provider_id"),
            "provider_instance_id": scene.get("provider_instance_id"),
            "boot_id": scene.get("boot_id"),
            "sequence": scene.get("sequence"),
            "scene_revision": scene_revision,
            "object_id": selected_object_id,
            "extent_kind": source.get("extent_kind"),
            "corner_name": selected_corner,
            "frame_id": source_frame,
            "convention_id": WORLD_CONVENTION_ID,
            "observed_at_us": source_observed_at_us,
            "expires_at_us": source_expires_at_us,
        }
        derivation_basis = {
            "source": source_identity,
            "source_point_m": list(source_point_arm),
            "requested_offset_vector": list(raw_offset),
            "requested_offset_unit": selected_unit,
            "offset_vector_m": list(offset),
            "offset_reference": selected_reference,
            "target_position_world_m": list(target_world),
            "target_world_frame_id": world_frame,
            "target_session_epoch": session_epoch,
            "world_from_arm_path": list(world_from_arm.get("path") or []),
            "offset_transform": offset_provenance,
        }
        derivation_id = "world-point-" + hashlib.sha256(
            json.dumps(
                derivation_basis,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema": "physical_agent.fabric_derived_world_point",
            "schema_version": 1,
            "status": "WORLD_POINT_READY",
            "workflow_complete": True,
            "physical_motion_authorized": False,
            "physical_motion_submitted": False,
            "derivation_id": derivation_id,
            "target_position_world_m": list(target_world),
            "target_world_frame_id": world_frame,
            "target_session_epoch": session_epoch,
            "convention_id": WORLD_CONVENTION_ID,
            "observed_at_us": source_observed_at_us,
            "derived_at_us": completed_at_us,
            "expires_at_us": source_expires_at_us,
            "source_age_ms_at_completion": source_age_ms,
            "temporal_policy_id": _TEMPORAL_POLICY_ID,
            "temporal_decision": "ACCEPTED_FRESH_SNAPSHOT",
            "inspected_scene_revision": (
                normalized_expected_revision or None
            ),
            "scene_revision_disposition": revision_disposition,
            "source": source_identity,
            "derivation": {
                "operation": "POINT_PLUS_VECTOR_THEN_TRANSFORM_TO_ACTIVE_WORLD",
                "source_point_m": list(source_point_arm),
                "requested_offset_vector": list(raw_offset),
                "requested_offset_unit": selected_unit,
                "offset_vector_m": list(offset),
                "offset_reference": selected_reference,
                "offset_vector_world_m": list(offset_world),
                "world_from_arm_transform_at_us": int(
                    world_from_arm.get("at_us") or source_observed_at_us
                ),
                "world_from_arm_transform_path": list(
                    world_from_arm.get("path") or []
                ),
                "offset_transform": offset_provenance,
                "workcell_activation": workcell_activation,
            },
            "skill_started_at_us": skill_started_at_us,
            "skill_completed_at_us": completed_at_us,
            "message": (
                "Forward target_position_world_m, target_world_frame_id, and "
                "target_session_epoch unchanged to the absolute world-point "
                "motion Skill when movement was requested."
            ),
        }

    async def _offset_in_world(
        self,
        *,
        offset: tuple[float, float, float],
        offset_reference: str,
        world_from_arm: dict[str, Any],
        world_frame: str,
        session_epoch: str,
    ) -> tuple[tuple[float, float, float], dict[str, Any]]:
        if offset_reference == "ACTIVE_WORLD":
            return offset, {
                "source_frame": world_frame,
                "target_frame": world_frame,
                "at_us": int(world_from_arm.get("at_us") or 0) or None,
                "transform_path": [],
            }
        if offset_reference == "SOURCE_FRAME":
            return _rotate_vector(world_from_arm, offset), {
                "source_frame": self.spatial_resolver.arm_base_frame,
                "target_frame": world_frame,
                "at_us": int(world_from_arm.get("at_us") or 0) or None,
                "transform_path": list(world_from_arm.get("path") or []),
            }

        arm_from_controlled = await self.fabric.transform(
            from_frame=self.controlled_effector_frame,
            to_frame=self.spatial_resolver.arm_base_frame,
            at_us=None,
            max_extrapolation_us=(
                self.spatial_resolver.maximum_transform_extrapolation_us
            ),
            session_epoch=session_epoch,
        )
        offset_arm = _rotate_vector(arm_from_controlled, offset)
        return _rotate_vector(world_from_arm, offset_arm), {
            "source_frame": self.controlled_effector_frame,
            "intermediate_frame": self.spatial_resolver.arm_base_frame,
            "target_frame": world_frame,
            "at_us": int(arm_from_controlled.get("at_us") or 0) or None,
            "transform_path": [
                *list(arm_from_controlled.get("path") or []),
                *list(world_from_arm.get("path") or []),
            ],
        }


def _finite_vector3(value: Any, field: str) -> tuple[float, float, float]:
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        vector = ()
    if len(vector) != 3 or not all(math.isfinite(item) for item in vector):
        raise ValueError(f"{field} must contain three finite values")
    return vector


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
    return tuple(
        rotated[index] + translation[index]
        for index in range(3)
    )
