from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from .clients import FabricClient, IntegratedControlClient


def _shortest_angle_delta(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    return (target - current + np.pi) % (2.0 * np.pi) - np.pi


def interpolate_targets(
    current_position_m: list[float],
    current_rpy_rad: list[float],
    target_position_m: list[float],
    target_rpy_rad: list[float],
    *,
    maximum_translation_m: float,
    maximum_orientation_rad: float,
) -> list[dict[str, list[float]]]:
    current_position = np.asarray(current_position_m, dtype=np.float64)
    current_rpy = np.asarray(current_rpy_rad, dtype=np.float64)
    target_position = np.asarray(target_position_m, dtype=np.float64)
    target_rpy = np.asarray(target_rpy_rad, dtype=np.float64)
    for name, value in (
        ("current_position_m", current_position),
        ("current_rpy_rad", current_rpy),
        ("target_position_m", target_position),
        ("target_rpy_rad", target_rpy),
    ):
        if value.shape != (3,) or not np.all(np.isfinite(value)):
            raise ValueError(f"{name} must contain three finite values")
    translation = target_position - current_position
    rotation = _shortest_angle_delta(target_rpy, current_rpy)
    translation_steps = int(
        math.ceil(float(np.linalg.norm(translation)) / maximum_translation_m)
    )
    orientation_steps = int(
        math.ceil(float(np.max(np.abs(rotation))) / maximum_orientation_rad)
    )
    step_count = max(1, translation_steps, orientation_steps)
    return [
        {
            "position_m": (
                current_position + translation * (index / step_count)
            ).tolist(),
            "rpy_rad": (
                current_rpy + rotation * (index / step_count)
            ).tolist(),
        }
        for index in range(1, step_count + 1)
    ]


class MotionExecutor:
    def __init__(
        self,
        *,
        fabric: FabricClient,
        integrated: IntegratedControlClient,
        config: dict[str, Any],
        skill_id: str,
        calibration: dict[str, Any],
        cancelled: Callable[[], bool],
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ):
        self.fabric = fabric
        self.integrated = integrated
        self.config = config
        self.skill_id = skill_id
        self.calibration = calibration
        self.cancelled = cancelled
        self.on_event = on_event
        self.command_sequence = 0

    async def configure(self) -> dict[str, Any]:
        return await self.integrated.settings(
            {
                "execution_mode": "PRESS_MIT",
                "interaction_mode": "ONE_SHOT",
                "ik_mode": "POSE_6DOF",
                "controlled_frame_offset_xyz_m": self.calibration[
                    "controlled_frame_offset_xyz_m"
                ],
                "controlled_frame_offset_rpy_rad": self.calibration[
                    "controlled_frame_offset_rpy_rad"
                ],
                "payload_mass_kg": float(
                    self.calibration["payload_mass_kg"]
                ),
                "payload_com_tool_m": self.calibration[
                    "payload_com_tool_m"
                ],
            }
        )

    async def move_to(
        self,
        *,
        label: str,
        position_m: list[float],
        rpy_rad: list[float],
        requested_speed_m_s: float,
        kp_multiplier: float,
        minimum_duration_s: float,
        require_arrival: bool,
    ) -> list[dict[str, Any]]:
        state = await self.integrated.state()
        measured = (
            (state.get("model_view") or {}).get("measured_controlled_frame")
            or {}
        )
        current_position = measured.get("position_m")
        current_rpy = measured.get("rpy_rad")
        if not isinstance(current_position, list) or not isinstance(
            current_rpy, list
        ):
            raise RuntimeError(
                "Integrated measured controlled frame is unavailable"
            )
        maximum_translation = float(
            self.config["maximum_translation_per_commit_m"]
        )
        maximum_orientation = math.radians(
            float(self.config["maximum_orientation_per_commit_deg"])
        )
        targets = interpolate_targets(
            current_position,
            current_rpy,
            position_m,
            rpy_rad,
            maximum_translation_m=maximum_translation,
            maximum_orientation_rad=maximum_orientation,
        )
        controller_shadow_plan: dict[str, Any] | None = None
        shadow_config = self.config.get("controller_path_planning_shadow", {})
        if bool(shadow_config.get("enabled", False)):
            try:
                controller_shadow_plan = (
                    await self.integrated.plan_transit_path_shadow(
                        target_position_m=list(position_m),
                        target_rpy_rad=list(rpy_rad),
                        requested_speed_m_s=float(requested_speed_m_s),
                        related_skill_id=self.skill_id,
                        command_id=f"cutting-shadow-path-{uuid.uuid4()}",
                    )
                )
            except Exception as error:
                controller_shadow_plan = {
                    "status": "SHADOW_UNAVAILABLE",
                    "enforcement": "SHADOW_NONPHYSICAL",
                    "physical_motion_authorized": False,
                    "error": str(error),
                }
                if bool(shadow_config.get("required", False)):
                    raise RuntimeError(
                        f"Integrated controller path shadow failed: {error}"
                    ) from error
        events: list[dict[str, Any]] = []
        previous = np.asarray(current_position, dtype=np.float64)
        for substep, target in enumerate(targets, start=1):
            if self.cancelled():
                raise asyncio.CancelledError
            next_position = np.asarray(target["position_m"], dtype=np.float64)
            distance = float(np.linalg.norm(next_position - previous))
            duration = max(
                float(minimum_duration_s),
                distance / max(float(requested_speed_m_s), 1e-3),
            )
            event = await self._commit(
                label=label,
                substep=substep,
                substep_count=len(targets),
                position_m=target["position_m"],
                rpy_rad=target["rpy_rad"],
                duration_s=duration,
                kp_multiplier=kp_multiplier,
                require_arrival=require_arrival,
                controller_shadow_plan=controller_shadow_plan,
            )
            events.append(event)
            previous = next_position
        return events

    async def stop_to_float(self) -> dict[str, Any]:
        return await self.integrated.request_float()

    async def _commit(
        self,
        *,
        label: str,
        substep: int,
        substep_count: int,
        position_m: list[float],
        rpy_rad: list[float],
        duration_s: float,
        kp_multiplier: float,
        require_arrival: bool,
        controller_shadow_plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state_before = await self.integrated.state()
        self._assert_state_can_commit(state_before)
        self.command_sequence += 1
        sequence = self.command_sequence
        now_us = time.time_ns() // 1000
        freshness_ms = int(self.config["command_freshness_ms"])
        command = {
            "schema": str(self.config["command_schema"]),
            "schema_version": 1,
            "stream": str(self.config["command_stream"]),
            "provider_id": "skill.vegetable_cutting",
            "provider_instance_id": self.skill_id,
            "boot_id": self.skill_id,
            "sequence": sequence,
            "observed_at_us": now_us,
            "expires_at_us": now_us + freshness_ms * 1000,
            "freshness_ms": freshness_ms,
            "related_skill_id": self.skill_id,
            "valid": True,
            "coordinate_frame": "rebot_arm_base",
            "data": {
                "command_type": "CARTESIAN_TARGET",
                "ik_location": {
                    "position_m": position_m,
                    "rpy_rad": rpy_rad,
                },
                "settings": {
                    "execution_mode": "PRESS_MIT",
                    "interaction_mode": "ONE_SHOT",
                    "ik_mode": "POSE_6DOF",
                    "duration_s": duration_s,
                    "kp_multiplier": kp_multiplier,
                    "controlled_frame_offset_xyz_m": self.calibration[
                        "controlled_frame_offset_xyz_m"
                    ],
                    "controlled_frame_offset_rpy_rad": self.calibration[
                        "controlled_frame_offset_rpy_rad"
                    ],
                    "payload_mass_kg": float(
                        self.calibration["payload_mass_kg"]
                    ),
                    "payload_com_tool_m": self.calibration[
                        "payload_com_tool_m"
                    ],
                },
            },
        }
        baseline_accepted = int(
            (state_before.get("fabric_input") or {}).get(
                "accepted_count"
            )
            or 0
        )
        staging = await self._wait_for_staging(
            sequence,
            baseline_accepted_count=baseline_accepted,
            command=command,
        )
        sequence = int(staging["sequence"])
        preview = await self.integrated.preview()
        self._assert_preview(preview)
        event = {
            "label": label,
            "substep": substep,
            "substep_count": substep_count,
            "sequence": sequence,
            "position_m": list(position_m),
            "rpy_rad": list(rpy_rad),
            "duration_s": duration_s,
            "kp_multiplier": kp_multiplier,
            "preview": preview,
            "controller_path_planning_shadow": controller_shadow_plan,
            "staging": staging,
            "preview_residual_semantics": (
                "INFORMATIONAL_INTEGRATED_IK_TELEMETRY_NOT_A_SKILL_FAILURE"
            ),
            "started_at_us": time.time_ns() // 1000,
        }
        await self._emit({**event, "state": "PREVIEW_ACCEPTED"})
        baseline_commit = int(state_before.get("commit_count") or 0)
        baseline_rejected = int(state_before.get("rejected_count") or 0)
        baseline_completed = int(
            (
                ((state_before.get("trajectory") or {}).get("last_completed") or {})
            ).get("completed_at_us")
            or 0
        )
        if not bool(state_before.get("engaged")):
            await self.integrated.engage(True)
        await self.integrated.teleop({"lb": False})
        await asyncio.sleep(0.06)
        await self.integrated.teleop({"lb": True})
        await asyncio.sleep(0.06)
        await self.integrated.teleop({"lb": False})
        started_state = await self._wait_for_motion_start(
            baseline_commit=baseline_commit,
            baseline_rejected=baseline_rejected,
        )
        completed_state = await self._wait_for_motion_completion(
            baseline_completed_us=baseline_completed,
            duration_s=duration_s,
            allow_noop=int(started_state.get("commit_count") or 0)
            > baseline_commit,
        )
        arrival_residual_mm = (
            self._arrival_residual_mm(completed_state, position_m)
            if require_arrival
            else None
        )
        completed = {
            **event,
            "state": "COMPLETED_FLOAT",
            "completed_at_us": time.time_ns() // 1000,
            "controller_state": completed_state,
            "arrival_residual_mm": arrival_residual_mm,
            "arrival_residual_semantics": (
                "INFORMATIONAL_INTEGRATED_MEASUREMENT_NOT_A_SKILL_FAILURE"
            ),
        }
        await self._emit(completed)
        return completed

    async def _wait_for_staging(
        self,
        sequence: int,
        *,
        baseline_accepted_count: int,
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(
            self.config["stage_accept_timeout_s"]
        )
        last_result = "UNKNOWN"
        last_error: Any = None
        last_sequence = -1
        last_age_ms: float | None = None
        publish_count = 0
        initial_sequence = sequence
        published_sequences: set[int] = set()
        if command is not None:
            self.command_sequence = max(self.command_sequence, sequence)
        freshness_ms = int(self.config["command_freshness_ms"])
        republish_interval_s = max(
            0.05,
            min(0.2, freshness_ms / 2500.0),
        )
        next_publish_at = time.monotonic()
        while time.monotonic() < deadline:
            if self.cancelled():
                raise asyncio.CancelledError
            now = time.monotonic()
            if command is not None and now >= next_publish_at:
                if publish_count > 0:
                    # A new sequence creates another staging opportunity while
                    # the absolute target remains identical and uncommitted.
                    self.command_sequence += 1
                    sequence = self.command_sequence
                    command["sequence"] = sequence
                observed_at_us = time.time_ns() // 1000
                command["observed_at_us"] = observed_at_us
                command["expires_at_us"] = (
                    observed_at_us + freshness_ms * 1000
                )
                command["freshness_ms"] = freshness_ms
                await self.fabric.publish(command)
                published_sequences.add(sequence)
                publish_count += 1
                next_publish_at = now + republish_interval_s
            state = await self.integrated.state()
            fabric_input = state.get("fabric_input") or {}
            last_result = str(fabric_input.get("last_result") or "UNKNOWN")
            last_error = fabric_input.get("last_error")
            last_sequence = int(
                fabric_input.get("last_sequence") or -1
            )
            last_age_value = fabric_input.get("last_age_ms")
            last_age_ms = (
                float(last_age_value)
                if last_age_value is not None
                else None
            )
            accepted_count = int(
                fabric_input.get("accepted_count") or 0
            )
            if (
                last_sequence == sequence
                and (
                    last_result == "ACCEPTED"
                    or accepted_count > baseline_accepted_count
                )
            ):
                return {
                    "initial_sequence": initial_sequence,
                    "sequence": sequence,
                    "publish_count": publish_count,
                    "accepted_count_before": baseline_accepted_count,
                    "accepted_count_after": accepted_count,
                    "last_result": last_result,
                    "last_sequence": last_sequence,
                    "last_age_ms": last_age_ms,
                    "republish_interval_s": republish_interval_s,
                    "freshness_ms": freshness_ms,
                }
            if (
                last_result == "REJECTED"
                and (
                    last_sequence == sequence
                    or last_sequence in published_sequences
                )
            ):
                raise RuntimeError(
                    "Integrated rejected staged target "
                    f"{last_sequence}: {last_error}"
                )
            await asyncio.sleep(float(self.config["poll_interval_s"]))
        raise TimeoutError(
            f"Integrated did not accept staged target {sequence}; "
            f"last result was {last_result}: {last_error}; "
            f"last sequence was {last_sequence}; "
            f"last age was {last_age_ms} ms; "
            f"published {publish_count} time(s)"
        )

    async def _wait_for_motion_start(
        self,
        *,
        baseline_commit: int,
        baseline_rejected: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(
            self.config["motion_start_timeout_s"]
        )
        while time.monotonic() < deadline:
            if self.cancelled():
                raise asyncio.CancelledError
            state = await self.integrated.state()
            rejected = int(state.get("rejected_count") or 0)
            if rejected > baseline_rejected:
                raise RuntimeError(
                    "Integrated rejected the physical commit: "
                    + str(state.get("last_error") or state.get("fault_reason"))
                )
            if int(state.get("commit_count") or 0) > baseline_commit:
                return state
            self._assert_state_health(state)
            await asyncio.sleep(float(self.config["poll_interval_s"]))
        raise TimeoutError("Integrated did not begin the committed MIT target")

    async def _wait_for_motion_completion(
        self,
        *,
        baseline_completed_us: int,
        duration_s: float,
        allow_noop: bool,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(duration_s) + float(
            self.config["motion_completion_margin_s"]
        )
        while time.monotonic() < deadline:
            if self.cancelled():
                raise asyncio.CancelledError
            state = await self.integrated.state()
            self._assert_state_health(state)
            trajectory = state.get("trajectory") or {}
            last_completed = trajectory.get("last_completed") or {}
            completed_at_us = int(last_completed.get("completed_at_us") or 0)
            if not bool(trajectory.get("active")):
                if (
                    completed_at_us > baseline_completed_us
                    and bool(last_completed.get("float_confirmed"))
                ):
                    return state
                if allow_noop and str(state.get("last_error") or "").startswith(
                    "target is already at"
                ):
                    return state
            await asyncio.sleep(float(self.config["poll_interval_s"]))
        raise TimeoutError(
            "Integrated MIT target did not complete with confirmed gravity-float"
        )

    def _assert_state_can_commit(self, state: dict[str, Any]) -> None:
        self._assert_state_health(state)
        trajectory = state.get("trajectory") or {}
        if bool(trajectory.get("active")):
            raise RuntimeError("Integrated already has an active trajectory")
        if str(state.get("residency") or "").upper() != "HOT":
            raise RuntimeError("Integrated controller is not HOT")
        if not bool(state.get("ready")):
            raise RuntimeError("Integrated controller is not ready")

    @staticmethod
    def _assert_state_health(state: dict[str, Any]) -> None:
        health = str(state.get("health") or "").upper()
        control_state = str(state.get("control_state") or "").upper()
        if health != "HEALTHY":
            raise RuntimeError(
                "Integrated health is not HEALTHY: "
                + str(state.get("last_error") or health)
            )
        if control_state.startswith("FAULT") or state.get("fault_reason"):
            raise RuntimeError(
                "Integrated is faulted: "
                + str(state.get("fault_reason") or control_state)
            )

    def _assert_preview(self, preview: dict[str, Any]) -> None:
        if bool(self.config["require_preview_every_commit"]):
            if not bool(preview.get("planning_valid")):
                raise RuntimeError(
                    "Integrated preview rejected the target: "
                    + "; ".join(preview.get("planning_reasons") or [])
                )
            if not bool(preview.get("physical_execution_enabled")):
                raise RuntimeError(
                    "Integrated preview blocks physical execution: "
                    + "; ".join(
                        preview.get("physical_execution_blockers") or []
                    )
                )
            if bool(preview.get("target_clamped")):
                raise RuntimeError(
                    "Integrated preview clamped a supposedly bounded substep"
                )
    @staticmethod
    def _arrival_residual_mm(
        state: dict[str, Any],
        target_position_m: list[float],
    ) -> float | None:
        measured = (
            (state.get("model_view") or {}).get("measured_controlled_frame")
            or {}
        )
        position = measured.get("position_m")
        if not isinstance(position, list):
            return None
        return float(
            np.linalg.norm(
                np.asarray(position, dtype=np.float64)
                - np.asarray(target_position_m, dtype=np.float64)
            )
            * 1000.0
        )

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            await self.on_event(event)
