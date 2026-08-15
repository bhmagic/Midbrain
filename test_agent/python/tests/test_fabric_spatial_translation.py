from __future__ import annotations

import copy
import math
import unittest
from types import SimpleNamespace

import numpy as np

from physical_agent_test.fabric_spatial_translation import (
    FabricSpatialTranslator,
)
from physical_agent_test.spatial_frames import WORLD_CONVENTION_ID


class _Fabric:
    def __init__(self) -> None:
        self.transforms: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []

    async def transform(self, **arguments):
        self.calls.append(copy.deepcopy(arguments))
        key = (arguments["from_frame"], arguments["to_frame"])
        return copy.deepcopy(self.transforms[key])


class _SpatialResolver:
    arm_base_frame = "rebot_arm_base"
    maximum_transform_extrapolation_us = 500_000

    def __init__(self, fabric: _Fabric) -> None:
        self.fabric = fabric
        self.calls: list[dict] = []
        self.provenance = {
            "world_frame": "world/alignment-1",
            "session_epoch": "epoch-1",
            "resolved_at_us": 5_000_000,
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
                    "activation_id": "activation-1",
                    "calibration_revision": "calibration-1",
                }
            ],
            "workcell_activation": {
                "activation_id": "activation-1",
                "calibration_revision": "calibration-1",
            },
        }

    async def resolve_world_point(self, **arguments):
        self.calls.append(copy.deepcopy(arguments))
        return SimpleNamespace(provenance=copy.deepcopy(self.provenance))


class FabricSpatialTranslatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_arm_base_direction_rotates_without_translation(self):
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_direction(
            direction=[2.0, 0.0, 0.0],
            source_reference="ARM_BASE",
            source_frame_id="rebot_arm_base",
            source_observed_at_us=None,
            source_session_epoch=None,
        )

        self.assertEqual(result["status"], "WORLD_DIRECTION_READY")
        np.testing.assert_allclose(
            result["direction_world"],
            [0.0, 1.0, 0.0],
            atol=1e-12,
        )
        self.assertEqual(
            result["target_world_frame_id"],
            "world/alignment-1",
        )
        self.assertEqual(result["target_session_epoch"], "epoch-1")
        self.assertEqual(result["calibration_revision"], "calibration-1")
        self.assertEqual(
            result["framed_direction_world"]["units"],
            "UNITLESS_UNIT_VECTOR",
        )
        self.assertEqual(
            result["framed_direction_world"]["convention_id"],
            WORLD_CONVENTION_ID,
        )
        self.assertFalse(result["physical_motion_authorized"])
        self.assertFalse(result["physical_motion_submitted"])
        self.assertEqual(fabric.calls, [])
        self.assertEqual(len(resolver.calls), 2)

    async def test_arm_base_pose_uses_full_rigid_transform(self):
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_pose(
            position_m=[1.0, 0.0, 0.0],
            orientation_xyzw=[0.0, 0.0, 0.0, 2.0],
            source_reference="ARM_BASE",
            source_frame_id=None,
            source_observed_at_us=None,
            source_session_epoch=None,
        )

        self.assertEqual(result["status"], "WORLD_POSE_READY")
        np.testing.assert_allclose(
            result["target_position_world_m"],
            [1.0, 3.0, 3.0],
            atol=1e-12,
        )
        np.testing.assert_allclose(
            result["target_orientation_world_xyzw"],
            [0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)],
            atol=1e-12,
        )
        self.assertEqual(
            result["framed_pose_world"]["frame_id"],
            "world/alignment-1",
        )
        self.assertFalse(result["physical_motion_authorized"])

    async def test_controlled_direction_composes_both_fabric_rotations(self):
        fabric = _Fabric()
        half_sqrt = math.sqrt(0.5)
        fabric.transforms[("rebot_arm_tool", "rebot_arm_base")] = {
            "from_frame": "rebot_arm_tool",
            "to_frame": "rebot_arm_base",
            "at_us": 4_900_000,
            "translation_m": [0.4, -0.2, 0.1],
            "rotation_xyzw": [0.0, 0.0, half_sqrt, half_sqrt],
            "path": [{"authority": "robot_arm.rebot_dm"}],
        }
        resolver = _SpatialResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_direction(
            direction=[1.0, 0.0, 0.0],
            source_reference="CONTROLLED_EFFECTOR_FRAME",
            source_frame_id="rebot_arm_tool",
            source_observed_at_us=4_900_000,
            source_session_epoch="epoch-1",
        )

        np.testing.assert_allclose(
            result["direction_world"],
            [-1.0, 0.0, 0.0],
            atol=1e-12,
        )
        self.assertEqual(len(fabric.calls), 1)
        self.assertEqual(fabric.calls[0]["at_us"], 4_900_000)
        self.assertEqual(fabric.calls[0]["session_epoch"], "epoch-1")
        self.assertEqual(
            [item["authority"] for item in result["derivation"]["transform_path"]],
            ["robot_arm.rebot_dm", "manager.workcell_calibration"],
        )

    async def test_active_world_direction_is_validated_and_preserved(self):
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_direction(
            direction=[0.0, 0.0, -4.0],
            source_reference="ACTIVE_WORLD",
            source_frame_id="world/alignment-1",
            source_observed_at_us=4_800_000,
            source_session_epoch="epoch-1",
        )

        self.assertEqual(result["direction_world"], [0.0, 0.0, -1.0])
        self.assertEqual(result["derivation"]["transform_path"], [])
        self.assertEqual(
            resolver.calls[0]["expected_world_frame"],
            "world/alignment-1",
        )

    async def test_source_frame_mismatch_fails_without_output(self):
        fabric = _Fabric()
        resolver = _SpatialResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_pose(
            position_m=[0.0, 0.0, 0.0],
            orientation_xyzw=[0.0, 0.0, 0.0, 1.0],
            source_reference="ARM_BASE",
            source_frame_id="some_other_arm",
            source_observed_at_us=None,
            source_session_epoch=None,
        )

        self.assertEqual(
            result["status"],
            "WORLD_POSE_SOURCE_FRAME_MISMATCH",
        )
        self.assertNotIn("target_position_world_m", result)
        self.assertFalse(result["physical_motion_authorized"])

    async def test_world_authority_change_during_translation_fails_closed(self):
        fabric = _Fabric()

        class _ChangingResolver(_SpatialResolver):
            async def resolve_world_point(self, **arguments):
                result = await super().resolve_world_point(**arguments)
                if len(self.calls) == 2:
                    result.provenance["workcell_activation"] = {
                        "activation_id": "activation-2",
                        "calibration_revision": "calibration-2",
                    }
                return result

        resolver = _ChangingResolver(fabric)
        translator = FabricSpatialTranslator(
            resolver,
            controlled_effector_frame="rebot_arm_tool",
        )

        result = await translator.translate_direction(
            direction=[-1.0, 0.0, 0.0],
            source_reference="ARM_BASE",
            source_frame_id=None,
            source_observed_at_us=None,
            source_session_epoch=None,
        )

        self.assertEqual(
            result["status"],
            "WORLD_DIRECTION_FRAME_AUTHORITY_CHANGED",
        )
        self.assertNotIn("direction_world", result)

    async def test_zero_direction_is_rejected(self):
        translator = FabricSpatialTranslator(
            _SpatialResolver(_Fabric()),
            controlled_effector_frame="rebot_arm_tool",
        )

        with self.assertRaisesRegex(ValueError, "direction must be non-zero"):
            await translator.translate_direction(
                direction=[0.0, 0.0, 0.0],
                source_reference="ARM_BASE",
                source_frame_id=None,
                source_observed_at_us=None,
                source_session_epoch=None,
            )
