import asyncio
import json
from pathlib import Path

import httpx
import numpy as np

from stationary_world_arm_alignment.pose_validation import (
    load_model_geometry,
    projected_visual_scale_review,
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
    assert diagnostics["image_size_px"] == [640, 480]
    assert np.allclose(
        diagnostics["projected_box_xyxy_px"],
        [264.444444, 184.444444, 375.555556, 295.555556],
        atol=1e-5,
    )


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


def test_projected_visual_scale_review_accepts_inclusive_boundaries() -> None:
    image_shape = (1000, 1000, 3)
    visual_box = [0, 0, 1000, 1000]

    lower = projected_visual_scale_review(
        {"projected_box_xyxy_px": [0.0, 0.0, 750.0, 750.0]},
        visual_box,
        image_shape,
    )
    upper = projected_visual_scale_review(
        {"projected_box_xyxy_px": [0.0, 0.0, 1250.0, 1250.0]},
        visual_box,
        image_shape,
    )

    assert lower["equivalent_linear_scale_ratio"] == 0.75
    assert upper["equivalent_linear_scale_ratio"] == 1.25
    assert lower["within_tolerance"] is True
    assert upper["within_tolerance"] is True
    assert lower["warning"] is None
    assert upper["warning"] is None


def test_projected_visual_scale_review_rejects_just_outside_boundaries() -> None:
    image_shape = (1000, 1000, 3)
    visual_box = [0, 0, 1000, 1000]

    lower = projected_visual_scale_review(
        {"projected_box_xyxy_px": [0.0, 0.0, 749.0, 749.0]},
        visual_box,
        image_shape,
    )
    upper = projected_visual_scale_review(
        {"projected_box_xyxy_px": [0.0, 0.0, 1251.0, 1251.0]},
        visual_box,
        image_shape,
    )

    assert lower["equivalent_linear_scale_ratio"] == 0.749
    assert np.isclose(upper["equivalent_linear_scale_ratio"], 1.251)
    assert lower["within_tolerance"] is False
    assert upper["within_tolerance"] is False
    assert "outside" in lower["warning"]
    assert "outside" in upper["warning"]


def test_projected_visual_scale_review_uses_linear_area_equivalent() -> None:
    review = projected_visual_scale_review(
        {"projected_box_xyxy_px": [10.0, 20.0, 410.0, 120.0]},
        [100, 200, 300, 600],
        (500, 1000, 3),
    )

    assert review["visual_size_px"] == [400.0, 100.0]
    assert review["projected_size_px"] == [400.0, 100.0]
    assert review["width_ratio"] == 1.0
    assert review["height_ratio"] == 1.0
    assert review["area_ratio"] == 1.0
    assert review["equivalent_linear_scale_ratio"] == 1.0
    assert review["within_tolerance"] is True


def test_projected_visual_scale_review_degenerate_box_is_warning() -> None:
    review = projected_visual_scale_review(
        {"projected_box_xyxy_px": None},
        [100, 200, 300, 600],
        (500, 1000, 3),
    )

    assert review["available"] is False
    assert review["within_tolerance"] is False
    assert review["equivalent_linear_scale_ratio"] is None
    assert "could not be compared" in review["warning"]


def test_best_of_two_selects_smallest_deterministic_scale_mismatch() -> None:
    validations = [
        {
            "scale_review": {"mismatch_fraction": 0.31},
            "projection": {"visible_corner_count": 8},
        },
        {
            "scale_review": {"mismatch_fraction": 0.27},
            "projection": {"visible_corner_count": 6},
        },
    ]

    assert select_best_pose_validation(validations) == 1


def test_base_geometry_comes_from_foundation_pose_registry() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    minimum, maximum, mesh_from_semantic = load_model_geometry(
        str(workspace_root),
        "robot_arm_root",
    )

    assert np.allclose(maximum - minimum, [0.14, 0.2, 0.08265], atol=1e-5)
    assert mesh_from_semantic.shape == (4, 4)


def test_base_geometry_uses_the_explicit_runtime_registry(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    mesh_path = (
        workspace_root
        / "providers"
        / "foundation_pose"
        / "defaults"
        / "rebot_b601_dm"
        / "models"
        / "Base_clean_centered.obj"
    )
    expected = np.eye(4, dtype=np.float64)
    expected[:3, :3] = np.array(
        [
            [0.0, -1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    expected[:3, 3] = [0.01, -0.02, -0.03]
    registry_path = tmp_path / "models.json"
    registry_path.write_text(
        json.dumps(
            {
                "revision": "test-runtime-registry",
                "models": [
                    {
                        "model_id": "robot_arm_root",
                        "mesh_path": str(mesh_path),
                        "scale_to_m": 0.001,
                        "mesh_from_semantic": expected.reshape(-1).tolist(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _, _, mesh_from_semantic = load_model_geometry(
        str(workspace_root),
        "robot_arm_root",
        str(registry_path),
    )

    assert np.array_equal(mesh_from_semantic, expected)


def test_vlm_axis_review_sends_only_overlay_and_uses_category() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    observed: dict = {}
    verdict = {
        "base_x_relation_to_gripper": "AWAY_FROM_GRIPPER",
        "notes": "The red +X arrow points opposite the visible gripper.",
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
    assert len(images) == 1
    assert images[0]["image_url"].startswith("data:image/jpeg;base64,")
    prompt = " ".join(
        item["text"] for item in content if item["type"] == "input_text"
    )
    assert "red +X arrow" in prompt
    assert "Ignore projected box size" in prompt
    assert observed["text"]["format"]["name"] == (
        "foundation_pose_base_axis_review"
    )


def test_vlm_axis_review_retries_one_malformed_structured_response() -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    request_count = 0
    verdict = {
        "base_x_relation_to_gripper": "TOWARD_GRIPPER",
        "notes": "The red +X arrow points toward the gripper.",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        text = '{"base_x_relation_to_gripper":"TOWARD_GRIPPER"' if (
            request_count == 1
        ) else json.dumps(verdict)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": text}
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
            result = await vision.validate_base_pose(
                b"\xff\xd8\xff\xd9",
                attempt=1,
            )
            assert result == verdict
        finally:
            await vision.close()

    asyncio.run(run())
    assert request_count == 2
