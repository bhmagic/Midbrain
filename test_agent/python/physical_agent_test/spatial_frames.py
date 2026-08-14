from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Protocol


WORLD_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)
CANONICAL_CAMERA_CALIBRATION_POLICY = (
    "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
)

_SEMANTIC_VECTORS = {
    "FRONT": (1.0, 0.0, 0.0),
    "FORWARD": (1.0, 0.0, 0.0),
    "BACK": (-1.0, 0.0, 0.0),
    "BACKWARD": (-1.0, 0.0, 0.0),
    "LEFT": (0.0, 1.0, 0.0),
    "RIGHT": (0.0, -1.0, 0.0),
    "UP": (0.0, 0.0, 1.0),
    "DOWN": (0.0, 0.0, -1.0),
    "POSITIVE_X": (1.0, 0.0, 0.0),
    "NEGATIVE_X": (-1.0, 0.0, 0.0),
    "POSITIVE_Y": (0.0, 1.0, 0.0),
    "NEGATIVE_Y": (0.0, -1.0, 0.0),
    "POSITIVE_Z": (0.0, 0.0, 1.0),
    "NEGATIVE_Z": (0.0, 0.0, -1.0),
}

_ARM_BASE_AXIS_VECTORS = {
    "ARM_BASE_POSITIVE_X": (1.0, 0.0, 0.0),
    "ARM_BASE_NEGATIVE_X": (-1.0, 0.0, 0.0),
    "ARM_BASE_POSITIVE_Y": (0.0, 1.0, 0.0),
    "ARM_BASE_NEGATIVE_Y": (0.0, -1.0, 0.0),
    "ARM_BASE_POSITIVE_Z": (0.0, 0.0, 1.0),
    "ARM_BASE_NEGATIVE_Z": (0.0, 0.0, -1.0),
}

_SURVEYED_DIRECTIONS = {"NORTH", "SOUTH", "EAST", "WEST"}
_EXPLICIT_WORLD_AXIS_DIRECTIONS = {
    "POSITIVE_X",
    "NEGATIVE_X",
    "POSITIVE_Y",
    "NEGATIVE_Y",
    "POSITIVE_Z",
    "NEGATIVE_Z",
}


class SpatialFabricProtocol(Protocol):
    async def latest_optional(
        self,
        stream: str,
    ) -> dict[str, Any] | None:
        """Return the latest observation for a stream."""

    async def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int | None = None,
        max_extrapolation_us: int = 500_000,
        session_epoch: str | None = None,
    ) -> dict[str, Any]:
        """Return a transform mapping from_frame coordinates into to_frame."""


class SpatialResolutionRequired(RuntimeError):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("message") or "spatial resolution required"))
        self.payload = payload


@dataclass(frozen=True)
class SpatialResolution:
    direction: str
    reference_frame: str
    vector_arm_base: tuple[float, float, float]
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "physical_agent.semantic_direction_resolution",
            "schema_version": 2,
            "convention_id": WORLD_CONVENTION_ID,
            "direction": self.direction,
            "reference_frame": self.reference_frame,
            "resolved_frame": self.provenance["arm_base_frame"],
            "resolved_unit_vector": list(self.vector_arm_base),
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class WorldPointResolution:
    target_position_world_m: tuple[float, float, float]
    target_position_arm_base_m: tuple[float, float, float]
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "physical_agent.absolute_world_point_resolution",
            "schema_version": 1,
            "convention_id": WORLD_CONVENTION_ID,
            "source_frame": self.provenance["world_frame"],
            "resolved_frame": self.provenance["arm_base_frame"],
            "target_position_world_m": list(self.target_position_world_m),
            "target_position_arm_base_m": list(
                self.target_position_arm_base_m
            ),
            "provenance": self.provenance,
        }


class SpatialFrameResolver:
    """Resolve semantic directions before they reach arm-base IK."""

    def __init__(
        self,
        fabric: SpatialFabricProtocol,
        *,
        arm_base_frame: str,
        maximum_transform_extrapolation_us: int = 500_000,
    ) -> None:
        self.fabric = fabric
        self.arm_base_frame = str(arm_base_frame)
        self.maximum_transform_extrapolation_us = int(
            maximum_transform_extrapolation_us
        )

    async def resolve(
        self,
        *,
        direction: str,
        reference_frame: str = "WORLD",
        arm_mount_assumption: str = "UNKNOWN",
        camera_level_assumption: str = "UNKNOWN",
    ) -> SpatialResolution:
        normalized_direction = str(direction or "").strip().upper()
        normalized_frame = str(reference_frame or "WORLD").strip().upper()
        mount_assumption = str(
            arm_mount_assumption or "UNKNOWN"
        ).strip().upper()
        camera_assumption = str(
            camera_level_assumption or "UNKNOWN"
        ).strip().upper()

        if normalized_direction in _SURVEYED_DIRECTIONS:
            raise SpatialResolutionRequired(
                {
                    "status": "SURVEYED_FRAME_REQUIRED",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        f"{normalized_direction} is not an alias for robot "
                        "front or a world axis. Supply a calibrated surveyed "
                        "or geographic frame, or restate the request as "
                        "front/back/left/right."
                    ),
                }
            )
        if normalized_direction in _ARM_BASE_AXIS_VECTORS:
            if normalized_frame not in {"ARM_BASE", "WORLD"}:
                raise ValueError(
                    "explicit ARM_BASE axis directions require reference_frame ARM_BASE"
                )
            vector = _ARM_BASE_AXIS_VECTORS[normalized_direction]
            return SpatialResolution(
                direction=normalized_direction,
                reference_frame="ARM_BASE",
                vector_arm_base=vector,
                provenance={
                    "resolution_source": "EXPLICIT_ARM_BASE_AXIS",
                    "arm_base_frame": self.arm_base_frame,
                    "resolved_at_us": time.time_ns() // 1000,
                    "transform_path": [],
                    "operator_attestation": None,
                },
            )

        if normalized_direction not in _SEMANTIC_VECTORS:
            raise ValueError(
                "direction must be FRONT, BACK, LEFT, RIGHT, UP, DOWN, "
                "a signed WORLD axis, or an explicit ARM_BASE signed axis"
            )
        if normalized_frame == "ARM_BASE":
            raise ValueError(
                "ordinary directions cannot use ARM_BASE; use an explicit "
                "ARM_BASE_POSITIVE_* or ARM_BASE_NEGATIVE_* direction"
            )
        if normalized_frame not in {"WORLD", "CAMERA_LEVEL"}:
            raise ValueError("reference_frame must be WORLD or CAMERA_LEVEL")

        source_vector = _SEMANTIC_VECTORS[normalized_direction]
        try:
            vio = await self._current_vio()
        except SpatialResolutionRequired as required:
            if normalized_frame == "WORLD":
                activation_resolution = (
                    await self._resolve_from_canonical_workcell_activation(
                        direction=normalized_direction,
                        source_vector=source_vector,
                    )
                )
                if activation_resolution is not None:
                    return activation_resolution
            if (
                normalized_frame == "WORLD"
                and mount_assumption == "CONFIRMED_X_FORWARD_Z_UP"
                and normalized_direction
                not in _EXPLICIT_WORLD_AXIS_DIRECTIONS
            ):
                return self._attested_arm_mount_resolution(
                    direction=normalized_direction,
                    source_vector=source_vector,
                    transform_error=str(
                        required.payload.get("message") or required
                    ),
                )
            raise
        world_frame = vio["world_frame"]
        session_epoch = vio["session_epoch"]
        observed_at_us = vio["observed_at_us"]

        if normalized_frame == "CAMERA_LEVEL":
            if camera_assumption != "CONFIRMED_GRAVITY_LEVELED":
                raise SpatialResolutionRequired(
                    {
                        "status": "CAMERA_DIRECTION_CONFIRMATION_REQUIRED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "question": (
                            "Use gravity-leveled camera directions? Up will "
                            "mean opposite gravity, and front/left/right will "
                            "ignore camera pitch and roll. Confirm y/n."
                        ),
                        "required_value": (
                            "camera_level_assumption="
                            "CONFIRMED_GRAVITY_LEVELED"
                        ),
                        "message": (
                            "Camera-relative 3D directions require explicit "
                            "operator confirmation of the gravity-leveled "
                            "semantics."
                        ),
                    }
                )
            camera_level_frame = vio.get("camera_level_frame")
            if not camera_level_frame:
                raise SpatialResolutionRequired(
                    {
                        "status": "CAMERA_LEVEL_UNAVAILABLE",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "message": (
                            "The current VIO epoch has no valid leveled camera "
                            "front. Reorient the camera away from gravity "
                            "vertical or provide another heading authority."
                        ),
                    }
                )
            transform = await self._query_transform(
                from_frame=camera_level_frame,
                to_frame=self.arm_base_frame,
                at_us=observed_at_us,
                session_epoch=session_epoch,
                failure_status="CAMERA_TO_ARM_ALIGNMENT_REQUIRED",
            )
            world_transform = await self._query_transform(
                from_frame=camera_level_frame,
                to_frame=world_frame,
                at_us=observed_at_us,
                session_epoch=session_epoch,
                failure_status="CAMERA_TO_WORLD_ALIGNMENT_REQUIRED",
            )
            vector_arm = _rotate_vector(
                transform["rotation_xyzw"],
                source_vector,
            )
            vector_world = _rotate_vector(
                world_transform["rotation_xyzw"],
                source_vector,
            )
            return SpatialResolution(
                direction=normalized_direction,
                reference_frame="CAMERA_LEVEL",
                vector_arm_base=_normalize(vector_arm),
                provenance={
                    "resolution_source": "TIMESTAMPED_CAMERA_LEVEL_TO_ARM",
                    "arm_base_frame": self.arm_base_frame,
                    "source_frame": camera_level_frame,
                    "world_frame": world_frame,
                    "session_epoch": session_epoch,
                    "resolved_at_us": observed_at_us,
                    "transform_path": list(transform.get("path") or []),
                    "visual_transform_path": list(
                        world_transform.get("path") or []
                    ),
                    "expected_direction_world": list(
                        _normalize(vector_world)
                    ),
                    "operator_attestation": {
                        "statement": (
                            "camera directions are gravity leveled and ignore "
                            "camera pitch and roll"
                        ),
                        "confirmed": True,
                    },
                },
            )

        try:
            transform = await self.fabric.transform(
                from_frame=self.arm_base_frame,
                to_frame=world_frame,
                at_us=observed_at_us,
                max_extrapolation_us=(
                    self.maximum_transform_extrapolation_us
                ),
                session_epoch=session_epoch,
            )
        except Exception as error:
            activation_resolution = (
                await self._resolve_from_canonical_workcell_activation(
                    direction=normalized_direction,
                    source_vector=source_vector,
                )
            )
            if activation_resolution is not None:
                return activation_resolution
            if not _is_missing_transform_error(error):
                raise SpatialResolutionRequired(
                    {
                        "status": "ARM_ALIGNMENT_INVALID",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "message": (
                            "The world-to-arm transform query failed for a "
                            "reason that cannot be replaced by a mount "
                            f"attestation: {error}"
                        ),
                    }
                ) from error
            if normalized_direction in _EXPLICIT_WORLD_AXIS_DIRECTIONS:
                raise SpatialResolutionRequired(
                    {
                        "status": "WORLD_TO_ARM_ALIGNMENT_REQUIRED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "recommended_action": (
                            "Review and accept the current stationary "
                            "world-to-arm calibration candidate, or create a "
                            "new candidate and review it."
                        ),
                        "message": (
                            f"Explicit world axis {normalized_direction} "
                            "requires a reviewed motion-usable transform from "
                            f"{world_frame} to {self.arm_base_frame}. It cannot "
                            "fall back to an upright arm-mount attestation."
                        ),
                        "transform_error": str(error),
                    }
                ) from error
            if mount_assumption == "REJECTED_OR_UNKNOWN":
                raise SpatialResolutionRequired(
                    {
                        "status": "ARM_ALIGNMENT_REQUIRED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "recommended_action": (
                            "Run stationary world-arm alignment for a measured "
                            "transform."
                        ),
                        "message": (
                            "The arm is not confirmed in the default upright "
                            "mount and no reviewed world-to-arm transform is "
                            "available."
                        ),
                        "transform_error": str(error),
                    }
                ) from error
            if mount_assumption != "CONFIRMED_X_FORWARD_Z_UP":
                raise SpatialResolutionRequired(
                    {
                        "status": "ARM_ALIGNMENT_OR_ATTESTATION_REQUIRED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "question": (
                            "Is the arm base mounted so base +Z points "
                            "opposite gravity and base +X points along "
                            "workcell/robot front? Confirm y/n."
                        ),
                        "required_value": (
                            "arm_mount_assumption="
                            "CONFIRMED_X_FORWARD_Z_UP"
                        ),
                        "recommended_action": (
                            "Run stationary world-arm alignment for a measured "
                            "transform."
                        ),
                        "message": (
                            "World directions cannot be converted into the "
                            "arm-base frame without a reviewed transform or an "
                            "explicit bounded-development mount attestation."
                        ),
                        "transform_error": str(error),
                    }
                ) from error
            return self._attested_arm_mount_resolution(
                direction=normalized_direction,
                source_vector=source_vector,
                world_frame=world_frame,
                session_epoch=session_epoch,
                observed_at_us=observed_at_us,
                transform_error=str(error),
            )

        world_from_arm = rotation_matrix(transform["rotation_xyzw"])
        vector_arm = tuple(
            sum(world_from_arm[row][column] * source_vector[row] for row in range(3))
            for column in range(3)
        )
        return SpatialResolution(
            direction=normalized_direction,
            reference_frame="WORLD",
            vector_arm_base=_normalize(vector_arm),
            provenance={
                "resolution_source": "TIMESTAMPED_WORLD_FROM_ARM_TRANSFORM",
                "arm_base_frame": self.arm_base_frame,
                "source_frame": world_frame,
                "world_frame": world_frame,
                "session_epoch": session_epoch,
                "resolved_at_us": int(transform.get("at_us") or observed_at_us),
                "transform_path": list(transform.get("path") or []),
                "expected_direction_world": list(source_vector),
                "operator_attestation": None,
            },
        )

    async def current_vio(self) -> dict[str, Any]:
        """Return the current motion-usable VIO epoch identity."""

        return await self._current_vio()

    async def resolve_world_point(
        self,
        *,
        target_position_world_m: list[float],
        expected_world_frame: str | None = None,
        expected_session_epoch: str | None = None,
    ) -> WorldPointResolution:
        """Resolve one absolute point from the active world into arm base."""

        try:
            point_world = tuple(
                float(value) for value in target_position_world_m
            )
        except (TypeError, ValueError):
            point_world = ()
        if len(point_world) != 3 or not all(
            math.isfinite(value) for value in point_world
        ):
            raise ValueError(
                "target_position_world_m must contain three finite values"
            )

        normalized_expected_frame = str(
            expected_world_frame or ""
        ).strip()
        normalized_expected_epoch = str(
            expected_session_epoch or ""
        ).strip()
        vio: dict[str, Any] | None = None
        vio_error: SpatialResolutionRequired | None = None
        try:
            vio = await self._current_vio()
        except SpatialResolutionRequired as required:
            vio_error = required
        activation_observation = await self.fabric.latest_optional(
            "manager.workcell_calibration.activation"
        )
        activation = self._active_workcell_activation(
            activation_observation
        )
        use_workcell_activation = bool(
            activation is not None
            and (
                not normalized_expected_frame
                or normalized_expected_frame
                == str(activation.get("world_frame") or "")
            )
            and (
                vio is not None
                or activation.get("validity_policy")
                == CANONICAL_CAMERA_CALIBRATION_POLICY
            )
        )
        if vio is None and not use_workcell_activation:
            assert vio_error is not None
            raise vio_error
        if use_workcell_activation:
            assert activation is not None
            world_frame = str(activation["world_frame"])
            session_epoch = str(
                activation.get("session_epoch")
                or (vio or {}).get("session_epoch")
            )
            observed_at_us = int(
                activation_observation.get("observed_at_us")
                or (vio or {}).get("observed_at_us")
                or time.time_ns() // 1000
            )
        else:
            assert vio is not None
            world_frame = vio["world_frame"]
            session_epoch = vio["session_epoch"]
            observed_at_us = vio["observed_at_us"]
        if (
            normalized_expected_frame
            and normalized_expected_frame != world_frame
        ):
            raise SpatialResolutionRequired(
                {
                    "status": "WORLD_POINT_FRAME_MISMATCH",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "expected_world_frame": normalized_expected_frame,
                    "active_world_frame": world_frame,
                    "active_workcell_world_frame": (
                        str(activation.get("world_frame") or "")
                        if activation is not None
                        else None
                    ),
                    "message": (
                        "The absolute point belongs to neither the current "
                        "VIO world nor the active motion-usable workcell "
                        "world. Re-observe the point or reactivate its exact "
                        "reviewed calibration."
                    ),
                }
            )
        if (
            normalized_expected_epoch
            and normalized_expected_epoch != session_epoch
        ):
            raise SpatialResolutionRequired(
                {
                    "status": "WORLD_POINT_EPOCH_MISMATCH",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "expected_session_epoch": normalized_expected_epoch,
                    "active_session_epoch": session_epoch,
                    "message": (
                        "The absolute point belongs to a different VIO "
                        "session. Re-observe the point in the active epoch."
                    ),
                }
            )

        if use_workcell_activation:
            assert activation is not None
            transforms = activation.get("transforms")
            transform = (
                transforms.get("world_from_base")
                if isinstance(transforms, dict)
                else None
            )
            if not isinstance(transform, dict):
                raise SpatialResolutionRequired(
                    {
                        "status": "ARM_ALIGNMENT_INVALID",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "message": (
                            "The active workcell calibration has no valid "
                            "world-from-arm-base transform."
                        ),
                    }
                )
            transform = {
                **transform,
                "at_us": observed_at_us,
                "path": [
                    {
                        "authority": "manager.workcell_calibration",
                        "activation_id": activation.get("activation_id"),
                        "calibration_revision": activation.get(
                            "calibration_revision"
                        ),
                    }
                ],
            }
        else:
            try:
                transform = await self.fabric.transform(
                    from_frame=self.arm_base_frame,
                    to_frame=world_frame,
                    at_us=observed_at_us,
                    max_extrapolation_us=(
                        self.maximum_transform_extrapolation_us
                    ),
                    session_epoch=session_epoch,
                )
            except Exception as error:
                status = (
                    "WORLD_TO_ARM_ALIGNMENT_REQUIRED"
                    if _is_missing_transform_error(error)
                    else "ARM_ALIGNMENT_INVALID"
                )
                raise SpatialResolutionRequired(
                    {
                        "status": status,
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "recommended_action": (
                            "Review and accept the current stationary "
                            "world-to-arm calibration candidate, or create a "
                            "new candidate and review it."
                        ),
                        "message": (
                            "An absolute world point requires a current "
                            "reviewed transform from "
                            f"{world_frame} to {self.arm_base_frame}; an "
                            "arm-mount assumption is not sufficient: "
                            f"{error}"
                        ),
                        "transform_error": str(error),
                    }
                ) from error

        translation = transform.get("translation_m")
        try:
            world_from_arm_translation = tuple(
                float(value) for value in translation
            )
        except (TypeError, ValueError):
            world_from_arm_translation = ()
        if len(world_from_arm_translation) != 3 or not all(
            math.isfinite(value) for value in world_from_arm_translation
        ):
            raise SpatialResolutionRequired(
                {
                    "status": "ARM_ALIGNMENT_INVALID",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The reviewed world-to-arm transform has no valid "
                        "translation."
                    ),
                }
            )
        try:
            world_from_arm = rotation_matrix(transform["rotation_xyzw"])
        except (KeyError, TypeError, ValueError) as error:
            raise SpatialResolutionRequired(
                {
                    "status": "ARM_ALIGNMENT_INVALID",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The reviewed world-to-arm transform has no valid "
                        f"rotation: {error}"
                    ),
                }
            ) from error
        translated_world = tuple(
            point_world[index] - world_from_arm_translation[index]
            for index in range(3)
        )
        point_arm = tuple(
            sum(
                world_from_arm[row][column] * translated_world[row]
                for row in range(3)
            )
            for column in range(3)
        )
        return WorldPointResolution(
            target_position_world_m=point_world,
            target_position_arm_base_m=point_arm,
            provenance={
                "resolution_source": (
                    "TIMESTAMPED_WORLD_FROM_ARM_RIGID_TRANSFORM"
                ),
                "arm_base_frame": self.arm_base_frame,
                "source_frame": world_frame,
                "world_frame": world_frame,
                "session_epoch": session_epoch,
                "resolved_at_us": int(
                    transform.get("at_us") or observed_at_us
                ),
                "transform_path": list(transform.get("path") or []),
                "world_from_arm_translation_m": list(
                    world_from_arm_translation
                ),
                "world_from_arm_rotation_xyzw": [
                    float(value)
                    for value in transform["rotation_xyzw"]
                ],
                "operator_attestation": None,
                "workcell_activation": (
                    {
                        "activation_id": activation.get("activation_id"),
                        "calibration_revision": activation.get(
                            "calibration_revision"
                        ),
                        "validity_policy": activation.get(
                            "validity_policy"
                        ),
                    }
                    if use_workcell_activation and activation is not None
                    else None
                ),
            },
        )

    def _active_workcell_activation(
        self,
        observation: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(observation, dict):
            return None
        if (
            observation.get("valid") is not True
            or observation.get("stream")
            != "manager.workcell_calibration.activation"
            or observation.get("schema")
            != "physical_agent.workcell_calibration_activation"
            or observation.get("provider_id")
            != "manager.workcell_calibration"
        ):
            return None
        data = observation.get("data")
        if not isinstance(data, dict):
            return None
        if (
            data.get("state") != "ACTIVE"
            or data.get("motion_usable") is not True
            or data.get("expires_at") is not None
            or data.get("expires_at_us") is not None
            or data.get("convention_id") != WORLD_CONVENTION_ID
            or str(data.get("arm_base_frame") or "")
            != self.arm_base_frame
            or not str(data.get("world_frame") or "").strip()
            or not str(data.get("activation_id") or "").strip()
            or not str(data.get("calibration_revision") or "").strip()
            or str(observation.get("calibration_revision") or "")
            != str(data.get("calibration_revision") or "")
        ):
            return None
        return data

    async def _resolve_from_canonical_workcell_activation(
        self,
        *,
        direction: str,
        source_vector: tuple[float, float, float],
    ) -> SpatialResolution | None:
        observation = await self.fabric.latest_optional(
            "manager.workcell_calibration.activation"
        )
        activation = self._active_workcell_activation(observation)
        if activation is None:
            return None
        if (
            activation.get("validity_policy")
            != CANONICAL_CAMERA_CALIBRATION_POLICY
        ):
            return None
        transforms = activation.get("transforms")
        transform = (
            transforms.get("world_from_base")
            if isinstance(transforms, dict)
            else None
        )
        if not isinstance(transform, dict):
            raise SpatialResolutionRequired(
                {
                    "status": "ARM_ALIGNMENT_INVALID",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The active canonical-camera workcell calibration "
                        "has no valid world-from-arm-base transform."
                    ),
                }
            )
        try:
            world_from_arm = rotation_matrix(transform["rotation_xyzw"])
        except (KeyError, TypeError, ValueError) as error:
            raise SpatialResolutionRequired(
                {
                    "status": "ARM_ALIGNMENT_INVALID",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The active canonical-camera workcell calibration "
                        f"has no valid rotation: {error}"
                    ),
                }
            ) from error
        vector_arm = tuple(
            sum(
                world_from_arm[row][column] * source_vector[row]
                for row in range(3)
            )
            for column in range(3)
        )
        observed_at_us = int(
            (observation or {}).get("observed_at_us")
            or time.time_ns() // 1000
        )
        return SpatialResolution(
            direction=direction,
            reference_frame="WORLD",
            vector_arm_base=_normalize(vector_arm),
            provenance={
                "resolution_source": (
                    "ACTIVE_CANONICAL_WORKCELL_WORLD_FROM_ARM_TRANSFORM"
                ),
                "arm_base_frame": self.arm_base_frame,
                "source_frame": str(activation["world_frame"]),
                "world_frame": str(activation["world_frame"]),
                "session_epoch": str(
                    activation.get("session_epoch") or ""
                ),
                "resolved_at_us": observed_at_us,
                "transform_path": [
                    {
                        "authority": "manager.workcell_calibration",
                        "activation_id": activation.get("activation_id"),
                        "calibration_revision": activation.get(
                            "calibration_revision"
                        ),
                    }
                ],
                "expected_direction_world": list(source_vector),
                "operator_attestation": None,
                "workcell_activation": {
                    "activation_id": activation.get("activation_id"),
                    "calibration_revision": activation.get(
                        "calibration_revision"
                    ),
                    "validity_policy": activation.get("validity_policy"),
                },
                "vio_dependency": (
                    "NOT_REQUIRED_BY_CANONICAL_CAMERA_CALIBRATION_POLICY"
                ),
            },
        )

    def _attested_arm_mount_resolution(
        self,
        *,
        direction: str,
        source_vector: tuple[float, float, float],
        world_frame: str | None = None,
        session_epoch: str | None = None,
        observed_at_us: int | None = None,
        transform_error: str | None = None,
    ) -> SpatialResolution:
        """Resolve through the confirmed arm mount without consulting VIO."""

        resolved_at_us = int(observed_at_us or time.time_ns() // 1000)
        attested_frame = "operator_attested_x_forward_y_left_z_up"
        provenance: dict[str, Any] = {
            "resolution_source": "OPERATOR_ATTESTED_IDENTITY_ROTATION",
            "arm_base_frame": self.arm_base_frame,
            "source_frame": world_frame or attested_frame,
            "world_frame": world_frame,
            "session_epoch": session_epoch,
            "resolved_at_us": resolved_at_us,
            "transform_path": [],
            "vio_dependency": "NOT_REQUIRED_FOR_ARM_MOUNT_ASSUMPTION",
            "expected_direction_world": list(source_vector),
            "operator_attestation": {
                "confirmed": True,
                "scope": "BOUNDED_DEVELOPMENT_ONLY",
                "statement": (
                    "arm-base +Z is opposite gravity and arm-base +X is "
                    "aligned with workcell forward"
                ),
                "arm_base_frame": self.arm_base_frame,
                "world_frame": world_frame,
                "session_epoch": session_epoch,
            },
        }
        if transform_error:
            provenance["transform_error"] = transform_error
        return SpatialResolution(
            direction=direction,
            reference_frame="WORLD",
            vector_arm_base=source_vector,
            provenance=provenance,
        )

    async def _current_vio(self) -> dict[str, Any]:
        observation = await self.fabric.latest_optional(
            "localization.vio.status"
        )
        data = (
            observation.get("data")
            if isinstance(observation, dict)
            else None
        )
        if not isinstance(data, dict):
            raise SpatialResolutionRequired(
                {
                    "status": "WORLD_FRAME_UNAVAILABLE",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "Initialize space cognition before using semantic "
                        "three-dimensional directions."
                    ),
                }
            )
        if data.get("convention_id") != WORLD_CONVENTION_ID:
            raise SpatialResolutionRequired(
                {
                    "status": "SPATIAL_CONVENTION_MISMATCH",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The active VIO epoch is not convention V2. Reset VIO "
                        "and regenerate every dependent alignment."
                    ),
                }
            )
        if data.get("tracking_state") != "TRACKING":
            raise SpatialResolutionRequired(
                {
                    "status": "WORLD_TRACKING_UNUSABLE",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": "Current VIO tracking is not usable for motion.",
                }
            )
        world_frame = str(data.get("world_frame") or "")
        session_epoch = str(data.get("session_epoch") or "")
        observed_at_us = int(
            observation.get("observed_at_us") or time.time_ns() // 1000
        )
        if not world_frame or not session_epoch:
            raise SpatialResolutionRequired(
                {
                    "status": "WORLD_FRAME_INCOMPLETE",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": "VIO status has no complete frame/epoch identity.",
                }
            )
        return {
            "world_frame": world_frame,
            "session_epoch": session_epoch,
            "observed_at_us": observed_at_us,
            "camera_level_frame": str(
                data.get("camera_level_frame") or ""
            ),
        }

    async def _query_transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        session_epoch: str,
        failure_status: str,
    ) -> dict[str, Any]:
        try:
            return await self.fabric.transform(
                from_frame=from_frame,
                to_frame=to_frame,
                at_us=at_us,
                max_extrapolation_us=(
                    self.maximum_transform_extrapolation_us
                ),
                session_epoch=session_epoch,
            )
        except Exception as error:
            raise SpatialResolutionRequired(
                {
                    "status": failure_status,
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        f"No current transform path from {from_frame} to "
                        f"{to_frame}: {error}"
                    ),
                }
            ) from error


def rotation_matrix(
    quaternion_xyzw: Any,
) -> tuple[tuple[float, float, float], ...]:
    values = tuple(float(value) for value in quaternion_xyzw)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("rotation_xyzw must contain four finite values")
    x, y, z, w = values
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm <= 1e-12:
        raise ValueError("rotation_xyzw has zero norm")
    x, y, z, w = (value / norm for value in values)
    return (
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y - z * w),
            2.0 * (x * z + y * w),
        ),
        (
            2.0 * (x * y + z * w),
            1.0 - 2.0 * (x * x + z * z),
            2.0 * (y * z - x * w),
        ),
        (
            2.0 * (x * z - y * w),
            2.0 * (y * z + x * w),
            1.0 - 2.0 * (x * x + y * y),
        ),
    )


def _rotate_vector(
    quaternion_xyzw: Any,
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    rotation = rotation_matrix(quaternion_xyzw)
    return tuple(
        sum(rotation[row][column] * vector[column] for column in range(3))
        for row in range(3)
    )


def _normalize(
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        raise ValueError("resolved direction has zero length")
    return tuple(value / norm for value in vector)


def _is_missing_transform_error(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == 404:
        return True
    message = str(error).strip().lower()
    return any(
        marker in message
        for marker in (
            "no transform path",
            "transform path not found",
            "no path from",
        )
    )
