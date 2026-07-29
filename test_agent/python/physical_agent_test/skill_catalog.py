from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SAFETY_CLASSES = {
    "READ_ONLY",
    "STATEFUL_NO_MOTION",
    "PHYSICAL_MOTION_AUTHORIZATION_REQUIRED",
    "MANUAL_ONLY",
}
_LATENCY_CLASSES = {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}


@dataclass(frozen=True)
class AgentSkillDescriptor:
    skill_type: str
    skill_version: str
    display_name: str
    manifest_path: str
    schema_version: int
    discoverable: bool
    tool_name: str
    description: str
    when_to_use: tuple[str, ...]
    when_not_to_use: tuple[str, ...]
    side_effects: tuple[str, ...]
    safety_class: str
    expected_latency: str
    required_permissions: tuple[str, ...]
    input_schema: dict[str, Any]
    execution_adapter_id: str
    execution_adapter_kind: str
    execution_entrypoint: str | None
    invocation_requires_approval: bool
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    route_policy: dict[str, Any] | None
    disabled_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_agent_skills(
    workspace_root: Path,
    *,
    include_disabled: bool = False,
) -> list[AgentSkillDescriptor]:
    """Read concise discovery metadata without importing or starting any Skill."""
    skills_root = workspace_root / "skills"
    descriptors: list[AgentSkillDescriptor] = []
    if not skills_root.is_dir():
        return descriptors

    for manifest_path in sorted(skills_root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        discovery = manifest.get("agent_discovery")
        if discovery is None:
            continue
        descriptor = _parse_descriptor(manifest_path, manifest, discovery)
        if descriptor.discoverable or include_disabled:
            descriptors.append(descriptor)
    return sorted(descriptors, key=lambda value: value.tool_name)


def _parse_descriptor(
    manifest_path: Path,
    manifest: dict[str, Any],
    discovery: Any,
) -> AgentSkillDescriptor:
    if not isinstance(discovery, dict):
        raise ValueError(f"{manifest_path}: agent_discovery must be an object")

    schema_version = discovery.get("schema_version")
    discoverable = discovery.get("discoverable")
    tool_name = discovery.get("tool_name")
    description = discovery.get("description")
    safety_class = discovery.get("safety_class")
    expected_latency = discovery.get("expected_latency")
    disabled_reason = discovery.get("disabled_reason")

    if schema_version != 1:
        raise ValueError(f"{manifest_path}: unsupported agent_discovery schema")
    if not isinstance(discoverable, bool):
        raise ValueError(f"{manifest_path}: discoverable must be boolean")
    if not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name):
        raise ValueError(f"{manifest_path}: invalid tool_name")
    if not isinstance(description, str) or not 20 <= len(description) <= 800:
        raise ValueError(f"{manifest_path}: description must contain 20 to 800 characters")
    if safety_class not in _SAFETY_CLASSES:
        raise ValueError(f"{manifest_path}: invalid safety_class")
    if expected_latency not in _LATENCY_CLASSES:
        raise ValueError(f"{manifest_path}: invalid expected_latency")
    if not discoverable and (
        not isinstance(disabled_reason, str) or len(disabled_reason.strip()) < 8
    ):
        raise ValueError(f"{manifest_path}: disabled Skill requires disabled_reason")

    when_to_use = _string_list(manifest_path, discovery, "when_to_use", required=True)
    when_not_to_use = _string_list(
        manifest_path,
        discovery,
        "when_not_to_use",
        required=False,
    )
    side_effects = _string_list(
        manifest_path,
        discovery,
        "side_effects",
        required=False,
    )
    required_permissions = _string_list(
        manifest_path,
        discovery,
        "required_permissions",
        required=False,
    )
    input_schema = discovery.get("input_schema")
    if not isinstance(input_schema, dict):
        raise ValueError(f"{manifest_path}: input_schema must be an object")
    if input_schema.get("type") != "object":
        raise ValueError(f"{manifest_path}: input_schema.type must be object")
    if input_schema.get("additionalProperties") is not False:
        raise ValueError(
            f"{manifest_path}: input_schema.additionalProperties must be false"
        )
    if not isinstance(input_schema.get("properties"), dict):
        raise ValueError(f"{manifest_path}: input_schema.properties must be an object")
    if not isinstance(input_schema.get("required"), list):
        raise ValueError(f"{manifest_path}: input_schema.required must be an array")

    execution_adapter = discovery.get("execution_adapter")
    if not isinstance(execution_adapter, dict):
        raise ValueError(f"{manifest_path}: execution_adapter must be an object")
    execution_adapter_id = _required_text(
        manifest_path,
        execution_adapter,
        "adapter_id",
    )
    execution_adapter_kind = _required_text(
        manifest_path,
        execution_adapter,
        "kind",
    )
    if execution_adapter_kind not in {
        "IN_PROCESS_BOUND_INSTANCE",
        "EXTERNAL_SKILL_ENTRYPOINT",
        "MANUAL_LOCAL_ONLY",
    }:
        raise ValueError(f"{manifest_path}: unsupported execution adapter kind")
    execution_entrypoint = execution_adapter.get("entrypoint")
    if execution_entrypoint is not None and (
        not isinstance(execution_entrypoint, str) or not execution_entrypoint.strip()
    ):
        raise ValueError(f"{manifest_path}: execution entrypoint must be text")
    invocation_requires_approval = execution_adapter.get(
        "invocation_requires_approval",
        safety_class not in {"READ_ONLY"},
    )
    if not isinstance(invocation_requires_approval, bool):
        raise ValueError(
            f"{manifest_path}: invocation_requires_approval must be boolean"
        )
    route_policy = manifest.get("route_policy")
    if route_policy is not None and not isinstance(route_policy, dict):
        raise ValueError(f"{manifest_path}: route_policy must be an object")

    return AgentSkillDescriptor(
        skill_type=_required_text(manifest_path, manifest, "skill_type"),
        skill_version=_required_text(manifest_path, manifest, "version"),
        display_name=_required_text(manifest_path, manifest, "display_name"),
        manifest_path=str(manifest_path),
        schema_version=schema_version,
        discoverable=discoverable,
        tool_name=tool_name,
        description=description,
        when_to_use=when_to_use,
        when_not_to_use=when_not_to_use,
        side_effects=side_effects,
        safety_class=safety_class,
        expected_latency=expected_latency,
        required_permissions=required_permissions,
        input_schema=json.loads(json.dumps(input_schema)),
        execution_adapter_id=execution_adapter_id,
        execution_adapter_kind=execution_adapter_kind,
        execution_entrypoint=execution_entrypoint,
        invocation_requires_approval=invocation_requires_approval,
        required_capabilities=tuple(manifest.get("required_capabilities") or ()),
        optional_capabilities=tuple(manifest.get("optional_capabilities") or ()),
        route_policy=(
            None
            if route_policy is None
            else json.loads(json.dumps(route_policy))
        ),
        disabled_reason=disabled_reason,
    )


def _required_text(manifest_path: Path, source: dict[str, Any], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{manifest_path}: {field} must be a non-empty string")
    return value


def _string_list(
    manifest_path: Path,
    source: dict[str, Any],
    field: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    value = source.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{manifest_path}: {field} must be a string array")
    if required and not value:
        raise ValueError(f"{manifest_path}: {field} must not be empty")
    return tuple(value)
