from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from .candidate_review import canonical_sha256


def candidate_expected_provenance(candidate: dict[str, Any]) -> dict[str, Any]:
    camera = candidate.get("camera_provenance") or {}
    vio = candidate.get("vio_provenance") or {}
    return {
        "workcell_calibration_revision": candidate.get(
            "workcell_calibration_revision"
        ),
        "camera_provider_id": camera.get("provider_id"),
        "camera_provider_instance_id": camera.get("provider_instance_id"),
        "camera_boot_id": camera.get("boot_id"),
        "camera_calibration_revision": camera.get("calibration_revision"),
        "vio_session_epoch": vio.get("session_epoch"),
    }


def issue_review_identity_assertion(
    *,
    secret: str,
    candidate: dict[str, Any],
    reviewer_id: str,
    issuer: str = "midbrain.local.development",
    decision: str = "APPROVE",
    nonce: str | None = None,
    lifetime_s: float = 120.0,
) -> tuple[str, dict[str, Any]]:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("MIDBRAIN_REVIEW_AUTH_SECRET must contain 32 bytes")
    if not 1.0 <= float(lifetime_s) <= 300.0:
        raise ValueError("identity assertion lifetime must be 1 to 300 seconds")
    candidate_id = str(candidate.get("candidate_id") or "")
    if not candidate_id:
        raise ValueError("candidate_id is required")
    digest = canonical_sha256(candidate)
    now_us = time.time_ns() // 1000
    payload = {
        "issuer": str(issuer),
        "reviewer_id": str(reviewer_id),
        "candidate_id": candidate_id,
        "candidate_sha256": digest,
        "decision": str(decision).upper(),
        "issued_at_us": now_us,
        "expires_at_us": now_us + int(float(lifetime_s) * 1_000_000),
        "nonce": str(nonce or uuid.uuid4()),
        "assurance": "LOCAL_DEVELOPMENT_USER_AUTHORIZED",
    }
    payload_bytes = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).digest()

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    return f"{encode(payload_bytes)}.{encode(signature)}", payload


def build_candidate_review_request(
    candidate: dict[str, Any],
    *,
    idempotency_key: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "decision": "APPROVE",
        "candidate_sha256": canonical_sha256(candidate),
        "expected_provenance": candidate_expected_provenance(candidate),
        "idempotency_key": str(idempotency_key),
        "rationale": str(rationale),
    }


def build_activation_request(
    candidate: dict[str, Any],
    review_decision: dict[str, Any],
    review_identity_assertion: str,
    *,
    request_id: str,
    activated_by: str,
    duration_ms: int,
) -> dict[str, Any]:
    if not 1_000 <= int(duration_ms) <= 300_000:
        raise ValueError("activation duration must be 1000 to 300000 ms")
    return {
        "request_id": str(request_id),
        "activated_by": str(activated_by),
        "candidate": candidate,
        "review_decision": review_decision,
        "review_identity_assertion": review_identity_assertion,
        "duration_ms": int(duration_ms),
    }
