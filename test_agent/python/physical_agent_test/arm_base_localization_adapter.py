from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from locate_arm_base.skill import LocateArmBaseSkill

from .arm_base_activation import ArmBaseActivationService
from .phase4_policy import extend_current_operation_hard_timeout, report_operation_progress
from .vlm_router import get_vlm_model_selection


class ArmBaseLocalizationSkillAdapter:
    """Finite in-process adapter for the maintained locate_arm_base Skill."""

    def __init__(
        self,
        skill: LocateArmBaseSkill,
        *,
        operation_hard_timeout_s: float = 180.0,
        activation_service: ArmBaseActivationService | None = None,
        readiness_ensurer: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        visual_evidence_store: Any = None,
    ) -> None:
        self.skill = skill
        self.operation_hard_timeout_s = float(operation_hard_timeout_s)
        self.activation_service = activation_service
        self.readiness_ensurer = readiness_ensurer
        self.visual_evidence_store = visual_evidence_store
        self._active_guard = asyncio.Lock()
        self._active_task: asyncio.Task[dict[str, Any]] | None = None
        self.last_result: dict[str, Any] | None = None

    async def run(self, *, request: str | None = None) -> dict[str, Any]:
        del request
        async with self._active_guard:
            task = self._active_task
            if task is None or task.done():
                task = asyncio.create_task(self._execute_once())
                task.add_done_callback(self._consume_background_exception)
                self._active_task = task
            else:
                report_operation_progress("LOCATE_ARM_BASE_JOIN_EXISTING_RUN")
        # asyncio.to_thread cannot stop its worker when an Agent or graph deadline
        # cancels one waiter. Shield the shared task so a later call joins the same
        # physical observation rather than racing a second localization attempt.
        return copy.deepcopy(await asyncio.shield(task))

    @staticmethod
    def _consume_background_exception(task: asyncio.Task[dict[str, Any]]) -> None:
        if not task.cancelled():
            task.exception()

    async def _execute_once(self) -> dict[str, Any]:
        extend_current_operation_hard_timeout(
            self.operation_hard_timeout_s,
            stage="LOCATE_ARM_BASE_EXTENDED_DEADLINE",
        )
        skill_request: dict[str, Any] = {"use_latest_camera": True}
        selected_vlm_model = get_vlm_model_selection()
        if selected_vlm_model is not None:
            skill_request.update(
                {
                    "vlm_model": selected_vlm_model,
                    "vlm_selection_source": "AGENT_UI_SELECTION",
                }
            )
        if self.readiness_ensurer is not None:
            report_operation_progress("LOCATE_ARM_BASE_WORLD_AXIS_READINESS")
            readiness = await self.readiness_ensurer()
            if readiness.get("status") != "tracking_ready":
                raise RuntimeError(
                    "locate_arm_base prerequisite did not establish a TRACKING world axis"
                )
            tracking = readiness.get("result")
            tracking = tracking if isinstance(tracking, dict) else {}
            world_frame = str(tracking.get("world_frame") or "").strip()
            session_epoch = str(tracking.get("session_epoch") or "").strip()
            if not world_frame or not session_epoch:
                raise RuntimeError(
                    "locate_arm_base prerequisite returned no world-frame epoch identity"
                )
            skill_request.update(
                {"world_frame": world_frame, "session_epoch": session_epoch}
            )
        report_operation_progress("LOCATE_ARM_BASE_VISUAL_PIPELINE")
        try:
            candidate = await asyncio.to_thread(
                self.skill.run,
                skill_request,
            )
        except Exception as exc:
            inspection = self._inspection_snapshot()
            result = {
                "schema": "midbrain.skill.locate_arm_base.failed_result",
                "schema_version": 1,
                "status": "FAILED",
                "run_id": inspection.get("run_id"),
                "failed_stage": inspection.get("failed_stage"),
                "error": str(exc),
                "workflow_complete": True,
                "terminal_failure": True,
                "retry_allowed": False,
                "motion_usable": False,
                "physical_motion_submitted": False,
                "candidate_published": False,
                "agent_instruction": (
                    "Arm-base localization failed closed and this Agent task must "
                    "terminate without an automatic retry. Report the failure and "
                    "attached visual evidence to the operator; no calibration "
                    "candidate was published."
                ),
            }
            visual_evidence = await self._register_candidate_evidence(inspection)
            if visual_evidence:
                result["visual_evidence"] = visual_evidence
            self.last_result = result
            return result
        result = {
            **candidate,
            "workflow_complete": False,
            "physical_motion_submitted": False,
            "required_next_tool": {
                "name": "review_and_activate_arm_base",
                "arguments": {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_sha256": candidate["candidate_sha256"],
                },
            },
            "agent_instruction": (
                "This candidate is not motion-usable. Submit its exact candidate ID "
                "and digest to review_and_activate_arm_base before relying on it."
            ),
        }
        visual_evidence = await self._register_candidate_evidence(
            self._inspection_snapshot()
        )
        if visual_evidence:
            result["visual_evidence"] = visual_evidence
        self.last_result = result
        return result

    def _inspection_snapshot(self) -> dict[str, Any]:
        snapshot = getattr(self.skill, "inspection_snapshot", None)
        if not callable(snapshot):
            return {}
        value = snapshot()
        return value if isinstance(value, dict) else {}

    async def _register_candidate_evidence(
        self, inspection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if self.visual_evidence_store is None:
            return []
        mask_candidates = inspection.get("mask_candidates")
        mask_review = (
            mask_candidates.get("review", {})
            if isinstance(mask_candidates, dict)
            else {}
        )
        retained_mask_ids = {
            str(value)
            for value in mask_review.get("accepted_candidate_ids", [])
        }
        foundation_pose = inspection.get("foundation_pose")
        fit_selection = (
            foundation_pose.get("selection", {})
            if isinstance(foundation_pose, dict)
            else {}
        )
        evidence: list[dict[str, Any]] = []
        for item in inspection.get("images", []):
            if not isinstance(item, dict):
                continue
            image_id = str(item.get("image_id") or "")
            if image_id.startswith("mask_candidate_"):
                selection = mask_review
                kind = "Independent SAM2 mask"
                candidate_id = image_id.removeprefix("mask_candidate_")
                selected = candidate_id in retained_mask_ids
                decision_label = "VLM retained" if selected else "VLM rejected"
            elif image_id == "mask_vote":
                selection = mask_review
                kind = "Pixel-voted mask"
                candidate_id = "vote"
                selected = True
                decision_label = "survivor vote"
            elif image_id == "mask_final_dilated":
                selection = mask_review
                kind = "Final mask"
                candidate_id = "dilated"
                selected = True
                decision_label = "used by all pose fits"
            elif image_id.startswith("fit_candidate_"):
                selection = fit_selection
                kind = "FoundationPose fit"
                candidate_id = image_id.removeprefix("fit_candidate_")
                selected = candidate_id == str(selection.get("candidate_id") or "")
                decision_label = "VLM selected" if selected else "not selected"
            elif image_id == "resolved_pose":
                orientation_selection = inspection.get("orientation_selection")
                orientation_record = (
                    orientation_selection
                    if isinstance(orientation_selection, dict)
                    else {}
                )
                selected_orientation_id = str(
                    orientation_record.get("selected_candidate_id") or "orientation"
                )
                matching_attempts = [
                    value
                    for value in orientation_record.get("attempts", [])
                    if isinstance(value, dict)
                    and str(value.get("candidate_id") or "")
                    == selected_orientation_id
                ]
                selected_attempt = max(
                    matching_attempts,
                    key=lambda value: float(value.get("confidence") or 0.0),
                    default={},
                )
                selection = {
                    **selected_attempt,
                    "candidate_id": selected_orientation_id,
                    "confidence": orientation_record.get("selected_confidence", 0.0),
                }
                kind = "Resolved arm-base pose"
                candidate_id = selected_orientation_id
                selected = bool(orientation_record.get("accepted"))
                decision_label = (
                    "90/180 correction accepted"
                    if selected
                    else "provisional correction retained"
                )
            else:
                continue
            path = Path(str(item.get("path") or "")).resolve()
            if not path.is_file():
                continue
            with Image.open(path) as image:
                width, height = image.size
            confidence_value = float(selection.get("confidence") or 0.0)
            confidence = (
                "high"
                if confidence_value >= 0.80
                else "medium"
                if confidence_value >= 0.60
                else "low"
            )
            title = f"{kind} candidate {candidate_id}"
            title += f" — {decision_label}"
            evidence.append(
                await self.visual_evidence_store.register_rgb(
                    image_bytes=path.read_bytes(),
                    media_type="image/png",
                    width=width,
                    height=height,
                    title=title,
                    annotations=[],
                    confidence=confidence,
                    model=str(selection.get("model") or "unknown"),
                    source_skill="locate_arm_base",
                )
            )
        return evidence

    async def review_and_activate(
        self,
        *,
        candidate_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        if self.activation_service is None:
            raise RuntimeError("arm-base activation is not configured")
        return await self.activation_service.review_and_activate(
            candidate_id=candidate_id,
            candidate_sha256=candidate_sha256,
        )

    def latest_activation_continuation(self) -> dict[str, Any] | None:
        if self.activation_service is None:
            return None
        return self.activation_service.latest_activation_continuation()

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        await asyncio.to_thread(self.skill.close)
