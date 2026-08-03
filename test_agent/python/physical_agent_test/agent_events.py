from __future__ import annotations

import json
from typing import Any

from .visual_evidence import sanitize_visual_evidence


_RUN_ITEM_EVENT_TYPES = {
    "message_output_created": "assistant.message.completed",
    "reasoning_item_created": "assistant.reasoning_summary.completed",
    "tool_called": "tool.called",
    "tool_output": "tool.completed",
    "tool_search_called": "tool.search.called",
    "tool_search_output_created": "tool.search.completed",
    "handoff_requested": "agent.handoff.requested",
    "handoff_occured": "agent.handoff.completed",
    "mcp_approval_requested": "mcp.approval.required",
    "mcp_approval_response": "mcp.approval.resolved",
    "mcp_list_tools": "mcp.tools.listed",
}


def translate_openai_sdk_event(
    event: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Translate one Agents SDK event into the stable Midbrain UI subset."""

    event_type = str(getattr(event, "type", ""))
    if event_type == "raw_response_event":
        return _translate_raw_response_event(getattr(event, "data", None))
    if event_type == "run_item_stream_event":
        return _translate_run_item_event(event)
    if event_type == "agent_updated_stream_event":
        agent = getattr(event, "new_agent", None)
        return (
            "agent.updated",
            {"agent_name": str(getattr(agent, "name", "") or "")},
        )
    return None


def translate_openai_sdk_events(
    event: Any,
) -> list[tuple[str, dict[str, Any]]]:
    """Translate one SDK event into one or more safe Midbrain events."""

    translated: list[tuple[str, dict[str, Any]]] = []
    lifecycle_event = translate_openai_sdk_event(event)
    if lifecycle_event is not None:
        translated.append(lifecycle_event)
    retry_event = _tool_retry_event(event)
    if retry_event is not None:
        translated.append(retry_event)
    visual_evidence = _tool_visual_evidence(event)
    if visual_evidence is not None:
        translated.append(("visual.evidence.created", visual_evidence))
    return translated


def _translate_raw_response_event(
    data: Any,
) -> tuple[str, dict[str, Any]] | None:
    response_type = str(getattr(data, "type", ""))
    if response_type == "response.output_text.delta":
        return (
            "assistant.message.delta",
            {
                "text": str(getattr(data, "delta", "") or ""),
                "item_id": _optional_text(getattr(data, "item_id", None)),
                "output_index": getattr(data, "output_index", None),
                "content_index": getattr(data, "content_index", None),
            },
        )
    if response_type == "response.reasoning_summary_text.delta":
        return (
            "assistant.reasoning_summary.delta",
            {
                "text": str(getattr(data, "delta", "") or ""),
                "item_id": _optional_text(getattr(data, "item_id", None)),
                "output_index": getattr(data, "output_index", None),
                "summary_index": getattr(data, "summary_index", None),
            },
        )
    return None


def _translate_run_item_event(
    event: Any,
) -> tuple[str, dict[str, Any]] | None:
    name = str(getattr(event, "name", ""))
    translated_type = _RUN_ITEM_EVENT_TYPES.get(name)
    if translated_type is None:
        return None
    item = getattr(event, "item", None)
    raw_item = getattr(item, "raw_item", None)
    payload: dict[str, Any] = {
        "sdk_item_event": name,
        "item_type": str(getattr(item, "type", "") or ""),
    }
    tool_name = _first_text(
        _read_field(raw_item, "name"),
        _read_field(getattr(item, "tool_origin", None), "tool_name"),
        getattr(item, "title", None),
    )
    call_id = _first_text(
        _read_field(raw_item, "call_id"),
        _read_field(raw_item, "id"),
    )
    if tool_name is not None:
        payload["tool_name"] = tool_name
    if call_id is not None:
        payload["call_id"] = call_id
    agent = getattr(item, "agent", None)
    agent_name = _optional_text(getattr(agent, "name", None))
    if agent_name is not None:
        payload["agent_name"] = agent_name
    return translated_type, payload


def _tool_visual_evidence(event: Any) -> dict[str, Any] | None:
    decoded = _tool_output_object(event)
    if decoded is None:
        return None
    return sanitize_visual_evidence(decoded.get("visual_evidence"))


def _tool_retry_event(
    event: Any,
) -> tuple[str, dict[str, Any]] | None:
    decoded = _tool_output_object(event)
    if decoded is None:
        return None
    history = decoded.get("retry_history")
    if not isinstance(history, dict):
        return None
    scope = history.get("scope")
    attempt_count = history.get("attempt_count")
    maximum_attempts = history.get("maximum_attempts")
    recovered = history.get("recovered")
    exhausted = history.get("exhausted")
    if scope != "CAPTURE_RGB_ONLY":
        return None
    if (
        not _bounded_integer(attempt_count, minimum=2, maximum=3)
        or not _bounded_integer(maximum_attempts, minimum=2, maximum=3)
        or attempt_count > maximum_attempts
        or not isinstance(recovered, bool)
        or not isinstance(exhausted, bool)
        or recovered == exhausted
        or history.get("requires_fresh_evidence") is not True
        or history.get("physical_action_submitted") is not False
    ):
        return None
    payload: dict[str, Any] = {
        "scope": scope,
        "attempt_count": attempt_count,
        "maximum_attempts": maximum_attempts,
        "recovered": recovered,
        "exhausted": exhausted,
        "requires_fresh_evidence": True,
        "physical_action_submitted": False,
    }
    item = getattr(event, "item", None)
    tool_name = _first_text(
        _read_field(getattr(item, "tool_origin", None), "tool_name"),
        _read_field(getattr(item, "raw_item", None), "name"),
    )
    if tool_name is not None:
        payload["tool_name"] = tool_name
    event_type = (
        "skill.retry.recovered" if recovered else "skill.retry.exhausted"
    )
    return event_type, payload


def _tool_output_object(event: Any) -> dict[str, Any] | None:
    if str(getattr(event, "type", "")) != "run_item_stream_event":
        return None
    if str(getattr(event, "name", "")) != "tool_output":
        return None
    item = getattr(event, "item", None)
    output = getattr(item, "output", None)
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            return None
    elif isinstance(output, dict):
        decoded = output
    else:
        return None
    return decoded if isinstance(decoded, dict) else None


def _bounded_integer(value: Any, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _read_field(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _optional_text(value)
        if text is not None:
            return text
    return None
