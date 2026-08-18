from __future__ import annotations

from typing import Any

from jsonschema import validate as validate_json

from .bindings import pointer_tokens, resolve_pointer
from .models import GraphValidationError


def schema_pointer_candidates(
    schema: dict[str, Any],
    pointer: str,
    *,
    field: str,
) -> tuple[dict[str, Any], ...]:
    """Resolve one JSON pointer only through explicitly declared schema paths."""

    candidates = _expanded((schema,))
    for token in pointer_tokens(pointer):
        next_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            properties = candidate.get("properties")
            if isinstance(properties, dict):
                child = properties.get(token)
                if isinstance(child, dict):
                    next_candidates.append(child)
            items = candidate.get("items")
            if isinstance(items, dict) and _is_array_index(token):
                next_candidates.append(items)
            prefix_items = candidate.get("prefixItems")
            if isinstance(prefix_items, list) and _is_array_index(token):
                index = int(token)
                if 0 <= index < len(prefix_items) and isinstance(
                    prefix_items[index], dict
                ):
                    next_candidates.append(prefix_items[index])
        candidates = _expanded(tuple(next_candidates))
        if not candidates:
            raise GraphValidationError(
                f"{field} points to undeclared schema path {pointer!r}"
            )
    return candidates


def require_schema_pointer(
    schema: dict[str, Any],
    pointer: str,
    *,
    field: str,
) -> None:
    schema_pointer_candidates(schema, pointer, field=field)


def require_compact_schema_pointer(
    schema: dict[str, Any],
    compact_pointers: tuple[str, ...],
    pointer: str,
    *,
    field: str,
) -> None:
    require_schema_pointer(schema, pointer, field=field)
    if compact_pointers and not any(
        pointer == selected or pointer.startswith(f"{selected}/")
        for selected in compact_pointers
    ):
        raise GraphValidationError(
            f"{field} points outside the compact Skill result tier: {pointer!r}"
        )


def validate_compact_instance(
    instance: dict[str, Any],
    schema: dict[str, Any],
    compact_pointers: tuple[str, ...],
    *,
    field: str,
) -> None:
    """Validate a host-produced compact projection against its source schema."""

    if not compact_pointers:
        validate_json(instance=instance, schema=schema)
        return
    for pointer in _leaf_pointers(instance):
        if pointer.startswith(("/detail_ref", "/compact_projection")):
            continue
        if not any(
            pointer == selected
            or pointer.startswith(f"{selected}/")
            or selected.startswith(f"{pointer}/")
            for selected in compact_pointers
        ):
            raise GraphValidationError(
                f"{field} returned undeclared compact path {pointer!r}"
            )
    for pointer in compact_pointers:
        try:
            value = resolve_pointer(
                instance,
                pointer,
                field=f"{field} compact result",
            )
        except GraphValidationError:
            continue
        candidates = schema_pointer_candidates(
            schema,
            pointer,
            field=f"{field} compact pointer",
        )
        validate_json(
            instance=value,
            schema={"anyOf": list(candidates)},
        )
    detail_ref = instance.get("detail_ref")
    if detail_ref is not None:
        if not isinstance(detail_ref, dict):
            raise GraphValidationError(f"{field}.detail_ref must be an object")
        if detail_ref.get("schema") != "midbrain.skill_result_detail_ref":
            raise GraphValidationError(f"{field}.detail_ref has invalid schema")
        if detail_ref.get("schema_version") != 1:
            raise GraphValidationError(
                f"{field}.detail_ref has invalid schema_version"
            )
        if not isinstance(detail_ref.get("available"), bool):
            raise GraphValidationError(
                f"{field}.detail_ref.available must be boolean"
            )
    projection = instance.get("compact_projection")
    if projection is not None:
        if not isinstance(projection, dict):
            raise GraphValidationError(
                f"{field}.compact_projection must be an object"
            )
        if projection.get("schema") != "midbrain.compact_result_projection":
            raise GraphValidationError(
                f"{field}.compact_projection has invalid schema"
            )
        if projection.get("schema_version") != 1:
            raise GraphValidationError(
                f"{field}.compact_projection has invalid schema_version"
            )
        if projection.get("complete") is not False:
            raise GraphValidationError(
                f"{field}.compact_projection.complete must be false"
            )


def _leaf_pointers(value: Any, prefix: str = "") -> tuple[str, ...]:
    pointers: list[str] = []
    if isinstance(value, dict) and value:
        for name, child in value.items():
            token = str(name).replace("~", "~0").replace("/", "~1")
            pointers.extend(_leaf_pointers(child, f"{prefix}/{token}"))
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            pointers.extend(_leaf_pointers(child, f"{prefix}/{index}"))
    elif prefix:
        pointers.append(prefix)
    return tuple(pointers)


def _expanded(
    schemas: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    pending = list(schemas)
    expanded: list[dict[str, Any]] = []
    seen: set[int] = set()
    while pending:
        schema = pending.pop()
        identity = id(schema)
        if identity in seen:
            continue
        seen.add(identity)
        expanded.append(schema)
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = schema.get(keyword)
            if isinstance(branches, list):
                pending.extend(
                    branch for branch in branches if isinstance(branch, dict)
                )
    return tuple(expanded)


def _is_array_index(token: str) -> bool:
    if not token or (len(token) > 1 and token.startswith("0")):
        return False
    return token.isdigit()
