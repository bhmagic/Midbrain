from __future__ import annotations

import asyncio
import contextvars
import os
import time
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Awaitable, TypeVar
from uuid import uuid4


_T = TypeVar("_T")
_POLICY_MODES = {"SHADOW", "ENFORCED", "FALLBACK"}
_PHYSICAL_MODES = {"DISABLED", "GUARDED", "ENABLED"}
_current_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "phase4_current_operation_id",
    default=None,
)


def _policy_mode(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().upper()
    if value not in _POLICY_MODES:
        raise ValueError(
            f"{name} must be one of {', '.join(sorted(_POLICY_MODES))}"
        )
    return value


def _physical_mode(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().upper()
    if value not in _PHYSICAL_MODES:
        raise ValueError(
            f"{name} must be one of {', '.join(sorted(_PHYSICAL_MODES))}"
        )
    return value


@dataclass(frozen=True)
class Phase4Policy:
    binding: str
    controller_audit: str
    manager_authority: str
    generic_rgbd_route: str
    physical_execution: str
    operation_hard_timeout_s: float
    operation_idle_timeout_s: float
    vlm_attempt_timeout_s: float
    skill_adapter_timeout_s: float

    @classmethod
    def from_environment(cls) -> "Phase4Policy":
        hard_timeout = float(os.getenv("PHASE4_OPERATION_HARD_TIMEOUT_S", "90"))
        idle_timeout = float(os.getenv("PHASE4_OPERATION_IDLE_TIMEOUT_S", "30"))
        vlm_timeout = float(os.getenv("PHASE4_VLM_ATTEMPT_TIMEOUT_S", "45"))
        adapter_timeout = float(os.getenv("PHASE4_SKILL_ADAPTER_TIMEOUT_S", "60"))
        if min(hard_timeout, idle_timeout, vlm_timeout, adapter_timeout) <= 0.0:
            raise ValueError("Phase 4 operation timeouts must be positive")
        if idle_timeout > hard_timeout:
            raise ValueError(
                "PHASE4_OPERATION_IDLE_TIMEOUT_S must not exceed the hard timeout"
            )
        return cls(
            binding=_policy_mode("PHASE4_BINDING_MODE", "SHADOW"),
            controller_audit=_policy_mode(
                "PHASE4_CONTROLLER_AUDIT_MODE",
                "SHADOW",
            ),
            manager_authority=_policy_mode(
                "PHASE4_MANAGER_AUTHORITY_MODE",
                "SHADOW",
            ),
            generic_rgbd_route=_policy_mode(
                "PHASE4_GENERIC_RGBD_ROUTE_MODE",
                "SHADOW",
            ),
            physical_execution=_physical_mode(
                "PHASE4_PHYSICAL_EXECUTION_MODE",
                "DISABLED",
            ),
            operation_hard_timeout_s=hard_timeout,
            operation_idle_timeout_s=idle_timeout,
            vlm_attempt_timeout_s=vlm_timeout,
            skill_adapter_timeout_s=adapter_timeout,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "independent_switches": True,
            "physical_authorization_inherited": False,
        }


@dataclass
class _Operation:
    operation_id: str
    label: str
    state: str
    started_monotonic: float
    last_progress_monotonic: float
    hard_timeout_s: float
    idle_timeout_s: float
    stage: str
    error: str | None = None
    finished_monotonic: float | None = None


class OperationRegistry:
    """Track bounded operations and cancel work that stops reporting progress."""

    def __init__(self, *, retained_operations: int = 64):
        self._lock = Lock()
        self._operations: dict[str, _Operation] = {}
        self._retained_operations = max(8, int(retained_operations))

    def touch(self, stage: str) -> None:
        operation_id = _current_operation_id.get()
        if operation_id is None:
            return
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.state != "RUNNING":
                return
            operation.last_progress_monotonic = time.monotonic()
            operation.stage = str(stage)

    def extend_current_hard_timeout(
        self,
        hard_timeout_s: float,
        *,
        stage: str,
    ) -> None:
        """Latch a longer deadline for the currently executing finite operation."""

        requested_timeout = float(hard_timeout_s)
        if requested_timeout <= 0.0:
            raise ValueError("extended hard timeout must be positive")
        operation_id = _current_operation_id.get()
        if operation_id is None:
            return
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.state != "RUNNING":
                return
            operation.hard_timeout_s = max(
                operation.hard_timeout_s,
                requested_timeout,
            )
            operation.last_progress_monotonic = time.monotonic()
            operation.stage = str(stage)

    async def run(
        self,
        label: str,
        awaitable: Awaitable[_T],
        *,
        hard_timeout_s: float,
        idle_timeout_s: float,
    ) -> _T:
        hard_timeout = float(hard_timeout_s)
        idle_timeout = float(idle_timeout_s)
        if min(hard_timeout, idle_timeout) <= 0.0:
            raise ValueError("operation timeouts must be positive")
        if idle_timeout > hard_timeout:
            raise ValueError("idle timeout must not exceed hard timeout")

        operation_id = str(uuid4())
        now = time.monotonic()
        operation = _Operation(
            operation_id=operation_id,
            label=str(label),
            state="RUNNING",
            started_monotonic=now,
            last_progress_monotonic=now,
            hard_timeout_s=hard_timeout,
            idle_timeout_s=idle_timeout,
            stage="STARTED",
        )
        with self._lock:
            self._operations[operation_id] = operation
            self._trim_locked()

        token = _current_operation_id.set(operation_id)
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=min(0.25, idle_timeout / 4.0),
                )
                if task in done:
                    result = task.result()
                    self._finish(operation_id, "SUCCEEDED", "COMPLETED", None)
                    return result

                now = time.monotonic()
                with self._lock:
                    current = self._operations[operation_id]
                    hard_age = now - current.started_monotonic
                    idle_age = now - current.last_progress_monotonic
                    stage = current.stage
                    effective_hard_timeout = current.hard_timeout_s
                if hard_age >= effective_hard_timeout:
                    task.cancel()
                    await _consume_cancel(task)
                    message = (
                        f"{label} exceeded its "
                        f"{effective_hard_timeout:.3f}s hard deadline "
                        f"during {stage}"
                    )
                    self._finish(
                        operation_id,
                        "FAILED",
                        "HARD_TIMEOUT",
                        message,
                    )
                    raise TimeoutError(message)
                if idle_age >= idle_timeout:
                    task.cancel()
                    await _consume_cancel(task)
                    message = (
                        f"{label} reported no progress for {idle_timeout:.3f}s "
                        f"during {stage}"
                    )
                    self._finish(
                        operation_id,
                        "FAILED",
                        "IDLE_TIMEOUT",
                        message,
                    )
                    raise TimeoutError(message)
        except asyncio.CancelledError:
            task.cancel()
            await _consume_cancel(task)
            self._finish(operation_id, "CANCELLED", "CANCELLED", None)
            raise
        except Exception as error:
            with self._lock:
                current = self._operations.get(operation_id)
                already_finished = current is None or current.state != "RUNNING"
            if not already_finished:
                self._finish(operation_id, "FAILED", "FAILED", str(error))
            raise
        finally:
            _current_operation_id.reset(token)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            operations = sorted(
                self._operations.values(),
                key=lambda item: item.started_monotonic,
                reverse=True,
            )
            return {
                "active_count": sum(
                    operation.state == "RUNNING" for operation in operations
                ),
                "operations": [
                    {
                        "operation_id": operation.operation_id,
                        "label": operation.label,
                        "state": operation.state,
                        "stage": operation.stage,
                        "elapsed_s": round(
                            (
                                operation.finished_monotonic
                                if operation.finished_monotonic is not None
                                else now
                            )
                            - operation.started_monotonic,
                            6,
                        ),
                        "idle_s": round(
                            (
                                operation.finished_monotonic
                                if operation.finished_monotonic is not None
                                else now
                            )
                            - operation.last_progress_monotonic,
                            6,
                        ),
                        "hard_timeout_s": operation.hard_timeout_s,
                        "idle_timeout_s": operation.idle_timeout_s,
                        "error": operation.error,
                    }
                    for operation in operations
                ],
            }

    def _finish(
        self,
        operation_id: str,
        state: str,
        stage: str,
        error: str | None,
    ) -> None:
        with self._lock:
            operation = self._operations[operation_id]
            now = time.monotonic()
            operation.state = state
            operation.stage = stage
            operation.error = error
            operation.last_progress_monotonic = now
            operation.finished_monotonic = now

    def _trim_locked(self) -> None:
        finished = sorted(
            (
                operation
                for operation in self._operations.values()
                if operation.state != "RUNNING"
            ),
            key=lambda item: item.finished_monotonic or item.started_monotonic,
        )
        while len(self._operations) > self._retained_operations and finished:
            operation = finished.pop(0)
            self._operations.pop(operation.operation_id, None)


async def _consume_cancel(task: asyncio.Future[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


_registry: OperationRegistry | None = None


def install_operation_registry(registry: OperationRegistry) -> None:
    global _registry
    _registry = registry


def report_operation_progress(stage: str) -> None:
    if _registry is not None:
        _registry.touch(stage)


def extend_current_operation_hard_timeout(
    hard_timeout_s: float,
    *,
    stage: str,
) -> None:
    if _registry is not None:
        _registry.extend_current_hard_timeout(
            hard_timeout_s,
            stage=stage,
        )


async def await_with_progress_heartbeat(
    awaitable: Awaitable[_T],
    *,
    stage: str,
    interval_s: float = 5.0,
) -> _T:
    interval = float(interval_s)
    if interval <= 0.0:
        raise ValueError("progress heartbeat interval must be positive")
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result()
            report_operation_progress(stage)
    except asyncio.CancelledError:
        task.cancel()
        await _consume_cancel(task)
        raise
