from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from agents import FunctionTool
from jsonschema import validate

from .phase4_policy import extend_current_operation_hard_timeout
from .skill_catalog import AgentSkillDescriptor


_LATENCY_TIMEOUT_FLOORS_S = {
    "HIGH": 600.0,
}


class SkillExecutionAdapter(Protocol):
    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """Invoke one selected finite Skill."""


@dataclass
class BoundMethodSkillAdapter:
    invoke_method: Callable[[dict[str, Any]], Awaitable[Any]]

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        return await self.invoke_method(arguments)


def build_agent_tools(
    descriptors: list[AgentSkillDescriptor],
    adapters: dict[str, SkillExecutionAdapter],
    *,
    eligible_tool_names: set[str],
    defer_loading: bool = False,
    adapter_timeout_s: float = 60.0,
    adapter_timeout_overrides_s: dict[str, float] | None = None,
    approval_overrides: dict[
        str,
        bool
        | Callable[[Any, dict[str, Any], str], Awaitable[bool]],
    ]
    | None = None,
) -> list[FunctionTool]:
    """Build only explicitly eligible tools after adapter registration checks."""

    by_name = {descriptor.tool_name: descriptor for descriptor in descriptors}
    missing_descriptors = sorted(eligible_tool_names - by_name.keys())
    if missing_descriptors:
        raise ValueError(
            "eligible tools are missing discoverable manifests: "
            + ", ".join(missing_descriptors)
        )

    timeout_overrides = dict(adapter_timeout_overrides_s or {})
    approval_overrides = dict(approval_overrides or {})
    invalid_timeout_tools = sorted(
        tool_name
        for tool_name, timeout_s in timeout_overrides.items()
        if tool_name not in eligible_tool_names or float(timeout_s) <= 0.0
    )
    if invalid_timeout_tools:
        raise ValueError(
            "adapter timeout overrides must be positive and target eligible "
            "tools: "
            + ", ".join(invalid_timeout_tools)
        )
    invalid_approval_tools = sorted(
        set(approval_overrides) - eligible_tool_names
    )
    if invalid_approval_tools:
        raise ValueError(
            "approval overrides must target eligible tools: "
            + ", ".join(invalid_approval_tools)
        )

    tools: list[FunctionTool] = []
    for tool_name in sorted(eligible_tool_names):
        descriptor = by_name[tool_name]
        adapter = adapters.get(descriptor.execution_adapter_id)
        if adapter is None:
            raise ValueError(
                f"{tool_name} has no registered execution adapter "
                f"{descriptor.execution_adapter_id}"
            )
        selected_timeout_s = float(
            timeout_overrides.get(
                descriptor.tool_name,
                max(
                    adapter_timeout_s,
                    _LATENCY_TIMEOUT_FLOORS_S.get(
                        descriptor.expected_latency,
                        adapter_timeout_s,
                    ),
                ),
            )
        )

        async def invoke_tool(
            _context,
            raw_arguments: str,
            *,
            selected_adapter: SkillExecutionAdapter = adapter,
            selected_descriptor: AgentSkillDescriptor = descriptor,
            effective_timeout_s: float = selected_timeout_s,
        ) -> Any:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Skill tool arguments must be a JSON object")
            validate(instance=arguments, schema=selected_descriptor.input_schema)
            extend_current_operation_hard_timeout(
                effective_timeout_s,
                stage=f"skill:{selected_descriptor.tool_name}:running",
            )
            return await selected_adapter.invoke(arguments)

        tools.append(
            FunctionTool(
                name=descriptor.tool_name,
                description=descriptor.description,
                params_json_schema=descriptor.input_schema,
                on_invoke_tool=invoke_tool,
                strict_json_schema=True,
                needs_approval=approval_overrides.get(
                    descriptor.tool_name,
                    descriptor.invocation_requires_approval,
                ),
                timeout_seconds=selected_timeout_s,
                timeout_behavior="raise_exception",
                defer_loading=bool(defer_loading),
            )
        )
    return tools
