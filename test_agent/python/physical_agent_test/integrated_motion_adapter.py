from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
import numpy as np

from .spatial_frames import (
    SpatialFrameResolver,
    SpatialResolution,
    SpatialResolutionRequired,
    rotation_matrix,
)


_EXPLICIT_WORLD_AXIS_DIRECTIONS = {
    "POSITIVE_X",
    "NEGATIVE_X",
    "POSITIVE_Y",
    "NEGATIVE_Y",
    "POSITIVE_Z",
    "NEGATIVE_Z",
}

_VIO_READINESS_FAILURES = {
    "WORLD_FRAME_UNAVAILABLE",
    "WORLD_TRACKING_UNUSABLE",
    "WORLD_FRAME_INCOMPLETE",
}

_POSITION_ONLY = "POSITION_ONLY"
_PRESERVE_MEASURED_ORIENTATION = "PRESERVE_MEASURED_CONTROLLED_FRAME"
_APPLY_CONTROLLED_FRAME_YAW_DELTA = (
    "APPLY_CONTROLLED_FRAME_YAW_DELTA"
)
MAX_CONTROLLED_FRAME_YAW_DELTA_DEG = 45.0
DEFAULT_RELATIVE_DURATION_S = 3.0
MIN_RELATIVE_DURATION_S = 0.05
MAX_RELATIVE_DURATION_S = 60.0
MAX_RELATIVE_TRANSLATION_M = 1.2
DEFAULT_RELATIVE_NOMINAL_SPEED_M_S = 5.0
JOINT_SPEED_AUTHENTICATION_THRESHOLD_RAD_S = 10.0
JOINT_SPEED_HARD_LIMIT_RAD_S = 20.0
_REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED = (
    "REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED"
)
_ENDPOINT_JOINT_TRAVEL_REASON_PREFIX = (
    "IK endpoint requires excessive joint travel on joints "
)
_AGGREGATE_JOINT_TRAVEL_REASON = (
    "IK path has excessive aggregate joint travel"
)
_POLICY_CLASSIFICATION_POSITION_RESIDUAL_CEILING_M = 0.0015
_POLICY_CLASSIFICATION_ORIENTATION_RESIDUAL_CEILING_RAD = 0.035
_BASIC_MOTION_CAPABILITY = "robot.motion.arm.basic"
_INTEGRATED_ONE_SHOT_CAPABILITY = (
    "robot.motion.arm.integrated.pos_vel.one_shot"
)


class IntegratedPreviewRejected(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        response: dict[str, Any],
        plan: dict[str, Any],
    ):
        super().__init__(message)
        self.response = response
        self.plan = plan


class IntegratedMotionClientProtocol(Protocol):
    async def state(self) -> dict[str, Any]:
        """Return the current Integrated Controller state."""

    async def preview_direct_motion(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage and preview a nonphysical Cartesian target."""

    async def engage_staged_motion(self) -> dict[str, Any]:
        """Execute the currently staged and previewed target."""

    async def trigger_one_shot_motion(self) -> dict[str, Any]:
        """Pulse and release the Integrated one-shot commit input."""


class IntegratedRelativeMotionAdapter:
    """Resolve, preview, and approve one relative Cartesian motion."""

    def __init__(
        self,
        client: IntegratedMotionClientProtocol,
        spatial_resolver: SpatialFrameResolver,
        *,
        approval_ttl_s: float = 120.0,
        vio_readiness_checker: (
            Callable[[str], Awaitable[dict[str, Any]]] | None
        ) = None,
        visual_evidence_capture: (
            Callable[[str], Awaitable[dict[str, Any]]] | None
        ) = None,
        require_visual_verification: bool = False,
        attempt_visual_verification: bool = False,
        require_upright_mount_confirmation: bool = False,
        calibration_activation_continuation: (
            Callable[[], dict[str, Any] | None] | None
        ) = None,
    ):
        self.client = client
        self.spatial_resolver = spatial_resolver
        self.approval_ttl_s = float(approval_ttl_s)
        self.vio_readiness_checker = vio_readiness_checker
        self.visual_evidence_capture = visual_evidence_capture
        self.require_visual_verification = bool(
            require_visual_verification
        )
        self.attempt_visual_verification = bool(
            attempt_visual_verification or require_visual_verification
        )
        self.require_upright_mount_confirmation = bool(
            require_upright_mount_confirmation
        )
        self.calibration_activation_continuation = (
            calibration_activation_continuation
        )
        self._pending: dict[str, dict[str, Any]] = {}
        self._root_alignment_correspondences: list[dict[str, Any]] = []
        self._root_alignment_identity: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    async def observation(self) -> dict[str, Any]:
        """Return a read-only view of the Skill workflow and controller."""
        now = time.monotonic()
        async with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if now - value["created_monotonic"] <= self.approval_ttl_s
            }
            pending = [
                {
                    "preview_id": preview_id,
                    "motion_intent": value["motion_intent"],
                    "direction": value["direction"],
                    "reference_frame": value["reference_frame"],
                    "resolved_direction_arm_base": value[
                        "resolved_direction_arm_base"
                    ],
                    "distance_m": value["distance_m"],
                    "requested_speed_m_s": value["requested_speed_m_s"],
                    "requested_duration_s": value["requested_duration_s"],
                    "planned_duration_s": value["planned_duration_s"],
                    "planned_nominal_speed_m_s": value[
                        "planned_nominal_speed_m_s"
                    ],
                    "timing_safety_limited": value[
                        "timing_safety_limited"
                    ],
                    "requested_peak_joint_speed_rad_s": value[
                        "requested_peak_joint_speed_rad_s"
                    ],
                    "effective_peak_joint_speed_rad_s": value[
                        "effective_peak_joint_speed_rad_s"
                    ],
                    "joint_speed_authentication_required": value[
                        "joint_speed_authentication_required"
                    ],
                    "start_position_m": value["start_position_m"],
                    "target_position_m": value["target_position_m"],
                    "orientation_policy": value["orientation_policy"],
                    "controlled_frame_yaw_delta_deg": value[
                        "controlled_frame_yaw_delta_deg"
                    ],
                    "target_orientation_rpy_rad": value[
                        "target_orientation_rpy_rad"
                    ],
                    "expires_in_s": max(
                        0.0,
                        self.approval_ttl_s
                        - (now - value["created_monotonic"]),
                    ),
                }
                for preview_id, value in self._pending.items()
            ]
        try:
            controller = await self.client.state()
        except Exception as error:
            controller = {
                "status": "UNAVAILABLE",
                "error": str(error),
            }
        return {
            "skill": "integrated_relative_effector_motion",
            "pending_previews": pending,
            "controller": controller,
            "read_only": True,
        }

    async def preview(
        self,
        *,
        direction: str,
        distance_m: float,
        requested_speed_m_s: float | None = None,
        reference_frame: str = "WORLD",
        arm_mount_assumption: str = "UNKNOWN",
        camera_level_assumption: str = "UNKNOWN",
        fixed_vio_rig_assumption: str = "UNKNOWN",
        orientation_policy: str = _POSITION_ONLY,
        controlled_frame_yaw_delta_deg: float | None = None,
    ) -> dict[str, Any]:
        distance = float(distance_m)
        if (
            not math.isfinite(distance)
            or distance < 0.0
            or distance > MAX_RELATIVE_TRANSLATION_M
            or 0.0 < distance < 0.001
        ):
            raise ValueError(
                "distance_m must be zero for rotation-only motion or "
                f"between 0.001 and {MAX_RELATIVE_TRANSLATION_M:g} for "
                "translation"
            )
        requested_speed: float | None = None
        requested_duration_s = DEFAULT_RELATIVE_DURATION_S
        if distance == 0.0 and requested_speed_m_s is not None:
            raise ValueError(
                "requested_speed_m_s is not applicable to rotation-only "
                "motion"
            )
        if requested_speed_m_s is not None:
            try:
                requested_speed = float(requested_speed_m_s)
            except (TypeError, ValueError):
                requested_speed = math.nan
            if (
                not math.isfinite(requested_speed)
                or requested_speed <= 0.0
            ):
                return self._unsupported_timing_request(
                    distance_m=distance,
                    requested_speed_m_s=requested_speed_m_s,
                    requested_duration_s=None,
                    reason=(
                        "requested_speed_m_s must be finite and positive"
                    ),
                )
            requested_duration_s = distance / requested_speed
            if requested_duration_s > MAX_RELATIVE_DURATION_S:
                return self._unsupported_timing_request(
                    distance_m=distance,
                    requested_speed_m_s=requested_speed,
                    requested_duration_s=requested_duration_s,
                    reason=(
                        "distance divided by requested speed exceeds the "
                        f"{MAX_RELATIVE_DURATION_S:g}-second controller window"
                    ),
                )
        normalized_reference_frame = str(
            reference_frame or "WORLD"
        ).strip().upper()
        normalized_mount_assumption = str(
            arm_mount_assumption or "UNKNOWN"
        ).strip().upper()
        normalized_fixed_rig_assumption = str(
            fixed_vio_rig_assumption or "UNKNOWN"
        ).strip().upper()
        normalized_direction_input = str(
            direction or ""
        ).strip().upper()
        normalized_orientation_policy = str(
            orientation_policy or _POSITION_ONLY
        ).strip().upper()
        if normalized_orientation_policy not in {
            _POSITION_ONLY,
            _PRESERVE_MEASURED_ORIENTATION,
            _APPLY_CONTROLLED_FRAME_YAW_DELTA,
        }:
            raise ValueError(
                "orientation_policy must be POSITION_ONLY or "
                "PRESERVE_MEASURED_CONTROLLED_FRAME or "
                "APPLY_CONTROLLED_FRAME_YAW_DELTA"
            )
        if (distance == 0.0) != (normalized_direction_input == "NONE"):
            raise ValueError(
                "direction must be NONE exactly when distance_m is zero"
            )
        normalized_yaw_delta_deg: float | None = None
        if normalized_orientation_policy == _APPLY_CONTROLLED_FRAME_YAW_DELTA:
            try:
                normalized_yaw_delta_deg = float(
                    controlled_frame_yaw_delta_deg
                )
            except (TypeError, ValueError):
                normalized_yaw_delta_deg = math.nan
            if (
                not math.isfinite(normalized_yaw_delta_deg)
                or abs(normalized_yaw_delta_deg) < 1e-9
                or abs(normalized_yaw_delta_deg)
                > MAX_CONTROLLED_FRAME_YAW_DELTA_DEG
            ):
                raise ValueError(
                    "controlled_frame_yaw_delta_deg must be a finite, "
                    "nonzero value from -45 to 45 for "
                    "APPLY_CONTROLLED_FRAME_YAW_DELTA"
                )
        elif controlled_frame_yaw_delta_deg is not None:
            raise ValueError(
                "controlled_frame_yaw_delta_deg must be null unless "
                "orientation_policy is APPLY_CONTROLLED_FRAME_YAW_DELTA"
            )
        if distance == 0.0 and (
            normalized_orientation_policy
            != _APPLY_CONTROLLED_FRAME_YAW_DELTA
        ):
            raise ValueError(
                "rotation-only motion requires "
                "APPLY_CONTROLLED_FRAME_YAW_DELTA"
            )
        motion_intent = (
            "NEW_RELATIVE_ROTATION"
            if distance == 0.0
            else "NEW_RELATIVE_POSE_MOVE"
            if normalized_orientation_policy
            == _APPLY_CONTROLLED_FRAME_YAW_DELTA
            else "NEW_RELATIVE_MOVE"
        )
        readiness: dict[str, Any] | None = None
        try:
            if distance == 0.0:
                resolution = SpatialResolution(
                    direction="NONE",
                    reference_frame="CONTROLLED_FRAME",
                    vector_arm_base=(0.0, 0.0, 0.0),
                    provenance={
                        "resolution_source": (
                            "ROTATION_ONLY_NO_TRANSLATION"
                        ),
                        "arm_base_frame": (
                            self.spatial_resolver.arm_base_frame
                        ),
                    },
                )
            else:
                resolution = await self.spatial_resolver.resolve(
                    direction=direction,
                    reference_frame=reference_frame,
                    arm_mount_assumption=arm_mount_assumption,
                    camera_level_assumption=camera_level_assumption,
                )
        except SpatialResolutionRequired as required:
            if _is_vio_readiness_failure(required.payload):
                if (
                    normalized_reference_frame == "WORLD"
                    and normalized_direction_input
                    not in _EXPLICIT_WORLD_AXIS_DIRECTIONS
                    and normalized_mount_assumption == "UNKNOWN"
                    and normalized_fixed_rig_assumption
                    != "CONFIRMED_FIXED_STATIONARY_RIG"
                ):
                    return self._arm_mount_confirmation_required(
                        direction=direction,
                        reference_frame=normalized_reference_frame,
                        distance_m=distance,
                        vio_failure=required.payload,
                    )
                if (
                    normalized_fixed_rig_assumption
                    != "CONFIRMED_FIXED_STATIONARY_RIG"
                ):
                    if (
                        normalized_fixed_rig_assumption
                        == "REJECTED_OR_UNKNOWN"
                    ):
                        if self.require_visual_verification:
                            return self._visual_verification_declined(
                                direction=direction,
                                reference_frame=(
                                    normalized_reference_frame
                                ),
                                distance_m=distance,
                            )
                        return {
                            **required.payload,
                            "requested_direction": (
                                normalized_direction_input
                            ),
                            "requested_reference_frame": (
                                normalized_reference_frame
                            ),
                            "distance_m": distance,
                        }
                    return self._fixed_vio_rig_confirmation_required(
                        direction=direction,
                        reference_frame=normalized_reference_frame,
                        distance_m=distance,
                        vio_failure=required.payload,
                    )
                readiness = await self._check_fixed_vio_rig_readiness(
                    direction=direction,
                    reference_frame=normalized_reference_frame,
                    distance_m=distance,
                    vio_failure=required.payload,
                )
                if readiness.get("status") != "VIO_TRACKING_READY":
                    return readiness
                try:
                    resolution = await self.spatial_resolver.resolve(
                        direction=direction,
                        reference_frame=reference_frame,
                        arm_mount_assumption=arm_mount_assumption,
                        camera_level_assumption=camera_level_assumption,
                    )
                except SpatialResolutionRequired as retry_required:
                    return {
                        **retry_required.payload,
                        "requested_direction": str(
                            direction or ""
                        ).strip().upper(),
                        "requested_reference_frame": (
                            normalized_reference_frame
                        ),
                        "distance_m": distance,
                        "vio_readiness_check": readiness.get("result"),
                    }
            else:
                if (
                    self.require_upright_mount_confirmation
                    and normalized_reference_frame == "WORLD"
                    and normalized_direction_input
                    not in _EXPLICIT_WORLD_AXIS_DIRECTIONS
                    and normalized_mount_assumption == "UNKNOWN"
                    and required.payload.get("status")
                    == "ARM_ALIGNMENT_OR_ATTESTATION_REQUIRED"
                ):
                    return self._arm_mount_confirmation_required(
                        direction=direction,
                        reference_frame=normalized_reference_frame,
                        distance_m=distance,
                        vio_failure=None,
                    )
                payload = {
                    **required.payload,
                    "requested_direction": str(
                        direction or ""
                    ).strip().upper(),
                    "requested_reference_frame": normalized_reference_frame,
                    "distance_m": distance,
                }
                if (
                    payload.get("status")
                    == "WORLD_TO_ARM_ALIGNMENT_REQUIRED"
                ):
                    continuation = None
                    if self.calibration_activation_continuation is not None:
                        try:
                            continuation = (
                                self.calibration_activation_continuation()
                            )
                        except Exception:
                            continuation = None
                    if continuation is None:
                        continuation = {
                            "name": "calibrate_stationary_workcell",
                            "arguments": {
                                "request": (
                                    "Create a current stationary world-to-arm "
                                    "calibration candidate for this explicit "
                                    "world-axis motion."
                                ),
                            },
                        }
                    payload["required_next_tool"] = continuation
                    payload["agent_instruction"] = (
                        "Call required_next_tool immediately with unchanged "
                        "arguments. If it returns motion_usable=true, retry "
                        "this original preview request without an arm-mount "
                        "assumption."
                    )
                return payload
        normalized_direction = resolution.direction
        normalized_reference_frame = resolution.reference_frame
        visual_baseline: dict[str, Any] | None = None
        visual_context: dict[str, Any] | None = None
        visual_baseline_status: dict[str, Any] | None = None
        if distance == 0.0:
            visual_baseline_status = {
                "required": False,
                "status": (
                    "SKIPPED_ROTATION_ONLY_NO_ORIENTATION_EVIDENCE"
                ),
                "reason": (
                    "The current before/after visual check measures a 3D "
                    "landmark translation, not controlled-frame "
                    "orientation. It was not used to define or veto this "
                    "rotation-only IK preview."
                ),
            }
        elif self.attempt_visual_verification:
            if (
                normalized_fixed_rig_assumption
                != "CONFIRMED_FIXED_STATIONARY_RIG"
            ):
                if self.require_visual_verification and (
                    normalized_fixed_rig_assumption
                    == "REJECTED_OR_UNKNOWN"
                ):
                    return self._visual_verification_declined(
                        direction=direction,
                        reference_frame=normalized_reference_frame,
                        distance_m=distance,
                    )
                if self.require_visual_verification:
                    return self._fixed_vio_rig_confirmation_required(
                        direction=direction,
                        reference_frame=normalized_reference_frame,
                        distance_m=distance,
                        vio_failure=None,
                    )
                visual_baseline_status = {
                    "required": False,
                    "status": "SKIPPED_FIXED_RIG_NOT_CONFIRMED",
                    "reason": (
                        "Visual verification was not attempted because the "
                        "camera/IMU fixed-rig condition was not confirmed. "
                        "This does not affect the operator-confirmed arm "
                        "direction or the IK preview."
                    ),
                }
            elif readiness is None:
                readiness = await self._check_fixed_vio_rig_readiness(
                    direction=direction,
                    reference_frame=normalized_reference_frame,
                    distance_m=distance,
                    vio_failure={},
                )
                if readiness.get("status") != "VIO_TRACKING_READY":
                    if self.require_visual_verification:
                        return readiness
                    visual_baseline_status = (
                        self._best_effort_visual_unavailable(
                            "VIO readiness check was unavailable",
                            detail=readiness,
                        )
                    )
                elif (
                    resolution.provenance.get("resolution_source")
                    == "OPERATOR_ATTESTED_IDENTITY_ROTATION"
                ):
                    try:
                        resolution = await self.spatial_resolver.resolve(
                            direction=direction,
                            reference_frame=reference_frame,
                            arm_mount_assumption=arm_mount_assumption,
                            camera_level_assumption=(
                                camera_level_assumption
                            ),
                        )
                    except SpatialResolutionRequired as required:
                        return {
                            **required.payload,
                            "requested_direction": (
                                normalized_direction_input
                            ),
                            "requested_reference_frame": (
                                normalized_reference_frame
                            ),
                            "distance_m": distance,
                            "vio_readiness_check": readiness.get("result"),
                        }
            if (
                normalized_fixed_rig_assumption
                == "CONFIRMED_FIXED_STATIONARY_RIG"
                and readiness is not None
                and readiness.get("status") == "VIO_TRACKING_READY"
            ):
                expected_world = resolution.provenance.get(
                    "expected_direction_world"
                )
                if not _is_finite_vector(expected_world):
                    if self.require_visual_verification:
                        return {
                            "status": "VISUAL_DIRECTION_ALIGNMENT_REQUIRED",
                            "workflow_complete": False,
                            "physical_motion_authorized": False,
                            "message": (
                                "Before/after visual verification requires "
                                "the commanded direction expressed in the "
                                "gravity-aligned world frame. Use a WORLD or "
                                "CAMERA_LEVEL semantic direction with current "
                                "spatial alignment."
                            ),
                        }
                    visual_baseline_status = (
                        self._best_effort_visual_unavailable(
                            "Command direction has no world-frame visual "
                            "verification vector"
                        )
                    )
                else:
                    identity = _vio_identity_from_readiness(readiness)
                    if identity is None:
                        if self.require_visual_verification:
                            return {
                                "status": (
                                    "VIO_READINESS_IDENTITY_INCOMPLETE"
                                ),
                                "workflow_complete": False,
                                "physical_motion_authorized": False,
                                "message": (
                                    "The VIO readiness check reached TRACKING "
                                    "but did not return a complete world-frame "
                                    "and epoch identity."
                                ),
                                "vio_readiness_check": readiness.get(
                                    "result"
                                ),
                            }
                        visual_baseline_status = (
                            self._best_effort_visual_unavailable(
                                "VIO tracking identity was incomplete",
                                detail=readiness.get("result"),
                            )
                        )
                    elif self.visual_evidence_capture is None:
                        if self.require_visual_verification:
                            return {
                                "status": (
                                    "VISUAL_EVIDENCE_CAPTURE_UNAVAILABLE"
                                ),
                                "workflow_complete": False,
                                "physical_motion_authorized": False,
                                "message": (
                                    "Before/after visual verification is "
                                    "required, but this Agent runtime has no "
                                    "effector evidence capture."
                                ),
                            }
                        visual_baseline_status = (
                            self._best_effort_visual_unavailable(
                                "Effector evidence capture is unavailable"
                            )
                        )
                    else:
                        try:
                            visual_baseline = (
                                await self.visual_evidence_capture(
                                    identity["world_frame"]
                                )
                            )
                            _visual_point(visual_baseline)
                        except Exception as error:
                            if self.require_visual_verification:
                                return {
                                    "status": "VISUAL_BASELINE_FAILED",
                                    "workflow_complete": False,
                                    "physical_motion_authorized": False,
                                    "message": (
                                        "The pre-move effector picture or 3D "
                                        "landmark could not be established: "
                                        f"{error}"
                                    ),
                                }
                            visual_baseline_status = (
                                self._best_effort_visual_unavailable(
                                    "Pre-move effector 3D evidence was "
                                    "unavailable",
                                    detail={"error": str(error)},
                                )
                            )
                        else:
                            visual_context = {
                                **identity,
                                "fixed_rig_attestation": {
                                    "confirmed": True,
                                    "statement": (
                                        "camera and IMU are a rigidly fixed "
                                        "VIO rig and remained stationary for "
                                        "the check"
                                    ),
                                    "reuse_policy": (
                                        "THIS_PREVIEW_AND_EXECUTION_ONLY"
                                    ),
                                },
                                "expected_direction_world": [
                                    float(value)
                                    for value in expected_world
                                ],
                                "before": visual_baseline,
                            }
                            visual_baseline_status = {
                                "required": (
                                    self.require_visual_verification
                                ),
                                "status": "BEFORE_EVIDENCE_READY",
                                "world_frame": visual_context["world_frame"],
                                "session_epoch": visual_context[
                                    "session_epoch"
                                ],
                                "before": visual_baseline,
                            }

        try:
            state = await self.client.state()
        except httpx.RequestError as exc:
            return self._dependency_unavailable(str(exc))
        if (
            state.get("residency") != "HOT"
            or state.get("ready") is not True
        ):
            return self._dependency_unavailable(
                "Integrated Controller is not HOT and ready",
                state=state,
            )
        attestation = resolution.provenance.get(
            "operator_attestation"
        )
        if isinstance(attestation, dict) and attestation.get("confirmed"):
            controller_identity = state.get("controller_identity")
            if (
                not isinstance(controller_identity, dict)
                or not controller_identity.get("provider_instance_id")
                or not controller_identity.get("boot_id")
                or not controller_identity.get("configuration_sha256")
            ):
                return {
                    "status": "CONTROLLER_IDENTITY_REQUIRED",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "The controller did not expose enough identity "
                        "metadata to bind the mount attestation. Restart or "
                        "upgrade the Integrated Controller."
                    ),
                }
            attestation["controller_identity"] = dict(
                controller_identity
            )
            attestation["reuse_policy"] = "THIS_PREVIEW_ONLY"
        model_view = state.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        current = measured.get("position_m")
        if (
            not isinstance(current, list)
            or len(current) != 3
            or not all(
                isinstance(value, (int, float)) and math.isfinite(float(value))
                for value in current
            )
        ):
            raise RuntimeError(
                "Integrated Controller has no measured controlled-frame pose"
            )
        vector = resolution.vector_arm_base
        target = [
            float(current[index]) + vector[index] * distance
            for index in range(3)
        ]
        target_orientation_rpy_rad: list[float] | None = None
        if (
            normalized_orientation_policy
            in {
                _PRESERVE_MEASURED_ORIENTATION,
                _APPLY_CONTROLLED_FRAME_YAW_DELTA,
            }
        ):
            measured_orientation = measured.get("rpy_rad")
            if not _is_finite_vector(measured_orientation):
                return {
                    "status": "ORIENTATION_STATE_REQUIRED",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "message": (
                        "This POSE_6DOF relative motion requires a current "
                        "measured controlled-frame orientation from the "
                        "Integrated Controller."
                    ),
                }
            if (
                normalized_orientation_policy
                == _APPLY_CONTROLLED_FRAME_YAW_DELTA
            ):
                target_orientation_rpy_rad = (
                    _apply_controlled_frame_yaw_delta(
                        measured_orientation,
                        math.radians(float(normalized_yaw_delta_deg)),
                    )
                )
            else:
                target_orientation_rpy_rad = [
                    float(value) for value in measured_orientation
                ]
        try:
            controller_requested_duration_s = max(
                MIN_RELATIVE_DURATION_S,
                requested_duration_s,
            )
            preview, preview_id = await self._stage_target(
                target,
                target_orientation_rpy_rad=target_orientation_rpy_rad,
                orientation_policy=normalized_orientation_policy,
                duration_s=controller_requested_duration_s,
            )
        except IntegratedPreviewRejected as error:
            policy_limit_diagnostics = (
                _one_shot_policy_limit_diagnostics(error.plan)
            )
            policy_limited = policy_limit_diagnostics is not None
            result = {
                "status": (
                    _REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED
                    if policy_limited
                    else "IK_PREVIEW_REJECTED"
                ),
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "direction": normalized_direction,
                "reference_frame": normalized_reference_frame,
                "resolved_direction_arm_base": list(vector),
                "spatial_resolution": resolution.as_dict(),
                "distance_m": distance,
                "requested_speed_m_s": requested_speed,
                "requested_duration_s": requested_duration_s,
                "start_position_m": [float(value) for value in current],
                "target_position_m": target,
                "orientation_policy": normalized_orientation_policy,
                "controlled_frame_yaw_delta_deg": (
                    normalized_yaw_delta_deg
                ),
                "start_orientation_rpy_rad": (
                    [float(value) for value in measured.get("rpy_rad", [])]
                    if normalized_orientation_policy
                    in {
                        _PRESERVE_MEASURED_ORIENTATION,
                        _APPLY_CONTROLLED_FRAME_YAW_DELTA,
                    }
                    else None
                ),
                "target_orientation_rpy_rad": (
                    list(target_orientation_rpy_rad)
                    if target_orientation_rpy_rad is not None
                    else None
                ),
                "message": (
                    "The controller found a collision-free IK solution "
                    "whose residuals passed its configured checks, but "
                    "rejected this one-shot preview solely because its "
                    "joint travel exceeded one or more configured "
                    "one-shot policy guards. No executable preview was "
                    "created, physical motion is not authorized, and "
                    "this adapter will not automatically segment the "
                    "request."
                    if policy_limited
                    else str(error)
                ),
                "controller_preview": error.plan,
                "controller_response_status": error.response.get("status"),
            }
            if policy_limited:
                result.update(
                    {
                        "classification": (
                            _REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED
                        ),
                        "policy_limit_diagnostics": (
                            policy_limit_diagnostics
                        ),
                        "controller_rejection_message": str(error),
                    }
                )
            return result
        preview_plan = preview.get("preview")
        preview_plan = (
            preview_plan if isinstance(preview_plan, dict) else {}
        )
        try:
            planned_duration_s = float(preview_plan.get("duration_s"))
        except (TypeError, ValueError):
            planned_duration_s = math.nan
        if (
            not math.isfinite(planned_duration_s)
            or planned_duration_s < MIN_RELATIVE_DURATION_S
            or planned_duration_s > MAX_RELATIVE_DURATION_S
            or planned_duration_s + 1e-9 < requested_duration_s
        ):
            raise RuntimeError(
                "Integrated Controller preview returned an invalid or "
                "shortened trajectory duration"
            )
        planned_nominal_speed_m_s = distance / planned_duration_s
        timing_safety_limited = (
            planned_duration_s > requested_duration_s + 1e-9
        )
        joint_speed_policy = preview_plan.get("joint_speed_policy")
        joint_speed_policy = (
            joint_speed_policy
            if isinstance(joint_speed_policy, dict)
            else {}
        )
        requested_peak_joint_speed_rad_s = float(
            joint_speed_policy.get(
                "requested_peak_joint_speed_rad_s", 0.0
            )
        )
        if requested_duration_s < MIN_RELATIVE_DURATION_S:
            requested_peak_joint_speed_rad_s *= (
                MIN_RELATIVE_DURATION_S / max(requested_duration_s, 1e-12)
            )
        effective_peak_joint_speed_rad_s = float(
            joint_speed_policy.get(
                "effective_peak_joint_speed_rad_s", 0.0
            )
        )
        joint_speed_authentication_required = bool(
            joint_speed_policy.get("authentication_required", False)
        ) or (
            requested_peak_joint_speed_rad_s
            > JOINT_SPEED_AUTHENTICATION_THRESHOLD_RAD_S
        )
        if bool(joint_speed_policy.get("hard_limit_exceeded", False)) or (
            requested_peak_joint_speed_rad_s
            >= JOINT_SPEED_HARD_LIMIT_RAD_S
        ):
            raise RuntimeError(
                "Integrated Controller preview reaches or exceeds the "
                "20 rad/s per-joint hard limit"
            )
        pending = {
            "preview_id": preview_id,
            "controller_preview_id": preview_id,
            "motion_intent": motion_intent,
            "direction": normalized_direction,
            "reference_frame": normalized_reference_frame,
            "resolved_direction_arm_base": list(vector),
            "spatial_resolution": resolution.as_dict(),
            "distance_m": distance,
            "original_request_distance_m": distance,
            "requested_speed_m_s": requested_speed,
            "requested_duration_s": requested_duration_s,
            "planned_duration_s": planned_duration_s,
            "planned_nominal_speed_m_s": planned_nominal_speed_m_s,
            "timing_safety_limited": timing_safety_limited,
            "requested_peak_joint_speed_rad_s": (
                requested_peak_joint_speed_rad_s
            ),
            "effective_peak_joint_speed_rad_s": (
                effective_peak_joint_speed_rad_s
            ),
            "joint_speed_authentication_required": (
                joint_speed_authentication_required
            ),
            "start_position_m": [float(value) for value in current],
            "target_position_m": target,
            "orientation_policy": normalized_orientation_policy,
            "controlled_frame_yaw_delta_deg": normalized_yaw_delta_deg,
            "start_orientation_rpy_rad": (
                [float(value) for value in measured.get("rpy_rad", [])]
                if normalized_orientation_policy
                in {
                    _PRESERVE_MEASURED_ORIENTATION,
                    _APPLY_CONTROLLED_FRAME_YAW_DELTA,
                }
                else None
            ),
            "target_orientation_rpy_rad": (
                list(target_orientation_rpy_rad)
                if target_orientation_rpy_rad is not None
                else None
            ),
            "created_monotonic": time.monotonic(),
            "visual_verification": visual_context,
            "visual_baseline_status": visual_baseline_status,
        }
        async with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if time.monotonic() - value["created_monotonic"]
                <= self.approval_ttl_s
            }
            self._pending[preview_id] = pending
        required_next_arguments = {
            "preview_id": preview_id,
            "motion_intent": pending["motion_intent"],
            "direction": normalized_direction,
            "reference_frame": normalized_reference_frame,
            "resolved_direction_arm_base": list(vector),
            "distance_m": distance,
            "original_request_distance_m": distance,
            "requested_speed_m_s": requested_speed,
            "requested_duration_s": requested_duration_s,
            "planned_duration_s": planned_duration_s,
            "planned_nominal_speed_m_s": planned_nominal_speed_m_s,
            "timing_safety_limited": timing_safety_limited,
            "requested_peak_joint_speed_rad_s": (
                requested_peak_joint_speed_rad_s
            ),
            "effective_peak_joint_speed_rad_s": (
                effective_peak_joint_speed_rad_s
            ),
            "joint_speed_authentication_required": (
                joint_speed_authentication_required
            ),
            "target_position_m": target,
            "orientation_policy": normalized_orientation_policy,
            "controlled_frame_yaw_delta_deg": normalized_yaw_delta_deg,
            "target_orientation_rpy_rad": (
                list(target_orientation_rpy_rad)
                if target_orientation_rpy_rad is not None
                else None
            ),
        }
        return {
            "status": "PREVIEW_READY",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "motion_intent": pending["motion_intent"],
            "direction": normalized_direction,
            "reference_frame": normalized_reference_frame,
            "resolved_direction_arm_base": list(vector),
            "spatial_resolution": resolution.as_dict(),
            "distance_m": distance,
            "requested_speed_m_s": requested_speed,
            "requested_duration_s": requested_duration_s,
            "planned_duration_s": planned_duration_s,
            "planned_nominal_speed_m_s": planned_nominal_speed_m_s,
            "timing_safety_limited": timing_safety_limited,
            "requested_peak_joint_speed_rad_s": (
                requested_peak_joint_speed_rad_s
            ),
            "effective_peak_joint_speed_rad_s": (
                effective_peak_joint_speed_rad_s
            ),
            "joint_speed_authentication_required": (
                joint_speed_authentication_required
            ),
            "start_position_m": pending["start_position_m"],
            "target_position_m": target,
            "orientation_policy": normalized_orientation_policy,
            "orientation_reference_frame": (
                "CONTROLLED_FRAME"
                if normalized_yaw_delta_deg is not None
                else None
            ),
            "controlled_frame_yaw_delta_deg": normalized_yaw_delta_deg,
            "target_orientation_rpy_rad": (
                list(target_orientation_rpy_rad)
                if target_orientation_rpy_rad is not None
                else None
            ),
            "preview_id": preview_id,
            "approval_required": True,
            "next_tool": "execute_integrated_motion_preview",
            "required_next_tool": {
                "name": "execute_integrated_motion_preview",
                "arguments": required_next_arguments,
            },
            "message": (
                "The exact IK target is previewed but has not moved. Do not "
                "answer the operator yet. Call required_next_tool now with "
                "its arguments unchanged to present operator approval."
            ),
            "integrated_preview": preview,
            "visual_verification": (
                visual_baseline_status
                if visual_baseline_status is not None
                else {
                    "required": False,
                    "status": "NOT_CONFIGURED",
                }
            ),
        }

    @staticmethod
    def _best_effort_visual_unavailable(
        reason: str,
        *,
        detail: Any = None,
    ) -> dict[str, Any]:
        result = {
            "required": False,
            "status": "BEFORE_EVIDENCE_UNAVAILABLE",
            "reason": (
                f"{reason}. Visual evidence is best-effort and did not "
                "prevent creation of the IK preview."
            ),
        }
        if detail is not None:
            result["detail"] = detail
        return result

    @staticmethod
    def _unsupported_timing_request(
        *,
        distance_m: float,
        requested_speed_m_s: Any,
        requested_duration_s: float | None,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "status": "RELATIVE_MOTION_TIMING_UNSUPPORTED",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "distance_m": distance_m,
            "requested_speed_m_s": requested_speed_m_s,
            "requested_duration_s": requested_duration_s,
            "supported_duration_s": {
                "minimum": MIN_RELATIVE_DURATION_S,
                "maximum": MAX_RELATIVE_DURATION_S,
            },
            "message": reason,
        }

    @staticmethod
    def _arm_mount_confirmation_required(
        *,
        direction: str,
        reference_frame: str,
        distance_m: float,
        vio_failure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "status": "ARM_MOUNT_CONFIRMATION_REQUIRED",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "retry_same_tool": True,
            "question": (
                "Is the arm base mounted upright on a horizontal plane, with "
                "base +Z pointing opposite gravity and base +X pointing "
                "toward robot/workcell front? Confirm y/n."
            ),
            "required_value_on_yes": (
                "arm_mount_assumption=CONFIRMED_X_FORWARD_Z_UP"
            ),
            "required_value_on_no": (
                "arm_mount_assumption=REJECTED_OR_UNKNOWN"
            ),
            "next_step_on_no": (
                "The preview will ask whether a fixed stationary VIO rig may "
                "be checked for measured world alignment."
            ),
            "message": (
                "The default upright arm-mount path is independent of VIO. "
                "No preview or physical motion has been created."
            ),
            "vio_status": dict(vio_failure or {}),
            "requested_direction": str(direction or "").strip().upper(),
            "requested_reference_frame": reference_frame,
            "distance_m": distance_m,
        }

    @staticmethod
    def _fixed_vio_rig_confirmation_required(
        *,
        direction: str,
        reference_frame: str,
        distance_m: float,
        vio_failure: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "status": "FIXED_VIO_RIG_CONFIRMATION_REQUIRED",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "retry_same_tool": True,
            "question": (
                "Is the camera and IMU a rigidly fixed VIO rig that can "
                "remain completely stationary while tracking is checked? "
                "Confirm y/n."
            ),
            "required_value_on_yes": (
                "fixed_vio_rig_assumption="
                "CONFIRMED_FIXED_STATIONARY_RIG"
            ),
            "required_value_on_no": (
                "fixed_vio_rig_assumption=REJECTED_OR_UNKNOWN"
            ),
            "message": (
                "A non-destructive VIO readiness check may start or verify "
                "the camera and VIO providers only after this confirmation. "
                "It will not reset the VIO epoch."
            ),
            "vio_status": dict(vio_failure or {}),
            "requested_direction": str(direction or "").strip().upper(),
            "requested_reference_frame": reference_frame,
            "distance_m": distance_m,
        }

    async def _check_fixed_vio_rig_readiness(
        self,
        *,
        direction: str,
        reference_frame: str,
        distance_m: float,
        vio_failure: dict[str, Any],
    ) -> dict[str, Any]:
        if self.vio_readiness_checker is None:
            return {
                "status": "VIO_READINESS_CHECK_UNAVAILABLE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "message": (
                    "This Agent runtime has no non-destructive VIO readiness "
                    "checker. No VIO reset, preview, or motion was attempted."
                ),
                "vio_status": dict(vio_failure),
                "requested_direction": str(direction or "").strip().upper(),
                "requested_reference_frame": reference_frame,
                "distance_m": distance_m,
            }
        try:
            result = await self.vio_readiness_checker(
                "World-relative motion requires current measured spatial "
                "tracking and the operator confirmed a fixed stationary VIO rig"
            )
        except Exception as error:
            return {
                "status": "VIO_READINESS_CHECK_FAILED",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "message": (
                    "The non-destructive VIO readiness check did not reach "
                    f"TRACKING: {error}"
                ),
                "vio_status": dict(vio_failure),
                "requested_direction": str(direction or "").strip().upper(),
                "requested_reference_frame": reference_frame,
                "distance_m": distance_m,
            }
        return {
            "status": "VIO_TRACKING_READY",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "result": result,
        }

    @staticmethod
    def _visual_verification_declined(
        *,
        direction: str,
        reference_frame: str,
        distance_m: float,
    ) -> dict[str, Any]:
        return {
            "status": "VISUAL_VERIFICATION_DECLINED",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "retry_same_tool": False,
            "message": (
                "This runtime requires gravity-aligned before/after visual "
                "evidence for relative motion. The fixed-rig condition was "
                "not confirmed, so no preview or motion was created."
            ),
            "requested_direction": str(direction or "").strip().upper(),
            "requested_reference_frame": reference_frame,
            "distance_m": distance_m,
        }

    @staticmethod
    def _dependency_unavailable(
        reason: str,
        *,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        controller_state = state if isinstance(state, dict) else {}
        if (
            str(controller_state.get("residency") or "").upper()
            == "RECOVERY_REQUIRED"
        ):
            return {
                "status": "INTEGRATED_RECOVERY_REQUIRED",
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "retry_same_tool": False,
                "required_next_tool": {
                    "name": "set_provider_residency",
                    "arguments": {
                        "provider_id": "robot_arm.primary.integrated",
                        "action": "hot",
                        "required_capability": (
                            _INTEGRATED_ONE_SHOT_CAPABILITY
                        ),
                    },
                },
                "message": (
                    "Integrated lost its Basic lease and requires an explicit "
                    "approved HOT transition even though its process is "
                    "already running. Call required_next_tool unchanged, then "
                    "create a fresh preview. This transition reacquires "
                    "authority but does not itself move the arm."
                ),
                "controller": controller_state,
                "connection_detail": reason,
            }
        return {
            "status": "DEPENDENCY_UNAVAILABLE",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "retry_same_tool": False,
            "required_provider_sequence": [
                {
                    "provider_id": "robot_arm.rebot_dm",
                    "required_residency": "HOT",
                    "required_capability": _BASIC_MOTION_CAPABILITY,
                },
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "required_residency": "HOT",
                    "required_capability": (
                        _INTEGRATED_ONE_SHOT_CAPABILITY
                    ),
                },
            ],
            "required_next_tool": {
                "name": "inspect_midbrain_runtime",
                "arguments": {},
            },
            "message": (
                "The controller dependency is unavailable. Do not call this "
                "preview tool again yet. Inspect the current Midbrain runtime, "
                "activate Basic and then Integrated to HOT with approval, and "
                "only then create a fresh preview."
            ),
            "connection_detail": reason,
        }

    async def _stage_target(
        self,
        target: list[float],
        *,
        target_orientation_rpy_rad: list[float] | None,
        orientation_policy: str,
        duration_s: float,
    ) -> tuple[dict[str, Any], str]:
        target_payload: dict[str, Any] = {"position_m": target}
        if target_orientation_rpy_rad is not None:
            target_payload["rpy_rad"] = list(
                target_orientation_rpy_rad
            )
        preview = await self.client.preview_direct_motion(
            {
                "command": {
                    "command_type": "CARTESIAN_TARGET",
                    "target": target_payload,
                    "settings": {
                        "execution_mode": "TRANSIT_SPEED",
                        "interaction_mode": "ONE_SHOT",
                        "ik_mode": (
                            "POSE_6DOF"
                            if orientation_policy
                            in {
                                _PRESERVE_MEASURED_ORIENTATION,
                                _APPLY_CONTROLLED_FRAME_YAW_DELTA,
                            }
                            else "POSITION_3DOF"
                        ),
                        "duration_s": duration_s,
                    },
                },
                "related_skill_id": (
                    "test_agent.relative_effector_motion.v1"
                ),
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            }
        )
        plan = preview.get("preview")
        plan = plan if isinstance(plan, dict) else {}
        preview_id = str(
            preview.get("plan_id") or plan.get("preview_id") or ""
        ).strip()
        if (
            preview.get("status") != "PLANNED"
            or plan.get("planning_valid") is not True
            or not preview_id
        ):
            raise IntegratedPreviewRejected(
                "Integrated Controller rejected the requested IK preview: "
                + _preview_rejection_reason(preview, plan),
                response=preview,
                plan=plan,
            )
        return preview, preview_id

    async def execute(
        self,
        *,
        preview_id: str,
        motion_intent: str,
        direction: str,
        reference_frame: str,
        resolved_direction_arm_base: list[float],
        distance_m: float,
        original_request_distance_m: float,
        requested_speed_m_s: float | None,
        requested_duration_s: float,
        planned_duration_s: float,
        planned_nominal_speed_m_s: float,
        timing_safety_limited: bool,
        requested_peak_joint_speed_rad_s: float,
        effective_peak_joint_speed_rad_s: float,
        joint_speed_authentication_required: bool,
        target_position_m: list[float],
        orientation_policy: str = _POSITION_ONLY,
        controlled_frame_yaw_delta_deg: float | None = None,
        target_orientation_rpy_rad: list[float] | None = None,
    ) -> dict[str, Any]:
        normalized_preview_id = str(preview_id or "").strip()
        normalized_direction = str(direction or "").strip().upper()
        normalized_reference_frame = str(
            reference_frame or ""
        ).strip().upper()
        normalized_orientation_policy = str(
            orientation_policy or _POSITION_ONLY
        ).strip().upper()
        if normalized_orientation_policy not in {
            _POSITION_ONLY,
            _PRESERVE_MEASURED_ORIENTATION,
            _APPLY_CONTROLLED_FRAME_YAW_DELTA,
        }:
            raise ValueError("unsupported orientation_policy")
        normalized_yaw_delta_deg = (
            None
            if controlled_frame_yaw_delta_deg is None
            else float(controlled_frame_yaw_delta_deg)
        )
        normalized_requested_speed = (
            None
            if requested_speed_m_s is None
            else float(requested_speed_m_s)
        )
        normalized_requested_duration = float(requested_duration_s)
        normalized_planned_duration = float(planned_duration_s)
        normalized_planned_speed = float(planned_nominal_speed_m_s)
        if not isinstance(timing_safety_limited, bool):
            raise ValueError("timing_safety_limited must be boolean")
        normalized_requested_peak_joint_speed = float(
            requested_peak_joint_speed_rad_s
        )
        normalized_effective_peak_joint_speed = float(
            effective_peak_joint_speed_rad_s
        )
        if not isinstance(joint_speed_authentication_required, bool):
            raise ValueError(
                "joint_speed_authentication_required must be boolean"
            )
        if (
            normalized_requested_speed is not None
            and not math.isfinite(normalized_requested_speed)
        ) or not all(
            math.isfinite(value) and value > 0.0
            for value in (
                normalized_requested_duration,
                normalized_planned_duration,
            )
        ) or (
            not math.isfinite(normalized_planned_speed)
            or normalized_planned_speed < 0.0
            or not math.isfinite(normalized_requested_peak_joint_speed)
            or normalized_requested_peak_joint_speed < 0.0
            or normalized_requested_peak_joint_speed
            >= JOINT_SPEED_HARD_LIMIT_RAD_S
            or not math.isfinite(normalized_effective_peak_joint_speed)
            or normalized_effective_peak_joint_speed < 0.0
        ):
            raise ValueError("motion timing arguments must be finite and positive")
        target_orientation = (
            None
            if target_orientation_rpy_rad is None
            else [float(value) for value in target_orientation_rpy_rad]
        )
        if (
            normalized_orientation_policy
            in {
                _PRESERVE_MEASURED_ORIENTATION,
                _APPLY_CONTROLLED_FRAME_YAW_DELTA,
            }
            and not _is_finite_vector(target_orientation)
        ):
            raise ValueError(
                "target_orientation_rpy_rad must contain three finite "
                "values for a POSE_6DOF relative motion"
            )
        if (
            normalized_orientation_policy == _POSITION_ONLY
            and target_orientation is not None
        ):
            raise ValueError(
                "target_orientation_rpy_rad must be null for POSITION_ONLY"
            )
        if normalized_orientation_policy == _APPLY_CONTROLLED_FRAME_YAW_DELTA:
            if (
                normalized_yaw_delta_deg is None
                or not math.isfinite(normalized_yaw_delta_deg)
                or abs(normalized_yaw_delta_deg) < 1e-9
                or abs(normalized_yaw_delta_deg)
                > MAX_CONTROLLED_FRAME_YAW_DELTA_DEG
            ):
                raise ValueError(
                    "controlled_frame_yaw_delta_deg must be a finite, "
                    "nonzero value from -45 to 45"
                )
        elif normalized_yaw_delta_deg is not None:
            raise ValueError(
                "controlled_frame_yaw_delta_deg must be null unless the "
                "orientation policy applies a controlled-frame yaw delta"
            )
        resolved_vector = [
            float(value) for value in resolved_direction_arm_base
        ]
        if (
            len(resolved_vector) != 3
            or not all(math.isfinite(value) for value in resolved_vector)
        ):
            raise ValueError(
                "resolved_direction_arm_base must contain three finite values"
            )
        target = [float(value) for value in target_position_m]
        if len(target) != 3 or not all(math.isfinite(value) for value in target):
            raise ValueError(
                "target_position_m must contain three finite values"
            )
        normalized_distance = float(distance_m)
        normalized_original_distance = float(original_request_distance_m)
        if (
            (normalized_distance == 0.0)
            != (normalized_direction == "NONE")
            or (normalized_original_distance == 0.0)
            != (normalized_direction == "NONE")
            or (
                normalized_direction == "NONE"
                and normalized_orientation_policy
                != _APPLY_CONTROLLED_FRAME_YAW_DELTA
            )
            or (
                normalized_distance > 0.0
                and normalized_planned_speed <= 0.0
            )
            or (
                normalized_distance == 0.0
                and normalized_planned_speed != 0.0
            )
        ):
            raise ValueError(
                "translation, direction, timing, and rotation-only "
                "arguments are inconsistent"
            )
        async with self._lock:
            pending = self._pending.get(normalized_preview_id)
        if pending is None:
            raise RuntimeError(
                "the approved IK preview is missing, expired, or already used"
            )
        if time.monotonic() - pending["created_monotonic"] > self.approval_ttl_s:
            async with self._lock:
                if self._pending.get(normalized_preview_id) is pending:
                    self._pending.pop(normalized_preview_id, None)
            raise RuntimeError("the approved IK preview has expired")
        if (
            str(motion_intent or "").strip().upper()
            != pending["motion_intent"]
            or not math.isclose(
                normalized_original_distance,
                pending["original_request_distance_m"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or normalized_direction != pending["direction"]
            or normalized_reference_frame != pending["reference_frame"]
            or normalized_orientation_policy
            != pending["orientation_policy"]
            or not _optional_float_equal(
                normalized_yaw_delta_deg,
                pending["controlled_frame_yaw_delta_deg"],
                tolerance=1e-9,
            )
            or not _optional_float_equal(
                normalized_requested_speed,
                pending["requested_speed_m_s"],
                tolerance=1e-9,
            )
            or not math.isclose(
                normalized_requested_duration,
                pending["requested_duration_s"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or not math.isclose(
                normalized_planned_duration,
                pending["planned_duration_s"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or not math.isclose(
                normalized_planned_speed,
                pending["planned_nominal_speed_m_s"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or timing_safety_limited
            is not pending["timing_safety_limited"]
            or not math.isclose(
                normalized_requested_peak_joint_speed,
                pending["requested_peak_joint_speed_rad_s"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or not math.isclose(
                normalized_effective_peak_joint_speed,
                pending["effective_peak_joint_speed_rad_s"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or joint_speed_authentication_required
            is not pending["joint_speed_authentication_required"]
            or not _optional_vector_equal(
                target_orientation,
                pending["target_orientation_rpy_rad"],
                tolerance=1e-9,
            )
            or any(
                not math.isclose(
                    resolved_vector[index],
                    pending["resolved_direction_arm_base"][index],
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                for index in range(3)
            )
            or not math.isclose(
                normalized_distance,
                pending["distance_m"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or any(
                not math.isclose(
                    target[index],
                    pending["target_position_m"][index],
                    rel_tol=0.0,
                    abs_tol=1e-8,
                )
                for index in range(3)
            )
        ):
            raise RuntimeError(
                "approved IK arguments do not match the stored preview"
            )

        stored_resolution = pending["spatial_resolution"]
        stored_provenance = stored_resolution.get("provenance") or {}
        resolution_source = str(
            stored_provenance.get("resolution_source") or ""
        )
        if normalized_direction == "NONE":
            current_vector = (0.0, 0.0, 0.0)
        else:
            try:
                current_resolution = await self.spatial_resolver.resolve(
                    direction=normalized_direction,
                    reference_frame=normalized_reference_frame,
                    arm_mount_assumption=(
                        "CONFIRMED_X_FORWARD_Z_UP"
                        if resolution_source
                        == "OPERATOR_ATTESTED_IDENTITY_ROTATION"
                        else "UNKNOWN"
                    ),
                    camera_level_assumption=(
                        "CONFIRMED_GRAVITY_LEVELED"
                        if normalized_reference_frame == "CAMERA_LEVEL"
                        else "UNKNOWN"
                    ),
                )
            except SpatialResolutionRequired as required:
                raise RuntimeError(
                    "spatial evidence became invalid before approval: "
                    + str(required.payload.get("message") or required)
                ) from required
            current_vector = current_resolution.vector_arm_base
            stored_epoch = str(
                stored_provenance.get("session_epoch") or ""
            )
            current_epoch = str(
                current_resolution.provenance.get("session_epoch") or ""
            )
            if (
                stored_epoch != current_epoch
                or _path_identity(stored_provenance.get("transform_path"))
                != _path_identity(
                    current_resolution.provenance.get("transform_path")
                )
                or any(
                    not math.isclose(
                        current_vector[index],
                        pending["resolved_direction_arm_base"][index],
                        rel_tol=0.0,
                        abs_tol=1e-7,
                    )
                    for index in range(3)
                )
            ):
                raise RuntimeError(
                    "spatial resolution changed before approval; request a "
                    "fresh preview"
                )

        state = await self.client.state()
        stored_attestation = stored_provenance.get(
            "operator_attestation"
        )
        if (
            isinstance(stored_attestation, dict)
            and stored_attestation.get("confirmed")
        ):
            if (
                state.get("controller_identity")
                != stored_attestation.get("controller_identity")
            ):
                raise RuntimeError(
                    "controller identity changed after the mount attestation; "
                    "request a fresh preview and confirmation"
                )
        planning = state.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        current_preview = planning.get("last_preview")
        current_preview = (
            current_preview if isinstance(current_preview, dict) else {}
        )
        model_view = state.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        measured_position = measured.get("position_m")
        if (
            not _is_finite_vector(measured_position)
            or any(
                not math.isclose(
                    float(measured_position[index]),
                    pending["start_position_m"][index],
                    rel_tol=0.0,
                    abs_tol=0.003,
                )
                for index in range(3)
            )
        ):
            raise RuntimeError(
                "measured arm pose changed after the preview and before "
                "approval; request a fresh preview and before image"
            )
        if (
            normalized_orientation_policy
            in {
                _PRESERVE_MEASURED_ORIENTATION,
                _APPLY_CONTROLLED_FRAME_YAW_DELTA,
            }
        ):
            measured_orientation = measured.get("rpy_rad")
            if (
                not _is_finite_vector(measured_orientation)
                or _rpy_angular_distance(
                    measured_orientation,
                    pending["start_orientation_rpy_rad"],
                )
                > 0.02
            ):
                raise RuntimeError(
                    "measured controlled-frame orientation changed after "
                    "the preview; request a fresh POSE_6DOF "
                    "preview"
                )
        mismatch_reasons = _controller_preview_mismatch_reasons(
            planning=planning,
            model_view=model_view,
            controller_preview_id=pending["controller_preview_id"],
            planned_duration_s=pending["planned_duration_s"],
            target_position_m=target,
            orientation_policy=normalized_orientation_policy,
            target_orientation_rpy_rad=target_orientation,
        )
        controller_preview_refreshed = False
        if mismatch_reasons:
            refreshed, refreshed_preview_id = await self._stage_target(
                target,
                target_orientation_rpy_rad=target_orientation,
                orientation_policy=normalized_orientation_policy,
                duration_s=pending["planned_duration_s"],
            )
            refreshed_plan = refreshed.get("preview")
            refreshed_plan = (
                refreshed_plan
                if isinstance(refreshed_plan, dict)
                else {}
            )
            try:
                refreshed_duration_s = float(
                    refreshed_plan.get("duration_s")
                )
            except (TypeError, ValueError):
                refreshed_duration_s = math.nan
            if (
                not math.isfinite(refreshed_duration_s)
                or refreshed_duration_s + 1e-9
                < pending["planned_duration_s"]
                or refreshed_duration_s > MAX_RELATIVE_DURATION_S
            ):
                raise RuntimeError(
                    "Integrated Controller refreshed the approved target "
                    "with an invalid or faster trajectory duration"
                )
            pending["controller_preview_id"] = refreshed_preview_id
            pending["active_planned_duration_s"] = refreshed_duration_s
            state = await self.client.state()
            planning = state.get("planning")
            planning = planning if isinstance(planning, dict) else {}
            model_view = state.get("model_view")
            model_view = model_view if isinstance(model_view, dict) else {}
            mismatch_reasons = _controller_preview_mismatch_reasons(
                planning=planning,
                model_view=model_view,
                controller_preview_id=refreshed_preview_id,
                planned_duration_s=refreshed_duration_s,
                target_position_m=target,
                orientation_policy=normalized_orientation_policy,
                target_orientation_rpy_rad=target_orientation,
            )
            controller_preview_refreshed = True
        if mismatch_reasons:
            raise RuntimeError(
                "Integrated Controller could not hold the approved target "
                "long enough to commit it after one bounded refresh "
                f"({', '.join(mismatch_reasons)})"
            )
        starting_commit_count = int(state.get("commit_count") or 0)
        starting_completed = (state.get("trajectory") or {}).get(
            "last_completed"
        )
        engagement = await self.client.engage_staged_motion()
        if engagement.get("status") != "engaged_target_edit":
            raise RuntimeError(
                "Integrated Controller did not enter target-edit engagement"
            )
        trigger = await self.client.trigger_one_shot_motion()
        if trigger.get("physical_motion_authorized") is not True:
            raise RuntimeError(
                "Integrated Controller did not accept the approved one-shot "
                "commit trigger"
            )
        async with self._lock:
            if self._pending.get(normalized_preview_id) is pending:
                self._pending.pop(normalized_preview_id, None)

        deadline = time.monotonic() + 15.0
        saw_active_trajectory = False
        terminal_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            current = await self.client.state()
            trajectory = current.get("trajectory")
            trajectory = trajectory if isinstance(trajectory, dict) else {}
            saw_active_trajectory = bool(
                saw_active_trajectory or trajectory.get("active")
            )
            commit_count = int(current.get("commit_count") or 0)
            completed = trajectory.get("last_completed")
            if (
                commit_count > starting_commit_count
                and not trajectory.get("active")
                and isinstance(completed, dict)
                and completed != starting_completed
            ):
                terminal_state = current
                break
            if current.get("health") in {"FAULTED", "UNHEALTHY"}:
                raise RuntimeError(
                    "Integrated Controller faulted during approved motion: "
                    f"{current.get('fault_reason') or current.get('last_error')}"
                )
            await asyncio.sleep(0.1)
        if terminal_state is None:
            phase = (
                "trajectory remained active"
                if saw_active_trajectory
                else "controller did not start a trajectory"
            )
            raise RuntimeError(
                "approved Integrated motion did not reach a terminal state "
                f"within 15 seconds: {phase}"
            )
        completion = terminal_state["trajectory"]["last_completed"]
        completion_success = completion.get("completion_success") is True
        completion_outcome = str(
            completion.get("completion_outcome") or "UNKNOWN"
        )
        controller_duration_s = completion.get("duration_s")
        controller_duration_s = (
            float(controller_duration_s)
            if isinstance(controller_duration_s, (int, float))
            and math.isfinite(float(controller_duration_s))
            else None
        )
        visual_verification: dict[str, Any] | None = None
        if isinstance(pending.get("visual_verification"), dict):
            terminal_model_view = terminal_state.get("model_view")
            terminal_model_view = (
                terminal_model_view
                if isinstance(terminal_model_view, dict)
                else {}
            )
            terminal_measured = terminal_model_view.get(
                "measured_controlled_frame"
            )
            terminal_measured = (
                terminal_measured
                if isinstance(terminal_measured, dict)
                else {}
            )
            controller_after_position = terminal_measured.get(
                "position_m"
            )
            if not _is_finite_vector(controller_after_position):
                controller_after_position = completion.get(
                    "controlled_goal_position_m"
                )
            if not _is_finite_vector(controller_after_position):
                controller_after_position = pending["target_position_m"]
            visual_verification = (
                await self._complete_visual_verification(
                    pending["visual_verification"],
                    commanded_distance_m=pending["distance_m"],
                    controller_before_arm_base_m=pending[
                        "start_position_m"
                    ],
                    controller_after_arm_base_m=controller_after_position,
                )
            )
        elif isinstance(pending.get("visual_baseline_status"), dict):
            visual_verification = dict(
                pending["visual_baseline_status"]
            )
        visually_confirmed = (
            visual_verification is not None
            and visual_verification.get("status")
            == "VISUALLY_CONFIRMED"
        )
        has_commanded_yaw = normalized_yaw_delta_deg is not None
        visual_motion_confirmed = visually_confirmed and not has_commanded_yaw
        if completion_success and visually_confirmed and has_commanded_yaw:
            result_status = (
                "MOTION_COMPLETED_TRANSLATION_VISUALLY_CONFIRMED_"
                "ORIENTATION_UNVERIFIED"
            )
        elif completion_success and visually_confirmed:
            result_status = "MOTION_COMPLETED_VISUALLY_CONFIRMED"
        elif (
            completion_success
            and visual_verification is not None
            and visual_verification.get("status")
            == "BEFORE_EVIDENCE_UNAVAILABLE"
        ):
            result_status = "MOTION_COMPLETED_VISUAL_CHECK_UNAVAILABLE"
        elif (
            completion_success
            and visual_verification is not None
            and visual_verification.get("status") in {
                "SKIPPED_FIXED_RIG_NOT_CONFIRMED",
                "SKIPPED_ROTATION_ONLY_NO_ORIENTATION_EVIDENCE",
            }
        ):
            result_status = "MOTION_COMPLETED"
        elif completion_success and visual_verification is not None:
            result_status = "MOTION_COMPLETED_VISUAL_CHECK_INCONCLUSIVE"
        elif completion_success:
            result_status = "MOTION_COMPLETED"
        else:
            result_status = "MOTION_FINISHED_WITHOUT_CONFIRMED_ARRIVAL"
        result = {
            "status": result_status,
            "physical_motion_requested": True,
            "physical_motion_completed": completion_success,
            "visual_motion_confirmed": visual_motion_confirmed,
            "visual_translation_confirmed": visually_confirmed,
            "visual_orientation_confirmed": (
                False if has_commanded_yaw else None
            ),
            "motion_intent": pending["motion_intent"],
            "preview_id": normalized_preview_id,
            "controller_preview_id": pending["controller_preview_id"],
            "controller_preview_refreshed": controller_preview_refreshed,
            "direction": normalized_direction,
            "reference_frame": normalized_reference_frame,
            "resolved_direction_arm_base": resolved_vector,
            "spatial_resolution": pending["spatial_resolution"],
            "distance_m": pending["distance_m"],
            "original_request_distance_m": pending[
                "original_request_distance_m"
            ],
            "requested_speed_m_s": pending["requested_speed_m_s"],
            "requested_duration_s": pending["requested_duration_s"],
            "planned_duration_s": pending["planned_duration_s"],
            "planned_nominal_speed_m_s": pending[
                "planned_nominal_speed_m_s"
            ],
            "timing_safety_limited": pending["timing_safety_limited"],
            "requested_peak_joint_speed_rad_s": pending[
                "requested_peak_joint_speed_rad_s"
            ],
            "effective_peak_joint_speed_rad_s": pending[
                "effective_peak_joint_speed_rad_s"
            ],
            "joint_speed_authentication_required": pending[
                "joint_speed_authentication_required"
            ],
            "controller_duration_s": controller_duration_s,
            "timing": {
                "semantics": (
                    "DEFAULT_DURATION_WITH_JOINT_RATE_SAFETY"
                    if pending["distance_m"] == 0.0
                    else "NOMINAL_AVERAGE_ENDPOINT_SPEED"
                ),
                "constant_cartesian_speed": False,
                "requested_speed_m_s": pending["requested_speed_m_s"],
                "requested_duration_s": pending["requested_duration_s"],
                "planned_duration_s": pending["planned_duration_s"],
                "planned_nominal_speed_m_s": pending[
                    "planned_nominal_speed_m_s"
                ],
                "provider_safety_lengthened": pending[
                    "timing_safety_limited"
                ],
                "controller_duration_s": controller_duration_s,
            },
            "target_position_m": target,
            "orientation_policy": normalized_orientation_policy,
            "orientation_reference_frame": (
                "CONTROLLED_FRAME"
                if normalized_yaw_delta_deg is not None
                else None
            ),
            "controlled_frame_yaw_delta_deg": (
                normalized_yaw_delta_deg
            ),
            "target_orientation_rpy_rad": target_orientation,
            "engagement": engagement,
            "one_shot_trigger": trigger,
            "completion": completion,
            "visual_verification": visual_verification,
            "message": (
                "The controller confirmed the combined pose target, and the "
                "gravity-aligned before/after evidence confirmed its "
                "translation. The visual check did not measure or confirm "
                "the commanded controlled-frame yaw."
                if (
                    completion_success
                    and visually_confirmed
                    and has_commanded_yaw
                )
                else
                "The controller and the gravity-aligned before/after visual "
                "evidence both confirmed the approved motion."
                if completion_success and visually_confirmed
                else "The controller confirmed completion. The optional "
                "visual check was unavailable and did not define or veto "
                "the arm motion."
                if (
                    completion_success
                    and visual_verification is not None
                    and visual_verification.get("status")
                    == "BEFORE_EVIDENCE_UNAVAILABLE"
                )
                else "The controller confirmed completion. Visual checking "
                "was skipped because the fixed camera/IMU rig was not "
                "confirmed."
                if (
                    completion_success
                    and visual_verification is not None
                    and visual_verification.get("status")
                    == "SKIPPED_FIXED_RIG_NOT_CONFIRMED"
                )
                else "The controller confirmed completion. The positional "
                "before/after visual check was not applied to this "
                "rotation-only motion because it does not measure "
                "controlled-frame orientation."
                if (
                    completion_success
                    and visual_verification is not None
                    and visual_verification.get("status")
                    == (
                        "SKIPPED_ROTATION_ONLY_NO_ORIENTATION_EVIDENCE"
                    )
                )
                else "The controller confirmed completion, but the "
                "before/after visual check was inconclusive."
                if completion_success and visual_verification is not None
                else "The Integrated Controller confirmed completion of the "
                "approved motion."
                if completion_success
                else "The controller finished the attempt but did not "
                f"confirm target arrival ({completion_outcome})."
            ),
        }
        return result

    async def _complete_visual_verification(
        self,
        context: dict[str, Any],
        *,
        commanded_distance_m: float,
        controller_before_arm_base_m: list[float],
        controller_after_arm_base_m: list[float],
    ) -> dict[str, Any]:
        before = context.get("before")
        result: dict[str, Any] = {
            "schema": "physical_agent.before_after_motion_verification",
            "schema_version": 1,
            "status": "INCONCLUSIVE",
            "world_frame": context.get("world_frame"),
            "session_epoch": context.get("session_epoch"),
            "fixed_rig_attestation": context.get(
                "fixed_rig_attestation"
            ),
            "expected_direction_world": context.get(
                "expected_direction_world"
            ),
            "commanded_distance_m": commanded_distance_m,
            "before": before,
            "after": None,
        }
        try:
            current_vio = await self.spatial_resolver.current_vio()
        except Exception as error:
            result["reason"] = (
                "VIO was not motion-usable for the after picture: "
                f"{error}"
            )
            return result
        if (
            current_vio.get("world_frame") != context.get("world_frame")
            or current_vio.get("session_epoch")
            != context.get("session_epoch")
        ):
            result["reason"] = (
                "The VIO world frame or epoch changed between the before and "
                "after observations."
            )
            result["after_vio"] = current_vio
            return result
        if self.visual_evidence_capture is None:
            result["reason"] = "The after-picture capture is unavailable."
            return result
        try:
            after = await self.visual_evidence_capture(
                str(context["world_frame"])
            )
            before_point = _visual_point(before)
            after_point = _visual_point(after)
        except Exception as error:
            result["reason"] = (
                "The after picture or effector landmark was unavailable: "
                f"{error}"
            )
            return result
        expected = tuple(
            float(value)
            for value in context["expected_direction_world"]
        )
        displacement = tuple(
            after_point[index] - before_point[index]
            for index in range(3)
        )
        magnitude = math.sqrt(
            sum(value * value for value in displacement)
        )
        projection = sum(
            displacement[index] * expected[index]
            for index in range(3)
        )
        lateral_vector = tuple(
            displacement[index] - projection * expected[index]
            for index in range(3)
        )
        lateral = math.sqrt(
            sum(value * value for value in lateral_vector)
        )
        cosine = projection / magnitude if magnitude > 1e-9 else 0.0
        minimum_projection = max(0.005, commanded_distance_m * 0.35)
        maximum_lateral = max(0.03, commanded_distance_m * 0.5)
        distance_tolerance = max(0.03, commanded_distance_m * 0.5)
        confirmed = (
            projection >= minimum_projection
            and cosine >= 0.6
            and lateral <= maximum_lateral
            and abs(projection - commanded_distance_m)
            <= distance_tolerance
        )
        result.update(
            {
                "status": (
                    "VISUALLY_CONFIRMED"
                    if confirmed
                    else "INCONCLUSIVE"
                ),
                "reason": (
                    "The before/after landmark displacement agrees with the "
                    "commanded gravity-aligned direction."
                    if confirmed
                    else "The before/after landmark displacement did not "
                    "meet the directional verification thresholds."
                ),
                "after": after,
                "before_point_world_m": list(before_point),
                "after_point_world_m": list(after_point),
                "observed_displacement_world_m": list(displacement),
                "observed_distance_m": magnitude,
                "directional_projection_m": projection,
                "lateral_error_m": lateral,
                "direction_cosine": cosine,
                "thresholds": {
                    "minimum_projection_m": minimum_projection,
                    "maximum_lateral_error_m": maximum_lateral,
                    "distance_tolerance_m": distance_tolerance,
                    "minimum_direction_cosine": 0.6,
                },
            }
        )
        result["arm_root_realignment"] = await self._accumulate_root_alignment(
            world_frame=str(context.get("world_frame") or ""),
            session_epoch=str(context.get("session_epoch") or ""),
            controller_before_arm_base_m=controller_before_arm_base_m,
            controller_after_arm_base_m=controller_after_arm_base_m,
            observed_before_world_m=before_point,
            observed_after_world_m=after_point,
        )
        return result

    async def _accumulate_root_alignment(
        self,
        *,
        world_frame: str,
        session_epoch: str,
        controller_before_arm_base_m: list[float],
        controller_after_arm_base_m: list[float],
        observed_before_world_m: tuple[float, float, float],
        observed_after_world_m: tuple[float, float, float],
    ) -> dict[str, Any]:
        """Accumulate effector correspondences and propose a rigid root update."""

        identity = (world_frame, session_epoch)
        if self._root_alignment_identity != identity:
            self._root_alignment_identity = identity
            self._root_alignment_correspondences = []
        for arm_point, world_point, phase in (
            (
                controller_before_arm_base_m,
                observed_before_world_m,
                "BEFORE",
            ),
            (
                controller_after_arm_base_m,
                observed_after_world_m,
                "AFTER",
            ),
        ):
            if not _is_finite_vector(arm_point):
                return {
                    "status": "CONTROLLER_CORRESPONDENCE_UNAVAILABLE",
                    "motion_usable": False,
                }
            self._root_alignment_correspondences.append(
                {
                    "arm_base_point_m": [float(value) for value in arm_point],
                    "world_point_m": [float(value) for value in world_point],
                    "phase": phase,
                    "observed_at_us": time.time_ns() // 1000,
                }
            )
        self._root_alignment_correspondences = (
            self._root_alignment_correspondences[-48:]
        )
        arm_points = np.asarray(
            [
                item["arm_base_point_m"]
                for item in self._root_alignment_correspondences
            ],
            dtype=float,
        )
        world_points = np.asarray(
            [
                item["world_point_m"]
                for item in self._root_alignment_correspondences
            ],
            dtype=float,
        )
        fit = _fit_rigid_correspondences(arm_points, world_points)
        if fit is None:
            next_direction = _preferred_noncollinear_arm_direction(
                arm_points
            )
            return {
                "status": "MORE_NONCOLLINEAR_MOTION_REQUIRED",
                "motion_usable": False,
                "correspondence_count": len(arm_points),
                "minimum_unique_positions": 3,
                "requirement": (
                    "Accumulate at least three non-collinear controlled-frame "
                    "positions; one straight move cannot observe rotation "
                    "about its own displacement axis."
                ),
                "recommended_next_move": {
                    "reference_frame": "ARM_BASE",
                    "direction": next_direction,
                    "preferred_baseline_m": 0.25,
                    "orientation_policy": (
                        "PRESERVE_MEASURED_CONTROLLED_FRAME"
                    ),
                    "execution_policy": (
                        "CREATE_A_FRESH_CONTROLLER_PREVIEW_AND_USE_ITS_"
                        "SCENE_AND_IK_CHECKS_BEFORE_EACH_MOVE"
                    ),
                },
            }

        fitted = fit["world_from_arm_base"]
        current_payload: dict[str, Any] | None = None
        try:
            current_payload = await self.spatial_resolver.fabric.transform(
                from_frame=self.spatial_resolver.arm_base_frame,
                to_frame=world_frame,
                session_epoch=session_epoch or None,
                max_extrapolation_us=(
                    self.spatial_resolver.maximum_transform_extrapolation_us
                ),
            )
        except Exception:
            current_payload = None
        current = _transform_payload_matrix(current_payload)
        correction: dict[str, Any] | None = None
        if current is not None:
            delta = fitted @ np.linalg.inv(current)
            correction = {
                "world_frame_delta_matrix_row_major": delta.reshape(-1).tolist(),
                "translation_delta_m": delta[:3, 3].tolist(),
                "translation_delta_norm_m": float(
                    np.linalg.norm(delta[:3, 3])
                ),
                "rotation_delta_rad": _rotation_angle(delta[:3, :3]),
            }
        return {
            "status": "ROOT_REFINEMENT_CANDIDATE_READY",
            "motion_usable": False,
            "activation_required": True,
            "world_frame": world_frame,
            "arm_base_frame": self.spatial_resolver.arm_base_frame,
            "session_epoch": session_epoch,
            "correspondence_count": len(arm_points),
            "world_from_arm_base": {
                "translation_m": fitted[:3, 3].tolist(),
                "rotation_xyzw": _matrix_to_quaternion_xyzw(
                    fitted[:3, :3]
                ),
                "matrix_row_major": fitted.reshape(-1).tolist(),
            },
            "fit": {
                "rms_residual_m": fit["rms_residual_m"],
                "maximum_residual_m": fit["maximum_residual_m"],
                "baseline_singular_values_m": fit[
                    "baseline_singular_values_m"
                ],
            },
            "correction_from_current": correction,
            "policy": (
                "CANDIDATE_ONLY_UNTIL_REVIEWED_ACTIVATION; accumulate more "
                "non-collinear moves to average RGB-D landmark noise"
            ),
        }


def _preview_rejection_reason(
    response: dict[str, Any],
    preview: dict[str, Any],
) -> str:
    candidates = [
        response.get("message"),
        response.get("error"),
        response.get("reason"),
        preview.get("message"),
        preview.get("error"),
        preview.get("reason"),
        preview.get("planning_reasons"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, list) and candidate:
            return "; ".join(str(value) for value in candidate)
    return "controller returned no diagnostic reason"


def _one_shot_policy_limit_diagnostics(
    preview: dict[str, Any],
) -> dict[str, Any] | None:
    """Recognize a safe IK result rejected only by one-shot travel guards."""
    if (
        preview.get("planning_valid") is not False
        or preview.get("collision_free") is not True
        or preview.get("target_clamped") is not False
        or preview.get("physical_motion_authorized") is True
    ):
        return None

    position_residual = _finite_nonnegative_number(
        preview.get("position_residual_m")
    )
    orientation_residual = _finite_nonnegative_number(
        preview.get("orientation_residual_rad")
    )
    if position_residual is None or orientation_residual is None:
        return None
    if (
        position_residual
        > _POLICY_CLASSIFICATION_POSITION_RESIDUAL_CEILING_M
        or (
            str(preview.get("ik_mode") or "").strip().upper()
            != "POSITION_3DOF"
            and orientation_residual
            > _POLICY_CLASSIFICATION_ORIENTATION_RESIDUAL_CEILING_RAD
        )
    ):
        return None

    planning_reasons = _nonempty_string_list(
        preview.get("planning_reasons")
    )
    if not planning_reasons:
        return None
    parsed_reasons = [
        _one_shot_travel_reason(reason) for reason in planning_reasons
    ]
    if any(reason is None for reason in parsed_reasons):
        return None

    physical_blockers = preview.get("physical_execution_blockers")
    if physical_blockers is not None:
        blockers = _string_list(physical_blockers)
        if blockers is None or any(
            _one_shot_travel_reason(reason) is None
            for reason in blockers
        ) or not set(blockers).issubset(set(planning_reasons)):
            return None

    endpoint_deltas = _finite_nonnegative_number_list(
        preview.get("endpoint_joint_delta_rad")
    )
    endpoint_limits = _finite_nonnegative_number_list(
        preview.get("endpoint_joint_delta_limit_rad")
    )
    endpoint_reason_joints = {
        joint
        for parsed in parsed_reasons
        if parsed is not None and parsed[0] == "ENDPOINT_JOINT_TRAVEL"
        for joint in parsed[1]
    }
    endpoint_violations: list[dict[str, Any]] = []
    if endpoint_deltas is not None or endpoint_limits is not None:
        if endpoint_deltas is None or endpoint_limits is None or (
            len(endpoint_deltas) != len(endpoint_limits)
        ):
            return None
        endpoint_violations = [
            {
                "joint": index + 1,
                "delta_rad": delta,
                "limit_rad": limit,
                "excess_rad": delta - limit,
            }
            for index, (delta, limit) in enumerate(
                zip(endpoint_deltas, endpoint_limits, strict=True)
            )
            if delta > limit
        ]
        if {
            violation["joint"] for violation in endpoint_violations
        } != endpoint_reason_joints:
            return None
    elif endpoint_reason_joints:
        return None

    has_aggregate_reason = any(
        parsed is not None and parsed[0] == "AGGREGATE_JOINT_TRAVEL"
        for parsed in parsed_reasons
    )
    aggregate_diagnostics: dict[str, Any] | None = None
    if has_aggregate_reason:
        continuity = preview.get("cartesian_continuity")
        if not isinstance(continuity, dict):
            return None
        total_travel = _finite_nonnegative_number(
            continuity.get("total_joint_travel_rad")
        )
        if total_travel is None:
            return None
        aggregate_limit = _first_finite_nonnegative_number(
            preview.get("maximum_total_joint_travel_rad"),
            preview.get("total_joint_travel_limit_rad"),
            continuity.get("maximum_total_joint_travel_rad"),
            continuity.get("total_joint_travel_limit_rad"),
        )
        if aggregate_limit is not None and total_travel <= aggregate_limit:
            return None
        aggregate_diagnostics = {
            "total_rad": total_travel,
            "limit_rad": aggregate_limit,
            "excess_rad": (
                total_travel - aggregate_limit
                if aggregate_limit is not None
                else None
            ),
        }

    guard_codes = []
    if endpoint_reason_joints:
        guard_codes.append("ENDPOINT_JOINT_TRAVEL")
    if has_aggregate_reason:
        guard_codes.append("AGGREGATE_JOINT_TRAVEL")
    return {
        "guard_codes": guard_codes,
        "planning_reasons": planning_reasons,
        "physical_execution_blockers": (
            list(physical_blockers)
            if isinstance(physical_blockers, list)
            else None
        ),
        "collision_free": True,
        "target_clamped": False,
        "controller_residual_checks_passed": True,
        "position_residual_m": position_residual,
        "orientation_residual_rad": orientation_residual,
        "endpoint_joint_violations": endpoint_violations,
        "aggregate_joint_travel": aggregate_diagnostics,
        "automatic_segmentation_performed": False,
    }


def _one_shot_travel_reason(
    reason: str,
) -> tuple[str, tuple[int, ...]] | None:
    if reason == _AGGREGATE_JOINT_TRAVEL_REASON:
        return ("AGGREGATE_JOINT_TRAVEL", ())
    if not reason.startswith(_ENDPOINT_JOINT_TRAVEL_REASON_PREFIX):
        return None
    suffix = reason[len(_ENDPOINT_JOINT_TRAVEL_REASON_PREFIX) :]
    parts = [part.strip() for part in suffix.split(",")]
    if not parts or any(not part.isdigit() for part in parts):
        return None
    joints = tuple(int(part) for part in parts)
    if any(joint <= 0 for joint in joints) or len(set(joints)) != len(joints):
        return None
    return ("ENDPOINT_JOINT_TRAVEL", joints)


def _nonempty_string_list(value: Any) -> list[str] | None:
    strings = _string_list(value)
    if not strings:
        return None
    return strings


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return None
    return list(value)


def _finite_nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


def _finite_nonnegative_number_list(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    numbers = [_finite_nonnegative_number(item) for item in value]
    if any(number is None for number in numbers):
        return None
    return [float(number) for number in numbers if number is not None]


def _first_finite_nonnegative_number(*values: Any) -> float | None:
    for value in values:
        number = _finite_nonnegative_number(value)
        if number is not None:
            return number
    return None


def _controller_preview_mismatch_reasons(
    *,
    planning: dict[str, Any],
    model_view: dict[str, Any],
    controller_preview_id: str,
    planned_duration_s: float,
    target_position_m: list[float],
    orientation_policy: str,
    target_orientation_rpy_rad: list[float] | None,
) -> list[str]:
    current_preview = planning.get("last_preview")
    current_preview = (
        current_preview if isinstance(current_preview, dict) else {}
    )
    staged = model_view.get("staged_controlled_frame")
    staged = staged if isinstance(staged, dict) else {}
    staged_position = staged.get("position_m")
    reasons: list[str] = []
    if str(current_preview.get("preview_id") or "") != controller_preview_id:
        reasons.append("controller_preview_id")
    if current_preview.get("planning_valid") is not True:
        reasons.append("planning_valid")
    if current_preview.get("target_revision") != planning.get(
        "target_revision"
    ):
        reasons.append("target_revision")
    try:
        current_duration_s = float(current_preview.get("duration_s"))
    except (TypeError, ValueError):
        current_duration_s = math.nan
    if not math.isclose(
        current_duration_s,
        planned_duration_s,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("duration_s")
    if (
        not _is_finite_vector(staged_position)
        or any(
            not math.isclose(
                float(staged_position[index]),
                target_position_m[index],
                rel_tol=0.0,
                abs_tol=1e-8,
            )
            for index in range(3)
        )
    ):
        reasons.append("staged_position_m")
    if orientation_policy in {
        _PRESERVE_MEASURED_ORIENTATION,
        _APPLY_CONTROLLED_FRAME_YAW_DELTA,
    } and (
        not _is_finite_vector(staged.get("rpy_rad"))
        or _rpy_angular_distance(
            staged["rpy_rad"],
            target_orientation_rpy_rad,
        )
        > 1e-7
    ):
        reasons.append("staged_orientation_rpy_rad")
    return reasons


def _path_identity(path: Any) -> tuple[tuple[Any, ...], ...]:
    fields = (
        "from_frame",
        "to_frame",
        "parent_frame",
        "child_frame",
        "direction",
        "authority",
        "provider_id",
        "provider_instance_id",
        "session_epoch",
        "calibration_revision",
    )
    if not isinstance(path, list):
        return ()
    return tuple(
        tuple(step.get(field) for field in fields)
        for step in path
        if isinstance(step, dict)
    )


def _is_vio_readiness_failure(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "") in _VIO_READINESS_FAILURES


def _is_finite_vector(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(
            isinstance(component, (int, float))
            and math.isfinite(float(component))
            for component in value
        )
    )


def _optional_vector_equal(
    left: Any,
    right: Any,
    *,
    tolerance: float,
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return (
        _is_finite_vector(left)
        and _is_finite_vector(right)
        and all(
            math.isclose(
                float(left[index]),
                float(right[index]),
                rel_tol=0.0,
                abs_tol=tolerance,
            )
            for index in range(3)
        )
    )


def _optional_float_equal(
    left: Any,
    right: Any,
    *,
    tolerance: float,
) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(left_value)
        and math.isfinite(right_value)
        and math.isclose(
            left_value,
            right_value,
            rel_tol=0.0,
            abs_tol=tolerance,
        )
    )


def _rpy_matrix(value: Any) -> tuple[tuple[float, ...], ...]:
    if not _is_finite_vector(value):
        raise ValueError("RPY orientation must contain three finite values")
    roll, pitch, yaw = (float(component) for component in value)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (
            cy * cp,
            cy * sp * sr - sy * cr,
            cy * sp * cr + sy * sr,
        ),
        (
            sy * cp,
            sy * sp * sr + cy * cr,
            sy * sp * cr - cy * sr,
        ),
        (-sp, cp * sr, cp * cr),
    )


def _matrix_multiply(
    left: tuple[tuple[float, ...], ...],
    right: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def _matrix_rpy(
    rotation: tuple[tuple[float, ...], ...],
) -> list[float]:
    pitch = math.asin(max(-1.0, min(1.0, -rotation[2][0])))
    cosine_pitch = math.cos(pitch)
    if abs(cosine_pitch) > 1e-8:
        roll = math.atan2(rotation[2][1], rotation[2][2])
        yaw = math.atan2(rotation[1][0], rotation[0][0])
    else:
        roll = 0.0
        yaw = math.atan2(-rotation[0][1], rotation[1][1])
    return [roll, pitch, yaw]


def _apply_controlled_frame_yaw_delta(
    measured_rpy_rad: Any,
    yaw_delta_rad: float,
) -> list[float]:
    """Postmultiply an intrinsic yaw about the measured controlled +Z axis."""

    current = _rpy_matrix(measured_rpy_rad)
    cosine = math.cos(float(yaw_delta_rad))
    sine = math.sin(float(yaw_delta_rad))
    controlled_yaw = (
        (cosine, -sine, 0.0),
        (sine, cosine, 0.0),
        (0.0, 0.0, 1.0),
    )
    return _matrix_rpy(_matrix_multiply(current, controlled_yaw))


def _rpy_angular_distance(left: Any, right: Any) -> float:
    left_matrix = _rpy_matrix(left)
    right_matrix = _rpy_matrix(right)
    relative_trace = sum(
        left_matrix[row][column] * right_matrix[row][column]
        for row in range(3)
        for column in range(3)
    )
    cosine = max(-1.0, min(1.0, (relative_trace - 1.0) / 2.0))
    return math.acos(cosine)


def _vio_identity_from_readiness(
    readiness: dict[str, Any],
) -> dict[str, str] | None:
    outer = readiness.get("result")
    if not isinstance(outer, dict):
        return None
    candidate = outer.get("result")
    if not isinstance(candidate, dict):
        candidate = outer
    world_frame = str(candidate.get("world_frame") or "")
    session_epoch = str(candidate.get("session_epoch") or "")
    if not world_frame or not session_epoch:
        return None
    return {
        "world_frame": world_frame,
        "session_epoch": session_epoch,
    }


def _visual_point(evidence: Any) -> tuple[float, float, float]:
    if not isinstance(evidence, dict):
        raise ValueError("visual effector evidence is not an object")
    reference = evidence.get("control_reference")
    reference = reference if isinstance(reference, dict) else {}
    point = reference.get("target_point_m")
    if not _is_finite_vector(point):
        raise ValueError(
            "visual effector evidence has no finite world target point"
        )
    return tuple(float(value) for value in point)


def _fit_rigid_correspondences(
    source_points: np.ndarray,
    target_points: np.ndarray,
) -> dict[str, Any] | None:
    """Fit target_from_source when the samples span two independent axes."""

    source = np.asarray(source_points, dtype=float)
    target = np.asarray(target_points, dtype=float)
    if (
        source.shape != target.shape
        or source.ndim != 2
        or source.shape[1] != 3
        or source.shape[0] < 3
        or not np.all(np.isfinite(source))
        or not np.all(np.isfinite(target))
    ):
        return None
    source_center = np.mean(source, axis=0)
    target_center = np.mean(target, axis=0)
    source_centered = source - source_center
    target_centered = target - target_center
    baseline_singular_values = np.linalg.svd(
        source_centered,
        compute_uv=False,
    )
    if (
        len(baseline_singular_values) < 2
        or baseline_singular_values[1] < 0.01
    ):
        return None
    covariance = source_centered.T @ target_centered
    left, _, right_transpose = np.linalg.svd(covariance)
    rotation = right_transpose.T @ left.T
    if np.linalg.det(rotation) < 0.0:
        right_transpose[-1, :] *= -1.0
        rotation = right_transpose.T @ left.T
    translation = target_center - rotation @ source_center
    transform = np.eye(4, dtype=float)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation
    predicted = (rotation @ source.T).T + translation
    residuals = np.linalg.norm(predicted - target, axis=1)
    return {
        "world_from_arm_base": transform,
        "rms_residual_m": float(np.sqrt(np.mean(residuals * residuals))),
        "maximum_residual_m": float(np.max(residuals)),
        "baseline_singular_values_m": baseline_singular_values.tolist(),
    }


def _preferred_noncollinear_arm_direction(
    source_points: np.ndarray,
) -> str:
    """Choose a canonical arm axis least aligned with collected motion."""

    source = np.asarray(source_points, dtype=float)
    names = (
        "ARM_BASE_POSITIVE_X",
        "ARM_BASE_POSITIVE_Y",
        "ARM_BASE_POSITIVE_Z",
    )
    if source.ndim != 2 or source.shape[1:] != (3,) or len(source) < 2:
        return "ARM_BASE_POSITIVE_Z"
    centered = source - np.mean(source, axis=0)
    _, singular_values, right_transpose = np.linalg.svd(
        centered,
        full_matrices=False,
    )
    if not len(singular_values) or singular_values[0] < 1e-6:
        return "ARM_BASE_POSITIVE_Z"
    principal = right_transpose[0]
    index = int(np.argmin(np.abs(principal)))
    return names[index]


def _transform_payload_matrix(payload: Any) -> np.ndarray | None:
    if not isinstance(payload, dict):
        return None
    translation = payload.get("translation_m")
    quaternion = payload.get("rotation_xyzw")
    if not _is_finite_vector(translation) or not (
        isinstance(quaternion, (list, tuple))
        and len(quaternion) == 4
        and all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in quaternion
        )
    ):
        return None
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = np.asarray(rotation_matrix(quaternion), dtype=float)
    matrix[:3, 3] = np.asarray(translation, dtype=float)
    return matrix


def _matrix_to_quaternion_xyzw(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=float)
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * scale
        qx = (matrix[2, 1] - matrix[1, 2]) / scale
        qy = (matrix[0, 2] - matrix[2, 0]) / scale
        qz = (matrix[1, 0] - matrix[0, 1]) / scale
    else:
        index = int(np.argmax(np.diag(matrix)))
        if index == 0:
            scale = math.sqrt(
                1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]
            ) * 2.0
            qw = (matrix[2, 1] - matrix[1, 2]) / scale
            qx = 0.25 * scale
            qy = (matrix[0, 1] + matrix[1, 0]) / scale
            qz = (matrix[0, 2] + matrix[2, 0]) / scale
        elif index == 1:
            scale = math.sqrt(
                1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]
            ) * 2.0
            qw = (matrix[0, 2] - matrix[2, 0]) / scale
            qx = (matrix[0, 1] + matrix[1, 0]) / scale
            qy = 0.25 * scale
            qz = (matrix[1, 2] + matrix[2, 1]) / scale
        else:
            scale = math.sqrt(
                1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]
            ) * 2.0
            qw = (matrix[1, 0] - matrix[0, 1]) / scale
            qx = (matrix[0, 2] + matrix[2, 0]) / scale
            qy = (matrix[1, 2] + matrix[2, 1]) / scale
            qz = 0.25 * scale
    quaternion = np.asarray([qx, qy, qz, qw], dtype=float)
    quaternion /= np.linalg.norm(quaternion)
    if quaternion[3] < 0.0:
        quaternion *= -1.0
    return quaternion.tolist()


def _rotation_angle(rotation: np.ndarray) -> float:
    cosine = float(np.clip((np.trace(rotation) - 1.0) * 0.5, -1.0, 1.0))
    return float(math.acos(cosine))
