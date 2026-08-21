from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from locate_arm_base.skill import EffectorOrientationHintRequired, LocateArmBaseSkill

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

    async def run(
        self,
        *,
        rough_arm_base_positive_x_world: list[float] | None = None,
    ) -> dict[str, Any]:
        async with self._active_guard:
            task = self._active_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._execute_once(rough_arm_base_positive_x_world)
                )
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

    async def _execute_once(
        self,
        rough_arm_base_positive_x_world: list[float] | None,
    ) -> dict[str, Any]:
        extend_current_operation_hard_timeout(
            self.operation_hard_timeout_s,
            stage="LOCATE_ARM_BASE_EXTENDED_DEADLINE",
        )
        skill_request: dict[str, Any] = {"use_latest_camera": True}
        if rough_arm_base_positive_x_world is not None:
            skill_request["rough_arm_base_positive_x_world"] = list(
                rough_arm_base_positive_x_world
            )
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
            orientation_hint_required = isinstance(
                exc, EffectorOrientationHintRequired
            )
            result = {
                "schema": "midbrain.skill.locate_arm_base.failed_result",
                "schema_version": 1,
                "status": "FAILED",
                "run_id": inspection.get("run_id"),
                "failed_stage": inspection.get("failed_stage"),
                "error": str(exc),
                "workflow_complete": True,
                "terminal_failure": not orientation_hint_required,
                "retry_allowed": orientation_hint_required,
                "motion_usable": False,
                "physical_motion_submitted": False,
                "candidate_published": False,
                "agent_instruction": (
                    "The single effector-recognition VLM call could not identify "
                    "the active gripper/effector. Obtain a rough world-frame "
                    "direction pointing along arm-base +X, then call "
                    "locate_arm_base once more with that three-value direction in "
                    "rough_arm_base_positive_x_world. The rerun uses the supplied "
                    "world vector for the bounded local-Z correction and does "
                    "not repeat effector recognition."
                    if orientation_hint_required
                    else (
                        "Arm-base localization failed closed and this Agent task must "
                        "terminate without an automatic retry. Report the failure and "
                        "attached visual evidence to the operator; no calibration "
                        "candidate was published."
                    )
                ),
            }
            if orientation_hint_required:
                result["required_next_tool"] = {
                    "name": "locate_arm_base",
                    "required_argument": "rough_arm_base_positive_x_world",
                    "argument_semantics": (
                        "Three finite world-axis components pointing approximately "
                        "along arm-base +X; normalization is performed by the Skill."
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
        images = {
            str(item.get("image_id") or ""): item
            for item in inspection.get("images", [])
            if isinstance(item, dict) and item.get("image_id")
        }
        evidence: list[dict[str, Any]] = []

        async def register(
            image_id: str,
            *,
            title: str,
            annotations: list[dict[str, Any]],
            confidence: str,
            model: str,
        ) -> None:
            item = images.get(image_id)
            if not isinstance(item, dict):
                return
            path = Path(str(item.get("path") or "")).resolve()
            if not path.is_file():
                return
            with Image.open(path) as image:
                width, height = image.size
            evidence.append(
                await self.visual_evidence_store.register_rgb(
                    image_bytes=path.read_bytes(),
                    media_type="image/png",
                    width=width,
                    height=height,
                    title=title,
                    annotations=annotations,
                    confidence=confidence,
                    model=model,
                    source_skill="locate_arm_base",
                )
            )

        await register(
            "mask_candidates_multicolor",
            title="SAM2 mask ensemble — all candidates on one RGB frame",
            annotations=[],
            confidence="unknown",
            model="SAM2 independent mask ensemble",
        )

        orientation = inspection.get("orientation_selection")
        orientation = orientation if isinstance(orientation, dict) else {}
        attempts = [
            value
            for value in orientation.get("attempts", [])
            if isinstance(value, dict)
        ]
        if int(orientation.get("vlm_invocation_count") or 0) > 0 and attempts:
            selected_attempt = max(
                attempts,
                key=lambda value: float(value.get("confidence") or 0.0),
            )
            point_annotations = [
                {
                    "id": f"vlm-point-{index + 1}",
                    "type": "point",
                    "label": str(point.get("point_id") or f"VLM point {index + 1}"),
                    "x": float(point["x"]) / 1000.0,
                    "y": float(point["y"]) / 1000.0,
                    "applies_to_channels": ["rgb"],
                    "confidence": "unknown",
                    "default_visible": True,
                }
                for index, point in enumerate(
                    selected_attempt.get("points_yx_0_1000", [])
                )
                if isinstance(point, dict)
                and "x" in point
                and "y" in point
            ]
            confidence_value = float(selected_attempt.get("confidence") or 0.0)
            confidence = (
                "high"
                if confidence_value >= 0.80
                else "medium"
                if confidence_value >= 0.60
                else "low"
            )
            await register(
                "current_rgb",
                title="Single VLM effector observation points",
                annotations=point_annotations,
                confidence=confidence,
                model=str(selected_attempt.get("model") or "unknown"),
            )

        axis_layers = inspection.get("axis_vector_overlays")
        axis_layers = axis_layers if isinstance(axis_layers, dict) else {}
        axis_annotations: list[dict[str, Any]] = []
        for layer_name, default_visible, label_prefix in (
            ("final", True, "Final"),
            ("pre_rotation", False, "Pre-rotate"),
        ):
            for axis in axis_layers.get(layer_name, []):
                if not isinstance(axis, dict):
                    continue
                axis_name = str(axis.get("axis") or "axis")
                axis_annotations.append(
                    {
                        "id": f"{layer_name}-{axis_name.lower()}",
                        "type": "vector",
                        "label": f"{label_prefix} {axis_name}",
                        "x1": axis.get("x1"),
                        "y1": axis.get("y1"),
                        "x2": axis.get("x2"),
                        "y2": axis.get("y2"),
                        "color": axis.get("color"),
                        "applies_to_channels": ["rgb"],
                        "confidence": "high",
                        "default_visible": default_visible,
                    }
                )
        await register(
            "resolved_pose",
            title="Resolved pose — mask, CAD, and toggleable axis frames",
            annotations=axis_annotations,
            confidence="high" if orientation.get("accepted") else "medium",
            model=str(orientation.get("method") or "deterministic geometry"),
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
