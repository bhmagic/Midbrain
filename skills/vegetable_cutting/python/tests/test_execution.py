from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from vegetable_cutting.config import load_skill_config
from vegetable_cutting.artifacts import MonitorArtifacts
from vegetable_cutting.camera import RgbdFrame
from vegetable_cutting.execution import MotionExecutor, interpolate_targets
from vegetable_cutting.models import Phase, RunParameters, SkillState
from vegetable_cutting.skill import VegetableCuttingSkill


def test_target_interpolation_bounds_translation_and_orientation() -> None:
    targets = interpolate_targets(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.21, 0.0, 0.0],
        [0.0, 0.0, np.deg2rad(31.0)],
        maximum_translation_m=0.1,
        maximum_orientation_rad=np.deg2rad(15.0),
    )
    assert len(targets) == 3
    previous_position = np.zeros(3)
    previous_rpy = np.zeros(3)
    for target in targets:
        position = np.asarray(target["position_m"])
        rpy = np.asarray(target["rpy_rad"])
        assert np.linalg.norm(position - previous_position) <= 0.1 + 1e-12
        assert np.max(np.abs(rpy - previous_rpy)) <= np.deg2rad(15.0) + 1e-12
        previous_position = position
        previous_rpy = rpy


class FakeIntegrated:
    def __init__(
        self,
        *,
        preview_valid: bool = True,
        preview_position_residual_m: float = 0.001,
    ):
        self.preview_valid = preview_valid
        self.preview_position_residual_m = preview_position_residual_m
        self.safe_terminate_calls = 0
        self.request_float_calls = 0
        self.staged_target = [0.0, 0.0, 0.0]
        self.staged_rpy = [0.0, 0.0, 0.0]
        self.settings_payloads: list[dict[str, Any]] = []
        self.teleop_payloads: list[dict[str, Any]] = []
        self.shadow_path_requests: list[dict[str, Any]] = []
        self.value: dict[str, Any] = {
            "health": "HEALTHY",
            "residency": "HOT",
            "ready": True,
            "engaged": False,
            "control_state": "GRIPPER_MIT_CLOSE_LATCHED",
            "fault_reason": None,
            "last_error": None,
            "commit_count": 0,
            "rejected_count": 0,
            "runtime": {
                "workspace": {
                    "abs_x_max_m": 0.72,
                    "abs_y_max_m": 0.72,
                    "z_min_m": 0.02,
                    "z_max_m": 0.72,
                }
            },
            "fabric_input": {
                "last_sequence": None,
                "last_result": "NO_OBSERVATION",
                "last_error": None,
                "accepted_count": 0,
            },
            "gripper": {
                "latched_hold": True,
                "active_action": "CLOSE",
            },
            "trajectory": {
                "active": False,
                "last_completed": None,
            },
            "model_view": {
                "measured_controlled_frame": {
                    "position_m": [0.0, 0.0, 0.0],
                    "rpy_rad": [0.0, 0.0, 0.0],
                }
            },
        }

    async def state(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)

    async def settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.settings_payloads.append(copy.deepcopy(payload))
        return copy.deepcopy(payload)

    async def preview(self) -> dict[str, Any]:
        return {
            "planning_valid": self.preview_valid,
            "planning_reasons": [] if self.preview_valid else ["blocked path"],
            "physical_execution_enabled": self.preview_valid,
            "physical_execution_blockers": [],
            "target_clamped": False,
            "position_residual_m": self.preview_position_residual_m,
            "orientation_residual_rad": 0.01,
        }

    async def plan_transit_path_shadow(self, **request: Any) -> dict[str, Any]:
        self.shadow_path_requests.append(copy.deepcopy(request))
        return {
            "status": "PLANNED",
            "plan_id": "shadow-plan-1",
            "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
            "enforcement": "SHADOW_NONPHYSICAL",
            "physical_motion_authorized": False,
        }

    async def engage(self, enabled: bool) -> dict[str, Any]:
        self.value["engaged"] = bool(enabled)
        return {"engaged": bool(enabled)}

    async def teleop(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.teleop_payloads.append(copy.deepcopy(payload))
        if bool(payload.get("lb")):
            self.value["commit_count"] += 1
            completed_at_us = time.time_ns() // 1000
            self.value["trajectory"] = {
                "active": False,
                "last_completed": {
                    "completed_at_us": completed_at_us,
                    "float_confirmed": True,
                },
            }
            self.value["model_view"]["measured_controlled_frame"] = {
                "position_m": list(self.staged_target),
                "rpy_rad": list(self.staged_rpy),
            }
        return {"accepted": True}

    async def request_float(self) -> dict[str, Any]:
        self.request_float_calls += 1
        self.value["engaged"] = False
        return {"status": "gravity_float"}

    async def safe_terminate(self) -> dict[str, Any]:
        self.safe_terminate_calls += 1
        return {
            "status": "accepted",
            "safe_termination": {
                "state": "RUNNING",
                "message": "Safe-home is running",
            },
        }


class FakeFabric:
    def __init__(self, integrated: FakeIntegrated):
        self.integrated = integrated
        self.commands: list[dict[str, Any]] = []

    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(copy.deepcopy(observation))
        sequence = int(observation["sequence"])
        target = observation["data"]["ik_location"]
        self.integrated.staged_target = list(target["position_m"])
        self.integrated.staged_rpy = list(target["rpy_rad"])
        accepted_count = int(
            self.integrated.value["fabric_input"].get(
                "accepted_count"
            )
            or 0
        )
        self.integrated.value["fabric_input"] = {
            "last_sequence": sequence,
            "last_result": "ACCEPTED",
            "last_error": None,
            "accepted_count": accepted_count + 1,
        }
        return {"accepted": True}


def executor(
    integrated: FakeIntegrated,
    *,
    events: list[dict[str, Any]],
) -> MotionExecutor:
    config = copy.deepcopy(load_skill_config()["execution"])
    config["poll_interval_s"] = 0.001
    return MotionExecutor(
        fabric=FakeFabric(integrated),
        integrated=integrated,
        config=config,
        skill_id="skill-test",
        calibration={
            "controlled_frame_offset_xyz_m": [0.1, 0.0, 0.0],
            "controlled_frame_offset_rpy_rad": [0.0, 0.0, 0.0],
            "payload_mass_kg": 0.0,
            "payload_com_tool_m": [0.0, 0.0, 0.0],
        },
        cancelled=lambda: False,
        on_event=lambda event: _append(events, event),
    )


async def _append(
    events: list[dict[str, Any]],
    event: dict[str, Any],
) -> None:
    events.append(copy.deepcopy(event))


class FakeProgress:
    def __init__(self, value: dict[str, Any]):
        self.value = copy.deepcopy(value)

    async def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)

    async def update(self, **changes: Any) -> dict[str, Any]:
        self.value.update(copy.deepcopy(changes))
        return copy.deepcopy(self.value)


def test_executor_previews_and_confirms_float_for_every_bounded_commit() -> None:
    integrated = FakeIntegrated()
    events: list[dict[str, Any]] = []
    runner = executor(integrated, events=events)

    async def run() -> None:
        await runner.configure()
        await runner.move_to(
            label="TRANSFER",
            position_m=[0.15, 0.0, 0.0],
            rpy_rad=[0.0, 0.0, 0.0],
            requested_speed_m_s=0.4,
            kp_multiplier=1.0,
            minimum_duration_s=0.25,
            require_arrival=True,
        )

    asyncio.run(run())
    fabric = runner.fabric
    assert isinstance(fabric, FakeFabric)
    assert len(fabric.commands) == 3
    assert len(integrated.shadow_path_requests) == 1
    assert integrated.shadow_path_requests[0]["target_position_m"] == [
        0.15,
        0.0,
        0.0,
    ]
    assert integrated.value["commit_count"] == 3
    assert [event["state"] for event in events] == [
        "PREVIEW_ACCEPTED",
        "COMPLETED_FLOAT",
        "PREVIEW_ACCEPTED",
        "COMPLETED_FLOAT",
        "PREVIEW_ACCEPTED",
        "COMPLETED_FLOAT",
    ]
    completed_events = [
        event for event in events if event["state"] == "COMPLETED_FLOAT"
    ]
    assert all(
        event["arrival_residual_semantics"]
        == "INFORMATIONAL_INTEGRATED_MEASUREMENT_NOT_A_SKILL_FAILURE"
        for event in completed_events
    )
    assert all(event["arrival_residual_mm"] == pytest.approx(0.0) for event in completed_events)
    assert all(
        event["controller_path_planning_shadow"]["planner_owner"]
        == "ROBOT_ARM_INTEGRATED_CONTROLLER"
        for event in completed_events
    )
    for command in fabric.commands:
        settings = command["data"]["settings"]
        assert settings["execution_mode"] == "PRESS_MIT"
        assert settings["interaction_mode"] == "ONE_SHOT"
        assert settings["ik_mode"] == "POSE_6DOF"
        assert command["freshness_ms"] == 500


def test_staging_acceptance_survives_stale_result_overwrite() -> None:
    integrated = FakeIntegrated()
    integrated.value["fabric_input"] = {
        "last_sequence": 3,
        "last_result": "STALE_IGNORED",
        "last_error": None,
        "accepted_count": 3,
    }
    runner = executor(integrated, events=[])

    asyncio.run(
        runner._wait_for_staging(
            3,
            baseline_accepted_count=2,
        )
    )


def test_staging_waits_across_new_producer_sequence_restart() -> None:
    integrated = FakeIntegrated()
    integrated.value["fabric_input"] = {
        "last_sequence": 3,
        "last_result": "STALE_IGNORED",
        "last_error": None,
        "accepted_count": 3,
    }
    original_state = integrated.state
    state_calls = 0

    async def state_with_delayed_new_producer() -> dict[str, Any]:
        nonlocal state_calls
        state_calls += 1
        if state_calls == 2:
            integrated.value["fabric_input"] = {
                "last_sequence": 1,
                "last_result": "STALE_IGNORED",
                "last_error": None,
                "accepted_count": 4,
            }
        return await original_state()

    integrated.state = state_with_delayed_new_producer
    runner = executor(integrated, events=[])

    asyncio.run(
        runner._wait_for_staging(
            1,
            baseline_accepted_count=3,
        )
    )

    assert state_calls == 2


def test_staging_republishes_same_command_identity_until_acknowledged() -> None:
    integrated = FakeIntegrated()
    integrated.value["fabric_input"] = {
        "last_sequence": 20,
        "last_result": "STALE_IGNORED",
        "last_error": None,
        "last_age_ms": 700.0,
        "accepted_count": 20,
    }

    class DelayedFabric:
        def __init__(self) -> None:
            self.commands: list[dict[str, Any]] = []

        async def publish(
            self,
            observation: dict[str, Any],
        ) -> dict[str, Any]:
            self.commands.append(copy.deepcopy(observation))
            if len(self.commands) == 3:
                integrated.value["fabric_input"] = {
                    "last_sequence": int(observation["sequence"]),
                    "last_result": "ACCEPTED",
                    "last_error": None,
                    "last_age_ms": 25.0,
                    "accepted_count": 21,
                }
            return {"accepted": True}

    runner = executor(integrated, events=[])
    delayed = DelayedFabric()
    runner.fabric = delayed  # type: ignore[assignment]
    runner.config["command_freshness_ms"] = 100
    runner.config["stage_accept_timeout_s"] = 0.4
    command = {
        "sequence": 21,
        "observed_at_us": 0,
        "expires_at_us": 0,
        "freshness_ms": 100,
        "data": {
            "ik_location": {
                "position_m": [0.1, 0.2, 0.3],
                "rpy_rad": [0.0, 0.0, 0.0],
            }
        },
    }

    result = asyncio.run(
        runner._wait_for_staging(
            21,
            baseline_accepted_count=20,
            command=command,
        )
    )

    assert result["publish_count"] == 3
    assert result["accepted_count_after"] == 21
    assert len(delayed.commands) == 3
    assert [item["sequence"] for item in delayed.commands] == [21, 22, 23]
    assert result["initial_sequence"] == 21
    assert result["sequence"] == 23
    assert {
        tuple(item["data"]["ik_location"]["position_m"])
        for item in delayed.commands
    } == {(0.1, 0.2, 0.3)}
    assert [
        item["observed_at_us"] for item in delayed.commands
    ] == sorted(item["observed_at_us"] for item in delayed.commands)
    assert all(
        item["expires_at_us"]
        == item["observed_at_us"] + 100_000
        for item in delayed.commands
    )


def test_staging_stops_after_rejection_of_any_sequence_from_same_attempt() -> None:
    integrated = FakeIntegrated()

    class RejectingFabric:
        def __init__(self) -> None:
            self.commands: list[dict[str, Any]] = []

        async def publish(
            self,
            observation: dict[str, Any],
        ) -> dict[str, Any]:
            self.commands.append(copy.deepcopy(observation))
            integrated.value["fabric_input"] = {
                "last_sequence": int(observation["sequence"]),
                "last_result": "REJECTED",
                "last_error": (
                    "target X 0.7201 m is outside the configured workspace"
                ),
                "accepted_count": 0,
            }
            return {"accepted": True}

    runner = executor(integrated, events=[])
    rejecting = RejectingFabric()
    runner.fabric = rejecting  # type: ignore[assignment]
    command = {
        "sequence": 31,
        "observed_at_us": 0,
        "expires_at_us": 0,
        "freshness_ms": 500,
        "data": {
            "ik_location": {
                "position_m": [0.7201, 0.0, 0.2],
                "rpy_rad": [0.0, 0.0, 0.0],
            }
        },
    }

    with pytest.raises(
        RuntimeError,
        match="rejected staged target 31.*target X 0.7201",
    ):
        asyncio.run(
            runner._wait_for_staging(
                31,
                baseline_accepted_count=0,
                command=command,
            )
        )

    assert len(rejecting.commands) == 1


def test_arrival_residual_is_informational_even_above_twenty_mm() -> None:
    state = {
        "model_view": {
            "measured_controlled_frame": {
                "position_m": [0.02369, 0.0, 0.0],
            }
        }
    }

    residual_mm = MotionExecutor._arrival_residual_mm(
        state,
        [0.0, 0.0, 0.0],
    )

    assert residual_mm == pytest.approx(23.69)


def test_executor_does_not_press_lb_when_preview_is_rejected() -> None:
    integrated = FakeIntegrated(preview_valid=False)
    runner = executor(integrated, events=[])

    async def run() -> None:
        await runner.configure()
        with pytest.raises(RuntimeError, match="preview rejected"):
            await runner.move_to(
                label="BLOCKED",
                position_m=[0.05, 0.0, 0.0],
                rpy_rad=[0.0, 0.0, 0.0],
                requested_speed_m_s=0.4,
                kp_multiplier=1.0,
                minimum_duration_s=0.25,
                require_arrival=True,
            )

    asyncio.run(run())
    assert integrated.teleop_payloads == []
    assert integrated.value["commit_count"] == 0


def test_preview_position_residual_is_informational_for_mit_commit() -> None:
    integrated = FakeIntegrated(preview_position_residual_m=0.02695)
    events: list[dict[str, Any]] = []
    runner = executor(integrated, events=events)

    async def run() -> None:
        await runner.configure()
        await runner.move_to(
            label="MIT_WITH_IK_RESIDUAL",
            position_m=[0.05, 0.0, 0.0],
            rpy_rad=[0.0, 0.0, 0.0],
            requested_speed_m_s=0.4,
            kp_multiplier=1.0,
            minimum_duration_s=0.25,
            require_arrival=True,
        )

    asyncio.run(run())

    assert integrated.value["commit_count"] == 1
    preview_event = next(
        event for event in events if event["state"] == "PREVIEW_ACCEPTED"
    )
    assert preview_event["preview"]["position_residual_m"] == pytest.approx(
        0.02695
    )
    assert (
        preview_event["preview_residual_semantics"]
        == "INFORMATIONAL_INTEGRATED_IK_TELEMETRY_NOT_A_SKILL_FAILURE"
    )


def test_executor_does_not_require_gripper_latch_telemetry() -> None:
    integrated = FakeIntegrated()
    integrated.value["control_state"] = "GRAVITY_FLOAT"
    integrated.value["gripper"] = {
        "latched_hold": False,
        "active_action": None,
    }
    runner = executor(integrated, events=[])

    async def run() -> None:
        await runner.configure()
        await runner.move_to(
            label="HARD_MOUNT_NO_GRIPPER_LATCH",
            position_m=[0.05, 0.0, 0.0],
            rpy_rad=[0.0, 0.0, 0.0],
            requested_speed_m_s=0.4,
            kp_multiplier=1.0,
            minimum_duration_s=0.25,
            require_arrival=True,
        )

    asyncio.run(run())

    assert integrated.value["commit_count"] == 1


def test_cut_targets_apply_one_global_alignment_translation() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.execution_translation_arm_base_m = np.asarray(
        [0.01, -0.02, 0.0]
    )
    skill.execution_rotation_rpy_rad = np.asarray([0.01, 0.02, 0.03])
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [{"center_arm_base_m": [0.5, 0.1, 0.02]}],
        "execution_preview": {
            "approach_board_offset_mm": 70.0,
            "segments": [
                {
                    "cut_index": 0,
                    "target": {
                        "blade_frame": {
                            "rotation_matrix_arm_base": np.eye(3).tolist()
                        }
                    },
                }
            ],
        },
    }

    approach, approach_rpy = skill._cut_target(
        plan,
        cut_index=0,
        approach=True,
    )
    contact, contact_rpy = skill._cut_target(
        plan,
        cut_index=0,
        approach=False,
    )

    assert approach == pytest.approx([0.51, 0.08, 0.09])
    assert contact == pytest.approx([0.51, 0.08, 0.02])
    assert approach_rpy == pytest.approx([0.01, 0.02, 0.03])
    assert contact_rpy == pytest.approx([0.01, 0.02, 0.03])


def test_first_cut_transfer_preserves_gripper_orientation_until_review() -> None:
    class FakeCamera:
        def __init__(self) -> None:
            self.calls = 0

        async def capture(self) -> RgbdFrame:
            self.calls += 1
            return RgbdFrame(
                rgb=np.zeros((12, 16, 3), dtype=np.uint8),
                depth_m=np.ones((12, 16), dtype=np.float32),
                intrinsics={
                    "fx": 10.0,
                    "fy": 10.0,
                    "cx": 8.0,
                    "cy": 6.0,
                },
                timestamp_us=self.calls,
                frame_number=self.calls,
                camera_frame="camera",
                session_epoch="epoch",
                calibration_revision="revision",
                observations={},
            )

    class RecordingExecutor:
        def __init__(self) -> None:
            self.configure_calls = 0
            self.moves: list[dict[str, Any]] = []

        async def configure(self) -> dict[str, Any]:
            self.configure_calls += 1
            return {}

        async def move_to(self, **payload: Any) -> list[dict[str, Any]]:
            self.moves.append(copy.deepcopy(payload))
            return []

    integrated = FakeIntegrated()
    integrated.value["model_view"]["measured_controlled_frame"] = {
        "position_m": [0.25, 0.10, 0.30],
        "rpy_rad": [0.10, 0.20, 0.30],
    }
    runner = RecordingExecutor()
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.integrated = integrated
    skill.motion_executor = runner
    skill.camera = FakeCamera()
    transform_translations = iter(
        ([0.05, 0.05, 0.0], [0.10, 0.10, 0.0])
    )

    async def capture_transforms(_: RgbdFrame) -> dict[str, Any]:
        return {
            "arm_from_camera": {
                "translation_m": next(transform_translations),
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        }

    skill._capture_frame_transforms = capture_transforms
    skill.progress = FakeProgress({"execution": {}})
    skill.execution_events = []
    skill.execution_translation_arm_base_m = np.zeros(3)
    skill.execution_rotation_rpy_rad = np.zeros(3)
    skill.execution_cut_centers_arm_base_m = None

    async def skip_visual_prealignment(_: dict[str, Any]) -> None:
        return None

    skill._prealign_first_cut_at_clearance = skip_visual_prealignment
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [
            {
                "center_camera_m": [0.50, 0.20, 0.02],
                "center_arm_base_m": [0.50, 0.20, 0.02],
            }
        ],
        "execution_preview": {
            "approach_board_offset_mm": 60.0,
            "segments": [
                {
                    "cut_index": 0,
                    "target": {
                        "blade_frame": {
                            "rotation_matrix_arm_base": np.eye(3).tolist()
                        }
                    },
                }
            ],
        },
    }

    asyncio.run(skill._transfer_to_first_cut_approach(plan))

    assert runner.configure_calls == 1
    assert [move["label"] for move in runner.moves] == [
        "INITIAL_VERTICAL_CLEARANCE_LIFT",
        "WRIST_SINGULARITY_ESCAPE_POSITIVE_Y",
        "CLEARANCE_ARM_BASE_X_TRANSFER",
        "CLEARANCE_ARM_BASE_Y_TRANSFER",
        "RECAPTURED_XY_ALIGNMENT_AT_CLEARANCE",
        "VERTICAL_DESCENT_TO_FIRST_APPROACH",
    ]
    assert runner.moves[0]["position_m"] == pytest.approx(
        [0.25, 0.10, 0.45]
    )
    assert runner.moves[0]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[1]["position_m"] == pytest.approx(
        [0.25, 0.275, 0.45]
    )
    assert runner.moves[1]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[2]["position_m"] == pytest.approx(
        [0.55, 0.275, 0.45]
    )
    assert runner.moves[2]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[3]["position_m"] == pytest.approx(
        [0.55, 0.25, 0.45]
    )
    assert runner.moves[3]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[4]["position_m"] == pytest.approx(
        [0.60, 0.30, 0.45]
    )
    assert runner.moves[4]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[5]["position_m"] == pytest.approx(
        [0.60, 0.30, 0.17]
    )
    assert runner.moves[5]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )
    assert runner.moves[5]["requested_speed_m_s"] == pytest.approx(0.08)
    assert skill.camera.calls == 2
    assert np.allclose(
        skill.execution_cut_centers_arm_base_m,
        [[0.60, 0.30, 0.02]],
    )
    refresh_events = [
        event
        for event in skill.execution_events
        if event["state"] == "EXECUTION_CUT_GEOMETRY_REFRESHED"
    ]
    assert [event["reason"] for event in refresh_events] == [
        "AFTER_INITIAL_CLEARANCE_LIFT",
        "BEFORE_FIRST_CUT_REVIEW_APPROACH",
    ]
    assert all(
        event["alignment_translation_correction_m"] == [0.0, 0.0, 0.0]
        and event["controlled_frame_offset_application"]
        == "INTEGRATED_ONLY_NOT_ADDED_TO_TARGET_POSITION"
        for event in refresh_events
    )


def test_nearby_first_cut_reapproach_skips_redundant_clearance_route() -> None:
    class RecordingExecutor:
        def __init__(self) -> None:
            self.moves: list[dict[str, Any]] = []

        async def configure(self) -> dict[str, Any]:
            return {}

        async def move_to(self, **payload: Any) -> list[dict[str, Any]]:
            self.moves.append(copy.deepcopy(payload))
            return []

    integrated = FakeIntegrated()
    integrated.value["model_view"]["measured_controlled_frame"] = {
        "position_m": [0.25, 0.10, 0.30],
        "rpy_rad": [0.10, 0.20, 0.30],
    }
    runner = RecordingExecutor()
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.integrated = integrated
    skill.motion_executor = runner
    skill.execution_translation_arm_base_m = np.zeros(3)
    skill.execution_rotation_rpy_rad = np.zeros(3)
    skill.execution_control_rpy_rad = None
    skill.execution_cut_centers_arm_base_m = None
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [{"center_arm_base_m": [0.26, 0.10, 0.15]}],
        "execution_preview": {
            "approach_board_offset_mm": 60.0,
            "segments": [],
        },
    }

    asyncio.run(skill._transfer_to_first_cut_approach(plan))

    assert [move["label"] for move in runner.moves] == [
        "NEARBY_REAPPROACH_TO_FIRST_REVIEW"
    ]
    assert runner.moves[0]["position_m"] == pytest.approx(
        [0.26, 0.10, 0.30]
    )
    assert runner.moves[0]["rpy_rad"] == pytest.approx(
        [0.10, 0.20, 0.30]
    )


def test_first_cut_visual_servo_recaptures_after_each_bounded_move() -> None:
    class FakeCamera:
        def __init__(self) -> None:
            self.calls = 0

        async def capture(self) -> RgbdFrame:
            self.calls += 1
            return RgbdFrame(
                rgb=np.zeros((120, 160, 3), dtype=np.uint8),
                depth_m=np.full((120, 160), 0.8, dtype=np.float32),
                intrinsics={
                    "fx": 120.0,
                    "fy": 120.0,
                    "cx": 80.0,
                    "cy": 60.0,
                },
                timestamp_us=self.calls,
                frame_number=self.calls,
                camera_frame="camera",
                session_epoch="epoch",
                calibration_revision="revision",
                observations={},
            )

    class FakeVision:
        def __init__(self) -> None:
            self.calls = 0

        async def assess_first_cut_alignment(
            self,
            _: np.ndarray,
            __: np.ndarray,
            *,
            depth_near_m: float,
            depth_far_m: float,
            target_depth_m: float,
        ) -> dict[str, Any]:
            self.calls += 1
            assert depth_near_m == pytest.approx(0.8)
            assert depth_far_m == pytest.approx(0.8)
            assert target_depth_m == pytest.approx(1.15)
            blade_points = (
                [500, 440],
                [500, 566],
                [500, 500],
            )
            blade_point = blade_points[self.calls - 1]
            return {
                "blade_and_target_visible": True,
                "depth_evidence_used": True,
                "depth_alignment_meaningful": self.calls == 3,
                "orange_cut_target_matches_vegetable": True,
                "blade_controlled_point_yx_1000": blade_point,
                "person_or_animal_visible_in_workspace": False,
                "confidence": 0.95,
                "notes": "synthetic visual-servo observation",
            }

    class RecordingExecutor:
        def __init__(self) -> None:
            self.moves: list[dict[str, Any]] = []

        async def move_to(self, **payload: Any) -> list[dict[str, Any]]:
            self.moves.append(copy.deepcopy(payload))
            return []

    camera = FakeCamera()
    vision = FakeVision()
    runner = RecordingExecutor()
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.camera = camera
    skill.scene_vision = vision
    skill.motion_executor = runner
    skill.integrated = FakeIntegrated()
    skill.artifacts = MonitorArtifacts()
    skill.execution_translation_arm_base_m = np.zeros(3)
    skill.execution_rotation_rpy_rad = np.zeros(3)
    skill.first_cut_correction_count = 0
    skill.first_cut_alignment_attempt_count = 0
    skill.execution_events = []
    skill.execution_task = None

    async def capture_transforms(_: RgbdFrame) -> dict[str, Any]:
        return {
            "arm_from_camera": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [
                    0.0,
                    0.0,
                    float(np.sqrt(0.5)),
                    float(np.sqrt(0.5)),
                ],
            }
        }

    skill._capture_frame_transforms = capture_transforms
    skill.progress = FakeProgress(
        {
            "state": SkillState.RUNNING,
            "phase": Phase.TRANSFER_TO_FIRST_CUT,
            "execution": {"state": "FIRST_CUT_VISUAL_SERVO"},
        }
    )
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [
            {
                "center_arm_base_m": [0.50, 0.20, 0.02],
                "entry_arm_base_m": [-0.05, 0.0, 0.02],
                "exit_arm_base_m": [0.05, 0.0, 0.02],
                "entry_camera_m": [-0.05, 0.0, 1.0],
                "exit_camera_m": [0.05, 0.0, 1.0],
            }
        ],
        "execution_preview": {
            "approach_board_offset_mm": 60.0,
            "segments": [
                {
                    "cut_index": 0,
                    "target": {
                        "blade_frame": {
                            "rotation_matrix_arm_base": np.eye(3).tolist()
                        }
                    },
                }
            ],
        },
    }

    asyncio.run(skill._align_first_cut(plan))
    progress = asyncio.run(skill.progress.snapshot())

    assert camera.calls == 2
    assert vision.calls == 2
    assert len(runner.moves) == 1
    assert skill.execution_translation_arm_base_m == pytest.approx(
        [0.0, 0.05, 0.0]
    )
    assert progress["phase"] == Phase.WAIT_FIRST_CUT_CONFIRMATION
    loop = progress["execution"]["first_cut_alignment_loop"]
    assert [entry["move_applied"] for entry in loop] == [
        True,
        False,
    ]
    assert all(
        entry["depth_evidence"]["registered_to_rgb"]
        and entry["depth_evidence"]["vlm_image_count"] == 2
        and entry["vlm_observation"]["depth_evidence_used"]
        for entry in loop
    )
    assert loop[0]["camera_to_arm_base_translation_conversion"][
        "translation_offset_camera_mm"
    ] == pytest.approx([95.833333, 0.0, 0.0], abs=1e-5)
    assert loop[0]["camera_to_arm_base_translation_conversion"][
        "translation_offset_arm_base_mm"
    ] == pytest.approx([0.0, 95.833333, 0.0], abs=1e-5)
    assert loop[0]["iterative_motion_bound"][
        "applied_translation_arm_base_m"
    ] == pytest.approx([0.0, 0.05, 0.0])
    assert loop[0]["iterative_motion_bound"][
        "recapture_required_after_move"
    ] is True
    assert (
        loop[1]["loop_termination"]["reason"]
        == "PIXEL_SERVO_RESIDUAL_DID_NOT_IMPROVE"
    )
    assert (
        loop[1]["pixel_servo_improvement"]["actual_improvement_mm"]
        < 2.0
    )
    assert (
        progress["execution"]["first_cut_alignment"]["status"]
        == "VLM_RESIDUAL_NOT_IMPROVING_HUMAN_REVIEW_REQUIRED"
    )


def test_first_cut_visual_target_does_not_follow_execution_translation() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.execution_translation_arm_base_m = np.asarray(
        [0.0, 0.2, 0.0],
        dtype=np.float64,
    )

    async def capture_transforms(_: RgbdFrame) -> dict[str, Any]:
        return {
            "arm_from_camera": {
                "translation_m": [0.4, -0.3, 0.2],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        }

    skill._capture_frame_transforms = capture_transforms
    frame = RgbdFrame(
        rgb=np.zeros((10, 10, 3), dtype=np.uint8),
        depth_m=np.ones((10, 10), dtype=np.float32),
        intrinsics={"fx": 10.0, "fy": 10.0, "cx": 5.0, "cy": 5.0},
        timestamp_us=1,
        frame_number=1,
        camera_frame="camera",
        session_epoch="epoch",
        calibration_revision="revision",
        observations={},
    )
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [
            {
                "entry_arm_base_m": [0.4, 0.1, 0.02],
                "exit_arm_base_m": [0.5, 0.1, 0.02],
                "entry_camera_m": [-0.1, 0.0, 0.8],
                "exit_camera_m": [0.1, 0.0, 0.8],
            }
        ],
    }

    entry, exit_point, transforms = asyncio.run(
        skill._first_cut_review_line_camera(plan, frame)
    )

    assert entry == pytest.approx([-0.1, 0.0, 0.95])
    assert exit_point == pytest.approx([0.1, 0.0, 0.95])
    assert transforms["first_cut_review_target"][
        "translation_independent"
    ] is True
    assert transforms["first_cut_review_target"][
        "execution_translation_excluded"
    ] is True


def test_cut_sequence_uses_ten_x_cut_and_retract_with_100_mm_lift() -> None:
    class RecordingExecutor:
        def __init__(self) -> None:
            self.moves: list[dict[str, Any]] = []

        async def move_to(self, **payload: Any) -> list[dict[str, Any]]:
            self.moves.append(copy.deepcopy(payload))
            return []

    runner = RecordingExecutor()
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.motion_executor = runner
    skill.execution_cancelled = False
    skill.execution_translation_arm_base_m = np.zeros(3)
    skill.execution_rotation_rpy_rad = np.zeros(3)
    skill.execution_control_rpy_rad = np.zeros(3)
    skill.execution_cut_centers_arm_base_m = None
    skill.execution_events = []
    skill.execution_task = None
    skill.progress = FakeProgress(
        {
            "state": SkillState.RUNNING,
            "phase": Phase.CUTTING,
            "execution": {"state": "CUTTING"},
        }
    )
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "cuts": [
            {"center_arm_base_m": [0.50, 0.10, 0.02]},
            {"center_arm_base_m": [0.50, 0.12, 0.02]},
        ],
        "execution_preview": {
            "approach_board_offset_mm": 60.0,
            "segments": [],
        },
    }

    asyncio.run(skill._run_cut_sequence(plan))

    labels = [move["label"] for move in runner.moves]
    assert labels == [
        "MIT_CUT_1",
        "RETRACT_AFTER_CUT_1",
        "SHIFT_TO_CUT_2_CLEARANCE",
        "DESCEND_TO_CUT_2_APPROACH",
        "MIT_CUT_2",
        "RETRACT_AFTER_CUT_2",
    ]
    assert runner.moves[0]["kp_multiplier"] == 10.0
    assert runner.moves[1]["kp_multiplier"] == 10.0
    assert runner.moves[1]["position_m"] == pytest.approx(
        [0.50, 0.10, 0.12]
    )
    assert runner.moves[2]["position_m"] == pytest.approx(
        [0.50, 0.12, 0.12]
    )
    assert runner.moves[3]["position_m"] == pytest.approx(
        [0.50, 0.12, 0.08]
    )
    assert runner.moves[2]["kp_multiplier"] == 1.5
    assert runner.moves[4]["kp_multiplier"] == 10.0
    assert runner.moves[5]["kp_multiplier"] == 10.0


def test_abort_with_human_confirmed_tool_requires_removal_before_safe_terminate() -> None:
    integrated = FakeIntegrated()
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.execution_cancelled = False
    skill.execution_task = None
    skill.execution_events = [{"state": "PREVIEW_ACCEPTED"}]
    skill.progress = FakeProgress(
        {
            "state": SkillState.RUNNING,
            "phase": Phase.CUTTING,
            "execution": {"state": "CUTTING"},
            "motion_submitted": True,
            "error": None,
            "operator_tool_attachment_confirmed": True,
        }
    )

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        stopped = await skill.abort("operator stop")
        terminated = await skill.confirm_tool_removed_and_safe_terminate()
        return stopped, terminated

    stopped, terminated = asyncio.run(run())

    assert stopped["state"] == SkillState.WAITING_FOR_OPERATOR
    assert stopped["phase"] == Phase.WAIT_TOOL_REMOVAL
    assert stopped["execution"]["state"] == "ABORTED_WAIT_TOOL_REMOVAL"
    assert integrated.safe_terminate_calls == 1
    assert terminated["state"] == SkillState.ABORTED
    assert terminated["phase"] == Phase.SAFE_TERMINATING
    assert (
        terminated["execution"]["state"]
        == "SAFE_TERMINATION_STARTED_AFTER_ABORT"
    )
    assert terminated["operator_tool_attachment_confirmed"] is False


def test_unconfirmed_safe_termination_does_not_claim_homing_started() -> None:
    class UnconfirmedIntegrated(FakeIntegrated):
        async def safe_terminate(self) -> dict[str, Any]:
            self.safe_terminate_calls += 1
            return {
                "status": "unconfirmed",
                "safe_termination": {
                    "state": "LAUNCH_UNCONFIRMED",
                    "message": "helper did not acknowledge startup",
                },
            }

    integrated = UnconfirmedIntegrated()
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.execution_events = []
    skill.progress = FakeProgress(
        {
            "state": SkillState.WAITING_FOR_OPERATOR,
            "phase": Phase.WAIT_TOOL_REMOVAL,
            "execution": {"state": "WAIT_TOOL_REMOVAL"},
            "motion_submitted": True,
            "error": None,
        }
    )

    result = asyncio.run(
        skill.confirm_tool_removed_and_safe_terminate()
    )

    assert result["state"] == SkillState.WAITING_FOR_OPERATOR
    assert result["phase"] == Phase.WAIT_TOOL_REMOVAL
    assert (
        result["execution"]["state"]
        == "SAFE_TERMINATION_LAUNCH_NOT_CONFIRMED"
    )
    assert result["error"] == "helper did not acknowledge startup"


def test_corrected_sequence_is_checked_before_later_cut_leaves_workspace() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.execution_cut_centers_arm_base_m = None
    plan = {
        "board": {"normal_arm_base": [0.0, 0.0, 1.0]},
        "execution_preview": {"approach_board_offset_mm": 60.0},
        "cuts": [
            {"center_arm_base_m": [0.6544, -0.19, 0.14]},
            {"center_arm_base_m": [0.6787, -0.01, 0.14]},
        ],
    }
    state = {
        "runtime": {
            "workspace": {
                "abs_x_max_m": 0.72,
                "abs_y_max_m": 0.72,
                "z_min_m": 0.02,
                "z_max_m": 0.72,
            }
        }
    }

    with pytest.raises(
        RuntimeError,
        match="cut 2 CUT_CONTACT.*X 0.7282",
    ):
        skill._assert_corrected_cut_workspace(
            plan,
            state,
            translation_arm_base_m=np.asarray(
                [0.0495, 0.0, 0.0],
                dtype=np.float64,
            ),
        )


def test_session_calibration_is_created_before_physical_location_review() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    calibration = skill._build_session_tool_calibration(
        plan_id="plan-one",
        plan_revision=1,
        consistency={
            "eligible_for_operator_review": True,
            "required_observations": 1,
            "representative_acting_point_from_tool_m": [
                0.21,
                0.0,
                -0.07,
            ],
            "representative_controlled_frame_rpy_from_tool": [
                2.1,
                0.0,
                0.0,
            ],
        },
    )

    assert calibration["source_observations"] == 1
    assert calibration["operator_reviewed"] is False
    assert (
        calibration["operator_review_deferred_to_first_cut_approach"]
        is True
    )
    assert calibration["controlled_frame_offset_xyz_m"] == pytest.approx(
        [0.21, 0.0, -0.07]
    )


def test_fixed_hard_mount_calibration_uses_configured_offset_and_payload() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()

    calibration = skill._build_fixed_tool_calibration(
        plan_id="plan-fixed",
        plan_revision=2,
    )

    assert calibration["source"] == "CONFIGURED_HARD_FIXED_BLADE_OFFSET"
    assert calibration["source_observations"] == 0
    assert calibration["controlled_frame_offset_xyz_m"] == pytest.approx(
        [0.18, 0.0, -0.02]
    )
    assert calibration["controlled_frame_offset_rpy_rad"] == pytest.approx(
        [0.0, 0.0, 0.0]
    )
    assert calibration["payload_mass_kg"] == pytest.approx(0.07)
    assert calibration["payload_com_tool_m"] == pytest.approx(
        [0.0, 0.0, 0.0]
    )


def test_tool_confirmation_uses_human_attachment_checkbox() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = FakeIntegrated()
    skill.integrated.value["gripper"] = {
        "latched_hold": False,
        "active_action": None,
    }
    skill.progress = FakeProgress(
        {
            "phase": Phase.WAIT_TOOL_LOAD,
            "operator_tool_loaded": False,
            "operator_tool_attachment_confirmed": False,
        }
    )

    with pytest.raises(ValueError, match="physically attached"):
        asyncio.run(
            skill.confirm_tool_loaded(
                operator_confirms_knife_attached=False,
            )
        )

    result = asyncio.run(
        skill.confirm_tool_loaded(
            operator_confirms_knife_attached=True,
        )
    )

    assert result["operator_tool_loaded"] is True
    assert result["operator_tool_attachment_confirmed"] is True
    assert result["phase"] == Phase.WAIT_WORKPIECE_LOAD


def test_fixed_mount_scene_validation_does_not_require_blade_landmarks() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    scene = {
        "board": {"visible": True, "confidence": 0.95},
        "vegetable": {"visible": True, "confidence": 0.95},
        "person_visible_in_workspace": False,
        "person_or_animal_visible_in_workspace": False,
    }

    skill._validate_scene(scene)


@pytest.mark.parametrize("recheck_confirms", [False, True])
def test_workspace_presence_alert_requires_focused_second_confirmation(
    recheck_confirms: bool,
) -> None:
    class FakeVision:
        async def recheck_workspace_presence(
            self,
            _: np.ndarray,
        ) -> dict[str, Any]:
            return {
                "person_or_animal_visible_in_workspace": recheck_confirms,
                "visible_subject_description": (
                    "human hand" if recheck_confirms else ""
                ),
                "confidence": 0.95,
                "notes": "focused synthetic recheck",
            }

    skill = object.__new__(VegetableCuttingSkill)
    skill.scene_vision = FakeVision()
    initial = {
        "person_or_animal_visible_in_workspace": True,
        "notes": "multi-purpose observation reported presence",
    }

    resolved, diagnostics = asyncio.run(
        skill._resolve_workspace_presence_alert(
            initial,
            np.zeros((32, 32, 3), dtype=np.uint8),
        )
    )

    assert diagnostics["required"] is True
    assert diagnostics["initially_reported"] is True
    assert diagnostics["confirmed"] is recheck_confirms
    assert (
        resolved["person_or_animal_visible_in_workspace"]
        is recheck_confirms
    )


def test_controlled_frame_offset_allows_mounting_direction_but_bounds_distance() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()

    assert skill._validate_controlled_frame_offset(
        [0.214, -0.08, -0.12]
    ) == pytest.approx(np.linalg.norm([0.214, -0.08, -0.12]))
    reverse_mount = [-0.6543105566, -0.0775236243, 0.0138963295]
    assert skill._validate_controlled_frame_offset(
        reverse_mount
    ) == pytest.approx(np.linalg.norm(reverse_mount))
    with pytest.raises(RuntimeError, match="physical limit"):
        skill._validate_controlled_frame_offset([-0.81, 0.0, 0.0])


def test_execution_preview_failure_preserves_tool_when_inactive() -> None:
    integrated = FakeIntegrated()
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.execution_cancelled = False
    skill.execution_events = [{"state": "COMPLETED_FLOAT"}]
    skill.progress = FakeProgress(
        {
            "state": SkillState.RUNNING,
            "phase": Phase.TRANSFER_TO_FIRST_CUT,
            "execution": {"state": "TRANSFER_TO_FIRST_CUT"},
            "motion_submitted": True,
            "error": None,
        }
    )

    async def fake_evidence(
        error: Exception,
        *,
        context: str,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"status": "CAPTURED", "error": str(error)}

    skill.capture_failure_evidence = fake_evidence

    result = asyncio.run(
        _fail_and_snapshot(
            skill,
            RuntimeError("preview position residual is 26.95 mm"),
        )
    )

    assert integrated.request_float_calls == 0
    assert result["execution"]["float_requested"] is False
    assert (
        result["execution"]["state"]
        == "FAILED_NO_ACTIVE_TRAJECTORY_TOOL_PRESERVED"
    )
    assert integrated.value["gripper"]["latched_hold"] is True


def test_execution_failure_requests_float_for_active_trajectory() -> None:
    integrated = FakeIntegrated()
    integrated.value["trajectory"]["active"] = True
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.execution_cancelled = False
    skill.execution_events = []
    skill.progress = FakeProgress(
        {
            "state": SkillState.RUNNING,
            "phase": Phase.CUTTING,
            "execution": {"state": "CUTTING"},
            "motion_submitted": True,
            "error": None,
        }
    )

    async def fake_evidence(
        error: Exception,
        *,
        context: str,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"status": "CAPTURED", "error": str(error)}

    skill.capture_failure_evidence = fake_evidence

    result = asyncio.run(
        _fail_and_snapshot(skill, RuntimeError("trajectory failure"))
    )

    assert integrated.request_float_calls == 1
    assert result["execution"]["float_requested"] is True
    assert (
        result["execution"]["state"]
        == "FAILED_ACTIVE_TRAJECTORY_FLOAT_REQUESTED"
    )


def test_failed_session_reset_is_software_only_and_preserves_camera_lock() -> None:
    integrated = FakeIntegrated()
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.lock = asyncio.Lock()
    skill.execution_task = None
    skill.stationary_camera_transform_lock = {
        "alignment_id": "alignment-one",
        "arm_from_camera": {"translation_m": [0.1, 0.2, 0.3]},
    }
    skill.local_vio_stop_result = {"status": "stopped"}
    skill.progress = FakeProgress(
        {
            "skill_id": "failed-skill",
            "plan_id": "failed-plan",
            "state": SkillState.FAILED,
            "phase": Phase.FAILED,
            "message": "failed",
            "started_at_us": 1,
            "operator_tool_loaded": True,
            "operator_tool_attachment_confirmed": True,
            "operator_workpiece_loaded": True,
            "operator_outside_workspace": True,
            "motion_submission_enabled": False,
            "motion_submitted": True,
            "provider_readiness": {},
            "alignment": {"alignment_id": "alignment-one"},
            "tracking": {},
            "execution": {"state": "FAILED_NO_ACTIVE_TRAJECTORY_TOOL_PRESERVED"},
            "result": {"plan_id": "failed-plan"},
            "error": "synthetic failure",
        }
    )

    result = asyncio.run(skill.reset_failed_session())

    assert result["state"] == SkillState.IDLE
    assert result["phase"] == Phase.IDLE
    assert result["error"] is None
    assert result["plan_id"] == ""
    assert result["operator_tool_attachment_confirmed"] is False
    assert (
        result["execution"]["fixed_camera_transform_lock_preserved"]
        is True
    )
    assert skill.stationary_camera_transform_lock["alignment_id"] == (
        "alignment-one"
    )
    assert skill.local_vio_stop_result == {"status": "stopped"}
    assert integrated.request_float_calls == 0


def test_failed_session_reset_rejects_active_integrated_trajectory() -> None:
    integrated = FakeIntegrated()
    integrated.value["trajectory"]["active"] = True
    skill = object.__new__(VegetableCuttingSkill)
    skill.integrated = integrated
    skill.execution_task = None
    skill.progress = FakeProgress(
        {
            "state": SkillState.FAILED,
            "phase": Phase.FAILED,
        }
    )

    with pytest.raises(RuntimeError, match="trajectory is active"):
        asyncio.run(skill.reset_failed_session())

    assert integrated.request_float_calls == 0


def test_new_session_reuses_existing_fixed_camera_transform_lock() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.lock = asyncio.Lock()
    skill.stationary_camera_transform_lock = {
        "alignment_id": "alignment-one"
    }
    skill.local_vio_stop_result = {"status": "stopped"}
    skill.progress = FakeProgress(
        {
            "state": SkillState.IDLE,
            "phase": Phase.IDLE,
        }
    )

    async def readiness_snapshot() -> dict[str, Any]:
        return {
            "integrated_idle": True,
            "integrated_idle_reasons": [],
        }

    async def alignment_snapshot() -> dict[str, Any]:
        return {
            "alignment_id": "alignment-one",
            "valid": True,
        }

    async def capture_skill_frame() -> object:
        return object()

    async def capture_frame_transforms(_: object) -> dict[str, Any]:
        assert skill.stationary_camera_transform_lock is not None
        return {
            "arm_from_camera": {
                "stationary_camera_pose_lock": {
                    "applied": True,
                    "local_vio_stop": {"status": "stopped"},
                }
            },
            "local_vio_stop_result": {"status": "stopped"},
        }

    skill.readiness_snapshot = readiness_snapshot
    skill._alignment_snapshot = alignment_snapshot
    skill._capture_skill_frame = capture_skill_frame
    skill._capture_frame_transforms = capture_frame_transforms

    result = asyncio.run(
        skill.start_session(
            RunParameters(
                slice_spacing_mm=15.0,
                blade_yaw_deg=0.0,
                maximum_cut_count=10,
            )
        )
    )

    assert result["phase"] == Phase.WAIT_TOOL_LOAD
    assert skill.stationary_camera_transform_lock == {
        "alignment_id": "alignment-one"
    }
    assert skill.local_vio_stop_result == {"status": "stopped"}


async def _fail_and_snapshot(
    skill: VegetableCuttingSkill,
    error: Exception,
) -> dict[str, Any]:
    await skill._execution_failure(error)
    return await skill.progress.snapshot()


def test_failure_evidence_persists_rgb_depth_overlay_and_metadata(
    tmp_path: Any,
) -> None:
    class FakeCamera:
        async def capture(self, *, require_vio: bool) -> RgbdFrame:
            assert require_vio is False
            return RgbdFrame(
                rgb=np.full((180, 320, 3), 96, dtype=np.uint8),
                depth_m=np.full((180, 320), 0.8, dtype=np.float32),
                intrinsics={
                    "fx": 200.0,
                    "fy": 200.0,
                    "cx": 160.0,
                    "cy": 90.0,
                },
                timestamp_us=123,
                frame_number=7,
                camera_frame="camera",
                session_epoch="",
                calibration_revision="revision",
                observations={},
            )

    skill = object.__new__(VegetableCuttingSkill)
    skill.camera = FakeCamera()
    skill.artifacts = MonitorArtifacts()
    skill.settings = SimpleNamespace(run_root=tmp_path)

    evidence = asyncio.run(
        skill.capture_failure_evidence(
            RuntimeError("preview position residual is 26.95 mm"),
            context="PHYSICAL_EXECUTION",
            progress={"plan_id": "plan-one", "phase": "CUTTING"},
        )
    )

    assert evidence["status"] == "CAPTURED"
    assert evidence["plan_id"] == "plan-one"
    for key in ("rgb_path", "depth_path", "overlay_path", "metadata_path"):
        assert tmp_path in Path(evidence[key]).parents
        assert Path(evidence[key]).is_file()
    assert asyncio.run(skill.artifacts.image("overlay")) is not None


def test_first_cut_rejection_allows_repeated_operator_requested_rounds() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    skill.execution_task = None
    skill.execution_events = []
    skill.first_cut_alignment_attempt_count = 0
    skill.progress = FakeProgress(
        {
            "state": SkillState.WAITING_FOR_OPERATOR,
            "phase": Phase.WAIT_FIRST_CUT_CONFIRMATION,
            "result": {"plan_id": "plan-one"},
            "execution": {"state": "WAIT_FIRST_CUT_CONFIRMATION"},
        }
    )
    reruns: list[dict[str, Any]] = []

    async def fake_align(plan: dict[str, Any]) -> None:
        reruns.append(plan)

    skill._align_first_cut = fake_align

    async def run() -> None:
        response = await skill.first_cut_decision("NO_READJUST")
        assert response["phase"] == Phase.TRANSFER_TO_FIRST_CUT
        await asyncio.sleep(0)
        assert reruns == [{"plan_id": "plan-one"}]
        skill.progress.value["phase"] = Phase.WAIT_FIRST_CUT_CONFIRMATION
        skill.execution_task = None
        second = await skill.first_cut_decision("NO_READJUST")
        assert second["phase"] == Phase.TRANSFER_TO_FIRST_CUT
        await asyncio.sleep(0)
        assert reruns == [
            {"plan_id": "plan-one"},
            {"plan_id": "plan-one"},
        ]

    asyncio.run(run())
