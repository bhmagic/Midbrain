import asyncio
import json
from pathlib import Path

import httpx
import numpy as np

from stationary_world_arm_alignment.pose_validation import (
    load_model_geometry,
    pose_verdict_best_of_two_acceptable,
    pose_verdict_accepted,
    render_pose_overlay,
    select_best_pose_validation,
)
from stationary_world_arm_alignment.models import (
    PUBLIC_RUN_MODES,
    RunMode,
    canonical_run_mode,
    mode_contract,
)
from stationary_world_arm_alignment.vlm import GripperVision


def test_render_pose_overlay_projects_box_and_axes() -> None:
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    camera_from_semantic = np.eye(4, dtype=np.float64)
    camera_from_semantic[2, 3] = 1.0
    payload, diagnostics = render_pose_overlay(
        rgb,
        camera_from_semantic,
        {"fx": 500.0, "fy": 500.0, "cx": 320.0, "cy": 240.0},
        np.asarray([-0.1, -0.1, -0.1]),
        np.asarray([0.1, 0.1, 0.1]),
        np.eye(4, dtype=np.float64),
        axis_length_m=0.15,
        attempt=1,
    )

    assert payload.startswith(b"\xff\xd8")
    assert diagnostics["positive_depth_corner_count"] == 8
    assert diagnostics["visible_corner_count"] == 8
    assert diagnostics["axis_origin_visible"] is True


def test_legacy_full_mode_is_hidden_and_dim_dual_mode_is_available() -> None:
    public_values = {mode.value for mode in PUBLIC_RUN_MODES}
    assert "full" not in public_values
    assert "vlm_refine" not in public_values
    assert RunMode.FOUNDATION_BASE_VLM_GRIPPER.value == "foundation_base_vlm_gripper"
    assert RunMode.FOUNDATION_BASE_GRIPPER.value == "foundation_base_gripper"
    assert RunMode.VLM_GRIPPER_ONLY.value == "vlm_gripper_only"


def test_legacy_vlm_refine_input_maps_to_public_gripper_only_mode() -> None:
    assert canonical_run_mode(RunMode("vlm_refine")) == RunMode.VLM_GRIPPER_ONLY
    assert mode_contract(RunMode.VLM_GRIPPER_ONLY) == {
        "base_alignment_source": "PRIOR_ALIGNMENT_LOCKED_ROTATION",
        "gripper_alignment_source": "VLM_RGBD_BEAK",
        "foundation_pose_models": [],
        "requires_prior_alignment": True,
    }


def test_manifest_discovers_only_public_modes_and_source_contracts() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    manifest = json.loads(
        (
            workspace_root
            / "skills"
            / "stationary_world_arm_alignment"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["modes"] == [mode.value for mode in PUBLIC_RUN_MODES]
    assert manifest["result_schema_version"] == 3
    assert manifest["mode_contracts"] == {
        mode.value: mode_contract(mode)
        for mode in PUBLIC_RUN_MODES
        if mode != RunMode.AUTO
    }


def test_verdict_requires_reasonable_confident_non_bad_pose() -> None:
    acceptable = {
        "pose_reasonable": True,
        "confidence": 0.81,
        "box_fit": "ACCEPTABLE",
        "orientation_fit": "GOOD",
    }
    assert pose_verdict_accepted(acceptable, 0.7)
    assert not pose_verdict_accepted({**acceptable, "confidence": 0.69}, 0.7)
    assert not pose_verdict_accepted({**acceptable, "box_fit": "BAD"}, 0.7)
    assert not pose_verdict_accepted({**acceptable, "pose_reasonable": False}, 0.7)


def test_best_of_two_selects_geometry_before_rejection_confidence() -> None:
    validations = [
        {
            "verdict": {
                "pose_reasonable": False,
                "confidence": 0.99,
                "box_fit": "BAD",
                "orientation_fit": "BAD",
            },
            "projection": {"visible_corner_count": 8},
        },
        {
            "verdict": {
                "pose_reasonable": True,
                "confidence": 0.65,
                "box_fit": "ACCEPTABLE",
                "orientation_fit": "GOOD",
            },
            "projection": {"visible_corner_count": 6},
        },
    ]
    selected = select_best_pose_validation(validations)
    assert selected == 1
    assert pose_verdict_best_of_two_acceptable(
        validations[selected]["verdict"],
        0.6,
    )
    assert not pose_verdict_accepted(
        validations[selected]["verdict"],
        0.7,
    )


def test_best_of_two_never_accepts_bad_geometry() -> None:
    verdict = {
        "pose_reasonable": False,
        "confidence": 0.99,
        "box_fit": "BAD",
        "orientation_fit": "ACCEPTABLE",
    }
    assert not pose_verdict_best_of_two_acceptable(verdict, 0.6)


def test_base_geometry_comes_from_foundation_pose_registry() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    minimum, maximum, mesh_from_semantic = load_model_geometry(
        str(workspace_root),
        "robot_arm_root",
    )

    assert np.allclose(maximum - minimum, [0.14, 0.2, 0.08265], atol=1e-5)
    assert mesh_from_semantic.shape == (4, 4)


def test_vlm_validation_sends_only_overlay_and_base_atlas() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    observed: dict = {}
    verdict = {
        "pose_reasonable": True,
        "confidence": 0.9,
        "box_fit": "GOOD",
        "orientation_fit": "ACCEPTABLE",
        "matched_reference_view": "front-left",
        "reasons": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(verdict)}
                        ],
                    }
                ]
            },
        )

    async def run() -> None:
        vision = GripperVision("test-key", "test-model", workspace_root)
        await vision.http.aclose()
        vision.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await vision.validate_base_pose(b"\xff\xd8\xff\xd9", attempt=1)
            assert result == verdict
        finally:
            await vision.close()

    asyncio.run(run())
    content = observed["input"][0]["content"]
    images = [item for item in content if item["type"] == "input_image"]
    assert len(images) == 2
    assert images[0]["image_url"].startswith("data:image/jpeg;base64,")
    assert images[1]["image_url"].startswith("data:image/png;base64,")
