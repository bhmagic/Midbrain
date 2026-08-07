from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Protocol

from stationary_world_arm_alignment.candidate_review import canonical_sha256
from stationary_world_arm_alignment.math3d import YawUnobservableError

from .phase4_policy import (
    extend_current_operation_hard_timeout,
    report_operation_progress,
)


FOUNDATIONPOSE_CANONICAL_INVOCATION = (
    "Use FoundationPose to establish the stationary world-to-arm-base "
    "transform."
)
FOUNDATIONPOSE_CANONICAL_NAME = "foundationpose"


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def mentions_foundation_pose(request: str) -> bool:
    words = re.findall(r"[a-z0-9]+", str(request).casefold())
    for start in range(len(words)):
        for word_count in (1, 2, 3):
            candidate = "".join(words[start : start + word_count])
            if not candidate:
                continue
            if abs(len(candidate) - len(FOUNDATIONPOSE_CANONICAL_NAME)) > 2:
                continue
            if _edit_distance(candidate, FOUNDATIONPOSE_CANONICAL_NAME) <= 2:
                return True
    return False


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


class StationaryCalibrationActivator(Protocol):
    async def review_and_activate(
        self,
        *,
        alignment_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]: ...


class StationaryCalibrationSkillAdapter:
    """Deferred, finite wrapper around the maintained calibration Skill."""

    def __init__(
        self,
        runtime_factory: Callable[[], StationaryCalibrationRuntime],
        *,
        operation_hard_timeout_s: float = 600.0,
        activation_service: StationaryCalibrationActivator | None = None,
    ):
        self.runtime_factory = runtime_factory
        self.operation_hard_timeout_s = float(operation_hard_timeout_s)
        self.activation_service = activation_service
        if self.operation_hard_timeout_s <= 0.0:
            raise ValueError(
                "stationary calibration hard timeout must be positive"
            )
        self._runtime: StationaryCalibrationRuntime | None = None
        self._lock = asyncio.Lock()
        self.last_result: dict[str, Any] | None = None

    async def run(self, *, request: str) -> dict[str, Any]:
        user_request = str(request).strip()
        if not user_request:
            raise ValueError("request must be non-empty")
        if not mentions_foundation_pose(user_request):
            result = {
                "status": "FOUNDATIONPOSE_EXPLICIT_INVOCATION_REQUIRED",
                "workflow_complete": False,
                "motion_usable": False,
                "reason_code": "FOUNDATIONPOSE_NOT_EXPLICITLY_REQUESTED",
                "message": (
                    "FoundationPose was not started. The regular Agent route "
                    "may invoke this long-running initializer only when the "
                    "operator explicitly names FoundationPose. Case, spacing, "
                    "hyphenation, and minor spelling errors are accepted. Ordinary "
                    "world-to-arm alignment is reserved for the movement-"
                    "based gripper alignment workflow."
                ),
                "required_name_mention": "FoundationPose",
                "canonical_example": FOUNDATIONPOSE_CANONICAL_INVOCATION,
                "physical_motion_submitted": False,
                "agent_request": user_request,
                "agent_adapter": {
                    "adapter_id": (
                        "skill.stationary_world_arm_alignment.cli.v1"
                    ),
                    "execution": "NOT_STARTED_EXPLICIT_INVOCATION_REQUIRED",
                    "mode": None,
                    "arm_is_home_claimed": False,
                    "active_control_interrupt_allowed": False,
                    "physical_motion_submitted_by_adapter": False,
                    "foundationpose_name_match": False,
                },
            }
            self.last_result = result
            return result
        if self._lock.locked():
            raise RuntimeError("stationary calibration is already running")
        async with self._lock:
            extend_current_operation_hard_timeout(
                self.operation_hard_timeout_s,
                stage="STATIONARY_CALIBRATION_EXTENDED_DEADLINE",
            )
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
            except YawUnobservableError as error:
                result = {
                    "status": "CALIBRATION_POSE_REQUIRED",
                    "workflow_complete": False,
                    "motion_usable": False,
                    "reason_code": "BASE_YAW_UNOBSERVABLE",
                    "message": str(error),
                    "diagnostics": dict(error.diagnostics),
                    "required_operator_action": (
                        "Move the end effector sideways until its horizontal "
                        "distance from the base Z axis exceeds the reported "
                        "minimum, keep the camera/base rig stationary, and "
                        "retry the same calibration request."
                    ),
                }
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
                    "foundationpose_name_match": True,
                },
            }
            candidate = wrapped.get("candidate")
            alignment_id = str(wrapped.get("alignment_id") or "")
            if isinstance(candidate, dict) and alignment_id:
                candidate_digest = canonical_sha256(candidate)
                wrapped.update(
                    {
                        "candidate_sha256": candidate_digest,
                        "workflow_complete": False,
                        "required_next_tool": {
                            "name": (
                                "review_and_activate_stationary_calibration"
                            ),
                            "arguments": {
                                "alignment_id": alignment_id,
                                "candidate_sha256": candidate_digest,
                            },
                        },
                        "agent_instruction": (
                            "This candidate is not yet motion-usable. Call "
                            "required_next_tool immediately with unchanged "
                            "arguments. Do not report the world-to-arm "
                            "relationship as established until that tool "
                            "returns motion_usable=true."
                        ),
                    }
                )
            self.last_result = wrapped
            return wrapped

    async def review_and_activate(
        self,
        *,
        alignment_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        if self.activation_service is None:
            raise RuntimeError(
                "stationary calibration activation is not configured"
            )
        return await self.activation_service.review_and_activate(
            alignment_id=alignment_id,
            candidate_sha256=candidate_sha256,
        )

    def latest_activation_continuation(self) -> dict[str, Any] | None:
        service = self.activation_service
        if service is None or not hasattr(
            service,
            "latest_activation_continuation",
        ):
            return None
        return service.latest_activation_continuation()

    async def cancel(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            await runtime.cancel()
