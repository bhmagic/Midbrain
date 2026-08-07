from __future__ import annotations

import copy
from typing import Any

import numpy as np

from .geometry import apply_transform, finite_vector, rigid_transform


REFINEMENT_SCHEMA = "midbrain.arm_root_translation_refinement"
REFINEMENT_SCHEMA_VERSION = 1
COMPACT_STATE_SCHEMA = "midbrain.compact_arm_root_alignment_state"
COMPACT_STATE_SCHEMA_VERSION = 1


def _adoption_factor(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("adoption_factor must be a number from zero to one")
    try:
        factor = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "adoption_factor must be a number from zero to one"
        ) from error
    if not np.isfinite(factor) or not 0.0 <= factor <= 1.0:
        raise ValueError("adoption_factor must be a number from zero to one")
    return factor


def prepare_translation_refinement(
    *,
    active_world_from_base: Any,
    base_from_tool: Any,
    tool_landmark_point_m: Any,
    observed_world_landmark_point_m: Any,
    adoption_factor: float,
    review_threshold_m: float,
    source_revision: int,
    identities: dict[str, Any],
    landmark_id: str,
    observation_provenance: dict[str, Any],
) -> dict[str, Any]:
    active = rigid_transform(active_world_from_base, "active_world_from_base")
    fk = rigid_transform(base_from_tool, "base_from_tool")
    tool_point = finite_vector(tool_landmark_point_m, "tool_landmark_point_m")
    observed_world = finite_vector(
        observed_world_landmark_point_m,
        "observed_world_landmark_point_m",
    )
    factor = _adoption_factor(adoption_factor)
    threshold = float(review_threshold_m)
    if not np.isfinite(threshold) or threshold < 0.0:
        raise ValueError("review_threshold_m must be non-negative and finite")
    if isinstance(source_revision, bool) or int(source_revision) < 0:
        raise ValueError("source_revision must be a non-negative integer")
    if not isinstance(identities, dict) or not identities:
        raise ValueError("identities must be a non-empty object")
    if not isinstance(observation_provenance, dict):
        raise ValueError("observation_provenance must be an object")
    if not str(landmark_id).strip():
        raise ValueError("landmark_id must be non-empty")

    rotation = active[:3, :3]
    base_landmark = apply_transform(fk, tool_point)
    world_from_controlled_rotation = rotation @ fk[:3, :3]
    estimated_world_controlled_origin = (
        observed_world - world_from_controlled_rotation @ tool_point
    )
    estimated_translation = (
        estimated_world_controlled_origin - rotation @ fk[:3, 3]
    )
    current_translation = active[:3, 3]
    raw_delta = estimated_translation - current_translation
    raw_norm = float(np.linalg.norm(raw_delta))
    adopted_delta = factor * raw_delta
    proposed = active.copy()
    proposed[:3, 3] = current_translation + adopted_delta
    review_required = raw_norm > threshold
    if factor == 0.0:
        status = "OBSERVATION_ONLY"
        workflow_complete = True
        eligible = False
    elif review_required:
        status = "SECOND_VLM_REVIEW_REQUIRED"
        workflow_complete = False
        eligible = False
    else:
        status = "TRANSLATION_UPDATE_READY"
        workflow_complete = True
        eligible = True
    return {
        "schema": REFINEMENT_SCHEMA,
        "schema_version": REFINEMENT_SCHEMA_VERSION,
        "status": status,
        "workflow_complete": workflow_complete,
        "eligible_for_state_update": eligible,
        "motion_usable": False,
        "physical_motion_submitted": False,
        "physical_motion_authorized": False,
        "rotation_change_allowed": False,
        "rotation_change_rad": 0.0,
        "landmark_id": str(landmark_id),
        "source_revision": int(source_revision),
        "identities": copy.deepcopy(identities),
        "source_world_from_base": active.tolist(),
        "base_from_tool": fk.tolist(),
        "tool_landmark_point_m": tool_point.tolist(),
        "controlled_frame_to_landmark_translation_m": tool_point.tolist(),
        "landmark_to_controlled_frame_translation_m": (-tool_point).tolist(),
        "observed_world_landmark_point_m": observed_world.tolist(),
        "estimated_world_controlled_frame_origin_m": (
            estimated_world_controlled_origin.tolist()
        ),
        "expected_base_landmark_point_m": base_landmark.tolist(),
        "estimated_full_translation_m": estimated_translation.tolist(),
        "raw_translation_delta_m": raw_delta.tolist(),
        "raw_translation_delta_norm_m": raw_norm,
        "adoption_factor": factor,
        "adopted_translation_delta_m": adopted_delta.tolist(),
        "proposed_world_from_base": proposed.tolist(),
        "quality_review": {
            "required": review_required,
            "threshold_m": threshold,
            "threshold_basis": "RAW_FULL_TRANSLATION_DELTA_NORM_BEFORE_ADOPTION",
            "verdict": "NOT_RUN",
        },
        "observation_provenance": copy.deepcopy(observation_provenance),
        "state_model": {
            "recursive_parent_chain": False,
            "activation_policy": "ATOMIC_EXPECTED_REVISION_MATCH",
        },
    }


def finalize_translation_refinement(
    proposal: dict[str, Any],
    *,
    quality_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(proposal, dict)
        or proposal.get("schema") != REFINEMENT_SCHEMA
        or proposal.get("schema_version") != REFINEMENT_SCHEMA_VERSION
    ):
        raise ValueError("proposal is not an arm-root translation refinement")
    result = copy.deepcopy(proposal)
    required = bool((result.get("quality_review") or {}).get("required"))
    if not required:
        if quality_review is not None:
            raise ValueError("quality review was supplied when it was not required")
        return result
    if not isinstance(quality_review, dict):
        raise RuntimeError("the required second VLM quality review is missing")
    if (
        quality_review.get("schema")
        != "midbrain.effector_landmark_quality_review"
        or quality_review.get("schema_version") != 1
        or quality_review.get("landmark_id") != result.get("landmark_id")
    ):
        raise RuntimeError("quality-review identity does not match the refinement")
    verdict = str(quality_review.get("verdict") or "").upper()
    if verdict not in {"PASS", "FAIL", "UNRESOLVED"}:
        raise RuntimeError("quality-review verdict is invalid")
    result["quality_review"] = {
        **result["quality_review"],
        "verdict": verdict,
        "reason": str(quality_review.get("reason") or ""),
        "reviewed_point_ids": list(
            quality_review.get("reviewed_point_ids") or []
        ),
    }
    result["workflow_complete"] = True
    if verdict == "PASS":
        if float(result["adoption_factor"]) == 0.0:
            result["status"] = "OBSERVATION_ONLY"
            result["eligible_for_state_update"] = False
        else:
            result["status"] = "TRANSLATION_UPDATE_READY"
            result["eligible_for_state_update"] = True
    else:
        result["status"] = "REJECTED_QUALITY_REVIEW"
        result["eligible_for_state_update"] = False
    return result


def apply_compact_translation_update(
    state: dict[str, Any],
    refinement: dict[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(state, dict)
        or state.get("schema") != COMPACT_STATE_SCHEMA
        or state.get("schema_version") != COMPACT_STATE_SCHEMA_VERSION
    ):
        raise ValueError("state is not a compact arm-root alignment state")
    if (
        not isinstance(refinement, dict)
        or refinement.get("schema") != REFINEMENT_SCHEMA
        or refinement.get("schema_version") != REFINEMENT_SCHEMA_VERSION
    ):
        raise ValueError("refinement schema is invalid")
    current_revision = int(state.get("revision"))
    if int(refinement.get("source_revision")) != current_revision:
        raise RuntimeError("translation refinement is stale for the active revision")
    if refinement.get("identities") != state.get("identities"):
        raise RuntimeError("translation refinement identities do not match active state")
    current = rigid_transform(state.get("world_from_base"), "state.world_from_base")
    source = rigid_transform(
        refinement.get("source_world_from_base"),
        "refinement.source_world_from_base",
    )
    if not np.array_equal(current, source):
        raise RuntimeError("translation refinement source transform is stale")
    if refinement.get("status") == "OBSERVATION_ONLY":
        return copy.deepcopy(state)
    if (
        refinement.get("status") != "TRANSLATION_UPDATE_READY"
        or refinement.get("eligible_for_state_update") is not True
    ):
        raise RuntimeError("translation refinement is not eligible for state update")
    proposed = rigid_transform(
        refinement.get("proposed_world_from_base"),
        "refinement.proposed_world_from_base",
    )
    if not np.array_equal(current[:3, :3], proposed[:3, :3]):
        raise RuntimeError("translation refinement attempted to change rotation")
    return {
        "schema": COMPACT_STATE_SCHEMA,
        "schema_version": COMPACT_STATE_SCHEMA_VERSION,
        "revision": current_revision + 1,
        "world_from_base": proposed.tolist(),
        "identities": copy.deepcopy(state["identities"]),
        "last_update": {
            "landmark_id": refinement["landmark_id"],
            "source_revision": current_revision,
            "adoption_factor": float(refinement["adoption_factor"]),
            "raw_translation_delta_m": list(
                refinement["raw_translation_delta_m"]
            ),
            "raw_translation_delta_norm_m": float(
                refinement["raw_translation_delta_norm_m"]
            ),
            "adopted_translation_delta_m": list(
                refinement["adopted_translation_delta_m"]
            ),
            "quality_review_verdict": str(
                (refinement.get("quality_review") or {}).get("verdict")
                or "NOT_RUN"
            ),
        },
    }
