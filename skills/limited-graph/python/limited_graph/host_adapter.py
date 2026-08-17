from __future__ import annotations

from pathlib import Path
from typing import Any

from limited_graph.runner import LimitedGraphRunner


class LimitedGraphHostAdapter:
    def __init__(self, broker: Any):
        if broker is None:
            raise RuntimeError("Limited Graph requires a host child-Skill broker")
        self.broker = broker
        self.runner = LimitedGraphRunner(broker)

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._invoke(arguments, root_context=None)

    async def invoke_with_context(
        self,
        arguments: dict[str, Any],
        context_wrapper: Any,
    ) -> dict[str, Any]:
        return await self._invoke(arguments, root_context=context_wrapper)

    async def _invoke(
        self,
        arguments: dict[str, Any],
        *,
        root_context: Any,
    ) -> dict[str, Any]:
        graph = arguments.get("graph")
        if not isinstance(graph, dict):
            raise ValueError("graph must be an object")
        call_id = str(getattr(root_context, "tool_call_id", "") or "")
        return await self.runner.run(
            graph,
            root_context=root_context,
            root_call_id=call_id,
            child_result_observer=self.broker.observe_child_result,
        )


def build_host_adapter(
    *,
    skill_root: Path,
    manifest: dict[str, Any],
    services: Any,
) -> LimitedGraphHostAdapter:
    del skill_root, manifest
    return LimitedGraphHostAdapter(
        getattr(services, "skill_invocation_broker", None)
    )
