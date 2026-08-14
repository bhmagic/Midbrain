from __future__ import annotations

from typing import Any
import base64
import hashlib
import hmac
import json
import time


class AuthorizationError(PermissionError):
    """A Contact Skill assertion is absent, invalid, stale, or mismatched."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as exc:
        raise AuthorizationError("authorization contains invalid base64url") from exc


def sign_assertion(payload: dict[str, Any], secret: str) -> str:
    if len(secret.encode("utf-8")) < 32:
        raise AuthorizationError("Contact Skill signing secret must be at least 32 bytes")
    payload_segment = _encode(canonical_bytes(payload))
    signature = hmac.new(
        secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_segment}.{_encode(signature)}"


def verify_assertion(
    token: str,
    secret: str,
    *,
    expected: dict[str, Any],
    now_us: int | None = None,
) -> dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 2 or not all(parts):
        raise AuthorizationError("authorization must contain payload and signature")
    payload_segment, signature_segment = parts
    expected_signature = hmac.new(
        secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected_signature, _decode(signature_segment)):
        raise AuthorizationError("authorization signature is invalid")
    try:
        payload = json.loads(_decode(payload_segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError("authorization payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AuthorizationError("authorization payload must be an object")
    required_constants = {
        "schema": "midbrain.contact_work_authorization",
        "schema_version": 1,
    }
    for name, value in {**required_constants, **expected}.items():
        if payload.get(name) != value:
            raise AuthorizationError(f"authorization does not match {name}")
    for name in ("assertion_id", "nonce", "mounted_effector_revision"):
        if not str(payload.get(name) or "").strip():
            raise AuthorizationError(f"authorization is missing {name}")
    now = time.time_ns() // 1000 if now_us is None else int(now_us)
    try:
        issued = int(payload["issued_at_us"])
        expires = int(payload["expires_at_us"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationError("authorization time bounds are invalid") from exc
    if issued <= 0 or expires <= issued or issued > now + 1_000_000:
        raise AuthorizationError("authorization time range is invalid")
    if expires <= now:
        raise AuthorizationError("authorization has expired")
    return payload
