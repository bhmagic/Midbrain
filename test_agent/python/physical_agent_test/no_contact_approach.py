from __future__ import annotations

import asyncio
import copy
import math
import time
from typing import Any, Protocol

import numpy as np

from .observation_motion import attach_controller_preview


DEFAULT_MAXIMUM_ARM_RADIUS_M = 1.2
VERTICAL_POLICIES = {"NO_DESCENT", "PRESERVE_CURRENT_HEIGHT", "FREE_3D"}
MOUNTED_WORKCELL_POLICY_V1 = "MOUNTED_IDENTITY_TRACKING_GATED_V1"
MOUNTED_WORKCELL_POLICY_V2 = "MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2"
SUPPORTED_MOUNTED_WORKCELL_POLICIES = {
    MOUNTED_WORKCELL_POLICY_V1,
    MOUNTED_WORKCELL_POLICY_V2,
}


class ItemLocatorProtocol(Protocol):
    async def run(self, **arguments: Any) -> dict[str, Any]:
        """Return one metric or degraded item location."""


class EffectorLocatorProtocol(Protocol):
    async def run(self, **arguments: Any) -> dict[str, Any]:
        """Return one metric effector-front reference."""


class SceneInspectorProtocol(Protocol):
    async def run(self, **arguments: Any) -> dict[str, Any]:
        """Return the current canonical arm-scene summary."""


class WorkcellManagerProtocol(Protocol):
    async def workcell_calibrations(self) -> dict[str, Any]:
        """Return current reviewed mounted-workcell activations."""


class TransitPreviewProtocol(Protocol):
    async def preview_transit_path(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one nonphysical controller-owned path preview."""

    async def commit_transit_path(
        self,
        payload: dict[str, Any],
        *,
        authorization_assertion: str,
    ) -> dict[str, Any]:
        """Commit one exact controller-owned path preview."""

    async def state(self) -> dict[str, Any]:
        """Return current Integrated controller state."""

    async def release_transit_path(self) -> dict[str, Any]:
        """Release a completed transit endpoint to gravity float."""


class AuthorizationStoreProtocol(Protocol):
    def create(self, **arguments: Any) -> dict[str, Any]:
        """Create one bounded physical-action decision."""

    def resolve(
        self,
        decision_id: str,
        *,
        resolution: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one decision after host tool authorization."""

    def issue_execution_assertion(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        """Issue one decision-bound execution assertion."""


def _arm_base_transform_is_missing(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    message = str(error)
    return (
        status_code == 404
        and "to_frame=rebot_arm_base" in message
        and "/v1/transform" in message
    )


def _point(value: Any, label: str) -> np.ndarray:
    point = np.asarray(value, dtype=np.float64)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ValueError(f"{label} must contain three finite values")
    return point


def _item_point(item: dict[str, Any]) -> tuple[np.ndarray, float]:
    if item.get("eligible_for_control_math") is not True:
        raise ValueError(
            "ITEM_METRIC_LOCATION_REQUIRED: acquire another view or a "
            "bounded task-plane estimate before approach planning"
        )
    location = item.get("location")
    if not isinstance(location, dict):
        raise ValueError("item metric location is missing")
    point = _point(location.get("target_point_m"), "item target point")
    uncertainty_m = float(location.get("uncertainty_radius_m") or 0.0)
    if not math.isfinite(uncertainty_m) or uncertainty_m < 0.0:
        raise ValueError("item uncertainty must be finite and non-negative")
    return point, uncertainty_m


def _effector_point(effector: dict[str, Any]) -> tuple[np.ndarray, float]:
    if effector.get("eligible_for_control_math") is not True:
        raise ValueError("EFFECTOR_METRIC_LOCATION_REQUIRED")
    reference = effector.get("control_reference")
    if not isinstance(reference, dict):
        raise ValueError("effector control reference is missing")
    point = _point(reference.get("target_point_m"), "effector target point")
    try:
        explicit_uncertainty = float(
            effector.get("uncertainty_radius_m") or 0.0
        )
    except (TypeError, ValueError):
        explicit_uncertainty = 0.0
    uncertainty_samples: list[float] = []
    for front in effector.get("front_points") or []:
        evidence = front.get("depth_evidence") if isinstance(front, dict) else None
        if not isinstance(evidence, dict):
            continue
        try:
            value = float(evidence.get("support_mad_m"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0.0:
            uncertainty_samples.append(value)
    uncertainty_m = max([0.005, explicit_uncertainty, *uncertainty_samples])
    return point, uncertainty_m


def _controller_tool_point(
    effector: dict[str, Any],
    *,
    effector_point: np.ndarray,
) -> tuple[np.ndarray, str]:
    consistency = effector.get("controller_consistency")
    consistency = consistency if isinstance(consistency, dict) else {}
    controller_reference = consistency.get("controller_reference")
    if isinstance(controller_reference, dict):
        point = _point(
            controller_reference.get("target_point_m"),
            "controller FK tool point",
        )
        return point, "CONTROLLER_CONSISTENCY_REFERENCE"

    reference = effector.get("control_reference")
    reference = reference if isinstance(reference, dict) else {}
    if str(reference.get("method") or "").strip().upper() == (
        "CURRENT_CONTROLLER_FORWARD_KINEMATICS"
    ):
        return effector_point.copy(), "CONTROL_REFERENCE_IS_CONTROLLER_FK"

    return (
        effector_point.copy(),
        "VISUAL_EFFECTOR_ABSOLUTE_FALLBACK_NO_CONTROLLER_FK",
    )


def _require_common_frame(
    item: dict[str, Any],
    effector: dict[str, Any],
) -> str:
    item_frame = str(item.get("target_frame") or "").strip()
    effector_frame = str(effector.get("target_frame") or "").strip()
    if item_frame != effector_frame or item_frame != "rebot_arm_base":
        raise ValueError(
            "item and effector must share the rebot_arm_base target frame"
        )
    item_calibration = str(item.get("calibration_revision") or "")
    effector_calibration = str(effector.get("calibration_revision") or "")
    if item_calibration != effector_calibration:
        raise ValueError(
            "item and effector observations use different calibration revisions"
        )
    return item_frame


def build_no_contact_correction_plan(
    *,
    item_location: dict[str, Any],
    effector_location: dict[str, Any],
    requested_standoff_m: float,
    iteration_index: int,
    maximum_iterations: int = 6,
    maximum_step_m: float = 1.2,
    alignment_tolerance_m: float = 0.008,
    uncertainty_clearance_m: float = 0.015,
    maximum_observation_skew_ms: float = 2_000.0,
    maximum_arm_radius_m: float = DEFAULT_MAXIMUM_ARM_RADIUS_M,
    vertical_policy: str = "FREE_3D",
    planned_at_us: int | None = None,
) -> dict[str, Any]:
    """Plan one destination-seeking Cartesian correction and re-observation."""

    target_frame = _require_common_frame(item_location, effector_location)
    requested_standoff = float(requested_standoff_m)
    if not math.isfinite(requested_standoff) or not 0.03 <= requested_standoff <= 0.5:
        raise ValueError("requested_standoff_m must be between 0.03 and 0.5")
    if not 1 <= int(maximum_iterations) <= 20:
        raise ValueError("maximum_iterations must be between 1 and 20")
    iteration = int(iteration_index)
    if iteration < 0:
        raise ValueError("iteration_index must be non-negative")
    maximum_step = float(maximum_step_m)
    if not math.isfinite(maximum_step) or not 0.005 <= maximum_step <= 1.2:
        raise ValueError("maximum_step_m must be between 0.005 and 1.2")
    tolerance = float(alignment_tolerance_m)
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("alignment_tolerance_m must be positive")
    maximum_arm_radius = float(maximum_arm_radius_m)
    if not math.isfinite(maximum_arm_radius) or maximum_arm_radius <= 0.0:
        raise ValueError("maximum_arm_radius_m must be positive")
    normalized_vertical_policy = str(vertical_policy or "").strip().upper()
    if normalized_vertical_policy not in VERTICAL_POLICIES:
        raise ValueError(
            "vertical_policy must be NO_DESCENT, PRESERVE_CURRENT_HEIGHT, "
            "or FREE_3D"
        )

    item_observed_at_us = int(item_location.get("observed_at_us") or 0)
    effector_observed_at_us = int(effector_location.get("observed_at_us") or 0)
    if item_observed_at_us <= 0 or effector_observed_at_us <= 0:
        raise ValueError("both observations require observed_at_us")
    observation_skew_ms = abs(
        item_observed_at_us - effector_observed_at_us
    ) / 1000.0
    if observation_skew_ms > float(maximum_observation_skew_ms):
        raise ValueError(
            "ITEM_EFFECTOR_OBSERVATION_SKEW: reacquire item and effector in "
            "one parallel observation cycle"
        )

    item_point, item_uncertainty_m = _item_point(item_location)
    effector_point, effector_uncertainty_m = _effector_point(effector_location)
    controller_tool_point, controller_reference_source = _controller_tool_point(
        effector_location,
        effector_point=effector_point,
    )
    combined_uncertainty_m = (
        item_uncertainty_m
        + effector_uncertainty_m
        + float(uncertainty_clearance_m)
    )
    effective_standoff_m = max(requested_standoff, combined_uncertainty_m)
    difference = effector_point - item_point
    current_distance_m = float(np.linalg.norm(difference))
    if current_distance_m <= 1e-9:
        raise ValueError(
            "effector and item references are coincident; retreat direction is undefined"
        )
    direction_from_item = difference / current_distance_m
    desired_effector_point = item_point + direction_from_item * effective_standoff_m
    unconstrained_desired_effector_point = desired_effector_point.copy()
    if normalized_vertical_policy == "PRESERVE_CURRENT_HEIGHT":
        desired_effector_point[2] = effector_point[2]
    elif normalized_vertical_policy == "NO_DESCENT":
        desired_effector_point[2] = max(
            desired_effector_point[2],
            effector_point[2],
        )
    full_correction = desired_effector_point - effector_point
    correction_distance_m = float(np.linalg.norm(full_correction))
    now_us = time.time_ns() // 1000 if planned_at_us is None else int(planned_at_us)

    if iteration >= int(maximum_iterations):
        status = "ITERATION_LIMIT_REACHED"
        predicted_effector_point = effector_point
        step_distance_m = 0.0
        workflow_complete = False
        next_action = "STOP_AND_REQUEST_REVIEW"
    elif correction_distance_m <= tolerance:
        status = "ALIGNED_AT_NO_CONTACT_STANDOFF"
        predicted_effector_point = effector_point
        step_distance_m = 0.0
        workflow_complete = True
        next_action = "NONE"
    else:
        status = "CORRECTION_STEP_READY"
        step_distance_m = min(correction_distance_m, maximum_step)
        predicted_effector_point = (
            effector_point
            + full_correction / correction_distance_m * step_distance_m
        )
        workflow_complete = False
        next_action = "MOVE_THEN_REOBSERVE_BOTH"

    applied_correction = predicted_effector_point - effector_point
    controller_target = controller_tool_point + applied_correction
    controller_uses_relative_delta = controller_reference_source == (
        "VISUAL_EFFECTOR_ABSOLUTE_FALLBACK_NO_CONTROLLER_FK"
    )
    predicted_distance_to_item_m = float(
        np.linalg.norm(predicted_effector_point - item_point)
    )
    if predicted_distance_to_item_m + 1e-9 < effective_standoff_m:
        raise RuntimeError("planned correction violates the no-contact standoff")
    item_object_id = str(item_location.get("object_id") or "").strip()
    return {
        "schema": "physical_agent.no_contact_item_correction_plan",
        "schema_version": 1,
        "status": status,
        "workflow_complete": workflow_complete,
        "physical_motion_authorized": False,
        "motion_submitted": False,
        "target_frame": target_frame,
        "object_id": item_object_id,
        "iteration_index": iteration,
        "maximum_iterations": int(maximum_iterations),
        "requested_standoff_m": requested_standoff,
        "effective_standoff_m": effective_standoff_m,
        "current_distance_to_item_m": current_distance_m,
        "predicted_distance_to_item_m": predicted_distance_to_item_m,
        "alignment_tolerance_m": tolerance,
        "combined_uncertainty_m": combined_uncertainty_m,
        "item_point_arm_base_m": item_point.tolist(),
        "effector_point_arm_base_m": effector_point.tolist(),
        "controller_tool_point_arm_base_m": controller_tool_point.tolist(),
        "controller_reference_source": controller_reference_source,
        "desired_effector_point_arm_base_m": desired_effector_point.tolist(),
        "unconstrained_desired_effector_point_arm_base_m": (
            unconstrained_desired_effector_point.tolist()
        ),
        "vertical_policy": normalized_vertical_policy,
        "predicted_effector_point_arm_base_m": predicted_effector_point.tolist(),
        "next_target_arm_base_m": controller_target.tolist(),
        "controller_target_policy": (
            "INTEGRATED_RESOLVES_DELTA_FROM_MEASURED_CONTROLLED_FRAME"
            if controller_uses_relative_delta
            else "APPLY_EFFECTOR_CORRECTION_DELTA_TO_CONTROLLER_FK"
        ),
        "full_correction_vector_m": full_correction.tolist(),
        "step_distance_m": step_distance_m,
        "maximum_step_m": maximum_step,
        "maximum_arm_radius_m": maximum_arm_radius,
        "arm_radius_policy": (
            "ADVISORY_ONLY_IK_JOINT_LIMITS_AND_SEMANTIC_SCENE_ARE_AUTHORITATIVE"
        ),
        "observation_skew_ms": observation_skew_ms,
        "planned_at_us": now_us,
        "contact_policy": {
            "behavior": "NO_CONTACT",
            "allowed_contact_object_ids": [],
            "permit_pushable_contact": False,
            "workpiece_contact_permission_ignored_for_this_behavior": True,
        },
        "controller_plan_request": (
            None
            if step_distance_m == 0.0
            else {
                "target": {
                    **(
                        {"position_delta_m": applied_correction.tolist()}
                        if controller_uses_relative_delta
                        else {"position_m": controller_target.tolist()}
                    ),
                    "rpy_rad": None,
                },
                "ik_mode": "POSE_6DOF",
                "requested_speed_m_s": 0.30,
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
                "final_state": "WAIT_FOR_NEXT",
                "execute": False,
                "physical_motion_authorized": False,
            }
        ),
        "required_before_motion": [
            "fresh canonical semantic scene from Fabric",
            "Integrated collision-free path preview",
            "fresh fenced physical authority",
            "controller-local safety checks",
        ],
        "required_after_motion": [
            "locate item again",
            "locate effector front again",
            "reject stale or calibration-mismatched observations",
            "recompute correction or stop when aligned",
        ],
        "next_action": next_action,
        "source_evidence": {
            "item_location": item_location,
            "effector_location": effector_location,
        },
    }


def build_no_contact_preview_context(
    *,
    item_location: dict[str, Any],
    effector_location: dict[str, Any],
    scene: dict[str, Any],
    workcell_calibrations: dict[str, Any],
) -> dict[str, Any]:
    """Bind one correction preview to exact perception and mount identities."""

    capability = item_location.get("capability_binding")
    capability = capability if isinstance(capability, dict) else {}
    binding = capability.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    binding_id = str(binding.get("binding_id") or "").strip()
    camera_provider_id = str(capability.get("provider_id") or "").strip()
    camera_instance_id = str(
        capability.get("provider_instance_id") or ""
    ).strip()
    camera_boot_id = str(capability.get("boot_id") or "").strip()
    capture = item_location.get("camera_capture")
    capture = capture if isinstance(capture, dict) else {}
    session_epoch = str(capture.get("session_epoch") or "").strip()
    camera_calibration_revision = str(
        item_location.get("calibration_revision") or ""
    ).strip()
    effector_capability = effector_location.get("capability_binding")
    effector_capability = (
        effector_capability
        if isinstance(effector_capability, dict)
        else {}
    )
    effector_capture = effector_location.get("camera_capture")
    effector_capture = (
        effector_capture if isinstance(effector_capture, dict) else {}
    )
    effector_identity = {
        "camera_provider_id": str(
            effector_capability.get("provider_id") or ""
        ).strip(),
        "camera_provider_instance_id": str(
            effector_capability.get("provider_instance_id") or ""
        ).strip(),
        "camera_boot_id": str(
            effector_capability.get("boot_id") or ""
        ).strip(),
        "camera_calibration_revision": str(
            effector_location.get("calibration_revision") or ""
        ).strip(),
    }
    scene_revision = str(scene.get("scene_revision") or "").strip()
    scene_expires_at_us = int(scene.get("expires_at_us") or 0)
    required_text = {
        "binding_id": binding_id,
        "camera_provider_id": camera_provider_id,
        "camera_provider_instance_id": camera_instance_id,
        "camera_boot_id": camera_boot_id,
        "scene_revision": scene_revision,
    }
    missing = [name for name, value in required_text.items() if not value]
    if missing:
        raise ValueError(
            "NO_CONTACT_PREVIEW_CONTEXT_MISSING:" + ",".join(missing)
        )
    expected_effector_identity = {
        "camera_provider_id": camera_provider_id,
        "camera_provider_instance_id": camera_instance_id,
        "camera_boot_id": camera_boot_id,
        "camera_calibration_revision": camera_calibration_revision,
    }
    if effector_identity != expected_effector_identity:
        raise ValueError("NO_CONTACT_OBSERVATION_CAMERA_IDENTITY_MISMATCH")
    now_us = time.time_ns() // 1000
    if scene.get("status") != "SCENE_READY" or scene_expires_at_us <= now_us:
        raise ValueError("NO_CONTACT_SEMANTIC_SCENE_NOT_CURRENT")

    activations = workcell_calibrations.get("activations")
    activations = activations if isinstance(activations, list) else []
    matches = []
    for activation in activations:
        if not isinstance(activation, dict):
            continue
        policy = str(activation.get("validity_policy") or "")
        stable_match = (
            activation.get("state") == "ACTIVE"
            and activation.get("motion_usable") is True
            and activation.get("expires_at_us") is None
            and policy in SUPPORTED_MOUNTED_WORKCELL_POLICIES
            and str(activation.get("camera_provider_id") or "")
            == camera_provider_id
            and str(activation.get("camera_calibration_revision") or "")
            == camera_calibration_revision
        )
        if not stable_match:
            continue
        if policy == MOUNTED_WORKCELL_POLICY_V1 and (
            str(activation.get("session_epoch") or "") != session_epoch
            or str(activation.get("camera_provider_instance_id") or "")
            != camera_instance_id
            or str(activation.get("camera_boot_id") or "") != camera_boot_id
        ):
            continue
        matches.append(activation)
    if len(matches) != 1:
        raise ValueError("NO_CURRENT_EXACT_WORKCELL_ACTIVATION")
    activation = matches[0]
    workcell_policy = str(activation.get("validity_policy") or "")
    effector_session_epoch = str(
        effector_capture.get("session_epoch") or ""
    ).strip()
    context_advisories: list[str] = []
    if workcell_policy == MOUNTED_WORKCELL_POLICY_V1:
        if not session_epoch:
            raise ValueError(
                "NO_CONTACT_PREVIEW_CONTEXT_MISSING:vio_session_epoch"
            )
        if effector_session_epoch != session_epoch:
            raise ValueError(
                "NO_CONTACT_OBSERVATION_CAMERA_IDENTITY_MISMATCH"
            )
        vio_session_epoch_policy = "REQUIRED_BY_MOUNTED_V1"
    else:
        vio_session_epoch_policy = "ADVISORY_FOR_MOUNTED_V2"
        if not session_epoch or not effector_session_epoch:
            context_advisories.append(
                "VIO_SESSION_EPOCH_UNAVAILABLE_NOT_REQUIRED_FOR_MOUNTED_V2"
            )
        elif effector_session_epoch != session_epoch:
            context_advisories.append(
                "VIO_SESSION_EPOCH_DIFFERED_NOT_USED_BY_MOUNTED_V2"
            )
    observation_timestamp_us = max(
        int(item_location.get("observed_at_us") or 0),
        int(effector_location.get("observed_at_us") or 0),
    )
    if observation_timestamp_us <= 0:
        raise ValueError("NO_CONTACT_OBSERVATION_TIMESTAMP_MISSING")
    return {
        "binding_id": binding_id,
        "camera_provider_id": camera_provider_id,
        "camera_provider_instance_id": camera_instance_id,
        "camera_boot_id": camera_boot_id,
        "camera_calibration_revision": camera_calibration_revision,
        "workcell_transform_id": str(
            activation.get("candidate_id")
            or activation.get("activation_id")
            or ""
        ),
        "workcell_transform_revision": str(
            activation.get("calibration_revision") or ""
        ),
        "workcell_transform_validity_policy": str(
            activation.get("validity_policy") or ""
        ),
        "vio_session_epoch": session_epoch or None,
        "vio_session_epoch_policy": vio_session_epoch_policy,
        "context_advisories": context_advisories,
        "observation_timestamp_us": observation_timestamp_us,
        "observation_expires_at_us": scene_expires_at_us,
        "scene_revision": scene_revision,
    }


class NoContactItemApproachAdapter:
    """Acquire both landmarks in parallel and plan one no-contact correction."""

    def __init__(
        self,
        item_locator: ItemLocatorProtocol,
        effector_locator: EffectorLocatorProtocol,
        *,
        scene_inspector: SceneInspectorProtocol | None = None,
        manager: WorkcellManagerProtocol | None = None,
        integrated: TransitPreviewProtocol | None = None,
        authorization_store: AuthorizationStoreProtocol | None = None,
    ):
        self.item_locator = item_locator
        self.effector_locator = effector_locator
        self.scene_inspector = scene_inspector
        self.manager = manager
        self.integrated = integrated
        self.authorization_store = authorization_store
        self.last_result: dict[str, Any] | None = None
        self._pending_execution: dict[str, dict[str, Any]] = {}
        self._pending_lock = asyncio.Lock()

    async def run(
        self,
        *,
        question: str,
        object_id: str | None = None,
        requested_standoff_m: float = 0.10,
        iteration_index: int = 0,
        maximum_iterations: int = 6,
        maximum_step_m: float = 1.2,
        vertical_policy: str = "FREE_3D",
        _observation_retry_count: int = 0,
    ) -> dict[str, Any]:
        shared_spatial = getattr(self.item_locator, "spatial", None)
        shared_context = None
        if (
            shared_spatial is not None
            and shared_spatial is getattr(self.effector_locator, "spatial", None)
            and callable(getattr(shared_spatial, "prepare_context", None))
        ):
            shared_context = await shared_spatial.prepare_context(
                target_frame="rebot_arm_base",
                skill_id=f"no-contact-paired-observation-{time.time_ns()}",
            )
        item_arguments = {
            "question": question,
            "target_frame": "rebot_arm_base",
            "object_id": object_id,
            "contact_policy": "NO_CONTACT",
            "depth_requirement": "PREFER_METRIC",
        }
        effector_arguments = {"target_frame": "rebot_arm_base"}
        if shared_context is not None:
            item_arguments["spatial_context"] = shared_context
            effector_arguments["spatial_context"] = shared_context
        item_task = self.item_locator.run(
            **item_arguments,
        )
        effector_task = self.effector_locator.run(**effector_arguments)
        acquired = await asyncio.gather(
            item_task,
            effector_task,
            return_exceptions=True,
        )
        errors = [
            value for value in acquired if isinstance(value, BaseException)
        ]
        if errors:
            if all(_arm_base_transform_is_missing(error) for error in errors):
                result = {
                    "schema": (
                        "physical_agent.no_contact_item_correction_plan"
                    ),
                    "schema_version": 1,
                    "status": "ARM_BASE_REGISTRATION_REQUIRED",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "motion_submitted": False,
                    "target_frame": "rebot_arm_base",
                    "object_id": str(object_id or ""),
                    "contact_policy": {
                        "behavior": "NO_CONTACT",
                        "allowed_contact_object_ids": [],
                        "permit_pushable_contact": False,
                    },
                    "reason": (
                        "The current VIO world epoch has no motion-usable "
                        "transform to rebot_arm_base. World-only item depth is "
                        "available, but arm motion requires this relationship."
                    ),
                    "required_next_tool": {
                        "name": "calibrate_stationary_workcell",
                        "arguments": {
                            "request": (
                                "Establish the current world-to-arm-base "
                                "relationship required to approach: "
                                f"{question}"
                            )
                        },
                    },
                    "retry_after_prerequisite": {
                        "name": "plan_no_contact_item_approach",
                        "arguments": {
                            "question": question,
                            "object_id": object_id,
                            "requested_standoff_m": requested_standoff_m,
                            "iteration_index": iteration_index,
                            "maximum_iterations": maximum_iterations,
                            "maximum_step_m": maximum_step_m,
                            "vertical_policy": vertical_policy,
                        },
                    },
                }
                self.last_result = result
                return result
            if _observation_retry_count < 2:
                await asyncio.sleep(0.15)
                return await self.run(
                    question=question,
                    object_id=object_id,
                    requested_standoff_m=requested_standoff_m,
                    iteration_index=iteration_index,
                    maximum_iterations=maximum_iterations,
                    maximum_step_m=maximum_step_m,
                    vertical_policy=vertical_policy,
                    _observation_retry_count=_observation_retry_count + 1,
                )
            raise errors[0]
        item, effector = acquired
        assert isinstance(item, dict) and isinstance(effector, dict)
        if item.get("eligible_for_control_math") is not True:
            if _observation_retry_count < 2:
                await asyncio.sleep(0.15)
                return await self.run(
                    question=question,
                    object_id=object_id,
                    requested_standoff_m=requested_standoff_m,
                    iteration_index=iteration_index,
                    maximum_iterations=maximum_iterations,
                    maximum_step_m=maximum_step_m,
                    vertical_policy=vertical_policy,
                    _observation_retry_count=_observation_retry_count + 1,
                )
            result = self._observation_rejected(
                status="ITEM_OBSERVATION_REJECTED",
                reason=str(item.get("status") or "ITEM_METRIC_LOCATION_REQUIRED"),
                next_action="REOBSERVE_ITEM_WITH_DEPTH_FALLBACK",
                item=item,
                effector=effector,
                object_id=object_id,
            )
        elif effector.get("eligible_for_control_math") is not True:
            if _observation_retry_count < 2:
                await asyncio.sleep(0.15)
                return await self.run(
                    question=question,
                    object_id=object_id,
                    requested_standoff_m=requested_standoff_m,
                    iteration_index=iteration_index,
                    maximum_iterations=maximum_iterations,
                    maximum_step_m=maximum_step_m,
                    vertical_policy=vertical_policy,
                    _observation_retry_count=_observation_retry_count + 1,
                )
            result = self._observation_rejected(
                status="EFFECTOR_OBSERVATION_REJECTED",
                reason=str(
                    effector.get("status") or "EFFECTOR_METRIC_LOCATION_REQUIRED"
                ),
                next_action="REOBSERVE_EFFECTOR_WITH_CONTROLLER_PRIOR",
                item=item,
                effector=effector,
                object_id=object_id,
            )
        else:
            try:
                result = build_no_contact_correction_plan(
                    item_location=item,
                    effector_location=effector,
                    requested_standoff_m=requested_standoff_m,
                    iteration_index=iteration_index,
                    maximum_iterations=maximum_iterations,
                    maximum_step_m=maximum_step_m,
                    vertical_policy=vertical_policy,
                )
            except (ValueError, RuntimeError) as error:
                if (
                    _observation_retry_count < 2
                    and self._observation_retryable_reason(str(error))
                ):
                    await asyncio.sleep(0.15)
                    return await self.run(
                        question=question,
                        object_id=object_id,
                        requested_standoff_m=requested_standoff_m,
                        iteration_index=iteration_index,
                        maximum_iterations=maximum_iterations,
                        maximum_step_m=maximum_step_m,
                        vertical_policy=vertical_policy,
                        _observation_retry_count=_observation_retry_count + 1,
                    )
                result = self._observation_rejected(
                    status="PLAN_INPUT_REJECTED",
                    reason=str(error),
                    next_action="REOBSERVE_BOTH_AND_REPLAN",
                    item=item,
                    effector=effector,
                    object_id=object_id,
                )
            if (
                result.get("status") == "CORRECTION_STEP_READY"
                and self.scene_inspector is not None
                and self.manager is not None
                and self.integrated is not None
            ):
                result = await self._attach_controller_preview(
                    result,
                    item=item,
                    effector=effector,
                    retry_arguments={
                        "question": question,
                        "object_id": object_id,
                        "requested_standoff_m": requested_standoff_m,
                        "iteration_index": iteration_index,
                        "maximum_iterations": maximum_iterations,
                        "maximum_step_m": maximum_step_m,
                        "vertical_policy": vertical_policy,
                    },
                )
        self.last_result = result
        return result

    @staticmethod
    def _observation_retryable_reason(reason: str) -> bool:
        normalized = str(reason or "").upper()
        return any(
            token in normalized
            for token in (
                "OBSERVATION",
                "CAMERA_IDENTITY",
                "CALIBRATION_REVISION",
                "ITEM_",
                "EFFECTOR_",
                "DEPTH",
                "METRIC",
                "CONTROLLER_FK",
            )
        )

    async def _attach_controller_preview(
        self,
        plan: dict[str, Any],
        *,
        item: dict[str, Any],
        effector: dict[str, Any],
        retry_arguments: dict[str, Any],
    ) -> dict[str, Any]:
        assert self.scene_inspector is not None
        assert self.manager is not None
        assert self.integrated is not None
        try:
            scene, calibrations = await asyncio.gather(
                self.scene_inspector.run(include_spheres=False),
                self.manager.workcell_calibrations(),
            )
            request_context = build_no_contact_preview_context(
                item_location=item,
                effector_location=effector,
                scene=scene,
                workcell_calibrations=calibrations,
            )
        except (ValueError, RuntimeError) as error:
            rejected = copy.deepcopy(plan)
            reason = str(error)
            scene_status = (
                scene.get("status")
                if "scene" in locals() and isinstance(scene, dict)
                else None
            )
            scene_required = (
                scene_status
                in {
                    None,
                    "NO_SCENE",
                    "TRACKER_COVERAGE_REQUIRED",
                    "SCENE_STALE",
                    "SCENE_INVALID",
                }
                or "scene_revision" in reason
                or "SEMANTIC_SCENE" in reason
            )
            rejected.update(
                {
                    "status": (
                        "SEMANTIC_SCENE_PROVIDER_REQUIRED"
                        if scene_required
                        else "CONTROLLER_PREVIEW_CONTEXT_REJECTED"
                    ),
                    "correction_status": plan.get("status"),
                    "reason": reason,
                    "next_action": (
                        "ACTIVATE_SCENE_COMPILER_THEN_RETRY_EXACT_PLAN"
                        if scene_required
                        else "RESTORE_SCENE_OR_WORKCELL_CONTEXT"
                    ),
                }
            )
            if scene_required:
                required_provider_id = (
                    str(scene.get("required_provider_id") or "")
                    if "scene" in locals() and isinstance(scene, dict)
                    else ""
                ) or "world_model.arm_scene_compiler"
                required_capability = (
                    "perception.scene.semantic_obstacles"
                    if required_provider_id == "perception.sam2_scene_tracker"
                    else "world_model.arm.semantic_scene"
                )
                rejected["next_action"] = (
                    "PUBLISH_POLICY_AND_ACTIVATE_SAM2_TRACKER_THEN_RETRY_EXACT_PLAN"
                    if required_provider_id == "perception.sam2_scene_tracker"
                    else "ACTIVATE_SCENE_COMPILER_THEN_RETRY_EXACT_PLAN"
                )
                rejected["required_next_tool"] = {
                    "name": "set_provider_residency",
                    "arguments": {
                        "provider_id": required_provider_id,
                        "action": "hot",
                        "required_capability": required_capability,
                    },
                }
                rejected["retry_after_prerequisite"] = {
                    "name": "plan_no_contact_item_approach",
                    "arguments": copy.deepcopy(retry_arguments),
                }
            return rejected

        request = copy.deepcopy(plan.get("controller_plan_request"))
        if not isinstance(request, dict):
            raise RuntimeError("no-contact correction has no plan request")
        request["request_context"] = request_context
        staged = copy.deepcopy(plan)
        staged["controller_plan_request"] = request
        staged["source_evidence"]["semantic_scene_summary"] = scene
        try:
            preview = await self.integrated.preview_transit_path(request)
        except Exception as error:
            staged.update(
                {
                    "status": "CONTROLLER_PREVIEW_UNAVAILABLE",
                    "correction_status": plan.get("status"),
                    "reason": str(error),
                    "next_action": "RESTORE_INTEGRATED_CONTROLLER_AND_REPLAN",
                    "required_next_tool": {
                        "name": "set_provider_residency",
                        "arguments": {
                            "provider_id": "robot_arm.primary.integrated",
                            "action": "hot",
                            "required_capability": (
                                "robot.motion.arm.integrated.plan."
                                "transit_path.shadow"
                            ),
                        },
                    },
                }
            )
            return staged
        attached = attach_controller_preview(staged, preview)
        attached["correction_status"] = plan.get("status")
        if attached.get("controller_preview_valid") is True:
            attached["status"] = "CONTROLLER_PREVIEW_READY"
            authority = attached.get("controller_preview_authority")
            authority = authority if isinstance(authority, dict) else {}
            requested_speed_m_s = float(
                request.get("requested_speed_m_s") or 0.0
            )
            selected_plan = preview.get("selected_plan")
            selected_plan = (
                selected_plan if isinstance(selected_plan, dict) else {}
            )
            speed_schedule = selected_plan.get("speed_schedule")
            speed_schedule = (
                speed_schedule if isinstance(speed_schedule, dict) else {}
            )
            joint_speed_policy = speed_schedule.get("joint_speed_policy")
            joint_speed_policy = (
                joint_speed_policy
                if isinstance(joint_speed_policy, dict)
                else {}
            )
            execution_arguments = {
                "plan_id": str(authority.get("plan_id") or ""),
            }
            authorization_arguments = {
                **execution_arguments,
                "request_sha256": str(authority.get("request_sha256") or ""),
                "preview_sha256": str(authority.get("preview_sha256") or ""),
                "distance_m": float(attached.get("step_distance_m") or 0.0),
                "planned_nominal_speed_m_s": requested_speed_m_s,
                "motion_intent": "NEW_RELATIVE_MOVE",
                "direction": "TARGET_VECTOR",
                "orientation_policy": "PRESERVE_CURRENT",
                "controlled_frame_yaw_delta_deg": None,
                "requested_peak_joint_speed_rad_s": float(
                    joint_speed_policy.get(
                        "requested_peak_joint_speed_rad_s", 0.0
                    )
                ),
                "effective_peak_joint_speed_rad_s": float(
                    joint_speed_policy.get(
                        "effective_peak_joint_speed_rad_s", 0.0
                    )
                ),
                "joint_speed_authentication_required": bool(
                    joint_speed_policy.get("authentication_required", False)
                ),
            }
            attached["required_next_tool"] = {
                "name": "execute_no_contact_approach_step",
                "arguments": execution_arguments,
            }
            attached["next_action"] = (
                "EXECUTE_EXACT_PREVIEW_THEN_REOBSERVE_BOTH"
            )
            if self.authorization_store is not None:
                async with self._pending_lock:
                    self._pending_execution[execution_arguments["plan_id"]] = {
                        "action": copy.deepcopy(attached),
                        "arguments": copy.deepcopy(authorization_arguments),
                        "authority": copy.deepcopy(authority),
                    }
            else:
                attached["execution_adapter_available"] = False
                attached["next_action"] = (
                    "RESTORE_DECISION_BOUND_EXECUTION_ADAPTER"
                )
        else:
            attached["status"] = "CONTROLLER_PREVIEW_REJECTED"
            attached["next_action"] = "FIX_CONTROLLER_PREVIEW_BLOCKERS"
        return attached

    async def pending_execution_authorization_arguments(
        self,
        plan_id: str,
    ) -> dict[str, Any] | None:
        """Return the canonical motion envelope behind one opaque continuation."""

        normalized_plan_id = str(plan_id or "").strip()
        if not normalized_plan_id:
            return None
        async with self._pending_lock:
            pending = self._pending_execution.get(normalized_plan_id)
            if pending is None:
                return None
            return copy.deepcopy(pending["arguments"])

    async def execute_preview(
        self,
        *,
        plan_id: str,
        _automatic_retry_count: int = 0,
    ) -> dict[str, Any]:
        """Execute one exact preview after the Agent SDK authorizes this tool."""

        if self.integrated is None or self.authorization_store is None:
            raise RuntimeError("no-contact execution dependencies are unavailable")
        normalized_plan_id = str(plan_id or "").strip()
        expired_action: dict[str, Any] | None = None
        async with self._pending_lock:
            pending = self._pending_execution.get(normalized_plan_id)
            if pending is None:
                return self._replan_required_result(
                    status="NO_CONTACT_PREVIEW_NOT_PENDING",
                    reason=(
                        "The requested preview is not pending in this Agent "
                        "process. Reobserve both landmarks and create a fresh plan."
                    ),
                )
            expected = pending["arguments"]
            authority = copy.deepcopy(pending["authority"])
            action = copy.deepcopy(pending["action"])
            expires_at_us = int(authority.get("expires_at_us") or 0)
            remaining_s = (expires_at_us - time.time_ns() // 1000) / 1_000_000.0
            if remaining_s <= 1.1:
                self._pending_execution.pop(normalized_plan_id, None)
                expired_action = action
            else:
                decision = self.authorization_store.create(
                    requester_type="SKILL",
                    requester_id="approach-item-no-contact",
                    decision_type="PHYSICAL_OBSERVATION_POSE",
                    title="Execute one no-contact item-approach correction",
                    summary=(
                        "Move the effector along the exact collision-checked controller "
                        "preview, then reacquire both item and effector landmarks."
                    ),
                    proposed_action=action,
                    evidence=copy.deepcopy(action.get("source_evidence") or {}),
                    safety={
                        "physical_motion": True,
                        "approval_executes_action": False,
                        "controller_preview_required": True,
                        "controller_preview_authority": authority,
                        "fresh_fenced_authority_required_at_execution": True,
                        "execution_must_reject_expired_or_restarted_preview": True,
                        "contact_policy": "NO_CONTACT",
                        "maximum_single_step_m": expected["distance_m"],
                        "reobservation_required_after_execution": True,
                    },
                    expires_in_s=min(120.0, remaining_s - 0.1),
                )
                approved = self.authorization_store.resolve(
                    decision["decision_id"],
                    resolution="APPROVED",
                    resolved_by="agent-sdk-approved-execution-tool",
                    note=(
                        "The Agent SDK invoked this execution function only after "
                        "its host authorization policy approved the exact tool call."
                    ),
                )
                issued = self.authorization_store.issue_execution_assertion(
                    approved["decision_id"]
                )
                self._pending_execution.pop(normalized_plan_id, None)

        if expired_action is not None:
            if _automatic_retry_count < 1:
                return await self._recover_commit_with_fresh_observations(
                    action=expired_action,
                    first_error="collision-checked preview expired before execution",
                    automatic_retry_count=_automatic_retry_count + 1,
                )
            return self._replan_required_result(
                status="NO_CONTACT_PREVIEW_EXPIRED",
                reason="collision-checked preview expired during automatic retry",
                action=expired_action,
            )

        try:
            result = await self.integrated.commit_transit_path(
                {
                    "plan_id": authority["plan_id"],
                    "request_sha256": authority["request_sha256"],
                    "preview_sha256": authority["preview_sha256"],
                    "decision_id": approved["decision_id"],
                    "authorization_assertion_sha256": issued["assertion_sha256"],
                },
                authorization_assertion=issued["assertion"],
            )
        except Exception as error:
            if (
                _automatic_retry_count < 1
                and self._retryable_commit_error(error)
            ):
                return await self._recover_commit_with_fresh_observations(
                    action=action,
                    first_error=str(error),
                    automatic_retry_count=_automatic_retry_count + 1,
                )
            return self._replan_required_result(
                status="NO_CONTACT_COMMIT_REJECTED",
                reason=str(error),
                action=action,
            )
        completion = await self._wait_for_transit_terminal(
            plan_id=normalized_plan_id,
            commit_result=result,
        )
        return {
            "schema": "physical_agent.no_contact_approach_step_execution",
            "schema_version": 1,
            "status": "COMPLETED",
            "decision_id": approved["decision_id"],
            "plan_id": normalized_plan_id,
            "motion_submitted": True,
            "measured_arrival_confirmed": completion[
                "measured_arrival_confirmed"
            ],
            "post_move_reobservation_required": True,
            "contact_policy": "NO_CONTACT",
            "executed_step_m": expected["distance_m"],
            "required_next_tool": {
                "name": "plan_no_contact_item_approach",
                "arguments": {
                    "question": str(
                        (
                            action.get("source_evidence", {})
                            .get("item_location", {})
                            .get("item_label")
                        )
                        or action.get("object_id")
                        or "the same item"
                    ),
                    "object_id": action.get("object_id") or None,
                    "requested_standoff_m": action.get("requested_standoff_m"),
                    "iteration_index": int(action.get("iteration_index") or 0) + 1,
                    "maximum_iterations": action.get("maximum_iterations"),
                    "maximum_step_m": action.get("maximum_step_m"),
                    "vertical_policy": action.get(
                        "vertical_policy", "FREE_3D"
                    ),
                },
            },
            "next_action": "REOBSERVE_BOTH_AND_REPLAN",
            "integrated_controller": {
                "commit": result,
                **completion,
            },
        }

    @staticmethod
    def _retryable_commit_error(error: BaseException) -> bool:
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "semantic scene is stale",
                "transit plan has expired",
                "arm moved after preview",
                "became colliding during commit revalidation",
                "transit plan is unavailable",
                "transit plan was consumed",
            )
        )

    async def _recover_commit_with_fresh_observations(
        self,
        *,
        action: dict[str, Any],
        first_error: str,
        automatic_retry_count: int,
    ) -> dict[str, Any]:
        evidence = action.get("source_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        item = evidence.get("item_location")
        item = item if isinstance(item, dict) else {}
        arguments = {
            "question": str(
                item.get("item_label")
                or action.get("object_id")
                or "the same user-requested work object"
            ),
            "object_id": action.get("object_id") or None,
            "requested_standoff_m": float(
                action.get("requested_standoff_m") or 0.1
            ),
            "iteration_index": int(action.get("iteration_index") or 0),
            "maximum_iterations": int(action.get("maximum_iterations") or 6),
            "maximum_step_m": float(action.get("maximum_step_m") or 1.2),
            "vertical_policy": str(
                action.get("vertical_policy") or "NO_DESCENT"
            ),
        }
        deadline = time.monotonic() + 12.0
        replanned: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            replanned = await self.run(**arguments)
            if replanned.get("status") == "CONTROLLER_PREVIEW_READY":
                break
            reason = str(replanned.get("reason") or "").upper()
            if (
                replanned.get("status") == "SEMANTIC_SCENE_PROVIDER_REQUIRED"
                and (
                    "STALE" in reason
                    or "NOT_CURRENT" in reason
                    or "EXPIRED" in reason
                )
            ):
                await asyncio.sleep(0.4)
                continue
            return {
                **replanned,
                "automatic_commit_recovery": {
                    "attempted": True,
                    "first_error": first_error,
                    "result": "REPLAN_DID_NOT_REACH_EXECUTABLE_PREVIEW",
                },
            }
        if not isinstance(replanned, dict) or replanned.get("status") != (
            "CONTROLLER_PREVIEW_READY"
        ):
            return self._replan_required_result(
                status="NO_CONTACT_AUTOMATIC_REPLAN_TIMEOUT",
                reason=(
                    f"{first_error}; no fresh executable scene/preview became "
                    "available during the bounded automatic retry"
                ),
                action=action,
            )
        next_tool = replanned.get("required_next_tool")
        next_tool = next_tool if isinstance(next_tool, dict) else {}
        next_arguments = next_tool.get("arguments")
        if not isinstance(next_arguments, dict):
            return self._replan_required_result(
                status="NO_CONTACT_AUTOMATIC_REPLAN_INCOMPLETE",
                reason=f"{first_error}; fresh preview has no execution continuation",
                action=action,
            )
        recovered = await self.execute_preview(
            **next_arguments,
            _automatic_retry_count=automatic_retry_count,
        )
        recovered["automatic_commit_recovery"] = {
            "attempted": True,
            "first_error": first_error,
            "result": recovered.get("status"),
            "fresh_item_effector_observation": True,
            "fresh_controller_preview": True,
        }
        return recovered

    async def _wait_for_transit_terminal(
        self,
        *,
        plan_id: str,
        commit_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Wait for the exact controller-selected terminal state."""

        assert self.integrated is not None
        if commit_result.get("status") != "EXECUTING":
            raise RuntimeError(
                "Integrated did not enter EXECUTING after transit commit"
            )
        planned_duration_s = float(
            commit_result.get("planned_duration_s") or 0.0
        )
        final_state = str(
            commit_result.get("final_state") or ""
        ).strip().upper()
        if final_state not in {"FLOAT", "FIXED", "WAIT_FOR_NEXT"}:
            raise RuntimeError(
                "Integrated commit did not report a valid final_state"
            )
        timeout_s = min(75.0, max(12.0, planned_duration_s + 15.0))
        deadline = time.monotonic() + timeout_s
        last_execution: dict[str, Any] | None = None
        try:
            while time.monotonic() < deadline:
                state = await self.integrated.state()
                planning = state.get("planning")
                planning = planning if isinstance(planning, dict) else {}
                execution = planning.get("authorized_transit")
                execution = execution if isinstance(execution, dict) else None
                if execution is not None:
                    last_execution = copy.deepcopy(execution)
                    if str(execution.get("plan_id") or "") != plan_id:
                        raise RuntimeError(
                            "Integrated reports a different active transit"
                        )
                    status = str(execution.get("status") or "").upper()
                    expected_status = {
                        "FIXED": "HOLDING_FINAL",
                        "WAIT_FOR_NEXT": "WAITING_NEXT",
                    }.get(final_state)
                    if expected_status is not None and status == expected_status:
                        return {
                            "arrival": last_execution,
                            "final_state": final_state,
                            "terminal_status": status,
                            "measured_arrival_confirmed": True,
                            "arrival_confirmation": (
                                "CONTROLLER_MEASURED_FINAL_POSITION_AND_"
                                "SETTLED_VELOCITY"
                            ),
                            "gravity_float_confirmed": False,
                        }
                    if status in {"FAILED", "CANCELLED"}:
                        raise RuntimeError(
                            "Integrated transit ended before arrival: "
                            f"{execution.get('error') or status}"
                        )
                else:
                    completed = planning.get("last_authorized_transit")
                    if isinstance(completed, dict):
                        last_execution = copy.deepcopy(completed)
                        status = str(completed.get("status") or "").upper()
                        if status in {"FAILED", "CANCELLED"}:
                            raise RuntimeError(
                                "Integrated transit ended before arrival: "
                                f"{completed.get('error') or status}"
                            )
                        if final_state == "FLOAT" and status == "COMPLETED_FLOAT":
                            if (
                                (state.get("safety") or {}).get(
                                    "float_confirmed"
                                )
                                is not True
                            ):
                                raise RuntimeError(
                                    "Integrated reported completed FLOAT "
                                    "without verified gravity float"
                                )
                            return {
                                "arrival": last_execution,
                                "final_state": final_state,
                                "terminal_status": status,
                                "measured_arrival_confirmed": True,
                                "arrival_confirmation": (
                                    "CONTROLLER_MEASURED_FINAL_POSITION_AND_"
                                    "SETTLED_VELOCITY_THEN_VERIFIED_FLOAT"
                                ),
                                "gravity_float_confirmed": True,
                            }
                await asyncio.sleep(0.1)
            raise TimeoutError(
                "Integrated transit did not reach its selected final state within "
                f"{timeout_s:.1f} seconds"
            )
        except Exception:
            try:
                await self.integrated.release_transit_path()
            except Exception:
                pass
            raise

    @staticmethod
    def _replan_required_result(
        *,
        status: str,
        reason: str,
        action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = action if isinstance(action, dict) else {}
        evidence = source.get("source_evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        item = evidence.get("item_location")
        item = item if isinstance(item, dict) else {}
        object_id = str(source.get("object_id") or item.get("object_id") or "")
        question = str(
            item.get("item_label")
            or object_id
            or "the same user-requested work object"
        )
        return {
            "schema": "physical_agent.no_contact_approach_step_execution",
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "motion_submitted": False,
            "contact_policy": "NO_CONTACT",
            "required_next_tool": {
                "name": "plan_no_contact_item_approach",
                "arguments": {
                    "question": question,
                    "object_id": object_id or None,
                    "requested_standoff_m": float(
                        source.get("requested_standoff_m") or 0.1
                    ),
                    "iteration_index": int(
                        source.get("iteration_index") or 0
                    ),
                    "maximum_iterations": int(
                        source.get("maximum_iterations") or 6
                    ),
                    "maximum_step_m": float(
                        source.get("maximum_step_m") or 1.2
                    ),
                    "vertical_policy": str(
                        source.get("vertical_policy") or "NO_DESCENT"
                    ),
                },
            },
            "next_action": "REOBSERVE_BOTH_AND_REPLAN",
        }

    @staticmethod
    def _observation_rejected(
        *,
        status: str,
        reason: str,
        next_action: str,
        item: dict[str, Any],
        effector: dict[str, Any],
        object_id: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": "physical_agent.no_contact_item_correction_plan",
            "schema_version": 1,
            "status": status,
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "motion_submitted": False,
            "target_frame": "rebot_arm_base",
            "object_id": str(object_id or item.get("object_id") or ""),
            "reason": reason,
            "contact_policy": {
                "behavior": "NO_CONTACT",
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            },
            "next_action": next_action,
            "source_evidence": {
                "item_location": item,
                "effector_location": effector,
            },
        }
