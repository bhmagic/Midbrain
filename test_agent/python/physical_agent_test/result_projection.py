from __future__ import annotations

import copy
import json
import re
from typing import Any

from .skill_catalog import AgentSkillDescriptor, SkillResultTierPolicy
from .skill_result_details import SkillResultDetailStore


_CREDENTIAL_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|cookie|credential|password|passwd|"
    r"private[-_ ]?key|secret|token)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TEXT = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_OUTCOME_POINTER_PRIORITY = (
    "/status",
    "/workflow_complete",
    "/physical_motion_authorized",
    "/physical_motion_requested",
    "/physical_motion_submitted",
    "/physical_motion_completed",
    "/task_success_assessed",
    "/result_semantics",
    "/required_next_tool",
    "/message",
    "/visual_evidence",
)


def redact_credential_values(value: Any) -> Any:
    """Return a safe copy with credential-like values removed."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(marker in normalized for marker in _CREDENTIAL_MARKERS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_credential_values(item)
        return redacted
    if isinstance(value, list):
        return [redact_credential_values(item) for item in value]
    if isinstance(value, str):
        redacted = _BEARER_TEXT.sub("Bearer [REDACTED]", value)
        return _CREDENTIAL_TEXT.sub(r"\1=[REDACTED]", redacted)
    return copy.deepcopy(value)


def compact_pointer_allows(
    compact_pointers: tuple[str, ...],
    pointer: str,
) -> bool:
    return any(
        pointer == selected or pointer.startswith(f"{selected}/")
        for selected in compact_pointers
    )


def project_compact_result(
    result: dict[str, Any],
    policy: SkillResultTierPolicy,
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for pointer in policy.compact_pointers:
        found, value = _resolve_optional_pointer(result, pointer)
        if found:
            _write_pointer(projected, pointer, value)
    return projected


async def finalize_skill_result(
    result: Any,
    descriptor: AgentSkillDescriptor,
    detail_store: SkillResultDetailStore | None,
) -> dict[str, Any]:
    """Validate, retain, and compact one complete adapter result."""

    from jsonschema import validate

    normalized = _normalize_result(result)
    validate(instance=normalized, schema=descriptor.output_schema)
    sanitized = redact_credential_values(normalized)
    compact = project_compact_result(sanitized, descriptor.result_tiers)
    if descriptor.result_tiers.detail_policy == "HOST_SANITIZED_REFERENCE":
        if detail_store is None:
            detail_ref = {
                "schema": "midbrain.skill_result_detail_ref",
                "schema_version": 1,
                "available": False,
                "reason": "DETAIL_STORE_NOT_CONFIGURED",
            }
        else:
            detail_ref = await detail_store.store(
                sanitized,
                tool_name=descriptor.tool_name,
                skill_type=descriptor.skill_type,
                skill_version=descriptor.skill_version,
                output_schema=descriptor.output_schema,
            )
        compact["detail_ref"] = detail_ref
    if _encoded_size(compact) > descriptor.result_tiers.max_compact_bytes:
        compact = _bounded_compact_result(
            sanitized,
            descriptor.result_tiers,
            detail_ref=compact.get("detail_ref"),
        )
    return compact


def _bounded_compact_result(
    result: dict[str, Any],
    policy: SkillResultTierPolicy,
    *,
    detail_ref: Any,
) -> dict[str, Any]:
    """Preserve outcome evidence when a selected diagnostic is unexpectedly large."""

    priority = {pointer: index for index, pointer in enumerate(_OUTCOME_POINTER_PRIORITY)}
    selected = sorted(
        policy.compact_pointers,
        key=lambda pointer: (
            priority.get(pointer, len(priority)),
            policy.compact_pointers.index(pointer),
        ),
    )
    bounded: dict[str, Any] = {}
    if detail_ref is not None:
        bounded["detail_ref"] = copy.deepcopy(detail_ref)
    retained: set[str] = set()
    present: list[str] = []
    for pointer in selected:
        found, value = _resolve_optional_pointer(result, pointer)
        if not found:
            continue
        present.append(pointer)
        candidate = copy.deepcopy(bounded)
        _write_pointer(candidate, pointer, value)
        candidate["compact_projection"] = {
            "schema": "midbrain.compact_result_projection",
            "schema_version": 1,
            "complete": False,
            "reason": "MAX_COMPACT_BYTES",
        }
        if _encoded_size(candidate) <= policy.max_compact_bytes:
            bounded = candidate
            retained.add(pointer)
    omitted = [pointer for pointer in present if pointer not in retained]
    marker: dict[str, Any] = {
        "schema": "midbrain.compact_result_projection",
        "schema_version": 1,
        "complete": False,
        "reason": "MAX_COMPACT_BYTES",
        "omitted_pointer_count": len(omitted),
        "omitted_pointers": omitted,
    }
    bounded["compact_projection"] = marker
    if _encoded_size(bounded) > policy.max_compact_bytes:
        marker.pop("omitted_pointers")
    if _encoded_size(bounded) > policy.max_compact_bytes:
        bounded = {
            "compact_projection": {
                "schema": "midbrain.compact_result_projection",
                "schema_version": 1,
                "complete": False,
                "reason": "MAX_COMPACT_BYTES",
                "omitted_pointer_count": len(present),
            }
        }
        if detail_ref is not None:
            bounded["detail_ref"] = copy.deepcopy(detail_ref)
    if _encoded_size(bounded) > policy.max_compact_bytes:
        raise ValueError("compact result metadata exceeds its declared byte limit")
    return bounded


def _encoded_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def select_result_detail(
    record: dict[str, Any],
    pointer: str | None,
) -> dict[str, Any]:
    payload = record["payload"]
    if pointer is None:
        selected = copy.deepcopy(payload)
    else:
        found, selected = _resolve_optional_pointer(payload, pointer)
        if not found:
            raise KeyError(f"detail pointer {pointer!r} is absent")
    return {
        "schema": "midbrain.skill_result_detail",
        "schema_version": 1,
        "result_id": record["result_id"],
        "tool_name": record["tool_name"],
        "skill_type": record["skill_type"],
        "skill_version": record["skill_version"],
        "schema_sha256": record["schema_sha256"],
        "payload_sha256": record["payload_sha256"],
        "source_size_bytes": record["size_bytes"],
        "selected_pointer": pointer,
        "detail": selected,
    }


def select_json_pointer(value: Any, pointer: str | None) -> Any:
    if pointer is None:
        return copy.deepcopy(value)
    found, selected = _resolve_optional_pointer(value, pointer)
    if not found:
        raise KeyError(f"JSON pointer {pointer!r} is absent")
    return selected


def _normalize_result(result: Any) -> dict[str, Any]:
    normalized = result
    if isinstance(result, str):
        try:
            normalized = json.loads(result)
        except json.JSONDecodeError as error:
            raise ValueError("Skill result must be a JSON object") from error
    if not isinstance(normalized, dict):
        raise ValueError("Skill result must be a JSON object")
    return normalized


def _pointer_tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with /")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def _resolve_optional_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    current = value
    for token in _pointer_tokens(pointer):
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, copy.deepcopy(current)


def _write_pointer(output: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    current = output
    for token in tokens[:-1]:
        child = current.get(token)
        if child is None:
            child = {}
            current[token] = child
        if not isinstance(child, dict):
            raise ValueError(f"compact pointers overlap at {pointer!r}")
        current = child
    final = tokens[-1]
    existing = current.get(final)
    if isinstance(existing, dict) and isinstance(value, dict):
        existing.update(copy.deepcopy(value))
    else:
        current[final] = copy.deepcopy(value)
