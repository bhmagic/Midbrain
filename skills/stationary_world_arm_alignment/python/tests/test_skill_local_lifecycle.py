from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cv2
import numpy as np

from stationary_world_arm_alignment.config import load_skill_config
from stationary_world_arm_alignment.foundation_engine import (
    LocalFoundationPoseEngine,
    SKILL_LOCAL_ROUTE,
)
from stationary_world_arm_alignment.skill import AlignmentSkill


class _Keeper:
    async def ensure_valid(self) -> None:
        return None

    def status(self) -> dict:
        return {"mode": "test"}


class _Progress:
    async def update(self, **_kwargs) -> None:
        return None


class _Engine:
    def __init__(self, *, close_error: Exception | None = None):
        self.backend = SimpleNamespace(name="finite-test-backend")
        self.close_error = close_error
        self.closed = False

    async def collect_samples(self, **_kwargs):
        sample = np.eye(4, dtype=np.float64)
        return (
            {"base": {"camera": [sample], "vio": [sample]}},
            {"base": "finite-local-session"},
        )

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _skill() -> AlignmentSkill:
    skill = object.__new__(AlignmentSkill)
    skill.config = load_skill_config()
    skill.base_pose_engine_route = SKILL_LOCAL_ROUTE
    skill.local_foundation_engine = None
    skill.last_base_pose_engine_lifecycle = {}
    skill.progress = _Progress()
    skill.fabric = object()
    skill.cancel_event = asyncio.Event()
    return skill


def _frame() -> SimpleNamespace:
    return SimpleNamespace(
        rgb=np.zeros((4, 4, 3), dtype=np.uint8),
        depth_m=np.ones((4, 4), dtype=np.float32),
        intrinsics={"fx": 4.0, "fy": 4.0, "cx": 1.5, "cy": 1.5},
        timestamp_us=100,
        frame_number=1,
        camera_frame="camera",
        world_frame="workcell",
        session_epoch="epoch",
    )


def test_skill_local_backend_is_closed_before_samples_return(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _Engine()
    monkeypatch.setattr(
        LocalFoundationPoseEngine,
        "from_config",
        classmethod(lambda cls, config, root: engine),
    )
    cv2.imwrite(
        str(tmp_path / "base_mask.png"),
        np.ones((4, 4), dtype=np.uint8),
    )
    skill = _skill()

    samples, sessions = asyncio.run(
        skill._collect_skill_local_foundation(
            skill_id="skill",
            run_dir=tmp_path,
            attempt=1,
            frame=_frame(),
            keeper=_Keeper(),
            include_gripper=False,
        )
    )

    assert samples["base"]["camera"]
    assert sessions == {"base": "finite-local-session"}
    assert engine.closed is True
    assert skill.local_foundation_engine is None
    assert skill.last_base_pose_engine_lifecycle == {
        "route": SKILL_LOCAL_ROUTE,
        "state": "CLOSED",
        "backend": "finite-test-backend",
        "owned_session_count_after": 0,
        "owned_sessions": ["finite-local-session"],
        "gpu_resources_released": True,
        "backend_closed": True,
        "cleanup_error": None,
    }


def test_skill_local_cleanup_failure_rejects_success(
    tmp_path,
    monkeypatch,
) -> None:
    engine = _Engine(close_error=RuntimeError("GPU close failed"))
    monkeypatch.setattr(
        LocalFoundationPoseEngine,
        "from_config",
        classmethod(lambda cls, config, root: engine),
    )
    cv2.imwrite(
        str(tmp_path / "base_mask.png"),
        np.ones((4, 4), dtype=np.uint8),
    )
    skill = _skill()

    try:
        asyncio.run(
            skill._collect_skill_local_foundation(
                skill_id="skill",
                run_dir=tmp_path,
                attempt=1,
                frame=_frame(),
                keeper=_Keeper(),
                include_gripper=False,
            )
        )
    except RuntimeError as error:
        assert "backend cleanup failed" in str(error)
    else:
        raise AssertionError("cleanup failure must reject the route result")

    assert skill.local_foundation_engine is None
    assert skill.last_base_pose_engine_lifecycle["state"] == "CLEANUP_FAILED"
    assert skill.last_base_pose_engine_lifecycle["gpu_resources_released"] is False
