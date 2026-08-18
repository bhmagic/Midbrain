from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents import FunctionTool
from jsonschema import validate

from physical_agent_test.limited_graph_authoring import (
    LIMITED_GRAPH_AUTHORING_GUIDANCE,
    compile_limited_graph_arguments,
    limited_graph_authoring_input_schema,
)
from physical_agent_test.skill_catalog import discover_agent_skills


def _limits(*, model_routes: int = 0) -> dict[str, int | float]:
    return {
        "seconds": 30.0,
        "transitions": 16,
        "visits": 3,
        "model_routes": model_routes,
        "physical_actions": 2,
        "result_bytes": 65536,
    }


def _graph(**overrides):
    graph = {
        "authoring_version": 1,
        "name": "concise authoring test",
        "start": "",
        "initial": [],
        "steps": [
            {
                "id": "first",
                "tool": "echo_value",
                "args_json": '{"value":1}',
                "bind": [],
            },
            {
                "id": "second",
                "tool": "echo_value",
                "args_json": '{"value":0}',
                "bind": [{"to": "/value", "from": "first#/value"}],
            },
        ],
        "edges": [],
        "retries": [],
        "switches": [],
        "model_routes": [],
        "terminals": [],
        "limits": _limits(),
    }
    graph.update(overrides)
    return {"graph": graph}


def _canonical_input_schema() -> dict:
    workspace = Path(__file__).resolve().parents[3]
    return next(
        item.input_schema
        for item in discover_agent_skills(workspace)
        if item.tool_name == "run_limited_graph"
    )


def test_model_facing_schema_is_strict_sdk_compatible_and_concise() -> None:
    schema = limited_graph_authoring_input_schema()
    tool = FunctionTool(
        name="run_limited_graph",
        description="Test concise graph authoring.",
        params_json_schema=schema,
        on_invoke_tool=lambda _context, _arguments: None,
        strict_json_schema=True,
    )

    graph_properties = tool.params_json_schema["properties"]["graph"]["properties"]
    assert "steps" in graph_properties
    assert "nodes" not in graph_properties
    assert "$defs" in tool.params_json_schema
    initial_properties = graph_properties["initial"]["items"]["properties"]
    step_properties = graph_properties["steps"]["items"]["properties"]
    condition_properties = tool.params_json_schema["$defs"]["condition"][
        "properties"
    ]
    assert "value_json" in initial_properties
    assert "value" not in initial_properties
    assert "args_json" in step_properties
    assert "args" not in step_properties
    assert "expected_json" in condition_properties
    assert "value" not in condition_properties


def test_linear_projection_compiles_to_canonical_graph() -> None:
    concise = _graph()
    validate(instance=concise, schema=limited_graph_authoring_input_schema())

    compiled = compile_limited_graph_arguments(concise)
    validate(instance=compiled, schema=_canonical_input_schema())
    graph = compiled["graph"]

    assert graph["start_node"] == "first"
    assert [node["id"] for node in graph["nodes"]] == [
        "first",
        "second",
        "complete",
        "failed",
    ]
    assert graph["nodes"][0]["skill"]["next_node"] == "second"
    assert graph["nodes"][1]["skill"]["next_node"] == "complete"
    assert graph["nodes"][1]["skill"]["bindings"][0] == {
        "target_pointer": "/value",
        "source_kind": "NODE_RESULT",
        "source_name": None,
        "source_node_id": "first",
        "source_pointer": "/value",
    }


@pytest.mark.parametrize(
    "initial, reference, expected_name, expected_pointer",
    [
        (
            [{"name": "request", "value_json": '{"payload":7}'}],
            "$initial#/request/payload",
            "request",
            "/payload",
        ),
        (
            [{"name": "request", "value_json": '{"payload":7}'}],
            "$request#/payload",
            "request",
            "/payload",
        ),
        (
            [{"name": "initial", "value_json": '{"request":7}'}],
            "$initial#/request",
            "initial",
            "/request",
        ),
    ],
)
def test_initial_binding_namespace_alias_compiles_without_ambiguity(
    initial,
    reference,
    expected_name,
    expected_pointer,
) -> None:
    concise = _graph(
        initial=initial,
        steps=[
            {
                "id": "first",
                "tool": "echo_value",
                "args_json": '{"value":0}',
                "bind": [{"to": "/value", "from": reference}],
            }
        ],
    )

    compiled = compile_limited_graph_arguments(concise)
    binding = compiled["graph"]["nodes"][0]["skill"]["bindings"][0]

    assert binding["source_kind"] == "INITIAL"
    assert binding["source_name"] == expected_name
    assert binding["source_pointer"] == expected_pointer


def test_model_guidance_publishes_both_initial_binding_spellings() -> None:
    assert "$name#/pointer" in LIMITED_GRAPH_AUTHORING_GUIDANCE
    assert "$initial#/name/pointer" in LIMITED_GRAPH_AUTHORING_GUIDANCE


def test_projection_compiles_retry_switch_and_model_route() -> None:
    concise = _graph(
        start="choose",
        initial=[
            {
                "name": "choice",
                "value_json": '{"payload":{"kind":"model"}}',
            }
        ],
        edges=[{"node": "first", "success": "route", "failure": "failed"}],
        retries=[
            {
                "node": "first",
                "attempts": 2,
                "when": {
                    "pointer": "/status",
                    "op": "NE",
                    "expected_json": '"OK"',
                },
            }
        ],
        switches=[
            {
                "id": "choose",
                "from": "$choice#/payload",
                "cases": [
                    {
                        "when": {
                            "pointer": "/kind",
                            "op": "EQ",
                            "expected_json": '"model"',
                        },
                        "target": "first",
                    }
                ],
                "default": "failed",
            }
        ],
        model_routes=[
            {
                "id": "route",
                "profile": "FAST_TEXT",
                "modality": "TEXT",
                "instruction": "Select the declared edge.",
                "inputs": [{"name": "value", "from": "first#/value"}],
                "routes": [
                    {"edge": "accept", "description": "Accept", "target": "second"},
                    {"edge": "reject", "description": "Reject", "target": "failed"},
                ],
                "confidence": 0.8,
                "fallback": "failed",
            }
        ],
        limits=_limits(model_routes=1),
    )
    validate(instance=concise, schema=limited_graph_authoring_input_schema())

    compiled = compile_limited_graph_arguments(concise)
    validate(instance=compiled, schema=_canonical_input_schema())
    nodes = {node["id"]: node for node in compiled["graph"]["nodes"]}

    assert nodes["first"]["skill"]["max_attempts"] == 2
    assert nodes["choose"]["switch"]["source_name"] == "choice"
    assert nodes["choose"]["switch"]["cases"][0]["condition"][
        "source_pointer"
    ] == "/payload/kind"
    assert nodes["route"]["model_route"]["routes"][0]["edge_id"] == "accept"


@pytest.mark.parametrize(
    "payload, message",
    [
        (_graph(edges=[{"node": "missing", "success": "complete", "failure": "failed"}]), "unknown Skill step"),
        (_graph(terminals=[{"id": "", "status": "FAILED", "message": "bad"}]), "non-empty IDs"),
        (
            _graph(
                steps=[
                    {
                        "id": "first",
                        "tool": "echo_value",
                        "args_json": "{}",
                        "bind": [{"to": "/value", "from": "malformed"}],
                    }
                ]
            ),
            "must use node-id",
        ),
    ],
)
def test_projection_rejects_ambiguous_shortcuts(payload, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compile_limited_graph_arguments(payload)


def test_canonical_input_remains_accepted_by_compiler() -> None:
    canonical = {"graph": {"schema_version": 1}}
    assert compile_limited_graph_arguments(canonical) == canonical
    assert compile_limited_graph_arguments(canonical) is not canonical
