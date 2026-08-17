from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class GraphValidationError(ValueError):
    """Reject one graph before any child Skill is invoked."""


class ChildAuthorizationRequired(PermissionError):
    """Stop before a child Skill whose exact invocation needs authorization."""

    def __init__(self, tool_name: str, child_call_id: str, reason: str):
        super().__init__(reason)
        self.tool_name = tool_name
        self.child_call_id = child_call_id
        self.reason = reason


class ChildInvocationTimeout(TimeoutError):
    """Report a host-bounded child timeout without inferring side effects."""

    def __init__(self, tool_name: str, child_call_id: str, timeout_s: float):
        super().__init__(f"{tool_name} exceeded its {timeout_s:.3f}s timeout")
        self.tool_name = tool_name
        self.child_call_id = child_call_id
        self.timeout_s = float(timeout_s)


class ChildInvocationNotStarted(RuntimeError):
    """Report a host rejection that occurred before child execution."""

    def __init__(self, tool_name: str, child_call_id: str, reason: str):
        super().__init__(reason)
        self.tool_name = tool_name
        self.child_call_id = child_call_id
        self.reason = reason


@dataclass(frozen=True)
class ChildInvocationResult:
    """Return one child result with host-side prerequisite trace events."""

    result: Any
    preparation_trace: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ChildDescriptor:
    tool_name: str
    skill_type: str
    safety_class: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    expected_latency: str = "UNKNOWN"

    @property
    def read_only(self) -> bool:
        return self.safety_class == "READ_ONLY"

    @property
    def physical(self) -> bool:
        return self.safety_class == "PHYSICAL_MOTION_AUTHORIZATION_REQUIRED"


@dataclass(frozen=True)
class GraphCallContext:
    graph_run_id: str
    graph_sha256: str
    root_call_id: str
    root_context: Any
    node_id: str | None = None
    attempt: int | None = None
    child_call_id: str | None = None
    deadline_monotonic: float | None = None

    def child(
        self,
        *,
        node_id: str,
        attempt: int,
        child_call_id: str,
        deadline_monotonic: float,
    ) -> "GraphCallContext":
        return GraphCallContext(
            graph_run_id=self.graph_run_id,
            graph_sha256=self.graph_sha256,
            root_call_id=self.root_call_id,
            root_context=self.root_context,
            node_id=node_id,
            attempt=attempt,
            child_call_id=child_call_id,
            deadline_monotonic=deadline_monotonic,
        )


@dataclass(frozen=True)
class ModelRouteDecision:
    edge_id: str
    confidence: float
    provenance: dict[str, Any]


class ChildSkillBroker(Protocol):
    def descriptors(self) -> Mapping[str, ChildDescriptor]:
        """Return only child Skills eligible in this Agent host."""

    async def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: GraphCallContext,
    ) -> Any:
        """Invoke one exact child using the direct-call host policy path."""

    async def route_model(
        self,
        *,
        routing_profile: str,
        modality: str,
        instruction: str,
        inputs: dict[str, Any],
        routes: list[dict[str, str]],
        context: GraphCallContext,
    ) -> ModelRouteDecision:
        """Select one predeclared route or raise when no profile is available."""
