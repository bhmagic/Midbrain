from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import threading
import time
import uuid
from typing import Any


class AuthorizationStore:
    """Ephemeral decision records; approval never executes an action by itself."""

    def __init__(self, signing_secret: str | None = None):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._signing_secret = str(signing_secret or "")

    def create(
        self,
        *,
        requester_type: str,
        requester_id: str,
        decision_type: str,
        title: str,
        summary: str,
        proposed_action: dict[str, Any],
        evidence: dict[str, Any],
        safety: dict[str, Any],
        expires_in_s: float = 120.0,
    ) -> dict[str, Any]:
        now_us = time.time_ns() // 1000
        decision_id = str(uuid.uuid4())
        record = {
            "schema": "physical_agent.authorization_decision",
            "schema_version": 1,
            "decision_id": decision_id,
            "requester_type": str(requester_type).upper(),
            "requester_id": str(requester_id),
            "decision_type": str(decision_type).upper(),
            "title": str(title),
            "summary": str(summary),
            "proposed_action": copy.deepcopy(proposed_action),
            "evidence": copy.deepcopy(evidence),
            "safety": {
                **copy.deepcopy(safety),
                "approval_executes_action": False,
            },
            "status": "PENDING",
            "created_at_us": now_us,
            "expires_at_us": now_us + int(max(1.0, expires_in_s) * 1_000_000),
            "resolved_at_us": None,
            "resolution": None,
            "resolved_by": None,
            "note": None,
            "execution_assertion_id": None,
            "execution_assertion_sha256": None,
            "execution_assertion_issued_at_us": None,
        }
        with self._lock:
            self._records[decision_id] = record
        return copy.deepcopy(record)

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        self._expire()
        normalized = None if status is None else str(status).upper()
        with self._lock:
            records = [
                copy.deepcopy(record)
                for record in self._records.values()
                if normalized is None or record["status"] == normalized
            ]
        return sorted(records, key=lambda record: record["created_at_us"])

    def get(self, decision_id: str) -> dict[str, Any]:
        self._expire()
        with self._lock:
            record = self._records.get(decision_id)
            if record is None:
                raise KeyError(decision_id)
            return copy.deepcopy(record)

    def resolve(
        self,
        decision_id: str,
        *,
        resolution: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(resolution).upper()
        if normalized not in {"APPROVED", "DENIED"}:
            raise ValueError("resolution must be APPROVED or DENIED")
        self._expire()
        with self._lock:
            record = self._records.get(decision_id)
            if record is None:
                raise KeyError(decision_id)
            if record["status"] != "PENDING":
                raise RuntimeError(
                    f"authorization decision is already {record['status']}"
                )
            record["status"] = normalized
            record["resolution"] = normalized
            record["resolved_at_us"] = time.time_ns() // 1000
            record["resolved_by"] = str(resolved_by)
            record["note"] = None if note is None else str(note)
            return copy.deepcopy(record)

    def issue_execution_assertion(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        """Mint one short-lived assertion; this does not execute the action."""

        self._expire()
        with self._lock:
            record = self._records.get(decision_id)
            if record is None:
                raise KeyError(decision_id)
            if record["status"] != "APPROVED":
                raise RuntimeError(
                    "execution assertion requires an APPROVED decision"
                )
            if record["execution_assertion_id"] is not None:
                raise RuntimeError(
                    "execution assertion was already issued for this decision"
                )
            if len(self._signing_secret.encode("utf-8")) < 32:
                raise RuntimeError(
                    "MIDBRAIN_AUTHORIZATION_SECRET must contain at least 32 bytes"
                )
            authority = (
                record.get("safety", {}).get(
                    "controller_preview_authority"
                )
            )
            if not isinstance(authority, dict):
                raise RuntimeError(
                    "approved decision has no controller preview authority"
                )
            now_us = time.time_ns() // 1000
            expires_at_us = min(
                int(record["expires_at_us"]),
                int(authority["expires_at_us"]),
            )
            if expires_at_us <= now_us:
                raise RuntimeError(
                    "approved decision or controller preview has expired"
                )
            assertion_id = str(uuid.uuid4())
            claims = {
                "schema": (
                    "physical_agent.authorization_execution_assertion"
                ),
                "schema_version": 1,
                "assertion_id": assertion_id,
                "issuer": "physical-agent-ui",
                "audience": authority["controller_provider_id"],
                "action": "EXECUTE_TRANSIT_PATH",
                "decision_id": record["decision_id"],
                "decision_type": record["decision_type"],
                "resolution": record["resolution"],
                "resolved_by": record["resolved_by"],
                "issued_at_us": now_us,
                "expires_at_us": expires_at_us,
                "controller_provider_id": authority[
                    "controller_provider_id"
                ],
                "controller_provider_instance_id": authority[
                    "controller_provider_instance_id"
                ],
                "controller_boot_id": authority["controller_boot_id"],
                "controller_configuration_sha256": authority[
                    "controller_configuration_sha256"
                ],
                "plan_id": authority["plan_id"],
                "request_sha256": authority["request_sha256"],
                "preview_sha256": authority["preview_sha256"],
                "scene_revision": authority["scene_revision"],
                "proposed_action_sha256": hashlib.sha256(
                    json.dumps(
                        record["proposed_action"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
            }
            payload_segment = base64.urlsafe_b64encode(
                json.dumps(
                    claims,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).rstrip(b"=").decode("ascii")
            signature_segment = base64.urlsafe_b64encode(
                hmac.new(
                    self._signing_secret.encode("utf-8"),
                    payload_segment.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            ).rstrip(b"=").decode("ascii")
            assertion = f"{payload_segment}.{signature_segment}"
            assertion_sha256 = hashlib.sha256(
                assertion.encode("ascii")
            ).hexdigest()
            record["execution_assertion_id"] = assertion_id
            record["execution_assertion_sha256"] = assertion_sha256
            record["execution_assertion_issued_at_us"] = now_us
            return {
                "schema": (
                    "physical_agent.authorization_execution_assertion_issue"
                ),
                "schema_version": 1,
                "assertion": assertion,
                "assertion_sha256": assertion_sha256,
                "claims": copy.deepcopy(claims),
                "approval_executes_action": False,
            }

    def _expire(self) -> None:
        now_us = time.time_ns() // 1000
        with self._lock:
            for record in self._records.values():
                if (
                    record["status"] == "PENDING"
                    and now_us >= int(record["expires_at_us"])
                ):
                    record["status"] = "EXPIRED"
                    record["resolution"] = "EXPIRED"
                    record["resolved_at_us"] = now_us
