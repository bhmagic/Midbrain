from __future__ import annotations

import argparse
import importlib.util
import sys
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np


PROVIDER_PATH = Path(__file__).resolve().parents[2] / "provider.py"
SPEC = importlib.util.spec_from_file_location("foundation_pose_provider_entry", PROVIDER_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
FoundationPoseProvider = MODULE.FoundationPoseProvider


class ProviderSessionTests(unittest.TestCase):
    def test_create_estimate_session_with_mock_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "revision": "test",
                        "models": [
                            {
                                "model_id": "arm_root",
                                "mesh_path": "not-required.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:1",
                control_port=7103,
                backend="mock",
                foundationpose_root=None,
                model_registry=str(registry),
                poll_interval=0.01,
                default_update_hz=3.0,
                default_track_duration_s=30.0,
                pose_freshness_ms=750,
                minimum_mask_pixels=4,
                max_consecutive_failures=10,
                estimate_iterations=5,
                track_iterations=2,
                debug_level=0,
                debug_dir=str(root / "debug"),
            )
            provider = FoundationPoseProvider(args)
            provider.residency = "HOT"
            provider.ready = True
            provider._best_effort_status = lambda *_args, **_kwargs: None
            session = provider.handle_request(
                {
                    "action": "estimate",
                    "model_id": "arm_root",
                    "target_id": "base",
                    "mask_path": str(root / "mask.png"),
                }
            )
            self.assertEqual(session["operation"], "ESTIMATE")
            self.assertEqual(session["state"], "WAITING_FOR_INPUTS")
            self.assertTrue(session["child_frame"].startswith("observed_object/base/"))
            with self.assertRaises(RuntimeError):
                provider.handle_request({"action": "release_resources"})
            provider.handle_request(
                {
                    "action": "stop",
                    "session_id": session["session_id"],
                }
            )
            release = provider.handle_request(
                {"action": "release_resources"}
            )
            self.assertTrue(release["resources_released"])
            self.assertEqual(release["status"], "resources_released")
            provider.http.close()

    def test_create_estimate_session_from_manager_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "revision": "test",
                        "models": [
                            {
                                "model_id": "arm_root",
                                "mesh_path": "not-required.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:1",
                control_port=7103,
                backend="mock",
                foundationpose_root=None,
                model_registry=str(registry),
                poll_interval=0.01,
                default_update_hz=3.0,
                default_track_duration_s=30.0,
                pose_freshness_ms=750,
                minimum_mask_pixels=4,
                max_consecutive_failures=10,
                estimate_iterations=5,
                track_iterations=2,
                debug_level=0,
                debug_dir=str(root / "debug"),
            )
            provider = FoundationPoseProvider(args)
            provider.residency = "HOT"
            provider.ready = True
            provider._best_effort_status = lambda *_args, **_kwargs: None
            session = provider.handle_request(
                {
                    "action": "estimate",
                    "payload": {
                        "model_id": "arm_root",
                        "target_id": "base",
                        "bounding_box": {
                            "box_2d": [250, 250, 750, 750],
                            "coordinate_space": "normalized_0_1000",
                        },
                    },
                    "request_id": "manager-request-test",
                    "related_skill_id": "skill-test",
                }
            )
            self.assertEqual(session["operation"], "ESTIMATE")
            self.assertEqual(session["state"], "WAITING_FOR_INPUTS")
            self.assertTrue(session["child_frame"].startswith("observed_object/base/"))
            self.assertEqual(
                session["bounding_box"]["box_2d"],
                [250.0, 250.0, 750.0, 750.0],
            )
            internal_session = provider.sessions[session["session_id"]]
            mask = provider._load_initial_mask(internal_session, (100, 200))
            self.assertIsNotNone(mask)
            self.assertEqual(int(mask.sum()), 50 * 100)
            provider.http.close()

    def test_process_session_publishes_semantic_pose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transform = np.eye(4)
            transform[0, 3] = 0.1
            registry = root / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "revision": "test",
                        "models": [
                            {
                                "model_id": "arm_root",
                                "mesh_path": "not-required.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": transform.reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mask_path = root / "mask.png"
            import cv2

            cv2.imwrite(str(mask_path), np.ones((8, 8), dtype=np.uint8) * 255)
            args = argparse.Namespace(
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:1",
                control_port=7103,
                backend="mock",
                foundationpose_root=None,
                model_registry=str(registry),
                poll_interval=0.01,
                default_update_hz=3.0,
                default_track_duration_s=30.0,
                pose_freshness_ms=750,
                minimum_mask_pixels=4,
                max_consecutive_failures=10,
                estimate_iterations=5,
                track_iterations=2,
                debug_level=0,
                debug_dir=str(root / "debug"),
            )
            provider = FoundationPoseProvider(args)
            provider.residency = "HOT"
            provider.ready = True
            provider.camera_matrix = np.array([[500.0, 0.0, 4.0], [0.0, 500.0, 4.0], [0.0, 0.0, 1.0]])
            provider._best_effort_status = lambda *_args, **_kwargs: None
            captured = {}

            def capture(**kwargs):
                captured.update(kwargs)

            provider._publish_pose_result = capture
            response = provider.handle_request(
                {
                    "action": "estimate",
                    "model_id": "arm_root",
                    "target_id": "base",
                    "mask_path": str(mask_path),
                }
            )
            session = provider.sessions[response["session_id"]]
            rgb = np.zeros((8, 8, 3), dtype=np.uint8)
            depth = np.ones((8, 8), dtype=np.float32) * 0.75
            provider._process_session(
                session,
                rgb=rgb,
                depth_m=depth,
                frame_number=12,
                observed_at_us=123456,
            )
            self.assertEqual(session.state, "COMPLETED")
            self.assertEqual(session.result_count, 1)
            np.testing.assert_allclose(captured["camera_from_semantic"][:3, 3], [0.1, 0.0, 0.75])
            self.assertEqual(captured["observed_at_us"], 123456)
            provider.http.close()


    def test_model_default_child_frame_is_used_for_stable_reporter_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "revision": "test",
                        "models": [
                            {
                                "model_id": "robot_gripper",
                                "role": "robot_gripper",
                                "default_child_frame": "observed_object/test_robot/gripper",
                                "mesh_path": "not-required.obj",
                                "semantic_frame": "robot/gripper_reference",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:1",
                control_port=7103,
                backend="mock",
                foundationpose_root=None,
                model_registry=str(registry),
                poll_interval=0.01,
                default_update_hz=3.0,
                default_track_duration_s=30.0,
                pose_freshness_ms=750,
                minimum_mask_pixels=4,
                max_consecutive_failures=10,
                estimate_iterations=5,
                track_iterations=2,
                debug_level=0,
                debug_dir=str(root / "debug"),
            )

            provider = FoundationPoseProvider(args)
            provider.residency = "HOT"
            provider.ready = True
            provider._best_effort_status = lambda *_args, **_kwargs: None

            session = provider.handle_request(
                {
                    "action": "estimate",
                    "payload": {
                        "model_id": "robot_gripper",
                    },
                }
            )

            self.assertEqual(
                session["child_frame"],
                "observed_object/test_robot/gripper",
            )
            provider.http.close()

    def test_published_pose_metadata_includes_model_role_and_semantic_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "models.json"
            registry.write_text(
                json.dumps(
                    {
                        "revision": "test",
                        "models": [
                            {
                                "model_id": "robot_base",
                                "role": "robot_base",
                                "mesh_path": "not-required.obj",
                                "semantic_frame": "robot/arm_root",
                                "mesh_from_semantic": np.eye(4).reshape(-1).tolist(),
                                "scale_to_m": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                manager_url="http://127.0.0.1:1",
                fabric_url="http://127.0.0.1:1",
                control_port=7103,
                backend="mock",
                foundationpose_root=None,
                model_registry=str(registry),
                poll_interval=0.01,
                default_update_hz=3.0,
                default_track_duration_s=30.0,
                pose_freshness_ms=750,
                minimum_mask_pixels=4,
                max_consecutive_failures=10,
                estimate_iterations=5,
                track_iterations=2,
                debug_level=0,
                debug_dir=str(root / "debug"),
            )

            provider = FoundationPoseProvider(args)
            provider.residency = "HOT"
            provider.ready = True
            provider.camera_calibration_revision = "cal-test"

            class Response:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

            captured: dict[str, object] = {}

            def fake_post(url: str, json: object):
                captured["url"] = url
                captured["json"] = json
                return Response()

            provider.http.post = fake_post  # type: ignore[method-assign]

            model = provider.registry.get(
                "robot_base",
                require_mesh=False,
            )
            session = MODULE.PoseSession(
                session_id="session-1",
                operation="TRACK",
                model_id="robot_base",
                target_id="base",
                child_frame="observed_object/test/base",
                parent_frame="femto_bolt_color_optical_frame",
                mask_stream="perception.object.mask",
                mask_path=None,
                bounding_box=None,
                related_skill_id=None,
                max_duration_s=30.0,
                max_update_hz=3.0,
            )

            result = MODULE.BackendResult(
                camera_from_mesh=np.eye(4),
                score=None,
                latency_ms=5.0,
                backend_details={"mock": True},
            )

            provider._publish_pose_result(
                session=session,
                model=model,
                result=result,
                camera_from_semantic=np.eye(4),
                frame_number=7,
                observed_at_us=123456,
                mode="TRACKED",
                quality={"valid_depth_ratio": 1.0},
            )

            batch = captured["json"]
            assert isinstance(batch, dict)
            observations = batch["observations"]
            pose_data = observations[0]["data"]
            transform_data = observations[1]["data"]

            self.assertEqual(pose_data["object_role"], "robot_base")
            self.assertEqual(
                pose_data["semantic_frame"],
                "robot/arm_root",
            )
            self.assertEqual(
                transform_data["source"]["object_role"],
                "robot_base",
            )
            self.assertEqual(
                transform_data["source"]["semantic_frame"],
                "robot/arm_root",
            )
            observations = batch["observations"]
            self.assertEqual(observations[0]["observed_at_us"], 123456)
            self.assertEqual(observations[1]["observed_at_us"], 123456)
            self.assertEqual(
                transform_data["session_epoch"],
                "session-1",
            )
            self.assertEqual(
                transform_data["child_frame"],
                "observed_object/test/base",
            )
            provider.http.close()


if __name__ == "__main__":
    unittest.main()
