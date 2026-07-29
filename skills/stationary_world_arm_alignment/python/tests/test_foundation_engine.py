from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

from stationary_world_arm_alignment.foundation_engine import (
    LocalFoundationPoseEngine,
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
    normalize_base_pose_engine_route,
)
from stationary_world_arm_alignment.math3d import transform_payload


@dataclass
class _Result:
    camera_from_mesh: np.ndarray
    latency_ms: float = 1.0


class _Backend:
    name = "mock-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.reset_sessions: list[str] = []
        self.closed = False

    def initialize(self, session_id, model, rgb, depth, camera_matrix, mask):
        del rgb, depth, camera_matrix, mask
        self.calls.append(("initialize", session_id))
        return _Result(_translated(model.offset_m, 0.0, 1.0))

    def track(self, session_id, rgb, depth, camera_matrix):
        del rgb, depth, camera_matrix
        self.calls.append(("track", session_id))
        offset = 0.2 if "gripper" in session_id else 0.0
        return _Result(_translated(offset, 0.0, 1.0))

    def reset(self, session_id):
        self.reset_sessions.append(session_id)

    def close(self):
        self.closed = True


class _Registry:
    def get(self, model_id, require_mesh=True):
        del require_mesh
        return SimpleNamespace(
            model_id=model_id,
            offset_m=0.2 if model_id == "gripper-model" else 0.0,
            mesh_from_semantic=np.eye(4),
        )


class _Capture:
    def __init__(self, frames):
        self.frames = list(frames)

    async def capture(self, attempts=3):
        del attempts
        return self.frames.pop(0)


class _Fabric:
    async def transform(self, **kwargs):
        del kwargs
        return transform_payload(np.eye(4))


def _translated(x, y, z):
    value = np.eye(4)
    value[:3, 3] = [x, y, z]
    return value


def _frame(frame_number):
    return SimpleNamespace(
        rgb=np.zeros((4, 4, 3), dtype=np.uint8),
        depth_m=np.ones((4, 4), dtype=np.float32),
        intrinsics={"fx": 4.0, "fy": 4.0, "cx": 1.5, "cy": 1.5},
        timestamp_us=100 + frame_number,
        frame_number=frame_number,
        camera_frame="camera",
        world_frame="workcell",
        session_epoch="epoch",
    )


def test_route_default_is_provider_compatibility() -> None:
    assert normalize_base_pose_engine_route({}) == PROVIDER_COMPATIBILITY_ROUTE
    assert (
        normalize_base_pose_engine_route(
            {"base_pose_engine": {"active_route": "skill_local"}}
        )
        == SKILL_LOCAL_ROUTE
    )


def test_local_engine_produces_provider_compatible_sample_shape() -> None:
    backend = _Backend()
    engine = LocalFoundationPoseEngine(backend, _Registry())
    guard_count = 0

    async def guard():
        nonlocal guard_count
        guard_count += 1

    samples, sessions = asyncio.run(
        engine.collect_samples(
            skill_id="skill",
            attempt=1,
            initial_frame=_frame(1),
            capture=_Capture([_frame(2), _frame(3)]),
            fabric=_Fabric(),
            masks={
                "base": np.ones((4, 4), dtype=np.uint8),
                "gripper": np.ones((4, 4), dtype=np.uint8),
            },
            model_ids={
                "base": "base-model",
                "gripper": "gripper-model",
            },
            required_counts={"base": 3, "gripper": 2},
            hard_timeout_s=2.0,
            minimum_sample_interval_s=0.0,
            guard=guard,
        )
    )

    assert len(samples["base"]["camera"]) == 3
    assert len(samples["base"]["vio"]) == 3
    assert len(samples["gripper"]["camera"]) == 2
    assert samples["gripper"]["camera"][0][0, 3] == 0.2
    assert sessions["base"].startswith("skill-local-base")
    assert guard_count == 3
    assert set(backend.reset_sessions) == set(sessions.values())
    assert engine.last_diagnostics["engine_route"] == SKILL_LOCAL_ROUTE


def test_local_engine_rejects_world_epoch_change() -> None:
    backend = _Backend()
    engine = LocalFoundationPoseEngine(backend, _Registry())
    changed = _frame(2)
    changed.session_epoch = "new-epoch"

    async def guard():
        return None

    try:
        asyncio.run(
            engine.collect_samples(
                skill_id="skill",
                attempt=1,
                initial_frame=_frame(1),
                capture=_Capture([changed]),
                fabric=_Fabric(),
                masks={"base": np.ones((4, 4), dtype=np.uint8)},
                model_ids={"base": "base-model"},
                required_counts={"base": 2},
                hard_timeout_s=2.0,
                minimum_sample_interval_s=0.0,
                guard=guard,
            )
        )
    except RuntimeError as error:
        assert "epoch changed" in str(error)
    else:
        raise AssertionError("epoch change should reject the local estimate")
