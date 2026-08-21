from __future__ import annotations

import httpx
import numpy as np
from PIL import Image
import pytest

from locate_arm_base.openai_responses import request_structured_response
from locate_arm_base.orientation import (
    OpenAIResponsesArmBasePromptLocator,
    OpenAIResponsesEffectorPointLocator,
)


def _request(
    client: httpx.Client,
    *,
    backend: str = "openai.responses",
    model: str = "test-model",
    content: list[dict] | None = None,
):
    return request_structured_response(
        client,
        backend=backend,
        key="test-key",
        model=model,
        reasoning_effort="low",
        content=content or [{"type": "input_text", "text": "Choose."}],
        schema_name="choice",
        schema={
            "type": "object",
            "properties": {"choice": {"type": "string"}},
            "required": ["choice"],
            "additionalProperties": False,
        },
        operation="test selection",
    )


def test_structured_response_retries_incomplete_reasoning_exhaustion() -> None:
    token_limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        token_limits.append(payload["max_output_tokens"])
        if len(token_limits) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp-incomplete",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp-complete",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"choice":"fit_2"}'}
                        ],
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = _request(client)
    assert response.value == {"choice": "fit_2"}
    assert response.response_id == "resp-complete"
    assert response.attempt_count == 2
    assert token_limits == [3000, 6000]


def test_structured_response_supports_gemini_robotics_er_json_schema() -> None:
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = __import__("json").loads(request.content)
        assert request.url.path.endswith(
            "/gemini-robotics-er-2-preview:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "test-key"
        return httpx.Response(
            200,
            json={
                "responseId": "gemini-response-1",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{"text": '{"choice":"fit_3"}'}]
                        },
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = _request(
            client,
            backend="google.gemini",
            model="gemini-robotics-er-2-preview",
            content=[
                {"type": "input_text", "text": "Choose."},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,aGk=",
                },
            ],
        )
    assert response.value == {"choice": "fit_3"}
    assert response.response_id == "gemini-response-1"
    generation = observed["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingBudget": 0}
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseJsonSchema"]["type"] == "object"
    assert observed["contents"][0]["parts"][1]["inlineData"] == {
        "mimeType": "image/png",
        "data": "aGk=",
    }


def test_structured_response_reports_final_incomplete_reason_and_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-no-json",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError,
            match="max_output_tokens.*resp-no-json",
        ):
            _request(client)


def test_structured_response_retries_transient_connection_reset() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(
            200,
            json={
                "id": "resp-after-reset",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"choice":"fit_1"}'}
                        ],
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = _request(client)
    assert attempts == 2
    assert response.value == {"choice": "fit_1"}
    assert response.attempt_count == 2


def test_arm_base_prompt_explicitly_excludes_touching_supports(
    tmp_path, monkeypatch
) -> None:
    reference_path = tmp_path / "reference.png"
    scene_path = tmp_path / "scene.png"
    Image.new("RGB", (32, 32), "white").save(reference_path)
    Image.new("RGB", (32, 32), "black").save(scene_path)
    observed_prompt = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_prompt
        payload = __import__("json").loads(request.content)
        observed_prompt = payload["input"][0]["content"][0]["text"]
        return httpx.Response(
            200,
            json={
                "id": "resp-prompt",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"top":100,"left":200,"bottom":700,'
                                        '"right":800,"point_1_y":300,'
                                        '"point_1_x":400,"negative_point_y":800,'
                                    '"negative_point_x":500,"confidence":0.9,'
                                    '"rationale":"CAD geometry only."}'
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    locator = OpenAIResponsesArmBasePromptLocator("test-model")
    locator.http.close()
    locator.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = locator.locate(
            (reference_path,),
            scene_path,
            additional_guidance="The target base housing is black.",
        )
    finally:
        locator.close()
    assert result.box_yxyx == (100, 200, 700, 800)
    assert result.negative_points_yx == ((800, 500),)
    assert "black pedestal, riser, enclosure" in observed_prompt
    assert "never put a positive point on the supporting pedestal" in observed_prompt
    assert "negative seed point" in observed_prompt
    assert "profile-described base housing" in observed_prompt
    assert "The target base housing is black." in observed_prompt
    assert "overrides generic appearance assumptions" in observed_prompt


def test_effector_prompt_requests_one_coarse_call_without_point_quality_gate(
    tmp_path, monkeypatch
) -> None:
    scene_path = tmp_path / "scene.png"
    Image.new("RGB", (64, 64), "black").save(scene_path)
    observed_prompt = ""
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_prompt, request_count
        request_count += 1
        payload = __import__("json").loads(request.content)
        observed_prompt = payload["input"][0]["content"][0]["text"]
        return httpx.Response(
            200,
            json={
                "id": "resp-effector",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"effector_identified":true,"points":['
                                    '{"point_id":"left_tip","y_0_1000":410,'
                                    '"x_0_1000":620}],"confidence":0.31,'
                                    '"rationale":"One jaw tip is visible."}'
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    mounted_effector = {
        "profile_id": "test.gripper",
        "display_name": "Test Gripper",
        "controlled_frame": {"frame_id": "test_tool"},
            "extensions": {
                "midbrain.skill.locate_arm_base.v1": {
                    "schema": "midbrain.effector_coarse_orientation_landmark",
                    "schema_version": 1,
                    "arm_base_frame": "test_base",
                    "landmark": {
                        "landmark_id": "tips",
                        "display_name": "gripper tips",
                        "eligible_point_ids": ["left_tip", "right_tip"],
                        "description_for_vlm": "Locate either visible rigid gripper jaw tip.",
                        "controlled_frame_to_landmark_translation_m": [0, 0, 0],
                        "qualification": "TEST",
                    },
                }
            },
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    locator = OpenAIResponsesEffectorPointLocator("test-model")
    locator.http.close()
    locator.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = locator.locate(mounted_effector, scene_path)
    finally:
        locator.close()

    assert request_count == 1
    assert result.identified is True
    assert result.points_yx_0_1000 == (("left_tip", 410, 620),)
    assert result.confidence == pytest.approx(0.31)
    assert "timestamped forward kinematics" in observed_prompt
    assert "one recognized point is sufficient" in observed_prompt
    assert "not a translation calibration or point-quality review" in observed_prompt
