from __future__ import annotations

import copy
import math
import time
import unittest
from types import SimpleNamespace

import numpy as np

from physical_agent_test.fabric_world_point import FabricWorldPointComposer
from physical_agent_test.spatial_frames import WORLD_CONVENTION_ID


class _SceneInspector:
    def __init__(self, result: dict):
        self.result = result
        self.calls: list[dict] = []

    async def run(self, **arguments):
        self.calls.append(copy.deepcopy(arguments))
        return copy.deepcopy(self.result)


class _Fabric:
    def __init__(self):
        self.transforms: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []

    async def transform(self, **arguments):
        self.calls.append(copy.deepcopy(arguments))
        key = (arguments["from_frame"], arguments["to_frame"])
        return copy.deepcopy(self.transforms[key])


class _SpatialResolver:
    arm_base_frame = "rebot_arm_base"
    maximum_transform_extrapolation_us = 500_000

    def __init__(self, fabric: _Fabric):
        self.fabric = fabric
        self.calls = 0
        self.provenance = {
            "world_frame": "world/alignment-1",
            "session_epoch": "epoch-1",
            "world_from_arm_translation_m": [1.0, 2.0, 3.0],
            "world_from_arm_rotation_xyzw": [
                0.0,
                0.0,
                math.sqrt(0.5),
                math.sqrt(0.5),
            ],
            "transform_path": [
                {
                    "authority": "manager.workcell_calibration",
                    "calibration_revision": "calibration-1",
                }
            ],
            "workcell_activation": {
                "activation_id": "activation-1",
                "calibration_revision": "calibration-1",
            },
        }

    async def resolve_world_point(self, **_arguments):
        self.calls += 1
        return SimpleNamespace(provenance=copy.deepcopy(self.provenance))


def _ready_scene() -> dict:
    now_us = time.time_ns() // 1000
    return {
        "status": "SCENE_READY",
        "stream": "robot_arm.primary.integrated.scene",
        "provider_id": "world_model.arm_scene_compiler",
        "provider_instance_id": "compiler-instance",
        "boot_id": "compiler-boot",
        "sequence": 7,
        "scene_revision": "scene-7",
        "frame_id": "rebot_arm_base",
        "visible_surface_aabbs": [
            {
                "extent_kind": "VISIBLE_SURFACE_AABB",
                "object_id": "workpiece",
                "type": "WORK_OBJECT",
                "frame_id": "rebot_arm_base",
                "convention_id": WORLD_CONVENTION_ID,
                "observed_at_us": now_us,
                "freshness_ms": 5000,
                "expires_at_us": now_us + 5_000_000,
                "corners_m": {
                    "right_forward_up": [0.5, -0.1, 0.2],
                },
            }
        ],
    }


class FabricWorldPointComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_frame_centimetres_become_typed_world_point(self):
        inspector = _SceneInspector(_ready_scene())
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, -2.0, 15.0],
            offset_unit="CENTIMETRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision="scene-7",
        )

        self.assertEqual(result["status"], "WORLD_POINT_READY")
        np.testing.assert_allclose(
            result["target_position_world_m"],
            [1.12, 2.5, 3.35],
            atol=1e-12,
        )
        self.assertEqual(
            result["target_world_frame_id"],
            "world/alignment-1",
        )
        self.assertEqual(result["target_session_epoch"], "epoch-1")
        self.assertEqual(
            result["derivation"]["requested_offset_unit"],
            "CENTIMETRES",
        )
        self.assertEqual(
            result["scene_revision_disposition"],
            "MATCHED_SELECTED_SNAPSHOT",
        )
        self.assertEqual(
            result["temporal_decision"],
            "ACCEPTED_FRESH_SNAPSHOT",
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertFalse(result["physical_motion_submitted"])
        self.assertEqual(len(inspector.calls), 1)
        self.assertTrue(
            all(
                call
                == {
                    "include_spheres": False,
                    "maximum_spheres": 1,
                    "include_visual_evidence": False,
                }
                for call in inspector.calls
            )
        )
        self.assertEqual(fabric.calls, [])

    async def test_vio_world_queries_arm_transform_at_aabb_timestamp(self):
        scene = _ready_scene()
        observed_at_us = scene["visible_surface_aabbs"][0][
            "observed_at_us"
        ]
        inspector = _SceneInspector(scene)
        fabric = _Fabric()
        fabric.transforms[("rebot_arm_base", "world/alignment-1")] = {
            "from_frame": "rebot_arm_base",
            "to_frame": "world/alignment-1",
            "at_us": observed_at_us,
            "translation_m": [0.1, 0.2, 0.3],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "path": [{"authority": "fabric-transform-graph"}],
        }
        resolver = _SpatialResolver(fabric)
        resolver.provenance["workcell_activation"] = None
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="METRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision=None,
        )

        np.testing.assert_allclose(
            result["target_position_world_m"],
            [0.6, 0.1, 0.5],
            atol=1e-12,
        )
        self.assertEqual(len(fabric.calls), 1)
        self.assertEqual(fabric.calls[0]["at_us"], observed_at_us)
        self.assertEqual(fabric.calls[0]["session_epoch"], "epoch-1")

    async def test_controlled_effector_offset_uses_latest_fabric_rotation(self):
        inspector = _SceneInspector(_ready_scene())
        fabric = _Fabric()
        half_sqrt = math.sqrt(0.5)
        fabric.transforms[("rebot_arm_tool", "rebot_arm_base")] = {
            "from_frame": "rebot_arm_tool",
            "to_frame": "rebot_arm_base",
            "at_us": 2_000_000,
            "translation_m": [8.0, 9.0, 10.0],
            "rotation_xyzw": [0.0, 0.0, half_sqrt, half_sqrt],
            "path": [{"authority": "robot_arm.rebot_dm"}],
        }
        resolver = _SpatialResolver(fabric)
        resolver.provenance.update(
            {
                "world_from_arm_translation_m": [0.0, 0.0, 0.0],
                "world_from_arm_rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        )
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[1.0, 0.0, 0.0],
            offset_unit="CENTIMETRES",
            offset_reference="CONTROLLED_EFFECTOR_FRAME",
            expected_scene_revision=None,
        )

        np.testing.assert_allclose(
            result["target_position_world_m"],
            [0.5, -0.09, 0.2],
            atol=1e-12,
        )
        self.assertEqual(len(fabric.calls), 1)
        self.assertIsNone(fabric.calls[0]["at_us"])
        self.assertEqual(
            result["derivation"]["offset_transform"]["source_frame"],
            "rebot_arm_tool",
        )

    async def test_advanced_scene_revision_uses_selected_fresh_snapshot(self):
        inspector = _SceneInspector(_ready_scene())
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="METRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision="scene-old",
        )

        self.assertEqual(result["status"], "WORLD_POINT_READY")
        self.assertEqual(
            result["inspected_scene_revision"],
            "scene-old",
        )
        self.assertEqual(
            result["scene_revision_disposition"],
            "SUPERSEDED_BY_SELECTED_FRESH_SNAPSHOT",
        )
        self.assertEqual(result["source"]["scene_revision"], "scene-7")
        self.assertEqual(resolver.calls, 2)
        self.assertEqual(fabric.calls, [])

    async def test_scene_snapshot_is_not_repolled_during_derivation(self):
        initial = _ready_scene()

        class _SingleReadInspector(_SceneInspector):
            async def run(self, **arguments):
                if self.calls:
                    raise AssertionError("scene snapshot was read more than once")
                return await super().run(**arguments)

        inspector = _SingleReadInspector(initial)
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="METRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision="scene-7",
        )

        self.assertEqual(result["status"], "WORLD_POINT_READY")
        self.assertEqual(result["source"]["scene_revision"], "scene-7")
        self.assertEqual(len(inspector.calls), 1)
        self.assertEqual(resolver.calls, 2)

    async def test_world_authority_change_during_derivation_fails_closed(self):
        inspector = _SceneInspector(_ready_scene())
        fabric = _Fabric()

        class _ChangingResolver(_SpatialResolver):
            async def resolve_world_point(self, **arguments):
                result = await super().resolve_world_point(**arguments)
                if self.calls == 2:
                    result.provenance["workcell_activation"] = {
                        "activation_id": "activation-2",
                        "calibration_revision": "calibration-2",
                    }
                return result

        resolver = _ChangingResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="METRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision=None,
        )

        self.assertEqual(
            result["status"],
            "WORLD_POINT_FRAME_AUTHORITY_CHANGED",
        )
        self.assertNotIn("target_position_world_m", result)

    async def test_expired_aabb_cannot_produce_world_point(self):
        scene = _ready_scene()
        source = scene["visible_surface_aabbs"][0]
        source["observed_at_us"] = 1
        source["expires_at_us"] = 2
        inspector = _SceneInspector(scene)
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="METRES",
            offset_reference="SOURCE_FRAME",
            expected_scene_revision=None,
        )

        self.assertEqual(result["status"], "WORLD_POINT_SOURCE_STALE")
        self.assertEqual(resolver.calls, 0)

    async def test_unready_scene_cannot_produce_motion_input(self):
        inspector = _SceneInspector(
            {
                "status": "SCENE_STALE",
                "workflow_complete": False,
                "physical_motion_authorized": False,
            }
        )
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        composer = FabricWorldPointComposer(
            inspector,
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await composer.run(
            object_id="workpiece",
            corner_name="right_forward_up",
            offset_vector=[0.0, 0.0, 0.0],
            offset_unit="MILLIMETRES",
            offset_reference="ACTIVE_WORLD",
            expected_scene_revision=None,
        )

        self.assertEqual(result["status"], "WORLD_POINT_SOURCE_UNAVAILABLE")
        self.assertNotIn("target_position_world_m", result)
        self.assertEqual(resolver.calls, 0)
