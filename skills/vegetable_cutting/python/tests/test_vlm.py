from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np

from vegetable_cutting.vlm import (
    SceneVision,
    extract_output_text,
    first_cut_alignment_schema,
    render_registered_depth_evidence,
    scene_schema,
    workspace_presence_schema,
)


def test_scene_schema_forbids_motion_fields() -> None:
    schema = scene_schema()
    assert schema["additionalProperties"] is False
    assert "person_or_animal_visible_in_workspace" in schema["required"]
    serialized = json.dumps(schema).lower()
    assert "motion_command" not in serialized
    assert "joint_target" not in serialized
    assert "kp" not in serialized


def test_scene_schema_only_localizes_two_point_cut_geometry() -> None:
    schema = scene_schema()
    assert "blade" not in schema["properties"]
    assert "knife_occludes_board" not in schema["properties"]
    vegetable_required = schema["properties"]["vegetable"]["required"]
    assert "cutting_line_board_endpoints_yx_1000" in vegetable_required
    assert "polygon_yx_1000" not in schema["properties"]["board"]["properties"]
    serialized = json.dumps(schema).lower()
    for unused_landmark in ("tip_yx", "heel_yx", "spine_yx", "junction_yx"):
        assert unused_landmark not in serialized


def test_extract_output_text_reads_strict_response_message() -> None:
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "{\"person_visible_in_workspace\":false}",
                    }
                ],
            }
        ]
    }
    assert extract_output_text(payload) == "{\"person_visible_in_workspace\":false}"


def test_first_cut_schema_returns_observed_blade_pixel_without_motion_fields() -> None:
    schema = first_cut_alignment_schema()
    assert schema["additionalProperties"] is False
    assert "blade_controlled_point_yx_1000" in schema["required"]
    assert "orange_cut_target_matches_vegetable" in schema["required"]
    assert "translation_offset_camera_mm" not in schema["properties"]
    assert "translation_offset_arm_base_mm" not in schema["properties"]
    assert "rotation_offset_arm_base_deg" not in schema["properties"]
    assert "depth_evidence_used" in schema["required"]
    assert "depth_alignment_meaningful" in schema["required"]
    serialized = json.dumps(schema).lower()
    for forbidden in (
        "motion_command",
        "joint_target",
        "controller",
        "force",
        "kp",
    ):
        assert forbidden not in serialized


def test_workspace_presence_recheck_is_focused_and_excludes_background_objects() -> None:
    captured: dict[str, object] = {}

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.update(payload)
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "{}"}
                            ],
                        }
                    ]
                },
            )

        vision = SceneVision("test-key", "test-model")
        await vision.http.aclose()
        vision.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        try:
            await vision.recheck_workspace_presence(
                np.zeros((8, 8, 3), dtype=np.uint8)
            )
        finally:
            await vision.close()

    asyncio.run(run())
    assert workspace_presence_schema()["additionalProperties"] is False
    content = captured["input"][0]["content"]  # type: ignore[index]
    prompt = content[0]["text"]
    assert "visible human or animal anatomy" in prompt
    assert "cat tree" in prompt
    assert "reflections are not people or animals" in prompt
    assert (
        captured["text"]["format"]["name"]  # type: ignore[index]
        == "vegetable_cutting_workspace_presence_recheck"
    )


def test_scene_and_first_cut_requests_use_separate_strict_schemas() -> None:
    schema_names: list[str] = []

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            schema_names.append(payload["text"]["format"]["name"])
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "{}"}
                            ],
                        }
                    ]
                },
            )

        vision = SceneVision("test-key", "test-model")
        await vision.http.aclose()
        vision.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        try:
            await vision.locate(image)
            await vision.assess_first_cut_alignment(
                image,
                image,
                depth_near_m=0.5,
                depth_far_m=1.5,
                target_depth_m=0.8,
            )
        finally:
            await vision.close()

    asyncio.run(run())
    assert schema_names == [
        "vegetable_cutting_scene",
        "vegetable_cutting_first_cut_alignment",
    ]


def test_first_cut_request_sends_rgb_and_registered_depth_images() -> None:
    captured: dict[str, object] = {}

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.update(payload)
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "{}"}
                            ],
                        }
                    ]
                },
            )

        vision = SceneVision("test-key", "test-model")
        await vision.http.aclose()
        vision.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        try:
            await vision.assess_first_cut_alignment(
                image,
                image,
                depth_near_m=0.5,
                depth_far_m=1.5,
                target_depth_m=0.8,
            )
        finally:
            await vision.close()

    asyncio.run(run())
    content = captured["input"][0]["content"]  # type: ignore[index]
    assert [item["type"] for item in content] == [
        "input_text",
        "input_image",
        "input_image",
    ]
    assert "aligned metric depth" in content[0]["text"]
    assert "0.8000 m" in content[0]["text"]
    assert "orange line" in content[0]["text"]
    assert "blue line" in content[0]["text"]
    assert "parallax" in content[0]["text"]
    assert "Do not move the blade onto the orange line" in content[0]["text"]
    assert "do not estimate a motion vector" in content[0]["text"]
    assert "Do not return camera or arm axes" in content[0]["text"]


def test_registered_depth_evidence_preserves_image_shape_and_validity() -> None:
    depth = np.asarray(
        [
            [0.0, 0.5],
            [1.0, 1.5],
        ],
        dtype=np.float32,
    )
    rgb, metadata = render_registered_depth_evidence(depth)

    assert rgb.shape == (2, 2, 3)
    assert np.array_equal(rgb[0, 0], [0, 0, 0])
    assert metadata["valid_fraction"] == 0.75
    assert metadata["near_m"] < metadata["far_m"]
