from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from vegetable_cutting.config import load_skill_config
from vegetable_cutting.math3d import (
    matrix_quaternion_xyzw,
    quaternion_matrix,
    transform_points,
)
from vegetable_cutting.skill import VegetableCuttingSkill


class FakeFabric:
    def __init__(
        self,
        *,
        fail_camera: bool = False,
        fail_tool: bool = False,
        reverse_alignment_parenting: bool = False,
    ):
        self.fail_camera = fail_camera
        self.fail_tool = fail_tool
        self.reverse_alignment_parenting = reverse_alignment_parenting
        self.calls: list[dict[str, Any]] = []

    async def transform(self, **query: Any) -> dict[str, Any]:
        self.calls.append(query)
        if query["from_frame"] == "camera" and self.fail_camera:
            raise RuntimeError("camera path missing")
        if query["from_frame"] == "tool" and self.fail_tool:
            raise RuntimeError("tool path missing")
        if (
            query["from_frame"] == "camera"
            and query["to_frame"] == "vio-epoch"
        ):
            path = [
                {
                    "from_frame": "camera",
                    "to_frame": "vio-epoch",
                    "parent_frame": "vio-epoch",
                    "child_frame": "camera",
                    "direction": "child_to_parent",
                    "authority": "localization.local_vio",
                    "provider_instance_id": "vio",
                }
            ]
            translation_m = [0.4, 0.1, 0.2]
        elif query["from_frame"] == "camera":
            world = "world/stationary_camera/alignment"
            vio = "local_vio/vio-epoch"
            alignment_direction = (
                "parent_to_child"
                if self.reverse_alignment_parenting
                else "child_to_parent"
            )
            path = [
                {
                    "from_frame": "camera",
                    "to_frame": vio,
                    "parent_frame": vio,
                    "child_frame": "camera",
                    "direction": "child_to_parent",
                    "authority": "localization.local_vio",
                    "provider_instance_id": "vio",
                },
                {
                    "from_frame": vio,
                    "to_frame": world,
                    "parent_frame": world,
                    "child_frame": vio,
                    "direction": alignment_direction,
                    "authority": "skill.stationary_world_arm_alignment",
                    "provider_instance_id": "alignment",
                },
                {
                    "from_frame": world,
                    "to_frame": "base",
                    "parent_frame": world,
                    "child_frame": "base",
                    "direction": "parent_to_child",
                    "authority": "skill.stationary_world_arm_alignment",
                    "provider_instance_id": "alignment",
                },
            ]
            translation_m = [0.3, 0.2, 0.1]
        else:
            path = [
                {
                    "from_frame": "tool",
                    "to_frame": "base",
                    "parent_frame": "base",
                    "child_frame": "tool",
                    "direction": "child_to_parent",
                    "authority": "robot_arm.rebot_dm",
                    "provider_instance_id": "arm",
                }
            ]
            translation_m = [0.0, 0.0, 0.0]
        return {
            "from_frame": query["from_frame"],
            "to_frame": query["to_frame"],
            "translation_m": translation_m,
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": path,
        }


class FakeManager:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop_provider(self, provider_id: str) -> dict[str, Any]:
        self.stopped.append(provider_id)
        return {"provider_id": provider_id, "status": "stopped"}


def make_skill(fabric: FakeFabric) -> VegetableCuttingSkill:
    skill = object.__new__(VegetableCuttingSkill)
    skill.fabric = fabric
    skill.manager = FakeManager()
    skill.config = {
        "alignment": {
            "transform_max_extrapolation_us": 500000,
            "stop_local_vio_after_transform_lock": True,
            "require_same_vio_epoch": True,
        },
        "frames": {"arm_base": "base", "arm_tool": "tool"},
        "providers": {"local_vio": "localization.local_vio"},
    }
    skill.stationary_camera_transform_lock = None
    skill.local_vio_stop_result = None

    async def fake_alignment_snapshot() -> dict[str, Any]:
        return {
            "alignment_id": "alignment",
            "valid": True,
            "vio_world_frame": "vio-epoch",
            "vio_session_epoch": "vio-epoch",
            "camera_frame": "camera",
            "camera_reference_timestamp_us": 100000,
            "world_from_vio": {
                "translation_m": [-0.01, 0.02, -0.03],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "world_from_base": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "vio_from_camera_reference": {
                "translation_m": [0.4, 0.1, 0.2],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
        }

    skill._alignment_snapshot = fake_alignment_snapshot
    return skill


def frame(
    *,
    timestamp_us: int = 123456,
    session_epoch: str = "vio-epoch",
) -> SimpleNamespace:
    return SimpleNamespace(
        camera_frame="camera",
        timestamp_us=timestamp_us,
        session_epoch=session_epoch,
    )


def test_frame_transforms_share_timestamp_but_only_camera_uses_vio_epoch() -> None:
    fabric = FakeFabric()
    captured = asyncio.run(make_skill(fabric)._capture_frame_transforms(frame()))

    assert captured["captured_before_vlm"] is True
    assert captured["arm_from_tool"] is not None
    assert captured["camera_arm_parent_chain"]["valid"] is True
    assert captured["camera_vio_parent_chain"]["valid"] is True
    assert captured["tool_arm_parent_chain"]["valid"] is True
    assert [call["from_frame"] for call in fabric.calls] == [
        "camera",
        "camera",
        "tool",
    ]
    assert all(call["at_us"] == 123456 for call in fabric.calls)
    assert fabric.calls[0]["session_epoch"] == "vio-epoch"
    assert fabric.calls[1]["session_epoch"] == "vio-epoch"
    assert fabric.calls[2]["session_epoch"] is None
    assert (
        captured["arm_from_camera"][
            "stationary_camera_translation_stabilization"
        ]["applied"]
        is True
    )
    assert (
        captured["arm_from_camera"]["stationary_camera_pose_lock"][
            "applied"
        ]
        is True
    )
    assert captured["local_vio_stop_result"]["status"] == "stopped"


def test_fixed_camera_pose_lock_stops_vio_once_and_skips_later_vio_queries() -> None:
    fabric = FakeFabric()
    skill = make_skill(fabric)

    first = asyncio.run(
        skill._capture_frame_transforms(frame(timestamp_us=123456))
    )
    second = asyncio.run(
        skill._capture_frame_transforms(
            frame(timestamp_us=223456, session_epoch="")
        )
    )

    assert skill.manager.stopped == ["localization.local_vio"]
    assert [call["from_frame"] for call in fabric.calls] == [
        "camera",
        "camera",
        "tool",
        "tool",
    ]
    np.testing.assert_allclose(
        first["arm_from_camera"]["translation_m"],
        second["arm_from_camera"]["translation_m"],
        atol=1e-12,
    )
    np.testing.assert_allclose(
        first["arm_from_camera"]["rotation_xyzw"],
        second["arm_from_camera"]["rotation_xyzw"],
        atol=1e-12,
    )
    assert second["fixed_camera_transform_lock_reused"] is True
    assert second["live_arm_from_camera"] is None
    assert second["live_vio_from_camera"] is None
    assert (
        second["arm_from_camera"]["stationary_camera_pose_lock"][
            "live_vio_queried"
        ]
        is False
    )


def test_post_lock_planning_binds_empty_capture_epoch_to_alignment() -> None:
    skill = make_skill(FakeFabric())
    skill.stationary_camera_transform_lock = {
        "alignment_id": "alignment",
        "vio_session_epoch": "vio-epoch",
    }
    binding = skill._resolve_camera_epoch_binding(
        alignment={
            "alignment_id": "alignment",
            "vio_session_epoch": "vio-epoch",
        },
        frame=frame(session_epoch=""),
    )

    assert binding["policy"] == "FIXED_CAMERA_TRANSFORM_LOCK"
    assert binding["compatible"] is True
    assert binding["effective_vio_session_epoch"] == "vio-epoch"
    assert binding["raw_capture_vio_session_epoch"] is None
    assert binding["post_lock_capture_requires_vio_epoch"] is False


def test_live_capture_still_rejects_different_vio_epoch_without_lock() -> None:
    skill = make_skill(FakeFabric())

    with pytest.raises(RuntimeError, match="different VIO epochs"):
        skill._resolve_camera_epoch_binding(
            alignment={
                "alignment_id": "alignment",
                "vio_session_epoch": "vio-epoch",
            },
            frame=frame(session_epoch="different-epoch"),
        )


def test_camera_lock_rejects_different_alignment_identity() -> None:
    skill = make_skill(FakeFabric())
    skill.stationary_camera_transform_lock = {
        "alignment_id": "other-alignment",
        "vio_session_epoch": "vio-epoch",
    }

    with pytest.raises(RuntimeError, match="different stationary alignment"):
        skill._resolve_camera_epoch_binding(
            alignment={
                "alignment_id": "alignment",
                "vio_session_epoch": "vio-epoch",
            },
            frame=frame(session_epoch=""),
        )


def test_missing_tool_transform_does_not_discard_cut_plan_geometry() -> None:
    captured = asyncio.run(
        make_skill(FakeFabric(fail_tool=True))._capture_frame_transforms(frame())
    )

    assert captured["arm_from_camera"] is not None
    assert captured["arm_from_tool"] is None
    assert captured["arm_from_tool_error"] == "tool path missing"


def test_missing_camera_transform_fails_before_vlm() -> None:
    with pytest.raises(RuntimeError, match="camera path missing"):
        asyncio.run(
            make_skill(FakeFabric(fail_camera=True))._capture_frame_transforms(frame())
        )


def test_reversed_stationary_alignment_parenting_fails_before_vlm() -> None:
    with pytest.raises(RuntimeError, match="reversed parent semantics"):
        asyncio.run(
            make_skill(
                FakeFabric(reverse_alignment_parenting=True)
            )._capture_frame_transforms(frame())
        )


def test_stationary_camera_transform_rejects_legacy_alignment_without_reference_pose() -> None:
    alignment = {
        "alignment_id": "alignment",
        "camera_frame": "camera",
        "camera_reference_timestamp_us": 100000,
        "world_from_vio": {
            "translation_m": [-0.006, 0.044, -0.03],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "world_from_base": {
            "translation_m": [-0.38, -0.74, 0.73],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
    }
    live_arm = {
        "from_frame": "camera",
        "to_frame": "base",
        "translation_m": [9.0, 9.0, 9.0],
        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "path": [{"authority": "skill.stationary_world_arm_alignment"}],
    }
    with pytest.raises(
        RuntimeError,
        match="lacks vio_from_camera_reference",
    ):
        VegetableCuttingSkill._freeze_stationary_camera_transform(
            live_arm_from_camera=live_arm,
            live_vio_from_camera={
                "translation_m": [0.01, -0.04, 0.03],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            alignment=alignment,
            arm_base_frame="base",
        )


def test_stationary_camera_transform_uses_saved_alignment_reference_rotation() -> None:
    alignment = {
        "alignment_id": "alignment",
        "camera_frame": "camera",
        "camera_reference_timestamp_us": 100000,
        "world_from_vio": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "world_from_base": {
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        "vio_from_camera_reference": {
            "translation_m": [0.1, 0.2, 0.3],
            "rotation_xyzw": [
                0.0,
                0.0,
                float(np.sqrt(0.5)),
                float(np.sqrt(0.5)),
            ],
        },
    }
    live_arm = {
        "from_frame": "camera",
        "to_frame": "base",
        "translation_m": [9.0, 9.0, 9.0],
        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        "path": [{"authority": "skill.stationary_world_arm_alignment"}],
    }
    first = VegetableCuttingSkill._freeze_stationary_camera_transform(
        live_arm_from_camera=live_arm,
        live_vio_from_camera={
            "translation_m": [4.0, 5.0, 6.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
        alignment=alignment,
        arm_base_frame="base",
    )
    second = VegetableCuttingSkill._freeze_stationary_camera_transform(
        live_arm_from_camera=live_arm,
        live_vio_from_camera={
            "translation_m": [-4.0, -5.0, -6.0],
            "rotation_xyzw": [1.0, 0.0, 0.0, 0.0],
        },
        alignment=alignment,
        arm_base_frame="base",
    )

    np.testing.assert_allclose(
        first["translation_m"],
        [0.1, 0.2, 0.3],
        atol=1e-12,
    )
    np.testing.assert_allclose(
        first["rotation_xyzw"],
        second["rotation_xyzw"],
        atol=1e-12,
    )
    stabilization = first[
        "stationary_camera_translation_stabilization"
    ]
    assert stabilization["alignment_reference_full_pose_available"] is True
    assert (
        stabilization["orientation_source"]
        == "ALIGNMENT_REFERENCE_FULL_POSE"
    )


def test_blade_mounting_direction_cannot_flip_published_arm_root_axis() -> None:
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    camera_points = {
        "tip": np.asarray([-0.25, 0.0, 0.30]),
        "heel": np.asarray([-0.05, 0.0, 0.30]),
        "spine": np.asarray([-0.15, 0.04, 0.30]),
    }

    def fake_observation(
        _: Any,
        __: dict[str, Any],
        arm_from_camera: np.ndarray,
    ) -> dict[str, Any]:
        return {
            "arm_base_points_m": {
                name: transform_points(arm_from_camera, point).tolist()
                for name, point in camera_points.items()
            },
            "camera_points_m": {
                name: point.tolist() for name, point in camera_points.items()
            },
            "depth_diagnostics": {
                name: {"p10_m": 0.29, "p90_m": 0.31}
                for name in camera_points
            },
        }

    skill._blade_observation = fake_observation
    selected, _, candidate, diagnostics = skill._select_blade_axis_hypothesis(
        frame=SimpleNamespace(),
        blade_scene={
            "tip_yx_1000": [500, 200],
            "heel_yx_1000": [500, 800],
            "spine_yx_1000": [400, 500],
        },
        published_arm_from_camera=np.eye(4),
        arm_from_tool=np.eye(4),
    )

    assert diagnostics["status"] == "PUBLISHED_AXIS_ACCEPTED"
    assert diagnostics["selected_flip_deg"] == 0
    assert diagnostics["forward_axis_gate_enabled"] is False
    assert candidate["quality_metrics"]["tool_forward_axis_cosine"] < 0.0
    np.testing.assert_allclose(
        selected[:3, :3],
        np.eye(3),
    )


def test_rotation_matrix_quaternion_round_trip_for_axis_flip() -> None:
    rotation = np.diag([-1.0, -1.0, 1.0])
    quaternion = matrix_quaternion_xyzw(rotation)
    np.testing.assert_allclose(
        quaternion_matrix(quaternion),
        rotation,
        atol=1e-12,
    )
