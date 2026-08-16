from __future__ import annotations

import copy
import json
from typing import Any

from .models import GraphValidationError


_MISSING = object()


def decode_json(value: str, *, field: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise GraphValidationError(f"{field} is not valid JSON: {error}") from error


def pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise GraphValidationError(
            f"JSON pointer must be empty or start with '/': {pointer!r}"
        )
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def resolve_pointer(value: Any, pointer: str, *, field: str) -> Any:
    cursor = value
    for token in pointer_tokens(pointer):
        if isinstance(cursor, dict):
            cursor = cursor.get(token, _MISSING)
        elif isinstance(cursor, list):
            try:
                index = int(token)
            except ValueError as error:
                raise GraphValidationError(
                    f"{field} list pointer token is not an integer: {token!r}"
                ) from error
            cursor = cursor[index] if 0 <= index < len(cursor) else _MISSING
        else:
            cursor = _MISSING
        if cursor is _MISSING:
            raise GraphValidationError(
                f"{field} points to a missing value at {pointer!r}"
            )
    return copy.deepcopy(cursor)


def write_pointer(root: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = pointer_tokens(pointer)
    if not tokens:
        if not isinstance(value, dict):
            raise GraphValidationError(
                "a root argument binding must supply a JSON object"
            )
        root.clear()
        root.update(copy.deepcopy(value))
        return

    cursor: Any = root
    for token in tokens[:-1]:
        if isinstance(cursor, dict):
            if token not in cursor:
                cursor[token] = {}
            cursor = cursor[token]
        elif isinstance(cursor, list):
            try:
                index = int(token)
            except ValueError as error:
                raise GraphValidationError(
                    f"target list pointer token is not an integer: {token!r}"
                ) from error
            if not 0 <= index < len(cursor):
                raise GraphValidationError(
                    f"target list pointer index is unavailable: {index}"
                )
            cursor = cursor[index]
        else:
            raise GraphValidationError(
                f"target pointer traverses a scalar at {pointer!r}"
            )

    final = tokens[-1]
    if isinstance(cursor, dict):
        cursor[final] = copy.deepcopy(value)
        return
    if isinstance(cursor, list):
        try:
            index = int(final)
        except ValueError as error:
            raise GraphValidationError(
                f"target list pointer token is not an integer: {final!r}"
            ) from error
        if not 0 <= index < len(cursor):
            raise GraphValidationError(
                f"target list pointer index is unavailable: {index}"
            )
        cursor[index] = copy.deepcopy(value)
        return
    raise GraphValidationError(f"target pointer parent is not a container: {pointer!r}")


def source_value(
    source: dict[str, Any],
    *,
    initial_values: dict[str, Any],
    node_results: dict[str, Any],
    field: str,
) -> Any:
    kind = source.get("source_kind")
    if kind == "INITIAL":
        name = source.get("source_name")
        if not isinstance(name, str) or name not in initial_values:
            raise GraphValidationError(f"{field} references unknown initial value {name!r}")
        value = initial_values[name]
    elif kind == "NODE_RESULT":
        node_id = source.get("source_node_id")
        if not isinstance(node_id, str) or node_id not in node_results:
            raise GraphValidationError(f"{field} references unavailable node result {node_id!r}")
        value = node_results[node_id]
    else:
        raise GraphValidationError(f"{field} has unsupported source_kind {kind!r}")
    return resolve_pointer(
        value,
        str(source.get("source_pointer") or ""),
        field=field,
    )


def apply_bindings(
    arguments: dict[str, Any],
    bindings: list[dict[str, Any]],
    *,
    initial_values: dict[str, Any],
    node_results: dict[str, Any],
    node_id: str,
) -> dict[str, Any]:
    output = copy.deepcopy(arguments)
    for index, binding in enumerate(bindings):
        value = source_value(
            binding,
            initial_values=initial_values,
            node_results=node_results,
            field=f"node {node_id} binding {index}",
        )
        write_pointer(output, str(binding.get("target_pointer") or ""), value)
    return output


def condition_matches(value: Any, condition: dict[str, Any], *, field: str) -> bool:
    pointer = str(condition.get("source_pointer") or "")
    operator = str(condition.get("operator") or "")
    if operator == "EXISTS":
        try:
            resolve_pointer(value, pointer, field=field)
            return True
        except GraphValidationError:
            return False
    actual = resolve_pointer(value, pointer, field=field)
    if operator == "TRUTHY":
        return bool(actual)
    expected_raw = condition.get("expected_json")
    expected = decode_json(expected_raw, field=f"{field}.expected_json")
    if operator == "EQ":
        return actual == expected
    if operator == "NE":
        return actual != expected
    if operator == "IN":
        if not isinstance(expected, list):
            raise GraphValidationError(f"{field} IN expected value must be an array")
        return actual in expected
    if operator in {"LT", "LTE", "GT", "GTE"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            raise GraphValidationError(f"{field} ordered comparison rejects booleans")
        try:
            if operator == "LT":
                return actual < expected
            if operator == "LTE":
                return actual <= expected
            if operator == "GT":
                return actual > expected
            return actual >= expected
        except TypeError as error:
            raise GraphValidationError(
                f"{field} compares incompatible JSON values"
            ) from error
    raise GraphValidationError(f"{field} has unsupported operator {operator!r}")
