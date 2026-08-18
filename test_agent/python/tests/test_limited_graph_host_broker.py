from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents import FunctionTool

from limited_graph import (
    ChildAuthorizationRequired,
    ChildInvocationNotStarted,
    ChildInvocationResult,
    ChildPhysicalActionNotSubmitted,
    ChildDescriptor,
    GraphValidationError,
    validate_graph,
)
from limited_graph.host_adapter import LimitedGraphHostAdapter
from physical_agent_test.skill_catalog import (
    AgentSkillDescriptor,
    SkillResultTierPolicy,
)
from physical_agent_test.skill_catalog import discover_agent_skills
from physical_agent_test.skill_execution import (
    BoundMethodSkillAdapter,
    HostedModelRouteProfile,
    HostedSkillInvocationBroker,
    SkillInvocationBrokerHandle,
    build_agent_tools,
    reset_graph_authoring_repair_state,
    reset_hosted_child_event_sink,
    set_graph_authoring_repair_state,
    set_hosted_child_event_sink,
)
from physical_agent_test.skill_result_details import SkillResultDetailStore


_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"value": {}},
    "required": ["value"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {},
        "status": {"type": "string"},
        "workflow_complete": {"type": "boolean"},
        "physical_motion_authorized": {"type": "boolean"},
        "physical_motion_submitted": {"type": "boolean"},
        "physical_motion_completed": {"type": "boolean"},
        "required_next_tool": {"type": ["object", "null"]},
    },
    "required": [],
    "additionalProperties": True,
}

_PROVIDER_LIFECYCLE_SCHEMA = {
    "type": "object",
    "properties": {
        "provider_id": {"type": "string"},
        "action": {"type": "string", "enum": ["start", "hot", "warm", "stop"]},
        "required_capability": {"type": ["string", "null"]},
    },
    "required": ["provider_id", "action", "required_capability"],
    "additionalProperties": False,
}


def provider_continuation(
    *,
    provider_id: str = "robot_arm.primary.integrated",
    capability: str | None = "robot_arm.integrated_motion.v1",
) -> dict[str, Any]:
    return {
        "workflow_complete": False,
        "physical_motion_authorized": False,
        "physical_motion_submitted": False,
        "required_next_tool": {
            "name": "set_provider_residency",
            "arguments": {
                "provider_id": provider_id,
                "action": "hot",
                "required_capability": capability,
            },
        },
    }


def lifecycle_complete(
    *,
    provider_id: str = "robot_arm.primary.integrated",
    capability: str | None = "robot_arm.integrated_motion.v1",
) -> dict[str, Any]:
    return {
        "lifecycle_request_accepted": True,
        "lifecycle_request_complete": True,
        "provider_id": provider_id,
        "requested_action": "HOT",
        "required_capability": capability,
        "readiness": {"status": "READY"},
    }


def descriptor(
    tool_name: str,
    *,
    safety_class: str = "READ_ONLY",
    skill_type: str = "test",
) -> AgentSkillDescriptor:
    return AgentSkillDescriptor(
        skill_type=skill_type,
        skill_version="1.0.0",
        display_name=tool_name,
        manifest_path=f"skills/{tool_name}/manifest.json",
        schema_version=3,
        discoverable=True,
        tool_name=tool_name,
        description="A test finite Skill with a typed invocation contract.",
        when_to_use=("A host broker test needs it.",),
        when_not_to_use=(),
        side_effects=(),
        safety_class=safety_class,
        expected_latency="LOW",
        required_permissions=(),
        input_schema=_INPUT_SCHEMA,
        output_schema=_OUTPUT_SCHEMA,
        result_tiers=SkillResultTierPolicy(
            schema_version=1,
            compact_pointers=(
                "/value",
                "/status",
                "/workflow_complete",
                "/physical_motion_authorized",
                "/physical_motion_submitted",
                "/physical_motion_completed",
                "/required_next_tool",
            ),
            detail_policy="HOST_SANITIZED_REFERENCE",
            max_compact_bytes=16384,
        ),
        execution_adapter_id=f"skill.{tool_name}.v1",
        execution_adapter_kind="IN_PROCESS_BOUND_INSTANCE",
        execution_entrypoint=None,
        host_adapter_entrypoint=None,
        host_adapter_factory=None,
        invocation_requires_approval=(safety_class != "READ_ONLY"),
        required_capabilities=(),
        optional_capabilities=(),
        route_policy=None,
        disabled_reason=None,
    )


def test_catalog_schemas_preflight_slice_point_offset_motion_chain() -> None:
    workspace = Path(__file__).resolve().parents[3]
    selected_names = {
        "slice_with_blade",
        "offset_world_point",
        "move_effector_to_world_point",
    }
    descriptors = {
        item.tool_name: ChildDescriptor(
            tool_name=item.tool_name,
            skill_type=item.skill_type,
            safety_class=item.safety_class,
            input_schema=item.input_schema,
            output_schema=item.output_schema,
            compact_pointers=item.result_tiers.compact_pointers,
            expected_latency=item.expected_latency,
        )
        for item in discover_agent_skills(workspace)
        if item.tool_name in selected_names
    }

    class CatalogBroker:
        def descriptors(self):
            return descriptors

    def graph_skill(
        node_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        bindings: list[dict[str, Any]],
        next_node: str,
    ) -> dict[str, Any]:
        return {
            "id": node_id,
            "kind": "SKILL",
            "skill": {
                "tool_name": tool_name,
                "arguments_json": json.dumps(arguments),
                "bindings": bindings,
                "max_attempts": 1,
                "retry_condition": None,
                "next_node": next_node,
                "failure_node": "failed",
            },
            "switch": None,
            "model_route": None,
            "terminal": None,
        }

    payload = {
        "schema_version": 1,
        "name": "slice point offset preflight",
        "start_node": "slice1",
        "initial_values": [],
        "nodes": [
            graph_skill(
                "slice1",
                "slice_with_blade",
                {
                    "point_mode": "RELATIVE_TO_CURRENT_EFFECTOR_WORLD",
                    "slice_begin_point_m": [0.0, 0.0, -0.1],
                    "blade_direction_world": [0.0, 0.0, -1.0],
                    "slicing_direction_world": [-1.0, 0.0, 0.0],
                    "slice_length_m": 0.2,
                    "blade_profile_number": None,
                    "motion_profile_number": None,
                    "integrated_execution_backend": "IMPEDANCE",
                },
                [],
                "offset",
            ),
            graph_skill(
                "offset",
                "offset_world_point",
                {
                    "source_position_world_m": [0.0, 0.0, 0.0],
                    "source_world_frame_id": None,
                    "source_observed_at_us": None,
                    "source_session_epoch": None,
                    "offset_vector": [0.0, 0.0, 10.0],
                    "offset_unit": "CENTIMETRES",
                    "offset_reference": "ACTIVE_WORLD",
                },
                [
                    {
                        "target_pointer": "/source_position_world_m",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "slice1",
                        "source_pointer": "/plan/path/slice_begin_point_world_m",
                    },
                    {
                        "target_pointer": "/source_world_frame_id",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "slice1",
                        "source_pointer": "/plan/workcell_binding/world_frame",
                    },
                ],
                "move",
            ),
            graph_skill(
                "move",
                "move_effector_to_world_point",
                {
                    "target_position_world_m": [0.0, 0.0, 0.0],
                    "target_world_frame_id": None,
                    "target_session_epoch": None,
                    "requested_speed_m_s": None,
                    "execution_backend": "IMPEDANCE",
                },
                [
                    {
                        "target_pointer": "/target_position_world_m",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "offset",
                        "source_pointer": "/target_position_world_m",
                    },
                    {
                        "target_pointer": "/target_world_frame_id",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "offset",
                        "source_pointer": "/target_world_frame_id",
                    },
                    {
                        "target_pointer": "/target_session_epoch",
                        "source_kind": "NODE_RESULT",
                        "source_name": None,
                        "source_node_id": "offset",
                        "source_pointer": "/target_session_epoch",
                    },
                ],
                "done",
            ),
            {
                "id": "done",
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {"status": "COMPLETED", "message": "done"},
            },
            {
                "id": "failed",
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {"status": "FAILED", "message": "failed"},
            },
        ],
        "limits": {
            "max_active_runtime_s": 30.0,
            "max_transitions": 8,
            "max_visits_per_node": 1,
            "max_model_routes": 0,
            "max_physical_actions": 2,
            "max_retained_result_bytes": 262144,
        },
    }

    validated = validate_graph(payload, CatalogBroker())

    assert set(validated.descriptors) == selected_names


class ContextAwareAdapter:
    def __init__(self) -> None:
        self.context: Any = None

    async def invoke(self, _arguments: dict[str, Any]) -> Any:
        raise AssertionError("context-aware invocation was not selected")

    async def invoke_with_context(
        self,
        arguments: dict[str, Any],
        context: Any,
    ) -> Any:
        self.context = context
        return arguments


def test_external_adapter_receives_original_function_tool_context() -> None:
    selected_descriptor = descriptor("echo_value")
    adapter = ContextAwareAdapter()
    tool = build_agent_tools(
        [selected_descriptor],
        {selected_descriptor.execution_adapter_id: adapter},
        eligible_tool_names={"echo_value"},
    )[0]
    root_context = SimpleNamespace(tool_call_id="root-call")

    result = asyncio.run(
        tool.on_invoke_tool(root_context, '{"value":7}')
    )

    assert json.loads(result) == {
        "value": 7,
        "detail_ref": {
            "schema": "midbrain.skill_result_detail_ref",
            "schema_version": 1,
            "available": False,
            "reason": "DETAIL_STORE_NOT_CONFIGURED",
        },
    }
    assert adapter.context is root_context


def test_direct_skill_tool_validates_declared_output_schema() -> None:
    async def invalid_result(_arguments: dict[str, Any]) -> str:
        return "not-json"

    selected_descriptor = descriptor("echo_value")
    tool = build_agent_tools(
        [selected_descriptor],
        {
            selected_descriptor.execution_adapter_id: BoundMethodSkillAdapter(
                invalid_result
            )
        },
        eligible_tool_names={"echo_value"},
    )[0]

    with pytest.raises(ValueError, match="Skill result must be a JSON object"):
        asyncio.run(tool.on_invoke_tool(SimpleNamespace(), '{"value":1}'))


def test_skill_tool_description_publishes_declared_result_pointers() -> None:
    selected_descriptor = descriptor("echo_value")
    tool = build_agent_tools(
        [selected_descriptor],
        {
            selected_descriptor.execution_adapter_id: BoundMethodSkillAdapter(
                ContextAwareAdapter()
            )
        },
        eligible_tool_names={"echo_value"},
    )[0]

    assert "Complete structured-result pointers" in tool.description
    assert "Compact graph-bindable pointers" in tool.description
    assert "/value" in tool.description


def test_hosted_child_observer_publishes_safe_visual_immediately() -> None:
    observed: list[tuple[str, dict[str, Any]]] = []
    graph_returned = False

    async def sink(event_type: str, payload: dict[str, Any]) -> None:
        observed.append(
            (event_type, {**payload, "graph_returned": graph_returned})
        )

    evidence_id = "live-graph-visual"
    result = {
        "visual_evidence": {
            "schema": "midbrain.visual_evidence",
            "schema_version": 1,
            "evidence_id": evidence_id,
            "title": "Live graph child visual",
            "default_channel": "rgb",
            "channels": [
                {
                    "id": "rgb",
                    "label": "RGB",
                    "url": f"/api/visual-evidence/{evidence_id}/channels/rgb",
                    "media_type": "image/png",
                    "width": 640,
                    "height": 480,
                    "sha256": "a" * 64,
                }
            ],
            "annotations": [],
            "confidence": "high",
            "model": "test-model",
            "source_skill": "test.live.graph",
            "private_path": "must-not-stream",
        }
    }
    broker = HostedSkillInvocationBroker([], [])
    token = set_hosted_child_event_sink(sink)
    try:
        asyncio.run(
            broker.observe_child_result(
                node_id="inspect",
                tool_name="inspect_arm_semantic_scene",
                attempt=1,
                result=result,
                context=SimpleNamespace(graph_run_id="graph-1"),
            )
        )
        graph_returned = True
    finally:
        reset_hosted_child_event_sink(token)

    assert len(observed) == 1
    assert observed[0][0] == "visual.evidence.created"
    assert observed[0][1]["evidence_id"] == evidence_id
    assert observed[0][1]["graph_returned"] is False
    assert "private_path" not in observed[0][1]


def test_broker_preserves_principal_and_intersects_active_route() -> None:
    observed: dict[str, Any] = {}

    async def invoke_echo(context: Any, raw_arguments: str) -> dict[str, Any]:
        observed["context"] = context
        observed["raw_arguments"] = raw_arguments
        return {"ok": True}

    echo_tool = FunctionTool(
        name="echo_value",
        description="Echo one value for a broker unit test.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_echo,
        needs_approval=False,
        timeout_seconds=2.0,
    )
    hidden_tool = FunctionTool(
        name="hidden_value",
        description="A route-hidden test Skill.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_echo,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [descriptor("echo_value"), descriptor("hidden_value")],
        [echo_tool, hidden_tool],
    )
    principal = object()
    active_agent = SimpleNamespace(tools=[echo_tool])
    root_context = SimpleNamespace(
        context=principal,
        agent=active_agent,
        run_config=object(),
    )
    graph_context = SimpleNamespace(
        root_context=root_context,
        child_call_id="graph:node:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("echo_value", {"value": 9}, graph_context)
    )

    assert result == {"ok": True}
    assert observed["context"].context is principal
    assert observed["context"].tool_call_id == "graph:node:1"
    assert set(broker.descriptors_for_context(root_context)) == {"echo_value"}
    with pytest.raises(RuntimeError, match="active Agent route"):
        asyncio.run(
            broker.invoke("hidden_value", {"value": 9}, graph_context)
        )


def test_broker_rechecks_exact_child_authorization_before_invocation() -> None:
    invoked = False

    async def invoke_motion(_context: Any, _raw_arguments: str) -> Any:
        nonlocal invoked
        invoked = True
        return {"physical_motion_completed": True}

    motion_tool = FunctionTool(
        name="move_once",
        description="A physical test Skill.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=True,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool],
    )
    assert set(broker.descriptors()) == {"move_once"}
    root_context = SimpleNamespace(
        context=object(),
        agent=SimpleNamespace(tools=[motion_tool]),
    )
    graph_context = SimpleNamespace(
        root_context=root_context,
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    with pytest.raises(ChildAuthorizationRequired):
        asyncio.run(
            broker.invoke("move_once", {"value": 1}, graph_context)
        )
    assert not invoked


def test_broker_rechecks_dynamic_tool_enablement_before_invocation() -> None:
    invoked = False

    async def invoke_echo(_context: Any, _raw_arguments: str) -> Any:
        nonlocal invoked
        invoked = True
        return {"ok": True}

    async def is_enabled(_context: Any, _agent: Any) -> bool:
        return False

    echo_tool = FunctionTool(
        name="echo_value",
        description="A dynamically disabled test Skill.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_echo,
        needs_approval=False,
        is_enabled=is_enabled,
    )
    broker = HostedSkillInvocationBroker(
        [descriptor("echo_value")],
        [echo_tool],
    )
    root_context = SimpleNamespace(
        context=object(),
        agent=SimpleNamespace(tools=[echo_tool]),
    )
    graph_context = SimpleNamespace(
        root_context=root_context,
        child_call_id="graph:echo:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    with pytest.raises(ChildInvocationNotStarted):
        asyncio.run(
            broker.invoke("echo_value", {"value": 1}, graph_context)
        )
    assert not invoked


def test_broker_maps_trusted_pre_submission_rejection() -> None:
    class PreviewRejected(RuntimeError):
        physical_action_submitted = False

    async def reject_preview(_context: Any, _raw_arguments: str) -> Any:
        raise PreviewRejected("preview rejected before physical submission")

    motion_tool = FunctionTool(
        name="move_once",
        description="A physical test Skill.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=reject_preview,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[motion_tool]),
        ),
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    with pytest.raises(ChildPhysicalActionNotSubmitted) as caught:
        asyncio.run(
            broker.invoke("move_once", {"value": 1}, graph_context)
        )

    assert caught.value.child_call_id == "graph:move:1"
    assert caught.value.reason == "PreviewRejected: preview rejected before physical submission"


def test_broker_completes_exact_provider_handover_then_resumes_child() -> None:
    child_calls: list[tuple[str, dict[str, Any]]] = []
    lifecycle_calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke_motion(context: Any, raw_arguments: str) -> Any:
        arguments = json.loads(raw_arguments)
        child_calls.append((context.tool_call_id, arguments))
        if len(child_calls) == 1:
            return provider_continuation()
        return {
            "workflow_complete": True,
            "physical_motion_completed": True,
        }

    async def invoke_lifecycle(context: Any, raw_arguments: str) -> str:
        lifecycle_calls.append((context.tool_call_id, json.loads(raw_arguments)))
        return json.dumps(lifecycle_complete())

    motion_tool = FunctionTool(
        name="move_once",
        description="A physical child requiring one Provider handover.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Complete one exact Provider lifecycle request.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool, lifecycle_tool],
    )
    root_context = SimpleNamespace(
        context=object(),
        agent=SimpleNamespace(tools=[motion_tool, lifecycle_tool]),
    )
    graph_context = SimpleNamespace(
        root_context=root_context,
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("move_once", {"value": 4}, graph_context)
    )

    assert isinstance(result, ChildInvocationResult)
    assert result.result["workflow_complete"] is True
    assert child_calls == [
        ("graph:move:1", {"value": 4}),
        ("graph:move:1:resume:1", {"value": 4}),
    ]
    assert lifecycle_calls == [
        (
            "graph:move:1:provider:1",
            {
                "provider_id": "robot_arm.primary.integrated",
                "action": "hot",
                "required_capability": "robot_arm.integrated_motion.v1",
            },
        )
    ]
    assert [event["event"] for event in result.preparation_trace] == [
        "PROVIDER_HANDOVER_STARTED",
        "PROVIDER_HANDOVER_COMPLETED",
    ]


def test_broker_can_switch_two_child_declared_providers_in_sequence() -> None:
    child_call_ids: list[str] = []
    lifecycle_provider_ids: list[str] = []

    async def invoke_child(context: Any, _raw_arguments: str) -> Any:
        child_call_ids.append(context.tool_call_id)
        if len(child_call_ids) == 1:
            return provider_continuation(
                provider_id="perception.sam2_scene_tracker",
                capability="perception.scene.semantic_obstacles",
            )
        if len(child_call_ids) == 2:
            return provider_continuation(
                provider_id="world_model.arm_scene_compiler",
                capability="world_model.arm.semantic_scene",
            )
        return {"workflow_complete": True, "value": 8}

    async def invoke_lifecycle(_context: Any, raw_arguments: str) -> Any:
        arguments = json.loads(raw_arguments)
        lifecycle_provider_ids.append(arguments["provider_id"])
        return lifecycle_complete(
            provider_id=arguments["provider_id"],
            capability=arguments["required_capability"],
        )

    child_tool = FunctionTool(
        name="echo_value",
        description="A child with two ordered Provider prerequisites.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_child,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Complete exact Provider lifecycle requests.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [descriptor("echo_value")],
        [child_tool, lifecycle_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[child_tool, lifecycle_tool]),
        ),
        child_call_id="graph:observe:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("echo_value", {"value": 8}, graph_context)
    )

    assert isinstance(result, ChildInvocationResult)
    assert result.result == {"workflow_complete": True, "value": 8}
    assert child_call_ids == [
        "graph:observe:1",
        "graph:observe:1:resume:1",
        "graph:observe:1:resume:2",
    ]
    assert lifecycle_provider_ids == [
        "perception.sam2_scene_tracker",
        "world_model.arm_scene_compiler",
    ]
    assert [event["event"] for event in result.preparation_trace] == [
        "PROVIDER_HANDOVER_STARTED",
        "PROVIDER_HANDOVER_COMPLETED",
        "PROVIDER_HANDOVER_STARTED",
        "PROVIDER_HANDOVER_COMPLETED",
    ]


def test_broker_preserves_lifecycle_authorization_boundary() -> None:
    child_invocations = 0
    lifecycle_invoked = False

    async def invoke_motion(_context: Any, _raw_arguments: str) -> Any:
        nonlocal child_invocations
        child_invocations += 1
        return provider_continuation()

    async def invoke_lifecycle(_context: Any, _raw_arguments: str) -> Any:
        nonlocal lifecycle_invoked
        lifecycle_invoked = True
        return lifecycle_complete()

    motion_tool = FunctionTool(
        name="move_once",
        description="A physical child requiring one Provider handover.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="An authorization-gated lifecycle request.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=True,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool, lifecycle_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[motion_tool, lifecycle_tool]),
        ),
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    with pytest.raises(ChildAuthorizationRequired) as caught:
        asyncio.run(broker.invoke("move_once", {"value": 4}, graph_context))

    assert caught.value.tool_name == "set_provider_residency"
    assert caught.value.child_call_id == "graph:move:1:provider:1"
    assert child_invocations == 1
    assert not lifecycle_invoked


def test_broker_stops_after_repeated_provider_handover() -> None:
    child_invocations = 0
    lifecycle_invocations = 0

    async def invoke_motion(_context: Any, _raw_arguments: str) -> Any:
        nonlocal child_invocations
        child_invocations += 1
        return provider_continuation()

    async def invoke_lifecycle(_context: Any, _raw_arguments: str) -> Any:
        nonlocal lifecycle_invocations
        lifecycle_invocations += 1
        return lifecycle_complete()

    motion_tool = FunctionTool(
        name="move_once",
        description="A child repeating one Provider prerequisite.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Complete one exact Provider lifecycle request.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool, lifecycle_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[motion_tool, lifecycle_tool]),
        ),
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("move_once", {"value": 4}, graph_context)
    )

    assert isinstance(result, ChildInvocationResult)
    assert child_invocations == 2
    assert lifecycle_invocations == 1
    assert result.preparation_trace[-1]["reason"] == (
        "REPEATED_PROVIDER_CONTINUATION"
    )


def test_broker_returns_child_prerequisite_when_lifecycle_is_incomplete() -> None:
    child_invocations = 0

    async def invoke_motion(_context: Any, _raw_arguments: str) -> Any:
        nonlocal child_invocations
        child_invocations += 1
        return provider_continuation()

    async def invoke_lifecycle(_context: Any, _raw_arguments: str) -> Any:
        result = lifecycle_complete()
        result["lifecycle_request_complete"] = False
        result["readiness"] = {"status": "TIMEOUT"}
        return result

    motion_tool = FunctionTool(
        name="move_once",
        description="A child requiring an unavailable Provider.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Return incomplete Provider readiness evidence.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool, lifecycle_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[motion_tool, lifecycle_tool]),
        ),
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("move_once", {"value": 4}, graph_context)
    )

    assert isinstance(result, ChildInvocationResult)
    assert result.result == provider_continuation()
    assert child_invocations == 1
    assert result.preparation_trace[-1]["reason"] == (
        "PROVIDER_LIFECYCLE_REQUEST_INCOMPLETE"
    )


def test_broker_does_not_handover_after_physical_authorization() -> None:
    lifecycle_invoked = False

    async def invoke_motion(_context: Any, _raw_arguments: str) -> Any:
        result = provider_continuation()
        result["physical_motion_authorized"] = True
        return result

    async def invoke_lifecycle(_context: Any, _raw_arguments: str) -> Any:
        nonlocal lifecycle_invoked
        lifecycle_invoked = True
        return lifecycle_complete()

    motion_tool = FunctionTool(
        name="move_once",
        description="A child with an unsafe post-authorization continuation.",
        params_json_schema=_INPUT_SCHEMA,
        on_invoke_tool=invoke_motion,
        needs_approval=False,
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Complete one exact Provider lifecycle request.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=invoke_lifecycle,
        needs_approval=False,
    )
    broker = HostedSkillInvocationBroker(
        [
            descriptor(
                "move_once",
                safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
            )
        ],
        [motion_tool, lifecycle_tool],
    )
    graph_context = SimpleNamespace(
        root_context=SimpleNamespace(
            context=object(),
            agent=SimpleNamespace(tools=[motion_tool, lifecycle_tool]),
        ),
        child_call_id="graph:move:1",
        deadline_monotonic=time.monotonic() + 2.0,
    )

    result = asyncio.run(
        broker.invoke("move_once", {"value": 4}, graph_context)
    )

    assert not isinstance(result, ChildInvocationResult)
    assert result["physical_motion_authorized"] is True
    assert not lifecycle_invoked


def test_broker_uses_only_named_host_model_profile() -> None:
    async def route(**arguments: Any) -> dict[str, Any]:
        assert arguments["routes"][0]["edge_id"] == "accept"
        return {
            "edge_id": "accept",
            "confidence": 0.91,
            "provenance": {"backend": "local-test"},
        }

    broker = HostedSkillInvocationBroker(
        [],
        [],
        model_route_profiles={
            "FAST_TEXT": HostedModelRouteProfile(
                modality="TEXT",
                invoke=route,
            )
        },
    )
    decision = asyncio.run(
        broker.route_model(
            routing_profile="FAST_TEXT",
            modality="TEXT",
            instruction="Choose.",
            inputs={"value": "yes"},
            routes=[
                {"edge_id": "accept", "description": "Accept"},
                {"edge_id": "reject", "description": "Reject"},
            ],
            context=SimpleNamespace(),
        )
    )

    assert decision.edge_id == "accept"
    assert decision.provenance == {"backend": "local-test"}


def test_graph_function_tool_invokes_child_function_tool_end_to_end() -> None:
    workspace = Path(__file__).resolve().parents[3]
    from physical_agent_test.skill_catalog import discover_agent_skills

    graph_descriptor = next(
        item
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )

    async def echo(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"value": arguments["value"], "source": "direct-function-tool"}

    echo_descriptor = descriptor("echo_value")
    handle = SkillInvocationBrokerHandle()
    tools = build_agent_tools(
        [echo_descriptor, graph_descriptor],
        {
            echo_descriptor.execution_adapter_id: BoundMethodSkillAdapter(echo),
            graph_descriptor.execution_adapter_id: LimitedGraphHostAdapter(handle),
        },
        eligible_tool_names={"echo_value", "run_limited_graph"},
    )
    handle.bind(
        HostedSkillInvocationBroker(
            [echo_descriptor, graph_descriptor],
            tools,
        )
    )
    graph_tool = next(tool for tool in tools if tool.name == "run_limited_graph")
    assert "authoring_version" in graph_tool.params_json_schema["properties"][
        "graph"
    ]["properties"]
    assert "nodes" not in graph_tool.params_json_schema["properties"]["graph"][
        "properties"
    ]
    root_context = SimpleNamespace(
        context=object(),
        tool_call_id="root-graph-call",
        agent=SimpleNamespace(tools=tools),
    )
    graph = {
        "authoring_version": 1,
        "name": "end to end",
        "start": "",
        "initial": [],
        "steps": [
            {
                "id": "echo",
                "tool": "echo_value",
                "args_json": '{"value":11}',
                "bind": [],
            },
        ],
        "edges": [],
        "retries": [],
        "switches": [],
        "model_routes": [],
        "terminals": [],
        "limits": {
            "seconds": 5.0,
            "transitions": 4,
            "visits": 2,
            "model_routes": 0,
            "physical_actions": 0,
            "result_bytes": 4096,
        },
    }

    result = json.loads(asyncio.run(
        graph_tool.on_invoke_tool(
            root_context,
            json.dumps({"graph": graph}),
        )
    ))

    assert result["status"] == "COMPLETED"
    assert result["node_results"]["echo"]["value"] == 11
    assert "source" not in result["node_results"]["echo"]
    assert result["node_results"]["echo"]["detail_ref"]["available"] is False


def test_graph_authoring_error_allows_one_corrected_preexecution_call() -> None:
    workspace = Path(__file__).resolve().parents[3]
    graph_descriptor = next(
        item
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )
    child_invocations = 0

    async def echo(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal child_invocations
        child_invocations += 1
        return {"value": arguments["value"]}

    echo_descriptor = descriptor("echo_value")
    handle = SkillInvocationBrokerHandle()
    tools = build_agent_tools(
        [echo_descriptor, graph_descriptor],
        {
            echo_descriptor.execution_adapter_id: BoundMethodSkillAdapter(echo),
            graph_descriptor.execution_adapter_id: LimitedGraphHostAdapter(handle),
        },
        eligible_tool_names={"echo_value", "run_limited_graph"},
    )
    handle.bind(HostedSkillInvocationBroker([echo_descriptor, graph_descriptor], tools))
    graph_tool = next(tool for tool in tools if tool.name == "run_limited_graph")
    root_context = SimpleNamespace(
        context=object(),
        tool_call_id="root-authoring-repair",
        agent=SimpleNamespace(tools=tools),
    )
    operator_request = "establish the arm base axis by foundation pose"

    def authoring_arguments(value_json: str) -> dict[str, Any]:
        return {
            "graph": {
                "authoring_version": 1,
                "name": "live operator request regression",
                "start": "",
                "initial": [
                    {
                        "name": "operator_request",
                        "value_json": value_json,
                    }
                ],
                "steps": [
                    {
                        "id": "echo",
                        "tool": "echo_value",
                        "args_json": '{"value":""}',
                        "bind": [
                            {
                                "to": "/value",
                                "from": "$operator_request#",
                            }
                        ],
                    }
                ],
                "edges": [],
                "retries": [],
                "switches": [],
                "model_routes": [],
                "terminals": [],
                "limits": {
                    "seconds": 5.0,
                    "transitions": 4,
                    "visits": 2,
                    "model_routes": 0,
                    "physical_actions": 0,
                    "result_bytes": 4096,
                },
            }
        }

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        token = set_graph_authoring_repair_state(maximum_corrections=1)
        try:
            rejected = json.loads(
                await graph_tool.on_invoke_tool(
                    root_context,
                    json.dumps(authoring_arguments(operator_request)),
                )
            )
            corrected = json.loads(
                await graph_tool.on_invoke_tool(
                    root_context,
                    json.dumps(
                        authoring_arguments(json.dumps(operator_request))
                    ),
                )
            )
            return rejected, corrected
        finally:
            reset_graph_authoring_repair_state(token)

    rejected, corrected = asyncio.run(scenario())

    assert rejected["status"] == "AUTHORING_INVALID"
    assert rejected["physical_action_count"] == 0
    assert rejected["transition_count"] == 0
    assert "value_json" in rejected["message"]
    assert child_invocations == 1
    assert corrected["status"] == "COMPLETED"
    assert corrected["node_results"]["echo"]["value"] == operator_request


def test_graph_authoring_second_invalid_call_terminates() -> None:
    workspace = Path(__file__).resolve().parents[3]
    graph_descriptor = next(
        item
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )
    echo_descriptor = descriptor("echo_value")
    handle = SkillInvocationBrokerHandle()
    tools = build_agent_tools(
        [echo_descriptor, graph_descriptor],
        {
            echo_descriptor.execution_adapter_id: BoundMethodSkillAdapter(
                lambda arguments: arguments
            ),
            graph_descriptor.execution_adapter_id: LimitedGraphHostAdapter(handle),
        },
        eligible_tool_names={"echo_value", "run_limited_graph"},
    )
    handle.bind(HostedSkillInvocationBroker([echo_descriptor, graph_descriptor], tools))
    graph_tool = next(tool for tool in tools if tool.name == "run_limited_graph")
    root_context = SimpleNamespace(
        context=object(),
        tool_call_id="root-authoring-limit",
        agent=SimpleNamespace(tools=tools),
    )
    invalid = {
        "graph": {
            "authoring_version": 1,
            "name": "bounded authoring failure",
            "start": "",
            "initial": [{"name": "request", "value_json": "raw text"}],
            "steps": [
                {
                    "id": "echo",
                    "tool": "echo_value",
                    "args_json": '{"value":""}',
                    "bind": [{"to": "/value", "from": "$request#"}],
                }
            ],
            "edges": [],
            "retries": [],
            "switches": [],
            "model_routes": [],
            "terminals": [],
            "limits": {
                "seconds": 5.0,
                "transitions": 4,
                "visits": 2,
                "model_routes": 0,
                "physical_actions": 0,
                "result_bytes": 4096,
            },
        }
    }

    async def scenario() -> dict[str, Any]:
        token = set_graph_authoring_repair_state(maximum_corrections=1)
        try:
            first = json.loads(
                await graph_tool.on_invoke_tool(
                    root_context,
                    json.dumps(invalid),
                )
            )
            with pytest.raises(GraphValidationError, match="not valid JSON"):
                await graph_tool.on_invoke_tool(
                    root_context,
                    json.dumps(invalid),
                )
            return first
        finally:
            reset_graph_authoring_repair_state(token)

    first = asyncio.run(scenario())
    assert first["status"] == "AUTHORING_INVALID"


def test_graph_function_tool_routes_pre_submission_rejection_end_to_end(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[3]
    graph_descriptor = next(
        item
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )
    child_invocations = 0

    class PreviewRejected(RuntimeError):
        physical_action_submitted = False

    async def reject_preview(_arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal child_invocations
        child_invocations += 1
        raise PreviewRejected("preview rejected before physical submission")

    move_descriptor = descriptor(
        "move_once",
        safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
    )
    detail_store = SkillResultDetailStore(
        tmp_path / "details.sqlite3",
        session_id="pre-submission-test",
        maximum_results=20,
        maximum_result_bytes=64 * 1024,
        maximum_total_bytes=256 * 1024,
        retention_days=1,
    )
    handle = SkillInvocationBrokerHandle()
    tools = build_agent_tools(
        [move_descriptor, graph_descriptor],
        {
            move_descriptor.execution_adapter_id: BoundMethodSkillAdapter(
                reject_preview
            ),
            graph_descriptor.execution_adapter_id: LimitedGraphHostAdapter(handle),
        },
        eligible_tool_names={"move_once", "run_limited_graph"},
        detail_store=detail_store,
        approval_overrides={"move_once": False},
    )
    handle.bind(
        HostedSkillInvocationBroker([move_descriptor, graph_descriptor], tools)
    )
    graph_tool = next(tool for tool in tools if tool.name == "run_limited_graph")
    root_context = SimpleNamespace(
        context=object(),
        tool_call_id="root-pre-submission-rejection",
        agent=SimpleNamespace(tools=tools),
    )
    graph = {
        "authoring_version": 1,
        "name": "pre-submission rejection",
        "start": "",
        "initial": [],
        "steps": [
            {
                "id": "move",
                "tool": "move_once",
                "args_json": '{"value":1}',
                "bind": [],
            }
        ],
        "edges": [],
        "retries": [],
        "switches": [],
        "model_routes": [],
        "terminals": [],
        "limits": {
            "seconds": 5.0,
            "transitions": 4,
            "visits": 2,
            "model_routes": 0,
            "physical_actions": 1,
            "result_bytes": 4096,
        },
    }

    result = json.loads(
        asyncio.run(
            graph_tool.on_invoke_tool(
                root_context,
                json.dumps({"graph": graph}),
            )
        )
    )

    assert result["status"] == "FAILED"
    assert result["terminal_node"] == "failed"
    assert result["physical_action_count"] == 0
    assert result["last_failure"]["kind"] == (
        "CHILD_PHYSICAL_ACTION_NOT_SUBMITTED"
    )
    assert result["last_failure"]["node_id"] == "move"
    assert result["last_failure"]["tool_name"] == "move_once"
    assert result["last_failure"]["physical_action_submitted"] is False
    assert "preview rejected" in result["last_failure"]["reason"]
    assert child_invocations == 1
    graph_detail = asyncio.run(
        detail_store.retrieve(result["detail_ref"]["result_id"])
    )
    assert graph_detail is not None
    assert any(
        event["event"] == "CHILD_PHYSICAL_ACTION_NOT_SUBMITTED"
        for event in graph_detail["payload"]["trace"]
    )


def test_graph_function_tool_completes_provider_handover_end_to_end(
    tmp_path: Path,
) -> None:
    workspace = Path(__file__).resolve().parents[3]
    from physical_agent_test.skill_catalog import discover_agent_skills

    graph_descriptor = next(
        item
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )
    child_invocations = 0

    async def move(_arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal child_invocations
        child_invocations += 1
        if child_invocations == 1:
            return provider_continuation()
        return {
            "workflow_complete": True,
            "physical_motion_completed": True,
        }

    async def activate(_context: Any, _raw_arguments: str) -> dict[str, Any]:
        return lifecycle_complete()

    move_descriptor = descriptor(
        "move_once",
        safety_class="PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
    )
    detail_store = SkillResultDetailStore(
        tmp_path / "details.sqlite3",
        session_id="graph-test",
        maximum_results=20,
        maximum_result_bytes=64 * 1024,
        maximum_total_bytes=256 * 1024,
        retention_days=1,
    )
    handle = SkillInvocationBrokerHandle()
    skill_tools = build_agent_tools(
        [move_descriptor, graph_descriptor],
        {
            move_descriptor.execution_adapter_id: BoundMethodSkillAdapter(move),
            graph_descriptor.execution_adapter_id: LimitedGraphHostAdapter(handle),
        },
        eligible_tool_names={"move_once", "run_limited_graph"},
        detail_store=detail_store,
        approval_overrides={"move_once": False},
    )
    lifecycle_tool = FunctionTool(
        name="set_provider_residency",
        description="Complete one exact Provider lifecycle request.",
        params_json_schema=_PROVIDER_LIFECYCLE_SCHEMA,
        on_invoke_tool=activate,
        needs_approval=False,
    )
    offered_tools = [*skill_tools, lifecycle_tool]
    handle.bind(
        HostedSkillInvocationBroker(
            [move_descriptor, graph_descriptor],
            offered_tools,
        )
    )
    graph_tool = next(
        tool for tool in offered_tools if tool.name == "run_limited_graph"
    )
    root_context = SimpleNamespace(
        context=object(),
        tool_call_id="root-graph-handover",
        agent=SimpleNamespace(tools=offered_tools),
    )
    graph = {
        "schema_version": 1,
        "name": "provider handover",
        "start_node": "move",
        "initial_values": [],
        "nodes": [
            {
                "id": "move",
                "kind": "SKILL",
                "skill": {
                    "tool_name": "move_once",
                    "arguments_json": '{"value":11}',
                    "bindings": [],
                    "max_attempts": 1,
                    "retry_condition": None,
                    "next_node": "done",
                    "failure_node": "failed",
                },
                "switch": None,
                "model_route": None,
                "terminal": None,
            },
            {
                "id": "done",
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {"status": "COMPLETED", "message": "done"},
            },
            {
                "id": "failed",
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {"status": "FAILED", "message": "failed"},
            },
        ],
        "limits": {
            "max_active_runtime_s": 5.0,
            "max_transitions": 4,
            "max_visits_per_node": 2,
            "max_model_routes": 0,
            "max_physical_actions": 1,
            "max_retained_result_bytes": 4096,
        },
    }

    result = json.loads(asyncio.run(
        graph_tool.on_invoke_tool(
            root_context,
            json.dumps({"graph": graph}),
        )
    ))

    assert result["status"] == "COMPLETED"
    assert result["physical_action_count"] == 1
    assert child_invocations == 2
    graph_detail = asyncio.run(
        detail_store.retrieve(result["detail_ref"]["result_id"])
    )
    assert graph_detail is not None
    assert [
        event["event"]
        for event in graph_detail["payload"]["trace"]
        if event["event"].startswith("PROVIDER_HANDOVER_")
    ] == ["PROVIDER_HANDOVER_STARTED", "PROVIDER_HANDOVER_COMPLETED"]
