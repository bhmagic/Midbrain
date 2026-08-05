from __future__ import annotations

import hmac
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from stationary_world_arm_alignment.candidate_review import (
    CandidateReviewError,
    CandidateReviewService,
    ExternalReviewIdentityVerifier,
    canonical_sha256,
)
from stationary_world_arm_alignment.local_review_activation import (
    build_activation_request,
    build_candidate_review_request,
    issue_review_identity_assertion,
)
from stationary_world_arm_alignment.persistence import CalibrationStore


class WorkcellCalibrationManager(Protocol):
    async def workcell_calibrations(self) -> dict[str, Any]: ...

    async def activate_workcell_calibration(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]: ...


def _has_motion_usable_semantic_quality(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("status") not in {
        "PASSED",
        "PASSED_WITH_WARNINGS",
    }:
        return False
    try:
        base_x_relation_to_gripper = str(
            value["base_x_relation_to_gripper"]
        )
        selected_yaw_flip_deg = int(value["selected_base_yaw_flip_deg"])
        fitted_yaw_deg = float(value["fitted_base_yaw_deg"])
        yaw_translation_norm_m = float(
            value["yaw_correction_translation_norm_m"]
        )
        world_up_available = value["world_up_available"] is True
        raw_z_dot_up = float(value["raw_base_z_dot_world_up"])
        corrected_z_dot_up = float(
            value["corrected_base_z_dot_world_up"]
        )
        upright_flip_required = (
            value["upright_hemisphere_flip_required"] is True
        )
        correction_axis = str(
            value["selected_orientation_correction_axis"]
        )
        correction_deg = int(
            value["selected_orientation_correction_deg"]
        )
        correction_count = int(value["orientation_correction_count"])
        orientation_translation_norm_m = float(
            value["orientation_correction_translation_norm_m"]
        )
        mesh_translation_norm_m = float(
            value["mesh_hypothesis_correction_translation_norm_m"]
        )
        root_adjustment_norm_m = float(
            value["semantic_root_translation_adjustment_norm_m"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    expected_yaw_flip_deg = {
        "TOWARD_GRIPPER": 0,
        "AWAY_FROM_GRIPPER": 180,
        "UNCLEAR": 0,
    }.get(base_x_relation_to_gripper)
    expected_upright_flip = raw_z_dot_up < 0.0
    expected_x_flip = base_x_relation_to_gripper == "AWAY_FROM_GRIPPER"
    expected_axis = {
        (False, False): "NONE",
        (False, True): "Z",
        (True, False): "X",
        (True, True): "Y",
    }[(expected_upright_flip, expected_x_flip)]
    expected_count = 0 if expected_axis == "NONE" else 1
    expected_deg = 0 if expected_count == 0 else 180
    return (
        expected_yaw_flip_deg == selected_yaw_flip_deg
        and math.isfinite(fitted_yaw_deg)
        and abs(fitted_yaw_deg - selected_yaw_flip_deg) <= 1e-9
        and math.isfinite(yaw_translation_norm_m)
        and abs(yaw_translation_norm_m) <= 1e-9
        and world_up_available
        and math.isfinite(raw_z_dot_up)
        and math.isfinite(corrected_z_dot_up)
        and corrected_z_dot_up >= -1e-9
        and upright_flip_required == expected_upright_flip
        and correction_axis == expected_axis
        and correction_deg == expected_deg
        and correction_count == expected_count
        and math.isfinite(orientation_translation_norm_m)
        and abs(orientation_translation_norm_m) <= 1e-9
        and value.get("orientation_application_origin")
        == "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
        and value.get("orientation_application_order")
        == (
            "parent_from_mesh @ mesh_hypothesis_correction @ "
            "mesh_from_semantic"
        )
        and math.isfinite(mesh_translation_norm_m)
        and abs(mesh_translation_norm_m) <= 1e-9
        and value.get("mesh_center_translation_preserved") is True
        and math.isfinite(root_adjustment_norm_m)
        and root_adjustment_norm_m >= 0.0
    )


def _has_single_orientation_provenance(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        field in value
        for field in (
            "world_up_available",
            "raw_base_z_dot_world_up",
            "corrected_base_z_dot_world_up",
            "upright_hemisphere_flip_required",
            "selected_orientation_correction_axis",
            "selected_orientation_correction_deg",
            "orientation_correction_count",
            "orientation_correction_translation_norm_m",
            "orientation_application_origin",
            "orientation_application_order",
            "mesh_hypothesis_correction_translation_norm_m",
            "mesh_center_translation_preserved",
            "semantic_root_translation_adjustment_norm_m",
        )
    )


def _has_exact_vio_provenance(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    vio = candidate.get("vio_provenance")
    if not isinstance(vio, dict):
        return False
    return all(
        isinstance(vio.get(field), str) and bool(vio[field].strip())
        for field in (
            "provider_id",
            "provider_instance_id",
            "boot_id",
            "world_frame",
            "session_epoch",
        )
    ) and isinstance(vio.get("reference_timestamp_us"), int)


def _has_documented_base_z_up(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    try:
        semantic = candidate["quality_provenance"]["semantic_alignment"]
        reviewed_dot = float(semantic["corrected_base_z_dot_world_up"])
        quaternion = [
            float(item)
            for item in candidate["transforms"]["world_from_base"][
                "rotation_xyzw"
            ]
        ]
    except (KeyError, TypeError, ValueError):
        return False
    if len(quaternion) != 4 or not all(
        math.isfinite(item) for item in quaternion
    ):
        return False
    norm_squared = sum(item * item for item in quaternion)
    if norm_squared <= 1e-18 or not math.isfinite(reviewed_dot):
        return False
    x, y, _, _ = quaternion
    documented_dot = 1.0 - 2.0 * (x * x + y * y) / norm_squared
    return (
        documented_dot >= -1e-9
        and abs(documented_dot - reviewed_dot) <= 1e-6
    )


class StationaryCalibrationActivationService:
    """Review and activate one mounted, identity-gated calibration candidate."""

    def __init__(
        self,
        manager: WorkcellCalibrationManager,
        *,
        review_auth_secret: str,
        calibration_root: Path,
        review_root: Path,
        reviewer_id: str = "local-operator:regular-agent-session",
    ) -> None:
        self.manager = manager
        self.review_auth_secret = str(review_auth_secret)
        self.store = CalibrationStore(calibration_root)
        self.reviews = CandidateReviewService(self.store, review_root)
        self.reviewer_id = str(reviewer_id)

    def latest_activation_continuation(self) -> dict[str, Any] | None:
        result = self.store.latest()
        if not isinstance(result, dict):
            return None
        candidate = result.get("candidate")
        alignment_id = str(result.get("alignment_id") or "")
        semantic_alignment = (
            (candidate.get("quality_provenance") or {}).get(
                "semantic_alignment"
            )
            if isinstance(candidate, dict)
            else None
        )
        if (
            not isinstance(candidate, dict)
            or not alignment_id
            or result.get("review_state") != "CANDIDATE_REVIEW_REQUIRED"
            or result.get("motion_usable") is not False
            or not _has_motion_usable_semantic_quality(semantic_alignment)
            or not _has_exact_vio_provenance(candidate)
            or not _has_documented_base_z_up(candidate)
            or int(candidate.get("expires_at_us") or 0)
            <= time.time_ns() // 1000
        ):
            return None
        return {
            "name": "review_and_activate_stationary_calibration",
            "arguments": {
                "alignment_id": alignment_id,
                "candidate_sha256": canonical_sha256(candidate),
            },
        }

    async def review_and_activate(
        self,
        *,
        alignment_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        normalized_alignment_id = str(alignment_id or "").strip()
        normalized_digest = str(candidate_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9A-Za-z-]+", normalized_alignment_id):
            raise ValueError("alignment_id has an invalid format")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_digest):
            raise ValueError("candidate_sha256 must be 64 lowercase hex digits")
        if len(self.review_auth_secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "MIDBRAIN_REVIEW_AUTH_SECRET is not configured with at "
                "least 32 bytes"
            )

        result = self.store.get(normalized_alignment_id)
        if result is None:
            raise RuntimeError(
                f"calibration {normalized_alignment_id!r} does not exist"
            )
        candidate = result.get("candidate")
        if not isinstance(candidate, dict):
            raise RuntimeError(
                "calibration result does not contain a review candidate"
            )
        actual_digest = canonical_sha256(candidate)
        if not hmac.compare_digest(normalized_digest, actual_digest):
            return {
                "status": "CALIBRATION_CANDIDATE_DIGEST_REFRESH_REQUIRED",
                "workflow_complete": False,
                "motion_usable": False,
                "alignment_id": normalized_alignment_id,
                "reason_code": "CANDIDATE_DIGEST_MISMATCH",
                "message": (
                    "The supplied candidate digest did not match the current "
                    "persisted calibration. No activation was attempted. "
                    "Call the exact required_next_tool below now; do not ask "
                    "the user to repeat the axis-establishment request."
                ),
                "required_next_tool": {
                    "name": "review_and_activate_stationary_calibration",
                    "arguments": {
                        "alignment_id": normalized_alignment_id,
                        "candidate_sha256": actual_digest,
                    },
                },
                "physical_motion_submitted": False,
            }
        now_us = time.time_ns() // 1000
        if int(candidate.get("expires_at_us") or 0) <= now_us:
            return self._fresh_calibration_required(
                normalized_alignment_id,
                "CANDIDATE_EXPIRED",
                "The persisted calibration candidate has expired.",
            )
        if not _has_exact_vio_provenance(candidate):
            return self._fresh_calibration_required(
                normalized_alignment_id,
                "CANDIDATE_PROVENANCE_SUPERSEDED",
                (
                    "The persisted calibration candidate predates exact VIO "
                    "Provider identity capture."
                ),
            )
        semantic_alignment = (
            candidate.get("quality_provenance") or {}
        ).get("semantic_alignment")
        if not _has_single_orientation_provenance(semantic_alignment):
            return self._fresh_calibration_required(
                normalized_alignment_id,
                "CANDIDATE_ORIENTATION_SUPERSEDED",
                (
                    "The persisted calibration candidate predates the "
                    "single mesh-centered base-orientation proof."
                ),
            )
        if not _has_motion_usable_semantic_quality(semantic_alignment):
            raise RuntimeError(
                "the calibration candidate lacks an exact reviewed "
                "base-orientation decision"
            )
        if not _has_documented_base_z_up(candidate):
            return self._fresh_calibration_required(
                normalized_alignment_id,
                "CANDIDATE_DOCUMENTED_TRANSFORM_INVALID",
                (
                    "The documented world-from-base transform does not "
                    "carry the reviewed upward base +Z direction."
                ),
            )

        active = await self._matching_active_activation(actual_digest)
        if active is not None:
            return self._result(
                normalized_alignment_id,
                active,
                review_created=False,
                review_decision_id=str(
                    active.get("review_decision_id") or ""
                ),
                already_active=True,
            )

        candidate_id = str(candidate.get("candidate_id") or "")
        existing_review = self.reviews.decision_for(candidate_id)
        if existing_review is not None:
            if (
                existing_review.get("decision") != "APPROVE"
                or existing_review.get("decision_state")
                != "APPROVED_FOR_ACTIVATION"
                or existing_review.get("candidate_sha256") != actual_digest
            ):
                raise CandidateReviewError(
                    "CANDIDATE_ALREADY_REVIEWED",
                    "the calibration candidate already has a final review "
                    "that is not an exact approval",
                )
            reviewer = existing_review.get("reviewer") or {}
            reviewer_id = str(reviewer.get("reviewer_id") or "")
            issuer = str(reviewer.get("issuer") or "")
            nonce = str(reviewer.get("assertion_nonce") or "")
            if not reviewer_id or not issuer or not nonce:
                raise RuntimeError(
                    "the existing review has incomplete reviewer identity"
                )
            assertion, _ = issue_review_identity_assertion(
                secret=self.review_auth_secret,
                candidate=candidate,
                reviewer_id=reviewer_id,
                issuer=issuer,
                nonce=nonce,
            )
            review_decision = existing_review
            review_created = False
        else:
            assertion, identity_payload = issue_review_identity_assertion(
                secret=self.review_auth_secret,
                candidate=candidate,
                reviewer_id=self.reviewer_id,
            )
            verified_identity = ExternalReviewIdentityVerifier(
                self.review_auth_secret
            ).verify(
                assertion,
                candidate_id=candidate_id,
                candidate_sha256=str(identity_payload["candidate_sha256"]),
                decision="APPROVE",
            )
            review_decision, review_created = self.reviews.decide(
                normalized_alignment_id,
                build_candidate_review_request(
                    candidate,
                    idempotency_key=f"agent-review-{uuid.uuid4()}",
                    rationale=(
                        "The local operator authorized review and activation "
                        "of this exact qualified mounted world-to-arm "
                        "candidate. Manager must independently revalidate "
                        "quality, provenance, provider identity, VIO epoch, "
                        "and tracking state."
                    ),
                ),
                verified_identity=verified_identity,
            )

        activation = await self.manager.activate_workcell_calibration(
            build_activation_request(
                candidate,
                review_decision,
                assertion,
                request_id=f"agent-activate-{uuid.uuid4()}",
                activated_by=self.reviewer_id,
            )
        )
        if (
            activation.get("state") != "ACTIVE"
            or activation.get("motion_usable") is not True
            or activation.get("candidate_sha256") != actual_digest
        ):
            raise RuntimeError(
                "Manager did not return an exact motion-usable ACTIVE "
                "calibration"
            )
        return self._result(
            normalized_alignment_id,
            activation,
            review_created=review_created,
            review_decision_id=str(review_decision["decision_id"]),
            already_active=False,
        )

    async def _matching_active_activation(
        self,
        candidate_sha256: str,
    ) -> dict[str, Any] | None:
        snapshot = await self.manager.workcell_calibrations()
        activations = snapshot.get("activations") or []
        for activation in activations:
            if (
                isinstance(activation, dict)
                and activation.get("state") == "ACTIVE"
                and activation.get("motion_usable") is True
                and activation.get("candidate_sha256") == candidate_sha256
                and activation.get("expires_at_us") is None
                and activation.get("validity_policy")
                == "MOUNTED_IDENTITY_TRACKING_GATED_V1"
            ):
                return activation
        return None

    @staticmethod
    def _fresh_calibration_required(
        alignment_id: str,
        reason_code: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "status": "FRESH_CALIBRATION_REQUIRED",
            "workflow_complete": False,
            "motion_usable": False,
            "alignment_id": alignment_id,
            "reason_code": reason_code,
            "message": (
                f"{message} Do not retry activation for this alignment. "
                "If the current user request is to establish the world-to-arm "
                "relationship, run calibrate_stationary_workcell now and use "
                "only its newly returned required_next_tool."
            ),
            "physical_motion_submitted": False,
        }

    @staticmethod
    def _result(
        alignment_id: str,
        activation: dict[str, Any],
        *,
        review_created: bool,
        review_decision_id: str,
        already_active: bool,
    ) -> dict[str, Any]:
        return {
            "status": "ACTIVE",
            "workflow_complete": True,
            "alignment_id": alignment_id,
            "candidate_sha256": activation.get("candidate_sha256"),
            "review_created": review_created,
            "review_decision_id": review_decision_id,
            "activation_id": activation.get("activation_id"),
            "expires_at_us": activation.get("expires_at_us"),
            "validity_policy": activation.get("validity_policy"),
            "invalidation_conditions": activation.get(
                "invalidation_conditions"
            ),
            "motion_usable": True,
            "already_active": already_active,
            "physical_motion_submitted": False,
            "message": (
                "The exact stationary world-to-arm calibration is reviewed, "
                "Manager-validated, and motion-usable without a wall-clock "
                "expiry while mounted-rig identity and tracking evidence "
                "remain current."
            ),
        }
