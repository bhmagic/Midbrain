from __future__ import annotations

import asyncio
import copy
import contextvars
import inspect
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from agents import FunctionTool
from jsonschema.exceptions import ValidationError
from jsonschema import validate
from limited_graph import GraphValidationError

from .agent_events import visual_evidences_from_result
from .limited_graph_authoring import (
    LIMITED_GRAPH_AUTHORING_GUIDANCE,
    compile_limited_graph_arguments,
    limited_graph_authoring_input_schema,
)
from .phase4_policy import extend_current_operation_hard_timeout
from .result_projection import finalize_skill_result
from .skill_catalog import AgentSkillDescriptor, describe_output_schema
from .skill_result_details import SkillResultDetailStore


_LATENCY_TIMEOUT_FLOORS_S = {
    "HIGH": 600.0,
}

_PROVIDER_LIFECYCLE_TOOL_NAME = "set_provider_residency"
_MAX_PROVIDER_HANDOVERS_PER_CHILD = 2
_PROVIDER_HANDOVER_TIMEOUT_MARGIN_S = 0.05
HostedChildEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
_hosted_child_event_sink: contextvars.ContextVar[HostedChildEventSink | None] = (
    contextvars.ContextVar("hosted_child_event_sink", default=None)
)
_graph_authoring_repair_state: contextvars.ContextVar[
    dict[str, int] | None
] = contextvars.ContextVar("graph_authoring_repair_state", default=None)


def set_hosted_child_event_sink(sink: HostedChildEventSink | None) -> Any:
    """Bind the current Agent run's presentation-only child event sink."""

    return _hosted_child_event_sink.set(sink)


def reset_hosted_child_event_sink(token: Any) -> None:
    _hosted_child_event_sink.reset(token)


def set_graph_authoring_repair_state(*, maximum_corrections: int = 1) -> Any:
    """Bind one mutable, run-local graph authoring correction budget."""

    if maximum_corrections < 0:
        raise ValueError("maximum_corrections must not be negative")
    return _graph_authoring_repair_state.set(
        {"remaining": int(maximum_corrections)}
    )


def reset_graph_authoring_repair_state(token: Any) -> None:
    _graph_authoring_repair_state.reset(token)


def _consume_graph_authoring_correction() -> bool:
    state = _graph_authoring_repair_state.get()
    if state is None or int(state.get("remaining", 0)) <= 0:
        return False
    state["remaining"] = int(state["remaining"]) - 1
    return True


def _graph_authoring_error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        pointer = "/" + "/".join(str(item) for item in error.absolute_path)
        location = pointer if pointer != "/" else "the graph root"
        return (
            f"concise input failed {error.validator or 'schema'} "
            f"validation at {location}"
        )
    return str(error).strip().replace("\r", " ").replace("\n", " ")[:700]


def _graph_authoring_failure_result(
    arguments: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    graph = arguments.get("graph")
    graph = graph if isinstance(graph, dict) else {}
    graph_name = str(graph.get("name") or "uncompiled-graph")[:120]
    reason = _graph_authoring_error_message(error)
    return {
        "schema": "midbrain.limited_graph.result",
        "schema_version": 1,
        "graph_run_id": "",
        "graph_sha256": "",
        "graph_name": graph_name,
        "status": "AUTHORING_INVALID",
        "terminal_node": None,
        "message": (
            "Graph authoring was rejected before any child started: "
            f"{reason}. Correct the reported field or topology and call "
            "run_limited_graph exactly once more. Encode initial values, "
            "child arguments, and comparison values as JSON in value_json, "
            "args_json, and expected_json."
        ),
        "last_completed_node": None,
        "last_failure": {
            "kind": "AUTHORING_INVALID",
            "node_id": None,
            "tool_name": "run_limited_graph",
            "reason": reason,
            "physical_action_submitted": False,
        },
        "active_runtime_ms": 0.0,
        "transition_count": 0,
        "node_visits": {},
        "retry_count": 0,
        "model_route_count": 0,
        "physical_action_count": 0,
        "retained_result_bytes": 0,
        "limit": None,
        "authorization": None,
        "trace": [],
        "node_results": {},
    }


class SkillExecutionAdapter(Protocol):
    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """Invoke one selected finite Skill."""


@dataclass(frozen=True)
class HostedChildDescriptor:
    tool_name: str
    skill_type: str
    safety_class: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    expected_latency: str
    compact_pointers: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return self.safety_class == "READ_ONLY"

    @property
    def physical(self) -> bool:
        return self.safety_class == "PHYSICAL_MOTION_AUTHORIZATION_REQUIRED"


ModelRouteCallable = Callable[..., Any | Awaitable[Any]]


@dataclass(frozen=True)
class HostedModelRouteProfile:
    """One host-configured model route; graphs can name but not alter it."""

    modality: str
    invoke: ModelRouteCallable


@dataclass(frozen=True)
class _HostedChildToolContext:
    context: Any
    tool_name: str
    tool_call_id: str
    tool_arguments: str
    agent: Any = None
    run_config: Any = None


@dataclass(frozen=True)
class _HostedModelRouteContext:
    graph_run_id: str
    graph_sha256: str
    node_id: str
    child_call_id: str
    deadline_monotonic: float


class SkillInvocationBrokerHandle:
    """Late-bound bridge used while external Skill adapters are loading."""

    def __init__(self) -> None:
        self._broker: HostedSkillInvocationBroker | None = None

    def bind(self, broker: "HostedSkillInvocationBroker") -> None:
        if self._broker is not None:
            raise RuntimeError("Skill invocation broker is already bound")
        self._broker = broker

    def _require_broker(self) -> "HostedSkillInvocationBroker":
        if self._broker is None:
            raise RuntimeError("Skill invocation broker is not bound")
        return self._broker

    def descriptors(self) -> Mapping[str, HostedChildDescriptor]:
        return self._require_broker().descriptors()

    def descriptors_for_context(
        self,
        root_context: Any,
    ) -> Mapping[str, HostedChildDescriptor]:
        return self._require_broker().descriptors_for_context(root_context)

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any,
    ) -> Any:
        return await self._require_broker().invoke(tool_name, arguments, context)

    async def route_model(self, **arguments: Any) -> Any:
        return await self._require_broker().route_model(**arguments)

    async def observe_child_result(self, **arguments: Any) -> None:
        await self._require_broker().observe_child_result(**arguments)


class HostedSkillInvocationBroker:
    """Invoke graph children through the same FunctionTools as direct calls."""

    def __init__(
        self,
        descriptors: list[AgentSkillDescriptor],
        tools: list[Any],
        *,
        model_route_profiles: Mapping[str, HostedModelRouteProfile] | None = None,
    ) -> None:
        descriptor_by_name = {
            descriptor.tool_name: descriptor for descriptor in descriptors
        }
        tool_by_name: dict[str, FunctionTool] = {}
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                continue
            if tool.name in tool_by_name:
                raise ValueError(f"duplicate hosted Skill tool {tool.name!r}")
            tool_by_name[tool.name] = tool

        self._provider_lifecycle_tool = tool_by_name.get(
            _PROVIDER_LIFECYCLE_TOOL_NAME
        )

        self._tools: dict[str, FunctionTool] = {}
        self._descriptors: dict[str, HostedChildDescriptor] = {}
        for tool_name, descriptor in descriptor_by_name.items():
            if (
                tool_name == "run_limited_graph"
                or descriptor.skill_type == "limited_graph"
                or descriptor.safety_class == "MANUAL_ONLY"
            ):
                continue
            tool = tool_by_name.get(tool_name)
            if tool is None:
                continue
            self._tools[tool_name] = tool
            self._descriptors[tool_name] = HostedChildDescriptor(
                tool_name=tool_name,
                skill_type=descriptor.skill_type,
                safety_class=descriptor.safety_class,
                input_schema=copy.deepcopy(descriptor.input_schema),
                output_schema=copy.deepcopy(descriptor.output_schema),
                compact_pointers=descriptor.result_tiers.compact_pointers,
                expected_latency=descriptor.expected_latency,
            )
        self._model_route_profiles = dict(model_route_profiles or {})

    def descriptors(self) -> Mapping[str, HostedChildDescriptor]:
        return dict(self._descriptors)

    def descriptors_for_context(
        self,
        root_context: Any,
    ) -> Mapping[str, HostedChildDescriptor]:
        if root_context is None:
            return {}
        active_agent = getattr(root_context, "agent", None)
        active_tools = getattr(active_agent, "tools", None)
        if not isinstance(active_tools, (list, tuple)):
            return {}
        active_names = {
            str(getattr(tool, "name", ""))
            for tool in active_tools
            if getattr(tool, "name", None)
        }
        return {
            name: descriptor
            for name, descriptor in self._descriptors.items()
            if name in active_names
        }

    async def observe_child_result(
        self,
        *,
        node_id: str,
        tool_name: str,
        attempt: int,
        result: Any,
        context: Any,
    ) -> None:
        """Publish safe child visuals without granting graph authority."""

        del node_id, tool_name, attempt, context
        sink = _hosted_child_event_sink.get()
        if sink is None:
            return
        for evidence in visual_evidences_from_result(result):
            await sink("visual.evidence.created", evidence)

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any,
    ) -> Any:
        available = self.descriptors_for_context(context.root_context)
        if tool_name not in available:
            raise RuntimeError(
                f"child Skill {tool_name!r} is unavailable in the active Agent route"
            )
        tool = self._tools[tool_name]
        validate(instance=arguments, schema=available[tool_name].input_schema)

        child_call_id = str(context.child_call_id or "")
        if not child_call_id:
            raise RuntimeError("graph child call identity is missing")
        from limited_graph import ChildAuthorizationRequired

        preparation_trace: list[dict[str, Any]] = []
        completed_handover_keys: set[tuple[str, str, str | None]] = set()
        active_call_id = child_call_id

        while True:
            try:
                raw_result = await self._invoke_function_tool(
                    tool,
                    tool_name=tool_name,
                    arguments=arguments,
                    call_id=active_call_id,
                    context=context,
                )
            except Exception as error:
                if (
                    available[tool_name].physical
                    and getattr(error, "physical_action_submitted", None) is False
                ):
                    from limited_graph import ChildPhysicalActionNotSubmitted

                    raise ChildPhysicalActionNotSubmitted(
                        tool_name,
                        active_call_id,
                        _bounded_error_text(error),
                    ) from error
                raise
            continuation = _provider_handover_continuation(
                raw_result,
                physical=available[tool_name].physical,
            )
            if continuation is None:
                return _child_result_with_trace(raw_result, preparation_trace)

            provider_id = continuation["provider_id"]
            required_capability = continuation["required_capability"]
            handover_key = (provider_id, "hot", required_capability)
            if handover_key in completed_handover_keys:
                preparation_trace.append(
                    {
                        "event": "PROVIDER_HANDOVER_FAILED",
                        "provider_id": provider_id,
                        "requested_action": "HOT",
                        "required_capability": required_capability,
                        "reason": "REPEATED_PROVIDER_CONTINUATION",
                    }
                )
                return _child_result_with_trace(raw_result, preparation_trace)
            if len(completed_handover_keys) >= _MAX_PROVIDER_HANDOVERS_PER_CHILD:
                preparation_trace.append(
                    {
                        "event": "PROVIDER_HANDOVER_FAILED",
                        "provider_id": provider_id,
                        "requested_action": "HOT",
                        "required_capability": required_capability,
                        "reason": "PROVIDER_HANDOVER_LIMIT_EXHAUSTED",
                    }
                )
                return _child_result_with_trace(raw_result, preparation_trace)

            lifecycle_tool = self._provider_lifecycle_tool
            lifecycle_call_id = (
                f"{child_call_id}:provider:{len(completed_handover_keys) + 1}"
            )
            preparation_trace.append(
                {
                    "event": "PROVIDER_HANDOVER_STARTED",
                    "provider_id": provider_id,
                    "requested_action": "HOT",
                    "required_capability": required_capability,
                    "lifecycle_call_id": lifecycle_call_id,
                }
            )
            if lifecycle_tool is None:
                preparation_trace.append(
                    {
                        "event": "PROVIDER_HANDOVER_FAILED",
                        "provider_id": provider_id,
                        "requested_action": "HOT",
                        "required_capability": required_capability,
                        "lifecycle_call_id": lifecycle_call_id,
                        "reason": "PROVIDER_LIFECYCLE_TOOL_UNAVAILABLE",
                    }
                )
                return _child_result_with_trace(raw_result, preparation_trace)

            try:
                lifecycle_result = await self._invoke_function_tool(
                    lifecycle_tool,
                    tool_name=_PROVIDER_LIFECYCLE_TOOL_NAME,
                    arguments=continuation,
                    call_id=lifecycle_call_id,
                    context=context,
                    reserve_timeout_margin=True,
                )
            except ChildAuthorizationRequired:
                raise
            except Exception as error:
                preparation_trace.append(
                    {
                        "event": "PROVIDER_HANDOVER_FAILED",
                        "provider_id": provider_id,
                        "requested_action": "HOT",
                        "required_capability": required_capability,
                        "lifecycle_call_id": lifecycle_call_id,
                        "reason": _bounded_error_text(error),
                    }
                )
                return _child_result_with_trace(raw_result, preparation_trace)

            lifecycle_evidence = _normalize_host_result(lifecycle_result)
            lifecycle_failure = _provider_handover_failure_reason(
                lifecycle_evidence,
                continuation,
            )
            if lifecycle_failure is not None:
                preparation_trace.append(
                    {
                        "event": "PROVIDER_HANDOVER_FAILED",
                        "provider_id": provider_id,
                        "requested_action": "HOT",
                        "required_capability": required_capability,
                        "lifecycle_call_id": lifecycle_call_id,
                        "reason": lifecycle_failure,
                    }
                )
                return _child_result_with_trace(raw_result, preparation_trace)

            completed_handover_keys.add(handover_key)
            preparation_trace.append(
                {
                    "event": "PROVIDER_HANDOVER_COMPLETED",
                    "provider_id": provider_id,
                    "requested_action": "HOT",
                    "required_capability": required_capability,
                    "lifecycle_call_id": lifecycle_call_id,
                }
            )
            active_call_id = (
                f"{child_call_id}:resume:{len(completed_handover_keys)}"
            )

    async def _invoke_function_tool(
        self,
        tool: FunctionTool,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
        context: Any,
        reserve_timeout_margin: bool = False,
    ) -> Any:
        validate(instance=arguments, schema=tool.params_json_schema)
        raw_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        root_context = context.root_context
        try:
            from agents.tool_context import ToolContext

            child_context = ToolContext.from_agent_context(
                root_context,
                call_id,
                agent=getattr(root_context, "agent", None),
                tool_name=tool_name,
                tool_arguments=raw_arguments,
                run_config=getattr(root_context, "run_config", None),
            )
        except (AttributeError, TypeError):
            child_context = _HostedChildToolContext(
                context=getattr(root_context, "context", None),
                tool_name=tool_name,
                tool_call_id=call_id,
                tool_arguments=raw_arguments,
                agent=getattr(root_context, "agent", None),
                run_config=getattr(root_context, "run_config", None),
            )

        enabled = tool.is_enabled
        if callable(enabled):
            evaluated_enabled = enabled(
                child_context,
                getattr(root_context, "agent", None),
            )
            if inspect.isawaitable(evaluated_enabled):
                evaluated_enabled = await evaluated_enabled
            enabled = bool(evaluated_enabled)
        if not enabled:
            from limited_graph import ChildInvocationNotStarted

            raise ChildInvocationNotStarted(
                tool_name,
                call_id,
                "the FunctionTool is disabled in the active Agent context",
            )

        approval_required = tool.needs_approval
        if callable(approval_required):
            evaluated = approval_required(
                child_context,
                copy.deepcopy(arguments),
                call_id,
            )
            if inspect.isawaitable(evaluated):
                evaluated = await evaluated
            approval_required = bool(evaluated)
        if approval_required:
            from limited_graph import ChildAuthorizationRequired

            raise ChildAuthorizationRequired(
                tool_name,
                call_id,
                "the exact FunctionTool invocation requires authorization",
            )

        remaining = float(context.deadline_monotonic or 0.0) - time.monotonic()
        configured_timeout = tool.timeout_seconds
        timeout_s = remaining
        if configured_timeout is not None:
            timeout_s = min(timeout_s, float(configured_timeout))
        if reserve_timeout_margin:
            timeout_s -= _PROVIDER_HANDOVER_TIMEOUT_MARGIN_S
        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            from limited_graph import ChildInvocationTimeout

            raise ChildInvocationTimeout(tool_name, call_id, 0.0)
        try:
            async with asyncio.timeout(timeout_s):
                return await tool.on_invoke_tool(child_context, raw_arguments)
        except TimeoutError as error:
            from limited_graph import ChildInvocationTimeout

            raise ChildInvocationTimeout(
                tool_name,
                call_id,
                timeout_s,
            ) from error

    async def route_model(
        self,
        *,
        routing_profile: str,
        modality: str,
        instruction: str,
        inputs: dict[str, Any],
        routes: list[dict[str, str]],
        context: Any,
    ) -> Any:
        profile = self._model_route_profiles.get(routing_profile)
        if profile is None:
            raise RuntimeError(
                f"model routing profile {routing_profile!r} is not configured"
            )
        if profile.modality != modality:
            raise RuntimeError(
                f"model routing profile {routing_profile!r} does not support {modality}"
            )
        raw_decision = profile.invoke(
            instruction=instruction,
            inputs=copy.deepcopy(inputs),
            routes=copy.deepcopy(routes),
            context=_HostedModelRouteContext(
                graph_run_id=str(getattr(context, "graph_run_id", "")),
                graph_sha256=str(getattr(context, "graph_sha256", "")),
                node_id=str(getattr(context, "node_id", "") or ""),
                child_call_id=str(
                    getattr(context, "child_call_id", "") or ""
                ),
                deadline_monotonic=float(
                    getattr(context, "deadline_monotonic", 0.0) or 0.0
                ),
            ),
        )
        if inspect.isawaitable(raw_decision):
            raw_decision = await raw_decision

        from limited_graph import ModelRouteDecision

        if isinstance(raw_decision, ModelRouteDecision):
            return raw_decision
        if not isinstance(raw_decision, Mapping):
            raise TypeError("model route profile returned an invalid decision")
        return ModelRouteDecision(
            edge_id=str(raw_decision.get("edge_id") or ""),
            confidence=float(raw_decision.get("confidence")),
            provenance=dict(raw_decision.get("provenance") or {}),
        )


def _normalize_host_result(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value is None or isinstance(value, (bool, int, float, list, dict)):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return str(value)


def _provider_handover_continuation(
    raw_result: Any,
    *,
    physical: bool,
) -> dict[str, Any] | None:
    result = _normalize_host_result(raw_result)
    if not isinstance(result, dict) or result.get("workflow_complete") is not False:
        return None
    if physical:
        if result.get("physical_motion_authorized") is not False:
            return None
        for field in (
            "physical_motion_requested",
            "physical_motion_submitted",
            "physical_motion_completed",
        ):
            if result.get(field) is True:
                return None

    required_next_tool = result.get("required_next_tool")
    if not isinstance(required_next_tool, dict):
        return None
    if required_next_tool.get("name") != _PROVIDER_LIFECYCLE_TOOL_NAME:
        return None
    if set(required_next_tool) != {"name", "arguments"}:
        return None
    arguments = required_next_tool.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {
        "provider_id",
        "action",
        "required_capability",
    }:
        return None
    provider_id = arguments.get("provider_id")
    action = arguments.get("action")
    required_capability = arguments.get("required_capability")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    if not isinstance(action, str) or action.strip().lower() != "hot":
        return None
    if required_capability is not None and (
        not isinstance(required_capability, str)
        or not required_capability.strip()
    ):
        return None
    return {
        "provider_id": provider_id.strip(),
        "action": "hot",
        "required_capability": (
            required_capability.strip()
            if isinstance(required_capability, str)
            else None
        ),
    }


def _provider_handover_failure_reason(
    lifecycle_result: Any,
    requested: dict[str, Any],
) -> str | None:
    if not isinstance(lifecycle_result, dict):
        return "INVALID_PROVIDER_LIFECYCLE_RESULT"
    if lifecycle_result.get("lifecycle_request_accepted") is not True:
        return "PROVIDER_LIFECYCLE_REQUEST_NOT_ACCEPTED"
    if lifecycle_result.get("lifecycle_request_complete") is not True:
        return "PROVIDER_LIFECYCLE_REQUEST_INCOMPLETE"
    if lifecycle_result.get("provider_id") != requested["provider_id"]:
        return "PROVIDER_LIFECYCLE_IDENTITY_MISMATCH"
    if str(lifecycle_result.get("requested_action") or "").lower() != "hot":
        return "PROVIDER_LIFECYCLE_ACTION_MISMATCH"
    if lifecycle_result.get("required_capability") != requested[
        "required_capability"
    ]:
        return "PROVIDER_LIFECYCLE_CAPABILITY_MISMATCH"
    readiness = lifecycle_result.get("readiness")
    if isinstance(readiness, dict) and readiness.get("status") != "READY":
        return "PROVIDER_NOT_READY"
    return None


def _child_result_with_trace(
    result: Any,
    preparation_trace: list[dict[str, Any]],
) -> Any:
    if not preparation_trace:
        return result
    from limited_graph import ChildInvocationResult

    return ChildInvocationResult(
        result=result,
        preparation_trace=tuple(copy.deepcopy(preparation_trace)),
    )


def _bounded_error_text(error: BaseException) -> str:
    from limited_graph.validation import redact_credential_values

    message = str(redact_credential_values(str(error)))
    if len(message) > 300:
        message = message[:297] + "..."
    return f"{type(error).__name__}: {message}"


@dataclass
class BoundMethodSkillAdapter:
    invoke_method: Callable[[dict[str, Any]], Awaitable[Any]]

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self.invoke_method(arguments)


def normalize_skill_result(result: Any) -> dict[str, Any]:
    """Normalize one agent-visible Skill result to a JSON object."""

    normalized = result
    if isinstance(result, str):
        try:
            normalized = json.loads(result)
        except json.JSONDecodeError as error:
            raise ValueError("Skill result must be a JSON object") from error
    if not isinstance(normalized, dict):
        raise ValueError("Skill result must be a JSON object")
    return normalized


def validate_skill_result(result: Any, output_schema: dict[str, Any]) -> None:
    """Validate one normalized result without changing its public encoding."""

    validate(instance=normalize_skill_result(result), schema=output_schema)


def build_agent_tools(
    descriptors: list[AgentSkillDescriptor],
    adapters: dict[str, SkillExecutionAdapter],
    *,
    eligible_tool_names: set[str],
    detail_store: SkillResultDetailStore | None = None,
    defer_loading: bool = False,
    adapter_timeout_s: float = 60.0,
    adapter_timeout_overrides_s: dict[str, float] | None = None,
    approval_overrides: dict[
        str,
        bool
        | Callable[[Any, dict[str, Any], str], Awaitable[bool]],
    ]
    | None = None,
) -> list[FunctionTool]:
    """Build only explicitly eligible tools after adapter registration checks."""

    by_name = {descriptor.tool_name: descriptor for descriptor in descriptors}
    missing_descriptors = sorted(eligible_tool_names - by_name.keys())
    if missing_descriptors:
        raise ValueError(
            "eligible tools are missing discoverable manifests: "
            + ", ".join(missing_descriptors)
        )

    timeout_overrides = dict(adapter_timeout_overrides_s or {})
    approval_overrides = dict(approval_overrides or {})
    invalid_timeout_tools = sorted(
        tool_name
        for tool_name, timeout_s in timeout_overrides.items()
        if tool_name not in eligible_tool_names or float(timeout_s) <= 0.0
    )
    if invalid_timeout_tools:
        raise ValueError(
            "adapter timeout overrides must be positive and target eligible "
            "tools: "
            + ", ".join(invalid_timeout_tools)
        )
    invalid_approval_tools = sorted(
        set(approval_overrides) - eligible_tool_names
    )
    if invalid_approval_tools:
        raise ValueError(
            "approval overrides must target eligible tools: "
            + ", ".join(invalid_approval_tools)
        )

    tools: list[FunctionTool] = []
    for tool_name in sorted(eligible_tool_names):
        descriptor = by_name[tool_name]
        adapter = adapters.get(descriptor.execution_adapter_id)
        if adapter is None:
            raise ValueError(
                f"{tool_name} has no registered execution adapter "
                f"{descriptor.execution_adapter_id}"
            )
        selected_timeout_s = float(
            timeout_overrides.get(
                descriptor.tool_name,
                max(
                    adapter_timeout_s,
                    _LATENCY_TIMEOUT_FLOORS_S.get(
                        descriptor.expected_latency,
                        adapter_timeout_s,
                    ),
                ),
            )
        )

        async def invoke_tool(
            _context,
            raw_arguments: str,
            *,
            selected_adapter: SkillExecutionAdapter = adapter,
            selected_descriptor: AgentSkillDescriptor = descriptor,
            effective_timeout_s: float = selected_timeout_s,
        ) -> Any:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Skill tool arguments must be a JSON object")
            is_limited_graph = (
                selected_descriptor.tool_name == "run_limited_graph"
            )

            async def encode_result(result: Any) -> str:
                compact_result = await finalize_skill_result(
                    result,
                    selected_descriptor,
                    detail_store,
                )
                return json.dumps(
                    compact_result,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )

            if is_limited_graph:
                authoring_arguments = arguments
                try:
                    graph = arguments.get("graph")
                    if (
                        isinstance(graph, dict)
                        and "authoring_version" in graph
                    ):
                        validate(
                            instance=arguments,
                            schema=limited_graph_authoring_input_schema(),
                        )
                    arguments = compile_limited_graph_arguments(arguments)
                    validate(
                        instance=arguments,
                        schema=selected_descriptor.input_schema,
                    )
                except (ValidationError, ValueError) as error:
                    if not _consume_graph_authoring_correction():
                        raise
                    return await encode_result(
                        _graph_authoring_failure_result(
                            authoring_arguments,
                            error,
                        )
                    )
            else:
                validate(
                    instance=arguments,
                    schema=selected_descriptor.input_schema,
                )
            extend_current_operation_hard_timeout(
                effective_timeout_s,
                stage=f"skill:{selected_descriptor.tool_name}:running",
            )
            context_invoke = getattr(selected_adapter, "invoke_with_context", None)
            try:
                if callable(context_invoke):
                    result = await context_invoke(arguments, _context)
                else:
                    result = await selected_adapter.invoke(arguments)
            except GraphValidationError as error:
                if not is_limited_graph or not _consume_graph_authoring_correction():
                    raise
                return await encode_result(
                    _graph_authoring_failure_result(arguments, error)
                )
            return await encode_result(result)

        tools.append(
            FunctionTool(
                name=descriptor.tool_name,
                description=(
                    f"{descriptor.description} Complete structured-result "
                    f"pointers: {describe_output_schema(descriptor.output_schema)}. "
                    "Compact graph-bindable pointers: "
                    f"{', '.join(descriptor.result_tiers.compact_pointers) or '(none)'}. "
                    "A completed result includes an opaque detail reference "
                    "for explicit full-output inspection."
                    + (
                        LIMITED_GRAPH_AUTHORING_GUIDANCE
                        if descriptor.tool_name == "run_limited_graph"
                        else ""
                    )
                ),
                params_json_schema=(
                    limited_graph_authoring_input_schema()
                    if descriptor.tool_name == "run_limited_graph"
                    else descriptor.input_schema
                ),
                on_invoke_tool=invoke_tool,
                strict_json_schema=True,
                needs_approval=approval_overrides.get(
                    descriptor.tool_name,
                    descriptor.invocation_requires_approval,
                ),
                timeout_seconds=selected_timeout_s,
                timeout_behavior="raise_exception",
                defer_loading=bool(
                    defer_loading
                    and descriptor.tool_name != "run_limited_graph"
                ),
            )
        )
    return tools
