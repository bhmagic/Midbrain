from __future__ import annotations

import time
from types import SimpleNamespace

from stationary_world_arm_alignment.candidate_review import canonical_sha256
from stationary_world_arm_alignment.skill import AlignmentSkill


def _skill(mode: str) -> AlignmentSkill:
    skill = object.__new__(AlignmentSkill)
    skill.config = {
        "candidate_review": {
            "mode": mode,
            "ttl_s": 900,
        }
    }
    return skill


def _v2_candidate(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "schema": (
            "midbrain.skill.stationary_world_arm_alignment."
            "calibration_candidate"
        ),
        "schema_version": 3,
        "quality_provenance": {
            "semantic_alignment": {
                "status": "PASSED",
            }
        },
        "frame_contract": {
            "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "camera_optical_convention_id": (
                "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            ),
            "legacy_candidate_compatibility": "REJECT",
        },
    }


def test_shadow_mode_rejects_legacy_and_accepts_v2_prior() -> None:
    skill = _skill("SHADOW")

    assert not skill._prior_alignment_review_usable(
        {
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "motion_usable": False,
            "expires_at_us": 1,
        },
        {},
    )
    assert skill._prior_alignment_review_usable(
        {
            "candidate": _v2_candidate("candidate-v2"),
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "motion_usable": False,
            "expires_at_us": 1,
        },
        {},
    )


def test_enforced_mode_rejects_pending_prior_candidate() -> None:
    skill = _skill("ENFORCED")

    assert not skill._prior_alignment_review_usable(
        {
            "alignment_id": "candidate-1",
            "vio_session_epoch": "epoch-1",
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "motion_usable": False,
            "expires_at_us": time.time_ns() // 1000 + 1_000_000,
        },
        {"activations": []},
    )


def test_enforced_mode_requires_matching_active_manager_registration() -> None:
    skill = _skill("ENFORCED")
    now_us = time.time_ns() // 1000
    prior = {
        "alignment_id": "candidate-1",
        "vio_session_epoch": "epoch-1",
        "candidate": _v2_candidate("candidate-1"),
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "motion_usable": False,
        "expires_at_us": now_us + 1_000_000,
    }

    assert skill._prior_alignment_review_usable(
        prior,
        {
            "activations": [
                {
                    "state": "ACTIVE",
                    "motion_usable": True,
                    "candidate_id": "candidate-1",
                    "session_epoch": "epoch-1",
                    "expires_at_us": now_us + 1_000_000,
                }
            ]
        },
    )
    assert not skill._prior_alignment_review_usable(
        prior,
        {
            "activations": [
                {
                    "state": "ACTIVE",
                    "motion_usable": True,
                    "candidate_id": "candidate-1",
                    "session_epoch": "epoch-1",
                    "expires_at_us": now_us - 1,
                }
            ]
        },
    )


def test_enforced_prior_selection_ignores_rejected_latest_candidate() -> None:
    skill = _skill("ENFORCED")
    approved = {
        "alignment_id": "approved-1",
        "candidate": _v2_candidate("approved-1"),
    }
    rejected = {
        "alignment_id": "rejected-2",
        "candidate": _v2_candidate("rejected-2"),
    }
    skill.store = SimpleNamespace(
        latest=lambda: rejected,
        get=lambda candidate_id: (
            approved if candidate_id == "approved-1" else None
        ),
    )

    selected = skill._manager_verified_prior_alignment(
        {
            "activations": [
                {
                    "state": "EXPIRED",
                    "enforcement": "ENFORCED",
                    "review_decision_id": "review-1",
                    "candidate_id": "approved-1",
                    "candidate_sha256": canonical_sha256(
                        approved["candidate"]
                    ),
                    "expires_at_us": 200,
                    "activated_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
    )

    assert selected == approved


def test_enforced_prior_selection_rejects_digest_mismatch_and_revocation() -> None:
    skill = _skill("ENFORCED")
    prior = {
        "alignment_id": "candidate-1",
        "candidate": _v2_candidate("candidate-1"),
    }
    skill.store = SimpleNamespace(
        latest=lambda: prior,
        get=lambda _candidate_id: prior,
    )

    assert (
        skill._manager_verified_prior_alignment(
            {
                "activations": [
                    {
                        "state": "EXPIRED",
                        "enforcement": "ENFORCED",
                        "review_decision_id": "review-1",
                        "candidate_id": "candidate-1",
                        "candidate_sha256": "0" * 64,
                        "expires_at_us": 200,
                    },
                    {
                        "state": "REVOKED",
                        "enforcement": "ENFORCED",
                        "review_decision_id": "review-1",
                        "candidate_id": "candidate-1",
                        "candidate_sha256": canonical_sha256(
                            prior["candidate"]
                        ),
                        "expires_at_us": 100,
                    },
                ]
            }
        )
        is None
    )
