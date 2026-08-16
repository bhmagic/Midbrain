from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from typing import Any

from jsonschema import validate as validate_json
from jsonschema.exceptions import ValidationError

from .bindings import (
    apply_bindings,
    condition_matches,
    decode_json,
    source_value,
)
from .models import (
    ChildAuthorizationRequired,
    ChildInvocationNotStarted,
    ChildInvocationResult,
    ChildInvocationTimeout,
    ChildSkillBroker,
    GraphCallContext,
    GraphValidationError,
    ModelRouteDecision,
)
from .validation import (
    ValidatedGraph,
    redact_credential_values,
    validate_graph,
)


class LimitedGraphRunner:
    """Execute one immutable, sequential, host-bounded Skill graph."""

    def __init__(self, broker: ChildSkillBroker):
        self.broker = broker

    async def run(
        self,
        graph: dict[str, Any],
        *,
        root_context: Any = None,
        root_call_id: str = "",
    ) -> dict[str, Any]:
        validated = validate_graph(
            graph,
            self.broker,
            root_context=root_context,
        )
        run_id = uuid.uuid4().hex
        started = time.monotonic()
        limits = validated.graph["limits"]
        deadline = started + float(limits["max_active_runtime_s"])
        context = GraphCallContext(
            graph_run_id=run_id,
            graph_sha256=validated.sha256,
            root_call_id=str(root_call_id or ""),
            root_context=root_context,
            deadline_monotonic=deadline,
        )
        state = _RunState(validated, context, started)

        current = str(validated.graph["start_node"])
        while True:
            exceeded = state.check_before_node(current, time.monotonic())
            if exceeded is not None:
                return state.limit_result(exceeded, time.monotonic())

            node = validated.nodes[current]
            kind = node["kind"]
            state.trace_event("NODE_STARTED", node_id=current, kind=kind)
            if kind == "TERMINAL":
                terminal = node["terminal"]
                status = str(terminal["status"])
                if status == "COMPLETED" and state.retry_count:
                    status = "COMPLETED_WITH_RETRIES"
                state.trace_event(
                    "NODE_COMPLETED",
                    node_id=current,
                    kind=kind,
                    status=status,
                )
                state.trace_event("GRAPH_TERMINATED", node_id=current, status=status)
                return state.final_result(
                    status=status,
                    terminal_node=current,
                    message=str(terminal.get("message") or ""),
                    now=time.monotonic(),
                )

            try:
                if kind == "SKILL":
                    outcome = await self._run_skill(node, state, deadline)
                    if isinstance(outcome, dict):
                        return outcome
                    next_node = outcome
                elif kind == "SWITCH":
                    next_node = self._run_switch(node, state)
                elif kind == "MODEL_ROUTE":
                    next_node = await self._run_model_route(node, state, deadline)
                else:  # pragma: no cover - validation owns this invariant.
                    raise GraphValidationError(f"unsupported node kind {kind!r}")
            except GraphValidationError as error:
                state.trace_event(
                    "GRAPH_DATA_ERROR",
                    node_id=current,
                    kind=kind,
                    error=_safe_error(error),
                )
                return state.final_result(
                    status="FAILED",
                    terminal_node=None,
                    message=_safe_error(error),
                    now=time.monotonic(),
                )

            state.trace_event(
                "NODE_COMPLETED",
                node_id=current,
                kind=kind,
                next_node=next_node,
            )
            exceeded = state.transition(current, next_node)
            if exceeded is not None:
                return state.limit_result(exceeded, time.monotonic())
            current = next_node

    async def _run_skill(
        self,
        node: dict[str, Any],
        state: "_RunState",
        deadline: float,
    ) -> str | dict[str, Any]:
        node_id = str(node["id"])
        config = node["skill"]
        tool_name = str(config["tool_name"])
        descriptor = state.validated.descriptors[tool_name]
        literal = decode_json(
            config["arguments_json"],
            field=f"node {node_id}.arguments_json",
        )
        assert isinstance(literal, dict)
        arguments = apply_bindings(
            literal,
            config.get("bindings") or [],
            initial_values=state.validated.initial_values,
            node_results=state.node_results,
            node_id=node_id,
        )
        try:
            validate_json(instance=arguments, schema=descriptor.input_schema)
        except ValidationError as error:
            state.trace_event(
                "CHILD_ARGUMENTS_INVALID",
                node_id=node_id,
                tool_name=tool_name,
                error=_safe_error(error),
            )
            return str(config["failure_node"])

        if descriptor.physical:
            if state.physical_actions >= int(
                state.validated.graph["limits"]["max_physical_actions"]
            ):
                return state.limit_result("max_physical_actions", time.monotonic())
            state.physical_actions += 1

        max_attempts = int(config["max_attempts"])
        for attempt in range(1, max_attempts + 1):
            child_call_id = f"{state.context.graph_run_id}:{node_id}:{attempt}"
            child_context = state.context.child(
                node_id=node_id,
                attempt=attempt,
                child_call_id=child_call_id,
                deadline_monotonic=deadline,
            )
            state.trace_event(
                "CHILD_ATTEMPT_STARTED",
                node_id=node_id,
                tool_name=tool_name,
                attempt=attempt,
                child_call_id=child_call_id,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return state.limit_result("max_active_runtime_s", time.monotonic())
            try:
                async with asyncio.timeout(remaining):
                    raw_result = await self.broker.invoke(
                        tool_name,
                        copy.deepcopy(arguments),
                        child_context,
                    )
                if isinstance(raw_result, ChildInvocationResult):
                    for preparation_event in raw_result.preparation_trace:
                        safe_event = redact_credential_values(
                            copy.deepcopy(preparation_event)
                        )
                        event_name = str(
                            safe_event.pop("event", "PROVIDER_HANDOVER_EVENT")
                        )
                        state.trace_event(
                            event_name,
                            node_id=node_id,
                            tool_name=tool_name,
                            attempt=attempt,
                            **safe_event,
                        )
                    raw_result = raw_result.result
                result = redact_credential_values(_normalize_result(raw_result))
                exceeded = state.retain_result(node_id, result)
                if exceeded is not None:
                    return state.limit_result(exceeded, time.monotonic())
                state.trace_event(
                    "CHILD_ATTEMPT_COMPLETED",
                    node_id=node_id,
                    tool_name=tool_name,
                    attempt=attempt,
                    result=state.trace_payload(result),
                )
                incomplete_reason = _explicit_incomplete_reason(
                    result,
                    physical=descriptor.physical,
                )
                if incomplete_reason is not None:
                    failure_node = str(config["failure_node"])
                    state.trace_event(
                        "CHILD_RESULT_INCOMPLETE",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        reason=incomplete_reason,
                        result_status=(
                            str(result.get("status") or "")
                            if isinstance(result, dict)
                            else ""
                        ),
                        failure_node=failure_node,
                    )
                    return failure_node
                retry_condition = config.get("retry_condition")
                retry_requested = bool(
                    retry_condition is not None
                    and condition_matches(
                        result,
                        retry_condition,
                        field=f"node {node_id} retry_condition",
                    )
                )
                if retry_requested:
                    if attempt < max_attempts:
                        state.retry_count += 1
                        state.trace_event(
                            "CHILD_RETRY",
                            node_id=node_id,
                            tool_name=tool_name,
                            attempt=attempt,
                            reason="RESULT_CONDITION",
                        )
                        continue
                    state.trace_event(
                        "CHILD_RETRY_EXHAUSTED",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                    )
                    return str(config["failure_node"])
                state.last_completed_node = node_id
                return str(config["next_node"])
            except ChildAuthorizationRequired as error:
                if descriptor.physical:
                    state.physical_actions -= 1
                state.trace_event(
                    "AUTHORIZATION_REQUIRED",
                    node_id=node_id,
                    tool_name=tool_name,
                    authorization_tool_name=error.tool_name,
                    child_call_id=error.child_call_id,
                    reason=error.reason,
                )
                return state.final_result(
                    status="AUTHORIZATION_REQUIRED",
                    terminal_node=None,
                    message=error.reason,
                    now=time.monotonic(),
                    authorization={
                        "tool_name": error.tool_name,
                        "child_call_id": error.child_call_id,
                    },
                )
            except ChildInvocationNotStarted as error:
                if descriptor.physical:
                    state.physical_actions -= 1
                state.trace_event(
                    "CHILD_NOT_STARTED",
                    node_id=node_id,
                    tool_name=tool_name,
                    child_call_id=error.child_call_id,
                    reason=error.reason,
                )
                return str(config["failure_node"])
            except (ChildInvocationTimeout, TimeoutError) as error:
                if descriptor.physical:
                    state.trace_event(
                        "UNKNOWN_OUTCOME",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        error=_safe_error(error),
                    )
                    return state.final_result(
                        status="UNKNOWN_OUTCOME",
                        terminal_node=None,
                        message=_safe_error(error),
                        now=time.monotonic(),
                    )
                if descriptor.read_only and attempt < max_attempts:
                    state.retry_count += 1
                    state.trace_event(
                        "CHILD_RETRY",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        reason="TIMEOUT",
                    )
                    continue
                state.trace_event(
                    "CHILD_FAILED",
                    node_id=node_id,
                    tool_name=tool_name,
                    attempt=attempt,
                    error=_safe_error(error),
                )
                return str(config["failure_node"])
            except asyncio.CancelledError:
                if descriptor.physical:
                    state.trace_event(
                        "UNKNOWN_OUTCOME",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        error="CancelledError: physical child call was cancelled",
                    )
                    return state.final_result(
                        status="UNKNOWN_OUTCOME",
                        terminal_node=None,
                        message="physical child call was cancelled",
                        now=time.monotonic(),
                    )
                raise
            except Exception as error:
                if descriptor.physical:
                    state.trace_event(
                        "UNKNOWN_OUTCOME",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        error=_safe_error(error),
                    )
                    return state.final_result(
                        status="UNKNOWN_OUTCOME",
                        terminal_node=None,
                        message=_safe_error(error),
                        now=time.monotonic(),
                    )
                if descriptor.read_only and attempt < max_attempts:
                    state.retry_count += 1
                    state.trace_event(
                        "CHILD_RETRY",
                        node_id=node_id,
                        tool_name=tool_name,
                        attempt=attempt,
                        reason=_safe_error(error),
                    )
                    continue
                state.trace_event(
                    "CHILD_FAILED",
                    node_id=node_id,
                    tool_name=tool_name,
                    attempt=attempt,
                    error=_safe_error(error),
                )
                return str(config["failure_node"])

        return str(config["failure_node"])  # pragma: no cover

    @staticmethod
    def _run_switch(node: dict[str, Any], state: "_RunState") -> str:
        node_id = str(node["id"])
        config = node["switch"]
        value = source_value(
            config,
            initial_values=state.validated.initial_values,
            node_results=state.node_results,
            field=f"node {node_id} switch source",
        )
        target = str(config["default_target"])
        selected_case: int | None = None
        for index, case in enumerate(config["cases"]):
            if condition_matches(
                value,
                case["condition"],
                field=f"node {node_id} case {index}",
            ):
                target = str(case["target_node"])
                selected_case = index
                break
        state.last_completed_node = node_id
        state.trace_event(
            "SWITCH_SELECTED",
            node_id=node_id,
            target_node=target,
            case_index=selected_case,
        )
        return target

    async def _run_model_route(
        self,
        node: dict[str, Any],
        state: "_RunState",
        deadline: float,
    ) -> str:
        node_id = str(node["id"])
        config = node["model_route"]
        if state.model_routes >= int(
            state.validated.graph["limits"]["max_model_routes"]
        ):
            target = str(config["fallback_target"])
            state.last_completed_node = node_id
            state.trace_event(
                "MODEL_ROUTE_FALLBACK",
                node_id=node_id,
                target_node=target,
                reason="max_model_routes is exhausted",
            )
            return target
        state.model_routes += 1
        inputs = {
            str(item["name"]): source_value(
                item,
                initial_values=state.validated.initial_values,
                node_results=state.node_results,
                field=f"node {node_id} model input {item['name']}",
            )
            for item in config["inputs"]
        }
        fallback = str(config["fallback_target"])
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0.0:
                raise TimeoutError("graph active runtime exhausted")
            async with asyncio.timeout(remaining):
                decision = await self.broker.route_model(
                    routing_profile=str(config["routing_profile"]),
                    modality=str(config["modality"]),
                    instruction=str(config["instruction"]),
                    inputs=inputs,
                    routes=copy.deepcopy(config["routes"]),
                    context=state.context.child(
                        node_id=node_id,
                        attempt=1,
                        child_call_id=f"{state.context.graph_run_id}:{node_id}:model",
                        deadline_monotonic=deadline,
                    ),
                )
            if not isinstance(decision, ModelRouteDecision):
                raise TypeError("model router returned an invalid decision type")
            routes = {str(route["edge_id"]): route for route in config["routes"]}
            route = routes.get(decision.edge_id)
            if (
                route is None
                or not math.isfinite(float(decision.confidence))
                or float(decision.confidence) < float(config["minimum_confidence"])
            ):
                raise ValueError("model route was unknown or below confidence")
            target = str(route["target_node"])
            state.trace_event(
                "MODEL_ROUTE_SELECTED",
                node_id=node_id,
                edge_id=decision.edge_id,
                confidence=float(decision.confidence),
                target_node=target,
                provenance=state.trace_payload(
                    redact_credential_values(decision.provenance)
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            target = fallback
            state.trace_event(
                "MODEL_ROUTE_FALLBACK",
                node_id=node_id,
                target_node=target,
                reason=_safe_error(error),
            )
        state.last_completed_node = node_id
        return target


class _RunState:
    def __init__(
        self,
        validated: ValidatedGraph,
        context: GraphCallContext,
        started: float,
    ):
        self.validated = validated
        self.context = context
        self.started = started
        self.trace: list[dict[str, Any]] = []
        self.node_results: dict[str, Any] = {}
        self.visits: dict[str, int] = {}
        self.transitions = 0
        self.retry_count = 0
        self.model_routes = 0
        self.physical_actions = 0
        self.retained_result_bytes = 0
        self.last_completed_node: str | None = None

    def trace_event(self, event: str, **payload: Any) -> None:
        self.trace.append(
            {
                "sequence": len(self.trace) + 1,
                "event": event,
                "elapsed_ms": round((time.monotonic() - self.started) * 1000.0, 3),
                **copy.deepcopy(payload),
            }
        )

    def trace_payload(self, value: Any) -> Any:
        raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(raw) <= 16384:
            return copy.deepcopy(value)
        return {
            "omitted": True,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def check_before_node(self, node_id: str, now: float) -> str | None:
        if now - self.started >= float(
            self.validated.graph["limits"]["max_active_runtime_s"]
        ):
            return "max_active_runtime_s"
        visits = self.visits.get(node_id, 0) + 1
        self.visits[node_id] = visits
        if visits > int(self.validated.graph["limits"]["max_visits_per_node"]):
            return "max_visits_per_node"
        return None

    def transition(self, source: str, target: str) -> str | None:
        self.transitions += 1
        self.trace_event("EDGE_SELECTED", source_node=source, target_node=target)
        if self.transitions > int(self.validated.graph["limits"]["max_transitions"]):
            return "max_transitions"
        return None

    def retain_result(self, node_id: str, value: Any) -> str | None:
        raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        maximum = int(
            self.validated.graph["limits"]["max_retained_result_bytes"]
        )
        if self.retained_result_bytes + len(raw) > maximum:
            return "max_retained_result_bytes"
        self.retained_result_bytes += len(raw)
        self.node_results[node_id] = copy.deepcopy(value)
        return None

    def limit_result(self, limit: str, now: float) -> dict[str, Any]:
        self.trace_event("LIMIT_EXHAUSTED", limit=limit)
        return self.final_result(
            status="LIMIT_EXHAUSTED",
            terminal_node=None,
            message=f"Limited Graph exhausted {limit}",
            now=now,
            limit=limit,
        )

    def final_result(
        self,
        *,
        status: str,
        terminal_node: str | None,
        message: str,
        now: float,
        limit: str | None = None,
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": "midbrain.limited_graph.result",
            "schema_version": 1,
            "status": status,
            "message": message,
            "graph_run_id": self.context.graph_run_id,
            "graph_sha256": self.validated.sha256,
            "graph_name": self.validated.graph["name"],
            "terminal_node": terminal_node,
            "last_completed_node": self.last_completed_node,
            "active_runtime_ms": round((now - self.started) * 1000.0, 3),
            "transition_count": self.transitions,
            "node_visits": copy.deepcopy(self.visits),
            "retry_count": self.retry_count,
            "model_route_count": self.model_routes,
            "physical_action_count": self.physical_actions,
            "retained_result_bytes": self.retained_result_bytes,
            "limit": limit,
            "authorization": copy.deepcopy(authorization),
            "trace": copy.deepcopy(self.trace),
            "node_results": copy.deepcopy(self.node_results),
        }


def _explicit_incomplete_reason(
    result: Any,
    *,
    physical: bool,
) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("workflow_complete") is False:
        return "workflow_complete=false"
    if physical and result.get("physical_motion_completed") is False:
        return "physical_motion_completed=false"
    return None


def _normalize_result(value: Any) -> Any:
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


def _safe_error(error: BaseException) -> str:
    message = str(redact_credential_values(str(error)))
    if len(message) > 500:
        message = message[:497] + "..."
    return f"{type(error).__name__}: {message}"
