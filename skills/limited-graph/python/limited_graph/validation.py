from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .bindings import decode_json, pointer_tokens, resolve_pointer
from .models import ChildDescriptor, ChildSkillBroker, GraphValidationError
from .schema_paths import require_schema_pointer, schema_pointer_candidates


_NODE_KINDS = {
    "SKILL": "skill",
    "SWITCH": "switch",
    "MODEL_ROUTE": "model_route",
    "TERMINAL": "terminal",
}
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


@dataclass(frozen=True)
class ValidatedGraph:
    graph: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    initial_values: dict[str, Any]
    descriptors: Mapping[str, ChildDescriptor]
    sha256: str


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_credential_keys(value: Any, *, field: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(marker in normalized for marker in _CREDENTIAL_MARKERS):
                raise GraphValidationError(
                    f"{field} contains prohibited credential-like key {key!r}"
                )
            _reject_credential_keys(item, field=field)
    elif isinstance(value, list):
        for item in value:
            _reject_credential_keys(item, field=field)
    elif isinstance(value, str) and redact_credential_values(value) != value:
        raise GraphValidationError(
            f"{field} contains prohibited credential-like text"
        )


def redact_credential_values(value: Any) -> Any:
    """Return a graph-safe copy with credential-like values removed."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(marker in normalized for marker in _CREDENTIAL_MARKERS):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_credential_values(item)
        return result
    if isinstance(value, list):
        return [redact_credential_values(item) for item in value]
    if isinstance(value, str):
        redacted = _BEARER_TEXT.sub("Bearer [REDACTED]", value)
        return _CREDENTIAL_TEXT.sub(r"\1=[REDACTED]", redacted)
    return copy.deepcopy(value)


def _validate_source(
    source: dict[str, Any],
    *,
    initial_values: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    descriptors: Mapping[str, ChildDescriptor],
    field: str,
) -> tuple[dict[str, Any], ...] | None:
    kind = source.get("source_kind")
    source_name = source.get("source_name")
    source_node_id = source.get("source_node_id")
    if kind == "INITIAL":
        if not isinstance(source_name, str) or source_name not in initial_values:
            raise GraphValidationError(
                f"{field} references unknown initial value {source_name!r}"
            )
        if source_node_id is not None:
            raise GraphValidationError(
                f"{field} INITIAL source_node_id must be null"
            )
        resolve_pointer(
            initial_values[source_name],
            str(source.get("source_pointer") or ""),
            field=field,
        )
        return None
    elif kind == "NODE_RESULT":
        if not isinstance(source_node_id, str) or source_node_id not in nodes:
            raise GraphValidationError(
                f"{field} references unknown node result {source_node_id!r}"
            )
        if source_name is not None:
            raise GraphValidationError(
                f"{field} NODE_RESULT source_name must be null"
            )
        source_node = nodes[source_node_id]
        if source_node["kind"] != "SKILL":
            raise GraphValidationError(
                f"{field} source node {source_node_id!r} has no Skill result"
            )
        source_tool_name = str(source_node["skill"]["tool_name"])
        source_descriptor = descriptors.get(source_tool_name)
        if source_descriptor is None:
            raise GraphValidationError(
                f"{field} source Skill {source_tool_name!r} is not eligible"
            )
        return schema_pointer_candidates(
            source_descriptor.output_schema,
            str(source.get("source_pointer") or ""),
            field=field,
        )
    else:
        raise GraphValidationError(f"{field} has invalid source_kind {kind!r}")


def _validate_condition(condition: dict[str, Any], *, field: str) -> None:
    pointer_tokens(str(condition.get("source_pointer") or ""))
    operator = str(condition.get("operator") or "")
    expected = condition.get("expected_json")
    if operator in {"EXISTS", "TRUTHY"}:
        if expected is not None:
            raise GraphValidationError(f"{field} {operator} expected_json must be null")
    else:
        if not isinstance(expected, str):
            raise GraphValidationError(f"{field} {operator} expected_json must be text")
        decoded = decode_json(expected, field=f"{field}.expected_json")
        if operator == "IN" and not isinstance(decoded, list):
            raise GraphValidationError(f"{field} IN expected_json must decode to an array")


def _targets(node: dict[str, Any]) -> set[str]:
    kind = node["kind"]
    config = node[_NODE_KINDS[kind]]
    if kind == "TERMINAL":
        return set()
    if kind == "SKILL":
        return {str(config["next_node"]), str(config["failure_node"])}
    if kind == "SWITCH":
        return {
            str(config["default_target"]),
            *(str(case["target_node"]) for case in config["cases"]),
        }
    return {
        str(config["fallback_target"]),
        *(str(route["target_node"]) for route in config["routes"]),
    }


def validate_graph(
    graph: dict[str, Any],
    broker: ChildSkillBroker,
    *,
    root_context: Any = None,
) -> ValidatedGraph:
    if not isinstance(graph, dict):
        raise GraphValidationError("graph must be an object")
    if graph.get("schema_version") != 1:
        raise GraphValidationError("unsupported Limited Graph schema_version")

    context_descriptors = getattr(broker, "descriptors_for_context", None)
    descriptors = dict(
        context_descriptors(root_context)
        if callable(context_descriptors)
        else broker.descriptors()
    )
    nodes: dict[str, dict[str, Any]] = {}
    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphValidationError("graph nodes must be a non-empty array")
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            raise GraphValidationError(f"node {index} must be an object")
        node_id = str(node.get("id") or "")
        if not node_id:
            raise GraphValidationError(f"node {index} has no id")
        if node_id in nodes:
            raise GraphValidationError(f"duplicate node id {node_id!r}")
        kind = str(node.get("kind") or "")
        if kind not in _NODE_KINDS:
            raise GraphValidationError(f"node {node_id} has unsupported kind {kind!r}")
        active_key = _NODE_KINDS[kind]
        for key in _NODE_KINDS.values():
            present = node.get(key)
            if key == active_key and not isinstance(present, dict):
                raise GraphValidationError(f"node {node_id} requires {active_key}")
            if key != active_key and present is not None:
                raise GraphValidationError(f"node {node_id} inactive {key} must be null")
        nodes[node_id] = copy.deepcopy(node)

    start_node = str(graph.get("start_node") or "")
    if start_node not in nodes:
        raise GraphValidationError(f"start_node {start_node!r} is unavailable")

    initial_values: dict[str, Any] = {}
    for index, item in enumerate(graph.get("initial_values") or []):
        if not isinstance(item, dict):
            raise GraphValidationError(f"initial value {index} must be an object")
        name = str(item.get("name") or "")
        if not name or name in initial_values:
            raise GraphValidationError(f"duplicate or empty initial value {name!r}")
        value = decode_json(item.get("value_json"), field=f"initial value {name}")
        _reject_credential_keys(value, field=f"initial value {name}")
        initial_values[name] = value

    for node_id, node in nodes.items():
        kind = node["kind"]
        config = node[_NODE_KINDS[kind]]
        for target in _targets(node):
            if target not in nodes:
                raise GraphValidationError(
                    f"node {node_id} targets unavailable node {target!r}"
                )

        if kind == "SKILL":
            tool_name = str(config.get("tool_name") or "")
            descriptor = descriptors.get(tool_name)
            if descriptor is None:
                raise GraphValidationError(
                    f"node {node_id} child Skill {tool_name!r} is not eligible"
                )
            if tool_name == "run_limited_graph" or descriptor.skill_type == "limited_graph":
                raise GraphValidationError(
                    f"node {node_id} cannot invoke a graph Skill"
                )
            if descriptor.safety_class == "MANUAL_ONLY":
                raise GraphValidationError(
                    f"node {node_id} cannot invoke manual-only Skill {tool_name}"
                )
            arguments = decode_json(
                config.get("arguments_json"),
                field=f"node {node_id}.arguments_json",
            )
            if not isinstance(arguments, dict):
                raise GraphValidationError(
                    f"node {node_id}.arguments_json must decode to an object"
                )
            _reject_credential_keys(arguments, field=f"node {node_id} arguments")
            max_attempts = int(config.get("max_attempts") or 0)
            if max_attempts < 1:
                raise GraphValidationError(f"node {node_id} max_attempts must be positive")
            if max_attempts > 1 and not descriptor.read_only:
                raise GraphValidationError(
                    f"node {node_id} may retry only a READ_ONLY Skill"
                )
            for binding_index, binding in enumerate(config.get("bindings") or []):
                _validate_source(
                    binding,
                    initial_values=initial_values,
                    nodes=nodes,
                    descriptors=descriptors,
                    field=f"node {node_id} binding {binding_index}",
                )
                require_schema_pointer(
                    descriptor.input_schema,
                    str(binding.get("target_pointer") or ""),
                    field=f"node {node_id} binding {binding_index} target",
                )
            retry_condition = config.get("retry_condition")
            if retry_condition is not None:
                _validate_condition(
                    retry_condition,
                    field=f"node {node_id} retry_condition",
                )
                require_schema_pointer(
                    descriptor.output_schema,
                    str(retry_condition.get("source_pointer") or ""),
                    field=f"node {node_id} retry_condition",
                )
        elif kind == "SWITCH":
            source_schemas = _validate_source(
                config,
                initial_values=initial_values,
                nodes=nodes,
                descriptors=descriptors,
                field=f"node {node_id} switch source",
            )
            for case_index, case in enumerate(config.get("cases") or []):
                _validate_condition(
                    case["condition"],
                    field=f"node {node_id} case {case_index}",
                )
                if source_schemas is not None:
                    require_schema_pointer(
                        {"anyOf": list(source_schemas)},
                        str(case["condition"].get("source_pointer") or ""),
                        field=f"node {node_id} case {case_index}",
                    )
        elif kind == "MODEL_ROUTE":
            _reject_credential_keys(
                config.get("instruction"),
                field=f"node {node_id} model instruction",
            )
            seen_inputs: set[str] = set()
            for input_index, item in enumerate(config.get("inputs") or []):
                name = str(item.get("name") or "")
                if not name or name in seen_inputs:
                    raise GraphValidationError(
                        f"node {node_id} has duplicate or empty model input {name!r}"
                    )
                seen_inputs.add(name)
                _validate_source(
                    item,
                    initial_values=initial_values,
                    nodes=nodes,
                    descriptors=descriptors,
                    field=f"node {node_id} model input {input_index}",
                )
            edge_ids = [str(route.get("edge_id") or "") for route in config.get("routes") or []]
            if len(edge_ids) < 2 or len(set(edge_ids)) != len(edge_ids) or not all(edge_ids):
                raise GraphValidationError(
                    f"node {node_id} model routes require unique non-empty edge IDs"
                )
            for route_index, route in enumerate(config.get("routes") or []):
                _reject_credential_keys(
                    route.get("description"),
                    field=f"node {node_id} route {route_index} description",
                )
        elif kind == "TERMINAL":
            _reject_credential_keys(
                config.get("message"),
                field=f"node {node_id} terminal message",
            )

    reachable: set[str] = set()
    pending = [start_node]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(_targets(nodes[node_id]) - reachable)
    unreachable = sorted(set(nodes) - reachable)
    if unreachable:
        raise GraphValidationError(
            "graph contains unreachable nodes: " + ", ".join(unreachable)
        )

    terminals = {
        node_id for node_id, node in nodes.items() if node["kind"] == "TERMINAL"
    }
    if not terminals:
        raise GraphValidationError("graph must contain at least one terminal node")
    can_reach_terminal = set(terminals)
    changed = True
    while changed:
        changed = False
        for node_id, node in nodes.items():
            if node_id not in can_reach_terminal and _targets(node) & can_reach_terminal:
                can_reach_terminal.add(node_id)
                changed = True
    trapped = sorted(reachable - can_reach_terminal)
    if trapped:
        raise GraphValidationError(
            "reachable nodes have no terminal path: " + ", ".join(trapped)
        )

    for node_id, node in nodes.items():
        if node["kind"] != "SKILL":
            continue
        tool_name = str(node["skill"]["tool_name"])
        descriptor = descriptors[tool_name]
        if not descriptor.physical:
            continue
        pending_cycle = list(_targets(node))
        seen_cycle: set[str] = set()
        while pending_cycle:
            candidate = pending_cycle.pop()
            if candidate == node_id:
                raise GraphValidationError(
                    f"physical node {node_id!r} cannot be part of a cycle"
                )
            if candidate in seen_cycle:
                continue
            seen_cycle.add(candidate)
            pending_cycle.extend(_targets(nodes[candidate]) - seen_cycle)

    normalized = copy.deepcopy(graph)
    return ValidatedGraph(
        graph=normalized,
        nodes=nodes,
        initial_values=initial_values,
        descriptors=descriptors,
        sha256=_canonical_sha256(normalized),
    )
