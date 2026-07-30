from __future__ import annotations

import asyncio
import json
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
    apply_transform,
    base_upright_correction,
    choose_base_symmetry,
    closest_pair_consensus,
    robust_average_transforms,
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
    pose_verdict_best_of_two_acceptable,
    pose_verdict_accepted,
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
                prior_is_upright = False
                if prior_is_current:
                    try:
                        prior_base = transform_from_payload(prior["world_from_base"])
                        prior_is_upright = float(
                            np.dot(prior_base[:3, 2], [0.0, 1.0, 0.0])
                        ) > 0.5
                    except Exception:
                        prior_is_upright = False
                selected_mode = (
                    RunMode.VLM_GRIPPER_ONLY
                    if prior_is_current and prior_is_upright
                    else RunMode.FOUNDATION_BASE_VLM_GRIPPER
                )
                await self.progress.update(
                    mode=str(selected_mode),
                    message=(
                        f"Auto selected {selected_mode}; prior VIO epoch current="
                        f"{prior_is_current}, prior base upright={prior_is_upright}."
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
            camera_beak, tip_depths = self._camera_beak(frame, vlm)
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
            vio_beak = apply_transform(vio_from_camera, camera_beak)
            base_from_tool = await self._base_from_tool(frame.timestamp_us)
            if selected_mode == RunMode.VLM_GRIPPER_ONLY:
                if not prior:
                    raise RuntimeError(
                        "VLM gripper-only alignment requires a prior alignment"
                    )
                result = await self._vlm_gripper_only(
                    prior=prior,
                    frame=frame,
                    camera_beak=camera_beak,
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
                    vio_beak=vio_beak,
                    keeper=keeper,
                    vision=vision,
                    own_sessions=own_sessions,
                    include_gripper=use_foundation_gripper,
                )
                finish = (
                    self._finish_foundation_dual
                    if use_foundation_gripper
                    else self._finish_base_vlm
                )
                result = await finish(
                        samples=samples,
                        validations=validations,
                        frame=frame,
                        vio_beak=vio_beak,
                        base_from_tool=base_from_tool,
                        alignment_id=alignment_id,
                        skill_id=skill_id,
                        vlm=vlm,
                        arm_is_home=arm_is_home,
                        keeper=keeper,
                        vio_from_camera=vio_from_camera,
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
            await self.progress.update(
                state=state,
                phase=str(state),
                message=str(error),
                details={
                    "error_type": type(error).__name__,
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

    def _camera_beak(self, frame: Any, vlm: dict[str, Any]) -> tuple[np.ndarray, list[Any]]:
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
                np.stack([value.camera_xyz_m for value in tip_depths]),
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
        vio_beak: np.ndarray,
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
                    "points": [
                        {
                            "name": "VLM beak",
                            "position_m": vio_beak.tolist(),
                            "color": "#ff9f43",
                        }
                    ],
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
        vio_beak: np.ndarray,
        keeper: MotionInhibitKeeper,
        vision: GripperVision,
        own_sessions: list[str],
        include_gripper: bool,
    ) -> tuple[
        dict[str, dict[str, list[np.ndarray]]],
        list[dict[str, Any]],
    ]:
        validation_config = self.config["pose_validation"]
        maximum_attempts = 2
        minimum_confidence = float(validation_config["minimum_confidence"])
        fallback_minimum_confidence = float(
            validation_config["best_of_two_fallback_minimum_confidence"]
        )
        mesh_minimum, mesh_maximum, mesh_from_semantic = load_model_geometry(
            str(WORKSPACE_ROOT),
            self.config["foundation_base_model_id"],
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
                    vio_beak=vio_beak,
                    keeper=keeper,
                )
            raw_vio_from_base, _ = robust_average_transforms(
                samples["base"]["vio"]
            )
            upright_correction, upright_diagnostics = base_upright_correction(
                raw_vio_from_base
            )
            for basis in ("vio", "camera"):
                samples["base"][basis] = [
                    transform @ upright_correction
                    for transform in samples["base"][basis]
                ]
            camera_from_base, camera_diagnostics = robust_average_transforms(
                samples["base"]["camera"]
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
                    f"VLM is checking base pose attempt {attempt} against the "
                    "eight-angle CAD atlas."
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
                        "state": "VLM_CHECKING",
                        "overlay": artifact,
                    },
                    "motion_inhibit": keeper.status(),
                },
            )
            verdict = await vision.validate_base_pose(overlay, attempt=attempt)
            accepted = pose_verdict_accepted(verdict, minimum_confidence)
            artifact = await self._publish_pose_overlay(
                skill_id=skill_id,
                alignment_id=alignment_id,
                frame=frame,
                overlay_path=overlay_path,
                attempt=attempt,
                projection=projection,
                verdict=verdict,
                accepted=accepted,
            )
            record = {
                "attempt": attempt,
                "accepted": accepted,
                "minimum_confidence": minimum_confidence,
                "verdict": verdict,
                "projection": projection,
                "camera_pose_samples": camera_diagnostics,
                "upright_normalization": upright_diagnostics,
                "foundation_pose_sessions": session_ids,
                "base_pose_engine_route": self.base_pose_engine_route,
                "fresh_registration_from_mask": True,
                "overlay": artifact,
            }
            validations.append(record)
            attempt_samples.append(samples)
            (run_dir / f"foundation_pose_attempt_{attempt}_validation.json").write_text(
                json.dumps(record, indent=2) + "\n",
                encoding="utf-8",
            )
            if accepted:
                return samples, validations
            if self.foundation_pose_execution_host == PROVIDER_EXECUTION_HOST:
                for session_id in session_ids.values():
                    await self.manager.provider_request(
                        self.config["foundation_pose_provider_id"],
                        action="stop",
                        payload={
                            "session_id": session_id,
                            "reason": (
                                "VLM rejected projected base pose; reset estimator "
                                "before fresh registration"
                            ),
                        },
                        related_skill_id=skill_id,
                    )
            if attempt < maximum_attempts:
                await self.progress.update(
                    phase="POSE_RETRY",
                    message=(
                        "VLM rejected the first projected base box/axes. The "
                        "The base-pose estimator was reset; starting a new "
                        "finite session and a fresh register-from-mask attempt."
                    ),
                    details={
                        "pose_validation": {
                            "attempt": attempt,
                            "maximum_attempts": maximum_attempts,
                            "state": "REJECTED_RETRYING",
                            "reset_sessions": list(session_ids.values()),
                            "next_attempt_fresh_registration": True,
                            "verdict": verdict,
                            "overlay": artifact,
                        }
                    },
                )
        best_index = select_best_pose_validation(validations)
        best_record = validations[best_index]
        best_record["selected_as_best_attempt"] = True
        best_record["strict_acceptance"] = False
        best_verdict = best_record["verdict"]
        best_overlay_path = Path(
            best_record["overlay"]["local_path"]
        )
        if best_overlay_path.is_file():
            await self.artifacts.set_overlay(best_overlay_path.read_bytes())
        if pose_verdict_best_of_two_acceptable(
            best_verdict,
            fallback_minimum_confidence,
        ):
            best_record["accepted"] = True
            best_record["acceptance_mode"] = "BEST_OF_TWO_FALLBACK"
            best_record["fallback_minimum_confidence"] = (
                fallback_minimum_confidence
            )
            (run_dir / "foundation_pose_best_of_two.json").write_text(
                json.dumps(best_record, indent=2) + "\n",
                encoding="utf-8",
            )
            await self.progress.update(
                phase="POSE_VALIDATION",
                message=(
                    f"Neither attempt met the strict threshold; attempt "
                    f"{best_record['attempt']} was retained as the bounded "
                    "best-of-two acceptable result."
                ),
                details={
                    "pose_validation": {
                        "attempt": best_record["attempt"],
                        "maximum_attempts": maximum_attempts,
                        "state": "BEST_OF_TWO_FALLBACK_ACCEPTED",
                        "strict_minimum_confidence": minimum_confidence,
                        "fallback_minimum_confidence": (
                            fallback_minimum_confidence
                        ),
                        "verdict": best_verdict,
                        "overlay": best_record["overlay"],
                    }
                },
            )
            return attempt_samples[best_index], validations
        best_record["acceptance_mode"] = "BEST_OF_TWO_REJECTED"
        (run_dir / "foundation_pose_best_of_two.json").write_text(
            json.dumps(best_record, indent=2) + "\n",
            encoding="utf-8",
        )
        await self.progress.update(
            phase="POSE_VALIDATION",
            message=(
                f"Both pose attempts were rejected. Attempt "
                f"{best_record['attempt']} is retained as the better diagnostic "
                "result, but it is not safe to publish as a valid alignment."
            ),
            details={
                "pose_validation": {
                    "attempt": best_record["attempt"],
                    "maximum_attempts": maximum_attempts,
                    "state": "BEST_OF_TWO_REJECTED",
                    "verdict": best_verdict,
                    "overlay": best_record["overlay"],
                }
            },
        )
        reasons = best_verdict.get("reasons", [])
        raise RuntimeError(
            "VLM rejected both projected FoundationPose base attempts; "
            f"attempt {best_record['attempt']} was better but still failed "
            "the absolute geometry gate: "
            + ("; ".join(reasons) if reasons else "pose or 3D box was unreasonable")
        )

    async def _finish_base_vlm(
        self,
        *,
        samples: dict[str, dict[str, list[np.ndarray]]],
        validations: list[dict[str, Any]],
        frame: Any,
        vio_beak: np.ndarray,
        base_from_tool: np.ndarray,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        arm_is_home: bool,
        keeper: MotionInhibitKeeper,
        vio_from_camera: np.ndarray,
    ) -> dict[str, Any]:
        await keeper.ensure_valid()
        await self.progress.update(
            phase="SOLVING",
            message="Resolving the base's 0/180-degree symmetry and fusing stationary measurements.",
            completed_units=6,
            progress_kind="milestone",
        )
        vio_from_base, diagnostics = robust_average_transforms(samples["base"]["vio"])
        if base_from_tool is None and not arm_is_home:
            raise RuntimeError("base symmetry is ambiguous without an arm tool pose or home assertion")
        oriented, symmetry = choose_base_symmetry(
            vio_from_base,
            vio_beak,
            base_tool_point=base_from_tool[:3, 3] if base_from_tool is not None else None,
        )
        rotation = oriented[:3, :3]
        weights = self.config["base_alignment"]["translation_fusion_weights"]
        translation_candidates = [
            (
                "foundation_base",
                oriented[:3, 3],
                float(weights["foundation_base"]),
            )
        ]
        configured_offset = self.config["tool_geometry"]["tool_to_beak_center_translation_m"]
        tool_beak = (
            np.asarray(configured_offset, np.float64)
            if configured_offset is not None
            else np.zeros(3, np.float64)
        )
        base_beak = apply_transform(base_from_tool, tool_beak)
        translation_candidates.append(
            (
                "vlm_beak_plus_arm",
                vio_beak - rotation @ base_beak,
                float(weights["vlm_beak_plus_arm"]),
            )
        )
        total_weight = sum(value[2] for value in translation_candidates)
        oriented[:3, 3] = sum(point * weight for _, point, weight in translation_candidates) / total_weight
        learned_tool_beak = apply_transform(np.linalg.inv(base_from_tool), apply_transform(np.linalg.inv(oriented), vio_beak))
        world_from_vio = np.eye(4, dtype=np.float64)
        world_from_vio[:3, 3] = -vio_from_camera[:3, 3]
        world_from_base = world_from_vio @ oriented
        world_from_beak = apply_transform(world_from_vio, vio_beak)
        return self._result(
            mode=str(RunMode.FOUNDATION_BASE_VLM_GRIPPER),
            alignment_id=alignment_id,
            skill_id=skill_id,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_base,
            vio_from_camera_reference=vio_from_camera,
            learned_tool_beak=learned_tool_beak,
            gripper_measurements=[
                {
                    "source_type": "VLM_RGBD_BEAK",
                    "semantic_point": "FOREMOST_BEAK_MEAN",
                    "position_world_m": world_from_beak.tolist(),
                    "role": "PRIMARY_ALIGNMENT_INPUT",
                    "used_in_alignment": True,
                }
            ],
            diagnostics={
                "base_samples": diagnostics,
                "gripper_source": "VLM_RGBD_BEAK",
                "foundation_pose_models": [self.config["foundation_base_model_id"]],
                "foundation_pose_validation": validations,
                "symmetry": symmetry,
                "translation_fusion": [
                    {"source": name, "translation_m": point.tolist(), "weight": weight}
                    for name, point, weight in translation_candidates
                ],
                "tool_beak_geometry": (
                    "configured"
                    if configured_offset is not None
                    else "learned_from_this_alignment; inherits base/VLM bias"
                ),
                "vlm": vlm,
                "motion_inhibit": keeper.status(),
            },
        )

    async def _finish_foundation_dual(
        self,
        *,
        samples: dict[str, dict[str, list[np.ndarray]]],
        validations: list[dict[str, Any]],
        frame: Any,
        vio_beak: np.ndarray,
        base_from_tool: np.ndarray,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        arm_is_home: bool,
        keeper: MotionInhibitKeeper,
        vio_from_camera: np.ndarray,
    ) -> dict[str, Any]:
        await keeper.ensure_valid()
        await self.progress.update(
            phase="SOLVING",
            message=(
                "Resolving base symmetry from the FoundationPose gripper and "
                "fusing the slower dim-scene measurements."
            ),
            completed_units=6,
            progress_kind="milestone",
        )
        vio_from_base, base_diagnostics = robust_average_transforms(
            samples["base"]["vio"]
        )
        vio_from_gripper, gripper_diagnostics = robust_average_transforms(
            samples["gripper"]["vio"]
        )
        if base_from_tool is None and not arm_is_home:
            raise RuntimeError(
                "base symmetry is ambiguous without an arm tool pose or home assertion"
            )
        oriented, symmetry = choose_base_symmetry(
            vio_from_base,
            vio_from_gripper[:3, 3],
            base_tool_point=base_from_tool[:3, 3],
        )
        symmetry["method"] = "FOUNDATION_GRIPPER_PLUS_ARM_KINEMATICS"
        rotation = oriented[:3, :3]
        weights = self.config["base_alignment"][
            "dual_translation_fusion_weights"
        ]
        translation_candidates = [
            (
                "foundation_base",
                oriented[:3, 3],
                float(weights["foundation_base"]),
            ),
            (
                "foundation_gripper_plus_arm",
                vio_from_gripper[:3, 3] - rotation @ base_from_tool[:3, 3],
                float(weights["foundation_gripper_plus_arm"]),
            ),
        ]
        configured_offset = self.config["tool_geometry"][
            "tool_to_beak_center_translation_m"
        ]
        tool_beak = (
            np.asarray(configured_offset, np.float64)
            if configured_offset is not None
            else np.zeros(3, np.float64)
        )
        base_beak = apply_transform(base_from_tool, tool_beak)
        translation_candidates.append(
            (
                "vlm_beak_plus_arm",
                vio_beak - rotation @ base_beak,
                float(weights["vlm_beak_plus_arm"]),
            )
        )
        total_weight = sum(value[2] for value in translation_candidates)
        oriented[:3, 3] = (
            sum(point * weight for _, point, weight in translation_candidates)
            / total_weight
        )
        learned_tool_beak = apply_transform(
            np.linalg.inv(base_from_tool),
            apply_transform(np.linalg.inv(oriented), vio_beak),
        )
        world_from_vio = np.eye(4, dtype=np.float64)
        world_from_vio[:3, 3] = -vio_from_camera[:3, 3]
        world_foundation_gripper = apply_transform(
            world_from_vio,
            vio_from_gripper[:3, 3],
        )
        world_vlm_beak = apply_transform(world_from_vio, vio_beak)
        return self._result(
            mode=str(RunMode.FOUNDATION_BASE_GRIPPER),
            alignment_id=alignment_id,
            skill_id=skill_id,
            frame=frame,
            world_from_vio=world_from_vio,
            world_from_base=world_from_vio @ oriented,
            vio_from_camera_reference=vio_from_camera,
            learned_tool_beak=learned_tool_beak,
            gripper_measurements=[
                {
                    "source_type": "FOUNDATIONPOSE_GRIPPER_POSE",
                    "semantic_point": "GRIPPER_MODEL_ORIGIN",
                    "position_world_m": world_foundation_gripper.tolist(),
                    "role": "PRIMARY_ALIGNMENT_INPUT",
                    "used_in_alignment": True,
                },
                {
                    "source_type": "VLM_RGBD_BEAK",
                    "semantic_point": "FOREMOST_BEAK_MEAN",
                    "position_world_m": world_vlm_beak.tolist(),
                    "role": "AUXILIARY_TRANSLATION_INPUT",
                    "used_in_alignment": True,
                },
            ],
            diagnostics={
                "base_samples": base_diagnostics,
                "gripper_samples": gripper_diagnostics,
                "gripper_source": "FOUNDATIONPOSE_GRIPPER_POSE",
                "gripper_auxiliary_source": "VLM_RGBD_BEAK",
                "foundation_pose_models": [
                    self.config["foundation_base_model_id"],
                    self.config["foundation_gripper_model_id"],
                ],
                "foundation_pose_validation": validations,
                "symmetry": symmetry,
                "translation_fusion": [
                    {
                        "source": name,
                        "translation_m": point.tolist(),
                        "weight": weight,
                    }
                    for name, point, weight in translation_candidates
                ],
                "tool_beak_geometry": (
                    "configured"
                    if configured_offset is not None
                    else "learned_from_this_alignment; inherits VLM bias"
                ),
                "vlm": vlm,
                "motion_inhibit": keeper.status(),
            },
        )

    async def _vlm_gripper_only(
        self,
        *,
        prior: dict[str, Any],
        frame: Any,
        camera_beak: np.ndarray,
        base_from_tool: np.ndarray,
        alignment_id: str,
        skill_id: str,
        vlm: dict[str, Any],
        keeper: MotionInhibitKeeper,
        vision: GripperVision,
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
            camera_beak,
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
                candidate_camera_beak, _ = self._camera_beak(frame, candidate_vlm)
                candidate_world_beak = apply_transform(
                    prior_world_from_camera,
                    candidate_camera_beak,
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
        if (
            not isinstance(prior_camera, dict)
            or not isinstance(frame_contract, dict)
            or not isinstance(route_observation, dict)
        ):
            return False
        comparisons = (
            (
                prior_camera.get("provider_id"),
                route_observation.get("provider_id"),
            ),
            (
                prior_camera.get("provider_instance_id"),
                route_observation.get("provider_instance_id"),
            ),
            (
                prior_camera.get("boot_id"),
                route_observation.get("boot_id"),
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
        learned_tool_beak: np.ndarray,
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
            "learned_tool_to_beak_translation_m": learned_tool_beak.tolist(),
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
        return {
            "schema": (
                "midbrain.skill.stationary_world_arm_alignment."
                "calibration_candidate"
            ),
            "schema_version": 2,
            "candidate_id": alignment_id,
            "workcell_calibration_revision": alignment_id,
            "created_at_us": created_at_us,
            "expires_at_us": expires_at_us,
            "review_state": "CANDIDATE_REVIEW_REQUIRED",
            "review_mode": review_mode,
            "motion_usable": False,
            "method": {
                "skill_version": "0.7.0",
                "base_pose_engine_route": self.base_pose_engine_route,
                "run_mode": mode,
            },
            "frame_contract": {
                "world_frame": f"world/stationary_camera/{alignment_id}",
                "vio_world_frame": frame.world_frame,
                "camera_frame": frame.camera_frame,
                "arm_base_frame": self.config["arm_base_frame"],
                "transform_semantics": "PARENT_FROM_CHILD",
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
                "world_frame": frame.world_frame,
                "session_epoch": frame.session_epoch,
            },
            "transforms": {
                "world_from_camera": transform_payload(
                    world_from_vio @ vio_from_camera_reference
                ),
                "world_from_vio": transform_payload(world_from_vio),
                "world_from_base": transform_payload(world_from_base),
            },
        }

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
        now_us = time.time_ns() // 1000
        for activation in workcell_calibrations.get("activations") or []:
            if (
                activation.get("state") == "ACTIVE"
                and activation.get("motion_usable") is True
                and str(activation.get("candidate_id") or "") == candidate_id
                and str(activation.get("session_epoch") or "") == session_epoch
                and int(activation.get("expires_at_us") or 0) > now_us
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
                int(activation.get("expires_at_us") or 0),
                str(activation.get("activated_at") or ""),
            ),
            reverse=True,
        )
        for activation in activations:
            if str(activation.get("state") or "") not in {
                "ACTIVE",
                "EXPIRED",
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
