from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .artifacts import MonitorArtifacts
from .candidate_review import canonical_sha256
from .camera import (
    RgbdCapture,
    encode_depth_png,
    encode_rgb_jpeg,
    make_initial_mask,
    render_overlay,
    save_frame_artifacts,
    segmented_surface_depth,
    tip_depth_from_near_cluster,
)
from .clients import FabricClient, FoundationPoseHealthClient, ManagerClient
from .config import Settings, WORKSPACE_ROOT, load_skill_config
from .foundation_engine import (
    LocalFoundationPoseEngine,
    IN_PROCESS_EXECUTION_HOST,
    PROVIDER_EXECUTION_HOST,
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
    normalize_base_pose_engine_route,
    normalize_foundation_pose_execution_host,
)
from .lease import MotionInhibitKeeper
from .math3d import (
    apply_base_mesh_hypothesis_correction,
    apply_transform,
    closest_pair_consensus,
    inspect_base_up_alignment,
    robust_average_transforms,
    select_base_orientation_correction,
    select_base_orientation_from_gripper_point,
    transform_from_payload,
    transform_payload,
)
from .models import (
    RunMode,
    SkillState,
    canonical_run_mode,
    mode_contract,
)
from .persistence import CalibrationStore
from .pose_validation import (
    load_model_geometry,
    projected_visual_scale_review,
    render_pose_overlay,
    select_best_pose_validation,
)
from .progress import ProgressReporter
from .vlm import (
    OPENAI_API_ROUTE,
    REVIEWED_FILE_ROUTE,
    GripperVision,
    ReviewedFileVision,
)


TERMINAL_POSE_STATES = {"FAILED", "STOPPED", "EXPIRED", "COMPLETED"}


def gripper_axis_depth_is_trusted(
    detection: dict[str, Any],
    minimum_confidence: float,
) -> bool:
    confidence = detection.get("confidence")
    return (
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and math.isfinite(float(confidence))
        and float(confidence) >= float(minimum_confidence)
    )


class AlignmentSkill:
    def __init__(
        self,
        settings: Settings | None = None,
        config: dict[str, Any] | None = None,
        artifacts: MonitorArtifacts | None = None,
    ):
        self.settings = settings or Settings()
        self.config = config or load_skill_config()
        self.manager = ManagerClient(self.settings.manager_url)
        self.fabric = FabricClient(self.settings.fabric_url)
        self.foundation_health = FoundationPoseHealthClient(
            self.settings.foundation_pose_control_url
        )
        self.progress = ProgressReporter(self.fabric)
        self.artifacts = artifacts or MonitorArtifacts()
        self.store = CalibrationStore(self.settings.calibration_root)
        self.base_pose_engine_route = normalize_base_pose_engine_route(
            self.config
        )
        self.foundation_pose_execution_host = (
            normalize_foundation_pose_execution_host(self.config)
        )
        self.local_foundation_engine: LocalFoundationPoseEngine | None = None
        self.last_base_pose_engine_lifecycle: dict[str, Any] = {
            "route": self.base_pose_engine_route,
            "state": "NOT_STARTED",
            "owned_session_count_after": 0,
            "execution_host": self.foundation_pose_execution_host,
            "gpu_resources_released": True,
            "backend_closed": True,
        }
        self.cancel_event = asyncio.Event()
        self.running_lock = asyncio.Lock()
        self.provider_request_lock = asyncio.Lock()
        self.sequence = 0
        self.current_task: asyncio.Task[dict[str, Any]] | None = None
        self.runtime_status: dict[str, Any] = {
            "state": "PENDING",
            "message": "Waiting to request required providers.",
            "updated_at_us": time.time_ns() // 1000,
            "providers": {},
        }

    def start(
        self,
        mode: RunMode,
        *,
        arm_is_home: bool = False,
        allow_active_control_interrupt: bool = False,
        vision_route: str = OPENAI_API_ROUTE,
        review_timeout_s: float = 300.0,
    ) -> asyncio.Task[dict[str, Any]]:
        if self.current_task is not None and not self.current_task.done():
            raise RuntimeError("an alignment run is already active")
        self.cancel_event.clear()
        self.current_task = asyncio.create_task(
            self.run(
                mode,
                arm_is_home=arm_is_home,
                allow_active_control_interrupt=allow_active_control_interrupt,
                vision_route=vision_route,
                review_timeout_s=review_timeout_s,
            ),
            name="stationary-world-arm-alignment",
        )
        return self.current_task

    async def cancel(self) -> None:
        self.cancel_event.set()

    async def run(
        self,
        mode: RunMode,
        *,
        arm_is_home: bool = False,
        allow_active_control_interrupt: bool = False,
        vision_route: str = OPENAI_API_ROUTE,
        review_timeout_s: float = 300.0,
    ) -> dict[str, Any]:
        if self.running_lock.locked():
            raise RuntimeError("an alignment run is already active")
        async with self.running_lock:
            return await self._run_locked(
                mode,
                arm_is_home=arm_is_home,
                allow_active_control_interrupt=allow_active_control_interrupt,
                vision_route=vision_route,
                review_timeout_s=review_timeout_s,
            )

    async def _run_locked(
        self,
        mode: RunMode,
        *,
        arm_is_home: bool,
        allow_active_control_interrupt: bool,
        vision_route: str,
        review_timeout_s: float,
    ) -> dict[str, Any]:
        mode = canonical_run_mode(RunMode(mode))
        skill_id = f"skill-align-{uuid.uuid4()}"
        alignment_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        started_us = time.time_ns() // 1000
        run_dir = self.settings.run_root / alignment_id
        run_dir.mkdir(parents=True, exist_ok=False)
        own_sessions: list[str] = []
        keeper: MotionInhibitKeeper | None = None
        vision: GripperVision | ReviewedFileVision | None = None
        selected_mode = mode
        await self.progress.update(
            skill_id=skill_id,
            alignment_id=alignment_id,
            mode=str(mode),
            state=SkillState.RUNNING,
            phase="PREFLIGHT",
            message="Checking Fabric, camera, VIO, arm stationarity, and prior calibration.",
            started_at_us=started_us,
            progress_kind="milestone",
            completed_units=0,
            total_units=8,
            provider_responsive=None,
            provider_sessions=[],
            details={},
            result=None,
        )
        try:
            _, _, workcell_calibrations = await asyncio.gather(
                self.manager.health(),
                self.fabric.health(),
                self.manager.workcell_calibrations(),
            )
            prior = self._manager_verified_prior_alignment(
                workcell_calibrations
            )
            provider_state = await self._provider_state()
            inhibit_config = self.config["motion_inhibit"]
            keeper = MotionInhibitKeeper(
                self.manager,
                owner_id=skill_id,
                related_skill_id=skill_id,
                duration_ms=int(inhibit_config["duration_ms"]),
                renew_every_ms=int(inhibit_config["renew_every_ms"]),
                failure_limit=int(inhibit_config["renewal_failure_limit"]),
            )
            await keeper.acquire()
            await self.progress.update(
                phase="MOTION_INHIBITED",
                message=(
                    f"Motion is inhibited ({keeper.mode}); requesting providers and "
                    "collecting stationary IMU samples for VIO."
                ),
                completed_units=1,
                details={"motion_inhibit": keeper.status()},
            )
            await self._ensure_hot_inputs()
            await keeper.ensure_valid()
            await self._verify_arm_stationary(allow_active_control_interrupt)
            await self._check_cancel()
            await self.progress.update(
                phase="MOTION_INHIBITED",
                message="VIO is tracking and the arm is stationary; capturing RGB-D evidence.",
                completed_units=1,
                details={"motion_inhibit": keeper.status()},
            )
            capture = RgbdCapture(self.fabric, self.config["camera_frame"])
            frame = await capture.capture()
            if mode == RunMode.AUTO:
                prior_is_current = bool(
                    prior
                    and prior.get("vio_session_epoch") == frame.session_epoch
                    and self._prior_alignment_review_usable(
                        prior,
                        workcell_calibrations,
                    )
                )
                prior_tool_beak = (
                    prior.get("learned_tool_to_beak_translation_m")
                    if isinstance(prior, dict)
                    else None
                )
                prior_is_refinable = bool(
                    prior_is_current
                    and isinstance(prior_tool_beak, list)
                    and len(prior_tool_beak) == 3
                )
                selected_mode = (
                    RunMode.VLM_GRIPPER_ONLY
                    if prior_is_refinable
                    else RunMode.FOUNDATION_BASE_VLM_GRIPPER
                )
                await self.progress.update(
                    mode=str(selected_mode),
                    message=(
                        f"Auto selected {selected_mode}; prior VIO epoch current="
                        f"{prior_is_current}, prior tool geometry usable="
                        f"{prior_is_refinable}."
                    ),
                )
            save_frame_artifacts(run_dir, frame)
            await self.artifacts.set_images(
                rgb_jpeg=encode_rgb_jpeg(frame.rgb),
                depth_png=encode_depth_png(frame.depth_m),
            )
            await self.progress.update(
                phase="VLM_LOCALIZATION",
                message="Locating the base, gripper, and foremost beak point.",
                completed_units=2,
            )
            selected_vision_route = str(vision_route).strip().upper()
            if selected_vision_route == OPENAI_API_ROUTE:
                vision = GripperVision(
                    self.settings.openai_api_key,
                    self.settings.openai_vision_model,
                    WORKSPACE_ROOT,
                )
            elif selected_vision_route == REVIEWED_FILE_ROUTE:
                vision = ReviewedFileVision(
                    WORKSPACE_ROOT,
                    run_dir,
                    timeout_s=review_timeout_s,
                )
            else:
                raise ValueError(
                    f"unsupported vision route: {vision_route}"
                )
            await self.progress.update(
                details={
                    "vision_route": selected_vision_route,
                    "review_timeout_s": (
                        float(review_timeout_s)
                        if selected_vision_route == REVIEWED_FILE_ROUTE
                        else None
                    ),
                    "automatic_fallback": False,
                }
            )
            vlm = await vision.locate(
                frame.rgb,
                require_base=selected_mode != RunMode.VLM_GRIPPER_ONLY,
            )
            (run_dir / "vlm.json").write_text(json.dumps(vlm, indent=2) + "\n", encoding="utf-8")
            camera_system_beak: np.ndarray | None = None
            tip_depths: dict[str, Any] = {}
            beak_depth_warning: str | None = None
            try:
                camera_system_beak, tip_depths = self._camera_system_beak(
                    frame,
                    vlm,
                )
            except Exception as error:
                if selected_mode == RunMode.VLM_GRIPPER_ONLY:
                    raise
                beak_depth_warning = str(error)
            visible_detections = {
                name: value
                for name, value in {"base": vlm["base"], "gripper": vlm["gripper"]}.items()
                if value["visible"]
            }
            masks = {
                name: make_initial_mask(
                    frame.rgb,
                    detection["box_2d"],
                    detection["positive_points_2d"],
                    padding_fraction=float(self.config["mask"]["box_padding_fraction"]),
                    minimum_pixels=int(self.config["mask"]["minimum_pixels"]),
                )
                for name, detection in visible_detections.items()
            }
            for name, mask in masks.items():
                cv2.imwrite(str(run_dir / f"{name}_mask.png"), mask)
            gripper_axis_reference: dict[str, Any] = {
                "available": False,
                "source": "VLM_GRIPPER_SEGMENTATION_WITH_ALIGNED_DEPTH",
            }
            gripper_mask = masks.get("gripper")
            minimum_gripper_axis_confidence = float(
                self.config["base_alignment"][
                    "minimum_gripper_axis_confidence"
                ]
            )
            gripper_detection = vlm["gripper"]
            if gripper_mask is not None and gripper_axis_depth_is_trusted(
                gripper_detection,
                minimum_gripper_axis_confidence,
            ):
                try:
                    gripper_surface = segmented_surface_depth(
                        frame,
                        gripper_mask,
                        self.config["depth"],
                    )
                    gripper_axis_reference = {
                        "available": True,
                        "source": (
                            "VLM_GRIPPER_SEGMENTATION_WITH_ALIGNED_DEPTH"
                        ),
                        **gripper_surface,
                    }
                except Exception as error:
                    gripper_axis_reference["warning"] = (
                        "Aligned gripper depth was unavailable; the bounded "
                        f"RGB axis review will be used instead: {error}"
                    )
            elif gripper_mask is not None:
                reported_confidence = gripper_detection.get("confidence")
                confidence_text = (
                    f"{float(reported_confidence):.3f}"
                    if isinstance(reported_confidence, (int, float))
                    and not isinstance(reported_confidence, bool)
                    and math.isfinite(float(reported_confidence))
                    else "unavailable"
                )
                gripper_axis_reference["warning"] = (
                    "The VLM gripper localization confidence "
                    f"{confidence_text} is below the "
                    f"{minimum_gripper_axis_confidence:.3f} axis-decision "
                    "minimum; the bounded RGB axis review will be used instead."
                )
            overlay = render_overlay(frame.rgb, visible_detections, masks, tip_depths)
            (run_dir / "overlay.jpg").write_bytes(overlay)
            await self.artifacts.set_overlay(overlay)

            vio_from_camera = transform_from_payload(
                await self.fabric.transform(
                    from_frame=frame.camera_frame,
                    to_frame=frame.world_frame,
                    at_us=frame.timestamp_us,
                    max_extrapolation_us=750_000,
                    session_epoch=frame.session_epoch,
                )
            )
            if selected_mode == RunMode.VLM_GRIPPER_ONLY:
                if not prior:
                    raise RuntimeError(
                        "VLM gripper-only alignment requires a prior alignment"
                    )
                if camera_system_beak is None:
                    raise RuntimeError(
                        "VLM gripper-only alignment requires beak depth"
                    )
                base_from_tool = await self._base_from_tool(
                    frame.timestamp_us
                )
                result = await self._vlm_gripper_only(
                    prior=prior,
                    frame=frame,
                    camera_system_beak=camera_system_beak,
                    base_from_tool=base_from_tool,
                    alignment_id=alignment_id,
                    skill_id=skill_id,
                    vlm=vlm,
                    keeper=keeper,
                    vision=vision,
                    run_dir=run_dir,
                    vio_from_camera=vio_from_camera,
                )
            else:
                if self.foundation_pose_execution_host == PROVIDER_EXECUTION_HOST:
                    await self.manager.ensure_hot(
                        self.config["foundation_pose_provider_id"],
                        timeout_s=90.0,
                    )
                use_foundation_gripper = (
                    selected_mode == RunMode.FOUNDATION_BASE_GRIPPER
                )
                samples, validations = await self._validated_foundation(
                    skill_id=skill_id,
                    alignment_id=alignment_id,
                    run_dir=run_dir,
                    frame=frame,
                    keeper=keeper,
                    vision=vision,
                    own_sessions=own_sessions,
                    include_gripper=use_foundation_gripper,
                    base_visual_box_2d=vlm["base"]["box_2d"],
                    vio_from_camera=vio_from_camera,
                    gripper_axis_reference=gripper_axis_reference,
                )
                base_from_tool_for_learning: np.ndarray | None = None
                tool_learning_warning = beak_depth_warning
                if camera_system_beak is not None:
                    try:
                        base_from_tool_for_learning = await self._base_from_tool(
                            frame.timestamp_us
                        )
                    except Exception as error:
                        tool_learning_warning = str(error)
                finish = (
                    self._finish_foundation_dual
                    if use_foundation_gripper
                    else self._finish_base_vlm
                )
                result = await finish(
                    samples=samples,
                    validations=validations,
                    frame=frame,
                    alignment_id=alignment_id,
                    skill_id=skill_id,
                    vlm=vlm,
                    arm_is_home=arm_is_home,
                    keeper=keeper,
                    vio_from_camera=vio_from_camera,
                    camera_system_beak=camera_system_beak,
                    base_from_tool_for_learning=base_from_tool_for_learning,
                    tool_learning_warning=tool_learning_warning,
                )
            path = self.store.save(result)
            await self._publish_result(result)
            await self.artifacts.set_geometry(result["monitor_geometry"])
            await self.progress.update(
                state=SkillState.SUCCEEDED,
                phase="COMPLETE",
                message=f"Alignment published and saved at {path}.",
                completed_units=8,
                result=result,
                details={
                    "motion_inhibit": keeper.status(),
                    "samples": {
                        "base": int(
                            (result.get("diagnostics", {}).get("base_samples") or {}).get(
                                "input_count",
                                0,
                            )
                        ),
                        "gripper": int(
                            (result.get("diagnostics", {}).get("gripper_samples") or {}).get(
                                "input_count",
                                0,
                            )
                        ),
                    },
                    "pose_validation": {
                        "attempt": len(
                            result.get("diagnostics", {}).get(
                                "foundation_pose_validation",
                                [],
                            )
                        ),
                        "maximum_attempts": 2,
                        "state": (
                            "ACCEPTED"
                            if result.get("diagnostics", {}).get(
                                "foundation_pose_validation"
                            )
                            else "NOT_USED"
                        ),
                    },
                },
            )
            return result
        except asyncio.CancelledError:
            await self.progress.update(
                state=SkillState.CANCELLED,
                phase="CANCELLED",
                message="Alignment was cancelled; no new transform was published.",
            )
            raise
        except Exception as error:
            state = SkillState.CANCELLED if self.cancel_event.is_set() else SkillState.FAILED
            error_diagnostics = getattr(error, "diagnostics", None)
            failure = {
                "error_type": type(error).__name__,
                "message": str(error),
                "diagnostics": (
                    error_diagnostics
                    if isinstance(error_diagnostics, dict)
                    else None
                ),
            }
            (run_dir / "failure.json").write_text(
                json.dumps(failure, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            await self.progress.update(
                state=state,
                phase=str(state),
                message=str(error),
                details={
                    "error_type": type(error).__name__,
                    "error_diagnostics": failure["diagnostics"],
                    "motion_inhibit": keeper.status() if keeper else None,
                },
            )
            raise
        finally:
            if self.foundation_pose_execution_host == PROVIDER_EXECUTION_HOST:
                cleanup_errors: list[str] = []
                for session_id in own_sessions:
                    try:
                        await self.manager.provider_request(
                            self.config["foundation_pose_provider_id"],
                            action="stop",
                            payload={
                                "session_id": session_id,
                                "reason": "alignment skill cleanup",
                            },
                            related_skill_id=skill_id,
                        )
                    except Exception as error:
                        cleanup_errors.append(
                            f"stop session {session_id}: {error}"
                        )
                try:
                    health = await self.foundation_health.health()
                    active_foreign = [
                        session
                        for session in self._health_sessions(health)
                        if str(session.get("session_id")) not in own_sessions
                        and str(session.get("state")) not in TERMINAL_POSE_STATES
                    ]
                    if not active_foreign:
                        release_result = await self.manager.provider_request(
                            self.config["foundation_pose_provider_id"],
                            action="release_resources",
                            payload={
                                "reason": "stationary alignment job complete",
                            },
                            related_skill_id=skill_id,
                        )
                        await self.manager.stop_provider(
                            self.config["foundation_pose_provider_id"]
                        )
                        self.last_base_pose_engine_lifecycle = {
                            "route": self.base_pose_engine_route,
                            "execution_host": PROVIDER_EXECUTION_HOST,
                            "state": "RELEASED_AND_STOPPED",
                            "owned_session_count_after": 0,
                            "owned_sessions": sorted(own_sessions),
                            "gpu_resources_released": bool(
                                release_result.get("resources_released")
                            ),
                            "backend_closed": bool(
                                release_result.get("resources_released")
                            ),
                            "cleanup_errors": cleanup_errors,
                        }
                    else:
                        self.last_base_pose_engine_lifecycle = {
                            "route": self.base_pose_engine_route,
                            "execution_host": PROVIDER_EXECUTION_HOST,
                            "state": "RETAINED_FOR_FOREIGN_SESSIONS",
                            "owned_session_count_after": 0,
                            "owned_sessions": sorted(own_sessions),
                            "foreign_sessions": [
                                str(session.get("session_id"))
                                for session in active_foreign
                            ],
                            "gpu_resources_released": False,
                            "backend_closed": False,
                            "cleanup_errors": cleanup_errors,
                        }
                except Exception as error:
                    cleanup_errors.append(str(error))
                    self.last_base_pose_engine_lifecycle = {
                        "route": self.base_pose_engine_route,
                        "execution_host": PROVIDER_EXECUTION_HOST,
                        "state": "CLEANUP_FAILED",
                        "owned_session_count_after": None,
                        "owned_sessions": sorted(own_sessions),
                        "gpu_resources_released": False,
                        "backend_closed": False,
                        "cleanup_errors": cleanup_errors,
                    }
            if keeper:
                try:
                    await keeper.release()
                except Exception:
                    pass
            if vision:
                await vision.close()

    async def _provider_state(self) -> list[dict[str, Any]]:
        providers = await self.manager.providers()
        selected = {
            key: self.config[key]
            for key in (
                "camera_provider_id",
                "vio_provider_id",
                "arm_provider_id",
            )
        }
        selected["base_pose_engine"] = self.base_pose_engine_route
        if self.foundation_pose_execution_host == PROVIDER_EXECUTION_HOST:
            selected["foundation_pose_provider_id"] = self.config[
                "foundation_pose_provider_id"
            ]
        await self.progress.update(selected_providers=selected)
        return providers

    async def request_runtime_inputs(self, *, wait_for_vio_tracking: bool = False) -> dict[str, Any]:
        async with self.provider_request_lock:
            required = {
                "camera": self.config["camera_provider_id"],
                "vio": self.config["vio_provider_id"],
                "arm": self.config["arm_provider_id"],
            }
            self._set_runtime_status(
                "REQUESTING",
                "Checking Manager and Fabric before requesting required providers.",
            )
            try:
                await asyncio.gather(self.manager.health(), self.fabric.health())
                providers = await self.manager.providers()
                self.runtime_status["providers"] = self._provider_views(providers)
                for role, provider_id in required.items():
                    if self._provider_was_hot(providers, provider_id):
                        self._set_runtime_status(
                            "REQUESTING",
                            (
                                f"Reusing current Manager-registered HOT {role} "
                                f"provider {provider_id}."
                            ),
                        )
                    else:
                        self._set_runtime_status(
                            "REQUESTING",
                            f"Requesting {role} provider {provider_id}.",
                        )
                        await self.manager.ensure_hot(provider_id, timeout_s=60.0)
                    providers = await self.manager.providers()
                    self.runtime_status["providers"] = self._provider_views(providers)
                if wait_for_vio_tracking:
                    self._set_runtime_status(
                        "WAITING_FOR_VIO",
                        "Required providers accepted; waiting for VIO TRACKING.",
                    )
                    await self._wait_for_vio_tracking()
                providers = await self.manager.providers()
                self.runtime_status["providers"] = self._provider_views(providers)
                self._set_runtime_status(
                    "READY",
                    (
                        "Required providers are requested and VIO is tracking."
                        if wait_for_vio_tracking
                        else "Required providers are requested; live readiness is shown below."
                    ),
                )
            except Exception as error:
                try:
                    providers = await self.manager.providers()
                    self.runtime_status["providers"] = self._provider_views(providers)
                except Exception:
                    pass
                self._set_runtime_status("DEGRADED", str(error))
                raise
            return dict(self.runtime_status)

    async def runtime_snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.runtime_status)
        try:
            providers = await asyncio.wait_for(self.manager.providers(), timeout=2.0)
            provider_views = self._provider_views(providers)
            snapshot["providers"] = provider_views
            snapshot["manager_reachable"] = True
            if snapshot.get("state") not in {"REQUESTING", "WAITING_FOR_VIO"}:
                not_ready = self._required_providers_not_ready(provider_views)
                if not_ready:
                    snapshot["state"] = "DEGRADED"
                    snapshot["message"] = (
                        "Required providers are not ready: " + ", ".join(not_ready)
                    )
                elif snapshot.get("state") == "DEGRADED":
                    snapshot["state"] = "READY"
                    snapshot["message"] = "All required providers are ready and HOT."
        except Exception as error:
            snapshot["manager_reachable"] = False
            snapshot["manager_error"] = str(error)
            snapshot["state"] = "DEGRADED"
            snapshot["message"] = "Midbrain Manager is unreachable."
        try:
            await asyncio.wait_for(self.fabric.health(), timeout=2.0)
            snapshot["fabric_reachable"] = True
        except Exception as error:
            snapshot["fabric_reachable"] = False
            snapshot["fabric_error"] = str(error)
            snapshot["state"] = "DEGRADED"
            snapshot["message"] = "World State Fabric is unreachable."
        return snapshot

    async def _ensure_hot_inputs(self) -> None:
        await self.request_runtime_inputs(wait_for_vio_tracking=True)
        deadline = time.monotonic() + 20.0
        not_ready: list[str] = []
        while time.monotonic() < deadline:
            provider_views = self._provider_views(await self.manager.providers())
            not_ready = self._required_providers_not_ready(provider_views)
            if not not_ready:
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(
            "required providers did not become ready: " + ", ".join(not_ready)
        )

    async def _wait_for_vio_tracking(self) -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            vio = await self.fabric.latest_optional("localization.vio.status")
            if vio and (vio.get("data") or {}).get("tracking_state") == "TRACKING":
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("VIO must be TRACKING before alignment starts")

    def _set_runtime_status(self, state: str, message: str) -> None:
        self.runtime_status.update(
            {
                "state": state,
                "message": message,
                "updated_at_us": time.time_ns() // 1000,
            }
        )

    @staticmethod
    def _provider_views(providers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for provider in providers:
            config = provider.get("config") or {}
            report = provider.get("report") or {}
            provider_id = str(config.get("id") or provider.get("id") or "")
            if not provider_id:
                continue
            details = report.get("details") or {}
            process_state = str(
                provider.get("process_state") or provider.get("state") or "unknown"
            )
            process_active = process_state.lower() in {"running", "starting"}
            report_active = (
                str(report.get("residency") or "").upper() == "HOT"
                and str(report.get("health") or "").upper() == "HEALTHY"
                and report.get("ready") is True
                and report.get("expired") is not True
            )
            provider_active = process_active or report_active
            result[provider_id] = {
                "provider_id": provider_id,
                "process_state": process_state,
                "activity_source": (
                    "MANAGER_PROCESS"
                    if process_active
                    else "REGISTERED_PROVIDER_REPORT"
                    if report_active
                    else "NONE"
                ),
                "provider_active": provider_active,
                "residency": (
                    report.get("residency") or provider.get("residency")
                    if provider_active
                    else None
                ),
                "health": report.get("health") if provider_active else None,
                "ready": provider_active and bool(report.get("ready", False)),
                "expired": bool(report.get("expired", False)),
                "last_exit": provider.get("last_exit"),
                "tracking_state": details.get("tracking_state"),
                "last_error": details.get("last_error"),
            }
        return result

    def _required_providers_not_ready(
        self,
        providers: dict[str, dict[str, Any]],
    ) -> list[str]:
        required = {
            "camera": self.config["camera_provider_id"],
            "VIO": self.config["vio_provider_id"],
            "arm": self.config["arm_provider_id"],
        }
        result = []
        for label, provider_id in required.items():
            provider = providers.get(provider_id) or {}
            ready = (
                provider.get("provider_active") is True
                and str(provider.get("residency", "")).upper() == "HOT"
                and str(provider.get("health", "")).upper() == "HEALTHY"
                and provider.get("ready") is True
                and provider.get("expired") is not True
            )
            if not ready:
                health = provider.get("health") or provider.get("process_state") or "not reported"
                result.append(f"{label} ({health})")
        return result

    @staticmethod
    def _provider_was_hot(providers: list[dict[str, Any]], provider_id: str) -> bool:
        for provider in providers:
            config = provider.get("config") or {}
            report = provider.get("report") or {}
            current_id = config.get("id") or provider.get("id")
            if current_id == provider_id:
                residency = str(report.get("residency") or provider.get("residency") or "").upper()
                report_active = (
                    residency == "HOT"
                    and str(report.get("health") or "").upper() == "HEALTHY"
                    and report.get("ready") is True
                    and report.get("expired") is not True
                )
                return report_active
        return False

    async def _verify_arm_stationary(self, allow_active_control_interrupt: bool) -> None:
        first = await self._base_from_tool(None)
        wait_s = float(self.config["base_alignment"]["stationary_window_s"])
        await asyncio.sleep(wait_s)
        second = await self._base_from_tool(None)
        delta = float(np.linalg.norm(first[:3, 3] - second[:3, 3]))
        if delta > 0.003 and not allow_active_control_interrupt:
            raise RuntimeError(
                f"arm tool moved {delta * 1000:.1f} mm during the stationary preflight; "
                "stop active control or explicitly allow interruption"
            )

    async def _base_from_tool(self, at_us: int | None) -> np.ndarray:
        payload = await self.fabric.transform(
            from_frame=self.config["arm_tool_frame"],
            to_frame=self.config["arm_base_frame"],
            at_us=at_us,
            max_extrapolation_us=1_000_000,
        )
        return transform_from_payload(payload)

    def _camera_system_beak(
        self,
        frame: Any,
        vlm: dict[str, Any],
    ) -> tuple[np.ndarray, list[Any]]:
        tip_depths = [
            tip_depth_from_near_cluster(
                frame,
                point,
                self.config["depth"],
                permit_local_minimum=bool(
                    vlm["use_local_depth_minimum"]
                    and vlm["beak_faces_camera"]
                    and not vlm["holding_object"]
                ),
            )
            for point in vlm["beak_points_2d"]
        ]
        return (
            np.mean(
                np.stack(
                    [
                        value.camera_system_xyz_m
                        for value in tip_depths
                    ]
                ),
                axis=0,
            ),
            tip_depths,
        )

    async def _start_foundation_sessions(
        self,
        skill_id: str,
        run_dir: Path,
        attempt: int,
        *,
        include_gripper: bool,
    ) -> dict[str, str]:
        mode_label = "base + gripper" if include_gripper else "base-only"
        await self.progress.update(
            phase="FOUNDATIONPOSE_START",
            message=(
                f"Starting fresh {mode_label} FoundationPose registration "
                f"attempt {attempt} of 2."
            ),
            completed_units=3,
            progress_kind="indeterminate",
            details={"pose_validation": {"attempt": attempt, "maximum_attempts": 2}},
        )
        base_config = self.config["base_alignment"]
        definitions = [
            (
                "base",
                self.config["foundation_base_model_id"],
                self.config["foundation_base_frame"],
                run_dir / "base_mask.png",
                float(base_config["base_update_hz"]),
            )
        ]
        if include_gripper:
            definitions.append(
                (
                    "gripper",
                    self.config["foundation_gripper_model_id"],
                    self.config["foundation_gripper_frame"],
                    run_dir / "gripper_mask.png",
                    float(base_config["gripper_update_hz"]),
                )
            )
        sessions: dict[str, str] = {}
        for role, model_id, child_frame, mask_path, update_hz in definitions:
            session_id = f"{skill_id}-{model_id}-attempt-{attempt}"
            response = await self.manager.provider_request(
                self.config["foundation_pose_provider_id"],
                action="track",
                payload={
                    "session_id": session_id,
                    "model_id": model_id,
                    "target_id": model_id,
                    "child_frame": child_frame,
                    "parent_frame": self.config["camera_frame"],
                    "mask_path": str(mask_path.resolve()),
                    "max_duration_s": float(base_config["hard_timeout_s"]),
                    "max_update_hz": update_hz,
                    "related_skill_id": skill_id,
                },
                request_id=f"request-{session_id}",
                related_skill_id=skill_id,
            )
            sessions[role] = str(
                self._find_value(response, "session_id") or session_id
            )
        self.last_base_pose_engine_lifecycle = {
            "route": self.base_pose_engine_route,
            "execution_host": PROVIDER_EXECUTION_HOST,
            "state": "ACTIVE",
            "owned_session_count_after": len(sessions),
            "owned_sessions": sorted(sessions.values()),
            "gpu_resources_released": False,
            "backend_closed": False,
        }
        return sessions

    async def _collect_skill_local_foundation(
        self,
        *,
        skill_id: str,
        run_dir: Path,
        attempt: int,
        frame: Any,
        keeper: MotionInhibitKeeper,
        include_gripper: bool,
    ) -> tuple[
        dict[str, dict[str, list[np.ndarray]]],
        dict[str, str],
    ]:
        if self.local_foundation_engine is not None:
            raise RuntimeError(
                "a FOUNDATIONPOSE_SKILL base-pose engine is already active"
            )
        foundation_skill_run_id = (
            f"{skill_id}:foundation_pose_object_localization:{attempt}"
        )
        roles = ["base", *(["gripper"] if include_gripper else [])]
        masks: dict[str, np.ndarray] = {}
        for role in roles:
            mask = cv2.imread(
                str(run_dir / f"{role}_mask.png"),
                cv2.IMREAD_GRAYSCALE,
            )
            if mask is None:
                raise RuntimeError(
                    f"Skill-local base-pose mask is unavailable for {role}"
                )
            masks[role] = mask
        base_config = self.config["base_alignment"]
        required_counts = {
            "base": int(base_config["minimum_base_samples"]),
        }
        model_ids = {
            "base": self.config["foundation_base_model_id"],
        }
        if include_gripper:
            required_counts["gripper"] = int(
                base_config["minimum_gripper_samples"]
            )
            model_ids["gripper"] = self.config[
                "foundation_gripper_model_id"
            ]

        async def guard() -> None:
            await self._check_cancel()
            await keeper.ensure_valid()

        async def progress(
            counts: dict[str, int],
            diagnostics: dict[str, Any],
        ) -> None:
            await self.progress.update(
                phase="BASE_POSE_SKILL_LOCAL",
                message=(
                    "The finite calibration Skill is estimating the stationary "
                    "base pose locally; no FoundationPose Provider session is active."
                ),
                progress_kind="indeterminate",
                completed_units=3,
                total_units=8,
                provider_responsive=None,
                provider_sessions=[],
                details={
                    "samples": {
                        **counts,
                        "required_total": sum(required_counts.values()),
                        "received_total": sum(counts.values()),
                    },
                    "base_pose_engine": diagnostics,
                    "pose_validation": {
                        "attempt": attempt,
                        "maximum_attempts": 2,
                        "state": "WAITING_FOR_BASE_POSE",
                    },
                    "motion_inhibit": keeper.status(),
                },
            )
            await self._publish_foundation_skill_status(
                run_id=foundation_skill_run_id,
                parent_skill_id=skill_id,
                attempt=attempt,
                state="RUNNING",
                phase="ESTIMATE_AND_TRACK",
                details={
                    "samples": counts,
                    "required_counts": required_counts,
                    "engine": diagnostics,
                },
            )

        engine_config = self.config.get("base_pose_engine") or {}
        local_config = (
            engine_config.get("foundation_pose_skill")
            or engine_config.get("skill_local")
            or {}
        )
        capture = RgbdCapture(self.fabric, self.config["camera_frame"])
        engine = LocalFoundationPoseEngine.from_config(
            self.config,
            WORKSPACE_ROOT,
        )
        self.local_foundation_engine = engine
        backend_name = getattr(
            engine.backend,
            "name",
            type(engine.backend).__name__,
        )
        self.last_base_pose_engine_lifecycle = {
            "route": SKILL_LOCAL_ROUTE,
            "state": "ACTIVE",
            "backend": backend_name,
            "owned_session_count_after": None,
            "gpu_resources_released": False,
            "backend_closed": False,
        }
        await self._publish_foundation_skill_status(
            run_id=foundation_skill_run_id,
            parent_skill_id=skill_id,
            attempt=attempt,
            state="RUNNING",
            phase="LOAD_BACKEND",
            details={
                "backend": backend_name,
                "engine_route": SKILL_LOCAL_ROUTE,
                "resource_policy": "RELEASE_ON_COMPLETION",
            },
        )
        primary_error: BaseException | None = None
        sessions: dict[str, str] = {}
        try:
            samples, sessions = await engine.collect_samples(
                skill_id=skill_id,
                attempt=attempt,
                initial_frame=frame,
                capture=capture,
                fabric=self.fabric,
                masks=masks,
                model_ids=model_ids,
                required_counts=required_counts,
                hard_timeout_s=float(base_config["hard_timeout_s"]),
                minimum_sample_interval_s=float(
                    local_config.get("minimum_sample_interval_s") or 0.2
                ),
                guard=guard,
                progress=progress,
            )
            return samples, sessions
        except BaseException as error:
            primary_error = error
            raise
        finally:
            close_error: str | None = None
            try:
                await engine.close()
            except Exception as error:
                close_error = str(error)
                if primary_error is None:
                    raise RuntimeError(
                        "FOUNDATIONPOSE_SKILL backend cleanup failed: "
                        f"{error}"
                    ) from error
            finally:
                self.local_foundation_engine = None
                self.last_base_pose_engine_lifecycle = {
                    "route": SKILL_LOCAL_ROUTE,
                    "state": (
                        "CLOSED"
                        if close_error is None
                        else "CLEANUP_FAILED"
                    ),
                    "backend": backend_name,
                    "owned_session_count_after": 0,
                    "owned_sessions": sorted(sessions.values()),
                    "gpu_resources_released": close_error is None,
                    "backend_closed": close_error is None,
                    "cleanup_error": close_error,
                }
                await self._publish_foundation_skill_status(
                    run_id=foundation_skill_run_id,
                    parent_skill_id=skill_id,
                    attempt=attempt,
                    state=(
                        "SUCCEEDED"
                        if primary_error is None and close_error is None
                        else "CANCELLED"
                        if isinstance(primary_error, asyncio.CancelledError)
                        else "FAILED"
                    ),
                    phase="RELEASE_BACKEND",
                    details={
                        "owned_sessions": sorted(sessions.values()),
                        "resources_released": close_error is None,
                        "backend_closed": close_error is None,
                        "cleanup_error": close_error,
                        "error": (
                            None
                            if primary_error is None
                            else str(primary_error)
                        ),
                    },
                )

    async def _wait_for_foundation(
        self,
        *,
        session_ids: dict[str, str],
        attempt: int,
        frame: Any,
        keeper: MotionInhibitKeeper,
    ) -> dict[str, dict[str, list[np.ndarray]]]:
        base_config = self.config["base_alignment"]
        deadline = time.monotonic() + float(base_config["hard_timeout_s"])
        samples: dict[str, dict[str, dict[int, np.ndarray]]] = {
            role: {"vio": {}, "camera": {}} for role in session_ids
        }
        session_roles = {session_id: role for role, session_id in session_ids.items()}
        last_health: dict[str, Any] = {}
        last_camera_frame_number: int | None = None
        last_camera_advance_at = time.monotonic()
        while time.monotonic() < deadline:
            await self._check_cancel()
            await keeper.ensure_valid()
            responsive = True
            try:
                last_health = await self.foundation_health.health()
            except Exception as error:
                responsive = False
                last_health = {"error": str(error)}
            bundle = await self.fabric.latest_optional("camera.rgbd.bundle")
            bundle_data = (bundle or {}).get("data") or {}
            rgb_reference = bundle_data.get("rgb") or {}
            camera_frame_number = int(rgb_reference.get("frame_number", -1))
            if camera_frame_number != last_camera_frame_number:
                last_camera_frame_number = camera_frame_number
                last_camera_advance_at = time.monotonic()
            elif (
                time.monotonic() - last_camera_advance_at
                > float(base_config["maximum_rgbd_stall_s"])
            ):
                raise RuntimeError(
                    "RGB-D stream stopped advancing during FoundationPose "
                    f"at frame {camera_frame_number}"
                )
            observations = await self.fabric.recent("perception.object.pose", limit=128)
            for observation in observations:
                data = observation.get("data") or {}
                session = str(data.get("tracking_session_id") or "")
                role = session_roles.get(session)
                if role is None:
                    continue
                stamp = int(
                    data.get("source_observed_at_us")
                    or observation.get("observed_at_us")
                    or 0
                )
                if stamp in samples[role]["vio"]:
                    continue
                try:
                    camera_from_object = transform_from_payload(data)
                    vio_from_camera = transform_from_payload(
                        await self.fabric.transform(
                            from_frame=self.config["camera_frame"],
                            to_frame=frame.world_frame,
                            at_us=stamp,
                            max_extrapolation_us=750_000,
                            session_epoch=frame.session_epoch,
                        )
                    )
                    samples[role]["camera"][stamp] = camera_from_object
                    samples[role]["vio"][stamp] = vio_from_camera @ camera_from_object
                except Exception:
                    continue
            counts = {
                role: len(role_samples["vio"])
                for role, role_samples in samples.items()
            }
            required_counts = {
                "base": int(base_config["minimum_base_samples"]),
            }
            if "gripper" in session_ids:
                required_counts["gripper"] = int(
                    base_config["minimum_gripper_samples"]
                )
            completed = sum(counts.values())
            required = sum(required_counts.values())
            sessions = self._health_sessions(last_health)
            state_summary = ", ".join(
                f"{item.get('model_id', item.get('session_id', '?'))}:"
                f"{item.get('state', '?')} ({item.get('result_count', 0)} results)"
                for item in sessions
                if str(item.get("session_id")) in session_roles
            )
            await self.progress.update(
                phase="FOUNDATIONPOSE_ALIGNING",
                message=(
                    "FoundationPose is responsive; registration has no native percentage. "
                    f"{state_summary or 'waiting for session telemetry'}"
                    if responsive
                    else "FoundationPose health endpoint is temporarily unreachable."
                ),
                progress_kind="indeterminate",
                completed_units=3,
                total_units=8,
                provider_responsive=responsive,
                provider_sessions=sessions,
                details={
                    "samples": {
                        **counts,
                        "required_total": required,
                        "received_total": completed,
                    },
                    "pose_validation": {
                        "attempt": attempt,
                        "maximum_attempts": 2,
                        "state": "WAITING_FOR_BASE_POSE",
                    },
                    "motion_inhibit": keeper.status(),
                },
            )
            frames = []
            for role, role_samples in samples.items():
                if role_samples["vio"]:
                    frames.append(
                        {
                            "name": f"FoundationPose {role}",
                            "transform": transform_payload(
                                list(role_samples["vio"].values())[-1]
                            ),
                        }
                    )
            await self.artifacts.set_geometry(
                {
                    "coordinate_frame": frame.world_frame,
                    "frames": frames,
                    "points": [],
                }
            )
            if all(counts[role] >= needed for role, needed in required_counts.items()):
                return {
                    role: {
                        basis: list(values.values())
                        for basis, values in role_samples.items()
                    }
                    for role, role_samples in samples.items()
                }
            terminal_failure = [
                item
                for item in sessions
                if str(item.get("session_id")) in session_roles
                and str(item.get("state")) in {"FAILED", "EXPIRED"}
            ]
            if terminal_failure:
                raise RuntimeError(f"FoundationPose session failed: {terminal_failure}")
            await asyncio.sleep(float(base_config["progress_poll_s"]))
        raise TimeoutError(
            "FoundationPose did not produce enough samples before the hard timeout"
        )

    async def _validated_foundation(
        self,
        *,
        skill_id: str,
        alignment_id: str,
        run_dir: Path,
        frame: Any,
        keeper: MotionInhibitKeeper,
        vision: GripperVision | ReviewedFileVision,
        own_sessions: list[str],
        include_gripper: bool,
        base_visual_box_2d: list[int],
        vio_from_camera: np.ndarray,
        gripper_axis_reference: dict[str, Any] | None = None,
    ) -> tuple[
        dict[str, dict[str, list[np.ndarray]]],
        list[dict[str, Any]],
    ]:
        validation_config = self.config["pose_validation"]
        base_config = self.config["base_alignment"]
        maximum_attempts = int(validation_config["maximum_attempts"])
        maximum_scale_mismatch = float(
            validation_config["maximum_projected_box_size_mismatch_fraction"]
        )
        foundation_skill_config = (
            (self.config.get("base_pose_engine") or {}).get(
                "foundation_pose_skill"
            )
            or {}
        )
        configured_model_registry = str(
            foundation_skill_config.get("model_registry")
            or "config/foundation_pose/models.json"
        ).strip()
        model_registry_path = Path(configured_model_registry)
        if not model_registry_path.is_absolute():
            model_registry_path = WORKSPACE_ROOT / model_registry_path
        model_registry_path = model_registry_path.resolve()
        mesh_minimum, mesh_maximum, mesh_from_semantic = load_model_geometry(
            str(WORKSPACE_ROOT),
            self.config["foundation_base_model_id"],
            str(model_registry_path),
        )
        validations: list[dict[str, Any]] = []
        attempt_samples: list[
            dict[str, dict[str, list[np.ndarray]]]
        ] = []
        for attempt in range(1, maximum_attempts + 1):
            if (
                self.foundation_pose_execution_host
                == IN_PROCESS_EXECUTION_HOST
            ):
                samples, session_ids = await self._collect_skill_local_foundation(
                    skill_id=skill_id,
                    run_dir=run_dir,
                    attempt=attempt,
                    frame=frame,
                    keeper=keeper,
                    include_gripper=include_gripper,
                )
            else:
                session_ids = await self._start_foundation_sessions(
                    skill_id,
                    run_dir,
                    attempt,
                    include_gripper=include_gripper,
                )
                own_sessions.extend(session_ids.values())
                samples = await self._wait_for_foundation(
                    session_ids=session_ids,
                    attempt=attempt,
                    frame=frame,
                    keeper=keeper,
                )
            camera_from_base, camera_diagnostics = robust_average_transforms(
                samples["base"]["camera"]
            )
            try:
                camera_system_up = (
                    vio_from_camera[:3, :3].T
                    @ np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
                )
            except Exception:
                camera_system_up = None
            mount_diagnostics = inspect_base_up_alignment(
                camera_from_base,
                camera_system_up=camera_system_up,
                warning_tilt_deg=float(
                    base_config["base_up_warning_tilt_deg"]
                ),
            )
            mount_diagnostics["up_axis_frame"] = frame.camera_frame
            mount_diagnostics["up_axis_source"] = (
                "TIMESTAMPED_VIO_GRAVITY_EXPRESSED_IN_CAMERA_SYSTEM"
                if camera_system_up is not None
                else "UNAVAILABLE"
            )
            overlay, projection = render_pose_overlay(
                frame.rgb,
                camera_from_base,
                frame.intrinsics,
                mesh_minimum,
                mesh_maximum,
                mesh_from_semantic,
                axis_length_m=float(validation_config["axis_length_m"]),
                attempt=attempt,
            )
            overlay_name = f"foundation_pose_attempt_{attempt}_overlay.jpg"
            overlay_path = run_dir / overlay_name
            overlay_path.write_bytes(overlay)
            await self.artifacts.set_overlay(overlay)
            scale_review = projected_visual_scale_review(
                projection,
                base_visual_box_2d,
                frame.rgb.shape,
                maximum_mismatch_fraction=maximum_scale_mismatch,
            )
            artifact = await self._publish_pose_overlay(
                skill_id=skill_id,
                alignment_id=alignment_id,
                frame=frame,
                overlay_path=overlay_path,
                attempt=attempt,
                projection=projection,
            )
            await self.progress.update(
                phase="POSE_VALIDATION",
                message=(
                    f"Base pose attempt {attempt} projected/visual linear "
                    f"scale ratio is "
                    f"{scale_review.get('equivalent_linear_scale_ratio')}."
                ),
                completed_units=5,
                progress_kind="indeterminate",
                details={
                    "samples": {
                        role: len(role_samples["vio"])
                        for role, role_samples in samples.items()
                    },
                    "pose_validation": {
                        "attempt": attempt,
                        "maximum_attempts": maximum_attempts,
                        "state": (
                            "VLM_AXIS_REVIEW"
                            if scale_review["within_tolerance"]
                            else "SIZE_RETRY_REQUIRED"
                        ),
                        "overlay": artifact,
                    },
                    "motion_inhibit": keeper.status(),
                },
            )
            record = {
                "attempt": attempt,
                "accepted": False,
                "acceptance_mode": None,
                "scale_review": scale_review,
                "axis_review": None,
                "orientation_resolution": None,
                "projection": projection,
                "camera_pose_samples": camera_diagnostics,
                "base_up_alignment": mount_diagnostics,
                "raw_base_up_alignment": mount_diagnostics,
                "warnings": [
                    warning
                    for warning in (scale_review.get("warning"),)
                    if warning
                ],
                "foundation_pose_sessions": session_ids,
                "base_pose_engine_route": self.base_pose_engine_route,
                "fresh_registration_from_mask": True,
                "overlay": artifact,
            }
            validations.append(record)
            attempt_samples.append(samples)
            if scale_review["within_tolerance"]:
                selected_overlay, selected_projection = (
                    await self._select_and_apply_base_orientation(
                        samples=samples,
                        vision=vision,
                        frame=frame,
                        attempt=attempt,
                        overlay=overlay,
                        mesh_minimum=mesh_minimum,
                        mesh_maximum=mesh_maximum,
                        mesh_from_semantic=mesh_from_semantic,
                        axis_length_m=float(
                            validation_config["axis_length_m"]
                        ),
                        gripper_axis_reference=gripper_axis_reference,
                        camera_system_up=camera_system_up,
                        model_registry_path=str(model_registry_path),
                    )
                )
                selected_path = run_dir / (
                    f"foundation_pose_attempt_{attempt}_selected_overlay.jpg"
                )
                selected_path.write_bytes(selected_overlay)
                await self.artifacts.set_overlay(selected_overlay)
                record["accepted"] = True
                record["acceptance_mode"] = "SIZE_WITHIN_25_PERCENT"
                record["axis_review"] = selected_projection["axis_review"]
                record["orientation_resolution"] = selected_projection[
                    "orientation_resolution"
                ]
                record["base_up_alignment"] = selected_projection[
                    "base_up_alignment"
                ]
                if selected_projection["orientation_resolution"].get("warning"):
                    record["warnings"].append(
                        selected_projection["orientation_resolution"]["warning"]
                    )
                if selected_projection["base_up_alignment"].get("warning"):
                    record["warnings"].append(
                        selected_projection["base_up_alignment"]["warning"]
                    )
                record["selected_projection"] = selected_projection[
                    "projection"
                ]
                record["overlay"] = await self._publish_pose_overlay(
                    skill_id=skill_id,
                    alignment_id=alignment_id,
                    frame=frame,
                    overlay_path=selected_path,
                    attempt=attempt,
                    projection=selected_projection["projection"],
                    verdict=selected_projection["axis_review"],
                    accepted=True,
                )
            (run_dir / f"foundation_pose_attempt_{attempt}_validation.json").write_text(
                json.dumps(record, indent=2) + "\n",
                encoding="utf-8",
            )
            if record["accepted"]:
                return samples, validations
            if self.foundation_pose_execution_host == PROVIDER_EXECUTION_HOST:
                for session_id in session_ids.values():
                    await self.manager.provider_request(
                        self.config["foundation_pose_provider_id"],
                        action="stop",
                        payload={
                            "session_id": session_id,
                            "reason": (
                                "Projected CAD size differed from the visual "
                                "base by more than 25 percent; reset estimator "
                                "for the one bounded retry"
                            ),
                        },
                        related_skill_id=skill_id,
                    )
            if attempt < maximum_attempts:
                await self.progress.update(
                    phase="POSE_RETRY",
                    message=(
                        "The first projected CAD size differed from the visual "
                        "base by more than 25 percent. Starting the second and "
                        "final fresh FoundationPose attempt."
                    ),
                    details={
                        "pose_validation": {
                            "attempt": attempt,
                            "maximum_attempts": maximum_attempts,
                            "state": "REJECTED_RETRYING",
                            "reset_sessions": list(session_ids.values()),
                            "next_attempt_fresh_registration": True,
                            "scale_review": scale_review,
                            "overlay": artifact,
                        }
                    },
                )
        best_index = select_best_pose_validation(validations)
        best_record = validations[best_index]
        best_record["selected_as_best_attempt"] = True
        best_record["accepted"] = True
        best_record["acceptance_mode"] = (
            "BEST_OF_TWO_SIZE_WARNING"
        )
        best_record["warnings"].append(
            "Both FoundationPose attempts differed from the tight visual "
            "base box by more than 25 percent; the closer attempt was "
            "retained as a warning, not an error."
        )
        best_attempt = int(best_record["attempt"])
        best_overlay_path = run_dir / (
            f"foundation_pose_attempt_{best_attempt}_overlay.jpg"
        )
        best_overlay = best_overlay_path.read_bytes()
        selected_overlay, selected_review = (
            await self._select_and_apply_base_orientation(
                samples=attempt_samples[best_index],
                vision=vision,
                frame=frame,
                attempt=best_attempt,
                overlay=best_overlay,
                mesh_minimum=mesh_minimum,
                mesh_maximum=mesh_maximum,
                mesh_from_semantic=mesh_from_semantic,
                axis_length_m=float(validation_config["axis_length_m"]),
                gripper_axis_reference=gripper_axis_reference,
                camera_system_up=camera_system_up,
                model_registry_path=str(model_registry_path),
            )
        )
        selected_path = run_dir / (
            f"foundation_pose_attempt_{best_attempt}_selected_overlay.jpg"
        )
        selected_path.write_bytes(selected_overlay)
        await self.artifacts.set_overlay(selected_overlay)
        best_record["axis_review"] = selected_review["axis_review"]
        best_record["orientation_resolution"] = selected_review[
            "orientation_resolution"
        ]
        best_record["base_up_alignment"] = selected_review[
            "base_up_alignment"
        ]
        if selected_review["orientation_resolution"].get("warning"):
            best_record["warnings"].append(
                selected_review["orientation_resolution"]["warning"]
            )
        if selected_review["base_up_alignment"].get("warning"):
            best_record["warnings"].append(
                selected_review["base_up_alignment"]["warning"]
            )
        best_record["selected_projection"] = selected_review["projection"]
        best_record["overlay"] = await self._publish_pose_overlay(
            skill_id=skill_id,
            alignment_id=alignment_id,
            frame=frame,
            overlay_path=selected_path,
            attempt=best_attempt,
            projection=selected_review["projection"],
            verdict=selected_review["axis_review"],
            accepted=True,
        )
        (
            run_dir
            / f"foundation_pose_attempt_{best_attempt}_validation.json"
        ).write_text(
            json.dumps(best_record, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "foundation_pose_best_of_two.json").write_text(
            json.dumps(best_record, indent=2) + "\n",
            encoding="utf-8",
        )
        await self.progress.update(
            phase="POSE_VALIDATION",
            message=(
                f"Both size comparisons exceeded 25 percent. Attempt "
                f"{best_attempt} was closer and was retained with a warning."
            ),
            details={
                "pose_validation": {
                    "attempt": best_attempt,
                    "maximum_attempts": maximum_attempts,
                    "state": "BEST_OF_TWO_SIZE_WARNING_ACCEPTED",
                    "scale_review": best_record["scale_review"],
                    "axis_review": best_record["axis_review"],
                    "overlay": best_record["overlay"],
                }
            },
        )
        return attempt_samples[best_index], validations

    async def _select_and_apply_base_orientation(
        self,
        *,
        samples: dict[str, dict[str, list[np.ndarray]]],
        vision: GripperVision | ReviewedFileVision,
        frame: Any,
        attempt: int,
        overlay: bytes,
        mesh_minimum: np.ndarray,
        mesh_maximum: np.ndarray,
        mesh_from_semantic: np.ndarray,
        axis_length_m: float,
        gripper_axis_reference: dict[str, Any] | None = None,
        camera_system_up: np.ndarray | None = None,
        model_registry_path: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        camera_from_base, _ = robust_average_transforms(
            samples["base"]["camera"]
        )
        reference_point = (
            gripper_axis_reference.get("camera_system_xyz_m")
            if isinstance(gripper_axis_reference, dict)
            and gripper_axis_reference.get("available") is True
            else None
        )
        if reference_point is not None:
            correction, axis_review, orientation_resolution = (
                select_base_orientation_from_gripper_point(
                    camera_from_base,
                    camera_system_up,
                    reference_point,
                )
            )
        else:
            axis_review = await vision.validate_base_pose(
                overlay,
                attempt=attempt,
            )
            correction, orientation_resolution = select_base_orientation_correction(
                camera_from_base,
                camera_system_up,
                axis_review["base_x_relation_to_gripper"]
            )
            orientation_resolution["reference_source"] = (
                "VLM_RGB_OVERLAY_FALLBACK"
            )
            if (
                isinstance(gripper_axis_reference, dict)
                and gripper_axis_reference.get("warning")
            ):
                orientation_resolution["warning"] = str(
                    gripper_axis_reference["warning"]
                )
        if orientation_resolution.get("orientation_correction_count") not in {
            0,
            1,
        }:
            raise AssertionError(
                "base orientation must use at most one discrete correction"
            )
        _, application_diagnostics = (
            apply_base_mesh_hypothesis_correction(
                camera_from_base,
                mesh_from_semantic,
                correction,
            )
        )
        orientation_resolution.update(application_diagnostics)
        orientation_resolution["model_registry_path"] = model_registry_path
        for basis in ("camera", "vio"):
            corrected_samples: list[np.ndarray] = []
            for value in samples["base"][basis]:
                corrected, per_sample_diagnostics = (
                    apply_base_mesh_hypothesis_correction(
                        value,
                        mesh_from_semantic,
                        correction,
                    )
                )
                if not per_sample_diagnostics[
                    "mesh_center_translation_preserved"
                ]:
                    raise AssertionError(
                        "base hypothesis selection moved the CAD mesh center"
                    )
                corrected_samples.append(corrected)
            samples["base"][basis] = corrected_samples
        corrected_camera_from_base, _ = robust_average_transforms(
            samples["base"]["camera"]
        )
        selected_overlay, projection = render_pose_overlay(
            frame.rgb,
            corrected_camera_from_base,
            frame.intrinsics,
            mesh_minimum,
            mesh_maximum,
            mesh_from_semantic,
            axis_length_m=axis_length_m,
            attempt=attempt,
        )
        corrected_up_alignment = inspect_base_up_alignment(
            corrected_camera_from_base,
            camera_system_up=camera_system_up,
            warning_tilt_deg=float(
                self.config["base_alignment"]["base_up_warning_tilt_deg"]
            ),
        )
        corrected_up_alignment["up_axis_frame"] = frame.camera_frame
        corrected_up_alignment["up_axis_source"] = (
            "TIMESTAMPED_VIO_GRAVITY_EXPRESSED_IN_CAMERA_SYSTEM"
            if camera_system_up is not None
            else "UNAVAILABLE"
        )
        corrected_up_alignment["orientation_correction_axis"] = (
            orientation_resolution["selected_orientation_correction_axis"]
        )
        corrected_up_alignment["orientation_correction_count"] = (
            orientation_resolution["orientation_correction_count"]
        )
        return selected_overlay, {
            "axis_review": axis_review,
            "orientation_resolution": orientation_resolution,
            "base_up_alignment": corrected_up_alignment,
            "projection": projection,
        }

    @staticmethod
    def _selected_orientation_resolution(
        validations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected = next(
            (
                value.get("orientation_resolution")
                for value in reversed(validations)
                if isinstance(value, dict) and value.get("accepted") is True
            ),
            None,
        )
        if not isinstance(selected, dict):
            raise RuntimeError(
                "accepted FoundationPose result is missing its exact base-orientation review"
            )
        return selected

    async def _finish_base_vlm(
        self,
        *,
        samples: dict[str, dict[str, list[np.ndarray]]],
        validations: list[dict[str, Any]],
        frame: Any,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        arm_is_home: bool,
        keeper: MotionInhibitKeeper,
        vio_from_camera: np.ndarray,
        camera_system_beak: np.ndarray | None = None,
        base_from_tool_for_learning: np.ndarray | None = None,
        tool_learning_warning: str | None = None,
    ) -> dict[str, Any]:
        await keeper.ensure_valid()
        await self.progress.update(
            phase="SOLVING",
            message=(
                "Publishing the FoundationPose base fit with one reviewed "
                "mesh-centered 0/180-degree orientation hypothesis."
            ),
            completed_units=6,
            progress_kind="milestone",
        )
        camera_from_base, camera_diagnostics = robust_average_transforms(
            samples["base"]["camera"]
        )
        vio_from_base, vio_diagnostics = robust_average_transforms(
            samples["base"]["vio"]
        )
        orientation_fit = self._selected_orientation_resolution(validations)
        world_from_vio = np.eye(4, dtype=np.float64)
        world_from_vio[:3, 3] = -vio_from_camera[:3, 3]
        world_from_camera = world_from_vio @ vio_from_camera
        world_from_base = world_from_camera @ camera_from_base
        learned_tool_beak: np.ndarray | None = None
        gripper_measurements: list[dict[str, Any]] = []
        tool_learning: dict[str, Any] = {
            "accepted_for_later_refinement": False,
            "used_in_current_base_transform": False,
            "basis": "POST_HOC_FROM_INDEPENDENT_BASE_POSE",
        }
        if (
            camera_system_beak is not None
            and base_from_tool_for_learning is not None
        ):
            learned_tool_beak, tool_learning = (
                self._bounded_tool_beak_estimate(
                    oriented=vio_from_camera @ camera_from_base,
                    base_from_tool=base_from_tool_for_learning,
                    vio_beak=apply_transform(
                        vio_from_camera,
                        camera_system_beak,
                    ),
                )
            )
            gripper_measurements.append(
                {
                    "source_type": "VLM_RGBD_BEAK",
                    "semantic_point": "FOREMOST_BEAK_MEAN",
                    "position_world_m": apply_transform(
                        world_from_camera,
                        camera_system_beak,
                    ).tolist(),
                    "role": "AUXILIARY_TOOL_GEOMETRY_OBSERVATION",
                    "used_in_alignment": False,
                }
            )
        elif tool_learning_warning:
            tool_learning["warning"] = tool_learning_warning
        vio_drift = self._stationary_camera_vio_drift(
            camera_from_base=camera_from_base,
            vio_from_base=vio_from_base,
            vio_from_camera_reference=vio_from_camera,
        )
        return self._result(
            mode=str(RunMode.FOUNDATION_BASE_VLM_GRIPPER),
            alignment_id=alignment_id,
            skill_id=skill_id,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_base,
            vio_from_camera_reference=vio_from_camera,
            learned_tool_beak=learned_tool_beak,
            gripper_measurements=gripper_measurements,
            diagnostics={
                "base_samples": camera_diagnostics,
                "base_samples_camera": camera_diagnostics,
                "base_samples_vio": vio_diagnostics,
                "stationary_camera_vio_drift": vio_drift,
                "gripper_source": orientation_fit.get(
                    "reference_source",
                    "VLM_RGB_OVERLAY_FALLBACK",
                ),
                "foundation_pose_models": [self.config["foundation_base_model_id"]],
                "foundation_pose_validation": validations,
                "orientation_fit": orientation_fit,
                "base_translation_authority": (
                    "FOUNDATIONPOSE_CENTERED_MESH_THEN_MESH_FROM_SEMANTIC_ROOT"
                ),
                "vlm": vlm,
                "motion_inhibit": keeper.status(),
                "tool_to_beak_learning": tool_learning,
            },
        )

    async def _finish_foundation_dual(
        self,
        *,
        samples: dict[str, dict[str, list[np.ndarray]]],
        validations: list[dict[str, Any]],
        frame: Any,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        arm_is_home: bool,
        keeper: MotionInhibitKeeper,
        vio_from_camera: np.ndarray,
        camera_system_beak: np.ndarray | None = None,
        base_from_tool_for_learning: np.ndarray | None = None,
        tool_learning_warning: str | None = None,
    ) -> dict[str, Any]:
        await keeper.ensure_valid()
        await self.progress.update(
            phase="SOLVING",
            message=(
                "Publishing the FoundationPose base fit with one reviewed "
                "mesh-centered 0/180-degree orientation hypothesis."
            ),
            completed_units=6,
            progress_kind="milestone",
        )
        camera_from_base, base_camera_diagnostics = robust_average_transforms(
            samples["base"]["camera"]
        )
        vio_from_base, base_vio_diagnostics = robust_average_transforms(
            samples["base"]["vio"]
        )
        camera_from_gripper, gripper_camera_diagnostics = (
            robust_average_transforms(samples["gripper"]["camera"])
        )
        vio_from_gripper, gripper_vio_diagnostics = robust_average_transforms(
            samples["gripper"]["vio"]
        )
        orientation_fit = self._selected_orientation_resolution(validations)
        world_from_vio = np.eye(4, dtype=np.float64)
        world_from_vio[:3, 3] = -vio_from_camera[:3, 3]
        world_from_camera = world_from_vio @ vio_from_camera
        world_from_base = world_from_camera @ camera_from_base
        world_foundation_gripper = apply_transform(
            world_from_camera,
            camera_from_gripper[:3, 3],
        )
        learned_tool_beak: np.ndarray | None = None
        gripper_measurements = [
            {
                "source_type": "FOUNDATIONPOSE_GRIPPER_POSE",
                "semantic_point": "GRIPPER_MODEL_ORIGIN",
                "position_world_m": world_foundation_gripper.tolist(),
                "role": "AUXILIARY_OBSERVATION",
                "used_in_alignment": False,
            },
        ]
        tool_learning: dict[str, Any] = {
            "accepted_for_later_refinement": False,
            "used_in_current_base_transform": False,
            "basis": "POST_HOC_FROM_INDEPENDENT_BASE_POSE",
        }
        if (
            camera_system_beak is not None
            and base_from_tool_for_learning is not None
        ):
            learned_tool_beak, tool_learning = (
                self._bounded_tool_beak_estimate(
                    oriented=vio_from_camera @ camera_from_base,
                    base_from_tool=base_from_tool_for_learning,
                    vio_beak=apply_transform(
                        vio_from_camera,
                        camera_system_beak,
                    ),
                )
            )
            gripper_measurements.append(
                {
                    "source_type": "VLM_RGBD_BEAK",
                    "semantic_point": "FOREMOST_BEAK_MEAN",
                    "position_world_m": apply_transform(
                        world_from_camera,
                        camera_system_beak,
                    ).tolist(),
                    "role": "AUXILIARY_TOOL_GEOMETRY_OBSERVATION",
                    "used_in_alignment": False,
                }
            )
        elif tool_learning_warning:
            tool_learning["warning"] = tool_learning_warning
        vio_drift = self._stationary_camera_vio_drift(
            camera_from_base=camera_from_base,
            vio_from_base=vio_from_base,
            vio_from_camera_reference=vio_from_camera,
        )
        return self._result(
            mode=str(RunMode.FOUNDATION_BASE_GRIPPER),
            alignment_id=alignment_id,
            skill_id=skill_id,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_base,
            vio_from_camera_reference=vio_from_camera,
            learned_tool_beak=learned_tool_beak,
            gripper_measurements=gripper_measurements,
            diagnostics={
                "base_samples": base_camera_diagnostics,
                "base_samples_camera": base_camera_diagnostics,
                "base_samples_vio": base_vio_diagnostics,
                "gripper_samples": gripper_camera_diagnostics,
                "gripper_samples_camera": gripper_camera_diagnostics,
                "gripper_samples_vio": gripper_vio_diagnostics,
                "stationary_camera_vio_drift": vio_drift,
                "gripper_source": "FOUNDATIONPOSE_GRIPPER_AUXILIARY_ONLY",
                "gripper_point_observation_sources": [
                    "FOUNDATIONPOSE_GRIPPER_POSE",
                ],
                "foundation_pose_models": [
                    self.config["foundation_base_model_id"],
                    self.config["foundation_gripper_model_id"],
                ],
                "foundation_pose_validation": validations,
                "orientation_fit": orientation_fit,
                "base_translation_authority": (
                    "FOUNDATIONPOSE_CENTERED_MESH_THEN_MESH_FROM_SEMANTIC_ROOT"
                ),
                "vlm": vlm,
                "motion_inhibit": keeper.status(),
                "tool_to_beak_learning": tool_learning,
            },
        )

    @staticmethod
    def _stationary_camera_vio_drift(
        *,
        camera_from_base: np.ndarray,
        vio_from_base: np.ndarray,
        vio_from_camera_reference: np.ndarray,
    ) -> dict[str, Any]:
        """Report VIO drift without allowing it to move a stationary fit."""
        expected_vio_from_base = (
            vio_from_camera_reference @ camera_from_base
        )
        translation_delta = (
            vio_from_base[:3, 3] - expected_vio_from_base[:3, 3]
        )
        rotation_delta = (
            expected_vio_from_base[:3, :3].T
            @ vio_from_base[:3, :3]
        )
        cosine = float(
            np.clip((np.trace(rotation_delta) - 1.0) * 0.5, -1.0, 1.0)
        )
        return {
            "method": "REFERENCE_CAMERA_POSE_VERSUS_ROBUST_VIO_SAMPLES",
            "used_in_alignment": False,
            "alignment_authority": "STATIONARY_REFERENCE_CAMERA",
            "translation_delta_vio_m": translation_delta.tolist(),
            "translation_delta_norm_m": float(
                np.linalg.norm(translation_delta)
            ),
            "rotation_delta_rad": float(np.arccos(cosine)),
            "expected_vio_from_base": transform_payload(
                expected_vio_from_base
            ),
            "measured_vio_from_base": transform_payload(vio_from_base),
        }

    def _bounded_tool_beak_estimate(
        self,
        *,
        oriented: np.ndarray,
        base_from_tool: np.ndarray,
        vio_beak: np.ndarray,
    ) -> tuple[np.ndarray | None, dict[str, Any]]:
        estimate = apply_transform(
            np.linalg.inv(base_from_tool),
            apply_transform(np.linalg.inv(oriented), vio_beak),
        )
        norm_m = float(np.linalg.norm(estimate))
        maximum_norm_m = float(
            self.config["tool_geometry"][
                "maximum_learned_tool_to_beak_norm_m"
            ]
        )
        accepted = norm_m <= maximum_norm_m
        return (
            estimate if accepted else None,
            {
                "translation_m": estimate.tolist(),
                "norm_m": norm_m,
                "maximum_norm_m": maximum_norm_m,
                "accepted_for_later_refinement": accepted,
                "used_in_current_base_transform": False,
                "basis": "POST_HOC_FROM_INDEPENDENT_BASE_POSE",
            },
        )

    async def _vlm_gripper_only(
        self,
        *,
        prior: dict[str, Any],
        frame: Any,
        camera_system_beak: np.ndarray,
        base_from_tool: np.ndarray,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        keeper: MotionInhibitKeeper,
        vision: GripperVision | ReviewedFileVision,
        run_dir: Path,
        vio_from_camera: np.ndarray,
    ) -> dict[str, Any]:
        await keeper.ensure_valid()
        if not self._same_stationary_camera_identity(prior, frame):
            raise RuntimeError(
                "VLM-only refinement requires the same stationary camera "
                "provider, instance, boot, calibration, and frame as its "
                "Manager-verified prior alignment"
            )
        vio_epoch_changed = prior["vio_session_epoch"] != frame.session_epoch
        if (
            self.config["vlm_refine"]["require_same_vio_epoch"]
            and vio_epoch_changed
            and not self._same_stationary_camera_identity(prior, frame)
        ):
            raise RuntimeError(
                "VLM-only refinement cannot cross a VIO session reset unless "
                "the stationary camera identity, boot, calibration, and frame "
                "match exactly"
            )
        learned = prior.get("learned_tool_to_beak_translation_m")
        if learned is None:
            raise RuntimeError("prior alignment has no learned tool-to-beak geometry")
        prior_world_from_base = transform_from_payload(prior["world_from_base"])
        prior_world_from_camera = transform_from_payload(
            prior["world_from_camera_reference"]
        )
        world_from_vio = (
            prior_world_from_camera @ np.linalg.inv(vio_from_camera)
        )
        base_beak = apply_transform(base_from_tool, np.asarray(learned, np.float64))
        rotation_offset = prior_world_from_base[:3, :3] @ base_beak
        world_beak = apply_transform(
            prior_world_from_camera,
            camera_system_beak,
        )
        translation_candidates = [world_beak - rotation_offset]
        beak_candidates = [world_beak]
        vlm_candidates = [vlm]
        estimated_translation = translation_candidates[0]
        delta = estimated_translation - prior_world_from_base[:3, 3]
        magnitude = float(np.linalg.norm(delta))
        trigger = float(self.config["vlm_refine"]["consensus_trigger_m"])
        consensus_diagnostics: dict[str, Any] = {
            "used": False,
            "trigger_m": trigger,
            "initial_translation_delta_norm_m": magnitude,
            "inference_count": 1,
        }
        if magnitude > trigger:
            await self.progress.update(
                phase="VLM_CONSENSUS",
                message=(
                    f"Initial refinement shift is {magnitude:.3f} m. Running two "
                    "additional VLM inferences and selecting the closest pair."
                ),
                completed_units=4,
                progress_kind="indeterminate",
            )
            for inference_index in (2, 3):
                candidate_vlm = await vision.locate(frame.rgb, require_base=False)
                (run_dir / f"vlm_gripper_vote_{inference_index}.json").write_text(
                    json.dumps(candidate_vlm, indent=2) + "\n",
                    encoding="utf-8",
                )
                candidate_camera_system_beak, _ = (
                    self._camera_system_beak(frame, candidate_vlm)
                )
                candidate_world_beak = apply_transform(
                    prior_world_from_camera,
                    candidate_camera_system_beak,
                )
                vlm_candidates.append(candidate_vlm)
                beak_candidates.append(candidate_world_beak)
                translation_candidates.append(
                    candidate_world_beak - rotation_offset
                )
            estimated_translation, pair_diagnostics = closest_pair_consensus(
                translation_candidates
            )
            selected_world_beak = estimated_translation + rotation_offset
            consensus_diagnostics = {
                **consensus_diagnostics,
                **pair_diagnostics,
                "used": True,
                "inference_count": 3,
                "translation_candidates_m": [
                    value.tolist() for value in translation_candidates
                ],
                "beak_candidates_world_m": [
                    value.tolist() for value in beak_candidates
                ],
            }
        else:
            selected_world_beak = world_beak
        delta = estimated_translation - prior_world_from_base[:3, 3]
        magnitude = float(np.linalg.norm(delta))
        world_from_base = prior_world_from_base.copy()
        world_from_base[:3, 3] = estimated_translation
        return self._result(
            mode=str(RunMode.VLM_GRIPPER_ONLY),
            alignment_id=alignment_id,
            skill_id=skill_id,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_base,
            vio_from_camera_reference=vio_from_camera,
            learned_tool_beak=np.asarray(learned, np.float64),
            gripper_measurements=[
                {
                    "source_type": "VLM_RGBD_BEAK",
                    "semantic_point": "FOREMOST_BEAK_MEAN",
                    "position_world_m": selected_world_beak.tolist(),
                    "role": "PRIMARY_ALIGNMENT_INPUT",
                    "used_in_alignment": True,
                }
            ],
            diagnostics={
                "parent_alignment_id": prior["alignment_id"],
                "translation_delta_m": delta.tolist(),
                "translation_delta_norm_m": magnitude,
                "rotation_change_rad": 0.0,
                "foundation_pose_used": False,
                "gripper_source": "VLM_RGBD_BEAK",
                "vlm": vlm,
                "vlm_consensus": consensus_diagnostics,
                "vlm_inferences": vlm_candidates,
                "vio_epoch_bridge": {
                    "used": vio_epoch_changed,
                    "prior_session_epoch": prior["vio_session_epoch"],
                    "current_session_epoch": frame.session_epoch,
                    "basis": (
                        "EXACT_STATIONARY_CAMERA_IDENTITY"
                        if vio_epoch_changed
                        else "SAME_VIO_SESSION"
                    ),
                },
                "motion_inhibit": keeper.status(),
            },
        )

    @staticmethod
    def _same_stationary_camera_identity(
        prior: dict[str, Any],
        frame: Any,
    ) -> bool:
        candidate = prior.get("candidate")
        if not isinstance(candidate, dict):
            return False
        prior_camera = candidate.get("camera_provenance")
        frame_contract = candidate.get("frame_contract")
        observations = getattr(frame, "observations", None)
        route_observation = (
            observations.get("route")
            if isinstance(observations, dict)
            else None
        )
        device_observation = (
            observations.get("device_info")
            if isinstance(observations, dict)
            else None
        )
        device_data = (
            device_observation.get("data")
            if isinstance(device_observation, dict)
            else None
        )
        if (
            not isinstance(prior_camera, dict)
            or not isinstance(frame_contract, dict)
            or not isinstance(route_observation, dict)
        ):
            return False
        stable_comparisons = (
            (
                prior_camera.get("provider_id"),
                route_observation.get("provider_id"),
            ),
            (
                prior_camera.get("calibration_revision"),
                getattr(frame, "calibration_revision", None),
            ),
            (
                frame_contract.get("camera_frame"),
                getattr(frame, "camera_frame", None),
            ),
        )
        prior_device_id = str(
            prior_camera.get("canonical_device_id") or ""
        )
        current_device_id = str(
            device_data.get("canonical_device_id") or ""
        ) if isinstance(device_data, dict) else ""
        if prior_device_id and current_device_id:
            comparisons = (
                *stable_comparisons,
                (prior_device_id, current_device_id),
            )
        else:
            comparisons = (
                *stable_comparisons,
                (
                    prior_camera.get("provider_instance_id"),
                    route_observation.get("provider_instance_id"),
                ),
                (
                    prior_camera.get("boot_id"),
                    route_observation.get("boot_id"),
                ),
            )
        return all(
            bool(str(expected or "").strip())
            and str(expected) == str(observed)
            for expected, observed in comparisons
        )

    def _result(
        self,
        *,
        mode: str,
        alignment_id: str,
        skill_id: str,
        frame: Any,
        world_from_vio: np.ndarray,
        world_from_base: np.ndarray,
        vio_from_camera_reference: np.ndarray,
        learned_tool_beak: np.ndarray | None,
        gripper_measurements: list[dict[str, Any]],
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        world_frame = f"world/stationary_camera/{alignment_id}"
        vio_from_world = np.linalg.inv(world_from_vio)
        vio_from_base = vio_from_world @ world_from_base
        normalized_measurements: list[dict[str, Any]] = []
        geometry_points: list[dict[str, Any]] = []
        source_colors = {
            "VLM_RGBD_BEAK": "#ff4757",
            "FOUNDATIONPOSE_GRIPPER_POSE": "#a855f7",
        }
        for measurement in gripper_measurements:
            normalized = dict(measurement)
            position_world = np.asarray(
                normalized.pop("position_world_m"),
                dtype=np.float64,
            )
            normalized["coordinate_frame"] = world_frame
            normalized["position_m"] = position_world.tolist()
            normalized_measurements.append(normalized)
            geometry_points.append(
                {
                    "name": (
                        f"{normalized['source_type']} / "
                        f"{normalized['semantic_point']}"
                    ),
                    "position_m": apply_transform(
                        vio_from_world,
                        position_world,
                    ).tolist(),
                    "color": source_colors.get(
                        str(normalized["source_type"]),
                        "#facc15",
                    ),
                }
            )
        geometry = {
            "coordinate_frame": frame.world_frame,
            "frames": [
                {"name": "aligned world origin", "transform": transform_payload(vio_from_world)},
                {"name": "arm base", "transform": transform_payload(vio_from_base)},
            ],
            "points": geometry_points,
        }
        measurement_sources = {
            str(item["source_type"]) for item in normalized_measurements
        }
        if {
            "VLM_RGBD_BEAK",
            "FOUNDATIONPOSE_GRIPPER_POSE",
        }.issubset(measurement_sources):
            cross_source_comparison = {
                "applicable": True,
                "directly_comparable": False,
                "reason": (
                    "VLM_RGBD_BEAK is the foremost beak point while "
                    "FOUNDATIONPOSE_GRIPPER_POSE is the gripper model origin; "
                    "apply calibrated tool geometry before computing an offset."
                ),
            }
        else:
            cross_source_comparison = {
                "applicable": False,
                "directly_comparable": False,
                "reason": "This result contains only one gripper measurement source.",
            }
        normalized_diagnostics = dict(diagnostics)
        normalized_diagnostics["base_pose_engine"] = {
            "route": self.base_pose_engine_route,
            "lifecycle": dict(self.last_base_pose_engine_lifecycle),
        }
        created_at_us = time.time_ns() // 1000
        candidate_review = self.config.get("candidate_review") or {}
        review_mode = str(
            candidate_review.get("mode") or "SHADOW"
        ).upper()
        expires_at_us = created_at_us + int(
            float(candidate_review.get("ttl_s") or 900) * 1_000_000
        )
        candidate = self._calibration_candidate(
            alignment_id=alignment_id,
            mode=mode,
            created_at_us=created_at_us,
            expires_at_us=expires_at_us,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_base,
            vio_from_camera_reference=vio_from_camera_reference,
            diagnostics=normalized_diagnostics,
            review_mode=review_mode,
        )
        world_from_camera_reference = (
            world_from_vio @ vio_from_camera_reference
        )
        return {
            "schema": "midbrain.skill.stationary_world_arm_alignment.result",
            "schema_version": 3,
            "alignment_id": alignment_id,
            "skill_id": skill_id,
            "created_at_us": created_at_us,
            "expires_at_us": expires_at_us,
            "mode": mode,
            "world_frame": world_frame,
            "vio_world_frame": frame.world_frame,
            "vio_session_epoch": frame.session_epoch,
            "camera_frame": frame.camera_frame,
            "camera_reference_timestamp_us": frame.timestamp_us,
            "camera_reference_frame_number": frame.frame_number,
            "camera_calibration_revision": frame.calibration_revision,
            "vio_from_camera_reference": transform_payload(
                vio_from_camera_reference
            ),
            "world_from_camera_reference": transform_payload(
                world_from_camera_reference
            ),
            "world_from_vio": transform_payload(world_from_vio),
            "world_from_base": transform_payload(world_from_base),
            "learned_tool_to_beak_translation_m": (
                learned_tool_beak.tolist()
                if learned_tool_beak is not None
                else None
            ),
            "mode_contract": mode_contract(mode),
            "gripper_measurements": normalized_measurements,
            "gripper_cross_source_comparison": cross_source_comparison,
            "valid": True,
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "candidate_review_mode": review_mode,
            "motion_usable": False,
            "candidate": candidate,
            "diagnostics": normalized_diagnostics,
            "monitor_geometry": geometry,
        }

    def _calibration_candidate(
        self,
        *,
        alignment_id: str,
        mode: str,
        created_at_us: int,
        expires_at_us: int,
        frame: Any,
        world_from_vio: np.ndarray,
        world_from_base: np.ndarray,
        vio_from_camera_reference: np.ndarray,
        diagnostics: dict[str, Any],
        review_mode: str,
    ) -> dict[str, Any]:
        frame_observations = getattr(frame, "observations", None)
        observations = (
            frame_observations
            if isinstance(frame_observations, dict)
            else {}
        )
        route_observation = observations.get("route")
        route_data = (
            route_observation.get("data")
            if isinstance(route_observation, dict)
            else None
        )
        routes = (
            route_data.get("routes")
            if isinstance(route_data, dict)
            and isinstance(route_data.get("routes"), list)
            else []
        )
        preferred_route_id = str(
            (
                route_data.get("preferred_route_id")
                if isinstance(route_data, dict)
                else None
            )
            or ""
        )
        selected_route = next(
            (
                route
                for route in routes
                if isinstance(route, dict)
                and str(route.get("route_id") or "")
                == preferred_route_id
            ),
            next(
                (
                    route
                    for route in routes
                    if isinstance(route, dict)
                    and str(route.get("capability") or "")
                    == "camera.rgbd.route.generic_shared_memory"
                ),
                None,
            ),
        )
        bundle_observation = observations.get("bundle")
        device_observation = observations.get("device_info")
        device_data = (
            device_observation.get("data")
            if isinstance(device_observation, dict)
            else None
        )
        vio_status_observation = observations.get("vio_status")
        bundle = (
            bundle_observation.get("data")
            if isinstance(bundle_observation, dict)
            else None
        )
        source_buffer_refs = {
            key: dict(value)
            for key, value in (
                (bundle.items() if isinstance(bundle, dict) else [])
            )
            if isinstance(value, dict)
        }

        confidence_values: list[float] = []
        for validation in diagnostics.get(
            "foundation_pose_validation",
            [],
        ):
            verdict = (
                validation.get("verdict")
                if isinstance(validation, dict)
                else None
            )
            if isinstance(verdict, dict):
                value = verdict.get("confidence")
                if isinstance(value, (int, float)):
                    confidence_values.append(float(value))
        base_samples = diagnostics.get("base_samples") or {}
        translation_bound = base_samples.get(
            "translation_max_residual_m"
        )
        rotation_bound = base_samples.get(
            "rotation_max_residual_rad"
        )
        quality_basis = "ROBUST_SAMPLE_MAX_RESIDUAL"
        quality_provenance: dict[str, Any] = {
            "source": "CURRENT_FOUNDATIONPOSE_SAMPLES",
        }
        ancestor_quality: dict[str, Any] | None = None
        if mode == str(RunMode.VLM_GRIPPER_ONLY):
            ancestor_quality = self._ancestor_candidate_quality(
                diagnostics.get("parent_alignment_id")
            )
            vlm_inferences = diagnostics.get("vlm_inferences")
            if not isinstance(vlm_inferences, list) or not vlm_inferences:
                vlm_inferences = [diagnostics.get("vlm")]
            gripper_confidences = [
                float(inference["gripper"]["confidence"])
                for inference in vlm_inferences
                if isinstance(inference, dict)
                and isinstance(inference.get("gripper"), dict)
                and isinstance(
                    inference["gripper"].get("confidence"),
                    (int, float),
                )
            ]
            if ancestor_quality is not None and gripper_confidences:
                confidence_values.extend(
                    [
                        float(ancestor_quality["confidence"]),
                        min(gripper_confidences),
                    ]
                )
                consensus = diagnostics.get("vlm_consensus") or {}
                consensus_distance = consensus.get(
                    "selected_pair_distance_m"
                )
                translation_floor = float(
                    self.config["vlm_refine"].get(
                        "single_observation_translation_error_bound_m",
                        0.01,
                    )
                )
                translation_components = [
                    translation_floor,
                    float(ancestor_quality["translation_m"]),
                ]
                if isinstance(consensus_distance, (int, float)):
                    translation_components.append(
                        float(consensus_distance) / 2.0
                    )
                translation_bound = max(translation_components)
                rotation_bound = float(
                    ancestor_quality["rotation_rad"]
                )
                quality_basis = (
                    "ANCESTOR_ROTATION_AND_VLM_TRANSLATION_FLOOR"
                )
                quality_provenance = {
                    "source": "VLM_GRIPPER_ONLY_LINEAGE",
                    "ancestor_alignment_id": ancestor_quality[
                        "alignment_id"
                    ],
                    "ancestor_candidate_id": ancestor_quality[
                        "candidate_id"
                    ],
                    "single_observation_translation_error_bound_m": (
                        translation_floor
                    ),
                    "vlm_inference_count": len(gripper_confidences),
                    "vlm_minimum_gripper_confidence": min(
                        gripper_confidences
                    ),
                    "consensus_selected_pair_distance_m": (
                        float(consensus_distance)
                        if isinstance(consensus_distance, (int, float))
                        else None
                    ),
                }
        semantic_alignment = self._semantic_alignment_quality(diagnostics)
        if semantic_alignment is None and ancestor_quality is not None:
            inherited = ancestor_quality.get("semantic_alignment")
            if isinstance(inherited, dict):
                semantic_alignment = {
                    **inherited,
                    "source": "ANCESTOR_REVIEWED_ALIGNMENT",
                    "ancestor_alignment_id": ancestor_quality[
                        "alignment_id"
                    ],
                }
        quality_provenance["semantic_alignment"] = (
            semantic_alignment
            if semantic_alignment is not None
            else {
                "status": "MISSING",
                "reason": (
                    "No current FoundationPose size review and exact "
                    "mesh-centered base-orientation decision is attached to this candidate."
                ),
            }
        )
        return {
            "schema": (
                "midbrain.skill.stationary_world_arm_alignment."
                "calibration_candidate"
            ),
            "schema_version": 3,
            "candidate_id": alignment_id,
            "workcell_calibration_revision": alignment_id,
            "created_at_us": created_at_us,
            "expires_at_us": expires_at_us,
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "review_mode": review_mode,
            "motion_usable": False,
            "method": {
                "skill_version": "0.8.9",
                "base_pose_engine_route": self.base_pose_engine_route,
                "run_mode": mode,
            },
            "frame_contract": {
                "world_frame": f"world/stationary_camera/{alignment_id}",
                "vio_world_frame": frame.world_frame,
                "camera_frame": frame.camera_frame,
                "arm_base_frame": self.config["arm_base_frame"],
                "convention_id": (
                    "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                ),
                "camera_optical_convention_id": (
                    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                ),
                "transform_semantics": "PARENT_FROM_CHILD",
                "legacy_candidate_compatibility": "REJECT",
            },
            "confidence": (
                min(confidence_values) if confidence_values else None
            ),
            "bounded_error_estimate": {
                "translation_m": (
                    float(translation_bound)
                    if isinstance(translation_bound, (int, float))
                    else None
                ),
                "rotation_rad": (
                    float(rotation_bound)
                    if isinstance(rotation_bound, (int, float))
                    else None
                ),
                "basis": quality_basis,
            },
            "quality_provenance": quality_provenance,
            "camera_provenance": {
                "provider_id": (
                    route_observation.get("provider_id")
                    if isinstance(route_observation, dict)
                    else None
                ),
                "provider_instance_id": (
                    route_observation.get("provider_instance_id")
                    if isinstance(route_observation, dict)
                    else None
                ),
                "boot_id": (
                    route_observation.get("boot_id")
                    if isinstance(route_observation, dict)
                    else None
                ),
                "canonical_device_id": (
                    device_data.get("canonical_device_id")
                    if isinstance(device_data, dict)
                    else None
                ),
                "route_id": (
                    selected_route.get("route_id")
                    if isinstance(selected_route, dict)
                    else None
                ),
                "calibration_revision": frame.calibration_revision,
                "reference_timestamp_us": frame.timestamp_us,
                "reference_frame_number": frame.frame_number,
                "source_buffer_refs": source_buffer_refs,
            },
            "vio_provenance": {
                "provider_id": (
                    vio_status_observation.get("provider_id")
                    if isinstance(vio_status_observation, dict)
                    else None
                ),
                "provider_instance_id": (
                    vio_status_observation.get("provider_instance_id")
                    if isinstance(vio_status_observation, dict)
                    else None
                ),
                "boot_id": (
                    vio_status_observation.get("boot_id")
                    if isinstance(vio_status_observation, dict)
                    else None
                ),
                "world_frame": frame.world_frame,
                "session_epoch": frame.session_epoch,
                "reference_timestamp_us": (
                    vio_status_observation.get("observed_at_us")
                    if isinstance(vio_status_observation, dict)
                    else None
                ),
            },
            "transforms": {
                "world_from_camera": transform_payload(
                    world_from_vio @ vio_from_camera_reference
                ),
                "world_from_vio": transform_payload(world_from_vio),
                "world_from_base": transform_payload(world_from_base),
            },
        }

    def _semantic_alignment_quality(
        self,
        diagnostics: dict[str, Any],
    ) -> dict[str, Any] | None:
        validations = diagnostics.get("foundation_pose_validation") or []
        accepted = next(
            (
                value
                for value in reversed(validations)
                if isinstance(value, dict)
                and value.get("accepted") is True
            ),
            None,
        )
        orientation_fit = diagnostics.get("orientation_fit")
        if not isinstance(accepted, dict) or not isinstance(orientation_fit, dict):
            return None
        axis_review = accepted.get("axis_review")
        scale_review = accepted.get("scale_review")
        base_up_alignment = accepted.get("base_up_alignment")
        relation = (
            axis_review.get("base_x_relation_to_gripper")
            if isinstance(axis_review, dict)
            else None
        )
        selected_flip = orientation_fit.get("selected_flip_deg")
        fitted_yaw = orientation_fit.get("fitted_yaw_deg")
        correction_translation = orientation_fit.get(
            "yaw_correction_translation_norm_m"
        )
        correction_axis = orientation_fit.get(
            "selected_orientation_correction_axis"
        )
        correction_deg = orientation_fit.get(
            "selected_orientation_correction_deg"
        )
        correction_count = orientation_fit.get("orientation_correction_count")
        orientation_translation = orientation_fit.get(
            "orientation_correction_translation_norm_m"
        )
        application_origin = orientation_fit.get("application_origin")
        application_order = orientation_fit.get("application_order")
        mesh_correction_translation = orientation_fit.get(
            "mesh_hypothesis_correction_translation_norm_m"
        )
        mesh_center_preserved = orientation_fit.get(
            "mesh_center_translation_preserved"
        )
        root_adjustment = orientation_fit.get(
            "semantic_root_translation_adjustment_norm_m"
        )
        world_up_available = orientation_fit.get("world_up_available")
        raw_z_dot_up = orientation_fit.get("raw_base_z_dot_world_up")
        corrected_z_dot_up = orientation_fit.get(
            "corrected_base_z_dot_world_up"
        )
        inspected_corrected_z_dot_up = (
            base_up_alignment.get("base_z_dot_world_up")
            if isinstance(base_up_alignment, dict)
            else None
        )
        needs_upright_flip = (
            isinstance(raw_z_dot_up, (int, float))
            and math.isfinite(float(raw_z_dot_up))
            and float(raw_z_dot_up) < 0.0
        )
        needs_x_flip = relation == "AWAY_FROM_GRIPPER"
        expected_correction_axis = {
            (False, False): "NONE",
            (False, True): "Z",
            (True, False): "X",
            (True, True): "Y",
        }[(needs_upright_flip, needs_x_flip)]
        expected_correction_count = (
            0 if expected_correction_axis == "NONE" else 1
        )
        expected_correction_deg = 0 if expected_correction_count == 0 else 180
        if (
            relation
            not in {"TOWARD_GRIPPER", "AWAY_FROM_GRIPPER", "UNCLEAR"}
            or selected_flip not in {0, 180}
            or not isinstance(fitted_yaw, (int, float))
            or abs(float(fitted_yaw) - float(selected_flip)) > 1e-9
            or not isinstance(correction_translation, (int, float))
            or abs(float(correction_translation)) > 1e-9
            or world_up_available is not True
            or not isinstance(raw_z_dot_up, (int, float))
            or not math.isfinite(float(raw_z_dot_up))
            or not isinstance(corrected_z_dot_up, (int, float))
            or not math.isfinite(float(corrected_z_dot_up))
            or float(corrected_z_dot_up) < -1e-9
            or not isinstance(inspected_corrected_z_dot_up, (int, float))
            or not math.isfinite(float(inspected_corrected_z_dot_up))
            or float(inspected_corrected_z_dot_up) < -1e-9
            or correction_axis != expected_correction_axis
            or correction_deg != expected_correction_deg
            or correction_count != expected_correction_count
            or not isinstance(orientation_translation, (int, float))
            or not math.isfinite(float(orientation_translation))
            or abs(float(orientation_translation)) > 1e-9
            or application_origin
            != "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN"
            or application_order
            != (
                "parent_from_mesh @ mesh_hypothesis_correction @ "
                "mesh_from_semantic"
            )
            or not isinstance(mesh_correction_translation, (int, float))
            or not math.isfinite(float(mesh_correction_translation))
            or abs(float(mesh_correction_translation)) > 1e-9
            or mesh_center_preserved is not True
            or not isinstance(root_adjustment, (int, float))
            or not math.isfinite(float(root_adjustment))
            or float(root_adjustment) < 0.0
        ):
            return None
        warnings = list(accepted.get("warnings") or [])
        if (
            orientation_fit.get("warning")
            and orientation_fit["warning"] not in warnings
        ):
            warnings.append(str(orientation_fit["warning"]))
        yaw_source = str(
            orientation_fit.get("reference_source")
            or "VLM_RGB_OVERLAY_FALLBACK"
        )
        result: dict[str, Any] = {
            "status": "PASSED_WITH_WARNINGS" if warnings else "PASSED",
            "source": (
                "CURRENT_FOUNDATIONPOSE_PROJECTED_SCALE_AND_"
                f"{yaw_source}_SINGLE_DISCRETE_BASE_ORIENTATION"
            ),
            "base_x_relation_to_gripper": relation,
            "selected_base_yaw_flip_deg": int(selected_flip),
            "fitted_base_yaw_deg": float(fitted_yaw),
            "yaw_correction_translation_norm_m": float(
                correction_translation
            ),
            "world_up_available": True,
            "raw_base_z_dot_world_up": float(raw_z_dot_up),
            "corrected_base_z_dot_world_up": float(
                inspected_corrected_z_dot_up
            ),
            "orientation_resolution_corrected_base_z_dot_world_up": float(
                corrected_z_dot_up
            ),
            "upright_hemisphere_flip_required": needs_upright_flip,
            "selected_orientation_correction_axis": correction_axis,
            "selected_orientation_correction_deg": int(correction_deg),
            "orientation_correction_count": int(correction_count),
            "orientation_correction_translation_norm_m": float(
                orientation_translation
            ),
            "orientation_application_origin": application_origin,
            "orientation_application_order": application_order,
            "mesh_hypothesis_correction_translation_norm_m": float(
                mesh_correction_translation
            ),
            "mesh_center_translation_preserved": True,
            "semantic_root_translation_adjustment_norm_m": float(
                root_adjustment
            ),
            "foundation_pose_attempt_count": len(validations),
            "acceptance_mode": accepted.get("acceptance_mode"),
            "size_warning_accepted": (
                accepted.get("acceptance_mode")
                == "BEST_OF_TWO_SIZE_WARNING"
            ),
            "warnings": warnings,
        }
        if isinstance(scale_review, dict):
            result.update(
                {
                    "projected_visual_linear_scale_ratio": scale_review.get(
                        "equivalent_linear_scale_ratio"
                    ),
                    "projected_visual_scale_mismatch_fraction": scale_review.get(
                        "mismatch_fraction"
                    ),
                    "maximum_projected_box_size_mismatch_fraction": (
                        scale_review.get("maximum_mismatch_fraction")
                    ),
                    "projected_visual_scale_within_tolerance": (
                        scale_review.get("within_tolerance")
                    ),
                }
            )
        if isinstance(base_up_alignment, dict):
            result.update(
                {
                    "base_up_status": base_up_alignment.get("status"),
                    "world_up_available": base_up_alignment.get(
                        "world_up_available"
                    ),
                    "base_z_dot_world_up": base_up_alignment.get(
                        "base_z_dot_world_up"
                    ),
                    "base_z_tilt_from_world_up_deg": base_up_alignment.get(
                        "base_z_tilt_from_world_up_deg"
                    ),
                    "base_up_warning_tilt_deg": base_up_alignment.get(
                        "warning_tilt_deg"
                    ),
                }
            )
        return result

    def _ancestor_candidate_quality(
        self,
        parent_alignment_id: Any,
    ) -> dict[str, Any] | None:
        """Find the nearest finite candidate quality record in the lineage."""

        current_id = str(parent_alignment_id or "").strip()
        visited: set[str] = set()
        while current_id and current_id not in visited and len(visited) < 16:
            visited.add(current_id)
            result = self.store.get(current_id)
            if not isinstance(result, dict):
                return None
            candidate = result.get("candidate")
            bounds = (
                candidate.get("bounded_error_estimate")
                if isinstance(candidate, dict)
                else None
            )
            confidence = (
                candidate.get("confidence")
                if isinstance(candidate, dict)
                else None
            )
            translation = (
                bounds.get("translation_m")
                if isinstance(bounds, dict)
                else None
            )
            rotation = (
                bounds.get("rotation_rad")
                if isinstance(bounds, dict)
                else None
            )
            values = (confidence, translation, rotation)
            if all(
                isinstance(value, (int, float))
                and np.isfinite(float(value))
                for value in values
            ):
                return {
                    "alignment_id": str(
                        result.get("alignment_id") or current_id
                    ),
                    "candidate_id": str(
                        candidate.get("candidate_id") or current_id
                    ),
                    "confidence": float(confidence),
                    "translation_m": float(translation),
                    "rotation_rad": float(rotation),
                    "semantic_alignment": (
                        (
                            candidate.get("quality_provenance") or {}
                        ).get("semantic_alignment")
                        if isinstance(candidate, dict)
                        else None
                    ),
                }
            diagnostics = result.get("diagnostics")
            current_id = str(
                (
                    diagnostics.get("parent_alignment_id")
                    if isinstance(diagnostics, dict)
                    else None
                )
                or ""
            ).strip()
        return None

    def _prior_alignment_review_usable(
        self,
        prior: dict[str, Any],
        workcell_calibrations: dict[str, Any],
    ) -> bool:
        candidate = prior.get("candidate")
        frame_contract = (
            candidate.get("frame_contract")
            if isinstance(candidate, dict)
            else None
        )
        if (
            not isinstance(candidate, dict)
            or candidate.get("schema_version") != 3
            or not isinstance(frame_contract, dict)
            or frame_contract.get("convention_id")
            != "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
            or frame_contract.get("camera_optical_convention_id")
            != "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            or frame_contract.get("legacy_candidate_compatibility")
            != "REJECT"
            or (
                (candidate.get("quality_provenance") or {}).get(
                    "semantic_alignment",
                    {},
                ).get("status")
                not in {"PASSED", "PASSED_WITH_WARNINGS"}
            )
        ):
            return False
        review_mode = str(
            (self.config.get("candidate_review") or {}).get("mode")
            or "SHADOW"
        ).upper()
        if review_mode == "SHADOW":
            return True
        candidate_id = str(
            (prior.get("candidate") or {}).get("candidate_id")
            or prior.get("alignment_id")
            or ""
        )
        session_epoch = str(prior.get("vio_session_epoch") or "")
        for activation in workcell_calibrations.get("activations") or []:
            if (
                activation.get("state") == "ACTIVE"
                and activation.get("motion_usable") is True
                and str(activation.get("candidate_id") or "") == candidate_id
                and str(activation.get("session_epoch") or "") == session_epoch
                and activation.get("validity_policy")
                == "MOUNTED_IDENTITY_TRACKING_GATED_V1"
            ):
                return True
        return False

    def _manager_verified_prior_alignment(
        self,
        workcell_calibrations: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Select a prior that Manager previously activated after exact review."""

        review_mode = str(
            (self.config.get("candidate_review") or {}).get("mode")
            or "SHADOW"
        ).upper()
        if review_mode == "SHADOW":
            return self.store.latest()

        activations = sorted(
            workcell_calibrations.get("activations") or [],
            key=lambda activation: (
                str(activation.get("activated_at") or ""),
            ),
            reverse=True,
        )
        for activation in activations:
            if str(activation.get("state") or "") not in {
                "ACTIVE",
                "SUPERSEDED",
                "INVALIDATED",
            }:
                continue
            if str(activation.get("enforcement") or "") != "ENFORCED":
                continue
            if not str(activation.get("review_decision_id") or "").strip():
                continue
            candidate_id = str(activation.get("candidate_id") or "").strip()
            candidate_sha256 = str(
                activation.get("candidate_sha256") or ""
            ).strip()
            if not candidate_id or not candidate_sha256:
                continue
            prior = self.store.get(candidate_id)
            if prior is None:
                continue
            candidate = prior.get("candidate")
            if not isinstance(candidate, dict):
                continue
            frame_contract = candidate.get("frame_contract")
            if (
                candidate.get("schema_version") != 3
                or not isinstance(frame_contract, dict)
                or frame_contract.get("convention_id")
                != "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
                or frame_contract.get("camera_optical_convention_id")
                != "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
                or frame_contract.get("legacy_candidate_compatibility")
                != "REJECT"
            ):
                continue
            if str(candidate.get("candidate_id") or "") != candidate_id:
                continue
            if canonical_sha256(candidate) != candidate_sha256:
                continue
            return prior
        return None

    async def _publish_pose_overlay(
        self,
        *,
        skill_id: str,
        alignment_id: str,
        frame: Any,
        overlay_path: Path,
        attempt: int,
        projection: dict[str, Any],
        verdict: dict[str, Any] | None = None,
        accepted: bool | None = None,
    ) -> dict[str, Any]:
        host = str(self.config["gui"]["host"])
        public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        address = (
            f"http://{public_host}:{int(self.config['gui']['port'])}"
            f"/api/pose-overlay/{alignment_id}/{attempt}"
        )
        artifact = {
            "kind": "RGB_WITH_PROJECTED_BASE_3D_BOX_AND_XYZ_ARROWS",
            "media_type": "image/jpeg",
            "address": address,
            "local_path": str(overlay_path.resolve()),
            "attempt": attempt,
            "alignment_id": alignment_id,
            "projection": projection,
            "validation_state": "COMPLETE" if verdict is not None else "PENDING",
        }
        if verdict is not None:
            artifact["accepted"] = bool(accepted)
            artifact["verdict"] = verdict
        now_us = time.time_ns() // 1000
        await self.fabric.publish(
            self._observation(
                stream="skills.stationary_world_arm_alignment.pose_overlay",
                schema="physical_agent.image_artifact",
                observed_at_us=now_us,
                coordinate_frame=frame.camera_frame,
                data=artifact,
                related_skill_id=skill_id,
            )
        )
        return artifact

    async def _publish_result(self, result: dict[str, Any]) -> None:
        now_us = time.time_ns() // 1000
        self.sequence += 1
        review_mode = str(
            result.get("candidate_review_mode") or "SHADOW"
        ).upper()
        transform_suffix = ".candidate" if review_mode == "ENFORCED" else ""
        observations = [
            self._observation(
                stream=f"transform.stationary_world.vio{transform_suffix}",
                schema="physical_agent.transform",
                observed_at_us=now_us,
                coordinate_frame=result["world_frame"],
                data={
                    "parent_frame": result["world_frame"],
                    "child_frame": result["vio_world_frame"],
                    **result["world_from_vio"],
                    "is_static": True,
                    "authority": "skill.stationary_world_arm_alignment",
                    "session_epoch": result["vio_session_epoch"],
                    "calibration_revision": result["alignment_id"],
                    "continuity": "CALIBRATION",
                    "review_state": result["review_state"],
                    "motion_usable": False,
                    "expires_at_us": result["expires_at_us"],
                },
                related_skill_id=result["skill_id"],
            ),
            self._observation(
                stream=(
                    f"transform.stationary_world.arm_base{transform_suffix}"
                ),
                schema="physical_agent.transform",
                observed_at_us=now_us,
                coordinate_frame=result["world_frame"],
                data={
                    "parent_frame": result["world_frame"],
                    "child_frame": self.config["arm_base_frame"],
                    **result["world_from_base"],
                    "is_static": True,
                    "authority": "skill.stationary_world_arm_alignment",
                    "session_epoch": result["vio_session_epoch"],
                    "calibration_revision": result["alignment_id"],
                    "continuity": "CALIBRATION",
                    "review_state": result["review_state"],
                    "motion_usable": False,
                    "expires_at_us": result["expires_at_us"],
                },
                related_skill_id=result["skill_id"],
            ),
            self._observation(
                stream="skills.stationary_world_arm_alignment.result",
                schema=result["schema"],
                observed_at_us=now_us,
                coordinate_frame=result["world_frame"],
                data=result,
                related_skill_id=result["skill_id"],
            ),
        ]
        await self.fabric.publish_batch(observations)

    async def _publish_foundation_skill_status(
        self,
        *,
        run_id: str,
        parent_skill_id: str,
        attempt: int,
        state: str,
        phase: str,
        details: dict[str, Any],
    ) -> None:
        now_us = time.time_ns() // 1000
        self.sequence = getattr(self, "sequence", 0) + 1
        try:
            await self.fabric.publish(
                {
                    "schema": "physical_agent.skill_status",
                    "schema_version": 1,
                    "stream": (
                        "skills.foundation_pose_object_localization.status"
                    ),
                    "provider_id": (
                        "skill.foundation_pose_object_localization"
                    ),
                    "provider_instance_id": run_id,
                    "boot_id": run_id,
                    "sequence": self.sequence,
                    "observed_at_us": now_us,
                    "freshness_ms": None,
                    "related_skill_id": parent_skill_id,
                    "valid": state not in {"FAILED", "CANCELLED"},
                    "data": {
                        "skill_id": run_id,
                        "skill": "foundation_pose_object_localization",
                        "parent_skill_id": parent_skill_id,
                        "attempt": attempt,
                        "state": state,
                        "phase": phase,
                        "updated_at_us": now_us,
                        "details": details,
                    },
                }
            )
        except Exception:
            # Parent execution and cleanup remain authoritative when Fabric is
            # temporarily unavailable.
            pass

    def _observation(
        self,
        *,
        stream: str,
        schema: str,
        observed_at_us: int,
        coordinate_frame: str,
        data: dict[str, Any],
        related_skill_id: str,
    ) -> dict[str, Any]:
        self.sequence += 1
        return {
            "schema": schema,
            "schema_version": 1,
            "stream": stream,
            "provider_id": "skill.stationary_world_arm_alignment",
            "provider_instance_id": related_skill_id,
            "boot_id": related_skill_id,
            "sequence": self.sequence,
            "observed_at_us": observed_at_us,
            "freshness_ms": None,
            "coordinate_frame": coordinate_frame,
            "related_skill_id": related_skill_id,
            "valid": True,
            "data": data,
        }

    @staticmethod
    def _find_value(value: Any, key: str) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for child in value.values():
                found = AlignmentSkill._find_value(child, key)
                if found is not None:
                    return found
        if isinstance(value, list):
            for child in value:
                found = AlignmentSkill._find_value(child, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _health_sessions(health: dict[str, Any]) -> list[dict[str, Any]]:
        value = health.get("sessions") or (health.get("details") or {}).get("sessions") or []
        if isinstance(value, dict):
            return [
                {"session_id": session_id, **session}
                for session_id, session in value.items()
                if isinstance(session, dict)
            ]
        return [item for item in value if isinstance(item, dict)]

    async def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError

    async def close(self) -> None:
        closers = [
            self.manager.close(),
            self.fabric.close(),
            self.foundation_health.close(),
        ]
        if self.local_foundation_engine is not None:
            closers.append(self.local_foundation_engine.close())
        await asyncio.gather(*closers)
