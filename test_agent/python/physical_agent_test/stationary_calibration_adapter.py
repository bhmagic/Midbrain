from __future__ import annotations

import asyncio
from typing import Any, Callable, Protocol

from .phase4_policy import report_operation_progress


class StationaryCalibrationRuntime(Protocol):
    async def run(
        self,
        mode: Any,
        *,
        arm_is_home: bool = False,
        allow_active_control_interrupt: bool = False,
    ) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class StationaryCalibrationSkillAdapter:
    """Deferred, finite wrapper around the maintained calibration Skill."""

    def __init__(
        self,
        runtime_factory: Callable[[], StationaryCalibrationRuntime],
    ):
        self.runtime_factory = runtime_factory
        self._runtime: StationaryCalibrationRuntime | None = None
        self._lock = asyncio.Lock()
        self.last_result: dict[str, Any] | None = None

    async def run(self, *, request: str) -> dict[str, Any]:
        user_request = str(request).strip()
        if not user_request:
            raise ValueError("request must be non-empty")
        if self._lock.locked():
            raise RuntimeError("stationary calibration is already running")
        async with self._lock:
            report_operation_progress("LOAD_STATIONARY_CALIBRATION_SKILL")
            runtime = self.runtime_factory()
            self._runtime = runtime
            try:
                report_operation_progress("RUN_STATIONARY_CALIBRATION_AUTO")
                result = await runtime.run(
                    "auto",
                    arm_is_home=False,
                    allow_active_control_interrupt=False,
                )
            except asyncio.CancelledError:
                report_operation_progress("CANCEL_STATIONARY_CALIBRATION")
                await runtime.cancel()
                raise
            finally:
                self._runtime = None
                report_operation_progress("CLOSE_STATIONARY_CALIBRATION_SKILL")
                await runtime.close()
            if not isinstance(result, dict):
                raise RuntimeError(
                    "stationary calibration returned a non-object result"
                )
            wrapped = {
                **result,
                "agent_request": user_request,
                "agent_adapter": {
                    "adapter_id": (
                        "skill.stationary_world_arm_alignment.cli.v1"
                    ),
                    "execution": "IN_PROCESS_DEFERRED_FINITE",
                    "mode": "auto",
                    "arm_is_home_claimed": False,
                    "active_control_interrupt_allowed": False,
                    "physical_motion_submitted_by_adapter": False,
                },
            }
            self.last_result = wrapped
            return wrapped

    async def cancel(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            await runtime.cancel()
