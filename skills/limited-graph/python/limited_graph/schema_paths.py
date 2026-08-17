from __future__ import annotations

from typing import Any

from .bindings import pointer_tokens
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
