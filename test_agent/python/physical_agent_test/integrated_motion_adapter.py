from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Protocol

import httpx


class IntegratedMotionClientProtocol(Protocol):
    async def state(self) -> dict[str, Any]:
        """Return the current Integrated Controller state."""

    async def preview_direct_motion(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage and preview a nonphysical Cartesian target."""

    async def engage_staged_motion(self) -> dict[str, Any]:
        """Execute the currently staged and previewed target."""

    async def trigger_one_shot_motion(self) -> dict[str, Any]:
        """Pulse and release the Integrated one-shot commit input."""


_DIRECTION_VECTORS = {
    "UP": (0.0, 1.0, 0.0),
    "DOWN": (0.0, -1.0, 0.0),
    "POSITIVE_X": (1.0, 0.0, 0.0),
    "NEGATIVE_X": (-1.0, 0.0, 0.0),
    "POSITIVE_Z": (0.0, 0.0, 1.0),
    "NEGATIVE_Z": (0.0, 0.0, -1.0),
}


class IntegratedRelativeMotionAdapter:
    """Preview and approve one exact relative Integrated Controller motion."""

    def __init__(
        self,
        client: IntegratedMotionClientProtocol,
        *,
        approval_ttl_s: float = 120.0,
    ):
        self.client = client
        self.approval_ttl_s = float(approval_ttl_s)
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def preview(
        self,
        *,
        direction: str,
        distance_m: float,
    ) -> dict[str, Any]:
        normalized_direction = str(direction or "").strip().upper()
        if normalized_direction not in _DIRECTION_VECTORS:
            raise ValueError(
                "direction must be UP, DOWN, POSITIVE_X, NEGATIVE_X, "
                "POSITIVE_Z, or NEGATIVE_Z"
            )
        distance = float(distance_m)
        if not math.isfinite(distance) or not 0.001 <= distance <= 0.2:
            raise ValueError("distance_m must be between 0.001 and 0.2")

        try:
            state = await self.client.state()
        except httpx.RequestError as exc:
            return self._dependency_unavailable(str(exc))
        if state.get("residency") != "HOT":
            return self._dependency_unavailable(
                "Integrated Controller is not HOT"
            )
        model_view = state.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        current = measured.get("position_m")
        if (
            not isinstance(current, list)
            or len(current) != 3
            or not all(
                isinstance(value, (int, float)) and math.isfinite(float(value))
                for value in current
            )
        ):
            raise RuntimeError(
                "Integrated Controller has no measured controlled-frame pose"
            )
        vector = _DIRECTION_VECTORS[normalized_direction]
        target = [
            float(current[index]) + vector[index] * distance
            for index in range(3)
        ]
        preview, preview_id = await self._stage_target(target)
        pending = {
            "preview_id": preview_id,
            "motion_intent": "NEW_RELATIVE_MOVE",
            "direction": normalized_direction,
            "distance_m": distance,
            "original_request_distance_m": distance,
            "start_position_m": [float(value) for value in current],
            "target_position_m": target,
            "created_monotonic": time.monotonic(),
        }
        async with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if time.monotonic() - value["created_monotonic"]
                <= self.approval_ttl_s
            }
            self._pending[preview_id] = pending
        required_next_arguments = {
            "preview_id": preview_id,
            "motion_intent": pending["motion_intent"],
            "direction": normalized_direction,
            "distance_m": distance,
            "original_request_distance_m": distance,
            "target_position_m": target,
        }
        return {
            "status": "PREVIEW_READY",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "motion_intent": pending["motion_intent"],
            "direction": normalized_direction,
            "distance_m": distance,
            "start_position_m": pending["start_position_m"],
            "target_position_m": target,
            "preview_id": preview_id,
            "approval_required": True,
            "next_tool": "execute_integrated_motion_preview",
            "required_next_tool": {
                "name": "execute_integrated_motion_preview",
                "arguments": required_next_arguments,
            },
            "message": (
                "The exact IK target is previewed but has not moved. Do not "
                "answer the operator yet. Call required_next_tool now with "
                "its arguments unchanged to present operator approval."
            ),
            "integrated_preview": preview,
        }

    @staticmethod
    def _dependency_unavailable(reason: str) -> dict[str, Any]:
        return {
            "status": "DEPENDENCY_UNAVAILABLE",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "retry_same_tool": False,
            "required_provider_sequence": [
                {
                    "provider_id": "robot_arm.rebot_dm",
                    "required_residency": "HOT",
                },
                {
                    "provider_id": "robot_arm.primary.integrated",
                    "required_residency": "HOT",
                },
            ],
            "required_next_tool": {
                "name": "inspect_midbrain_runtime",
                "arguments": {},
            },
            "message": (
                "The controller dependency is unavailable. Do not call this "
                "preview tool again yet. Inspect the current Midbrain runtime, "
                "activate Basic and then Integrated to HOT with approval, and "
                "only then create a fresh preview."
            ),
            "connection_detail": reason,
        }

    async def _stage_target(
        self,
        target: list[float],
    ) -> tuple[dict[str, Any], str]:
        preview = await self.client.preview_direct_motion(
            {
                "command": {
                    "command_type": "CARTESIAN_TARGET",
                    "target": {"position_m": target},
                    "settings": {
                        "execution_mode": "PRESS_MIT",
                        "interaction_mode": "ONE_SHOT",
                        "ik_mode": "POSITION_3DOF",
                        "duration_s": 3.0,
                    },
                },
                "related_skill_id": (
                    "test_agent.relative_effector_motion.v1"
                ),
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            }
        )
        plan = preview.get("preview")
        plan = plan if isinstance(plan, dict) else {}
        preview_id = str(
            preview.get("plan_id") or plan.get("preview_id") or ""
        ).strip()
        if (
            preview.get("status") != "PLANNED"
            or plan.get("planning_valid") is not True
            or not preview_id
        ):
            raise RuntimeError(
                "Integrated Controller rejected the requested IK preview"
            )
        return preview, preview_id

    async def execute(
        self,
        *,
        preview_id: str,
        motion_intent: str,
        direction: str,
        distance_m: float,
        original_request_distance_m: float,
        target_position_m: list[float],
    ) -> dict[str, Any]:
        normalized_preview_id = str(preview_id or "").strip()
        normalized_direction = str(direction or "").strip().upper()
        target = [float(value) for value in target_position_m]
        if len(target) != 3 or not all(math.isfinite(value) for value in target):
            raise ValueError(
                "target_position_m must contain three finite values"
            )
        async with self._lock:
            pending = self._pending.pop(normalized_preview_id, None)
        if pending is None:
            raise RuntimeError(
                "the approved IK preview is missing, expired, or already used"
            )
        if time.monotonic() - pending["created_monotonic"] > self.approval_ttl_s:
            raise RuntimeError("the approved IK preview has expired")
        if (
            str(motion_intent or "").strip().upper()
            != pending["motion_intent"]
            or not math.isclose(
                float(original_request_distance_m),
                pending["original_request_distance_m"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or normalized_direction != pending["direction"]
            or not math.isclose(
                float(distance_m),
                pending["distance_m"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or any(
                not math.isclose(
                    target[index],
                    pending["target_position_m"][index],
                    rel_tol=0.0,
                    abs_tol=1e-8,
                )
                for index in range(3)
            )
        ):
            raise RuntimeError(
                "approved IK arguments do not match the stored preview"
            )

        state = await self.client.state()
        planning = state.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        current_preview = planning.get("last_preview")
        current_preview = (
            current_preview if isinstance(current_preview, dict) else {}
        )
        model_view = state.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        staged = model_view.get("staged_controlled_frame")
        staged = staged if isinstance(staged, dict) else {}
        staged_position = staged.get("position_m")
        if (
            str(current_preview.get("preview_id") or "")
            != normalized_preview_id
            or current_preview.get("planning_valid") is not True
            or current_preview.get("target_revision")
            != planning.get("target_revision")
            or not isinstance(staged_position, list)
            or len(staged_position) != 3
            or any(
                not math.isclose(
                    float(staged_position[index]),
                    target[index],
                    rel_tol=0.0,
                    abs_tol=1e-8,
                )
                for index in range(3)
            )
        ):
            raise RuntimeError(
                "Integrated Controller preview changed before approval; "
                "request a fresh preview"
            )
        starting_commit_count = int(state.get("commit_count") or 0)
        starting_completed = (state.get("trajectory") or {}).get(
            "last_completed"
        )
        engagement = await self.client.engage_staged_motion()
        if engagement.get("status") != "engaged_target_edit":
            raise RuntimeError(
                "Integrated Controller did not enter target-edit engagement"
            )
        trigger = await self.client.trigger_one_shot_motion()
        if trigger.get("physical_motion_authorized") is not True:
            raise RuntimeError(
                "Integrated Controller did not accept the approved one-shot "
                "commit trigger"
            )

        deadline = time.monotonic() + 15.0
        saw_active_trajectory = False
        terminal_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            current = await self.client.state()
            trajectory = current.get("trajectory")
            trajectory = trajectory if isinstance(trajectory, dict) else {}
            saw_active_trajectory = bool(
                saw_active_trajectory or trajectory.get("active")
            )
            commit_count = int(current.get("commit_count") or 0)
            completed = trajectory.get("last_completed")
            if (
                commit_count > starting_commit_count
                and not trajectory.get("active")
                and isinstance(completed, dict)
                and completed != starting_completed
            ):
                terminal_state = current
                break
            if current.get("health") in {"FAULTED", "UNHEALTHY"}:
                raise RuntimeError(
                    "Integrated Controller faulted during approved motion: "
                    f"{current.get('fault_reason') or current.get('last_error')}"
                )
            await asyncio.sleep(0.1)
        if terminal_state is None:
            phase = (
                "trajectory remained active"
                if saw_active_trajectory
                else "controller did not start a trajectory"
            )
            raise RuntimeError(
                "approved Integrated motion did not reach a terminal state "
                f"within 15 seconds: {phase}"
            )
        completion = terminal_state["trajectory"]["last_completed"]
        completion_success = completion.get("completion_success") is True
        completion_outcome = str(
            completion.get("completion_outcome") or "UNKNOWN"
        )
        result = {
            "status": (
                "MOTION_COMPLETED"
                if completion_success
                else "MOTION_FINISHED_WITHOUT_CONFIRMED_ARRIVAL"
            ),
            "physical_motion_requested": True,
            "physical_motion_completed": completion_success,
            "motion_intent": pending["motion_intent"],
            "preview_id": normalized_preview_id,
            "direction": normalized_direction,
            "distance_m": pending["distance_m"],
            "original_request_distance_m": pending[
                "original_request_distance_m"
            ],
            "target_position_m": target,
            "engagement": engagement,
            "one_shot_trigger": trigger,
            "completion": completion,
            "message": (
                "The Integrated Controller confirmed completion of the "
                "approved motion."
                if completion_success
                else "The controller finished the attempt but did not "
                f"confirm target arrival ({completion_outcome})."
            ),
        }
        return result
