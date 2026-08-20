from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
import time
from typing import Any
import uuid

import numpy as np
from PIL import Image, ImageDraw

from .arm_profile import ArmProfileRecord, ArmProfileStore
from .candidate_selection import (
    OpenAIResponsesFitCandidateSelector,
    OpenAIResponsesMaskCandidateReviewer,
    VisualCandidateSelector,
    VisualMaskReviewer,
)
from .clients import MidbrainClients
from .fit_candidates import render_fit_overlay
from .mask_candidates import (
    MaskCandidate,
    build_image_contact_sheet,
    build_voted_mask,
    create_mask_candidate,
)
from .math3d import (
    matrix4,
    transform_from_translation_quaternion,
    transform_record,
    x_rotation,
)
from .orientation import (
    OpenAIResponsesOrientationSelector,
    OpenAIResponsesArmBasePromptLocator,
    PromptLocator,
    OrientationSelection,
    OrientationSelector,
    build_contact_sheet,
    orientation_evidence_hash,
)
from .profile import ModelProfile, canonical_sha256, file_sha256, load_profile_payload


def _bounded_selection_decision(
    attempts: list[Any],
    *,
    minimum_confidence: float,
    consensus_confidence_floor: float,
) -> tuple[Any, str, bool]:
    if not attempts:
        raise ValueError("bounded selection requires at least one attempt")
    if not 0.0 <= consensus_confidence_floor <= minimum_confidence <= 1.0:
        raise ValueError(
            "selection confidence policy must satisfy "
            "0 <= consensus floor <= minimum confidence <= 1"
        )
    selected = max(attempts, key=lambda value: value.confidence)
    if attempts[0].confidence >= minimum_confidence:
        return attempts[0], "FIRST_ATTEMPT_CONFIDENCE", True
    if selected.confidence >= minimum_confidence:
        return selected, "RETRY_CONFIDENCE", True
    candidate_ids = {value.candidate_id for value in attempts}
    repeated_consensus = (
        len(attempts) >= 2
        and len(candidate_ids) == 1
        and all(
            value.confidence >= consensus_confidence_floor for value in attempts
        )
    )
    if repeated_consensus:
        return selected, "REPEATED_CANDIDATE_CONSENSUS", True
    qualified_attempts = [
        value
        for value in attempts
        if value.confidence >= consensus_confidence_floor
    ]
    qualified_counts: dict[str, int] = {}
    for value in qualified_attempts:
        qualified_counts[value.candidate_id] = (
            qualified_counts.get(value.candidate_id, 0) + 1
        )
    if qualified_counts:
        ordered_counts = sorted(qualified_counts.values(), reverse=True)
        winning_count = ordered_counts[0]
        runner_up_count = ordered_counts[1] if len(ordered_counts) > 1 else 0
        if winning_count >= 2 and winning_count > runner_up_count:
            winning_id = next(
                candidate_id
                for candidate_id, count in qualified_counts.items()
                if count == winning_count
            )
            winning_attempt = max(
                (
                    value
                    for value in qualified_attempts
                    if value.candidate_id == winning_id
                ),
                key=lambda value: value.confidence,
            )
            return (
                winning_attempt,
                "QUALIFIED_MAJORITY_CANDIDATE_CONSENSUS",
                True,
            )
    return selected, "REJECTED_INSUFFICIENT_CONFIDENCE_OR_CONSENSUS", False


def _vlm_backend_for_model(model: str) -> str:
    normalized = str(model or "").strip().lower()
    if not normalized:
        raise ValueError("locate_arm_base VLM model is empty")
    return "google.gemini" if normalized.startswith("gemini-") else "openai.responses"


class LocateArmBaseSkill:
    """One finite run from current evidence to a non-active calibration candidate."""

    def __init__(
        self,
        config: dict[str, Any],
        root: Path,
        *,
        clients: MidbrainClients | None = None,
        selector: OrientationSelector | None = None,
        prompt_locator: PromptLocator | None = None,
        mask_selector: VisualMaskReviewer | None = None,
        fit_selector: VisualCandidateSelector | None = None,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self.profile_store = ArmProfileStore(self.root, config)
        initial_profile = self.profile_store.load()
        self.profile = initial_profile.model_profile
        selection = config.get("arm_profile_selection")
        selection = selection if isinstance(selection, dict) else {}
        self.assembly_stream = str(
            selection.get("assembly_stream") or "robot_arm.assembly_state"
        )
        self.clients = clients or MidbrainClients(
            str(config["manager_url"]),
            str(config["fabric_url"]),
            str(config.get("foundation_pose_provider_id") or "perception.foundation_pose"),
            str(config.get("sam2_provider_id") or "perception.sam2_scene_tracker"),
        )
        vlm = config.get("vlm") if isinstance(config.get("vlm"), dict) else {}
        vlm_backend = str(vlm.get("backend") or "google.gemini").strip().lower()
        vlm_model = str(
            vlm.get("model") or "gemini-robotics-er-2-preview"
        ).strip()
        self._vlm_config = vlm
        self._default_vlm_backend = vlm_backend
        self._default_vlm_model = vlm_model
        self._selector_override = selector
        self._prompt_locator_override = prompt_locator
        self._mask_selector_override = mask_selector
        self._fit_selector_override = fit_selector
        self.vlm_backend = ""
        self.vlm_model = ""
        self.vlm_selection_source = "SKILL_DEFAULT"
        self.selector = selector
        self.prompt_locator = prompt_locator
        self.mask_selector = mask_selector
        self.fit_selector = fit_selector
        self._configure_vlm_route(vlm_backend, vlm_model)
        artifact_root = Path(
            str(config.get("artifact_root") or self.root / "skills" / "locate_arm_base")
        )
        if not artifact_root.is_absolute():
            artifact_root = self.root / artifact_root
        self.run_root = artifact_root.resolve() / "run"
        self.candidate_root = artifact_root.resolve() / "config" / "calibrations"
        self._inspection_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._inspection: dict[str, Any] | None = None
        self._inspection_path: Path | None = None
        self._inspection_mtime_ns = 0
        self._refresh_latest_inspection()

    def _configure_vlm_route(self, backend: str, model: str) -> None:
        normalized_backend = str(backend or "").strip().lower()
        normalized_model = str(model or "").strip()
        if normalized_backend not in {"google.gemini", "openai.responses"}:
            raise ValueError(
                f"unsupported locate_arm_base VLM backend: {normalized_backend!r}"
            )
        if not normalized_model:
            raise ValueError("locate_arm_base VLM model is empty")
        if (
            self.vlm_backend == normalized_backend
            and self.vlm_model == normalized_model
        ):
            return

        timeout_s = float(self._vlm_config.get("timeout_s") or 60.0)
        reasoning_effort = str(
            self._vlm_config.get("reasoning_effort") or "low"
        )

        def replace_client(attribute: str, override: Any, factory: Any) -> None:
            if override is not None:
                setattr(self, attribute, override)
                return
            previous = getattr(self, attribute, None)
            close = getattr(previous, "close", None)
            if callable(close):
                close()
            setattr(
                self,
                attribute,
                factory(
                    model=normalized_model,
                    timeout_s=timeout_s,
                    reasoning_effort=reasoning_effort,
                    backend=normalized_backend,
                ),
            )

        replace_client(
            "selector",
            self._selector_override,
            OpenAIResponsesOrientationSelector,
        )
        replace_client(
            "prompt_locator",
            self._prompt_locator_override,
            OpenAIResponsesArmBasePromptLocator,
        )
        replace_client(
            "mask_selector",
            self._mask_selector_override,
            OpenAIResponsesMaskCandidateReviewer,
        )
        replace_client(
            "fit_selector",
            self._fit_selector_override,
            OpenAIResponsesFitCandidateSelector,
        )
        self.vlm_backend = normalized_backend
        self.vlm_model = normalized_model

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError(
                "LOCATE_ARM_BASE_ALREADY_RUNNING: the previous visual pipeline is "
                "still active; wait for it to finish before starting another run"
            )
        attempt_started = time.monotonic()
        try:
            try:
                return self._run_once(request)
            except Exception as exc:
                self._fail_inspection(
                    str(exc), elapsed_ms=(time.monotonic() - attempt_started) * 1000.0
                )
                raise
        finally:
            self._run_lock.release()

    def _run_once(self, request: dict[str, Any]) -> dict[str, Any]:
        requested_vlm_model = str(
            request.get("vlm_model") or self._default_vlm_model
        ).strip()
        requested_vlm_backend = str(
            request.get("vlm_backend")
            or (
                self._default_vlm_backend
                if requested_vlm_model == self._default_vlm_model
                else _vlm_backend_for_model(requested_vlm_model)
            )
        ).strip().lower()
        self._configure_vlm_route(requested_vlm_backend, requested_vlm_model)
        self.vlm_selection_source = str(
            request.get("vlm_selection_source")
            or ("REQUEST_OVERRIDE" if request.get("vlm_model") else "SKILL_DEFAULT")
        ).strip()
        run_id = str(uuid.uuid4())
        run_dir = self.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        self._begin_attempt_inspection(run_id, run_dir)
        record, profile, binding = self._resolve_active_profile()
        profile = self._snapshot_profile_assets(record, profile, run_dir)
        self.profile = profile
        self._begin_inspection(run_id, run_dir, record, profile, binding)
        started = time.monotonic()
        capture = self._capture(request, run_dir)
        depth_preview = self._write_depth_preview(
            Path(capture["depth_npy_path"]), run_dir / "capture" / "depth_preview.png"
        )
        self._update_inspection(
            stage="RGBD_CAPTURED",
            foundation_pose={
                "cad_filename": profile.mesh_path.name,
                "cad_path": str(profile.mesh_path),
                "cad_sha256": profile.mesh_sha256,
                "cad_scale_to_m": profile.mesh_scale_to_m,
                "rgb_path": str(capture["rgb_path"]),
                "depth_npy_path": str(capture["depth_npy_path"]),
                "depth_preview_path": str(depth_preview),
            },
        )
        self._add_image(
            "current_rgb",
            "Current RGB",
            Path(capture["rgb_path"]),
            ["VLM_SEED_LOCALIZATION", "FOUNDATIONPOSE"],
        )
        self._add_image(
            "depth_preview",
            "Aligned depth preview",
            depth_preview,
            ["DERIVED_PREVIEW_OF_FOUNDATIONPOSE_DEPTH"],
        )
        observed_at_us = int(capture["observed_at_us"])
        camera_frame = str(capture["camera_frame"])
        diagnostic_only = bool(request.get("diagnostic_only", False))
        mask_config = self.config.get("mask_ensemble")
        mask_config = mask_config if isinstance(mask_config, dict) else {}
        requested_mask_count = request.get(
            "mask_attempt_count", request.get("mask_candidate_count")
        )
        mask_attempt_count = int(
            mask_config.get("attempt_count", 2)
            if requested_mask_count is None
            else requested_mask_count
        )
        if not 1 <= mask_attempt_count <= 8:
            raise ValueError("mask_attempt_count must be between 1 and 8")
        final_dilation_radius_px = int(
            mask_config.get("final_dilation_radius_px", 4)
        )
        if not 0 <= final_dilation_radius_px <= 64:
            raise ValueError(
                "mask_ensemble.final_dilation_radius_px must be between 0 and 64"
            )
        fit_config = self.config.get("fit_candidates")
        fit_config = fit_config if isinstance(fit_config, dict) else {}
        requested_fit_count = request.get("fit_candidate_count")
        fit_candidate_count = int(
            fit_config.get("candidate_count", 2)
            if requested_fit_count is None
            else requested_fit_count
        )
        if not 1 <= fit_candidate_count <= 8:
            raise ValueError("fit_candidate_count must be between 1 and 8")
        minimum_up_dot = float(
            self.config.get("minimum_arm_base_up_dot_world", 0.5)
        )
        if not 0.0 <= minimum_up_dot <= 1.0:
            raise ValueError(
                "minimum_arm_base_up_dot_world must be between 0 and 1"
            )
        early_world_from_camera: np.ndarray | None = None
        early_world_axis_proof: dict[str, Any] | None = None
        if not diagnostic_only:
            early_world_from_camera, early_world_axis_proof = self._world_from_camera(
                request, camera_frame, observed_at_us
            )
            self._update_inspection(
                stage="TIMESTAMPED_WORLD_AXIS_BOUND",
                world_axis=early_world_axis_proof,
            )
        mask_stage_started = time.monotonic()
        mask_candidates, mask_acquisition = self._snapshot_masks(
            request,
            run_dir,
            capture,
            attempt_count=mask_attempt_count,
        )
        mask_source_elapsed_ms = (time.monotonic() - mask_stage_started) * 1000.0
        for candidate in mask_candidates:
            seed_overlay_path = candidate.prompt.get("seed_overlay_path")
            if seed_overlay_path:
                self._add_image(
                    f"vlm_seed_{candidate.candidate_id}",
                    f"VLM seed for {candidate.candidate_id}",
                    Path(str(seed_overlay_path)),
                    ["DERIVED_PREVIEW_OF_VLM_OUTPUT", "SAM2_PROMPT"],
                )
            self._add_image(
                f"mask_candidate_{candidate.candidate_id}",
                f"Independent SAM2 mask {candidate.candidate_id}",
                candidate.overlay_path,
                ["VLM_MASK_CANDIDATE_REVIEW", "AGENT_VISUAL_EVIDENCE"],
            )
        mask_sheet = build_image_contact_sheet(
            tuple(candidate.overlay_path for candidate in mask_candidates),
            run_dir / "mask_candidates.png",
        )
        self._add_image(
            "mask_candidates_contact_sheet",
            "Independent SAM2 masks supplied to VLM review",
            mask_sheet,
            ["VLM_MASK_CANDIDATE_REVIEW"],
        )
        mask_review_started = time.monotonic()
        mask_candidate_ids = tuple(
            candidate.candidate_id for candidate in mask_candidates
        )
        if mask_acquisition["method"] == "CALLER_PROVIDED_MASK":
            mask_review_record = {
                "accepted_candidate_ids": list(mask_candidate_ids),
                "rejected_candidate_ids": [],
                "confidence": 1.0,
                "minimum_confidence": 1.0,
                "accepted": True,
                "rationale": "Caller supplied the replay mask explicitly.",
                "model": "CALLER_PROVIDED_MASK",
                "response_id": None,
                "structured_output_attempt_count": 0,
            }
        else:
            mask_review = self.mask_selector.review(
                self.profile.segmentation_reference_paths,
                mask_sheet,
                mask_candidate_ids,
            )
            minimum_mask_confidence = float(
                mask_config.get("minimum_review_confidence", 0.60)
            )
            accepted_set = set(mask_review.accepted_candidate_ids)
            mask_review_record = {
                "accepted_candidate_ids": list(mask_review.accepted_candidate_ids),
                "rejected_candidate_ids": [
                    candidate_id
                    for candidate_id in mask_candidate_ids
                    if candidate_id not in accepted_set
                ],
                "confidence": mask_review.confidence,
                "minimum_confidence": minimum_mask_confidence,
                "accepted": mask_review.confidence >= minimum_mask_confidence,
                "rationale": mask_review.rationale,
                "model": mask_review.model,
                "response_id": mask_review.response_id,
                "structured_output_attempt_count": mask_review.attempt_count,
            }
        mask_review_elapsed_ms = (time.monotonic() - mask_review_started) * 1000.0
        if not mask_review_record["accepted"]:
            raise RuntimeError(
                "mask review confidence "
                f"{float(mask_review_record['confidence']):.3f} is below required "
                f"{float(mask_review_record['minimum_confidence']):.3f}"
            )
        vote_started = time.monotonic()
        voted_mask = build_voted_mask(
            candidates=mask_candidates,
            accepted_candidate_ids=tuple(mask_review_record["accepted_candidate_ids"]),
            rgb_path=Path(capture["rgb_path"]),
            output_dir=run_dir / "mask_vote",
            dilation_radius_px=final_dilation_radius_px,
        )
        mask_vote_elapsed_ms = (time.monotonic() - vote_started) * 1000.0
        self._add_image(
            "mask_vote",
            "Pixel-voted mask before dilation",
            voted_mask.voted_overlay_path,
            ["MASK_PIXEL_VOTE", "AGENT_VISUAL_EVIDENCE"],
        )
        self._add_image(
            "mask_final_dilated",
            "Final voted mask after one dilation",
            voted_mask.final_overlay_path,
            ["FOUNDATIONPOSE", "AGENT_VISUAL_EVIDENCE"],
        )
        self._update_inspection(
            stage="MASK_ENSEMBLE_VOTED_AND_DILATED",
            mask_acquisition={
                **mask_acquisition,
                "source_elapsed_ms": mask_source_elapsed_ms,
            },
            mask_candidates={
                "configured_count": mask_attempt_count,
                "produced_count": len(mask_candidates),
                "generation_policy": "INDEPENDENT_VLM_POINT_TO_SAM2_MASKS",
                "vlm_seed_guidance": self.profile.vlm_seed_guidance,
                "contact_sheet_path": str(mask_sheet),
                "candidates": [candidate.record() for candidate in mask_candidates],
                "review": mask_review_record,
                "review_elapsed_ms": mask_review_elapsed_ms,
                "vote": voted_mask.record(),
                "vote_elapsed_ms": mask_vote_elapsed_ms,
            },
        )
        selected_mask_id = voted_mask.mask_id
        selected_mask_path = voted_mask.final_mask_path
        self.clients.ensure_foundation_pose_hot()
        fit_by_id: dict[str, dict[str, Any]] = {}
        ordered_fits: list[dict[str, Any]] = []
        fitting_started = time.monotonic()
        for fit_index in range(fit_candidate_count):
            fit_id = f"fit_{fit_index + 1}"
            pose_request = {
                "request_id": f"{run_id}-{fit_id}",
                "observed_at_us": observed_at_us,
                "camera_frame": camera_frame,
                "camera_intrinsics": capture["camera_intrinsics"],
                "mesh": {
                    "path": str(self.profile.mesh_path),
                    "sha256": self.profile.mesh_sha256,
                    "scale_to_m": self.profile.mesh_scale_to_m,
                },
                "evidence": {
                    "rgb_path": str(capture["rgb_path"]),
                    "depth_npy_path": str(capture["depth_npy_path"]),
                    "mask": {"path": str(selected_mask_path)},
                },
            }
            fit_started = time.monotonic()
            measurement = self.clients.estimate_pose(pose_request)
            fit_elapsed_ms = (time.monotonic() - fit_started) * 1000.0
            score = float(
                measurement.get("quality", {}).get("score", float("nan"))
            )
            if not np.isfinite(score):
                raise RuntimeError(
                    f"FoundationPose returned a non-finite ranking score for {fit_id}"
                )
            camera_from_mesh = matrix4(
                measurement["camera_from_centered_mesh"],
                "camera_from_centered_mesh",
            )
            arm_base_positive_z_dot_world_raw: float | None = None
            arm_base_positive_z_dot_world: float | None = None
            physically_eligible = True
            physical_rejection_reason: str | None = None
            upright_correction = np.eye(4, dtype=np.float64)
            upright_normalization_degrees = 0
            if early_world_from_camera is not None:
                unresolved_world_from_arm_base = (
                    early_world_from_camera
                    @ camera_from_mesh
                    @ self.profile.centered_mesh_from_arm_base
                )
                arm_base_positive_z_dot_world_raw = float(
                    unresolved_world_from_arm_base[2, 2]
                )
                if arm_base_positive_z_dot_world_raw < 0.0:
                    upright_correction = x_rotation(180.0)
                    upright_normalization_degrees = 180
                normalized_world_from_arm_base = (
                    early_world_from_camera
                    @ camera_from_mesh
                    @ upright_correction
                    @ self.profile.centered_mesh_from_arm_base
                )
                arm_base_positive_z_dot_world = float(
                    normalized_world_from_arm_base[2, 2]
                )
                physically_eligible = (
                    arm_base_positive_z_dot_world >= minimum_up_dot
                )
                if not physically_eligible:
                    physical_rejection_reason = (
                        "ARM_BASE_POSITIVE_Z_INSUFFICIENTLY_ALIGNED_WITH_WORLD"
                    )
            native_elapsed_ms = float(
                measurement.get("timing", {}).get("native_elapsed_ms", fit_elapsed_ms)
            )
            overlay_path = render_fit_overlay(
                rgb_path=Path(capture["rgb_path"]),
                mask_path=selected_mask_path,
                mesh_path=self.profile.mesh_path,
                mesh_scale_to_m=self.profile.mesh_scale_to_m,
                camera_from_centered_mesh=camera_from_mesh,
                camera_intrinsics=capture["camera_intrinsics"],
                output_path=run_dir / "fit_candidates" / f"{fit_id}.png",
                candidate_id=fit_id,
                dilation_radius_px=voted_mask.dilation_radius_px,
                label_details="geometry-only review; native score withheld",
            )
            fit_record = {
                "candidate_id": fit_id,
                "source_mask_candidate_id": selected_mask_id,
                "dilation_radius_px": voted_mask.dilation_radius_px,
                "mask_path": str(selected_mask_path),
                "overlay_path": str(overlay_path),
                "request": pose_request,
                "measurement_id": measurement.get("measurement_id"),
                "measurement": measurement,
                "ranking_score_raw": score,
                "score_semantics": "AUDIT_ONLY_NOT_SELECTION_INPUT",
                "request_elapsed_ms": fit_elapsed_ms,
                "physically_eligible": physically_eligible,
                "physical_rejection_reason": physical_rejection_reason,
                "arm_base_positive_z_dot_world_raw": (
                    arm_base_positive_z_dot_world_raw
                ),
                "arm_base_positive_z_dot_world": arm_base_positive_z_dot_world,
                "upright_normalization_axis": "X",
                "upright_normalization_degrees": upright_normalization_degrees,
                "upright_correction": transform_record(upright_correction),
            }
            fit_by_id[fit_id] = fit_record
            ordered_fits.append(fit_record)
        fitting_elapsed_ms = (time.monotonic() - fitting_started) * 1000.0
        for fit in ordered_fits:
            self._add_image(
                f"fit_candidate_{fit['candidate_id']}",
                f"FoundationPose fit {fit['candidate_id']}",
                Path(fit["overlay_path"]),
                ["VLM_FIT_CANDIDATE_SELECTION", "AGENT_VISUAL_EVIDENCE"],
            )
        fit_sheet = build_image_contact_sheet(
            tuple(Path(fit["overlay_path"]) for fit in ordered_fits),
            run_dir / "fit_candidates.png",
        )
        self._add_image(
            "fit_candidates_contact_sheet",
            "FoundationPose fits supplied to VLM",
            fit_sheet,
            ["VLM_FIT_CANDIDATE_SELECTION"],
        )
        minimum_fit_confidence = float(
            fit_config.get("minimum_selection_confidence", 0.55)
        )
        fit_consensus_floor = float(
            fit_config.get("minimum_consensus_confidence", 0.40)
        )
        fit_selection_started = time.monotonic()
        fit_candidate_ids = tuple(
            fit["candidate_id"]
            for fit in ordered_fits
            if bool(fit["physically_eligible"])
        )
        if not fit_candidate_ids:
            self._update_inspection(
                stage="FIT_CANDIDATES_PHYSICALLY_REJECTED",
                foundation_pose={
                    **self.inspection_snapshot().get("foundation_pose", {}),
                    "candidate_count": len(ordered_fits),
                    "fits": ordered_fits,
                    "contact_sheet_path": str(fit_sheet),
                    "fitting_elapsed_ms": fitting_elapsed_ms,
                    "fit_policy": "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK",
                    "selected_mask_candidate_id": selected_mask_id,
                    "all_fits_use_selected_mask": True,
                    "physically_eligible_candidate_ids": [],
                },
            )
            raise RuntimeError(
                "all FoundationPose fits leave arm-base +Z insufficiently aligned "
                "with world +Z after bounded upright normalization; no pose can "
                "safely establish the arm-base axis"
            )
        fit_attempts = [
            self.fit_selector.select(
                self.profile.segmentation_reference_paths,
                fit_sheet,
                fit_candidate_ids,
            )
        ]
        if fit_attempts[0].confidence < minimum_fit_confidence:
            fit_attempts.append(
                self.fit_selector.select(
                    self.profile.segmentation_reference_paths,
                    fit_sheet,
                    fit_candidate_ids,
                )
            )
        _, _, fit_provisionally_accepted = _bounded_selection_decision(
            fit_attempts,
            minimum_confidence=minimum_fit_confidence,
            consensus_confidence_floor=fit_consensus_floor,
        )
        if not fit_provisionally_accepted and len(fit_attempts) < 3:
            fit_attempts.append(
                self.fit_selector.select(
                    self.profile.segmentation_reference_paths,
                    fit_sheet,
                    fit_candidate_ids,
                )
            )
        fit_selection, fit_decision_basis, fit_accepted = (
            _bounded_selection_decision(
                fit_attempts,
                minimum_confidence=minimum_fit_confidence,
                consensus_confidence_floor=fit_consensus_floor,
            )
        )
        fit_selection_elapsed_ms = (
            time.monotonic() - fit_selection_started
        ) * 1000.0
        fit_attempt_records = [
            {
                "candidate_id": value.candidate_id,
                "confidence": value.confidence,
                "rationale": value.rationale,
                "model": value.model,
                "response_id": value.response_id,
                "structured_output_attempt_count": value.attempt_count,
            }
            for value in fit_attempts
        ]
        fit_selection_record = {
            "candidate_id": fit_selection.candidate_id,
            "confidence": fit_selection.confidence,
            "minimum_confidence": minimum_fit_confidence,
            "minimum_consensus_confidence": fit_consensus_floor,
            "accepted": fit_accepted,
            "decision_basis": fit_decision_basis,
            "attempts": fit_attempt_records,
            "rationale": fit_selection.rationale,
            "model": fit_selection.model,
            "response_id": fit_selection.response_id,
            "structured_output_attempt_count": fit_selection.attempt_count,
        }
        self._update_inspection(
            stage="FIT_CANDIDATE_SELECTED",
            foundation_pose={
                **self.inspection_snapshot().get("foundation_pose", {}),
                "candidate_count": len(ordered_fits),
                "fits": ordered_fits,
                "contact_sheet_path": str(fit_sheet),
                "fitting_elapsed_ms": fitting_elapsed_ms,
                "fit_policy": "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK",
                "selected_mask_candidate_id": selected_mask_id,
                "all_fits_use_selected_mask": True,
                "physically_eligible_candidate_ids": list(fit_candidate_ids),
                "selection": fit_selection_record,
                "selection_elapsed_ms": fit_selection_elapsed_ms,
            },
        )
        if not fit_accepted:
            attempt_summary = ", ".join(
                f"{value.candidate_id}:{value.confidence:.3f}"
                for value in fit_attempts
            )
            raise RuntimeError(
                "fit candidate selection lacked confidence or qualified "
                f"consensus after attempts [{attempt_summary}]; required "
                f"confidence {minimum_fit_confidence:.3f} or same-candidate "
                f"majority consensus at {fit_consensus_floor:.3f}"
            )
        selected_fit = fit_by_id[fit_selection.candidate_id]
        mask_path = selected_mask_path
        pose_request = selected_fit["request"]
        measurement = selected_fit["measurement"]
        score = float(selected_fit["ranking_score_raw"])
        camera_from_mesh = matrix4(
            measurement["camera_from_centered_mesh"], "camera_from_centered_mesh"
        )
        upright_correction = matrix4(
            selected_fit["upright_correction"]["matrix"],
            "upright_correction",
        )
        upright_normalization = {
            "status": (
                "APPLIED_LOCAL_X_180"
                if int(selected_fit["upright_normalization_degrees"]) == 180
                else "NOT_REQUIRED"
            ),
            "method": "WORLD_UP_BOUNDED_LOCAL_X_HALF_TURN",
            "axis": "X",
            "degrees": int(selected_fit["upright_normalization_degrees"]),
            "minimum_arm_base_up_dot_world": minimum_up_dot,
            "raw_arm_base_positive_z_dot_world": selected_fit[
                "arm_base_positive_z_dot_world_raw"
            ],
            "corrected_arm_base_positive_z_dot_world": selected_fit[
                "arm_base_positive_z_dot_world"
            ],
            "correction": transform_record(upright_correction),
        }
        self._update_inspection(
            foundation_pose={
                **self.inspection_snapshot().get("foundation_pose", {}),
                "selected_candidate_id": fit_selection.candidate_id,
                "mask_vlm_accepted_candidate_ids": mask_review_record[
                    "accepted_candidate_ids"
                ],
                "all_fits_use_selected_mask": True,
                "mask_path": str(mask_path),
                "request": pose_request,
                "measurement_id": measurement.get("measurement_id"),
                "measurement": measurement,
            },
        )
        contact_sheet = build_contact_sheet(
            Path(capture["rgb_path"]),
            mask_path,
            camera_from_mesh,
            capture["camera_intrinsics"],
            self.profile,
            run_dir / "orientation_candidates.png",
            pre_orientation_correction=upright_correction,
        )
        self._add_image(
            "orientation_candidates",
            "World-up-normalized bounded orientation candidates",
            contact_sheet,
            ["VLM_ORIENTATION_SELECTION"],
        )
        self._update_inspection(stage="ORIENTATION_CANDIDATES_RENDERED")
        orientation_selection_started = time.monotonic()
        minimum_confidence = float(self.config.get("minimum_vlm_confidence", 0.72))
        orientation_consensus_floor = float(
            self.config.get("minimum_vlm_consensus_confidence", 0.55)
        )
        orientation_attempts = [
            self.selector.select(
                self.profile.orientation_reference_paths,
                contact_sheet,
                self.profile.candidates,
            )
        ]
        if orientation_attempts[0].confidence < minimum_confidence:
            orientation_attempts.append(
                self.selector.select(
                    self.profile.orientation_reference_paths,
                    contact_sheet,
                    self.profile.candidates,
                )
            )
        _, _, orientation_provisionally_accepted = _bounded_selection_decision(
            orientation_attempts,
            minimum_confidence=minimum_confidence,
            consensus_confidence_floor=orientation_consensus_floor,
        )
        if not orientation_provisionally_accepted and len(orientation_attempts) < 3:
            orientation_attempts.append(
                self.selector.select(
                    self.profile.orientation_reference_paths,
                    contact_sheet,
                    self.profile.candidates,
                )
            )
        selection, orientation_decision_basis, orientation_accepted = (
            _bounded_selection_decision(
                orientation_attempts,
                minimum_confidence=minimum_confidence,
                consensus_confidence_floor=orientation_consensus_floor,
            )
        )
        orientation_selection_elapsed_ms = (
            time.monotonic() - orientation_selection_started
        ) * 1000.0
        orientation_attempt_records = [
            {
                "candidate_id": value.candidate_id,
                "confidence": value.confidence,
                "rationale": value.rationale,
                "model": value.model,
                "response_id": value.response_id,
                "structured_output_attempt_count": value.attempt_count,
            }
            for value in orientation_attempts
        ]
        self._update_inspection(
            orientation_selection={
                "selected_candidate_id": selection.candidate_id,
                "selected_confidence": selection.confidence,
                "minimum_confidence": minimum_confidence,
                "minimum_consensus_confidence": orientation_consensus_floor,
                "accepted": orientation_accepted,
                "decision_basis": orientation_decision_basis,
                "attempts": orientation_attempt_records,
                "elapsed_ms": orientation_selection_elapsed_ms,
            }
        )
        candidate_definition = next(
            (value for value in self.profile.candidates if value.candidate_id == selection.candidate_id),
            None,
        )
        if candidate_definition is None:
            raise RuntimeError("orientation selector returned an unprofiled candidate")
        combined_orientation_correction = (
            upright_correction @ candidate_definition.matrix
        )
        camera_from_arm_base = (
            camera_from_mesh
            @ combined_orientation_correction
            @ self.profile.centered_mesh_from_arm_base
        )
        resolved_pose_path = render_fit_overlay(
            rgb_path=Path(capture["rgb_path"]),
            mask_path=mask_path,
            mesh_path=self.profile.mesh_path,
            mesh_scale_to_m=self.profile.mesh_scale_to_m,
            # Preserve the measured CAD projection; only the robot-semantic axis
            # frame changes when resolving a symmetric mesh orientation.
            camera_from_centered_mesh=camera_from_mesh,
            camera_intrinsics=capture["camera_intrinsics"],
            output_path=run_dir / "resolved_pose.png",
            candidate_id="resolved_pose",
            dilation_radius_px=voted_mask.dilation_radius_px,
            label_details=(
                f"selected {fit_selection.candidate_id}; local-Z "
                f"{candidate_definition.degrees}deg; upright local-X "
                f"{selected_fit['upright_normalization_degrees']}deg; "
                f"VLM {'accepted' if orientation_accepted else 'provisional-rejected'}"
            ),
            camera_from_axis_frame=camera_from_arm_base,
        )
        self._add_image(
            "resolved_pose",
            "Selected pose after bounded orientation correction",
            resolved_pose_path,
            ["AGENT_VISUAL_EVIDENCE", "FINAL_ORIENTATION_INSPECTION"],
        )
        self._update_inspection(
            stage=(
                "ORIENTATION_SELECTED"
                if orientation_accepted
                else "ORIENTATION_PROVISIONAL_RENDERED"
            ),
            resolved_pose_path=str(resolved_pose_path),
            resolved_pose_selection_accepted=orientation_accepted,
        )
        if not orientation_accepted:
            attempt_summary = ", ".join(
                f"{value.candidate_id}:{value.confidence:.3f}"
                for value in orientation_attempts
            )
            raise RuntimeError(
                "orientation selection lacked confidence or qualified consensus "
                f"after attempts [{attempt_summary}]; required confidence "
                f"{minimum_confidence:.3f} or same-candidate majority consensus at "
                f"{orientation_consensus_floor:.3f}"
            )
        orientation_proof = self._orientation_proof(
            selection,
            candidate_definition,
            contact_sheet,
            minimum_confidence,
            upright_normalization,
        )
        orientation_proof["selection_attempts"] = orientation_attempt_records
        orientation_proof["selection_decision_basis"] = orientation_decision_basis
        orientation_proof["minimum_consensus_confidence"] = (
            orientation_consensus_floor
        )
        if orientation_decision_basis in {
            "REPEATED_CANDIDATE_CONSENSUS",
            "QUALIFIED_MAJORITY_CANDIDATE_CONSENSUS",
        }:
            orientation_proof["status"] = "PASSED_WITH_WARNINGS"
        if diagnostic_only:
            diagnostic = {
                "schema": "midbrain.skill.locate_arm_base.visual_diagnostic",
                "schema_version": 1,
                "status": "VISUAL_PIPELINE_COMPLETED",
                "run_id": run_id,
                "observed_at_us": observed_at_us,
                "camera_frame": camera_frame,
                "motion_usable": False,
                "candidate_published": False,
                "foundation_pose": {
                    "measurement_id": measurement.get("measurement_id"),
                    "selected_fit_candidate_id": fit_selection.candidate_id,
                    "selected_mask_candidate_id": selected_mask_id,
                    "fit_policy": "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK",
                    "candidate_count": len(ordered_fits),
                    "ranking_score_raw": score,
                    "score_semantics": "AUDIT_ONLY_NOT_SELECTION_INPUT",
                    "camera_from_centered_mesh": transform_record(camera_from_mesh),
                },
                "mask_review": mask_review_record,
                "mask_vote": voted_mask.record(),
                "fit_selection": fit_selection_record,
                "orientation_resolution": orientation_proof,
                "camera_from_arm_base": transform_record(camera_from_arm_base),
                "message": (
                    "The camera-frame visual pipeline completed. Diagnostic mode "
                    "does not query the world axis or publish a calibration candidate."
                ),
                "timing": {
                    "mask_source_elapsed_ms": mask_source_elapsed_ms,
                    "mask_review_elapsed_ms": mask_review_elapsed_ms,
                    "mask_vote_elapsed_ms": mask_vote_elapsed_ms,
                    "foundation_pose_candidates_elapsed_ms": fitting_elapsed_ms,
                    "fit_selection_elapsed_ms": fit_selection_elapsed_ms,
                    "orientation_selection_elapsed_ms": orientation_selection_elapsed_ms,
                    "skill_elapsed_ms": (time.monotonic() - started) * 1000.0,
                },
            }
            self._update_inspection(
                status="COMPLETED",
                stage="VISUAL_DIAGNOSTIC_COMPLETED",
                candidate_id=None,
                selected_orientation={
                    "candidate_id": selection.candidate_id,
                    "confidence": selection.confidence,
                    "rationale": selection.rationale,
                },
                diagnostic_result=diagnostic,
            )
            return diagnostic
        world_from_camera, world_axis_proof = self._world_from_camera(
            request, camera_frame, observed_at_us
        )
        if early_world_from_camera is None or early_world_axis_proof is None:
            raise RuntimeError("timestamped world-axis binding is unavailable")
        if (
            str(world_axis_proof.get("world_frame") or "")
            != str(early_world_axis_proof.get("world_frame") or "")
            or str(world_axis_proof.get("session_epoch") or "")
            != str(early_world_axis_proof.get("session_epoch") or "")
        ):
            raise RuntimeError(
                "WORLD_AXIS_EPOCH_CHANGED: Local VIO reset during arm-base localization"
            )
        if not np.allclose(
            world_from_camera,
            early_world_from_camera,
            rtol=0.0,
            atol=1e-9,
        ):
            raise RuntimeError(
                "TIMESTAMPED_CAMERA_TRANSFORM_CHANGED: repeated transform query for "
                "the captured RGB-D timestamp returned different geometry"
            )
        world_from_arm_base = world_from_camera @ camera_from_arm_base
        candidate = {
            "schema": "midbrain.skill.locate_arm_base.calibration_candidate",
            "schema_version": 1,
            "candidate_id": str(uuid.uuid4()),
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at_us": time.time_ns() // 1000 + 86_400_000_000,
            "observed_at_us": observed_at_us,
            "parent_frame": str(world_axis_proof["world_frame"]),
            "child_frame": self.profile.semantic_frame,
            "camera_frame": camera_frame,
            "motion_usable": False,
            "review_state": "PENDING_REVIEW",
            "activation_owner": "RESOURCE_PROVIDER_MANAGER",
            "world_from_arm_base": transform_record(world_from_arm_base),
            "composition": {
                "world_from_camera": transform_record(world_from_camera),
                "camera_from_centered_mesh": transform_record(camera_from_mesh),
                "orientation_correction": transform_record(
                    combined_orientation_correction
                ),
                "upright_normalization_correction": transform_record(
                    upright_correction
                ),
                "local_z_orientation_correction": transform_record(
                    candidate_definition.matrix
                ),
                "centered_mesh_from_arm_base": transform_record(self.profile.centered_mesh_from_arm_base),
                "application_order": "world_from_camera @ camera_from_centered_mesh @ orientation_correction @ centered_mesh_from_arm_base",
            },
            "quality_provenance": {
                "foundation_pose": {
                    "measurement_id": measurement.get("measurement_id"),
                    "selected_fit_candidate_id": fit_selection.candidate_id,
                    "selected_mask_candidate_id": selected_mask_id,
                    "fit_policy": "REPEATED_INDEPENDENT_FITS_ON_VOTED_DILATED_MASK",
                    "ranking_score_raw": score,
                    "score_semantics": "AUDIT_ONLY_NOT_SELECTION_INPUT",
                    "hypothesis_count": measurement.get("quality", {}).get("hypothesis_count"),
                    "native_elapsed_ms": measurement.get("timing", {}).get("native_elapsed_ms"),
                    "provider": measurement.get("provenance"),
                    "candidate_count": len(ordered_fits),
                    "candidates": [
                        {
                            "candidate_id": fit["candidate_id"],
                            "source_mask_candidate_id": fit[
                                "source_mask_candidate_id"
                            ],
                            "dilation_radius_px": fit["dilation_radius_px"],
                            "measurement_id": fit["measurement_id"],
                            "ranking_score_raw": fit["ranking_score_raw"],
                            "request_elapsed_ms": fit["request_elapsed_ms"],
                            "native_elapsed_ms": fit["measurement"]
                            .get("timing", {})
                            .get("native_elapsed_ms"),
                            "physically_eligible": fit["physically_eligible"],
                            "physical_rejection_reason": fit[
                                "physical_rejection_reason"
                            ],
                            "arm_base_positive_z_dot_world_raw": fit[
                                "arm_base_positive_z_dot_world_raw"
                            ],
                            "arm_base_positive_z_dot_world": fit[
                                "arm_base_positive_z_dot_world"
                            ],
                            "upright_normalization_axis": fit[
                                "upright_normalization_axis"
                            ],
                            "upright_normalization_degrees": fit[
                                "upright_normalization_degrees"
                            ],
                            "upright_correction": fit["upright_correction"],
                        }
                        for fit in ordered_fits
                    ],
                },
                "mask_review": mask_review_record,
                "mask_vote": voted_mask.record(),
                "fit_selection": fit_selection_record,
                "orientation_resolution": orientation_proof,
                "world_axis": world_axis_proof,
                "model_profile": {
                    "profile_id": self.profile.profile_id,
                    "profile_sha256": self.profile.profile_sha256,
                    "mesh_sha256": self.profile.mesh_sha256,
                    "reference_set_sha256": self.profile.reference_set_sha256,
                },
                "source_evidence": {
                    "rgb_sha256": file_sha256(Path(capture["rgb_path"])),
                    "depth_sha256": file_sha256(Path(capture["depth_npy_path"])),
                    "mask_sha256": file_sha256(mask_path),
                    "contact_sheet_sha256": file_sha256(contact_sheet),
                    "source_observations": capture.get("source_observations"),
                    "mask_acquisition": {
                        **mask_acquisition,
                        "source_elapsed_ms": mask_source_elapsed_ms,
                    },
                    "mask_candidates": [candidate.record() for candidate in mask_candidates],
                    "mask_candidates_contact_sheet_sha256": file_sha256(mask_sheet),
                    "voted_mask_sha256": file_sha256(voted_mask.voted_mask_path),
                    "final_dilated_mask_sha256": file_sha256(
                        voted_mask.final_mask_path
                    ),
                    "fit_candidates_contact_sheet_sha256": file_sha256(fit_sheet),
                    "resolved_pose_sha256": file_sha256(resolved_pose_path),
                },
            },
            "timing": {
                "mask_source_elapsed_ms": mask_source_elapsed_ms,
                "mask_review_elapsed_ms": mask_review_elapsed_ms,
                "mask_vote_elapsed_ms": mask_vote_elapsed_ms,
                "foundation_pose_candidates_elapsed_ms": fitting_elapsed_ms,
                "fit_selection_elapsed_ms": fit_selection_elapsed_ms,
                "orientation_selection_elapsed_ms": orientation_selection_elapsed_ms,
                "skill_elapsed_ms": (time.monotonic() - started) * 1000.0,
            },
        }
        candidate["workcell_calibration_revision"] = candidate["candidate_id"]
        candidate["frame_contract"] = {
            "world_frame": candidate["parent_frame"],
            "camera_frame": camera_frame,
            "arm_base_frame": candidate["child_frame"],
            "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "camera_optical_convention_id": "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1",
            "transform_semantics": "PARENT_FROM_CHILD",
            "legacy_candidate_compatibility": "REJECT",
        }
        candidate["camera_provenance"] = self._camera_provenance(capture)
        candidate["candidate_sha256"] = canonical_sha256(candidate)
        self.candidate_root.mkdir(parents=True, exist_ok=True)
        candidate_path = self.candidate_root / f"{candidate['candidate_id']}.json"
        candidate_path.write_text(
            json.dumps(candidate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        candidate["candidate_path"] = str(candidate_path)
        self.clients.publish_candidate(candidate)
        self._update_inspection(
            status="COMPLETED",
            stage="CANDIDATE_PUBLISHED",
            candidate_id=candidate["candidate_id"],
            candidate_path=str(candidate_path),
            selected_orientation={
                "candidate_id": selection.candidate_id,
                "confidence": selection.confidence,
                "rationale": selection.rationale,
            },
        )
        return candidate

    @staticmethod
    def _camera_provenance(capture: dict[str, Any]) -> dict[str, Any]:
        sources = capture.get("source_observations")
        sources = sources if isinstance(sources, dict) else {}
        bundle = sources.get("bundle")
        bundle = bundle if isinstance(bundle, dict) else {}
        calibration = sources.get("calibration")
        calibration = calibration if isinstance(calibration, dict) else {}
        calibration_data = calibration.get("data")
        calibration_data = calibration_data if isinstance(calibration_data, dict) else {}
        bundle_data = bundle.get("data")
        bundle_data = bundle_data if isinstance(bundle_data, dict) else {}
        device_info = sources.get("device_info")
        device_info = device_info if isinstance(device_info, dict) else {}
        device_data = device_info.get("data")
        device_data = device_data if isinstance(device_data, dict) else {}
        provider_id = str(bundle.get("provider_id") or "camera.replay")
        canonical_device_id = str(
            calibration_data.get("canonical_device_id")
            or device_data.get("canonical_device_id")
            or bundle_data.get("canonical_device_id")
            or bundle.get("canonical_device_id")
            or ""
        ).strip()
        calibration_revision = str(
            calibration_data.get("calibration_revision")
            or calibration_data.get("revision")
            or bundle_data.get("calibration_revision")
            or bundle.get("calibration_revision")
            or ""
        ).strip()
        if provider_id != "camera.replay" and not canonical_device_id:
            raise RuntimeError(
                "live camera observations lack the canonical device identity required for axis activation"
            )
        if provider_id != "camera.replay" and not calibration_revision:
            raise RuntimeError(
                "live camera observations lack the calibration revision required for axis activation"
            )
        return {
            "provider_id": provider_id,
            "provider_instance_id": str(bundle.get("provider_instance_id") or "replay"),
            "boot_id": str(bundle.get("boot_id") or "replay"),
            "canonical_device_id": canonical_device_id or "REPLAY",
            "calibration_revision": calibration_revision or "REPLAY",
        }

    def _capture(self, request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
        if bool(request.get("use_latest_camera", False)):
            return self.clients.snapshot_latest_rgbd(run_dir / "capture")
        required = ("rgb_path", "depth_npy_path", "camera_intrinsics", "camera_frame", "observed_at_us")
        missing = [field for field in required if request.get(field) is None]
        if missing:
            raise ValueError(f"replay capture is missing fields: {', '.join(missing)}")
        rgb_source = Path(str(request["rgb_path"])).resolve()
        depth_source = Path(str(request["depth_npy_path"])).resolve()
        if not rgb_source.is_file() or not depth_source.is_file():
            raise ValueError("replay RGB or depth evidence is unavailable")
        capture_dir = run_dir / "capture"
        capture_dir.mkdir(parents=True)
        rgb_path, depth_path = capture_dir / "rgb.png", capture_dir / "depth_m.npy"
        shutil.copy2(rgb_source, rgb_path)
        shutil.copy2(depth_source, depth_path)
        return {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "camera_intrinsics": request["camera_intrinsics"],
            "camera_frame": str(request["camera_frame"]),
            "observed_at_us": int(request["observed_at_us"]),
            "source_observations": request.get("source_observations"),
        }

    def _snapshot_masks(
        self,
        request: dict[str, Any],
        run_dir: Path,
        capture: dict[str, Any],
        *,
        attempt_count: int,
    ) -> tuple[tuple[MaskCandidate, ...], dict[str, Any]]:
        mask_source = Path(str(request.get("mask_path") or "")).resolve()
        output_dir = run_dir / "mask_candidates"
        output_dir.mkdir(parents=True, exist_ok=True)
        rgb_path = Path(capture["rgb_path"])
        if mask_source.is_file():
            mask_path = output_dir / "mask_1.png"
            shutil.copy2(mask_source, mask_path)
            prompt_record = {
                "method": "CALLER_PROVIDED_MASK",
                "source_sha256": file_sha256(mask_source),
            }
            candidate = create_mask_candidate(
                candidate_id="mask_1",
                mask_path=mask_path,
                rgb_path=rgb_path,
                output_dir=output_dir,
                prompt=prompt_record,
                sam2_score=1.0,
                sam2_provenance=None,
            )
            return (candidate,), {
                "method": "CALLER_PROVIDED_MASK",
                "source_sha256": file_sha256(mask_source),
                "configured_attempt_count": attempt_count,
                "produced_candidate_count": 1,
                "vlm_localization_elapsed_ms": 0.0,
                "sam2_segmentation_elapsed_ms": 0.0,
            }
        self.clients.ensure_sam2_hot()
        minimum_score = float(self.config.get("minimum_sam2_score", 0.0))
        candidates: list[MaskCandidate] = []
        attempt_records: list[dict[str, Any]] = []
        vlm_elapsed_ms = 0.0
        sam2_elapsed_ms = 0.0
        rgb_sha256 = file_sha256(rgb_path)
        for attempt_index in range(attempt_count):
            candidate_id = f"mask_{attempt_index + 1}"
            vlm_started = time.monotonic()
            prompt = self.prompt_locator.locate(
                self.profile.segmentation_reference_paths,
                rgb_path,
                attempt_index=attempt_index + 1,
                attempt_count=attempt_count,
                additional_guidance=self.profile.vlm_seed_guidance,
            )
            prompt_vlm_elapsed_ms = (time.monotonic() - vlm_started) * 1000.0
            vlm_elapsed_ms += prompt_vlm_elapsed_ms
            prompt_record = {
                "candidate_id": candidate_id,
                "ensemble_attempt_index": attempt_index + 1,
                "ensemble_attempt_count": attempt_count,
                "box_yxyx": list(prompt.box_yxyx),
                "positive_points_yx": [list(point) for point in prompt.positive_points_yx],
                "negative_points_yx": [list(point) for point in prompt.negative_points_yx],
                "confidence": prompt.confidence,
                "rationale": prompt.rationale,
                "model": prompt.model,
                "response_id": prompt.response_id,
                "structured_output_attempt_count": prompt.attempt_count,
                "vlm_elapsed_ms": prompt_vlm_elapsed_ms,
                "profile_vlm_seed_guidance": self.profile.vlm_seed_guidance,
            }
            seed_overlay_path = self._write_seed_overlay(
                rgb_path,
                prompt_record,
                run_dir / f"vlm_seed_{candidate_id}.png",
            )
            if seed_overlay_path is not None:
                prompt_record["seed_overlay_path"] = str(seed_overlay_path)
            sam2_started = time.monotonic()
            segmentation = self.clients.segment_mask(
                {
                    "request_id": f"{run_dir.name}-{candidate_id}",
                    "rgb_path": str(rgb_path),
                    "rgb_sha256": rgb_sha256,
                    "box_yxyx": list(prompt.box_yxyx),
                    "positive_points_yx": [
                        list(point) for point in prompt.positive_points_yx
                    ],
                    "negative_points_yx": [
                        list(point) for point in prompt.negative_points_yx
                    ],
                    "prompt_confidence": prompt.confidence,
                }
            )
            prompt_sam2_elapsed_ms = (time.monotonic() - sam2_started) * 1000.0
            sam2_elapsed_ms += prompt_sam2_elapsed_ms
            score = float(
                segmentation.get("quality", {}).get("sam2_score", float("nan"))
            )
            if not np.isfinite(score) or score < minimum_score:
                raise RuntimeError(
                    f"SAM2 score {score!r} for {candidate_id} is below required "
                    f"{minimum_score}"
                )
            artifact = segmentation.get("mask_artifact")
            artifact = artifact if isinstance(artifact, dict) else {}
            source_mask = Path(str(artifact.get("path") or "")).resolve()
            if not source_mask.is_file():
                raise RuntimeError("SAM2 Provider mask artifact is unavailable")
            expected_mask_sha256 = str(artifact.get("sha256") or "").lower()
            if file_sha256(source_mask) != expected_mask_sha256:
                raise RuntimeError("SAM2 Provider mask artifact SHA-256 does not match")
            mask_path = output_dir / f"{candidate_id}.png"
            shutil.copy2(source_mask, mask_path)
            prompt_record.update(
                {
                    "sam2_score": score,
                    "minimum_sam2_score": minimum_score,
                    "sam2_elapsed_ms": prompt_sam2_elapsed_ms,
                    "sam2_mask_sha256": expected_mask_sha256,
                }
            )
            candidate = create_mask_candidate(
                candidate_id=candidate_id,
                mask_path=mask_path,
                rgb_path=rgb_path,
                output_dir=output_dir,
                prompt=prompt_record,
                sam2_score=score,
                sam2_provenance=segmentation.get("provenance"),
            )
            candidates.append(candidate)
            attempt_records.append(candidate.record())
            self._update_inspection(
                stage="MASK_ENSEMBLE_ACQUIRING",
                mask_acquisition={
                    "method": "INDEPENDENT_VLM_POINT_TO_SAM2_MASKS",
                    "configured_attempt_count": attempt_count,
                    "produced_candidate_count": len(candidates),
                    "attempts": attempt_records,
                    "vlm_localization_elapsed_ms": vlm_elapsed_ms,
                    "sam2_segmentation_elapsed_ms": sam2_elapsed_ms,
                },
            )
        return tuple(candidates), {
            "method": "INDEPENDENT_VLM_POINT_TO_SAM2_MASKS",
            "configured_attempt_count": attempt_count,
            "produced_candidate_count": len(candidates),
            "prompt_policy": "ONE_POSITIVE_POINT_AND_ONE_NEGATIVE_SUPPORT_POINT_PER_VLM",
            "reference_set_sha256": self.profile.reference_set_sha256,
            "minimum_sam2_score": minimum_score,
            "attempts": attempt_records,
            "vlm_localization_elapsed_ms": vlm_elapsed_ms,
            "sam2_segmentation_elapsed_ms": sam2_elapsed_ms,
        }

    def _world_from_camera(
        self, request: dict[str, Any], camera_frame: str, observed_at_us: int
    ) -> tuple[np.ndarray, dict[str, Any]]:
        explicit = request.get("world_from_camera")
        if explicit is not None:
            matrix = matrix4(explicit, "world_from_camera")
            world_frame = str(request.get("world_frame") or "world").strip()
            if not world_frame:
                raise ValueError("explicit replay world_frame must be non-empty")
            return matrix, {
                "status": "PASSED_WITH_REPLAY_OVERRIDE",
                "source": "EXPLICIT_REPLAY_REQUEST",
                "at_us": observed_at_us,
                "from_frame": camera_frame,
                "to_frame": world_frame,
                "world_frame": world_frame,
                "session_epoch": request.get("session_epoch"),
            }
        world_axis_stream = str(
            self.config.get("world_axis_stream") or "localization.vio.status"
        )
        required_convention = str(
            self.config.get("required_world_convention")
            or "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
        )
        context = self.clients.current_world_axis(
            world_axis_stream,
            required_convention=required_convention,
        )
        requested_world_frame = str(request.get("world_frame") or "").strip()
        requested_session_epoch = str(request.get("session_epoch") or "").strip()
        if requested_world_frame and requested_world_frame != context["world_frame"]:
            raise RuntimeError(
                "WORLD_AXIS_EPOCH_CHANGED: requested world frame does not match "
                "the active Local VIO epoch"
            )
        if (
            requested_session_epoch
            and requested_session_epoch != context["session_epoch"]
        ):
            raise RuntimeError(
                "WORLD_AXIS_EPOCH_CHANGED: requested session epoch does not match "
                "the active Local VIO epoch"
            )
        matrix, proof = self.clients.transform(
            camera_frame,
            str(context["world_frame"]),
            observed_at_us,
            int(self.config.get("maximum_transform_extrapolation_us", 250000)),
            session_epoch=str(context["session_epoch"]),
        )
        current = self.clients.current_world_axis(
            world_axis_stream,
            required_convention=required_convention,
        )
        if (
            current["world_frame"] != context["world_frame"]
            or current["session_epoch"] != context["session_epoch"]
        ):
            raise RuntimeError(
                "WORLD_AXIS_EPOCH_CHANGED: Local VIO reset while arm-base evidence "
                "was being composed"
            )
        return matrix, {
            "status": "PASSED",
            "source": "WORLD_STATE_FABRIC_TIMESTAMPED_TRANSFORM_GRAPH",
            "at_us": observed_at_us,
            "world_frame": context["world_frame"],
            "session_epoch": context["session_epoch"],
            "convention_id": context["convention_id"],
            "vio_status": context,
            "query": proof,
        }

    def _orientation_proof(
        self,
        selection: OrientationSelection,
        selected: Any,
        contact_sheet: Path,
        minimum_confidence: float,
        upright_normalization: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "PASSED",
            "method": "BOUNDED_REFERENCE_IMAGE_VLM",
            "profile_id": self.profile.profile_id,
            "profile_sha256": self.profile.profile_sha256,
            "reference_set_sha256": self.profile.reference_set_sha256,
            "allowed_candidates": [
                {
                    "candidate_id": value.candidate_id,
                    "axis": value.axis,
                    "degrees": value.degrees,
                    "rotation_xyzw": value.rotation_xyzw,
                }
                for value in self.profile.candidates
            ],
            "selected_candidate_id": selected.candidate_id,
            "selected_axis": selected.axis,
            "selected_degrees": selected.degrees,
            "selected_rotation_xyzw": selected.rotation_xyzw,
            "world_up_normalization": upright_normalization,
            "vlm": {
                "model": selection.model,
                "response_id": selection.response_id,
                "structured_output_attempt_count": selection.attempt_count,
                "confidence": selection.confidence,
                "minimum_confidence": minimum_confidence,
                "rationale": selection.rationale,
            },
            "source_evidence_sha256": orientation_evidence_hash(
                self.profile, contact_sheet, selection
            ),
            "application_origin": "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN",
            "application_order": "camera_from_centered_mesh @ orientation_correction @ centered_mesh_from_arm_base",
            "mesh_center_translation_preserved": True,
        }

    def _resolve_active_profile(
        self,
    ) -> tuple[ArmProfileRecord, ModelProfile, dict[str, Any]]:
        record = self.profile_store.load()
        readiness = {
            "status": "REQUESTING_HOT_RESIDENCY",
            "provider_id": record.arm_provider_id,
            "assembly_stream": self.assembly_stream,
            "residency_owner": "RESOURCE_PROVIDER_MANAGER",
        }
        self._update_inspection(
            stage="ARM_PROVIDER_READINESS",
            arm_profile=record.public(self.root),
            arm_provider_readiness=readiness,
        )
        state = self.clients.ensure_active_arm_profile_state(
            record.arm_provider_id,
            self.assembly_stream,
            timeout_s=float(self.config.get("arm_provider_readiness_timeout_s", 15.0)),
            poll_interval_s=float(
                self.config.get("arm_provider_readiness_poll_interval_s", 0.1)
            ),
        )
        self._update_inspection(
            arm_provider_readiness={
                **readiness,
                "status": "READY",
                "assembly_id": state.get("assembly_id"),
                "assembly_revision": state.get("assembly_revision"),
            }
        )
        identity = state.get("arm_model_identity")
        hashes = state.get("profile_file_sha256")
        appendix_root = state.get("arm_model_appendix")
        if not isinstance(identity, dict) or not isinstance(hashes, dict):
            raise RuntimeError("active assembly lacks arm-profile identity and digest")
        if str(state.get("arm_provider_id") or "") != record.arm_provider_id:
            raise RuntimeError(
                "active assembly arm Provider does not match the locally selected arm Provider"
            )
        if (
            str(identity.get("model_id") or "") != record.model_id
            or str(identity.get("model_revision") or "") != record.model_revision
        ):
            raise RuntimeError(
                "active assembly arm profile does not match the locally selected arm profile"
            )
        if str(hashes.get("arm_model") or "").lower() != record.model_file_sha256:
            raise RuntimeError(
                "active arm profile digest is stale; restart the arm Provider after profile edits"
            )
        if not isinstance(appendix_root, dict):
            raise RuntimeError(
                "active arm Provider does not publish the arm-model appendix; rebuild and restart it"
            )
        appendix = appendix_root.get(record.appendix_key)
        if not isinstance(appendix, dict):
            raise RuntimeError(
                f"active arm profile lacks appendix {record.appendix_key!r}"
            )
        if canonical_sha256(appendix) != canonical_sha256(record.appendix):
            raise RuntimeError(
                "active arm profile appendix is stale; restart the arm Provider after profile edits"
            )
        return record, load_profile_payload(appendix, self.root), {
            "assembly_id": state.get("assembly_id"),
            "assembly_revision": state.get("assembly_revision"),
            "assembly_fingerprint": state.get("assembly_fingerprint"),
            "arm_provider_id": record.arm_provider_id,
            "arm_model_id": record.model_id,
            "arm_model_revision": record.model_revision,
            "arm_model_file_sha256": record.model_file_sha256,
            "appendix_key": record.appendix_key,
            "appendix_sha256": canonical_sha256(appendix),
        }

    def _snapshot_profile_assets(
        self,
        record: ArmProfileRecord,
        profile: ModelProfile,
        run_dir: Path,
    ) -> ModelProfile:
        profile_dir = run_dir / "profile"
        mesh_dir = profile_dir / "mesh"
        reference_dir = profile_dir / "references"
        mesh_dir.mkdir(parents=True)
        reference_dir.mkdir(parents=True)
        mesh_path = mesh_dir / profile.mesh_path.name
        shutil.copy2(profile.mesh_path, mesh_path)
        mesh_preview_path: Path | None = None
        if profile.mesh_preview_path is not None:
            mesh_preview_path = mesh_dir / profile.mesh_preview_path.name
            shutil.copy2(profile.mesh_preview_path, mesh_preview_path)
        reference_paths: list[Path] = []
        snapshot_paths: dict[Path, Path] = {}
        for index, source in enumerate(profile.reference_paths, start=1):
            target = reference_dir / f"{index:02d}_{source.name}"
            shutil.copy2(source, target)
            reference_paths.append(target)
            snapshot_paths[source] = target
        (profile_dir / "arm_profile_appendix.json").write_text(
            json.dumps(record.appendix, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return replace(
            profile,
            mesh_path=mesh_path,
            mesh_preview_path=mesh_preview_path,
            reference_paths=tuple(reference_paths),
            segmentation_reference_paths=tuple(
                snapshot_paths[path] for path in profile.segmentation_reference_paths
            ),
            orientation_reference_paths=tuple(
                snapshot_paths[path] for path in profile.orientation_reference_paths
            ),
        )

    def _begin_attempt_inspection(self, run_id: str, run_dir: Path) -> None:
        with self._inspection_lock:
            self._inspection_path = run_dir / "inspection.json"
            self._inspection = {
                "schema": "midbrain.skill.locate_arm_base.development_inspection",
                "schema_version": 1,
                "run_id": run_id,
                "status": "RUNNING",
                "stage": "RUN_CREATED",
                "run_directory": str(run_dir),
                "arm_provider_readiness": {"status": "NOT_STARTED"},
                "images": [],
            }
            self._inspection_mtime_ns = 0
            self._persist_inspection_locked()

    def _begin_inspection(
        self,
        run_id: str,
        run_dir: Path,
        record: ArmProfileRecord,
        profile: ModelProfile,
        binding: dict[str, Any],
    ) -> None:
        with self._inspection_lock:
            if (
                self._inspection is None
                or self._inspection_path != run_dir / "inspection.json"
                or self._inspection.get("run_id") != run_id
            ):
                raise RuntimeError("current inspection does not match the active run")
            self._inspection.update(
                {
                    "status": "RUNNING",
                    "stage": "PROFILE_BOUND",
                    "arm_profile": record.public(self.root),
                    "active_binding": binding,
                    "vlm": {
                        "backend": self.vlm_backend,
                        "model": self.vlm_model,
                        "selection_source": self.vlm_selection_source,
                    },
                    "foundation_pose": {
                        "cad_filename": profile.mesh_path.name,
                        "cad_path": str(profile.mesh_path),
                        "cad_sha256": profile.mesh_sha256,
                        "cad_scale_to_m": profile.mesh_scale_to_m,
                    },
                    "images": [],
                }
            )
            if profile.mesh_preview_path is not None:
                self._inspection["images"].append(
                    {
                        "image_id": "foundation_pose_cad_preview",
                        "label": "Exact FoundationPose CAD preview",
                        "path": str(profile.mesh_preview_path),
                        "consumers": ["DEVELOPER_INSPECTION"],
                    }
                )
            for index, (path, consumers) in enumerate(
                zip(profile.reference_paths, profile.reference_consumers), start=1
            ):
                self._inspection["images"].append(
                    {
                        "image_id": f"reference_{index}",
                        "label": f"Arm profile reference {index}",
                        "path": str(path),
                        "consumers": list(consumers),
                    }
                )
            self._persist_inspection_locked()

    def _update_inspection(self, **values: Any) -> None:
        with self._inspection_lock:
            if self._inspection is None:
                return
            self._inspection.update(values)
            self._persist_inspection_locked()

    def _add_image(
        self,
        image_id: str,
        label: str,
        path: Path,
        consumers: list[str],
    ) -> None:
        with self._inspection_lock:
            if self._inspection is None:
                return
            images = self._inspection.setdefault("images", [])
            images[:] = [item for item in images if item.get("image_id") != image_id]
            images.append(
                {
                    "image_id": image_id,
                    "label": label,
                    "path": str(path.resolve()),
                    "consumers": consumers,
                }
            )
            self._persist_inspection_locked()

    def _fail_inspection(self, error: str, *, elapsed_ms: float) -> None:
        with self._inspection_lock:
            if self._inspection is None:
                return
            failed_stage = str(self._inspection.get("stage") or "UNKNOWN")
            if failed_stage == "ARM_PROVIDER_READINESS":
                readiness = self._inspection.get("arm_provider_readiness")
                readiness = dict(readiness) if isinstance(readiness, dict) else {}
                readiness.update(status="FAILED", error=error)
                self._inspection["arm_provider_readiness"] = readiness
            timing = self._inspection.get("timing")
            timing = dict(timing) if isinstance(timing, dict) else {}
            timing["skill_elapsed_ms"] = float(elapsed_ms)
            self._inspection.update(
                status="FAILED",
                stage="FAILED",
                failed_stage=failed_stage,
                error=error,
                timing=timing,
            )
            self._persist_inspection_locked()

    def _persist_inspection_locked(self) -> None:
        if self._inspection is None or self._inspection_path is None:
            return
        temporary = self._inspection_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._inspection, indent=2, ensure_ascii=False, default=str)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._inspection_path)
        self._inspection_mtime_ns = self._inspection_path.stat().st_mtime_ns

    def _refresh_latest_inspection(self) -> None:
        with self._inspection_lock:
            candidates = list(self.run_root.glob("*/inspection.json"))
            if not candidates:
                return
            latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
            modified = latest.stat().st_mtime_ns
            if self._inspection_path == latest and modified <= self._inspection_mtime_ns:
                return
            value = json.loads(latest.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._inspection = value
                self._inspection_path = latest
                self._inspection_mtime_ns = modified

    def inspection_snapshot(self) -> dict[str, Any]:
        self._refresh_latest_inspection()
        with self._inspection_lock:
            return json.loads(json.dumps(self._inspection or {}, default=str))

    def profile_snapshot(self) -> dict[str, Any]:
        return self.profile_store.load().public(self.root)

    def save_profile_appendix(self, appendix: dict[str, Any]) -> dict[str, Any]:
        record = self.profile_store.save_appendix(appendix)
        self.profile = record.model_profile
        return record.public(self.root)

    @staticmethod
    def _write_depth_preview(depth_path: Path, output_path: Path) -> Path:
        depth = np.load(depth_path, allow_pickle=False).astype(np.float32)
        valid = depth[np.isfinite(depth) & (depth > 0.0)]
        preview = np.zeros(depth.shape, dtype=np.uint8)
        if valid.size:
            low, high = np.percentile(valid, [2.0, 98.0])
            if high <= low:
                high = low + 1e-6
            normalized = np.clip((depth - low) / (high - low), 0.0, 1.0)
            preview = np.where(
                np.isfinite(depth) & (depth > 0.0),
                (255.0 * (1.0 - normalized)).astype(np.uint8),
                0,
            )
        Image.fromarray(preview).save(output_path)
        return output_path

    @staticmethod
    def _write_seed_overlay(
        rgb_path: Path,
        acquisition: dict[str, Any],
        output_path: Path,
    ) -> Path | None:
        box = acquisition.get("box_yxyx")
        points = acquisition.get("positive_points_yx")
        negative_points = acquisition.get("negative_points_yx")
        if not isinstance(box, list) or len(box) != 4:
            return None
        image = Image.open(rgb_path).convert("RGB")
        draw = ImageDraw.Draw(image)
        y0, x0, y1, x1 = [float(value) for value in box]
        scale_x, scale_y = image.width / 1000.0, image.height / 1000.0
        draw.rectangle(
            (x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y),
            outline=(255, 72, 72),
            width=max(2, image.width // 320),
        )
        for point in points if isinstance(points, list) else []:
            if not isinstance(point, list) or len(point) != 2:
                continue
            y, x = float(point[0]) * scale_y, float(point[1]) * scale_x
            radius = max(4, image.width // 120)
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=(80, 220, 120),
                outline=(0, 0, 0),
            )
        for point in negative_points if isinstance(negative_points, list) else []:
            if not isinstance(point, list) or len(point) != 2:
                continue
            y, x = float(point[0]) * scale_y, float(point[1]) * scale_x
            radius = max(4, image.width // 120)
            draw.line(
                (x - radius, y - radius, x + radius, y + radius),
                fill=(255, 72, 72),
                width=max(2, image.width // 320),
            )
            draw.line(
                (x - radius, y + radius, x + radius, y - radius),
                fill=(255, 72, 72),
                width=max(2, image.width // 320),
            )
        image.save(output_path)
        return output_path

    def close(self) -> None:
        close_selector = getattr(self.selector, "close", None)
        if callable(close_selector):
            close_selector()
        close_prompt_locator = getattr(self.prompt_locator, "close", None)
        if callable(close_prompt_locator):
            close_prompt_locator()
        close_mask_selector = getattr(self.mask_selector, "close", None)
        if callable(close_mask_selector):
            close_mask_selector()
        close_fit_selector = getattr(self.fit_selector, "close", None)
        if callable(close_fit_selector):
            close_fit_selector()
        self.clients.close()
