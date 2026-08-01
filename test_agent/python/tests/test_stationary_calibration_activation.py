from __future__ import annotations

import time
import unittest

from physical_agent_test.stationary_calibration_activation import (
    StationaryCalibrationActivationService,
)
from stationary_world_arm_alignment.candidate_review import canonical_sha256
from stationary_world_arm_alignment.persistence import CalibrationStore


SECRET = "review-test-secret-that-is-at-least-32-bytes"


def _candidate_result(
    *,
    semantic_status: str = "PASSED",
    base_x_relation_to_gripper: str = "AWAY_FROM_GRIPPER",
    selected_yaw_flip_deg: int = 180,
) -> dict:
    now_us = time.time_ns() // 1000
    orientation_axis = (
        "Z" if base_x_relation_to_gripper == "AWAY_FROM_GRIPPER" else "NONE"
    )
    orientation_count = 0 if orientation_axis == "NONE" else 1
    candidate = {
        "schema": (
            "midbrain.skill.stationary_world_arm_alignment."
            "calibration_candidate"
        ),
        "schema_version": 3,
        "candidate_id": "alignment-1",
        "workcell_calibration_revision": "alignment-1",
        "created_at_us": now_us,
        "expires_at_us": now_us + 60_000_000,
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "review_mode": "ENFORCED",
        "motion_usable": False,
        "frame_contract": {
            "world_frame": "world/stationary_camera/alignment-1",
            "vio_world_frame": "local_vio/epoch-1",
            "camera_frame": "femto_bolt_color_optical_frame",
            "arm_base_frame": "rebot_arm_base",
            "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "camera_optical_convention_id": (
                "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            ),
            "legacy_candidate_compatibility": "REJECT",
            "transform_semantics": "PARENT_FROM_CHILD",
        },
        "confidence": 0.0,
        "bounded_error_estimate": {
            "translation_m": 99.0,
            "rotation_rad": 99.0,
        },
        "quality_provenance": {
            "semantic_alignment": {
                "status": semantic_status,
                "source": "CURRENT_FOUNDATIONPOSE_VLM_BASE_X_REVIEW",
                "base_x_relation_to_gripper": (
                    base_x_relation_to_gripper
                ),
                "selected_base_yaw_flip_deg": selected_yaw_flip_deg,
                "fitted_base_yaw_deg": float(selected_yaw_flip_deg),
                "yaw_correction_translation_norm_m": 0.0,
                "world_up_available": True,
                "raw_base_z_dot_world_up": 1.0,
                "corrected_base_z_dot_world_up": 1.0,
                "upright_hemisphere_flip_required": False,
                "selected_orientation_correction_axis": orientation_axis,
                "selected_orientation_correction_deg": (
                    0 if orientation_count == 0 else 180
                ),
                "orientation_correction_count": orientation_count,
                "orientation_correction_translation_norm_m": 0.0,
                "orientation_application_origin": (
                    "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
                ),
                "orientation_application_order": (
                    "parent_from_mesh @ mesh_hypothesis_correction @ "
                    "mesh_from_semantic"
                ),
                "mesh_hypothesis_correction_translation_norm_m": 0.0,
                "mesh_center_translation_preserved": True,
                "semantic_root_translation_adjustment_norm_m": 0.0,
            }
        },
        "camera_provenance": {
            "provider_id": "camera.femto_bolt",
            "provider_instance_id": "camera-instance",
            "boot_id": "camera-boot",
            "calibration_revision": "camera-calibration",
        },
        "vio_provenance": {
            "provider_id": "localization.local_vio",
            "provider_instance_id": "vio-instance",
            "boot_id": "vio-boot",
            "world_frame": "local_vio/epoch-1",
            "session_epoch": "epoch-1",
            "reference_timestamp_us": now_us,
        },
        "transforms": {
            "world_from_camera": {
                "translation_m": [0.4, 0.5, 0.6],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "world_from_vio": {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
            "world_from_base": {
                "translation_m": [0.1, 0.2, 0.3],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
        },
    }
    return {
        "schema": "midbrain.skill.stationary_world_arm_alignment.result",
        "schema_version": 3,
        "alignment_id": "alignment-1",
        "valid": True,
        "review_state": "CANDIDATE_REVIEW_REQUIRED",
        "motion_usable": False,
        "candidate": candidate,
    }


class _Manager:
    def __init__(self) -> None:
        self.activations: list[dict] = []
        self.requests: list[dict] = []

    async def workcell_calibrations(self) -> dict:
        return {"activations": list(self.activations)}

    async def activate_workcell_calibration(self, request: dict) -> dict:
        self.requests.append(request)
        candidate = request["candidate"]
        activation = {
            "activation_id": "activation-1",
            "review_decision_id": request["review_decision"]["decision_id"],
            "candidate_sha256": canonical_sha256(candidate),
            "expires_at_us": candidate["expires_at_us"],
            "state": "ACTIVE",
            "motion_usable": True,
        }
        self.activations.append(activation)
        return activation


class StationaryCalibrationActivationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_reviews_and_activates_exact_persisted_candidate(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )
            digest = canonical_sha256(result["candidate"])
            continuation = service.latest_activation_continuation()

            activated = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )
            repeated = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )

        self.assertEqual(activated["status"], "ACTIVE")
        self.assertEqual(
            continuation,
            {
                "name": "review_and_activate_stationary_calibration",
                "arguments": {
                    "alignment_id": "alignment-1",
                    "candidate_sha256": digest,
                },
            },
        )
        self.assertTrue(activated["motion_usable"])
        self.assertFalse(activated["physical_motion_submitted"])
        self.assertTrue(activated["review_created"])
        self.assertNotIn("review_identity_assertion", activated)
        self.assertEqual(len(manager.requests), 1)
        self.assertTrue(repeated["already_active"])

    async def test_rejects_digest_mismatch_before_manager_activation(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "digest does not match",
            ):
                await service.review_and_activate(
                    alignment_id="alignment-1",
                    candidate_sha256="0" * 64,
                )

        self.assertEqual(manager.requests, [])

    async def test_expired_persisted_candidate_requires_fresh_calibration(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            result["candidate"]["expires_at_us"] = (
                time.time_ns() // 1000 - 1
            )
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )
            digest = canonical_sha256(result["candidate"])

            self.assertIsNone(service.latest_activation_continuation())
            response = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )

        self.assertEqual(response["status"], "FRESH_CALIBRATION_REQUIRED")
        self.assertEqual(response["reason_code"], "CANDIDATE_EXPIRED")
        self.assertFalse(response["motion_usable"])
        self.assertEqual(manager.requests, [])

    async def test_candidate_without_exact_vio_identity_is_not_replayed(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            result["candidate"]["vio_provenance"].pop("boot_id")
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )
            digest = canonical_sha256(result["candidate"])

            self.assertIsNone(service.latest_activation_continuation())
            response = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )

        self.assertEqual(response["status"], "FRESH_CALIBRATION_REQUIRED")
        self.assertEqual(
            response["reason_code"],
            "CANDIDATE_PROVENANCE_SUPERSEDED",
        )
        self.assertEqual(manager.requests, [])

    async def test_candidate_without_mesh_orientation_proof_requires_fresh_run(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            semantic = result["candidate"]["quality_provenance"][
                "semantic_alignment"
            ]
            semantic.pop("orientation_application_origin")
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )
            digest = canonical_sha256(result["candidate"])

            self.assertIsNone(service.latest_activation_continuation())
            response = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )

        self.assertEqual(response["status"], "FRESH_CALIBRATION_REQUIRED")
        self.assertEqual(
            response["reason_code"],
            "CANDIDATE_ORIENTATION_SUPERSEDED",
        )
        self.assertEqual(manager.requests, [])

    async def test_offers_and_activates_warning_candidate_without_retired_geometry(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result(
                semantic_status="PASSED_WITH_WARNINGS",
                base_x_relation_to_gripper="UNCLEAR",
                selected_yaw_flip_deg=0,
            )
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )

            continuation = service.latest_activation_continuation()
            activated = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=canonical_sha256(result["candidate"]),
            )

        self.assertIsNotNone(continuation)
        self.assertEqual(activated["status"], "ACTIVE")
        self.assertEqual(len(manager.requests), 1)

    async def test_does_not_offer_or_activate_continuous_yaw_candidate(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            semantic = result["candidate"]["quality_provenance"][
                "semantic_alignment"
            ]
            semantic["selected_base_yaw_flip_deg"] = 34
            semantic["fitted_base_yaw_deg"] = 34.0
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )

            self.assertIsNone(service.latest_activation_continuation())
            with self.assertRaisesRegex(
                RuntimeError,
                "exact reviewed base-orientation decision",
            ):
                await service.review_and_activate(
                    alignment_id="alignment-1",
                    candidate_sha256=canonical_sha256(result["candidate"]),
                )

        self.assertEqual(manager.requests, [])

    async def test_does_not_offer_direction_flip_mismatch(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result(
                base_x_relation_to_gripper="TOWARD_GRIPPER",
                selected_yaw_flip_deg=180,
            )
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )

            self.assertIsNone(service.latest_activation_continuation())
            with self.assertRaisesRegex(
                RuntimeError,
                "exact reviewed base-orientation decision",
            ):
                await service.review_and_activate(
                    alignment_id="alignment-1",
                    candidate_sha256=canonical_sha256(result["candidate"]),
                )

        self.assertEqual(manager.requests, [])

    async def test_documented_downward_base_z_requires_fresh_calibration(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _candidate_result()
            result["candidate"]["transforms"]["world_from_base"][
                "rotation_xyzw"
            ] = [1.0, 0.0, 0.0, 0.0]
            CalibrationStore(root / "calibrations").save(result)
            manager = _Manager()
            service = StationaryCalibrationActivationService(
                manager,
                review_auth_secret=SECRET,
                calibration_root=root / "calibrations",
                review_root=root / "reviews",
            )
            digest = canonical_sha256(result["candidate"])

            self.assertIsNone(service.latest_activation_continuation())
            response = await service.review_and_activate(
                alignment_id="alignment-1",
                candidate_sha256=digest,
            )

        self.assertEqual(response["status"], "FRESH_CALIBRATION_REQUIRED")
        self.assertEqual(
            response["reason_code"],
            "CANDIDATE_DOCUMENTED_TRANSFORM_INVALID",
        )
        self.assertEqual(manager.requests, [])
