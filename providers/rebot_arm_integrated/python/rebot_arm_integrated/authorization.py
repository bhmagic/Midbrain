from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class AuthorizationAssertionError(PermissionError):
    """A signed physical-action assertion is absent, invalid, or stale."""


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as error:
        raise AuthorizationAssertionError(
            "authorization assertion is not valid base64url"
        ) from error


def verify_transit_execution_assertion(
    assertion: str,
    secret: str,
    *,
    provider_id: str,
    provider_instance_id: str,
    boot_id: str,
    configuration_sha256: str,
    plan_id: str,
    request_sha256: str,
    preview_sha256: str,
    scene_revision: str | None,
    preview_expires_at_us: int,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Verify one UI-issued assertion against one exact controller preview."""

    if len(secret.encode("utf-8")) < 32:
        raise AuthorizationAssertionError(
            "MIDBRAIN_AUTHORIZATION_SECRET must contain at least 32 bytes"
        )
    token = str(assertion or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        raise AuthorizationAssertionError(
            "authorization assertion must contain payload and signature"
        )
    payload_segment, signature_segment = parts
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    supplied_signature = _decode_base64url(signature_segment)
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise AuthorizationAssertionError(
            "authorization assertion signature is invalid"
        )
    try:
        payload = json.loads(_decode_base64url(payload_segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizationAssertionError(
            "authorization assertion payload is not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise AuthorizationAssertionError(
            "authorization assertion payload must be an object"
        )

    expected_text = {
        "schema": "physical_agent.authorization_execution_assertion",
        "issuer": "physical-agent-ui",
        "audience": provider_id,
        "action": "EXECUTE_TRANSIT_PATH",
        "resolution": "APPROVED",
        "controller_provider_id": provider_id,
        "controller_provider_instance_id": provider_instance_id,
        "controller_boot_id": boot_id,
        "controller_configuration_sha256": configuration_sha256,
        "plan_id": plan_id,
        "request_sha256": request_sha256,
        "preview_sha256": preview_sha256,
    }
    for field, expected in expected_text.items():
        if payload.get(field) != expected:
            raise AuthorizationAssertionError(
                f"authorization assertion does not match {field}"
            )
    if payload.get("schema_version") != 1:
        raise AuthorizationAssertionError(
            "authorization assertion schema version is unsupported"
        )
    if payload.get("decision_type") != "PHYSICAL_OBSERVATION_POSE":
        raise AuthorizationAssertionError(
            "authorization assertion decision type is not valid for transit"
        )
    assertion_id = str(payload.get("assertion_id") or "").strip()
    decision_id = str(payload.get("decision_id") or "").strip()
    resolved_by = str(payload.get("resolved_by") or "").strip()
    if not assertion_id or not decision_id or not resolved_by:
        raise AuthorizationAssertionError(
            "authorization assertion identity is incomplete"
        )
    claimed_scene_revision = payload.get("scene_revision")
    if claimed_scene_revision != scene_revision:
        raise AuthorizationAssertionError(
            "authorization assertion scene revision is stale"
        )

    now = time.time_ns() // 1000 if now_us is None else int(now_us)
    try:
        issued_at_us = int(payload["issued_at_us"])
        expires_at_us = int(payload["expires_at_us"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuthorizationAssertionError(
            "authorization assertion time bounds are invalid"
        ) from error
    if issued_at_us <= 0 or expires_at_us <= issued_at_us:
        raise AuthorizationAssertionError(
            "authorization assertion time range is invalid"
        )
    if issued_at_us > now + 1_000_000:
        raise AuthorizationAssertionError(
            "authorization assertion was issued in the future"
        )
    if expires_at_us <= now:
        raise AuthorizationAssertionError(
            "authorization assertion has expired"
        )
    if expires_at_us > int(preview_expires_at_us):
        raise AuthorizationAssertionError(
            "authorization assertion outlives its controller preview"
        )
    return payload
