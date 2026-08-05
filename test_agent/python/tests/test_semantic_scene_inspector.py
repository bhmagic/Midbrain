from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from physical_agent_test.semantic_scene_inspector import SemanticSceneInspector


class FakeFabric:
    def __init__(self, observation):
        self.observation = observation

    async def latest_optional(self, _stream: str):
        return self.observation


class _VisualStore:
    def __init__(self) -> None:
        self.arguments = None

    async def register_channels(self, **kwargs):
        self.arguments = kwargs
        return {"schema": "midbrain.visual_evidence", "evidence_id": "map"}


class _ImageResponse:
    def __init__(self) -> None:
        self.content = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + b"\x00\x00\x00\x01\x00\x00\x00\x01"
        )

    def raise_for_status(self) -> None:
        return None


class _ImageClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url):
        return _ImageResponse()


class SemanticSceneInspectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_current_policy_does_not_reuse_previous_scene(self):
        class FailedMapFabric:
            async def latest_optional(self, stream: str):
                if stream == "robot_arm.scene.segmentation_policy":
                    return {
                        "schema": "physical_agent.arm_scene_segmentation_policy",
                        "data": {"revision": "policy-new"},
                    }
                if stream == "robot_arm.scene.tracked_semantic_assertions":
                    return {
                        "valid": False,
                        "data": {
                            "policy": {"revision": "policy-new"},
                            "mapping_failure": {
                                "status": (
                                    "VLM_MASK_QUALITY_REJECTED_AFTER_3_ATTEMPTS"
                                )
                            },
                        },
                    }
                return {"data": {"scene_revision": "old-scene"}}

        result = await SemanticSceneInspector(
            FailedMapFabric(),
            mapping_wait_s=0.0,
        ).run()

        self.assertEqual(result["status"], "SCENE_MAPPING_FAILED")
        self.assertIn("three", result["message"])

    async def test_ready_map_registers_switchable_visual_channels(self) -> None:
        store = _VisualStore()
        inspector = SemanticSceneInspector(
            FakeFabric(None),
            tracker_base_url="http://tracker",
            visual_evidence_store=store,
        )

        with patch(
            "physical_agent_test.semantic_scene_inspector.httpx.AsyncClient",
            return_value=_ImageClient(),
        ):
            evidence = await inspector._visual_evidence(
                scene_revision="scene-1",
                sphere_count=42,
            )

        self.assertEqual(evidence["evidence_id"], "map")
        self.assertEqual(store.arguments["default_channel"], "segmentation")
        self.assertEqual(
            [value["id"] for value in store.arguments["channels"]],
            ["rgb", "registered_depth", "segmentation"],
        )
        self.assertIn("42 collision spheres", store.arguments["title"])

    async def test_inspector_reports_missing_hot_provider(self) -> None:
        result = await SemanticSceneInspector(FakeFabric(None)).run()
        self.assertEqual(result["status"], "TRACKER_COVERAGE_REQUIRED")
        self.assertEqual(
            result["required_provider_id"],
            "perception.sam2_scene_tracker",
        )
        self.assertFalse(result["physical_motion_authorized"])

    async def test_ready_tracker_requests_compiler_when_scene_is_missing(
        self,
    ) -> None:
        class TrackerOnlyFabric:
            async def latest_optional(self, stream: str):
                if stream == "robot_arm.primary.integrated.scene":
                    return None
                return {
                    "valid": True,
                    "data": {"coverage": {"ready": True}},
                }

        result = await SemanticSceneInspector(TrackerOnlyFabric()).run()

        self.assertEqual(result["status"], "NO_SCENE")
        self.assertEqual(
            result["required_provider_id"],
            "world_model.arm_scene_compiler",
        )

    async def test_inspector_summarizes_fresh_scene_and_bounds_raw_spheres(
        self,
    ) -> None:
        now_us = time.time_ns() // 1000
        observation = {
            "schema": "physical_agent.arm_semantic_sphere_scene",
            "provider_id": "world_model.arm_scene_compiler",
            "provider_instance_id": "instance",
            "boot_id": "boot",
            "sequence": 3,
            "observed_at_us": now_us,
            "freshness_ms": 1000,
            "expires_at_us": now_us + 1_000_000,
            "valid": True,
            "data": {
                "contract_version": 2,
                "scene_revision": "scene-3",
                "frame_id": "rebot_arm_base",
                "roi_layers": [],
                "spheres": [
                    {"type": "KEEP_OUT", "roi_scope": "ARM_BASE_1P2M"},
                    {"type": "WORK_OBJECT", "roi_scope": "GRIPPER_0P5M"},
                ],
                "production": {"depth_mode": "POINT_CLOUD_AND_SEMANTICS"},
            },
        }
        result = await SemanticSceneInspector(FakeFabric(observation)).run(
            include_spheres=True,
            maximum_spheres=1,
        )
        self.assertEqual(result["status"], "SCENE_READY")
        self.assertEqual(
            result["sphere_type_counts"],
            {"KEEP_OUT": 1, "WORK_OBJECT": 1},
        )
        self.assertEqual(result["sphere_object_counts"], {"UNKNOWN": 2})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["spheres"]), 1)
