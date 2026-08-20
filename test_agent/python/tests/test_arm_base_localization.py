from __future__ import annotations

import json
import asyncio
from pathlib import Path
import threading
import time
import uuid

from jsonschema import validate
from PIL import Image
import pytest

from physical_agent_test.arm_base_activation import (
    ArmBaseActivationService,
    candidate_payload_sha256,
)
from physical_agent_test.arm_base_localization_adapter import (
    ArmBaseLocalizationSkillAdapter,
)
from physical_agent_test.visual_evidence import VisualEvidenceStore
from physical_agent_test.vlm_router import (
    reset_vlm_model_selection,
    set_vlm_model_selection,
)


class FakeManager:
    def __init__(self) -> None:
        self.request = None

    async def workcell_calibrations(self):
        return {"activations": []}

    async def activate_workcell_calibration(self, request):
        self.request = request
        return {
            "activation_id": "activation-1",
            "calibration_revision": request["candidate"]["candidate_id"],
            "candidate_sha256": request["candidate"]["candidate_sha256"],
            "state": "ACTIVE",
            "motion_usable": True,
        }


def write_candidate(root: Path) -> dict:
    candidate = {
        "schema": "midbrain.skill.locate_arm_base.calibration_candidate",
        "schema_version": 1,
        "candidate_id": str(uuid.uuid4()),
        "expires_at_us": time.time_ns() // 1000 + 60_000_000,
        "review_state": "PENDING_REVIEW",
        "motion_usable": False,
        "activation_owner": "RESOURCE_PROVIDER_MANAGER",
    }
    candidate["candidate_sha256"] = candidate_payload_sha256(candidate)
    root.mkdir(parents=True)
    (root / f"{candidate['candidate_id']}.json").write_text(
        json.dumps(candidate), encoding="utf-8"
    )
    return candidate


def test_activation_signs_exact_candidate_and_delegates_to_manager(
    tmp_path: Path,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    manager = FakeManager()
    service = ArmBaseActivationService(
        manager,
        review_auth_secret="a-review-secret-that-is-longer-than-thirty-two-bytes",
        candidate_root=tmp_path / "candidates",
        review_root=tmp_path / "reviews",
    )
    result = asyncio.run(
        service.review_and_activate(
            candidate_id=candidate["candidate_id"],
            candidate_sha256=candidate["candidate_sha256"],
        )
    )
    assert result["motion_usable"] is True
    assert manager.request["candidate"] == candidate
    assert manager.request["review_decision"]["candidate_sha256"] == candidate["candidate_sha256"]
    assert manager.request["review_identity_assertion"].count(".") == 1
    assert len(list((tmp_path / "reviews").glob("*.json"))) == 1


def test_activation_digest_mismatch_returns_exact_continuation(
    tmp_path: Path,
) -> None:
    candidate = write_candidate(tmp_path / "candidates")
    service = ArmBaseActivationService(
        FakeManager(),
        review_auth_secret="a-review-secret-that-is-longer-than-thirty-two-bytes",
        candidate_root=tmp_path / "candidates",
        review_root=tmp_path / "reviews",
    )
    result = asyncio.run(
        service.review_and_activate(
            candidate_id=candidate["candidate_id"],
            candidate_sha256="0" * 64,
        )
    )
    assert result["motion_usable"] is False
    assert result["required_next_tool"] == {
        "name": "review_and_activate_arm_base",
        "arguments": {
            "candidate_id": candidate["candidate_id"],
            "candidate_sha256": candidate["candidate_sha256"],
        },
    }


class FakeLocateSkill:
    def __init__(self, candidate: dict) -> None:
        self.candidate = candidate
        self.request = None

    def run(self, request):
        self.request = request
        return dict(self.candidate)

    def close(self):
        return None


class FakeLocateSkillWithEvidence(FakeLocateSkill):
    def __init__(self, candidate: dict, inspection: dict, error: Exception | None = None):
        super().__init__(candidate)
        self.inspection = inspection
        self.error = error

    def run(self, request):
        if self.error is not None:
            raise self.error
        return super().run(request)

    def inspection_snapshot(self):
        return self.inspection


def evidence_inspection(tmp_path: Path) -> dict:
    images = []
    for index in range(1, 5):
        candidate_id = f"mask_{index}"
        path = tmp_path / f"mask_candidate_{candidate_id}.png"
        Image.new("RGB", (32, 24), (index * 20, 30, 40)).save(path)
        images.append(
            {"image_id": f"mask_candidate_{candidate_id}", "path": str(path)}
        )
    for image_id in ("mask_vote", "mask_final_dilated"):
        path = tmp_path / f"{image_id}.png"
        Image.new("RGB", (32, 24), (80, 50, 40)).save(path)
        images.append({"image_id": image_id, "path": str(path)})
    for index in range(1, 5):
        candidate_id = f"fit_{index}"
        path = tmp_path / f"fit_candidate_{candidate_id}.png"
        Image.new("RGB", (32, 24), (index * 20, 30, 40)).save(path)
        images.append(
            {"image_id": f"fit_candidate_{candidate_id}", "path": str(path)}
        )
    resolved_path = tmp_path / "resolved_pose.png"
    Image.new("RGB", (32, 24), (20, 90, 40)).save(resolved_path)
    images.append({"image_id": "resolved_pose", "path": str(resolved_path)})
    review = {
        "accepted_candidate_ids": ["mask_1", "mask_3", "mask_4"],
        "confidence": 0.85,
        "model": "test-vlm",
    }
    selection = {"candidate_id": "fit_2", "confidence": 0.85, "model": "test-vlm"}
    return {
        "run_id": "run-evidence",
        "images": images,
        "mask_candidates": {"review": review},
        "foundation_pose": {"selection": selection},
        "orientation_selection": {
            "selected_candidate_id": "z180",
            "selected_confidence": 0.86,
            "accepted": True,
            "attempts": [
                {
                    "candidate_id": "z180",
                    "confidence": 0.86,
                    "model": "test-vlm",
                }
            ],
        },
    }


def test_agent_adapter_runs_current_camera_and_returns_review_boundary() -> None:
    candidate = {
        "candidate_id": str(uuid.uuid4()),
        "candidate_sha256": "a" * 64,
        "motion_usable": False,
        "review_state": "PENDING_REVIEW",
    }
    skill = FakeLocateSkill(candidate)
    adapter = ArmBaseLocalizationSkillAdapter(skill, operation_hard_timeout_s=180)
    result = asyncio.run(adapter.run())
    assert skill.request == {"use_latest_camera": True}
    assert result["motion_usable"] is False
    assert result["required_next_tool"]["name"] == "review_and_activate_arm_base"
    assert result["required_next_tool"]["arguments"]["candidate_sha256"] == "a" * 64


def test_agent_adapter_forwards_agent_ui_vlm_selection() -> None:
    candidate = {
        "candidate_id": str(uuid.uuid4()),
        "candidate_sha256": "e" * 64,
        "motion_usable": False,
        "review_state": "PENDING_REVIEW",
    }
    skill = FakeLocateSkill(candidate)
    adapter = ArmBaseLocalizationSkillAdapter(skill)

    async def scenario() -> None:
        token = set_vlm_model_selection("gemini-robotics-er-2-preview")
        try:
            await adapter.run()
        finally:
            reset_vlm_model_selection(token)

    asyncio.run(scenario())
    assert skill.request == {
        "use_latest_camera": True,
        "vlm_model": "gemini-robotics-er-2-preview",
        "vlm_selection_source": "AGENT_UI_SELECTION",
    }


def test_agent_adapter_establishes_world_axis_before_localization() -> None:
    events = []

    async def ensure_tracking():
        events.append("world_axis")
        return {
            "status": "tracking_ready",
            "result": {
                "tracking_state": "TRACKING",
                "world_frame": "local_vio/epoch-7",
                "session_epoch": "epoch-7",
            },
        }

    candidate = {
        "candidate_id": str(uuid.uuid4()),
        "candidate_sha256": "b" * 64,
        "motion_usable": False,
        "review_state": "PENDING_REVIEW",
    }
    skill = FakeLocateSkill(candidate)
    original_run = skill.run

    def ordered_run(request):
        events.append("localize")
        return original_run(request)

    skill.run = ordered_run
    adapter = ArmBaseLocalizationSkillAdapter(
        skill,
        operation_hard_timeout_s=180,
        readiness_ensurer=ensure_tracking,
    )
    asyncio.run(adapter.run())
    assert events == ["world_axis", "localize"]
    assert skill.request == {
        "use_latest_camera": True,
        "world_frame": "local_vio/epoch-7",
        "session_epoch": "epoch-7",
    }


def test_agent_adapter_projects_all_mask_and_fit_candidates(tmp_path: Path) -> None:
    candidate = {
        "candidate_id": str(uuid.uuid4()),
        "candidate_sha256": "c" * 64,
        "motion_usable": False,
        "review_state": "PENDING_REVIEW",
    }
    skill = FakeLocateSkillWithEvidence(
        candidate, evidence_inspection(tmp_path)
    )
    adapter = ArmBaseLocalizationSkillAdapter(
        skill,
        visual_evidence_store=VisualEvidenceStore(),
    )
    result = asyncio.run(adapter.run())
    assert len(result["visual_evidence"]) == 11
    titles = [item["title"] for item in result["visual_evidence"]]
    assert sum("VLM retained" in title for title in titles) == 3
    assert sum("VLM rejected" in title for title in titles) == 1
    assert sum("VLM selected" in title for title in titles) == 1
    assert sum("used by all pose fits" in title for title in titles) == 1
    assert sum("90/180 correction accepted" in title for title in titles) == 1


def test_agent_adapter_returns_failed_result_with_retained_visuals(tmp_path: Path) -> None:
    inspection = evidence_inspection(tmp_path)
    inspection["failed_stage"] = "FIT_CANDIDATE_SELECTED"
    skill = FakeLocateSkillWithEvidence(
        {}, inspection, RuntimeError("fit selection was ambiguous")
    )
    adapter = ArmBaseLocalizationSkillAdapter(
        skill,
        visual_evidence_store=VisualEvidenceStore(),
    )
    result = asyncio.run(adapter.run())
    assert result["status"] == "FAILED"
    assert result["workflow_complete"] is True
    assert result["terminal_failure"] is True
    assert result["retry_allowed"] is False
    assert result["candidate_published"] is False
    assert result["failed_stage"] == "FIT_CANDIDATE_SELECTED"
    assert len(result["visual_evidence"]) == 11
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "skills/locate_arm_base/manifest.json"
        ).read_text(encoding="utf-8")
    )
    validate(result, manifest["agent_discovery"]["output_schema"])


def test_agent_adapter_joins_worker_after_first_waiter_is_cancelled() -> None:
    started = threading.Event()
    finish = threading.Event()

    class BlockingSkill:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, _request):
            self.calls += 1
            started.set()
            if not finish.wait(2.0):
                raise RuntimeError("test worker was not released")
            return {
                "candidate_id": str(uuid.uuid4()),
                "candidate_sha256": "d" * 64,
                "motion_usable": False,
                "review_state": "PENDING_REVIEW",
            }

        def inspection_snapshot(self):
            return {}

        def close(self):
            return None

    async def scenario() -> None:
        skill = BlockingSkill()
        adapter = ArmBaseLocalizationSkillAdapter(skill)
        first_waiter = asyncio.create_task(adapter.run())
        assert await asyncio.to_thread(started.wait, 1.0)
        first_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_waiter
        second_waiter = asyncio.create_task(adapter.run())
        await asyncio.sleep(0)
        assert skill.calls == 1
        finish.set()
        result = await second_waiter
        assert result["candidate_sha256"] == "d" * 64
        assert skill.calls == 1

    asyncio.run(scenario())
