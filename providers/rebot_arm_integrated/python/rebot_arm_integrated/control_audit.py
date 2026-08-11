from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable


class ControlAuditOutbox:
    """Provider-local append-only control audit with asynchronous Fabric replay."""

    def __init__(
        self,
        provider_root: Path,
        provider_id: str,
        provider_instance_id: str,
        boot_id: str,
        config: dict[str, Any] | None,
    ):
        settings = config or {}
        self.enabled = bool(settings.get("enabled", True))
        self.mode = str(settings.get("mode", "SHADOW_BEST_EFFORT"))
        self.strict_local_write = bool(settings.get("strict_local_write", False))
        self.fabric_copy_enabled = bool(settings.get("fabric_copy_enabled", True))
        self.fabric_stream = str(
            settings.get(
                "fabric_stream",
                "robot_arm.integrated.control_audit",
            )
        )
        configured_path = Path(
            str(settings.get("path", "runtime_logs/control_audit/events.jsonl"))
        )
        self.events_path = (
            configured_path
            if configured_path.is_absolute()
            else (provider_root / configured_path).resolve()
        )
        configured_cursor = Path(
            str(settings.get("cursor_path", "runtime_logs/control_audit/fabric_cursor.json"))
        )
        self.cursor_path = (
            configured_cursor
            if configured_cursor.is_absolute()
            else (provider_root / configured_cursor).resolve()
        )
        self.provider_id = provider_id
        self.provider_instance_id = provider_instance_id
        self.boot_id = boot_id
        self.maximum_pending = max(64, int(settings.get("maximum_pending", 4096)))
        self.maximum_replay = max(0, int(settings.get("maximum_replay", 4096)))
        self.maximum_fabric_event_bytes = max(
            4096,
            int(settings.get("maximum_fabric_event_bytes", 1_000_000)),
        )
        self._lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._sequence = 0
        self._published_sequence = 0
        self._recorded_count = 0
        self._local_persisted_count = 0
        self._local_write_failure_count = 0
        self._published_count = 0
        self._projected_fabric_count = 0
        self._dropped_pending_count = 0
        self._last_local_error: str | None = None
        self._last_fabric_error: str | None = None
        self._load_existing()

    def record(
        self,
        *,
        lifecycle: str,
        endpoint: str,
        command_id: str,
        canonical_request: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: str | None = None,
        related_skill_id: str | None = None,
        binding_id: str | None = None,
        authority_id: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        request_copy = _json_copy(canonical_request)
        result_copy = None if result is None else _json_copy(result)
        request_bytes = json.dumps(
            request_copy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self._lock:
            self._sequence += 1
            event = {
                "schema": "physical_agent.control_audit_event",
                "schema_version": 1,
                "audit_event_id": str(uuid.uuid4()),
                "audit_sequence": self._sequence,
                "recorded_at_us": time.time_ns() // 1000,
                "provider_id": self.provider_id,
                "provider_instance_id": self.provider_instance_id,
                "boot_id": self.boot_id,
                "lifecycle": str(lifecycle),
                "endpoint": str(endpoint),
                "command_id": str(command_id),
                "plan_id": plan_id,
                "binding_id": binding_id,
                "authority_id": authority_id,
                "related_skill_id": related_skill_id,
                "canonical_request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "canonical_request": request_copy,
                "result": result_copy,
                "error": error,
                "mode": self.mode,
            }
            self._recorded_count += 1
            local_persisted = False
            local_error: str | None = None
            if self.enabled:
                try:
                    self.events_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(
                            json.dumps(
                                event,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                        handle.write("\n")
                        handle.flush()
                        if self.strict_local_write:
                            os.fsync(handle.fileno())
                    self._last_local_error = None
                    self._local_persisted_count += 1
                    local_persisted = True
                except Exception as audit_error:
                    local_error = str(audit_error)
                    self._last_local_error = local_error
                    self._local_write_failure_count += 1
                    if self.strict_local_write:
                        raise
            if self.fabric_copy_enabled:
                if len(self._pending) >= self.maximum_pending:
                    self._pending.popleft()
                    self._dropped_pending_count += 1
                self._pending.append(copy.deepcopy(event))
            result = copy.deepcopy(event)
            result["local_delivery"] = {
                "enabled": self.enabled,
                "persisted": local_persisted,
                "error": local_error,
            }
            return result

    def publish_pending(
        self,
        publish: Callable[[str, dict[str, Any]], None],
        *,
        maximum_events: int = 8,
    ) -> int:
        published = 0
        for _ in range(maximum_events):
            with self._lock:
                event = copy.deepcopy(self._pending[0]) if self._pending else None
            if event is None:
                break
            fabric_event, projected = self._fabric_event(event)
            try:
                publish(self.fabric_stream, fabric_event)
            except Exception as error:
                with self._lock:
                    self._last_fabric_error = str(error)
                break
            with self._lock:
                if (
                    self._pending
                    and self._pending[0]["audit_event_id"] == event["audit_event_id"]
                ):
                    self._pending.popleft()
                self._published_sequence = max(
                    self._published_sequence,
                    int(event["audit_sequence"]),
                )
                self._published_count += 1
                if projected:
                    self._projected_fabric_count += 1
                self._last_fabric_error = None
                self._write_cursor_locked()
            published += 1
        return published

    def _fabric_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        event_bytes = _canonical_json_bytes(event)
        if len(event_bytes) <= self.maximum_fabric_event_bytes:
            return event, False

        canonical_request = event.get("canonical_request")
        request_bytes = _canonical_json_bytes(canonical_request)
        request_for_fabric: Any = canonical_request
        if len(request_bytes) > self.maximum_fabric_event_bytes // 4:
            request_for_fabric = {
                "fabric_projection": "OVERSIZED_PROVIDER_LOCAL_CANONICAL_REQUEST",
                "sha256": hashlib.sha256(request_bytes).hexdigest(),
                "utf8_bytes": len(request_bytes),
            }

        result = event.get("result")
        result_bytes = _canonical_json_bytes(result)
        result_keys = sorted(result) if isinstance(result, dict) else []
        error = event.get("error")
        projected = {
            key: copy.deepcopy(event.get(key))
            for key in (
                "schema",
                "schema_version",
                "audit_event_id",
                "audit_sequence",
                "recorded_at_us",
                "provider_id",
                "provider_instance_id",
                "boot_id",
                "lifecycle",
                "endpoint",
                "command_id",
                "plan_id",
                "binding_id",
                "authority_id",
                "related_skill_id",
                "canonical_request_sha256",
                "mode",
            )
        }
        projected["canonical_request"] = request_for_fabric
        projected["result"] = {
            "fabric_projection": "OVERSIZED_PROVIDER_LOCAL_RESULT",
            "sha256": hashlib.sha256(result_bytes).hexdigest(),
            "utf8_bytes": len(result_bytes),
            "keys": result_keys,
        }
        projected["error"] = None if error is None else str(error)[:2048]
        projected["fabric_projection"] = {
            "reason": "FABRIC_REQUEST_BODY_LIMIT",
            "exact_provider_local_record": True,
            "original_event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "original_event_utf8_bytes": len(event_bytes),
        }
        return projected, True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "strict_local_write": self.strict_local_write,
                "fabric_copy_enabled": self.fabric_copy_enabled,
                "fabric_stream": self.fabric_stream,
                "events_path": str(self.events_path),
                "cursor_path": str(self.cursor_path),
                "last_sequence": self._sequence,
                "published_sequence": self._published_sequence,
                "pending_count": len(self._pending),
                "recorded_count": self._recorded_count,
                "local_persisted_count": self._local_persisted_count,
                "local_write_failure_count": self._local_write_failure_count,
                "published_count": self._published_count,
                "projected_fabric_count": self._projected_fabric_count,
                "maximum_fabric_event_bytes": self.maximum_fabric_event_bytes,
                "dropped_pending_count": self._dropped_pending_count,
                "last_local_error": self._last_local_error,
                "last_fabric_error": self._last_fabric_error,
            }

    def recent_events(self, *, limit: int = 50) -> dict[str, Any]:
        bounded_limit = max(1, min(200, int(limit)))
        with self._lock:
            published_sequence = self._published_sequence
            events: deque[dict[str, Any]] = deque(maxlen=bounded_limit)
            if self.enabled and self.events_path.is_file():
                try:
                    with self.events_path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            if line.strip():
                                events.append(json.loads(line))
                except Exception as error:
                    return {
                        "status": "ERROR",
                        "error": str(error),
                        "events": [],
                    }
        values = list(events)
        for event in values:
            sequence = int(event.get("audit_sequence") or 0)
            event["fabric_delivery"] = (
                "PUBLISHED"
                if sequence > 0 and sequence <= published_sequence
                else "PENDING"
            )
        return {
            "status": "OK",
            "mode": self.mode,
            "strict_local_write": self.strict_local_write,
            "exact_canonical_request_included": True,
            "event_count": len(values),
            "events": values,
        }

    def _load_existing(self) -> None:
        if not self.enabled:
            return
        if self.cursor_path.is_file():
            try:
                cursor = json.loads(self.cursor_path.read_text(encoding="utf-8"))
                self._published_sequence = int(cursor.get("published_sequence") or 0)
            except Exception as error:
                self._last_local_error = f"cursor load failed: {error}"
        if not self.events_path.is_file():
            return
        replay: deque[dict[str, Any]] = deque(maxlen=self.maximum_replay or None)
        try:
            with self.events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    sequence = int(event.get("audit_sequence") or 0)
                    self._sequence = max(self._sequence, sequence)
                    if (
                        self.fabric_copy_enabled
                        and sequence > self._published_sequence
                        and self.maximum_replay > 0
                    ):
                        replay.append(event)
            self._pending.extend(replay)
        except Exception as error:
            self._last_local_error = f"event replay failed: {error}"

    def _write_cursor_locked(self) -> None:
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cursor_path.with_suffix(self.cursor_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "published_sequence": self._published_sequence,
                        "updated_at_us": time.time_ns() // 1000,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.cursor_path)
        except Exception as error:
            self._last_local_error = f"cursor write failed: {error}"


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
