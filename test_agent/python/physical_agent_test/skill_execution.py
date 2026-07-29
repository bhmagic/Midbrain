from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from agents import FunctionTool
from jsonschema import validate

from .skill_catalog import AgentSkillDescriptor


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
) -> list[FunctionTool]:
    """Build only explicitly eligible tools after adapter registration checks."""

    by_name = {descriptor.tool_name: descriptor for descriptor in descriptors}
    missing_descriptors = sorted(eligible_tool_names - by_name.keys())
    if missing_descriptors:
        raise ValueError(
            "eligible tools are missing discoverable manifests: "
            + ", ".join(missing_descriptors)
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

        async def invoke_tool(
            _context,
            raw_arguments: str,
            *,
            selected_adapter: SkillExecutionAdapter = adapter,
            selected_descriptor: AgentSkillDescriptor = descriptor,
        ) -> Any:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Skill tool arguments must be a JSON object")
            validate(instance=arguments, schema=selected_descriptor.input_schema)
            return await selected_adapter.invoke(arguments)

        tools.append(
            FunctionTool(
                name=descriptor.tool_name,
                description=descriptor.description,
                params_json_schema=descriptor.input_schema,
                on_invoke_tool=invoke_tool,
                strict_json_schema=True,
                needs_approval=descriptor.invocation_requires_approval,
                timeout_seconds=float(adapter_timeout_s),
                timeout_behavior="raise_exception",
                defer_loading=bool(defer_loading),
            )
        )
    return tools
