from __future__ import annotations

import json
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pytest

from locate_arm_base.candidate_selection import VisualCandidateSelection
from locate_arm_base.orientation import (
    EffectorPointObservation,
    SegmentationPrompt,
)
from locate_arm_base.profile import canonical_sha256
from locate_arm_base.skill import (
    EffectorOrientationHintRequired,
    LocateArmBaseSkill,
    _bounded_selection_decision,
    _vlm_backend_for_model,
)


ROOT = Path(__file__).resolve().parents[4]


class FakeClients:
    def __init__(self) -> None:
        self.published = None
        self.readiness_requests = []
        self.pose_requests = []
        self.segmentation_requests = []
        self.foundation_pose_hot_requests = 0
        self.sam2_hot_requests = 0

    def ensure_foundation_pose_hot(self):
        self.foundation_pose_hot_requests += 1
        return {"status": "hot"}

    def ensure_sam2_hot(self):
        self.sam2_hot_requests += 1
        return {"status": "hot"}

    def estimate_pose(self, payload):
        self.pose_requests.append(payload)
        pose = np.eye(4)
        pose[2, 3] = 1.0
        return {
            "measurement_id": "measurement-1",
            "camera_from_centered_mesh": pose.tolist(),
            "quality": {"score": 0.88, "hypothesis_count": 252},
            "timing": {"native_elapsed_ms": 18.0},
            "provenance": {"backend": "TEST"},
        }

    def active_arm_profile_state(self, stream="robot_arm.assembly_state"):
        from locate_arm_base.profile import file_sha256

        model_path = (
            ROOT
            / "providers/rebot_arm_dm/config/arm_profiles/rebot_arm_b601_dm.v1.json"
        )
        model = json.loads(model_path.read_text(encoding="utf-8"))
        effector_path = (
            ROOT
            / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper.v2.json"
        )
        mounted_effector = json.loads(effector_path.read_text(encoding="utf-8"))
        return {
            "schema": "midbrain.robot_assembly_state",
            "assembly_id": "test-assembly",
            "assembly_revision": "test-assembly-v1",
            "assembly_fingerprint": "test-fingerprint",
            "arm_provider_id": "robot_arm.rebot_dm",
            "arm_model_identity": {
                "model_id": model["model_id"],
                "model_revision": model["model_revision"],
            },
            "profile_file_sha256": {"arm_model": file_sha256(model_path)},
            "arm_model_appendix": model["appendix"],
            "mounted_effector": mounted_effector,
        }

    def ensure_active_arm_profile_state(
        self,
        provider_id,
        stream="robot_arm.assembly_state",
        *,
        timeout_s=15.0,
        poll_interval_s=0.1,
    ):
        self.readiness_requests.append(
            {
                "provider_id": provider_id,
                "stream": stream,
                "timeout_s": timeout_s,
                "poll_interval_s": poll_interval_s,
            }
        )
        return self.active_arm_profile_state(stream)

    def publish_candidate(self, candidate):
        self.published = candidate

    def transform(
        self,
        from_frame,
        to_frame,
        at_us,
        max_extrapolation_us,
        session_epoch=None,
    ):
        if from_frame == "rebot_arm_tool" and to_frame == "rebot_arm_base":
            base_from_tool = np.eye(4)
            base_from_tool[:3, 3] = [0.30, 0.0, 0.20]
            return base_from_tool, {
                "path": [from_frame, to_frame],
                "at_us": at_us,
            }
        raise AssertionError(f"unexpected transform request {from_frame} -> {to_frame}")

    def segment_mask(self, payload):
        self.segmentation_requests.append(payload)
        rgb_path = Path(payload["rgb_path"])
        image = Image.open(rgb_path)
        mask_path = rgb_path.parent / f"fake_sam2_mask_{len(self.segmentation_requests)}.png"
        mask = np.zeros((image.height, image.width), dtype=np.uint8)
        mask[50:200, 80:250] = 255
        Image.fromarray(mask).save(mask_path)
        from locate_arm_base.profile import file_sha256

        return {
            "status": "SEGMENTED",
            "mask_artifact": {
                "path": str(mask_path),
                "sha256": file_sha256(mask_path),
            },
            "quality": {"sam2_score": 0.89},
            "provenance": {"provider_id": "test.sam2"},
        }

    def close(self):
        pass


class FakeEffectorLocator:
    def __init__(self, *, identified: bool = True) -> None:
        self.identified = identified
        self.calls = 0

    def locate(self, mounted_effector, scene_path):
        self.calls += 1
        return EffectorPointObservation(
            self.identified,
            (("rail_lateral_left", 500, 650),) if self.identified else (),
            0.35,
            "A coarse rail endpoint is visible." if self.identified else "No effector visible.",
            "test-vlm",
            "response-effector-1",
        )


class FakePromptLocator:
    def locate(self, reference_paths, scene_path, **kwargs):
        from locate_arm_base.orientation import SegmentationPrompt

        return SegmentationPrompt(
            (250, 280, 820, 760),
            ((500, 500),),
            0.94,
            "The profiled base is visible.",
            "test-vlm",
            "response-mask-1",
        )


class FakeFitSelector:
    def select(self, reference_paths, contact_sheet_path, candidate_ids):
        return VisualCandidateSelection(
            candidate_ids[0],
            0.92,
            "The projected CAD aligns with the observed base.",
            "test-vlm",
            "response-fit-selection-1",
        )


def test_skill_composes_profiled_orientation_and_emits_review_only_candidate(tmp_path: Path) -> None:
    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    rgb[60:190, 90:240] = [120, 150, 180]
    mask = np.zeros((240, 320), dtype=np.uint8)
    mask[60:190, 90:240] = 255
    rgb_path, depth_path, mask_path = tmp_path / "rgb.png", tmp_path / "depth.npy", tmp_path / "mask.png"
    Image.fromarray(rgb).save(rgb_path)
    Image.fromarray(mask).save(mask_path)
    np.save(depth_path, np.ones((240, 320), dtype=np.float32), allow_pickle=False)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(encoding="utf-8")
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    clients = FakeClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=FakeEffectorLocator(),
        fit_selector=FakeFitSelector(),
    )
    candidate = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {"fx": 300.0, "fy": 300.0, "cx": 160.0, "cy": 120.0},
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "world_from_camera": np.eye(4).tolist(),
        }
    )
    assert candidate["motion_usable"] is False
    assert candidate["review_state"] == "PENDING_REVIEW"
    orientation = candidate["quality_provenance"]["orientation_resolution"]
    assert orientation["selected_candidate_id"] == "z0"
    assert orientation["method"] == "SINGLE_VLM_EFFECTOR_POINT_WITH_TIMESTAMPED_FK"
    assert orientation["vlm"]["invocation_count"] == 1
    assert orientation["vlm"]["quality_gate_applied"] is False
    assert orientation["acceptance_policy"] == (
        "ONE_RECOGNIZED_EFFECTOR_POINT_IS_SUFFICIENT_NO_POINT_QUALITY_GATE"
    )


def test_skill_binds_candidate_to_current_epoch_scoped_world_frame(
    tmp_path: Path,
) -> None:
    class EpochWorldClients(FakeClients):
        def __init__(self) -> None:
            super().__init__()
            self.transform_requests = []

        def current_world_axis(self, stream, *, required_convention):
            assert stream == "localization.vio.status"
            assert required_convention == "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
            return {
                "world_frame": "local_vio/epoch-7",
                "session_epoch": "epoch-7",
                "tracking_state": "TRACKING",
                "convention_id": required_convention,
                "status_observed_at_us": 123,
                "provider_id": "localization.local_vio",
                "provider_instance_id": "vio-instance",
                "boot_id": "vio-boot",
            }

        def transform(
            self,
            from_frame,
            to_frame,
            at_us,
            max_extrapolation_us,
            session_epoch=None,
        ):
            request = {
                "from_frame": from_frame,
                "to_frame": to_frame,
                "at_us": at_us,
                "max_extrapolation_us": max_extrapolation_us,
                "session_epoch": session_epoch,
            }
            self.transform_requests.append(request)
            if from_frame == "test_camera" and to_frame == "local_vio/epoch-7":
                return np.eye(4), {"path": [from_frame, to_frame]}
            return super().transform(
                from_frame,
                to_frame,
                at_us,
                max_extrapolation_us,
                session_epoch=session_epoch,
            )

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(np.zeros((120, 160, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    Image.fromarray(mask).save(mask_path)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    clients = EpochWorldClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=FakeEffectorLocator(),
        fit_selector=FakeFitSelector(),
    )
    candidate = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {
                "fx": 150.0,
                "fy": 150.0,
                "cx": 80.0,
                "cy": 60.0,
            },
            "camera_frame": "test_camera",
            "observed_at_us": 1_234_567,
            "world_frame": "local_vio/epoch-7",
            "session_epoch": "epoch-7",
        }
    )
    assert candidate["parent_frame"] == "local_vio/epoch-7"
    assert candidate["frame_contract"]["world_frame"] == "local_vio/epoch-7"
    assert candidate["quality_provenance"]["world_axis"]["session_epoch"] == "epoch-7"
    assert clients.transform_requests[0] == {
        "from_frame": "test_camera",
        "to_frame": "local_vio/epoch-7",
        "at_us": 1_234_567,
        "max_extrapolation_us": 250000,
        "session_epoch": "epoch-7",
    }
    assert clients.transform_requests[1]["from_frame"] == "rebot_arm_tool"
    assert clients.transform_requests[1]["to_frame"] == "rebot_arm_base"
    assert abs(candidate["world_from_arm_base"]["translation_m"][2] - 0.9553750055) < 1e-9
    immutable = dict(candidate)
    immutable.pop("candidate_path")
    expected_hash = immutable.pop("candidate_sha256")
    assert canonical_sha256(immutable) == expected_hash
    assert clients.published is candidate
    inspection = skill.inspection_snapshot()
    assert len(clients.pose_requests) == 2
    assert clients.foundation_pose_hot_requests == 1
    assert clients.sam2_hot_requests == 0
    selected_mask_path = str(
        Path(inspection["mask_candidates"]["vote"]["final_mask_path"])
    )
    assert [
        request["evidence"]["mask"]["path"] for request in clients.pose_requests
    ] == [selected_mask_path] * 2
    assert clients.readiness_requests == [
        {
            "provider_id": "robot_arm.rebot_dm",
            "stream": "robot_arm.assembly_state",
            "timeout_s": 15.0,
            "poll_interval_s": 0.1,
        }
    ]
    assert inspection["status"] == "COMPLETED"
    assert inspection["vlm"] == {
        "backend": "google.gemini",
        "model": "gemini-robotics-er-2-preview",
        "selection_source": "SKILL_DEFAULT",
    }
    assert inspection["foundation_pose"]["cad_filename"] == "Base_clean_centered.obj"
    assert inspection["arm_provider_readiness"]["status"] == "READY"
    consumers = {
        item["image_id"]: item["consumers"] for item in inspection["images"]
    }
    assert "VLM_SEED_LOCALIZATION" in consumers["current_rgb"]
    assert len(
        [image_id for image_id in consumers if image_id.startswith("mask_candidate_")]
    ) == 1
    assert "mask_vote" in consumers
    assert "mask_final_dilated" in consumers
    assert len(
        [image_id for image_id in consumers if image_id.startswith("fit_candidate_")]
    ) == 2
    assert inspection["foundation_pose"]["selected_candidate_id"] == "fit_1"
    assert (
        inspection["foundation_pose"]["selected_mask_candidate_id"]
        == "voted_mask_dilated_r4"
    )
    assert inspection["foundation_pose"]["all_fits_use_selected_mask"] is True
    assert inspection["foundation_pose"]["fit_policy"] == (
        "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK"
    )
    assert {
        fit["source_mask_candidate_id"]
        for fit in inspection["foundation_pose"]["fits"]
    } == {"voted_mask_dilated_r4"}
    assert "FK_ORIENTATION_RESOLUTION" in consumers["effector_fk_orientation"]
    assert "FINAL_ORIENTATION_INSPECTION" in consumers["resolved_pose"]
    assert Path(inspection["resolved_pose_path"]).is_file()
    assert candidate["quality_provenance"]["foundation_pose"]["score_semantics"] == (
        "AUDIT_ONLY_NOT_SELECTION_INPUT"
    )


def test_vlm_model_override_derives_provider_backend() -> None:
    assert _vlm_backend_for_model("gemini-robotics-er-2-preview") == "google.gemini"
    assert _vlm_backend_for_model("gpt-5.6-luna") == "openai.responses"


def test_live_camera_provenance_uses_device_information_identity() -> None:
    provenance = LocateArmBaseSkill._camera_provenance(
        {
            "source_observations": {
                "bundle": {
                    "provider_id": "camera.femto_bolt",
                    "provider_instance_id": "camera-instance",
                    "boot_id": "camera-boot",
                    "calibration_revision": "calibration-7",
                    "data": {},
                },
                "calibration": {"data": {"revision": "calibration-7"}},
                "device_info": {
                    "data": {
                        "canonical_device_id": "orbbec:femto-bolt:CL8326300SJ"
                    }
                },
            }
        }
    )
    assert provenance == {
        "provider_id": "camera.femto_bolt",
        "provider_instance_id": "camera-instance",
        "boot_id": "camera-boot",
        "canonical_device_id": "orbbec:femto-bolt:CL8326300SJ",
        "calibration_revision": "calibration-7",
    }


def test_live_camera_provenance_rejects_missing_device_identity() -> None:
    with pytest.raises(RuntimeError, match="canonical device identity"):
        LocateArmBaseSkill._camera_provenance(
            {
                "source_observations": {
                    "bundle": {
                        "provider_id": "camera.femto_bolt",
                        "provider_instance_id": "camera-instance",
                        "boot_id": "camera-boot",
                        "calibration_revision": "calibration-7",
                        "data": {},
                    },
                    "calibration": {"data": {"revision": "calibration-7"}},
                }
            }
        )
def test_unidentified_effector_requests_one_agent_supplied_world_x_retry(
    tmp_path: Path,
) -> None:
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(rgb).save(rgb_path)
    Image.fromarray(mask).save(mask_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(encoding="utf-8")
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    effector_locator = FakeEffectorLocator(identified=False)
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=FakeClients(),
        effector_locator=effector_locator,
        prompt_locator=FakePromptLocator(),
        fit_selector=FakeFitSelector(),
    )
    with pytest.raises(
        EffectorOrientationHintRequired,
        match="rough_arm_base_positive_x_world",
    ):
        skill.run(
            {
                "rgb_path": str(rgb_path),
                "depth_npy_path": str(depth_path),
                "mask_path": str(mask_path),
                "camera_intrinsics": {"fx": 150.0, "fy": 150.0, "cx": 80.0, "cy": 60.0},
                "camera_frame": "test_camera",
                "observed_at_us": time.time_ns() // 1000,
                "world_from_camera": np.eye(4).tolist(),
            }
        )
    assert effector_locator.calls == 1
    inspection = skill.inspection_snapshot()
    assert inspection["orientation_selection"]["vlm_invocation_count"] == 1
    assert inspection["orientation_selection"]["accepted"] is False
    assert inspection["failed_stage"] == "EFFECTOR_ORIENTATION_POINT_NOT_IDENTIFIED"
    assert inspection["timing"]["skill_elapsed_ms"] > 0.0


def test_agent_world_x_retry_skips_effector_vlm_and_publishes_candidate(
    tmp_path: Path,
) -> None:
    rgb = np.zeros((120, 160, 3), dtype=np.uint8)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(rgb).save(rgb_path)
    Image.fromarray(mask).save(mask_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(encoding="utf-8")
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    effector_locator = FakeEffectorLocator(identified=False)
    clients = FakeClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=effector_locator,
        prompt_locator=FakePromptLocator(),
        fit_selector=FakeFitSelector(),
    )
    candidate = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {"fx": 150.0, "fy": 150.0, "cx": 80.0, "cy": 60.0},
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "world_from_camera": np.eye(4).tolist(),
            "rough_arm_base_positive_x_world": [1.0, 0.0, 0.0],
        }
    )
    orientation = candidate["quality_provenance"]["orientation_resolution"]
    assert candidate["review_state"] == "PENDING_REVIEW"
    assert clients.published is not None
    assert effector_locator.calls == 0
    assert orientation["method"] == "AGENT_SUPPLIED_ROUGH_WORLD_POSITIVE_X"
    assert orientation["vlm"]["invocation_count"] == 0
    assert orientation["timestamped_fk_transform_query"] is None


def test_qualified_majority_resolves_an_ambiguous_orientation_tie_break() -> None:
    attempts = [
        VisualCandidateSelection("z90", 0.56, "First choice.", "test-vlm", "one"),
        VisualCandidateSelection("z0", 0.68, "Second choice.", "test-vlm", "two"),
        VisualCandidateSelection("z0", 0.64, "Tie-break choice.", "test-vlm", "three"),
    ]
    selected, basis, accepted = _bounded_selection_decision(
        attempts,
        minimum_confidence=0.72,
        consensus_confidence_floor=0.55,
    )
    assert accepted is True
    assert selected.candidate_id == "z0"
    assert basis == "QUALIFIED_MAJORITY_CANDIDATE_CONSENSUS"


def test_all_downward_foundation_pose_fits_are_normalized_before_orientation(
    tmp_path: Path,
) -> None:
    class DownwardFitClients(FakeClients):
        def estimate_pose(self, payload):
            self.pose_requests.append(payload)
            pose = np.eye(4)
            pose[1, 1] = -1.0
            pose[2, 2] = -1.0
            pose[2, 3] = 1.0
            return {
                "measurement_id": f"measurement-{len(self.pose_requests)}",
                "camera_from_centered_mesh": pose.tolist(),
                "quality": {"score": 0.88, "hypothesis_count": 252},
                "timing": {"native_elapsed_ms": 18.0},
                "provenance": {"backend": "TEST"},
            }

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(np.zeros((120, 160, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    Image.fromarray(mask).save(mask_path)
    config = json.loads(
        (
            ROOT / "skills/locate_arm_base/config_templates/skill.default.json"
        ).read_text(encoding="utf-8")
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    clients = DownwardFitClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=FakeEffectorLocator(),
        fit_selector=FakeFitSelector(),
    )
    candidate = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {
                "fx": 150.0,
                "fy": 150.0,
                "cx": 80.0,
                "cy": 60.0,
            },
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "world_from_camera": np.eye(4).tolist(),
        }
    )
    assert len(clients.pose_requests) == 2
    inspection = skill.inspection_snapshot()
    assert inspection["foundation_pose"]["physically_eligible_candidate_ids"] == [
        "fit_1",
        "fit_2",
    ]
    assert all(
        fit["upright_normalization_degrees"] == 180
        for fit in inspection["foundation_pose"]["fits"]
    )
    normalization = candidate["quality_provenance"]["orientation_resolution"][
        "world_up_normalization"
    ]
    assert normalization["status"] == "APPLIED_LOCAL_X_180"
    assert normalization["raw_arm_base_positive_z_dot_world"] == pytest.approx(-1.0)
    assert normalization["corrected_arm_base_positive_z_dot_world"] == pytest.approx(1.0)
    assert candidate["world_from_arm_base"]["matrix"][2][2] == pytest.approx(1.0)


def test_repeated_moderate_fit_consensus_then_single_effector_call_is_accepted(
    tmp_path: Path,
) -> None:
    class ModerateFitSelector(FakeFitSelector):
        def __init__(self) -> None:
            self.calls = 0

        def select(self, reference_paths, contact_sheet_path, candidate_ids):
            self.calls += 1
            return VisualCandidateSelection(
                candidate_ids[0],
                0.48,
                "Repeated fit is moderately convincing.",
                "test-vlm",
                f"fit-response-{self.calls}",
            )

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(np.zeros((120, 160, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    Image.fromarray(mask).save(mask_path)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    fit_selector = ModerateFitSelector()
    effector_locator = FakeEffectorLocator()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=FakeClients(),
        effector_locator=effector_locator,
        fit_selector=fit_selector,
    )
    result = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {
                "fx": 150.0,
                "fy": 150.0,
                "cx": 80.0,
                "cy": 60.0,
            },
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "diagnostic_only": True,
        }
    )
    assert result["status"] == "VISUAL_PIPELINE_COMPLETED"
    inspection = skill.inspection_snapshot()
    assert fit_selector.calls == 2
    assert effector_locator.calls == 1
    assert inspection["foundation_pose"]["selection"]["decision_basis"] == (
        "REPEATED_CANDIDATE_CONSENSUS"
    )
    assert inspection["orientation_selection"]["decision_basis"] == (
        "SINGLE_VLM_EFFECTOR_POINT_WITH_TIMESTAMPED_FK"
    )
    assert result["orientation_resolution"]["vlm"]["invocation_count"] == 1


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("mask_attempt_count", "mask_attempt_count"),
        ("fit_candidate_count", "fit_candidate_count"),
    ],
)
def test_candidate_counts_reject_zero(
    tmp_path: Path, field: str, message: str
) -> None:
    rgb_path = tmp_path / f"{field}_rgb.png"
    depth_path = tmp_path / f"{field}_depth.npy"
    mask_path = tmp_path / f"{field}_mask.png"
    Image.fromarray(np.zeros((120, 160, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[20:100, 30:140] = 255
    Image.fromarray(mask).save(mask_path)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / f"{field}_artifacts")
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=FakeClients(),
        effector_locator=FakeEffectorLocator(),
        fit_selector=FakeFitSelector(),
    )
    request = {
        "rgb_path": str(rgb_path),
        "depth_npy_path": str(depth_path),
        "mask_path": str(mask_path),
        "camera_intrinsics": {
            "fx": 150.0,
            "fy": 150.0,
            "cx": 80.0,
            "cy": 60.0,
        },
        "camera_frame": "test_camera",
        "observed_at_us": time.time_ns() // 1000,
        field: 0,
    }
    with pytest.raises(ValueError, match=message):
        skill.run(request)


def test_skill_routes_vlm_seed_prompt_through_sam2_provider(tmp_path: Path) -> None:
    class RecordingPromptLocator(FakePromptLocator):
        def __init__(self) -> None:
            self.calls = 0
            self.additional_guidance: list[str] = []

        def locate(self, reference_paths, scene_path, **kwargs):
            self.calls += 1
            self.additional_guidance.append(str(kwargs.get("additional_guidance") or ""))
            return super().locate(reference_paths, scene_path, **kwargs)

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    Image.fromarray(np.zeros((240, 320, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((240, 320), dtype=np.float32), allow_pickle=False)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(encoding="utf-8")
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    prompt_locator = RecordingPromptLocator()
    clients = FakeClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=FakeEffectorLocator(),
        prompt_locator=prompt_locator,
        fit_selector=FakeFitSelector(),
    )
    candidate = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "camera_intrinsics": {"fx": 300.0, "fy": 300.0, "cx": 160.0, "cy": 120.0},
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "world_from_camera": np.eye(4).tolist(),
        }
    )
    acquisition = candidate["quality_provenance"]["source_evidence"]["mask_acquisition"]
    assert acquisition["method"] == "INDEPENDENT_VLM_POINT_TO_SAM2_MASKS"
    assert acquisition["configured_attempt_count"] == 2
    assert acquisition["produced_candidate_count"] == 2
    assert prompt_locator.calls == 2
    assert prompt_locator.additional_guidance == [
        skill.profile.vlm_seed_guidance
    ] * 2
    assert len(clients.segmentation_requests) == 2
    assert clients.sam2_hot_requests == 1
    assert len({request["request_id"] for request in clients.segmentation_requests}) == 2
    assert all(
        request["positive_points_yx"] == [[500, 500]]
        for request in clients.segmentation_requests
    )
    assert all(
        attempt["sam2_provenance"]["provider_id"] == "test.sam2"
        for attempt in acquisition["attempts"]
    )
    mask_proof = candidate["quality_provenance"]
    assert mask_proof["mask_retention"] == {
        "review_performed": False,
        "retention_policy": "ALL_ACQUIRED_MASKS_WITHOUT_POST_SAM2_VLM_REVIEW",
        "retained_candidate_ids": ["mask_1", "mask_2"],
        "rationale": (
            "Every successfully acquired mask is retained; no post-SAM2 VLM "
            "selection or rejection is performed."
        ),
        "model": None,
        "response_id": None,
        "structured_output_attempt_count": 0,
    }
    assert mask_proof["mask_vote"]["vote_threshold"] == 1
    assert mask_proof["mask_vote"]["retained_candidate_ids"] == [
        "mask_1",
        "mask_2",
    ]
    assert mask_proof["mask_vote"]["dilation_radius_px"] == 4


def test_visual_diagnostic_skips_world_axis_and_routes_references_by_role(
    tmp_path: Path,
) -> None:
    seen = {}

    class RecordingEffectorLocator(FakeEffectorLocator):
        def locate(self, mounted_effector, scene_path):
            seen["effector_profile"] = mounted_effector["profile_id"]
            seen["effector_scene"] = scene_path.name
            return super().locate(mounted_effector, scene_path)

    class RecordingPromptLocator(FakePromptLocator):
        def locate(self, reference_paths, scene_path, **kwargs):
            seen["seed"] = [path.name for path in reference_paths]
            return super().locate(reference_paths, scene_path, **kwargs)

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    Image.fromarray(np.zeros((240, 320, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((240, 320), dtype=np.float32), allow_pickle=False)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    clients = FakeClients()
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=clients,
        effector_locator=RecordingEffectorLocator(),
        prompt_locator=RecordingPromptLocator(),
        fit_selector=FakeFitSelector(),
    )
    result = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "camera_intrinsics": {
                "fx": 300.0,
                "fy": 300.0,
                "cx": 160.0,
                "cy": 120.0,
            },
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "diagnostic_only": True,
        }
    )
    assert result["status"] == "VISUAL_PIPELINE_COMPLETED"
    assert result["candidate_published"] is False
    assert clients.published is None
    assert seen["seed"] == [
        "01_Base_reference_atlas.png",
        "02_Arm_axis_reference_no_effector_4views.png",
    ]
    assert seen["effector_profile"] == "rebot_b601_dm.bare_gripper"
    assert seen["effector_scene"] == "rgb.png"
    assert skill.inspection_snapshot()["stage"] == "VISUAL_DIAGNOSTIC_COMPLETED"


def test_skill_rejects_stale_active_arm_profile_digest(tmp_path: Path) -> None:
    class StaleClients(FakeClients):
        def active_arm_profile_state(self, stream="robot_arm.assembly_state"):
            state = super().active_arm_profile_state(stream)
            state["profile_file_sha256"]["arm_model"] = "0" * 64
            return state

    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    skill = LocateArmBaseSkill(config, ROOT, clients=StaleClients())
    import pytest

    with pytest.raises(RuntimeError, match="restart the arm Provider"):
        skill.run({"use_latest_camera": True})


def test_pre_profile_failure_owns_new_inspection_and_preserves_previous_attempt(
    tmp_path: Path,
) -> None:
    class MissingAssemblyClients(FakeClients):
        def ensure_active_arm_profile_state(self, *args, **kwargs):
            raise RuntimeError(
                "ARM_ASSEMBLY_STATE_REQUIRED: selected arm Provider did not publish"
            )

    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    skill = LocateArmBaseSkill(config, ROOT, clients=MissingAssemblyClients())
    import pytest

    with pytest.raises(RuntimeError, match="ARM_ASSEMBLY_STATE_REQUIRED"):
        skill.run({"use_latest_camera": True})
    first = skill.inspection_snapshot()
    first_path = Path(first["run_directory"]) / "inspection.json"
    first_bytes = first_path.read_bytes()
    assert first["status"] == "FAILED"
    assert first["failed_stage"] == "ARM_PROVIDER_READINESS"
    assert first["arm_provider_readiness"]["status"] == "FAILED"
    assert first["arm_provider_readiness"]["provider_id"] == "robot_arm.rebot_dm"

    with pytest.raises(RuntimeError, match="ARM_ASSEMBLY_STATE_REQUIRED"):
        skill.run({"use_latest_camera": True})
    second = skill.inspection_snapshot()
    assert second["run_id"] != first["run_id"]
    assert Path(second["run_directory"], "inspection.json").is_file()
    assert first_path.read_bytes() == first_bytes


def test_overlapping_run_is_rejected_without_overwriting_active_inspection(
    tmp_path: Path,
) -> None:
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    skill = LocateArmBaseSkill(config, ROOT, clients=FakeClients())
    skill._inspection = {"run_id": "active-run", "status": "RUNNING"}
    skill._run_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="LOCATE_ARM_BASE_ALREADY_RUNNING"):
            skill.run({"use_latest_camera": True})
    finally:
        skill._run_lock.release()
    assert skill.inspection_snapshot() == {
        "run_id": "active-run",
        "status": "RUNNING",
    }


def test_negative_foundation_pose_ranking_scores_are_not_absolute_rejections(
    tmp_path: Path,
) -> None:
    class NegativeScoreClients(FakeClients):
        def estimate_pose(self, payload):
            value = super().estimate_pose(payload)
            value["quality"]["score"] = -11.75
            return value

    rgb_path = tmp_path / "rgb.png"
    depth_path = tmp_path / "depth.npy"
    mask_path = tmp_path / "mask.png"
    Image.fromarray(np.zeros((120, 160, 3), dtype=np.uint8)).save(rgb_path)
    np.save(depth_path, np.ones((120, 160), dtype=np.float32), allow_pickle=False)
    mask = np.zeros((120, 160), dtype=np.uint8)
    mask[30:100, 40:130] = 255
    Image.fromarray(mask).save(mask_path)
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    config["artifact_root"] = str(tmp_path / "artifacts")
    skill = LocateArmBaseSkill(
        config,
        ROOT,
        clients=NegativeScoreClients(),
        effector_locator=FakeEffectorLocator(),
        fit_selector=FakeFitSelector(),
    )
    result = skill.run(
        {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "mask_path": str(mask_path),
            "camera_intrinsics": {
                "fx": 150.0,
                "fy": 150.0,
                "cx": 80.0,
                "cy": 60.0,
            },
            "camera_frame": "test_camera",
            "observed_at_us": time.time_ns() // 1000,
            "diagnostic_only": True,
            "mask_attempt_count": 2,
            "fit_candidate_count": 5,
        }
    )
    assert result["status"] == "VISUAL_PIPELINE_COMPLETED"
    assert result["foundation_pose"]["ranking_score_raw"] == -11.75
    assert result["foundation_pose"]["score_semantics"] == (
        "AUDIT_ONLY_NOT_SELECTION_INPUT"
    )
    inspection = skill.inspection_snapshot()
    assert inspection["mask_candidates"]["configured_count"] == 2
    assert inspection["mask_candidates"]["produced_count"] == 1
    assert inspection["mask_candidates"]["vote"]["vote_threshold"] == 1
    assert inspection["mask_candidates"]["vote"]["dilation_radius_px"] == 4
    assert inspection["foundation_pose"]["candidate_count"] == 5
    assert len(skill.clients.pose_requests) == 5
    assert len(
        {
            request["evidence"]["mask"]["path"]
            for request in skill.clients.pose_requests
        }
    ) == 1
    assert inspection["foundation_pose"]["all_fits_use_selected_mask"] is True
