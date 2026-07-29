from __future__ import annotations

import asyncio
import time
from typing import Any

from vegetable_cutting.skill import (
    VegetableCuttingSkill,
    evaluate_integrated_readiness,
)


def test_integrated_readiness_accepts_healthy_idle_state() -> None:
    ready, reasons = evaluate_integrated_readiness(
        {
            "ready": True,
            "health": "HEALTHY",
            "control_state": "TARGET_EDIT",
            "fault_reason": None,
            "engaged": False,
            "trajectory": {"active": False},
        }
    )
    assert ready is True
    assert reasons == []


def test_integrated_readiness_rejects_fault_float() -> None:
    ready, reasons = evaluate_integrated_readiness(
        {
            "ready": True,
            "health": "DEGRADED",
            "control_state": "FAULT_FLOAT",
            "fault_reason": "Basic lease lost",
            "engaged": False,
            "trajectory": {"active": False},
        }
    )
    assert ready is False
    assert "Integrated controller health is not HEALTHY" in reasons
    assert "Integrated controller is faulted" in reasons


def test_integrated_readiness_accepts_latched_tool_hold() -> None:
    ready, reasons = evaluate_integrated_readiness(
        {
            "ready": True,
            "health": "HEALTHY",
            "control_state": "GRIPPER_MIT_CLOSE_LATCHED",
            "fault_reason": None,
            "engaged": True,
            "trajectory": {"active": False},
            "gripper": {
                "latched_hold": True,
                "active_action": "CLOSE",
            },
        }
    )
    assert ready is True
    assert reasons == []


def test_integrated_readiness_accepts_confirmed_post_motion_gravity_float() -> None:
    ready, reasons = evaluate_integrated_readiness(
        {
            "ready": True,
            "health": "HEALTHY",
            "engaged": True,
            "control_state": "TARGET_EDIT",
            "basic_state": {
                "provider_state": "SAFE_HOLD_GRAVITY_FLOAT",
            },
            "safety": {
                "float_confirmed": True,
            },
            "trajectory": None,
        }
    )

    assert ready is True
    assert reasons == []


class AlignmentFabric:
    def __init__(self, observation: dict[str, Any] | None):
        self.observation = observation

    async def latest_optional(self, _: str) -> dict[str, Any] | None:
        return self.observation


def alignment_skill(
    observation: dict[str, Any] | None,
) -> VegetableCuttingSkill:
    skill = object.__new__(VegetableCuttingSkill)
    skill.fabric = AlignmentFabric(observation)
    skill.config = {
        "alignment": {
            "result_stream": "alignment",
            "require_valid": True,
            "require_reviewed_motion_usable": True,
            "maximum_age_s": 60.0,
            "require_same_camera_calibration_revision": True,
        }
    }
    return skill


def test_alignment_status_is_live_and_nonthrowing() -> None:
    missing = asyncio.run(alignment_skill(None).alignment_status())
    assert missing["valid"] is False
    assert "no stationary" in missing["error"]

    now_us = time.time_ns() // 1000
    valid = asyncio.run(
        alignment_skill(
            {
                "observed_at_us": now_us,
                "data": {
                    "alignment_id": "alignment-live",
                    "valid": True,
                    "created_at_us": now_us,
                    "expires_at_us": now_us + 60_000_000,
                    "review_state": "ACCEPTED",
                    "motion_usable": True,
                    "vio_session_epoch": "epoch",
                    "camera_calibration_revision": "camera-revision",
                    "mode": "auto",
                },
            }
        ).alignment_status()
    )
    assert valid["valid"] is True
    assert valid["alignment_id"] == "alignment-live"


def test_alignment_status_rejects_pending_or_expired_candidate() -> None:
    now_us = time.time_ns() // 1000
    pending = asyncio.run(
        alignment_skill(
            {
                "observed_at_us": now_us,
                "data": {
                    "alignment_id": "pending",
                    "valid": True,
                    "created_at_us": now_us,
                    "expires_at_us": now_us + 60_000_000,
                    "review_state": "CANDIDATE_REVIEW_REQUIRED",
                    "motion_usable": False,
                    "camera_calibration_revision": "camera-revision",
                },
            }
        ).alignment_status()
    )
    assert pending["valid"] is False
    assert "non-motion candidate" in pending["error"]

    expired = asyncio.run(
        alignment_skill(
            {
                "observed_at_us": now_us,
                "data": {
                    "alignment_id": "expired",
                    "valid": True,
                    "created_at_us": now_us,
                    "expires_at_us": now_us - 1,
                    "review_state": "ACCEPTED",
                    "motion_usable": True,
                    "camera_calibration_revision": "camera-revision",
                },
            }
        ).alignment_status()
    )
    assert expired["valid"] is False
    assert "expired" in expired["error"]


def test_provider_refresh_does_not_restart_vio_after_camera_pose_lock() -> None:
    class Manager:
        def __init__(self) -> None:
            self.hot: list[str] = []

        async def ensure_hot(
            self,
            provider_id: str,
            *,
            timeout_s: float,
        ) -> dict[str, Any]:
            assert timeout_s == 10.0
            self.hot.append(provider_id)
            return {"status": "hot"}

    class Progress:
        async def update(self, **changes: Any) -> dict[str, Any]:
            return changes

    skill = object.__new__(VegetableCuttingSkill)
    skill.config = {
        "providers": {
            "camera": "camera",
            "local_vio": "vio",
            "basic_arm": "basic",
            "integrated_arm": "integrated",
        },
        "provider_startup": {
            "start_camera": True,
            "start_local_vio": True,
            "start_basic_arm": True,
            "start_integrated_arm": True,
            "timeout_s": 10.0,
        },
    }
    skill.stationary_camera_transform_lock = {"alignment_id": "alignment"}
    skill.manager = Manager()
    skill.progress = Progress()

    async def readiness() -> dict[str, Any]:
        return {"integrated_idle": True}

    skill.readiness_snapshot = readiness
    result = asyncio.run(skill.bootstrap_providers())

    assert skill.manager.hot == ["camera", "basic", "integrated"]
    assert result["startup"]["local_vio"]["intentionally_stopped"] is True
