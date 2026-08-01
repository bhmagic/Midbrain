from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from stationary_world_arm_alignment.vlm import ReviewedFileVision


def _localization_result() -> dict:
    detection = {
        "visible": True,
        "box_2d": [100, 200, 300, 400],
        "positive_points_2d": [[150, 250], [250, 350]],
        "confidence": 0.9,
    }
    return {
        "base": dict(detection),
        "gripper": dict(detection),
        "jaw_state": "open",
        "beak_points_2d": [[200, 300], [200, 350]],
        "beak_faces_camera": True,
        "holding_object": False,
        "use_local_depth_minimum": True,
        "notes": "reviewed test",
    }


async def _answer_request(path: Path, result: dict) -> None:
    for _ in range(100):
        if path.is_file():
            break
        await asyncio.sleep(0.01)
    request = json.loads(path.read_text(encoding="utf-8"))
    response_path = Path(request["response_path"])
    response_path.write_text(
        json.dumps(
            {
                "schema": "midbrain.reviewed_multimodal_response",
                "schema_version": 1,
                "review_kind": request["review_kind"],
                "request_sha256": request["request_sha256"],
                "reviewer": {
                    "kind": "CODEX_MULTIMODAL",
                    "model": "test-model",
                },
                "result": result,
            }
        ),
        encoding="utf-8",
    )


def test_reviewed_localization_binds_exact_request(tmp_path: Path) -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    vision = ReviewedFileVision(
        workspace_root,
        run_dir,
        timeout_s=2.0,
    )

    async def run() -> dict:
        answer = asyncio.create_task(
            _answer_request(
                run_dir / "localization_review_request.json",
                _localization_result(),
            )
        )
        result = await vision.locate(
            np.zeros((32, 48, 3), dtype=np.uint8),
            require_base=True,
        )
        await answer
        return result

    result = asyncio.run(run())
    assert result["base"]["visible"] is True
    assert result["review_provenance"]["route"] == "REVIEWED_FILE"
    assert result["review_provenance"]["fallback_allowed"] is False


def test_repeated_localization_uses_distinct_review_artifacts(
    tmp_path: Path,
) -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    vision = ReviewedFileVision(
        workspace_root,
        run_dir,
        timeout_s=2.0,
    )

    async def run() -> tuple[dict, dict]:
        first_answer = asyncio.create_task(
            _answer_request(
                run_dir / "localization_review_request.json",
                _localization_result(),
            )
        )
        first = await vision.locate(
            np.zeros((32, 48, 3), dtype=np.uint8),
            require_base=True,
        )
        await first_answer

        second_result = _localization_result()
        second_result["notes"] = "independent second vote"
        second_answer = asyncio.create_task(
            _answer_request(
                run_dir / "localization_vote_2_review_request.json",
                second_result,
            )
        )
        second = await vision.locate(
            np.zeros((32, 48, 3), dtype=np.uint8),
            require_base=True,
        )
        await second_answer
        return first, second

    first, second = asyncio.run(run())
    assert first["notes"] == "reviewed test"
    assert second["notes"] == "independent second vote"
    assert (
        second["review_provenance"]["request_path"]
        .endswith("localization_vote_2_review_request.json")
    )


def test_reviewed_axis_validation_binds_exact_categorical_request(
    tmp_path: Path,
) -> None:
    workspace_root = Path(__file__).resolve().parents[4]
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    vision = ReviewedFileVision(
        workspace_root,
        run_dir,
        timeout_s=2.0,
    )
    review = {
        "base_x_relation_to_gripper": "UNCLEAR",
        "notes": "The arrow is occluded by the arm.",
    }

    async def run() -> dict:
        answer = asyncio.create_task(
            _answer_request(
                run_dir / "pose_validation_attempt_1_review_request.json",
                review,
            )
        )
        result = await vision.validate_base_pose(
            b"\xff\xd8\xff\xd9",
            attempt=1,
        )
        await answer
        return result

    result = asyncio.run(run())
    assert result["base_x_relation_to_gripper"] == "UNCLEAR"
    assert result["notes"] == "The arrow is occluded by the arm."
    assert result["review_provenance"]["review_kind"] == (
        "FOUNDATIONPOSE_BASE_VALIDATION"
    )
    request = json.loads(
        (
            run_dir / "pose_validation_attempt_1_review_request.json"
        ).read_text(encoding="utf-8")
    )
    assert len(request["artifacts"]) == 1
    assert request["artifacts"][0]["label"] == "live_pose_overlay"
    assert "Look only at the red base +X arrow" in request["instructions"]
    schema = request["output_schema"]
    assert schema["required"] == [
        "base_x_relation_to_gripper",
        "notes",
    ]
