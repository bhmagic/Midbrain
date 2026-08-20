from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any, Protocol
import uuid

from locate_arm_base.profile import canonical_sha256


class WorkcellCalibrationManager(Protocol):
    async def activate_workcell_calibration(
        self, request: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def workcell_calibrations(self) -> dict[str, Any]: ...


def candidate_payload_sha256(candidate: dict[str, Any]) -> str:
    payload = dict(candidate)
    payload.pop("candidate_sha256", None)
    payload.pop("candidate_path", None)
    return canonical_sha256(payload)


class ArmBaseActivationService:
    """Create an auditable review and ask Manager to activate one exact candidate."""

    def __init__(
        self,
        manager: WorkcellCalibrationManager,
        *,
        review_auth_secret: str,
        candidate_root: Path,
        review_root: Path,
        reviewer_id: str = "local-operator:regular-agent-session",
    ) -> None:
        self.manager = manager
        self.review_auth_secret = str(review_auth_secret)
        self.candidate_root = candidate_root.resolve()
        self.review_root = review_root.resolve()
        self.reviewer_id = str(reviewer_id)

    def _candidate_path(self, candidate_id: str) -> Path:
        if not candidate_id or any(
            character not in "0123456789abcdefABCDEF-" for character in candidate_id
        ):
            raise ValueError("candidate_id must be a UUID-style identifier")
        path = (self.candidate_root / f"{candidate_id}.json").resolve()
        if path.parent != self.candidate_root:
            raise ValueError("candidate path escaped its configured root")
        return path

    def load_candidate(self, candidate_id: str) -> dict[str, Any]:
        path = self._candidate_path(candidate_id)
        if not path.is_file():
            raise RuntimeError(f"arm-base candidate {candidate_id!r} does not exist")
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict):
            raise RuntimeError("arm-base candidate is not a JSON object")
        if candidate.get("candidate_id") != candidate_id:
            raise RuntimeError("arm-base candidate identity does not match its file")
        actual = candidate_payload_sha256(candidate)
        if candidate.get("candidate_sha256") != actual:
            raise RuntimeError("arm-base candidate immutable hash is invalid")
        return candidate

    def latest_activation_continuation(self) -> dict[str, Any] | None:
        if not self.candidate_root.is_dir():
            return None
        paths = sorted(
            self.candidate_root.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in paths:
            try:
                candidate = self.load_candidate(path.stem)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                continue
            if (
                candidate.get("review_state") == "PENDING_REVIEW"
                and candidate.get("motion_usable") is False
                and int(candidate.get("expires_at_us") or 0) > time.time_ns() // 1000
            ):
                return {
                    "name": "review_and_activate_arm_base",
                    "arguments": {
                        "candidate_id": candidate["candidate_id"],
                        "candidate_sha256": candidate["candidate_sha256"],
                    },
                }
        return None

    async def review_and_activate(
        self,
        *,
        candidate_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        if len(self.review_auth_secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "MIDBRAIN_REVIEW_AUTH_SECRET must contain at least 32 bytes"
            )
        candidate = self.load_candidate(str(candidate_id or "").strip())
        expected_sha256 = str(candidate_sha256 or "").strip().lower()
        actual_sha256 = str(candidate["candidate_sha256"])
        if not hmac.compare_digest(expected_sha256, actual_sha256):
            return {
                "status": "CALIBRATION_CANDIDATE_DIGEST_REFRESH_REQUIRED",
                "workflow_complete": False,
                "motion_usable": False,
                "candidate_id": candidate["candidate_id"],
                "required_next_tool": {
                    "name": "review_and_activate_arm_base",
                    "arguments": {
                        "candidate_id": candidate["candidate_id"],
                        "candidate_sha256": actual_sha256,
                    },
                },
            }
        now_us = time.time_ns() // 1000
        if int(candidate.get("expires_at_us") or 0) <= now_us:
            return {
                "status": "FRESH_LOCALIZATION_REQUIRED",
                "workflow_complete": False,
                "motion_usable": False,
                "candidate_id": candidate["candidate_id"],
                "reason_code": "CANDIDATE_REVIEW_DEADLINE_EXPIRED",
            }
        active = await self._matching_active(actual_sha256)
        if active is not None:
            return self._activation_result(candidate, active, already_active=True)

        nonce = secrets.token_urlsafe(24)
        assertion_payload = {
            "issuer": "test_agent.local_review",
            "reviewer_id": self.reviewer_id,
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": actual_sha256,
            "decision": "APPROVE",
            "issued_at_us": now_us,
            "expires_at_us": now_us + 300_000_000,
            "nonce": nonce,
        }
        assertion_bytes = json.dumps(
            assertion_payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        signature = hmac.new(
            self.review_auth_secret.encode("utf-8"),
            assertion_bytes,
            hashlib.sha256,
        ).digest()
        assertion = (
            base64.urlsafe_b64encode(assertion_bytes).rstrip(b"=").decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )
        review = {
            "schema": "midbrain.skill.locate_arm_base.candidate_review_decision",
            "schema_version": 1,
            "decision_id": str(uuid.uuid4()),
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": actual_sha256,
            "decision": "APPROVE",
            "decision_state": "APPROVED_FOR_ACTIVATION",
            "activation_state": "NOT_ACTIVATED",
            "motion_usable": False,
            "decided_at_us": now_us,
            "reviewer": {
                "issuer": assertion_payload["issuer"],
                "reviewer_id": self.reviewer_id,
                "assertion_nonce": nonce,
            },
        }
        self._persist_review(review)
        activation = await self.manager.activate_workcell_calibration(
            {
                "request_id": f"activate-arm-base-{uuid.uuid4()}",
                "activated_by": self.reviewer_id,
                "candidate": candidate,
                "review_decision": review,
                "review_identity_assertion": assertion,
            }
        )
        if (
            activation.get("state") != "ACTIVE"
            or activation.get("motion_usable") is not True
            or activation.get("candidate_sha256") != actual_sha256
        ):
            raise RuntimeError("Manager did not activate the exact arm-base candidate")
        return self._activation_result(candidate, activation, already_active=False)

    async def _matching_active(self, candidate_sha256: str) -> dict[str, Any] | None:
        snapshot = await self.manager.workcell_calibrations()
        for activation in snapshot.get("activations") or []:
            if (
                isinstance(activation, dict)
                and activation.get("state") == "ACTIVE"
                and activation.get("motion_usable") is True
                and activation.get("candidate_sha256") == candidate_sha256
            ):
                return activation
        return None

    def _persist_review(self, review: dict[str, Any]) -> None:
        self.review_root.mkdir(parents=True, exist_ok=True)
        path = self.review_root / f"{review['decision_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(review, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _activation_result(
        candidate: dict[str, Any],
        activation: dict[str, Any],
        *,
        already_active: bool,
    ) -> dict[str, Any]:
        return {
            "status": "ACTIVE",
            "workflow_complete": True,
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": candidate["candidate_sha256"],
            "activation_id": activation.get("activation_id"),
            "calibration_revision": activation.get("calibration_revision"),
            "motion_usable": True,
            "already_active": already_active,
            "physical_motion_submitted": False,
        }
