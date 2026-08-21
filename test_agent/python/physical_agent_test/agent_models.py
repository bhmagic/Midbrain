from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from agents import (
    FunctionTool,
    Model,
    OpenAIChatCompletionsModel,
    ToolSearchTool,
)
from openai import AsyncOpenAI


GEMINI_OPENAI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
)
GPT_MODEL_PREFIX = "gpt-"
GEMINI_MODEL_PREFIX = "gemini-"
DEFAULT_AGENT_REASONING_EFFORTS = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
GEMINI_AGENT_REASONING_EFFORTS = ("low", "medium", "high")
LOCAL_TOOL_SEARCH_NAME = "tool_search"
_LOCAL_TOOL_SEARCH_SOURCES = "_midbrain_local_tool_search_sources"
_LOCAL_DEFERRED_TOOL = "_midbrain_local_deferred_tool"
_LOCAL_TOOL_SEARCH_MAX_ARGUMENT_CHARS = 65_536


logger = logging.getLogger(__name__)


def is_gemini_agent_model(model_id: str) -> bool:
    return model_id.strip().lower().startswith(GEMINI_MODEL_PREFIX)


def is_gpt_agent_model(model_id: str) -> bool:
    return model_id.strip().lower().startswith(GPT_MODEL_PREFIX)


def agent_model_api_key_name(model_id: str) -> str:
    if is_gemini_agent_model(model_id):
        return "GEMINI_API_KEY"
    return "OPENAI_API_KEY"


def require_agent_model_credential(model_id: str) -> None:
    key_name = agent_model_api_key_name(model_id)
    if not os.getenv(key_name, "").strip():
        raise RuntimeError(f"{key_name} is empty in config/api_keys.env")


def supported_agent_reasoning_efforts(model_id: str) -> tuple[str, ...]:
    if is_gemini_agent_model(model_id):
        return GEMINI_AGENT_REASONING_EFFORTS
    return DEFAULT_AGENT_REASONING_EFFORTS


def uses_native_gpt_tool_search(model_id: str) -> bool:
    return is_gpt_agent_model(model_id)


def tools_for_agent_model(
    model_id: str,
    tools: Sequence[Any],
) -> list[Any]:
    if uses_native_gpt_tool_search(model_id):
        return list(tools)
    deferred_tools = [
        tool
        for tool in tools
        if isinstance(tool, FunctionTool) and tool.defer_loading
    ]
    tool_search_count = sum(
        isinstance(tool, ToolSearchTool) for tool in tools
    )
    if deferred_tools and tool_search_count != 1:
        raise ValueError(
            "A non-GPT deferred Skill surface requires exactly one "
            "ToolSearchTool source"
        )
    compatible: list[Any] = []
    for tool in tools:
        if isinstance(tool, ToolSearchTool):
            continue
        if isinstance(tool, FunctionTool) and tool.defer_loading:
            compatible.append(_materialize_after_local_search(tool))
        else:
            compatible.append(tool)
    if deferred_tools:
        compatible.append(
            _build_local_tool_search(deferred_tools)
        )
    return compatible


def _materialize_after_local_search(tool: FunctionTool) -> FunctionTool:
    original_is_enabled = tool.is_enabled

    async def is_enabled(context_wrapper, agent) -> bool:
        if tool.name not in _loaded_tool_names(context_wrapper):
            return False
        if isinstance(original_is_enabled, bool):
            return original_is_enabled
        selected = original_is_enabled(context_wrapper, agent)
        if inspect.isawaitable(selected):
            selected = await selected
        return bool(selected)

    materialized = replace(
        tool,
        defer_loading=False,
        is_enabled=is_enabled,
    )
    setattr(materialized, _LOCAL_DEFERRED_TOOL, True)
    return materialized


def _build_local_tool_search(
    deferred_tools: Sequence[FunctionTool],
) -> FunctionTool:
    tools_by_name = {tool.name: tool for tool in deferred_tools}
    if len(tools_by_name) != len(deferred_tools):
        raise ValueError("Deferred Skill tools must have unique names")
    tier_one_entries = [
        f"{name}: {tool.description}"
        for name, tool in sorted(tools_by_name.items())
    ]

    async def search_tools(context_wrapper, raw_arguments: str) -> str:
        call_id = getattr(context_wrapper, "tool_call_id", None)
        arguments, parse_failure = _parse_local_tool_search_arguments(
            raw_arguments,
            call_id=call_id,
        )
        if parse_failure is not None:
            return parse_failure
        assert arguments is not None
        selected_names = arguments.get("paths")
        if (
            not isinstance(selected_names, list)
            or not selected_names
            or not all(isinstance(name, str) for name in selected_names)
        ):
            return _local_tool_search_failure(
                call_id=call_id,
                code="INVALID_PATHS",
                message=(
                    "paths must be a non-empty array of exact visible Skill "
                    "names. Retry tool_search with one valid JSON object."
                ),
                diagnostics={
                    "argument_length": len(raw_arguments),
                    "allowed_path_count": len(tools_by_name),
                },
                allowed_paths=sorted(tools_by_name),
            )
        unknown = sorted(set(selected_names) - tools_by_name.keys())
        if unknown:
            return _local_tool_search_failure(
                call_id=call_id,
                code="UNKNOWN_OR_INELIGIBLE_SKILL",
                message=(
                    "One or more requested paths are not eligible on this "
                    "tool-search surface. Retry using only allowed_paths."
                ),
                diagnostics={
                    "argument_length": len(raw_arguments),
                    "selected_path_count": len(selected_names),
                    "unknown_path_count": len(unknown),
                    "allowed_path_count": len(tools_by_name),
                },
                allowed_paths=sorted(tools_by_name),
            )
        loaded = _loaded_tool_names(context_wrapper)
        loaded.update(selected_names)
        loaded_tools = [
            {
                "type": "function",
                "name": name,
                "description": tools_by_name[name].description,
                "defer_loading": True,
                "parameters": tools_by_name[name].params_json_schema,
                "strict": tools_by_name[name].strict_json_schema,
            }
            for name in selected_names
        ]
        return json.dumps(
            {
                "type": "tool_search_output",
                "execution": "client",
                "call_id": call_id,
                "status": "completed",
                "tools": loaded_tools,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    search_tool = FunctionTool(
        name=LOCAL_TOOL_SEARCH_NAME,
        description=(
            "Search for and load one or more deferred finite Skill function "
            "definitions. Select paths using the exact visible Skill names. "
            "Loaded functions become callable on the next Agent turn. This "
            "search does not execute a Skill or grant authority. Searchable "
            "functions: "
            + " | ".join(tier_one_entries)
        ),
        params_json_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        on_invoke_tool=search_tools,
        strict_json_schema=True,
        needs_approval=False,
    )
    setattr(
        search_tool,
        _LOCAL_TOOL_SEARCH_SOURCES,
        tuple(deferred_tools),
    )
    return search_tool


def _parse_local_tool_search_arguments(
    raw_arguments: str,
    *,
    call_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    argument_length = len(raw_arguments)
    if argument_length > _LOCAL_TOOL_SEARCH_MAX_ARGUMENT_CHARS:
        return None, _local_tool_search_failure(
            call_id=call_id,
            code="ARGUMENTS_TOO_LARGE",
            message=(
                "Tool-search arguments exceed the bounded compatibility "
                "limit. Retry with one concise JSON object."
            ),
            diagnostics={"argument_length": argument_length},
        )
    try:
        value = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        duplicated = _collapse_identical_json_objects(raw_arguments)
        if duplicated is not None:
            value, duplicate_count = duplicated
            logger.warning(
                "Recovered repeated local tool-search JSON objects "
                "without retaining arguments: call_id=%s "
                "argument_length=%d duplicate_count=%d",
                call_id,
                argument_length,
                duplicate_count,
            )
        else:
            return None, _local_tool_search_failure(
                call_id=call_id,
                code="INVALID_JSON",
                message=(
                    "Tool-search arguments must be exactly one JSON object. "
                    "Retry tool_search without trailing text or another "
                    "object."
                ),
                diagnostics={
                    "argument_length": argument_length,
                    "error_position": error.pos,
                    "error_line": error.lineno,
                    "error_column": error.colno,
                },
            )
    if not isinstance(value, dict):
        return None, _local_tool_search_failure(
            call_id=call_id,
            code="INVALID_ARGUMENT_SHAPE",
            message=(
                "Tool-search arguments must be one JSON object. Retry with "
                "an object containing paths."
            ),
            diagnostics={"argument_length": argument_length},
        )
    return value, None


def _collapse_identical_json_objects(
    raw_arguments: str,
) -> tuple[Any, int] | None:
    decoder = json.JSONDecoder()
    cursor = 0
    values: list[Any] = []
    while cursor < len(raw_arguments) and len(values) < 4:
        while (
            cursor < len(raw_arguments)
            and raw_arguments[cursor].isspace()
        ):
            cursor += 1
        if cursor >= len(raw_arguments):
            break
        try:
            value, cursor = decoder.raw_decode(raw_arguments, cursor)
        except json.JSONDecodeError:
            return None
        values.append(value)
    if raw_arguments[cursor:].strip() or len(values) < 2:
        return None
    first = values[0]
    if not isinstance(first, dict) or any(value != first for value in values[1:]):
        return None
    return first, len(values)


def _local_tool_search_failure(
    *,
    call_id: str | None,
    code: str,
    message: str,
    diagnostics: dict[str, int],
    allowed_paths: list[str] | None = None,
) -> str:
    safe_diagnostics = {
        key: int(value)
        for key, value in diagnostics.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    logger.warning(
        "Rejected local tool-search arguments without retaining them: "
        "call_id=%s code=%s diagnostics=%s",
        call_id,
        code,
        safe_diagnostics,
    )
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": True,
    }
    if allowed_paths is not None:
        error["allowed_paths"] = allowed_paths
    return json.dumps(
        {
            "type": "tool_search_error",
            "execution": "client",
            "call_id": call_id,
            "status": "failed",
            "error": error,
            "diagnostics": safe_diagnostics,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def narrow_local_tool_search(
    tool: Any,
    allowed_tool_names: set[str],
) -> Any | None:
    sources = getattr(tool, _LOCAL_TOOL_SEARCH_SOURCES, None)
    if not isinstance(sources, tuple):
        return tool
    selected = [
        source
        for source in sources
        if isinstance(source, FunctionTool)
        and source.name in allowed_tool_names
    ]
    if not selected:
        return None
    return _build_local_tool_search(selected)


def is_local_deferred_tool(tool: Any) -> bool:
    return getattr(tool, _LOCAL_DEFERRED_TOOL, False) is True


def is_local_tool_search(tool: Any) -> bool:
    return isinstance(
        getattr(tool, _LOCAL_TOOL_SEARCH_SOURCES, None),
        tuple,
    )


def _loaded_tool_names(context_wrapper: Any) -> set[str]:
    context = getattr(context_wrapper, "context", None)
    loaded = getattr(context, "loaded_tool_names", None)
    if not isinstance(loaded, set):
        raise RuntimeError(
            "Local Skill discovery requires Agent run discovery state"
        )
    return loaded


def resolve_agent_model(model_id: str) -> str | Model:
    normalized = model_id.strip()
    if not normalized:
        raise ValueError("Agent model ID cannot be empty")
    if not is_gemini_agent_model(normalized):
        return normalized
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    client = AsyncOpenAI(
        api_key=api_key or "missing-gemini-api-key",
        base_url=GEMINI_OPENAI_BASE_URL,
    )
    return OpenAIChatCompletionsModel(
        model=normalized,
        openai_client=client,
    )
