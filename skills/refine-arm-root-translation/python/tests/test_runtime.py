from __future__ import annotations

import copy
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from refine_arm_root_translation import TranslationRefinementSkill, load_effector_profile


SKILL_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = (
    WORKSPACE_ROOT
    / "providers"
    / "rebot_arm_dm"
    / "profiles"
    / "effectors"
    / "rebot_b601_dm_bare_gripper.v2.json"
)
IDENTITIES = {
    "world_frame": "local_vio/epoch-1",
    "vio_session_epoch": "epoch-1",
    "spatial_convention": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
    "camera_provider_id": "camera.test",
    "camera_boot_id": "camera-boot",
    "camera_calibration_revision": "cal-1",
    "arm_provider_id": "arm.test",
    "arm_boot_id": "arm-boot",
    "arm_model_id": "rebot_arm_b601_dm",
    "arm_model_revision": "rebot-owner-observed-wrist-gripper-envelope-0.1.31",
    "assembly_id": "primary_manipulator",
    "assembly_revision": "test-assembly-v1",
    "assembly_fingerprint": "assembly-fingerprint-test",
    "effector_profile_id": "rebot_b601_dm.bare_gripper",
    "effector_profile_revision": "rebot-b601-dm-bare-gripper-v5",
    "effector_profile_sha256": None,
}


def detection_response(
    *,
    landmark_id: str = "rail_lateral_endpoint_mean",
    point_ids: tuple[str, str] = ("rail_lateral_left", "rail_lateral_right"),
    point_xs: tuple[int, int] = (400, 600),
) -> str:
    return json.dumps(
        {
            "schema": "midbrain.effector_landmark_detection",
            "schema_version": 2,
            "scene_suitable": True,
            "landmark_id": landmark_id,
            "coordinate_space": "NORMALIZED_YX_0_1000_PER_IMAGE",
            "reason": "Both neon-green rail endpoints and their depth surfaces are visible.",
            "points": [
                {
                    "point_id": point_ids[0],
                    "rgb_yx_0_1000": [500, point_xs[0]],
                    "registered_depth_yx_0_1000": [500, point_xs[0]],
                    "confidence": 0.96,
                    "same_surface_confidence": 0.95,
                    "reason": "Left rail terminal face on the same depth surface.",
                },
                {
                    "point_id": point_ids[1],
                    "rgb_yx_0_1000": [500, point_xs[1]],
                    "registered_depth_yx_0_1000": [500, point_xs[1]],
                    "confidence": 0.97,
                    "same_surface_confidence": 0.96,
                    "reason": "Right rail terminal face on the same depth surface.",
                },
            ],
        }
    )


def review_response(verdict: str = "PASS") -> str:
    return json.dumps(
        {
            "schema": "midbrain.effector_landmark_quality_review",
            "schema_version": 1,
            "landmark_id": "rail_lateral_endpoint_mean",
            "verdict": verdict,
            "reason": "The marked rail endpoints and registered depth surfaces agree.",
            "reviewed_point_ids": [
                "rail_lateral_left",
                "rail_lateral_right",
            ],
        }
    )


class FakeVlm:
    model_id = "vlm.test"

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeStateStore:
    def __init__(self) -> None:
        self.state = {
            "schema": "midbrain.compact_arm_root_alignment_state",
            "schema_version": 1,
            "revision": 3,
            "world_from_base": np.eye(4).tolist(),
            "identities": copy.deepcopy(IDENTITIES),
            "last_update": None,
        }
        self.swaps: list[dict] = []
        self.allow_swap = True

    async def snapshot(self):
        return copy.deepcopy(self.state)

    async def compare_and_swap(
        self,
        *,
        expected_revision: int,
        state: dict,
        refinement: dict,
    ):
        self.swaps.append(
            {
                "expected_revision": expected_revision,
                "state": copy.deepcopy(state),
                "refinement": copy.deepcopy(refinement),
            }
        )
        if not self.allow_swap or expected_revision != self.state["revision"]:
            return False
        self.state = copy.deepcopy(state)
        return True


class FakeEvidencePublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def register_channels(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "schema": "midbrain.visual_evidence",
            "schema_version": 1,
            "evidence_id": "evidence-1",
        }


def observation_source(*, tool_z_m: float):
    frame_number = 9

    async def capture():
        nonlocal frame_number
        frame_number += 1
        base_from_tool = np.eye(4)
        base_from_tool[0, 3] = 0.08
        base_from_tool[2, 3] = tool_z_m
        return {
            "coherent_snapshot": True,
            "tracking_state": "TRACKING",
            "rgb": np.full((11, 11, 3), 80, dtype=np.uint8),
            "registered_depth_m": np.ones((11, 11), dtype=np.float64),
            "intrinsics": {"fx": 11.0, "fy": 11.0, "cx": 5.0, "cy": 5.0},
            "world_from_camera": np.eye(4),
            "base_from_tool": base_from_tool,
            "temporal_alignment": {
                "policy_id": "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1",
                "base_from_tool_samples": [
                    {
                        "at_us": at_us,
                        "maximum_extrapolation_us": 0,
                        "base_from_tool": base_from_tool.tolist(),
                    }
                    for at_us in (100, 200, 300)
                ],
            },
            "runtime_landmark_bindings": {},
            "identities": copy.deepcopy(IDENTITIES),
            "provenance": {
                "observed_at_us": 123 + frame_number,
                "frame_number": frame_number,
                "rgb_sha256": "a" * 64,
                "registered_depth_sha256": "b" * 64,
            },
        }

    return capture


def state_revalidator():
    async def revalidate(observation: dict):
        return {
            "tracking_state": "TRACKING",
            "identities": copy.deepcopy(IDENTITIES),
            "checked_at_us": 456,
        }

    return revalidate


def runtime(
    *,
    tool_z_m: float,
    responses: list[str | Exception],
    threshold: float = 0.005,
    maximum_raw_delta_m: float = 0.1,
    maximum_adopted_delta_m: float = 0.025,
    reference_image_source=None,
    profile_path: Path = PROFILE_PATH,
):
    vlm = FakeVlm(responses)
    store = FakeStateStore()
    evidence = FakeEvidencePublisher()
    skill = TranslationRefinementSkill(
        profile_path=profile_path,
        observation_source=observation_source(tool_z_m=tool_z_m),
        state_revalidator=state_revalidator(),
        vlm=vlm,
        state_store=store,
        visual_evidence_publisher=evidence,
        reference_image_source=reference_image_source,
        review_threshold_m=threshold,
        maximum_raw_translation_delta_m=maximum_raw_delta_m,
        maximum_adopted_translation_delta_m=maximum_adopted_delta_m,
    )
    return skill, vlm, store, evidence


def test_small_delta_calls_one_vlm_and_publishes_svg_annotations() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert result["landmark_depth_reselection"]["outcome"] == "NOT_REQUIRED"
    assert result["landmark_depth_reselection"]["attempt_count"] == 1
    assert len(vlm.calls) == 1
    assert [image["id"] for image in vlm.calls[0]["images"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
    ]
    assert len(evidence.calls) == 1
    assert evidence.calls[0]["default_channel"] == "marked_overlap"
    assert [channel["id"] for channel in evidence.calls[0]["channels"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
        "marked_overlap",
    ]
    annotation_ids = {
        annotation["id"] for annotation in evidence.calls[0]["annotations"]
    }
    assert {
        "new-arm-base-origin",
        "old-alignment-landmark",
        "new-alignment-landmark",
    } <= annotation_ids
    back_projection = result["alignment_image_back_projection"]
    assert len(back_projection["points"]) == 4
    assert store.state["revision"] == 4
    assert np.allclose(
        np.asarray(store.state["world_from_base"])[:3, 3],
        [0.0, 0.0, 0.001],
    )


def test_slow_capture_motion_below_five_mm_is_accepted() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )
    original_source = skill.observation_source

    async def slowly_moving_observation():
        observation = await original_source()
        samples = observation["temporal_alignment"][
            "base_from_tool_samples"
        ]
        for index, sample in enumerate(samples):
            matrix = np.asarray(sample["base_from_tool"], dtype=np.float64)
            matrix[0, 3] += 0.004 * index / (len(samples) - 1)
            sample["base_from_tool"] = matrix.tolist()
        return observation

    skill.observation_source = slowly_moving_observation

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert result["capture_motion"][
        "measured_maximum_landmark_motion_m"
    ] == pytest.approx(0.004)
    assert len(store.swaps) == 1


def test_large_raw_delta_review_gets_raw_channels_full_marking_and_crop() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.99,
        responses=[detection_response(), review_response("PASS")],
    )

    result = asyncio.run(skill.run(adoption_factor=0.1))

    assert len(vlm.calls) == 2
    assert vlm.calls[1]["purpose"] == (
        "EFFECTOR_LANDMARK_MARKING_QUALITY_REVIEW"
    )
    assert [image["id"] for image in vlm.calls[1]["images"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "marked_overlap",
        "landmark_review_crop",
    ]
    assert "outside the physical landmark geometry" in vlm.calls[1]["prompt"]
    assert "do not trust the first model's confidence" in vlm.calls[1]["prompt"]
    assert [
        channel["id"] for channel in evidence.calls[0]["channels"]
    ][-2:] == ["marked_overlap", "landmark_review_crop"]
    assert result["quality_review_evidence"]["channel_ids"] == [
        "rgb",
        "depth",
        "depth_validity",
        "marked_overlap",
        "landmark_review_crop",
    ]
    assert len(result["quality_review_evidence"]["crop_panels"]) == 2
    assert result["quality_review"]["verdict"] == "PASS"
    assert result["state_update_applied"]
    assert np.allclose(
        np.asarray(store.state["world_from_base"])[:3, 3],
        [0.0, 0.0, 0.001],
    )


def test_multi_sample_refinement_averages_raw_vectors_and_updates_once() -> None:
    responses = [
        response
        for _ in range(3)
        for response in (detection_response(), review_response("PASS"))
    ]
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.99,
        responses=responses,
    )
    tool_z_values = iter((0.99, 0.98, 0.97))
    frame_number = 20

    async def varying_observation():
        nonlocal frame_number
        frame_number += 1
        observation = await observation_source(tool_z_m=next(tool_z_values))()
        observation["provenance"]["frame_number"] = frame_number
        observation["provenance"]["observed_at_us"] += frame_number
        return observation

    skill.observation_source = varying_observation

    result = asyncio.run(
        skill.run(adoption_factor=0.5, sample_count=3)
    )

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert len(store.swaps) == 1
    assert store.state["revision"] == 4
    assert np.allclose(result["raw_translation_delta_m"], [0.0, 0.0, 0.02])
    assert np.allclose(result["adopted_translation_delta_m"], [0.0, 0.0, 0.01])
    assert np.allclose(
        np.asarray(store.state["world_from_base"])[:3, 3],
        [0.0, 0.0, 0.01],
    )
    multi = result["multi_sample_refinement"]
    assert multi["requested_sample_count"] == 3
    assert multi["completed_sample_count"] == 3
    assert multi["aggregation"] == (
        "ARITHMETIC_MEAN_OF_RAW_TRANSLATION_DELTAS"
    )
    assert multi["component_standard_deviation_m"][2] == pytest.approx(
        np.std([0.01, 0.02, 0.03])
    )
    assert result["refinement_limits"]["maximum_raw_translation_delta_m"] == (
        pytest.approx(0.3)
    )
    assert result["refinement_limits"][
        "maximum_adopted_translation_delta_m"
    ] == pytest.approx(0.075)
    assert len(vlm.calls) == 6
    assert len(evidence.calls) == 3


def test_multi_sample_failure_is_excluded_and_limits_use_accepted_count() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.99,
        responses=[
            detection_response(),
            review_response("PASS"),
            detection_response(),
            review_response("FAIL"),
            detection_response(),
            review_response("PASS"),
        ],
    )
    tool_z_values = iter((0.99, 0.50, 0.97))
    frame_number = 40

    async def varying_observation():
        nonlocal frame_number
        frame_number += 1
        observation = await observation_source(
            tool_z_m=next(tool_z_values)
        )()
        observation["provenance"]["frame_number"] = frame_number
        observation["provenance"]["observed_at_us"] += frame_number
        return observation

    skill.observation_source = varying_observation

    result = asyncio.run(skill.run(sample_count=3))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert len(store.swaps) == 1
    assert store.state["revision"] == 4
    assert result["raw_translation_delta_m"][2] == pytest.approx(0.02)
    multi = result["multi_sample_refinement"]
    assert multi["completed_sample_count"] == 3
    assert multi["accepted_sample_count"] == 2
    assert multi["excluded_sample_count"] == 1
    assert multi["accepted_sample_indexes"] == [1, 3]
    assert multi["excluded_sample_indexes"] == [2]
    assert multi["threshold_scale"] == 2
    assert multi["threshold_scale_basis"] == "ACCEPTED_SAMPLE_COUNT"
    assert [item["included_in_aggregation"] for item in multi["samples"]] == [
        True,
        False,
        True,
    ]
    assert "marked rail endpoints" in multi["samples"][1]["exclusion_reason"]
    assert result["refinement_limits"]["maximum_raw_translation_delta_m"] == (
        pytest.approx(0.2)
    )
    assert result["refinement_limits"][
        "maximum_adopted_translation_delta_m"
    ] == pytest.approx(0.05)
    assert len(result["sample_visual_evidence"]) == 3
    assert len(result["sample_quality_review_evidence"]) == 3


def test_multi_sample_rejects_when_every_sample_is_excluded() -> None:
    responses = [
        response
        for _ in range(3)
        for response in (detection_response(), review_response("FAIL"))
    ]
    skill, _, store, _ = runtime(
        tool_z_m=0.99,
        responses=responses,
    )

    result = asyncio.run(skill.run(sample_count=3))

    assert result["status"] == "REJECTED_QUALITY_REVIEW"
    assert "excluded every sample" in result["reason"]
    multi = result["multi_sample_refinement"]
    assert multi["completed_sample_count"] == 3
    assert multi["accepted_sample_count"] == 0
    assert multi["excluded_sample_count"] == 3
    assert multi["accepted_sample_indexes"] == []
    assert multi["excluded_sample_indexes"] == [1, 2, 3]
    assert multi["threshold_scale"] == 0
    assert all(
        not item["included_in_aggregation"] for item in multi["samples"]
    )
    assert len(result["sample_visual_evidence"]) == 3
    assert len(result["sample_quality_review_evidence"]) == 3
    assert not result["state_update_applied"]
    assert not store.swaps
    assert store.state["revision"] == 3


def test_multi_sample_enforces_shift_limit_from_accepted_count() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.85,
        responses=[
            detection_response(),
            review_response("PASS"),
            detection_response(),
            review_response("FAIL"),
            detection_response(),
            review_response("FAIL"),
        ],
    )
    tool_z_values = iter((0.85, 0.50, 0.50))
    frame_number = 70

    async def varying_observation():
        nonlocal frame_number
        frame_number += 1
        observation = await observation_source(
            tool_z_m=next(tool_z_values)
        )()
        observation["provenance"]["frame_number"] = frame_number
        observation["provenance"]["observed_at_us"] += frame_number
        return observation

    skill.observation_source = varying_observation

    result = asyncio.run(
        skill.run(adoption_factor=0.1, sample_count=3)
    )

    assert result["status"] == "REJECTED_DELTA_LIMIT"
    assert "raw translation delta" in result["reason"]
    assert result["multi_sample_refinement"]["accepted_sample_count"] == 1
    assert result["multi_sample_refinement"]["threshold_scale"] == 1
    assert result["refinement_limits"][
        "maximum_raw_translation_delta_m"
    ] == pytest.approx(0.1)
    assert result["raw_translation_delta_norm_m"] == pytest.approx(0.15)
    assert not result["state_update_applied"]
    assert not store.swaps


def test_five_sample_analysis_starts_concurrently_with_unique_request_ids() -> None:
    class CoordinatedVlm:
        model_id = "vlm.concurrent-test"

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.active = 0
            self.maximum_active = 0
            self.started = 0
            self.all_started = asyncio.Event()

        async def invoke(self, **kwargs):
            self.calls.append(kwargs)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.started += 1
            if self.started == 5:
                self.all_started.set()
            try:
                await asyncio.wait_for(self.all_started.wait(), timeout=1.0)
                return detection_response()
            finally:
                self.active -= 1

    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[],
    )
    vlm = CoordinatedVlm()
    skill.vlm = vlm

    result = asyncio.run(skill.run(sample_count=5))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert len(store.swaps) == 1
    assert vlm.maximum_active == 5
    request_ids = [call["request_id"] for call in vlm.calls]
    assert len(set(request_ids)) == 5
    assert all(
        f"/sample-{index:02d}/detect" in request_ids[index - 1]
        for index in range(1, 6)
    )
    multi = result["multi_sample_refinement"]
    assert multi["capture_execution"] == "SEQUENTIAL_DISTINCT_RGBD_FRAMES"
    assert multi["analysis_execution"] == "CONCURRENT_PER_SAMPLE"


def test_multi_sample_rejects_reused_frame_before_vlm_or_state_update() -> None:
    skill, vlm, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()] * 3,
    )
    source = skill.observation_source

    async def repeated_frame():
        observation = await source()
        observation["provenance"].update(
            {
                "observed_at_us": 123,
                "frame_number": 10,
                "rgb_sha256": "a" * 64,
                "registered_depth_sha256": "b" * 64,
            }
        )
        return observation

    skill.observation_source = repeated_frame
    policy = skill.profile["capture_motion_policy"]
    policy["maximum_transform_wait_ms"] = 5.0
    policy["transform_retry_interval_ms"] = 1.0

    with pytest.raises(RuntimeError, match="distinct RGB-D frame"):
        asyncio.run(skill.run(sample_count=3))

    assert not vlm.calls
    assert not store.swaps
    assert store.state["revision"] == 3


def test_multi_sample_defaults_are_one_sample_and_full_adoption() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    result = asyncio.run(skill.run())

    assert result["adoption_factor"] == 1.0
    assert result["multi_sample_refinement"]["requested_sample_count"] == 1
    assert result["multi_sample_refinement"]["threshold_scale"] == 1
    assert len(store.swaps) == 1


@pytest.mark.parametrize("sample_count", [0, 6, 1.5, True])
def test_multi_sample_count_is_bounded(sample_count) -> None:
    skill, _, _, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    with pytest.raises(ValueError, match="sample_count"):
        asyncio.run(skill.run(sample_count=sample_count))


def test_invalid_exact_depth_gets_one_vlm_reselection_attempt() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.999,
        responses=[
            detection_response(),
            detection_response(point_xs=(300, 500)),
        ],
        threshold=0.1,
    )
    original_source = skill.observation_source

    async def observation_with_one_invalid_selection():
        observation = await original_source()
        observation["registered_depth_m"][5, 6] = np.nan
        return observation

    skill.observation_source = observation_with_one_invalid_selection

    result = asyncio.run(skill.run(adoption_factor=0.1))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert len(vlm.calls) == 2
    assert vlm.calls[1]["purpose"] == "EFFECTOR_LANDMARK_DEPTH_RESELECTION"
    assert result["landmark_depth_reselection"]["outcome"] == (
        "VALID_EXACT_DEPTH"
    )
    assert result["landmark_depth_reselection"]["attempt_count"] == 2
    assert [image["id"] for image in vlm.calls[1]["images"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
        "invalid_depth_retry_input",
    ]
    assert "WHITE means a usable exact depth" in vlm.calls[1]["prompt"]
    assert len(store.swaps) == 1
    assert [
        channel["id"] for channel in evidence.calls[0]["channels"]
    ] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
        "invalid_depth_retry_input",
        "marked_overlap",
    ]


def test_invalid_depth_reselection_failure_rejects_without_inventing_depth() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.999,
        responses=[detection_response(), detection_response()],
        threshold=0.1,
    )
    original_source = skill.observation_source

    async def observation_with_one_invalid_selection():
        observation = await original_source()
        observation["registered_depth_m"][5, 6] = np.nan
        return observation

    skill.observation_source = observation_with_one_invalid_selection

    result = asyncio.run(skill.run(adoption_factor=0.1))

    assert result["status"] == "REJECTED_OBSERVATION"
    assert "one VLM depth-reselection attempt failed" in result["reason"]
    assert result["landmark_depth_reselection"]["outcome"] == "REJECTED"
    assert len(vlm.calls) == 2
    assert not store.swaps
    assert len(evidence.calls) == 1


def test_zero_adoption_quality_reviews_large_measurement_without_state_write() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.5,
        responses=[detection_response(), review_response("PASS")],
    )

    result = asyncio.run(skill.run(adoption_factor=0.0))

    assert result["status"] == "OBSERVATION_ONLY"
    assert len(vlm.calls) == 2
    assert result["quality_review"]["verdict"] == "PASS"
    assert len(evidence.calls) == 1
    assert not store.swaps
    assert store.state["revision"] == 3


def test_profile_raw_delta_limit_rejects_gross_landmark_mismatch() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.5,
        responses=[detection_response(), review_response("PASS")],
    )

    result = asyncio.run(skill.run(adoption_factor=0.1))

    assert result["status"] == "REJECTED_DELTA_LIMIT"
    assert "raw translation delta" in result["reason"]
    assert len(vlm.calls) == 2
    assert result["quality_review"]["verdict"] == "PASS"
    assert len(evidence.calls) == 1
    assert not store.swaps


def test_profile_adopted_delta_limit_requires_smaller_factor() -> None:
    skill, vlm, store, _ = runtime(
        tool_z_m=0.95,
        responses=[detection_response(), review_response("PASS")],
    )

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "REJECTED_DELTA_LIMIT"
    assert "reduce adoption_factor" in result["reason"]
    assert len(vlm.calls) == 2
    assert result["quality_review"]["verdict"] == "PASS"
    assert not store.swaps


def test_failed_second_review_never_writes_state() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.99,
        responses=[detection_response(), review_response("FAIL")],
    )

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "REJECTED_QUALITY_REVIEW"
    assert not result["state_update_applied"]
    assert not store.swaps


def test_malformed_second_review_is_nonmutating_unresolved_rejection() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.99,
        responses=[
            detection_response(),
            json.dumps({"verdict": "PASS"}),
        ],
    )

    result = asyncio.run(skill.run(adoption_factor=0.5))

    assert result["status"] == "REJECTED_QUALITY_REVIEW"
    assert result["workflow_complete"]
    assert not result["eligible_for_state_update"]
    assert result["quality_review"]["verdict"] == "UNRESOLVED"
    assert "quality-review VLM output rejected" in result["quality_review"][
        "reason"
    ]
    assert "missing:" in result["quality_review"]["reason"]
    assert not result["state_update_applied"]
    assert not store.swaps
    assert store.state["revision"] == 3
    assert len(vlm.calls) == 2
    assert len(evidence.calls) == 1


def test_capture_motion_over_limit_is_rejected_before_vlm() -> None:
    skill, vlm, _, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    async def fast_capture_observation():
        value = await observation_source(tool_z_m=0.999)()
        moved = np.asarray(
            value["temporal_alignment"]["base_from_tool_samples"][-1][
                "base_from_tool"
            ],
            dtype=np.float64,
        )
        moved[0, 3] += 0.006
        value["temporal_alignment"]["base_from_tool_samples"][-1][
            "base_from_tool"
        ] = moved.tolist()
        return value

    skill.observation_source = fast_capture_observation

    with pytest.raises(RuntimeError, match="capture-time limit"):
        asyncio.run(skill.run(adoption_factor=1.0))
    assert not vlm.calls


def test_capture_motion_gate_includes_rotation_of_offset_landmark() -> None:
    skill, _, _, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )
    observation = asyncio.run(observation_source(tool_z_m=0.999)())
    angle_rad = np.deg2rad(2.0)
    rotated = np.asarray(
        observation["temporal_alignment"]["base_from_tool_samples"][-1][
            "base_from_tool"
        ],
        dtype=np.float64,
    )
    rotated[:3, :3] = [
        [np.cos(angle_rad), -np.sin(angle_rad), 0.0],
        [np.sin(angle_rad), np.cos(angle_rad), 0.0],
        [0.0, 0.0, 1.0],
    ]
    observation["temporal_alignment"]["base_from_tool_samples"][-1][
        "base_from_tool"
    ] = rotated.tolist()

    with pytest.raises(RuntimeError, match="capture-time limit"):
        skill._validate_capture_motion(
            observation,
            tool_point=np.asarray([0.2, 0.0, 0.0]),
        )


def test_stale_compare_and_swap_returns_nonapplied_result() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )
    store.allow_swap = False

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "STALE_ACTIVE_REVISION"
    assert not result["state_update_applied"]
    assert store.state["revision"] == 3


def test_tool_motion_after_capture_does_not_invalidate_state_update() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    async def moved(observation: dict):
        return {
            "tracking_state": "TRACKING",
            "identities": copy.deepcopy(IDENTITIES),
            "checked_at_us": 456,
        }

    skill.state_revalidator = moved

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "TRANSLATION_UPDATE_READY"
    assert result["state_update_applied"]
    assert result["context_revalidation"][
        "post_capture_tool_motion_invalidates_capture"
    ] is False
    assert len(store.swaps) == 1


def test_world_tracking_loss_during_vlm_inference_rejects_update() -> None:
    skill, _, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )

    async def tracking_lost(observation: dict):
        return {
            "tracking_state": "LOST",
            "identities": copy.deepcopy(IDENTITIES),
            "checked_at_us": 456,
        }

    skill.state_revalidator = tracking_lost

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "REJECTED_CONTEXT_CHANGED"
    assert "world tracking is not TRACKING" in result["reason"]
    assert not result["state_update_applied"]
    assert not store.swaps


def test_invalid_exact_depth_retry_failure_still_publishes_evidence() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.999,
        responses=[
            detection_response(),
            RuntimeError("VLM route unavailable"),
        ],
    )
    valid_source = skill.observation_source

    async def invalid_depth_observation():
        observation = await valid_source()
        observation["registered_depth_m"][5, 4] = np.nan
        return observation

    skill.observation_source = invalid_depth_observation

    result = asyncio.run(skill.run(adoption_factor=1.0))

    assert result["status"] == "REJECTED_OBSERVATION"
    assert "one VLM depth-reselection attempt failed" in result["reason"]
    assert "VLM route unavailable" in result["reason"]
    assert len(vlm.calls) == 2
    assert not store.swaps
    assert len(evidence.calls) == 1
    assert evidence.calls[0]["default_channel"] == "marked_overlap"
    assert len(evidence.calls[0]["annotations"]) == 6
    rejected_annotations = [
        annotation
        for annotation in evidence.calls[0]["annotations"]
        if str(annotation["id"]).endswith("-rejected-selection")
    ]
    assert len(rejected_annotations) == 2
    assert all(
        annotation["applies_to_channels"] == ["invalid_depth_retry_input"]
        for annotation in rejected_annotations
    )


def test_malformed_vlm_output_is_visible_nonmutating_rejection() -> None:
    skill, vlm, store, evidence = runtime(
        tool_z_m=0.999,
        responses=[json.dumps({"landmark_id": "rail_lateral_endpoint_mean"})],
    )

    result = asyncio.run(skill.run(adoption_factor=0.5))

    assert result["status"] == "REJECTED_OBSERVATION"
    assert result["workflow_complete"]
    assert not result["eligible_for_state_update"]
    assert "landmark VLM output rejected" in result["reason"]
    assert "missing:" in result["reason"]
    assert not store.swaps
    assert store.state["revision"] == 3
    assert len(vlm.calls) == 1
    assert len(evidence.calls) == 1
    assert evidence.calls[0]["default_channel"] == "marked_overlap"
    assert [channel["id"] for channel in evidence.calls[0]["channels"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
        "marked_overlap",
    ]
    assert evidence.calls[0]["annotations"] == []


def test_arm_model_revision_must_match_effector_profile() -> None:
    skill, vlm, store, _ = runtime(
        tool_z_m=0.999,
        responses=[detection_response()],
    )
    original_source = skill.observation_source
    store.state["identities"]["arm_model_revision"] = "unexpected-model"

    async def mismatched_observation():
        observation = await original_source()
        observation["identities"]["arm_model_revision"] = "unexpected-model"
        return observation

    skill.observation_source = mismatched_observation

    with pytest.raises(RuntimeError, match="active arm_model_revision"):
        asyncio.run(skill.run(adoption_factor=1.0))

    assert not vlm.calls
    assert not store.swaps


def test_rail_landmark_sends_configured_cad_atlas_to_vlm(
    tmp_path: Path,
) -> None:
    loaded_asset_ids: list[list[str]] = []

    async def reference_images(asset_ids: list[str]):
        loaded_asset_ids.append(list(asset_ids))
        return [
            {
                "id": "cad_reference_0",
                "label": "Rail-Bracket CAD reference",
                "image_bytes": b"test-png",
                "media_type": "image/png",
                "width": 640,
                "height": 480,
            }
        ]

    profile = load_effector_profile(PROFILE_PATH)
    rail = next(
        item
        for item in profile["visual_alignment_landmarks"]
        if item["landmark_id"] == "rail_lateral_endpoint_mean"
    )
    rail["vlm_reference_asset_ids"] = [
        "rebot_b601_dm.gripper_reference_atlas.v1"
    ]
    profile_path = tmp_path / "effector_profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    skill, vlm, store, _ = runtime(
        tool_z_m=0.999,
        responses=[
            detection_response(
                landmark_id="rail_lateral_endpoint_mean",
                point_ids=("rail_lateral_left", "rail_lateral_right"),
                point_xs=(0, 1000),
            )
        ],
        reference_image_source=reference_images,
        profile_path=profile_path,
    )
    original_source = skill.observation_source

    async def rail_observation():
        observation = await original_source()
        observation["intrinsics"]["fx"] = 55.0
        return observation

    skill.observation_source = rail_observation

    result = asyncio.run(
        skill.run(
            adoption_factor=0.0,
            landmark_id="rail_lateral_endpoint_mean",
        )
    )

    assert result["status"] == "OBSERVATION_ONLY"
    assert loaded_asset_ids == [
        ["rebot_b601_dm.gripper_reference_atlas.v1"]
    ]
    assert [image["id"] for image in vlm.calls[0]["images"]] == [
        "rgb",
        "depth",
        "depth_validity",
        "rgb_depth",
        "cad_reference_0",
    ]
    assert not store.swaps
