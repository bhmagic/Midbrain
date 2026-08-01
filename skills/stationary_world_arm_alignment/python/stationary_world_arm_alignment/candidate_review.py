from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .persistence import CalibrationStore


class CandidateReviewError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_sha256(value: Any) -> str:
    """Hash a cross-language typed tree with normalized physical-number precision."""

    def normalized_float(number: float) -> str:
        if not math.isfinite(number):
            raise CandidateReviewError(
                "NONFINITE_CANONICAL_NUMBER",
                "canonical review values must contain only finite numbers",
            )
        token = repr(number).lower()
        sign = ""
        if token.startswith("-"):
            sign, token = "-", token[1:]
        mantissa, separator, exponent_text = token.partition("e")
        exponent = int(exponent_text) if separator else 0
        whole, dot, fraction = mantissa.partition(".")
        digits = whole + (fraction if dot else "")
        exponent -= len(fraction) if dot else 0
        digits = digits.lstrip("0")
        if not digits:
            return "0e+0"
        trailing_zero_count = len(digits) - len(digits.rstrip("0"))
        if trailing_zero_count:
            digits = digits[:-trailing_zero_count]
            exponent += trailing_zero_count
        return f"{sign}{digits}e{exponent:+d}"

    def typed_tree(item: Any) -> Any:
        if item is None:
            return ["null"]
        if isinstance(item, bool):
            return ["boolean", "1" if item else "0"]
        if isinstance(item, int):
            return ["integer", str(item)]
        if isinstance(item, float):
            return ["decimal", normalized_float(item)]
        if isinstance(item, str):
            return ["utf8", item.encode("utf-8").hex()]
        if isinstance(item, (list, tuple)):
            return ["array", [typed_tree(element) for element in item]]
        if isinstance(item, dict):
            entries = sorted(
                (
                    str(key).encode("utf-8"),
                    typed_tree(element),
                )
                for key, element in item.items()
            )
            return [
                "object",
                [[key.hex(), element] for key, element in entries],
            ]
        raise CandidateReviewError(
            "UNSUPPORTED_CANONICAL_VALUE",
            f"unsupported canonical review value: {type(item).__name__}",
        )

    payload = json.dumps(
        typed_tree(value),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _clean_identifier(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z._:@-]{1,160}", cleaned):
        raise CandidateReviewError(
            "INVALID_IDENTIFIER",
            f"{field} must contain only safe identifier characters",
        )
    return cleaned


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as error:
        raise CandidateReviewError(
            "INVALID_IDENTITY_ASSERTION",
            "review identity assertion is not valid base64url",
        ) from error


class ExternalReviewIdentityVerifier:
    """Verify a decision-scoped assertion issued by an external identity service."""

    def __init__(
        self,
        secret: str | None = None,
        *,
        now_us: Callable[[], int] | None = None,
    ):
        self._secret = (
            secret
            if secret is not None
            else os.environ.get("MIDBRAIN_REVIEW_AUTH_SECRET", "")
        ).encode("utf-8")
        self._now_us = now_us or (lambda: time.time_ns() // 1000)

    @property
    def available(self) -> bool:
        return len(self._secret) >= 32

    def verify(
        self,
        assertion: str | None,
        *,
        candidate_id: str,
        candidate_sha256: str,
        decision: str,
    ) -> dict[str, Any]:
        if not self.available:
            raise CandidateReviewError(
                "IDENTITY_SERVICE_UNAVAILABLE",
                "external review identity verification is not configured",
            )
        if not assertion or "." not in assertion:
            raise CandidateReviewError(
                "IDENTITY_ASSERTION_REQUIRED",
                "a decision-scoped external identity assertion is required",
            )
        payload_token, signature_token = assertion.split(".", 1)
        payload_bytes = _b64url_decode(payload_token)
        signature = _b64url_decode(signature_token)
        expected = hmac.new(self._secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise CandidateReviewError(
                "INVALID_IDENTITY_ASSERTION",
                "review identity assertion signature is invalid",
            )
        try:
            payload = json.loads(payload_bytes)
        except Exception as error:
            raise CandidateReviewError(
                "INVALID_IDENTITY_ASSERTION",
                "review identity assertion payload is not valid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise CandidateReviewError(
                "INVALID_IDENTITY_ASSERTION",
                "review identity assertion payload must be an object",
            )
        required = {
            "issuer",
            "reviewer_id",
            "candidate_id",
            "candidate_sha256",
            "decision",
            "issued_at_us",
            "expires_at_us",
            "nonce",
        }
        missing = sorted(required.difference(payload))
        if missing:
            raise CandidateReviewError(
                "INVALID_IDENTITY_ASSERTION",
                f"review identity assertion is missing: {', '.join(missing)}",
            )
        now_us = self._now_us()
        if int(payload["issued_at_us"]) > now_us + 5_000_000:
            raise CandidateReviewError(
                "INVALID_IDENTITY_ASSERTION",
                "review identity assertion was issued in the future",
            )
        if int(payload["expires_at_us"]) < now_us:
            raise CandidateReviewError(
                "EXPIRED_IDENTITY_ASSERTION",
                "review identity assertion has expired",
            )
        expected_fields = {
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha256,
            "decision": decision,
        }
        for field, expected_value in expected_fields.items():
            if str(payload.get(field)) != expected_value:
                raise CandidateReviewError(
                    "IDENTITY_SCOPE_MISMATCH",
                    f"review identity assertion {field} does not match the decision",
                )
        return {
            "issuer": _clean_identifier(str(payload["issuer"]), "issuer"),
            "reviewer_id": _clean_identifier(
                str(payload["reviewer_id"]), "reviewer_id"
            ),
            "assurance": str(payload.get("assurance") or "EXTERNAL_VERIFIED"),
            "assertion_nonce": _clean_identifier(str(payload["nonce"]), "nonce"),
            "assertion_expires_at_us": int(payload["expires_at_us"]),
        }


class CandidateReviewService:
    """Create append-only review decisions without activating a transform."""

    def __init__(
        self,
        calibrations: CalibrationStore,
        review_root: Path,
        *,
        now_us: Callable[[], int] | None = None,
    ):
        self.calibrations = calibrations
        self.review_root = review_root
        self.review_root.mkdir(parents=True, exist_ok=True)
        self._now_us = now_us or (lambda: time.time_ns() // 1000)
        self._lock = threading.Lock()

    def candidate_view(self, result: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate_from_result(result)
        candidate_id = str(candidate["candidate_id"])
        decision = self.decision_for(candidate_id)
        now_us = self._now_us()
        return {
            "alignment_id": str(result["alignment_id"]),
            "candidate": candidate,
            "candidate_sha256": canonical_sha256(candidate),
            "expired": int(candidate["expires_at_us"]) < now_us,
            "decision": decision,
            "activation_state": "NOT_ACTIVATED",
            "motion_usable": False,
        }

    def list_candidates(self) -> list[dict[str, Any]]:
        output = []
        for result in self.calibrations.list():
            try:
                output.append(self.candidate_view(result))
            except CandidateReviewError:
                continue
        return output

    def decide(
        self,
        alignment_id: str,
        request: dict[str, Any],
        *,
        verified_identity: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        result = self.calibrations.get(alignment_id)
        if result is None:
            raise CandidateReviewError(
                "CANDIDATE_NOT_FOUND",
                "calibration candidate does not exist",
            )
        candidate = self._candidate_from_result(result)
        candidate_id = _clean_identifier(
            str(candidate["candidate_id"]), "candidate_id"
        )
        decision = str(request.get("decision") or "").strip().upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise CandidateReviewError(
                "INVALID_DECISION",
                "decision must be APPROVE or REJECT",
            )
        idempotency_key = _clean_identifier(
            str(request.get("idempotency_key") or ""), "idempotency_key"
        )
        supplied_digest = str(request.get("candidate_sha256") or "").lower()
        actual_digest = canonical_sha256(candidate)
        if not hmac.compare_digest(supplied_digest, actual_digest):
            raise CandidateReviewError(
                "CANDIDATE_DIGEST_MISMATCH",
                "candidate changed after the review surface was loaded",
            )
        now_us = self._now_us()
        if int(candidate["expires_at_us"]) < now_us:
            raise CandidateReviewError(
                "CANDIDATE_EXPIRED",
                "calibration candidate has expired",
            )
        expected = request.get("expected_provenance")
        self._verify_provenance(candidate, expected)
        identity = self._verified_identity(verified_identity)
        intent = {
            "alignment_id": alignment_id,
            "candidate_id": candidate_id,
            "candidate_sha256": actual_digest,
            "decision": decision,
            "idempotency_key": idempotency_key,
            "reviewer_id": identity["reviewer_id"],
            "expected_provenance": expected,
            "rationale": str(request.get("rationale") or "").strip(),
        }
        intent_sha256 = canonical_sha256(intent)
        with self._lock:
            existing_by_key = self._decision_by_idempotency_key(idempotency_key)
            if existing_by_key is not None:
                if existing_by_key.get("intent_sha256") != intent_sha256:
                    raise CandidateReviewError(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency key was already used for a different review",
                    )
                return existing_by_key, False
            existing = self.decision_for(candidate_id)
            if existing is not None:
                raise CandidateReviewError(
                    "CANDIDATE_ALREADY_REVIEWED",
                    "calibration candidate already has a final review decision",
                )
            record = {
                "schema": (
                    "midbrain.skill.stationary_world_arm_alignment."
                    "candidate_review_decision"
                ),
                "schema_version": 1,
                "decision_id": str(uuid.uuid4()),
                "alignment_id": alignment_id,
                "candidate_id": candidate_id,
                "candidate_sha256": actual_digest,
                "decision": decision,
                "decision_state": (
                    "APPROVED_FOR_ACTIVATION"
                    if decision == "APPROVE"
                    else "REJECTED"
                ),
                "activation_state": "NOT_ACTIVATED",
                "motion_usable": False,
                "decided_at_us": now_us,
                "candidate_expires_at_us": int(candidate["expires_at_us"]),
                "reviewer": identity,
                "idempotency_key": idempotency_key,
                "intent_sha256": intent_sha256,
                "expected_provenance": expected,
                "rationale": intent["rationale"],
            }
            self._atomic_json(self.review_root / f"{candidate_id}.json", record)
            self._atomic_json(
                self.review_root / f"idempotency-{idempotency_key}.json",
                record,
            )
            return record, True

    def decision_for(self, candidate_id: str) -> dict[str, Any] | None:
        candidate_id = _clean_identifier(candidate_id, "candidate_id")
        return self._read_json(self.review_root / f"{candidate_id}.json")

    def _decision_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self._read_json(
            self.review_root / f"idempotency-{idempotency_key}.json"
        )

    @staticmethod
    def _candidate_from_result(result: dict[str, Any]) -> dict[str, Any]:
        candidate = result.get("candidate")
        if not isinstance(candidate, dict):
            raise CandidateReviewError(
                "NOT_A_REVIEW_CANDIDATE",
                "calibration result does not contain a candidate",
            )
        if (
            result.get("review_state") != "CANDIDATE_REVIEW_REQUIRED"
            or result.get("motion_usable") is not False
            or candidate.get("review_state") != "CANDIDATE_REVIEW_REQUIRED"
            or candidate.get("motion_usable") is not False
        ):
            raise CandidateReviewError(
                "INVALID_CANDIDATE_STATE",
                "candidate is not in the immutable review-required state",
            )
        frame_contract = candidate.get("frame_contract")
        if (
            candidate.get("schema_version") != 3
            or not isinstance(frame_contract, dict)
            or frame_contract.get("convention_id")
            != "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
            or frame_contract.get("camera_optical_convention_id")
            != "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            or frame_contract.get("legacy_candidate_compatibility")
            != "REJECT"
        ):
            raise CandidateReviewError(
                "LEGACY_SPATIAL_CONVENTION",
                "candidate predates the Z-up spatial convention and must be regenerated",
            )
        if result.get("valid") is not True:
            raise CandidateReviewError(
                "INVALID_CANDIDATE",
                "calibration candidate is not valid",
            )
        return candidate

    @staticmethod
    def _verify_provenance(
        candidate: dict[str, Any], expected: Any
    ) -> None:
        if not isinstance(expected, dict):
            raise CandidateReviewError(
                "EXPECTED_PROVENANCE_REQUIRED",
                "the reviewed provenance snapshot is required",
            )
        camera = candidate.get("camera_provenance") or {}
        vio = candidate.get("vio_provenance") or {}
        actual = {
            "workcell_calibration_revision": candidate.get(
                "workcell_calibration_revision"
            ),
            "camera_provider_id": camera.get("provider_id"),
            "camera_provider_instance_id": camera.get("provider_instance_id"),
            "camera_boot_id": camera.get("boot_id"),
            "camera_calibration_revision": camera.get("calibration_revision"),
            "vio_session_epoch": vio.get("session_epoch"),
        }
        if expected != actual:
            raise CandidateReviewError(
                "PROVENANCE_MISMATCH",
                "candidate provenance changed or the reviewed snapshot is incomplete",
            )

    @staticmethod
    def _verified_identity(value: dict[str, Any]) -> dict[str, Any]:
        required = {
            "issuer",
            "reviewer_id",
            "assertion_nonce",
            "assertion_expires_at_us",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise CandidateReviewError(
                "VERIFIED_IDENTITY_REQUIRED",
                "a verified reviewer identity is required",
            )
        return {
            "issuer": _clean_identifier(str(value.get("issuer") or ""), "issuer"),
            "reviewer_id": _clean_identifier(
                str(value.get("reviewer_id") or ""), "reviewer_id"
            ),
            "assurance": str(value.get("assurance") or "EXTERNAL_VERIFIED"),
            "assertion_nonce": _clean_identifier(
                str(value.get("assertion_nonce") or ""), "assertion_nonce"
            ),
            "assertion_expires_at_us": int(value["assertion_expires_at_us"]),
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
