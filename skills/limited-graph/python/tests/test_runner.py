from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import validate as validate_json

from limited_graph import (
    ChildAuthorizationRequired,
    ChildDescriptor,
    ChildInvocationResult,
    ChildPhysicalActionNotSubmitted,
    GraphCallContext,
    GraphValidationError,
    LimitedGraphRunner,
    ModelRouteDecision,
    validate_graph,
)


_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {},
    },
    "required": ["value"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {},
        "ok": {"type": "boolean"},
        "workflow_complete": {"type": "boolean"},
        "physical_motion_completed": {"type": "boolean"},
    },
    "required": [],
    "additionalProperties": True,
}


class FakeBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], GraphCallContext]] = []
        self.failures_remaining = 0
        self.authorization_required = False
        self.invalid_result = False
        self.model_decision = ModelRouteDecision(
            edge_id="accept",
            confidence=0.9,
            provenance={"backend": "fake"},
        )
        self._descriptors = {
            "echo_value": ChildDescriptor(
                tool_name="echo_value",
                skill_type="echo",
                safety_class="READ_ONLY",
                input_schema=_OBJECT_SCHEMA,
                output_schema=_OUTPUT_SCHEMA,
            ),
            "move_once": ChildDescriptor(
                tool_name="move_once",
                skill_type="motion",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
                input_schema=_OBJECT_SCHEMA,
                output_schema=_OUTPUT_SCHEMA,
            ),
            "run_limited_graph": ChildDescriptor(
                tool_name="run_limited_graph",
                skill_type="limited_graph",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                output_schema=_OUTPUT_SCHEMA,
            ),
        }

    def descriptors(self):
        return self._descriptors

    async def invoke(self, tool_name, arguments, context):
        self.calls.append((tool_name, copy.deepcopy(arguments), context))
        if self.authorization_required:
            raise ChildAuthorizationRequired(
                tool_name,
                str(context.child_call_id),
                "exact child authorization is required",
            )
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("transient failure")
        if self.invalid_result:
            return ["not", "an", "object"]
        return {"value": arguments["value"], "ok": True}

    async def route_model(self, **_arguments):
        return self.model_decision


def terminal(node_id: str, status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "TERMINAL",
        "skill": None,
        "switch": None,
        "model_route": None,
        "terminal": {"status": status, "message": node_id},
    }


def skill(
    node_id: str,
    tool_name: str,
    *,
    arguments_json: str = '{"value":null}',
    bindings: list[dict[str, Any]] | None = None,
    max_attempts: int = 1,
    retry_condition: dict[str, Any] | None = None,
    next_node: str = "done",
    failure_node: str = "failed",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": "SKILL",
        "skill": {
            "tool_name": tool_name,
            "arguments_json": arguments_json,
            "bindings": bindings or [],
            "max_attempts": max_attempts,
            "retry_condition": retry_condition,
            "next_node": next_node,
            "failure_node": failure_node,
        },
        "switch": None,
        "model_route": None,
        "terminal": None,
    }


def limits(**overrides: Any) -> dict[str, Any]:
    result = {
        "max_active_runtime_s": 5.0,
        "max_transitions": 20,
        "max_visits_per_node": 5,
        "max_model_routes": 2,
        "max_physical_actions": 2,
        "max_retained_result_bytes": 65536,
    }
    result.update(overrides)
    return result


def graph(nodes: list[dict[str, Any]], *, initial=None, **limit_overrides):
    return {
        "schema_version": 1,
        "name": "test graph",
        "start_node": nodes[0]["id"],
        "initial_values": initial or [],
        "nodes": nodes,
        "limits": limits(**limit_overrides),
    }


def test_linear_binding_and_terminal_completion() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "echo",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/value",
                        "source_kind": "INITIAL",
                        "source_name": "requested",
                        "source_node_id": None,
                        "source_pointer": "/number",
                    }
                ],
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        initial=[{"name": "requested", "value_json": '{"number":7}'}],
    )

    result = asyncio.run(
        LimitedGraphRunner(broker).run(payload, root_call_id="root-1")
    )

    assert result["status"] == "COMPLETED"
    assert result["node_results"]["echo"] == {"value": 7, "ok": True}
    assert broker.calls[0][2].root_call_id == "root-1"
    assert broker.calls[0][2].node_id == "echo"
    result_schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "limited_graph_result.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    validate_json(instance=result, schema=result_schema)


def test_validated_child_result_is_observed_before_graph_returns() -> None:
    broker = FakeBroker()
    observed: list[dict[str, Any]] = []
    graph_returned = False

    async def observe(**event: Any) -> None:
        observed.append({**event, "graph_returned": graph_returned})

    async def execute() -> dict[str, Any]:
        nonlocal graph_returned
        result = await LimitedGraphRunner(broker).run(
            graph(
                [
                    skill("echo", "echo_value", arguments_json='{"value":7}'),
                    terminal("done"),
                    terminal("failed", "FAILED"),
                ]
            ),
            child_result_observer=observe,
        )
        graph_returned = True
        return result

    result = asyncio.run(execute())

    assert result["status"] == "COMPLETED"
    assert len(observed) == 1
    assert observed[0]["node_id"] == "echo"
    assert observed[0]["tool_name"] == "echo_value"
    assert observed[0]["attempt"] == 1
    assert observed[0]["result"] == {"value": 7, "ok": True}
    assert observed[0]["graph_returned"] is False


def test_child_result_observer_failure_does_not_change_graph_result() -> None:
    async def broken_observer(**_event: Any) -> None:
        raise RuntimeError("presentation observer unavailable")

    result = asyncio.run(
        LimitedGraphRunner(FakeBroker()).run(
            graph(
                [
                    skill("echo", "echo_value", arguments_json='{"value":7}'),
                    terminal("done"),
                    terminal("failed", "FAILED"),
                ]
            ),
            child_result_observer=broken_observer,
        )
    )

    assert result["status"] == "COMPLETED"
    assert result["node_results"]["echo"] == {"value": 7, "ok": True}


def test_node_result_binding_uses_declared_output_pointer() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "source",
                "echo_value",
                arguments_json='{"value":7}',
                next_node="consumer",
            ),
            skill(
                "consumer",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/value",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "source",
                        "source_pointer": "/value",
                    }
                ],
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED"
    assert result["node_results"]["consumer"]["value"] == 7
    assert len(broker.calls) == 2


def test_undeclared_node_result_pointer_fails_before_any_child() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "source",
                "echo_value",
                arguments_json='{"value":7}',
                next_node="consumer",
            ),
            skill(
                "consumer",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/value",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "source",
                        "source_pointer": "/outward_retract_end_position_world_m",
                    }
                ],
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    with pytest.raises(
        GraphValidationError,
        match="undeclared schema path.*outward_retract_end_position_world_m",
    ):
        asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert broker.calls == []


def test_node_result_pointer_outside_compact_tier_fails_preflight() -> None:
    broker = FakeBroker()
    broker._descriptors["echo_value"] = replace(
        broker._descriptors["echo_value"],
        compact_pointers=("/value",),
    )
    payload = graph(
        [
            skill(
                "source",
                "echo_value",
                arguments_json='{"value":7}',
                next_node="consumer",
            ),
            skill(
                "consumer",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/value",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "source",
                        "source_pointer": "/ok",
                    }
                ],
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    with pytest.raises(
        GraphValidationError,
        match="outside the compact Skill result tier.*'/ok'",
    ):
        validate_graph(payload, broker)

    assert broker.calls == []


def test_child_cannot_return_fields_outside_compact_tier() -> None:
    broker = FakeBroker()
    broker._descriptors["echo_value"] = replace(
        broker._descriptors["echo_value"],
        compact_pointers=("/value",),
    )

    result = asyncio.run(
        LimitedGraphRunner(broker).run(
            graph(
                [
                    skill(
                        "echo",
                        "echo_value",
                        arguments_json='{"value":7}',
                    ),
                    terminal("done"),
                    terminal("failed", "FAILED"),
                ]
            )
        )
    )

    assert result["status"] == "FAILED"
    failed = next(
        event
        for event in result["trace"]
        if event["event"] == "CHILD_FAILED"
    )
    assert "undeclared compact path '/ok'" in failed["error"]
    assert result["node_results"] == {}


def test_undeclared_binding_target_fails_before_any_child() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "echo",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/invented_argument",
                        "source_kind": "INITIAL",
                        "source_name": "requested",
                        "source_node_id": None,
                        "source_pointer": "/number",
                    }
                ],
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        initial=[{"name": "requested", "value_json": '{"number":7}'}],
    )

    with pytest.raises(
        GraphValidationError,
        match="binding 0 target points to undeclared schema path",
    ):
        asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert broker.calls == []


def test_physical_output_schema_mismatch_is_unknown_outcome() -> None:
    broker = FakeBroker()
    broker.invalid_result = True
    payload = graph(
        [
            skill(
                "move",
                "move_once",
                arguments_json='{"value":7}',
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        max_physical_actions=1,
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "UNKNOWN_OUTCOME"
    assert result["physical_action_count"] == 1
    assert len(broker.calls) == 1


def test_read_only_exception_retries_and_remains_visible() -> None:
    broker = FakeBroker()
    broker.failures_remaining = 1
    payload = graph(
        [
            skill(
                "echo",
                "echo_value",
                arguments_json='{"value":3}',
                max_attempts=2,
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED_WITH_RETRIES"
    assert result["retry_count"] == 1
    assert len(broker.calls) == 2
    assert any(item["event"] == "CHILD_RETRY" for item in result["trace"])


def test_provider_handover_trace_does_not_count_as_retry_or_extra_motion() -> None:
    class HandoverBroker(FakeBroker):
        async def invoke(self, tool_name, arguments, context):
            self.calls.append((tool_name, copy.deepcopy(arguments), context))
            return ChildInvocationResult(
                result={
                    "workflow_complete": True,
                    "physical_motion_completed": True,
                    "value": arguments["value"],
                },
                preparation_trace=(
                    {
                        "event": "PROVIDER_HANDOVER_STARTED",
                        "provider_id": "robot_arm.primary.integrated",
                        "requested_action": "HOT",
                        "required_capability": "robot_arm.integrated_motion.v1",
                        "lifecycle_call_id": "graph:move:1:provider:1",
                    },
                    {
                        "event": "PROVIDER_HANDOVER_COMPLETED",
                        "provider_id": "robot_arm.primary.integrated",
                        "requested_action": "HOT",
                        "required_capability": "robot_arm.integrated_motion.v1",
                        "lifecycle_call_id": "graph:move:1:provider:1",
                    },
                ),
            )

    broker = HandoverBroker()
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":3}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED"
    assert result["retry_count"] == 0
    assert result["physical_action_count"] == 1
    assert [
        item["event"]
        for item in result["trace"]
        if item["event"].startswith("PROVIDER_HANDOVER_")
    ] == ["PROVIDER_HANDOVER_STARTED", "PROVIDER_HANDOVER_COMPLETED"]


def test_validation_rejects_any_cycle_containing_a_physical_node() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "move",
                "move_once",
                arguments_json='{"value":1}',
                next_node="observe",
            ),
            skill(
                "observe",
                "echo_value",
                arguments_json='{"value":1}',
                next_node="move",
            ),
            terminal("failed", "FAILED"),
        ]
    )

    with pytest.raises(GraphValidationError, match="physical node 'move'"):
        validate_graph(payload, broker)

    assert broker.calls == []


@pytest.mark.parametrize(
    ("tool_name", "child_result", "expected_reason"),
    [
        (
            "echo_value",
            {
                "status": "FAILED",
                "workflow_complete": True,
                "terminal_failure": True,
                "retry_allowed": False,
            },
            "terminal_failure=true",
        ),
        (
            "echo_value",
            {
                "status": "REVIEW_REQUIRED",
                "workflow_complete": False,
                "required_next_tool": "review_candidate",
            },
            "workflow_complete=false",
        ),
        (
            "move_once",
            {
                "status": "IK_PREVIEW_REJECTED",
                "workflow_complete": False,
                "physical_motion_completed": False,
            },
            "workflow_complete=false",
        ),
        (
            "move_once",
            {
                "status": "MOTION_NOT_COMPLETED",
                "workflow_complete": True,
                "physical_motion_completed": False,
            },
            "physical_motion_completed=false",
        ),
    ],
)
def test_explicit_incomplete_child_result_follows_failure_edge(
    tool_name: str,
    child_result: dict[str, Any],
    expected_reason: str,
) -> None:
    broker = FakeBroker()

    async def invoke(_tool_name, _arguments, context):
        broker.calls.append((_tool_name, copy.deepcopy(_arguments), context))
        return copy.deepcopy(child_result)

    broker.invoke = invoke
    payload = graph(
        [
            skill("child", tool_name, arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "FAILED"
    assert result["terminal_node"] == "failed"
    assert result["last_completed_node"] is None
    incomplete = next(
        item
        for item in result["trace"]
        if item["event"] == "CHILD_RESULT_INCOMPLETE"
    )
    assert incomplete["reason"] == expected_reason
    assert incomplete["result_status"] == child_result["status"]


def test_completed_physical_result_still_follows_success_edge() -> None:
    broker = FakeBroker()

    async def invoke(_tool_name, _arguments, context):
        broker.calls.append((_tool_name, copy.deepcopy(_arguments), context))
        return {
            "status": "MOTION_COMPLETED",
            "workflow_complete": True,
            "physical_motion_completed": True,
        }

    broker.invoke = invoke
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED"
    assert result["terminal_node"] == "done"
    assert result["last_completed_node"] == "move"


def test_switch_and_model_route_select_declared_edges() -> None:
    broker = FakeBroker()
    switch_node = {
        "id": "choose",
        "kind": "SWITCH",
        "skill": None,
        "switch": {
            "source_kind": "INITIAL",
            "source_name": "choice",
            "source_node_id": None,
            "cases": [
                {
                    "condition": {
                        "source_pointer": "",
                        "operator": "EQ",
                        "expected_json": '"model"',
                    },
                    "target_node": "model",
                }
            ],
            "default_target": "failed",
        },
        "model_route": None,
        "terminal": None,
    }
    model_node = {
        "id": "model",
        "kind": "MODEL_ROUTE",
        "skill": None,
        "switch": None,
        "model_route": {
            "routing_profile": "FAST_TEXT",
            "modality": "TEXT",
            "instruction": "Choose a route.",
            "inputs": [
                {
                    "name": "choice",
                    "source_kind": "INITIAL",
                    "source_name": "choice",
                    "source_node_id": None,
                    "source_pointer": "",
                }
            ],
            "routes": [
                {"edge_id": "accept", "description": "Accept", "target_node": "done"},
                {"edge_id": "reject", "description": "Reject", "target_node": "failed"},
            ],
            "minimum_confidence": 0.8,
            "fallback_target": "failed",
        },
        "terminal": None,
    }
    payload = graph(
        [switch_node, model_node, terminal("done"), terminal("failed", "FAILED")],
        initial=[{"name": "choice", "value_json": '"model"'}],
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED"
    assert result["model_route_count"] == 1
    assert any(item["event"] == "MODEL_ROUTE_SELECTED" for item in result["trace"])


def test_loop_stops_at_visit_limit() -> None:
    broker = FakeBroker()
    loop = {
        "id": "loop",
        "kind": "SWITCH",
        "skill": None,
        "switch": {
            "source_kind": "INITIAL",
            "source_name": "again",
            "source_node_id": None,
            "cases": [
                {
                    "condition": {
                        "source_pointer": "",
                        "operator": "TRUTHY",
                        "expected_json": None,
                    },
                    "target_node": "loop",
                }
            ],
            "default_target": "done",
        },
        "model_route": None,
        "terminal": None,
    }
    payload = graph(
        [loop, terminal("done")],
        initial=[{"name": "again", "value_json": "true"}],
        max_visits_per_node=2,
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "LIMIT_EXHAUSTED"
    assert result["limit"] == "max_visits_per_node"
    assert result["node_visits"]["loop"] == 3


def test_nested_graph_and_physical_retry_are_rejected() -> None:
    broker = FakeBroker()
    nested = graph(
        [skill("nested", "run_limited_graph"), terminal("done"), terminal("failed")]
    )
    repeated_motion = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}', max_attempts=2),
            terminal("done"),
            terminal("failed"),
        ]
    )

    with pytest.raises(GraphValidationError, match="cannot invoke a graph"):
        validate_graph(nested, broker)
    with pytest.raises(GraphValidationError, match="retry only a READ_ONLY"):
        validate_graph(repeated_motion, broker)


def test_credential_like_argument_key_is_rejected() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill("echo", "echo_value", arguments_json='{"value":1,"api_key":"x"}'),
            terminal("done"),
            terminal("failed"),
        ]
    )

    with pytest.raises(GraphValidationError, match="credential-like key"):
        validate_graph(payload, broker)


def test_credential_like_initial_text_is_rejected() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill("echo", "echo_value", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        initial=[{"name": "unsafe", "value_json": '"token=must-not-enter"'}],
    )

    with pytest.raises(GraphValidationError, match="credential-like text"):
        validate_graph(payload, broker)


def test_physical_exception_is_unknown_and_never_retried() -> None:
    broker = FakeBroker()
    broker.failures_remaining = 1
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "UNKNOWN_OUTCOME"
    assert len(broker.calls) == 1


def test_proven_pre_submission_rejection_follows_failure_edge() -> None:
    broker = FakeBroker()

    async def reject_before_submission(tool_name, arguments, context):
        broker.calls.append((tool_name, copy.deepcopy(arguments), context))
        raise ChildPhysicalActionNotSubmitted(
            tool_name,
            str(context.child_call_id),
            "preview rejected before physical submission",
        )

    broker.invoke = reject_before_submission
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "FAILED"
    assert result["terminal_node"] == "failed"
    assert result["physical_action_count"] == 0
    assert result["last_failure"] == {
        "kind": "CHILD_PHYSICAL_ACTION_NOT_SUBMITTED",
        "node_id": "move",
        "tool_name": "move_once",
        "reason": "preview rejected before physical submission",
        "physical_action_submitted": False,
    }
    assert len(broker.calls) == 1
    rejection = next(
        item
        for item in result["trace"]
        if item["event"] == "CHILD_PHYSICAL_ACTION_NOT_SUBMITTED"
    )
    assert rejection["node_id"] == "move"
    assert rejection["failure_node"] == "failed"


def test_declared_output_paths_cover_retry_and_branching_sources() -> None:
    broker = FakeBroker()
    invalid_retry = graph(
        [
            skill(
                "echo",
                "echo_value",
                max_attempts=2,
                retry_condition={
                    "source_pointer": "/invented",
                    "operator": "TRUTHY",
                    "expected_json": None,
                },
            ),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )
    switch_node = {
        "id": "route",
        "kind": "SWITCH",
        "skill": None,
        "switch": {
            "source_kind": "NODE_RESULT",
            "source_name": None,
            "source_node_id": "echo",
            "source_pointer": "",
            "cases": [
                {
                    "condition": {
                        "source_pointer": "/invented",
                        "operator": "TRUTHY",
                        "expected_json": None,
                    },
                    "target_node": "done",
                }
            ],
            "default_target": "failed",
        },
        "model_route": None,
        "terminal": None,
    }
    invalid_switch = graph(
        [
            skill("echo", "echo_value", next_node="route"),
            switch_node,
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )
    model_node = {
        "id": "route",
        "kind": "MODEL_ROUTE",
        "skill": None,
        "switch": None,
        "model_route": {
            "routing_profile": "FAST_TEXT",
            "modality": "TEXT",
            "instruction": "Choose a declared edge.",
            "inputs": [
                {
                    "name": "decision",
                    "source_kind": "NODE_RESULT",
                    "source_name": None,
                    "source_node_id": "echo",
                    "source_pointer": "/invented",
                }
            ],
            "routes": [
                {
                    "edge_id": "accept",
                    "description": "Accept",
                    "target_node": "done",
                },
                {
                    "edge_id": "reject",
                    "description": "Reject",
                    "target_node": "failed",
                },
            ],
            "minimum_confidence": 0.8,
            "fallback_target": "failed",
        },
        "terminal": None,
    }
    invalid_model_input = graph(
        [
            skill("echo", "echo_value", next_node="route"),
            model_node,
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    for payload in (invalid_retry, invalid_switch, invalid_model_input):
        with pytest.raises(
            GraphValidationError,
            match="undeclared schema path.*invented",
        ):
            validate_graph(payload, broker)

    assert broker.calls == []


def test_physical_cancellation_is_unknown_and_never_retried() -> None:
    broker = FakeBroker()

    async def cancel(_tool_name, _arguments, _context):
        raise asyncio.CancelledError()

    broker.invoke = cancel
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "UNKNOWN_OUTCOME"
    assert result["physical_action_count"] == 1


def test_path_dependent_unavailable_result_returns_failed_trace() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill(
                "first",
                "echo_value",
                bindings=[
                    {
                        "target_pointer": "/value",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "later",
                        "source_pointer": "/value",
                    }
                ],
                failure_node="later",
            ),
            terminal("done"),
            skill(
                "later",
                "echo_value",
                arguments_json='{"value":2}',
                next_node="failed",
                failure_node="failed",
            ),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "FAILED"
    assert any(item["event"] == "GRAPH_DATA_ERROR" for item in result["trace"])
    assert broker.calls == []


def test_authorization_stops_before_child_action() -> None:
    broker = FakeBroker()
    broker.authorization_required = True
    payload = graph(
        [
            skill("echo", "echo_value", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "AUTHORIZATION_REQUIRED"
    assert result["authorization"]["tool_name"] == "echo_value"


def test_child_result_credentials_are_redacted_before_trace() -> None:
    broker = FakeBroker()

    async def invoke_with_secret(tool_name, arguments, context):
        broker.calls.append((tool_name, copy.deepcopy(arguments), context))
        return {"value": arguments["value"], "api_token": "must-not-leak"}

    broker.invoke = invoke_with_secret
    payload = graph(
        [
            skill("echo", "echo_value", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ]
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["node_results"]["echo"]["api_token"] == "[REDACTED]"
    assert "must-not-leak" not in str(result)


def test_physical_result_is_validated_before_authorization_redaction() -> None:
    broker = FakeBroker()
    broker._descriptors["move_once"] = ChildDescriptor(
        tool_name="move_once",
        skill_type="motion",
        safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
        input_schema=_OBJECT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "value": {},
                "authorization": {"type": "object"},
            },
            "required": ["value", "authorization"],
            "additionalProperties": False,
        },
    )

    async def invoke_with_authorization(tool_name, arguments, context):
        broker.calls.append((tool_name, copy.deepcopy(arguments), context))
        return {
            "value": arguments["value"],
            "authorization": {
                "decision_id": "decision-1",
                "signed_action_token": "must-not-leak",
            },
        }

    broker.invoke = invoke_with_authorization
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        max_physical_actions=1,
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "COMPLETED"
    assert result["physical_action_count"] == 1
    assert result["node_results"]["move"]["authorization"] == "[REDACTED]"
    assert "must-not-leak" not in str(result)


def test_invalid_physical_result_does_not_expose_authorization_value() -> None:
    broker = FakeBroker()
    broker._descriptors["move_once"] = ChildDescriptor(
        tool_name="move_once",
        skill_type="motion",
        safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
        input_schema=_OBJECT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "value": {},
                "authorization": {"type": "object"},
            },
            "required": ["value", "authorization"],
            "additionalProperties": False,
        },
    )

    async def invoke_with_invalid_authorization(tool_name, arguments, context):
        broker.calls.append((tool_name, copy.deepcopy(arguments), context))
        return {
            "value": arguments["value"],
            "authorization": "must-not-leak",
        }

    broker.invoke = invoke_with_invalid_authorization
    payload = graph(
        [
            skill("move", "move_once", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed", "FAILED"),
        ],
        max_physical_actions=1,
    )

    result = asyncio.run(LimitedGraphRunner(broker).run(payload))

    assert result["status"] == "UNKNOWN_OUTCOME"
    assert "/authorization" in result["message"]
    assert "must-not-leak" not in str(result)


def test_digest_is_stable_for_equivalent_graph_objects() -> None:
    broker = FakeBroker()
    payload = graph(
        [
            skill("echo", "echo_value", arguments_json='{"value":1}'),
            terminal("done"),
            terminal("failed"),
        ]
    )
    reordered = {key: payload[key] for key in reversed(list(payload))}

    assert validate_graph(payload, broker).sha256 == validate_graph(reordered, broker).sha256
