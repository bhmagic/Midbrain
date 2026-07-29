from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any

import numpy as np


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transform_point(matrix: np.ndarray, point: np.ndarray) -> np.ndarray:
    return (matrix @ np.append(point, 1.0))[:3]


def _normalized_plan_request(proposal: dict[str, Any]) -> dict[str, Any]:
    request = proposal.get("controller_plan_request")
    if not isinstance(request, dict):
        return {}
    target = request.get("target")
    if not isinstance(target, dict):
        return {}
    context = request.get("request_context")
    if not isinstance(context, dict):
        return {}
    allowed = request.get("allowed_contact_object_ids", [])
    if not isinstance(allowed, list):
        return {}
    try:
        speed = float(request.get("requested_speed_m_s", 0.05))
    except (TypeError, ValueError):
        return {}
    return {
        "target": {
            "position_m": copy.deepcopy(target.get("position_m")),
            "rpy_rad": copy.deepcopy(target.get("rpy_rad")),
        },
        "requested_speed_m_s": speed,
        "allowed_contact_object_ids": sorted(str(value) for value in allowed),
        "permit_pushable_contact": bool(
            request.get("permit_pushable_contact", False)
        ),
        "request_context": copy.deepcopy(context),
    }


def _preview_validation_issues(
    proposal: dict[str, Any],
    *,
    now_us: int | None = None,
) -> list[str]:
    now = time.time_ns() // 1000 if now_us is None else int(now_us)
    preview = proposal.get("controller_preview")
    if not isinstance(preview, dict):
        return ["CONTROLLER_PREVIEW_MISSING"]

    issues: list[str] = []
    expected_values = {
        "status": "PLANNED",
        "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
        "enforcement": "SHADOW_NONPHYSICAL",
        "physical_motion_authorized": False,
        "control_state_unchanged": True,
        "lease_unchanged": True,
    }
    for field, expected in expected_values.items():
        if preview.get(field) != expected:
            issues.append(f"PREVIEW_FIELD_MISMATCH:{field}")

    plan_id = str(preview.get("plan_id") or "").strip()
    if not plan_id:
        issues.append("PREVIEW_PLAN_ID_MISSING")
    selected_plan = preview.get("selected_plan")
    if not isinstance(selected_plan, dict):
        issues.append("SELECTED_PLAN_MISSING")
    else:
        if selected_plan.get("planning_valid") is not True:
            issues.append("SELECTED_PLAN_INVALID")
        selected_preview = selected_plan.get("preview")
        if not isinstance(selected_preview, dict):
            issues.append("SELECTED_PLAN_PREVIEW_MISSING")
        else:
            if selected_preview.get("collision_free") is not True:
                issues.append("SELECTED_PLAN_NOT_COLLISION_FREE")
            if str(selected_preview.get("preview_id") or "") != plan_id:
                issues.append("SELECTED_PLAN_ID_MISMATCH")

    contract = preview.get("preview_contract")
    if not isinstance(contract, dict):
        return issues + ["PREVIEW_CONTRACT_MISSING"]
    if (
        contract.get("schema")
        != "physical_agent.integrated_transit_preview_contract"
        or contract.get("schema_version") != 1
    ):
        issues.append("PREVIEW_CONTRACT_SCHEMA_INVALID")
    if str(contract.get("preview_id") or "") != plan_id:
        issues.append("PREVIEW_CONTRACT_PLAN_ID_MISMATCH")
    for field in (
        "controller_provider_id",
        "controller_provider_instance_id",
        "controller_boot_id",
        "controller_configuration_sha256",
    ):
        if not str(contract.get(field) or "").strip():
            issues.append(f"PREVIEW_CONTRACT_IDENTITY_MISSING:{field}")
    if contract.get("physical_motion_authorized") is not False:
        issues.append("PREVIEW_CONTRACT_PHYSICAL_AUTHORITY_INVALID")
    if contract.get("preview_grants_commit_authority") is not False:
        issues.append("PREVIEW_CONTRACT_COMMIT_AUTHORITY_INVALID")
    if contract.get("commit_endpoint_exposed") is not False:
        issues.append("PREVIEW_CONTRACT_COMMIT_SURFACE_INVALID")

    try:
        issued_at_us = int(contract.get("issued_at_us"))
        expires_at_us = int(contract.get("expires_at_us"))
        if issued_at_us <= 0 or expires_at_us <= issued_at_us:
            issues.append("PREVIEW_CONTRACT_TIME_RANGE_INVALID")
        if issued_at_us > now + 1_000_000:
            issues.append("PREVIEW_CONTRACT_ISSUED_IN_FUTURE")
        if expires_at_us <= now:
            issues.append("PREVIEW_CONTRACT_EXPIRED")
    except (TypeError, ValueError):
        issues.append("PREVIEW_CONTRACT_TIME_INVALID")

    expected_request = _normalized_plan_request(proposal)
    normalized_request = contract.get("normalized_request")
    if not expected_request or normalized_request != expected_request:
        issues.append("PREVIEW_REQUEST_MISMATCH")
    if contract.get("request_sha256") != _canonical_sha256(expected_request):
        issues.append("PREVIEW_REQUEST_DIGEST_MISMATCH")

    request_context = expected_request.get("request_context", {})
    if (
        contract.get("request_context_sha256")
        != _canonical_sha256(request_context)
    ):
        issues.append("PREVIEW_CONTEXT_DIGEST_MISMATCH")
    if contract.get("request_context_complete") is not True:
        issues.append("PREVIEW_CONTEXT_INCOMPLETE")
    if contract.get("request_context_issues") not in ([], ()):
        issues.append("PREVIEW_CONTEXT_HAS_ISSUES")

    scene_revision = request_context.get("scene_revision")
    if (
        not str(scene_revision or "").strip()
        or contract.get("scene_revision") != scene_revision
        or preview.get("scene_revision") != scene_revision
    ):
        issues.append("PREVIEW_SCENE_REVISION_MISMATCH")
    for field in (
        "workcell_transform_expires_at_us",
        "observation_expires_at_us",
    ):
        try:
            if int(request_context.get(field)) <= now:
                issues.append(f"PREVIEW_CONTEXT_EXPIRED:{field}")
        except (TypeError, ValueError):
            issues.append(f"PREVIEW_CONTEXT_TIME_INVALID:{field}")

    unsigned_contract = copy.deepcopy(contract)
    claimed_preview_sha256 = unsigned_contract.pop("preview_sha256", None)
    planning_result = copy.deepcopy(preview)
    planning_result.pop("preview_contract", None)
    planning_result.pop("control_audit", None)
    expected_preview_sha256 = _canonical_sha256(
        {
            "planning_result": planning_result,
            "preview_contract": unsigned_contract,
        }
    )
    if claimed_preview_sha256 != expected_preview_sha256:
        issues.append("PREVIEW_DIGEST_MISMATCH")
    return issues


def _preview_authority(preview: dict[str, Any]) -> dict[str, Any]:
    contract = preview.get("preview_contract")
    contract = contract if isinstance(contract, dict) else {}
    return {
        "schema": "physical_agent.observation_preview_authority",
        "schema_version": 1,
        "plan_id": preview.get("plan_id"),
        "request_sha256": contract.get("request_sha256"),
        "preview_sha256": contract.get("preview_sha256"),
        "controller_provider_id": contract.get("controller_provider_id"),
        "controller_provider_instance_id": contract.get(
            "controller_provider_instance_id"
        ),
        "controller_boot_id": contract.get("controller_boot_id"),
        "controller_configuration_sha256": contract.get(
            "controller_configuration_sha256"
        ),
        "issued_at_us": contract.get("issued_at_us"),
        "expires_at_us": contract.get("expires_at_us"),
        "scene_revision": contract.get("scene_revision"),
        "lease_snapshot": copy.deepcopy(contract.get("lease_snapshot")),
    }


def build_observation_motion_proposal(
    *,
    object_point_world_m: list[float] | tuple[float, float, float],
    world_from_arm_base: list[float] | np.ndarray,
    view_mode: str,
    standoff_m: float,
    source_evidence: dict[str, Any],
    preview_context: dict[str, Any],
) -> dict[str, Any]:
    """Build a nonphysical front/top end-effector observation proposal."""

    object_world = np.asarray(object_point_world_m, dtype=np.float64)
    transform = np.asarray(world_from_arm_base, dtype=np.float64)
    if object_world.shape != (3,) or not np.all(np.isfinite(object_world)):
        raise ValueError("object_point_world_m must contain three finite values")
    if transform.size == 16:
        transform = transform.reshape(4, 4)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("world_from_arm_base must be a finite 4x4 transform")
    distance = float(standoff_m)
    if not 0.05 <= distance <= 0.50:
        raise ValueError("standoff_m must be between 0.05 and 0.50")
    mode = str(view_mode).upper()
    arm_origin_world = transform[:3, 3]
    if mode == "TOP":
        direction_from_object = np.asarray([0.0, 1.0, 0.0])
    elif mode == "FRONT":
        direction_from_object = arm_origin_world - object_world
        direction_from_object[1] = 0.0
        norm = float(np.linalg.norm(direction_from_object))
        if norm <= 1e-9:
            raise ValueError(
                "FRONT view is undefined when the object is vertically above the arm base"
            )
        direction_from_object /= norm
    else:
        raise ValueError("view_mode must be FRONT or TOP")
    if not isinstance(preview_context, dict):
        raise ValueError("preview_context must be an object")

    target_world = object_world + distance * direction_from_object
    arm_from_world = np.linalg.inv(transform)
    target_arm = _transform_point(arm_from_world, target_world)
    proposal = {
        "schema": "physical_agent.observation_motion_proposal",
        "schema_version": 1,
        "status": "AUTHORIZATION_REQUIRED",
        "motion_usable": False,
        "physical_motion_authorized": False,
        "view_mode": mode,
        "standoff_m": distance,
        "object_point_world_m": object_world.tolist(),
        "proposed_position_world_m": target_world.tolist(),
        "proposed_position_arm_base_m": target_arm.tolist(),
        "orientation_policy": "PRESERVE_CURRENT_TOOL_ORIENTATION",
        "controller_plan_request": {
            "endpoint": "/v1/motion/path-plan",
            "target": {
                "position_m": target_arm.tolist(),
                "rpy_rad": None,
            },
            "requested_speed_m_s": 0.05,
            "allowed_contact_object_ids": [],
            "permit_pushable_contact": False,
            "request_context": copy.deepcopy(preview_context),
            "execute": False,
            "physical_motion_authorized": False,
        },
        "source_evidence": dict(source_evidence),
        "required_before_execution": [
            "controller path preview accepts a collision-free candidate",
            "decision-specific operator authorization",
            "fresh fenced physical authority",
            "provider-local safety checks",
        ],
    }
    return proposal


def create_observation_motion_authorization(
    store: Any,
    proposal: dict[str, Any],
    *,
    requester_id: str,
    expires_in_s: float = 120.0,
) -> dict[str, Any]:
    if proposal.get("physical_motion_authorized") is not False:
        raise ValueError("only non-authorized proposals may enter the UI")
    issues = _preview_validation_issues(proposal)
    if issues or proposal.get("controller_preview_valid") is not True:
        raise ValueError(
            "a valid nonphysical Integrated controller preview is required "
            f"before authorization: {', '.join(issues)}"
        )
    preview = proposal["controller_preview"]
    preview_authority = _preview_authority(preview)
    remaining_s = (
        int(preview_authority["expires_at_us"]) - time.time_ns() // 1000
    ) / 1_000_000.0
    if remaining_s <= 1.1:
        raise ValueError(
            "the Integrated controller preview expires too soon for "
            "authorization"
        )
    decision_lifetime_s = min(float(expires_in_s), remaining_s - 0.1)
    return store.create(
        requester_type="SKILL",
        requester_id=requester_id,
        decision_type="PHYSICAL_OBSERVATION_POSE",
        title=f"Move to a {proposal['view_mode'].lower()} observation pose",
        summary=(
            "The agent has identified and registered the pointed object and "
            "proposes an end-effector observation position."
        ),
        proposed_action=proposal,
        evidence=proposal.get("source_evidence") or {},
        safety={
            "physical_motion": True,
            "approval_executes_action": False,
            "controller_preview_required": True,
            "controller_preview_authority": preview_authority,
            "fresh_fenced_authority_required_at_execution": True,
            "execution_must_reject_expired_or_restarted_preview": True,
        },
        expires_in_s=decision_lifetime_s,
    )


def attach_controller_preview(
    proposal: dict[str, Any],
    preview: dict[str, Any],
) -> dict[str, Any]:
    """Attach one real nonphysical Integrated preview to an observation proposal."""

    result = copy.deepcopy(proposal)
    result["controller_preview"] = copy.deepcopy(preview)
    result["controller_preview_received"] = True
    issues = _preview_validation_issues(result)
    valid = not issues
    result["controller_preview_authority"] = _preview_authority(preview)
    result["controller_preview_validation_issues"] = issues
    result["controller_preview_valid"] = valid
    result["motion_usable"] = False
    result["physical_motion_authorized"] = False
    result["status"] = "AUTHORIZATION_REQUIRED" if valid else "PREVIEW_REJECTED"
    return result
