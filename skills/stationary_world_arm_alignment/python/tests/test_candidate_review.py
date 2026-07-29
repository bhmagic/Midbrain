from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from stationary_world_arm_alignment.candidate_review import (
    CandidateReviewError,
    CandidateReviewService,
    ExternalReviewIdentityVerifier,
    canonical_sha256,
)
from stationary_world_arm_alignment.persistence import CalibrationStore


NOW_US = 1_800_000_000_000_000
SECRET = "review-test-secret-that-is-at-least-32-bytes"


def test_canonical_digest_matches_cross_language_precision_vector() -> None:
    value = {
        "z": 0.10911479224498183,
        "a": [-0.49381709129731677, 0.0, 1, True, "世界"],
    }

    assert canonical_sha256(value) == (
        "15b524dce44a65210d4047af8258aa6a24e1a39d03ed9d747226f48abaa5ac76"
    )


def test_canonical_digest_matches_problematic_transform_tokens() -> None:
    value = {
        "world_from_camera": {
            "translation_m": [
                -0.011727320462452727,
                0.0015374757156448454,
                -0.00846359167984511,
            ],
            "rotation_xyzw": [
                -0.008771022963335652,
                -0.20525675842452193,
                0.9786415876301645,
                -0.00730583588273101,
            ],
        },
    }

    assert canonical_sha256(value) == (
        "2e4412aebeb711d1661955413a91572446af6ac4a2ecd3807f19dd82ee5f82f9"
    )


def candidate_result(*, expires_at_us: int = NOW_US + 60_000_000) -> dict:
    candidate = {
        "schema": (
            "midbrain.skill.stationary_world_arm_alignment."
            "calibration_candidate"
        ),
        "schema_version": 2,
        "candidate_id": "alignment-1",
        "workcell_calibration_revision": "alignment-1",
        "created_at_us": NOW_US - 1_000_000,
        "expires_at_us": expires_at_us,
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "review_mode": "ENFORCED",
        "motion_usable": False,
        "method": {"skill_version": "0.7.0"},
        "frame_contract": {
            "world_frame": "world/stationary_camera/alignment-1",
            "vio_world_frame": "local_vio/epoch-1",
            "camera_frame": "femto_bolt_color_optical_frame",
            "arm_base_frame": "rebot_arm_base",
            "transform_semantics": "PARENT_FROM_CHILD",
        },
        "confidence": 0.9,
        "bounded_error_estimate": {"translation_m": 0.002},
        "camera_provenance": {
            "provider_id": "camera.femto_bolt",
            "provider_instance_id": "camera-instance",
            "boot_id": "camera-boot",
            "route_id": "orbbec.direct",
            "calibration_revision": "camera-calibration",
            "reference_timestamp_us": NOW_US - 2_000_000,
            "source_buffer_refs": {},
        },
        "vio_provenance": {
            "world_frame": "local_vio/epoch-1",
            "session_epoch": "epoch-1",
        },
        "transforms": {
            "world_from_camera": {
                "translation_m": [0.4, 0.5, 0.6],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "world_from_vio": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "world_from_base": {
                "translation_m": [0.1, 0.2, 0.3],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
        },
    }
    return {
        "schema": "midbrain.skill.stationary_world_arm_alignment.result",
        "schema_version": 3,
        "alignment_id": "alignment-1",
        "valid": True,
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "motion_usable": False,
        "candidate": candidate,
    }


def expected_provenance() -> dict:
    return {
        "workcell_calibration_revision": "alignment-1",
        "camera_provider_id": "camera.femto_bolt",
        "camera_provider_instance_id": "camera-instance",
        "camera_boot_id": "camera-boot",
        "camera_calibration_revision": "camera-calibration",
        "vio_session_epoch": "epoch-1",
    }


def identity() -> dict:
    return {
        "issuer": "test.identity",
        "reviewer_id": "operator@example.test",
        "assurance": "TEST_VERIFIED",
        "assertion_nonce": "nonce-1",
        "assertion_expires_at_us": NOW_US + 30_000_000,
    }


def request_for(result: dict, **overrides) -> dict:
    request = {
        "decision": "APPROVE",
        "candidate_sha256": canonical_sha256(result["candidate"]),
        "expected_provenance": expected_provenance(),
        "idempotency_key": "review-request-1",
        "rationale": "The visual and numeric evidence agree.",
    }
    request.update(overrides)
    return request


def service(tmp_path, result: dict) -> CandidateReviewService:
    store = CalibrationStore(tmp_path / "calibrations")
    store.save(result)
    return CandidateReviewService(
        store,
        tmp_path / "reviews",
        now_us=lambda: NOW_US,
    )


def signed_assertion(payload: dict) -> str:
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"{encode(body)}.{encode(signature)}"


def test_approval_is_append_only_and_never_activates_motion(tmp_path) -> None:
    result = candidate_result()
    reviewer = service(tmp_path, result)

    decision, created = reviewer.decide(
        "alignment-1",
        request_for(result),
        verified_identity=identity(),
    )

    assert created is True
    assert decision["decision_state"] == "APPROVED_FOR_ACTIVATION"
    assert decision["activation_state"] == "NOT_ACTIVATED"
    assert decision["motion_usable"] is False
    assert reviewer.calibrations.get("alignment-1") == result


def test_exact_idempotent_replay_returns_original_decision(tmp_path) -> None:
    result = candidate_result()
    reviewer = service(tmp_path, result)
    first, _ = reviewer.decide(
        "alignment-1",
        request_for(result),
        verified_identity=identity(),
    )
    second, created = reviewer.decide(
        "alignment-1",
        request_for(result),
        verified_identity=identity(),
    )
    assert created is False
    assert second == first


def test_idempotency_conflict_and_second_decision_are_rejected(tmp_path) -> None:
    result = candidate_result()
    reviewer = service(tmp_path, result)
    reviewer.decide(
        "alignment-1",
        request_for(result),
        verified_identity=identity(),
    )
    with pytest.raises(CandidateReviewError, match="idempotency key"):
        reviewer.decide(
            "alignment-1",
            request_for(result, decision="REJECT"),
            verified_identity=identity(),
        )
    with pytest.raises(CandidateReviewError, match="already has"):
        reviewer.decide(
            "alignment-1",
            request_for(result, idempotency_key="review-request-2"),
            verified_identity=identity(),
        )


@pytest.mark.parametrize(
    ("request_change", "expected_error"),
    [
        ({"candidate_sha256": "0" * 64}, "candidate changed"),
        (
            {
                "expected_provenance": {
                    **expected_provenance(),
                    "camera_boot_id": "different-boot",
                }
            },
            "provenance changed",
        ),
    ],
)
def test_review_rejects_digest_or_provenance_mismatch(
    tmp_path, request_change, expected_error
) -> None:
    result = candidate_result()
    reviewer = service(tmp_path, result)
    with pytest.raises(CandidateReviewError, match=expected_error):
        reviewer.decide(
            "alignment-1",
            request_for(result, **request_change),
            verified_identity=identity(),
        )


def test_expired_candidate_and_unverified_identity_are_rejected(tmp_path) -> None:
    expired = candidate_result(expires_at_us=NOW_US - 1)
    expired_reviewer = service(tmp_path / "expired", expired)
    with pytest.raises(CandidateReviewError, match="expired"):
        expired_reviewer.decide(
            "alignment-1",
            request_for(expired),
            verified_identity=identity(),
        )

    current = candidate_result()
    current_reviewer = service(tmp_path / "identity", current)
    with pytest.raises(CandidateReviewError, match="verified reviewer"):
        current_reviewer.decide(
            "alignment-1",
            request_for(current),
            verified_identity={},
        )


def test_candidate_view_exposes_decision_without_motion_usability(tmp_path) -> None:
    result = candidate_result()
    reviewer = service(tmp_path, result)
    before = reviewer.candidate_view(result)
    assert before["decision"] is None
    reviewer.decide(
        "alignment-1",
        request_for(result, decision="REJECT"),
        verified_identity=identity(),
    )
    after = reviewer.list_candidates()[0]
    assert after["decision"]["decision_state"] == "REJECTED"
    assert after["motion_usable"] is False


def test_external_identity_assertion_is_scoped_and_verified() -> None:
    payload = {
        "issuer": "manager.identity",
        "reviewer_id": "operator@example.test",
        "candidate_id": "alignment-1",
        "candidate_sha256": "a" * 64,
        "decision": "APPROVE",
        "issued_at_us": NOW_US - 1_000,
        "expires_at_us": NOW_US + 30_000_000,
        "nonce": "assertion-1",
    }
    verifier = ExternalReviewIdentityVerifier(
        SECRET,
        now_us=lambda: NOW_US,
    )
    verified = verifier.verify(
        signed_assertion(payload),
        candidate_id="alignment-1",
        candidate_sha256="a" * 64,
        decision="APPROVE",
    )
    assert verified["reviewer_id"] == "operator@example.test"
    with pytest.raises(CandidateReviewError, match="decision does not match"):
        verifier.verify(
            signed_assertion(payload),
            candidate_id="alignment-1",
            candidate_sha256="a" * 64,
            decision="REJECT",
        )


def test_external_identity_verifier_fails_closed_without_secret() -> None:
    verifier = ExternalReviewIdentityVerifier("", now_us=lambda: NOW_US)
    assert verifier.available is False
    with pytest.raises(CandidateReviewError, match="not configured"):
        verifier.verify(
            None,
            candidate_id="alignment-1",
            candidate_sha256="a" * 64,
            decision="APPROVE",
        )
