from __future__ import annotations

import copy
from typing import Any


LIMITED_GRAPH_AUTHORING_GUIDANCE = (
    " Author graph.authoring_version=1 with ordered steps "
    "{id,tool,args_json,bind}. JSON-encode args_json as an object, each "
    "initial value_json, and each condition expected_json. Bind with "
    "{to,from}; from uses node#/pointer, $name#/pointer, or the equivalent "
    "$initial#/name/pointer namespace form. Order supplies success edges; the "
    "last step goes to 'complete' and failures to 'failed'. "
    "Add edges, retries, switches, model_routes, or terminals only when needed; "
    "terminals=[] supplies defaults. The host compiles canonical v1 before "
    "preflight and execution."
)


def limited_graph_authoring_input_schema() -> dict[str, Any]:
    """Return a fresh strict schema for the model-facing graph projection."""

    condition_definition = {
        "type": "object",
        "properties": {
            "pointer": {"type": "string", "maxLength": 512},
            "op": {
                "type": "string",
                "enum": [
                    "EQ",
                    "NE",
                    "LT",
                    "LTE",
                    "GT",
                    "GTE",
                    "IN",
                    "EXISTS",
                    "TRUTHY",
                ],
            },
            "expected_json": {
                "type": ["string", "null"],
                "maxLength": 65536,
                "description": (
                    "Comparison value encoded as JSON. Use null only when "
                    "the operator ignores the comparison value."
                ),
            },
        },
        "required": ["pointer", "op", "expected_json"],
        "additionalProperties": False,
    }
    source_definition = {
        "type": "string",
        "minLength": 2,
        "maxLength": 640,
    }
    condition = {"$ref": "#/$defs/condition"}
    source = {"$ref": "#/$defs/source"}
    schema = {
        "$defs": {
            "condition": condition_definition,
            "source": source_definition,
        },
        "type": "object",
        "properties": {
            "graph": {
                "type": "object",
                "properties": {
                    "authoring_version": {"type": "integer", "const": 1},
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 120,
                    },
                    "start": {
                        "type": "string",
                        "maxLength": 64,
                    },
                    "initial": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                                },
                                "value_json": {
                                    "type": "string",
                                    "maxLength": 65536,
                                    "description": (
                                        "Value encoded as JSON. A text value "
                                        "must include its JSON quotes."
                                    ),
                                },
                            },
                            "required": ["name", "value_json"],
                            "additionalProperties": False,
                        },
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                                },
                                "tool": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9_]{2,63}$",
                                },
                                "args_json": {
                                    "type": "string",
                                    "maxLength": 131072,
                                    "description": (
                                        "Complete child input encoded as one "
                                        "JSON object string."
                                    ),
                                },
                                "bind": {
                                    "type": "array",
                                    "maxItems": 64,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "to": {
                                                "type": "string",
                                                "maxLength": 512,
                                            },
                                            "from": source,
                                        },
                                        "required": ["to", "from"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["id", "tool", "args_json", "bind"],
                            "additionalProperties": False,
                        },
                    },
                    "edges": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "node": {"type": "string", "maxLength": 64},
                                "success": {
                                    "type": ["string", "null"],
                                    "maxLength": 64,
                                },
                                "failure": {
                                    "type": ["string", "null"],
                                    "maxLength": 64,
                                },
                            },
                            "required": ["node", "success", "failure"],
                            "additionalProperties": False,
                        },
                    },
                    "retries": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {
                            "type": "object",
                            "properties": {
                                "node": {"type": "string", "maxLength": 64},
                                "attempts": {
                                    "type": "integer",
                                    "minimum": 2,
                                    "maximum": 8,
                                },
                                "when": condition,
                            },
                            "required": ["node", "attempts", "when"],
                            "additionalProperties": False,
                        },
                    },
                    "switches": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "maxLength": 64},
                                "from": source,
                                "cases": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 32,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "when": condition,
                                            "target": {
                                                "type": "string",
                                                "maxLength": 64,
                                            },
                                        },
                                        "required": ["when", "target"],
                                        "additionalProperties": False,
                                    },
                                },
                                "default": {"type": "string", "maxLength": 64},
                            },
                            "required": ["id", "from", "cases", "default"],
                            "additionalProperties": False,
                        },
                    },
                    "model_routes": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "maxLength": 64},
                                "profile": {
                                    "type": "string",
                                    "pattern": "^[A-Z][A-Z0-9_]{2,63}$",
                                },
                                "modality": {
                                    "type": "string",
                                    "enum": ["TEXT", "VISION"],
                                },
                                "instruction": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                },
                                "inputs": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 16,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "pattern": (
                                                    "^[a-z][a-z0-9_-]{0,63}$"
                                                ),
                                            },
                                            "from": source,
                                        },
                                        "required": ["name", "from"],
                                        "additionalProperties": False,
                                    },
                                },
                                "routes": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 16,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "edge": {
                                                "type": "string",
                                                "pattern": (
                                                    "^[a-z][a-z0-9_-]{0,63}$"
                                                ),
                                            },
                                            "description": {
                                                "type": "string",
                                                "minLength": 1,
                                                "maxLength": 500,
                                            },
                                            "target": {
                                                "type": "string",
                                                "maxLength": 64,
                                            },
                                        },
                                        "required": [
                                            "edge",
                                            "description",
                                            "target",
                                        ],
                                        "additionalProperties": False,
                                    },
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                                "fallback": {"type": "string", "maxLength": 64},
                            },
                            "required": [
                                "id",
                                "profile",
                                "modality",
                                "instruction",
                                "inputs",
                                "routes",
                                "confidence",
                                "fallback",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "terminals": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "maxLength": 64},
                                "status": {
                                    "type": "string",
                                    "pattern": "^[A-Z][A-Z0-9_]{1,63}$",
                                },
                                "message": {"type": "string", "maxLength": 1000},
                            },
                            "required": ["id", "status", "message"],
                            "additionalProperties": False,
                        },
                    },
                    "limits": {
                        "type": "object",
                        "properties": {
                            "seconds": {
                                "type": "number",
                                "minimum": 0.1,
                                "maximum": 600.0,
                            },
                            "transitions": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 256,
                            },
                            "visits": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 32,
                            },
                            "model_routes": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 32,
                            },
                            "physical_actions": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 32,
                            },
                            "result_bytes": {
                                "type": "integer",
                                "minimum": 1024,
                                "maximum": 1048576,
                            },
                        },
                        "required": [
                            "seconds",
                            "transitions",
                            "visits",
                            "model_routes",
                            "physical_actions",
                            "result_bytes",
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "authoring_version",
                    "name",
                    "start",
                    "initial",
                    "steps",
                    "edges",
                    "retries",
                    "switches",
                    "model_routes",
                    "terminals",
                    "limits",
                ],
                "additionalProperties": False,
            }
        },
        "required": ["graph"],
        "additionalProperties": False,
    }
    return schema


def _require_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _source(
    reference: Any,
    *,
    field: str,
    initial_names: set[str],
) -> dict[str, Any]:
    if not isinstance(reference, str) or "#" not in reference:
        raise ValueError(
            f"{field} must use node-id#/pointer, $name#/pointer, or "
            "$initial#/name/pointer"
        )
    owner, pointer = reference.split("#", 1)
    if pointer and not pointer.startswith("/"):
        raise ValueError(f"{field} JSON pointer must be empty or start with /")
    if owner.startswith("$"):
        if owner == "$initial" and "initial" not in initial_names:
            parts = pointer.split("/")
            name = parts[1] if len(parts) > 1 else ""
            pointer = (
                "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
            )
        else:
            name = owner[1:]
        if not name:
            raise ValueError(f"{field} initial name must not be empty")
        return {
            "source_kind": "INITIAL",
            "source_name": name,
            "source_node_id": None,
            "source_pointer": pointer,
        }
    if not owner:
        raise ValueError(f"{field} node ID must not be empty")
    return {
        "source_kind": "NODE_RESULT",
        "source_name": None,
        "source_node_id": owner,
        "source_pointer": pointer,
    }


def _condition(value: Any, *, field: str) -> dict[str, Any]:
    item = _require_mapping(value, field=field)
    return {
        "source_pointer": item.get("pointer"),
        "operator": item.get("op"),
        "expected_json": item.get("expected_json"),
    }


def _unique_by_node(items: list[Any], *, field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(items):
        item = _require_mapping(raw_item, field=f"{field}[{index}]")
        node_id = item.get("node")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"{field}[{index}].node must be non-empty text")
        if node_id in result:
            raise ValueError(f"{field} repeats node {node_id!r}")
        result[node_id] = item
    return result


def compile_limited_graph_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Compile concise Agent authoring input into canonical graph v1 input."""

    graph = _require_mapping(arguments.get("graph"), field="graph")
    if "authoring_version" not in graph:
        return copy.deepcopy(arguments)
    if graph.get("authoring_version") != 1:
        raise ValueError("unsupported Limited Graph authoring_version")

    raw_initial = _require_list(graph.get("initial"), field="graph.initial")
    initial_items = [
        _require_mapping(raw_item, field=f"graph.initial[{index}]")
        for index, raw_item in enumerate(raw_initial)
    ]
    initial_names = {str(item.get("name") or "") for item in initial_items}

    raw_steps = _require_list(graph.get("steps"), field="graph.steps")
    if not raw_steps:
        raise ValueError("graph.steps must contain at least one Skill step")
    steps = [
        _require_mapping(item, field=f"graph.steps[{index}]")
        for index, item in enumerate(raw_steps)
    ]
    step_ids = [str(item.get("id") or "") for item in steps]
    if not all(step_ids) or len(set(step_ids)) != len(step_ids):
        raise ValueError("graph.steps must use unique non-empty IDs")

    edges = _unique_by_node(
        _require_list(graph.get("edges"), field="graph.edges"),
        field="graph.edges",
    )
    retries = _unique_by_node(
        _require_list(graph.get("retries"), field="graph.retries"),
        field="graph.retries",
    )
    unknown_overrides = sorted((set(edges) | set(retries)) - set(step_ids))
    if unknown_overrides:
        raise ValueError(
            "edge or retry override references unknown Skill step: "
            + ", ".join(unknown_overrides)
        )

    nodes: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        node_id = step_ids[index]
        edge = edges.get(node_id, {})
        retry = retries.get(node_id)
        default_next = (
            step_ids[index + 1] if index + 1 < len(step_ids) else "complete"
        )
        bindings = []
        for binding_index, raw_binding in enumerate(
            _require_list(step.get("bind"), field=f"step {node_id}.bind")
        ):
            binding = _require_mapping(
                raw_binding,
                field=f"step {node_id}.bind[{binding_index}]",
            )
            bindings.append(
                {
                    "target_pointer": binding.get("to"),
                    **_source(
                        binding.get("from"),
                        field=f"step {node_id}.bind[{binding_index}].from",
                        initial_names=initial_names,
                    ),
                }
            )
        nodes.append(
            {
                "id": node_id,
                "kind": "SKILL",
                "skill": {
                    "tool_name": step.get("tool"),
                    "arguments_json": step.get("args_json"),
                    "bindings": bindings,
                    "max_attempts": (
                        int(retry.get("attempts")) if retry is not None else 1
                    ),
                    "retry_condition": (
                        _condition(
                            retry.get("when"),
                            field=f"retry {node_id}.when",
                        )
                        if retry is not None
                        else None
                    ),
                    "next_node": edge.get("success") or default_next,
                    "failure_node": edge.get("failure") or "failed",
                },
                "switch": None,
                "model_route": None,
                "terminal": None,
            }
        )

    for index, raw_switch in enumerate(
        _require_list(graph.get("switches"), field="graph.switches")
    ):
        item = _require_mapping(raw_switch, field=f"graph.switches[{index}]")
        switch_source = _source(
            item.get("from"),
            field=f"switch {item.get('id')}.from",
            initial_names=initial_names,
        )
        base_pointer = switch_source.pop("source_pointer")
        cases = []
        for case_index, raw_case in enumerate(
            _require_list(item.get("cases"), field=f"switch {item.get('id')}.cases")
        ):
            case = _require_mapping(
                raw_case,
                field=f"switch {item.get('id')}.cases[{case_index}]",
            )
            condition = _condition(
                case.get("when"),
                field=f"switch {item.get('id')} case {case_index}",
            )
            condition["source_pointer"] = (
                base_pointer + condition["source_pointer"]
            )
            cases.append(
                {
                    "condition": condition,
                    "target_node": case.get("target"),
                }
            )
        nodes.append(
            {
                "id": item.get("id"),
                "kind": "SWITCH",
                "skill": None,
                "switch": {
                    **switch_source,
                    "cases": cases,
                    "default_target": item.get("default"),
                },
                "model_route": None,
                "terminal": None,
            }
        )

    for index, raw_route in enumerate(
        _require_list(graph.get("model_routes"), field="graph.model_routes")
    ):
        item = _require_mapping(raw_route, field=f"graph.model_routes[{index}]")
        inputs = []
        for input_index, raw_input in enumerate(
            _require_list(
                item.get("inputs"),
                field=f"model route {item.get('id')}.inputs",
            )
        ):
            route_input = _require_mapping(
                raw_input,
                field=f"model route {item.get('id')}.inputs[{input_index}]",
            )
            inputs.append(
                {
                    "name": route_input.get("name"),
                    **_source(
                        route_input.get("from"),
                        field=(
                            f"model route {item.get('id')}.inputs[{input_index}].from"
                        ),
                        initial_names=initial_names,
                    ),
                }
            )
        routes = []
        for route_index, raw_edge in enumerate(
            _require_list(
                item.get("routes"),
                field=f"model route {item.get('id')}.routes",
            )
        ):
            route = _require_mapping(
                raw_edge,
                field=f"model route {item.get('id')}.routes[{route_index}]",
            )
            routes.append(
                {
                    "edge_id": route.get("edge"),
                    "description": route.get("description"),
                    "target_node": route.get("target"),
                }
            )
        nodes.append(
            {
                "id": item.get("id"),
                "kind": "MODEL_ROUTE",
                "skill": None,
                "switch": None,
                "model_route": {
                    "routing_profile": item.get("profile"),
                    "modality": item.get("modality"),
                    "instruction": item.get("instruction"),
                    "inputs": inputs,
                    "routes": routes,
                    "minimum_confidence": item.get("confidence"),
                    "fallback_target": item.get("fallback"),
                },
                "terminal": None,
            }
        )

    terminal_ids: set[str] = set()
    for index, raw_terminal in enumerate(
        _require_list(graph.get("terminals"), field="graph.terminals")
    ):
        item = _require_mapping(
            raw_terminal,
            field=f"graph.terminals[{index}]",
        )
        terminal_id = str(item.get("id") or "")
        if not terminal_id:
            raise ValueError("graph.terminals must use non-empty IDs")
        terminal_ids.add(terminal_id)
        nodes.append(
            {
                "id": terminal_id,
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {
                    "status": item.get("status"),
                    "message": item.get("message"),
                },
            }
        )
    missing_defaults = {"complete", "failed"} - terminal_ids
    if len(terminal_ids) != len(
        _require_list(graph.get("terminals"), field="graph.terminals")
    ):
        raise ValueError("graph.terminals must use unique non-empty IDs")
    for terminal_id in sorted(missing_defaults):
        nodes.append(
            {
                "id": terminal_id,
                "kind": "TERMINAL",
                "skill": None,
                "switch": None,
                "model_route": None,
                "terminal": {
                    "status": (
                        "COMPLETED" if terminal_id == "complete" else "FAILED"
                    ),
                    "message": (
                        "Limited Graph completed"
                        if terminal_id == "complete"
                        else "Limited Graph failed"
                    ),
                },
            }
        )

    limits = _require_mapping(graph.get("limits"), field="graph.limits")
    start_node = graph.get("start") or step_ids[0]
    return {
        "graph": {
            "schema_version": 1,
            "name": graph.get("name"),
            "start_node": start_node,
            "initial_values": [
                {
                    "name": item.get("name"),
                    "value_json": item.get("value_json"),
                }
                for item in initial_items
            ],
            "nodes": nodes,
            "limits": {
                "max_active_runtime_s": limits.get("seconds"),
                "max_transitions": limits.get("transitions"),
                "max_visits_per_node": limits.get("visits"),
                "max_model_routes": limits.get("model_routes"),
                "max_physical_actions": limits.get("physical_actions"),
                "max_retained_result_bytes": limits.get("result_bytes"),
            },
        }
    }
