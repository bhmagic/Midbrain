from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import cv2
import httpx
import numpy as np
from spatial_registration_rgbd import select_depth_sample

from .artifacts import MonitorArtifacts
from .camera import RgbdCapture, RgbdFrame, encode_depth_png, encode_rgb_jpeg
from .clients import FabricClient, IntegratedControlClient, ManagerLifecycleClient
from .config import Settings
from .execution import MotionExecutor
from .first_cut_alignment import (
    build_first_cut_alignment_contract,
    build_first_cut_alignment_correction,
    build_first_cut_pixel_servo_measurement,
)
from .geometry import (
    plan_cut_points_on_line_3d,
    polygon_mask,
)
from .execution_preview import build_execution_preview
from .math3d import (
    deproject_pixel,
    matrix_quaternion_xyzw,
    matrix_rpy,
    normalized_yx_to_pixel,
    transform_matrix,
    transform_points,
)
from .models import Phase, RunParameters, SkillState
from .persistence import PlanStore
from .progress import ProgressReporter
from .render import render_first_cut_target_overlay, render_plan_overlay
from .tool_registration import (
    build_blade_registration_candidate,
    evaluate_blade_registration_consistency,
)
from .tracking import AppearanceTracker
from .vlm import SceneVision, render_registered_depth_evidence


def evaluate_integrated_readiness(state: dict[str, Any]) -> tuple[bool, list[str]]:
    engaged = bool(state.get("engaged"))
    control_state = str(state.get("control_state") or "").upper()
    gripper = state.get("gripper") or {}
    tool_hold_only = (
        control_state == "GRIPPER_MIT_CLOSE_LATCHED"
        and bool(gripper.get("latched_hold"))
        and str(gripper.get("active_action") or "").upper() == "CLOSE"
    )
    trajectory = state.get("trajectory")
    trajectory_active = bool(
        trajectory
        and (
            not isinstance(trajectory, dict)
            or trajectory.get("active", True)
        )
    )
    basic_state = state.get("basic_state") or {}
    safety = state.get("safety") or {}
    confirmed_gravity_float = (
        str(basic_state.get("provider_state") or "").upper()
        == "SAFE_HOLD_GRAVITY_FLOAT"
        and bool(safety.get("float_confirmed"))
        and not trajectory_active
    )
    acceptable_engaged_idle = tool_hold_only or confirmed_gravity_float
    ready = bool(state.get("ready"))
    health = str(state.get("health") or "").upper()
    faulted = control_state.startswith("FAULT") or bool(state.get("fault_reason"))
    reasons = [
        reason
        for reason, present in (
            ("Integrated controller is not ready", not ready),
            ("Integrated controller health is not HEALTHY", health != "HEALTHY"),
            ("Integrated controller is faulted", faulted),
            (
                "Integrated controller is engaged beyond an accepted idle hold",
                engaged and not acceptable_engaged_idle,
            ),
            ("Integrated trajectory is active", trajectory_active),
        )
        if present
    ]
    return not reasons, reasons


class VegetableCuttingSkill:
    def __init__(
        self,
        *,
        settings: Settings,
        config: dict[str, Any],
        artifacts: MonitorArtifacts,
    ):
        self.settings = settings
        self.config = config
        self.artifacts = artifacts
        self.manager = ManagerLifecycleClient(settings.manager_url)
        self.fabric = FabricClient(settings.fabric_url)
        self.integrated = IntegratedControlClient(settings.integrated_url)
        self.camera = RgbdCapture(self.fabric, config["frames"]["camera"])
        self.progress = ProgressReporter(self.fabric)
        self.store = PlanStore(settings.plan_root)
        self.lock = asyncio.Lock()
        self.parameters: RunParameters | None = None
        self.scene_vision: SceneVision | None = None
        self.reference_frame: RgbdFrame | None = None
        self.reference_mask: np.ndarray | None = None
        self.tracker: AppearanceTracker | None = None
        self.vlm_requery_count = 0
        self.plan_revision = 0
        self.sequence = 0
        self.blade_registration_candidates: list[dict[str, Any]] = []
        self.accepted_tool_calibration: dict[str, Any] | None = None
        self.motion_executor: MotionExecutor | None = None
        self.execution_task: asyncio.Task[None] | None = None
        self.execution_cancelled = False
        self.execution_events: list[dict[str, Any]] = []
        self.execution_translation_arm_base_m = np.zeros(
            3, dtype=np.float64
        )
        self.execution_rotation_rpy_rad = np.zeros(3, dtype=np.float64)
        self.execution_control_rpy_rad: np.ndarray | None = None
        self.execution_cut_centers_arm_base_m: np.ndarray | None = None
        self.first_cut_correction_count = 0
        self.first_cut_alignment_attempt_count = 0
        self.first_cut_alignment_round_count = 0
        self.stationary_camera_transform_lock: dict[str, Any] | None = None
        self.local_vio_stop_result: dict[str, Any] | None = None

    async def close(self) -> None:
        self.execution_cancelled = True
        if self.execution_task is not None and not self.execution_task.done():
            self.execution_task.cancel()
            try:
                await self.execution_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await self.integrated.request_float()
            except Exception:
                pass
        if self.scene_vision is not None:
            await self.scene_vision.close()
        await self.integrated.close()
        await self.fabric.close()
        await self.manager.close()

    async def bootstrap_providers(self) -> dict[str, Any]:
        providers = self.config["providers"]
        startup = self.config["provider_startup"]
        timeout_s = float(startup["timeout_s"])
        ordered = [
            ("camera", "start_camera"),
            ("local_vio", "start_local_vio"),
            ("basic_arm", "start_basic_arm"),
            ("integrated_arm", "start_integrated_arm"),
        ]
        results: dict[str, Any] = {}
        for role, flag in ordered:
            provider_id = str(providers[role])
            if (
                role == "local_vio"
                and getattr(
                    self,
                    "stationary_camera_transform_lock",
                    None,
                )
                is not None
            ):
                results[role] = {
                    "provider_id": provider_id,
                    "ready": True,
                    "intentionally_stopped": True,
                    "reason": "FIXED_CAMERA_TRANSFORM_LOCKED",
                }
                continue
            if not bool(startup[flag]):
                results[role] = {"provider_id": provider_id, "skipped": True}
                continue
            try:
                result = await self.manager.ensure_hot(provider_id, timeout_s=timeout_s)
                results[role] = {"provider_id": provider_id, "ready": True, "lifecycle": result}
            except Exception as error:
                results[role] = {
                    "provider_id": provider_id,
                    "ready": False,
                    "error": str(error),
                }
                break
        readiness = await self.readiness_snapshot()
        readiness["startup"] = results
        await self.progress.update(provider_readiness=readiness)
        return readiness

    async def readiness_snapshot(self) -> dict[str, Any]:
        output: dict[str, Any] = {
            "motion_boundary": {
                "motion_submission_capability_available": bool(
                    self.config["execution"]["enabled"]
                ),
                "motion_submission_enabled_for_session": (
                    self.accepted_tool_calibration is not None
                ),
                "integrated_access": "OPERATOR_SUPERVISED_CONTROL",
                "operator_takeover_required": True,
            }
        }
        try:
            output["manager"] = await self.manager.health()
        except Exception as error:
            output["manager"] = {"reachable": False, "error": str(error)}
        try:
            output["fabric"] = await self.fabric.health()
        except Exception as error:
            output["fabric"] = {"reachable": False, "error": str(error)}
        try:
            state = await self.integrated.state()
            output["integrated_state"] = state
            integrated_idle, reasons = evaluate_integrated_readiness(state)
            output["integrated_idle"] = integrated_idle
            output["integrated_idle_reasons"] = reasons
        except Exception as error:
            output["integrated_state"] = {"reachable": False, "error": str(error)}
            output["integrated_idle"] = False
            output["integrated_idle_reasons"] = ["integrated state is unavailable"]
        try:
            output["integrated_capabilities"] = await self.integrated.capabilities()
        except Exception as error:
            output["integrated_capabilities"] = {"reachable": False, "error": str(error)}
        return output

    async def capture_visual_snapshot(self) -> dict[str, Any]:
        frame = await self.camera.capture(require_vio=False)
        await self.artifacts.set_images(
            rgb_jpeg=encode_rgb_jpeg(frame.rgb),
            depth_png=encode_depth_png(frame.depth_m),
        )
        return {
            "captured": True,
            "motion_submitted": False,
            "camera_frame": frame.camera_frame,
            "timestamp_us": frame.timestamp_us,
            "frame_number": frame.frame_number,
            "vio_session_epoch": frame.session_epoch,
            "calibration_revision": frame.calibration_revision,
            "rgb_shape": list(frame.rgb.shape),
            "valid_depth_fraction": float(
                np.count_nonzero(np.isfinite(frame.depth_m) & (frame.depth_m > 0.05))
                / max(frame.depth_m.size, 1)
            ),
        }

    async def start_session(self, parameters: RunParameters) -> dict[str, Any]:
        parameters.validated()
        async with self.lock:
            current = await self.progress.snapshot()
            if current["state"] in {
                SkillState.RUNNING,
                SkillState.WAITING_FOR_OPERATOR,
                SkillState.READY_FOR_OPERATOR_TAKEOVER,
            }:
                raise RuntimeError("a cutting session is already active")
            skill_id = f"vegetable-cutting-{uuid.uuid4()}"
            self.parameters = parameters
            self.reference_frame = None
            self.reference_mask = None
            self.tracker = None
            self.vlm_requery_count = 0
            self.plan_revision = 0
            self.blade_registration_candidates = []
            self.accepted_tool_calibration = None
            self.motion_executor = None
            self.execution_cancelled = False
            self.execution_events = []
            self.execution_translation_arm_base_m = np.zeros(
                3, dtype=np.float64
            )
            self.execution_rotation_rpy_rad = np.zeros(
                3, dtype=np.float64
            )
            self.execution_control_rpy_rad = None
            self.execution_cut_centers_arm_base_m = None
            self.first_cut_correction_count = 0
            self.first_cut_alignment_attempt_count = 0
            self.first_cut_alignment_round_count = 0
        readiness = await self.readiness_snapshot()
        if not bool(readiness.get("integrated_idle")):
            raise RuntimeError(
                "Integrated must be ready, HEALTHY, fault-free, disengaged, and idle "
                "before the cutting Skill starts: "
                + ", ".join(readiness.get("integrated_idle_reasons") or [])
            )
        alignment = await self._alignment_snapshot()
        transform_lock_frame = await self._capture_skill_frame()
        transform_lock = await self._capture_frame_transforms(
            transform_lock_frame
        )
        alignment = {
            **alignment,
            "fixed_camera_transform_lock": (
                transform_lock["arm_from_camera"].get(
                    "stationary_camera_pose_lock"
                )
            ),
            "local_vio_stop_result": transform_lock.get(
                "local_vio_stop_result"
            ),
        }
        return await self.progress.update(
            skill_id=skill_id,
            plan_id="",
            state=SkillState.WAITING_FOR_OPERATOR,
            phase=Phase.WAIT_TOOL_LOAD,
            message=(
                "The fixed camera-to-arm pose is locked and local VIO is "
                "stopped. Use the Integrated GUI to load and grip the "
                "knife. This Skill cannot issue the gripper command."
            ),
            started_at_us=time.time_ns() // 1000,
            operator_tool_loaded=False,
            operator_tool_attachment_confirmed=False,
            operator_workpiece_loaded=False,
            operator_outside_workspace=False,
            motion_submission_enabled=False,
            motion_submitted=False,
            provider_readiness=readiness,
            alignment=alignment,
            tracking=None,
            execution={
                "state": "PLANNING_ONLY",
                "motion_submission_enabled": False,
                "operator_takeover_confirmed": False,
                "events": [],
            },
            result=None,
            error=None,
        )

    async def reset_failed_session(self) -> dict[str, Any]:
        current = await self.progress.snapshot()
        if current["state"] != SkillState.FAILED:
            raise RuntimeError(
                "only a FAILED cutting session can be reset with this action"
            )
        active_task = self.execution_task
        if active_task is not None and not active_task.done():
            raise RuntimeError(
                "the failed session still has an active execution task"
            )
        integrated_state = await self.integrated.state()
        trajectory = integrated_state.get("trajectory")
        trajectory_active = bool(
            trajectory
            and (
                not isinstance(trajectory, dict)
                or trajectory.get("active", True)
            )
        )
        if trajectory_active:
            raise RuntimeError(
                "the failed session cannot be reset while an Integrated "
                "trajectory is active"
            )
        async with self.lock:
            current = await self.progress.snapshot()
            if current["state"] != SkillState.FAILED:
                raise RuntimeError(
                    "the cutting session changed while reset was being verified"
                )
            self.parameters = None
            self.reference_frame = None
            self.reference_mask = None
            self.tracker = None
            self.vlm_requery_count = 0
            self.plan_revision = 0
            self.blade_registration_candidates = []
            self.accepted_tool_calibration = None
            self.motion_executor = None
            self.execution_task = None
            self.execution_cancelled = False
            self.execution_events = []
            self.execution_translation_arm_base_m = np.zeros(
                3, dtype=np.float64
            )
            self.execution_rotation_rpy_rad = np.zeros(
                3, dtype=np.float64
            )
            self.execution_control_rpy_rad = None
            self.execution_cut_centers_arm_base_m = None
            self.first_cut_correction_count = 0
            self.first_cut_alignment_attempt_count = 0
            self.first_cut_alignment_round_count = 0
        return await self.progress.update(
            skill_id="",
            plan_id="",
            state=SkillState.IDLE,
            phase=Phase.IDLE,
            message=(
                "Failed session state was cleared without motion, Float, or "
                "gripper commands. The fixed-camera transform lock and physical "
                "tool attachment are unchanged; start a new session and confirm "
                "the attached tool again."
            ),
            started_at_us=None,
            operator_tool_loaded=False,
            operator_tool_attachment_confirmed=False,
            operator_workpiece_loaded=False,
            operator_outside_workspace=False,
            motion_submission_enabled=False,
            motion_submitted=False,
            tracking=None,
            execution={
                "state": "IDLE_AFTER_FAILED_SESSION_RESET",
                "motion_submission_enabled": False,
                "motion_submitted": False,
                "integrated_trajectory_active_at_reset": False,
                "fixed_camera_transform_lock_preserved": bool(
                    self.stationary_camera_transform_lock
                ),
                "physical_tool_state_changed": False,
            },
            result=None,
            error=None,
        )

    async def confirm_tool_loaded(
        self,
        *,
        operator_confirms_knife_attached: bool,
    ) -> dict[str, Any]:
        current = await self.progress.snapshot()
        if current["phase"] != Phase.WAIT_TOOL_LOAD:
            raise RuntimeError("the Skill is not waiting for tool loading")
        if not operator_confirms_knife_attached:
            raise ValueError(
                "operator confirmation that the knife is physically attached "
                "is required"
            )
        state = await self.integrated.state()
        trajectory = state.get("trajectory") or {}
        if bool(trajectory.get("active")):
            raise RuntimeError(
                "an arm trajectory is active while confirming the tool"
            )
        if str(state.get("health") or "").upper() != "HEALTHY":
            raise RuntimeError(
                "Integrated health is not HEALTHY while confirming the tool"
            )
        return await self.progress.update(
            operator_tool_loaded=True,
            operator_tool_attachment_confirmed=True,
            phase=Phase.WAIT_WORKPIECE_LOAD,
            message=(
                "Place the vegetable on the cutting board, withdraw from the robot workspace, "
                "then confirm both conditions."
            ),
        )

    async def confirm_workpiece_loaded(self, *, operator_outside_workspace: bool) -> dict[str, Any]:
        current = await self.progress.snapshot()
        if current["phase"] != Phase.WAIT_WORKPIECE_LOAD:
            raise RuntimeError("the Skill is not waiting for workpiece loading")
        if not operator_outside_workspace:
            raise ValueError("operator_outside_workspace confirmation is required")
        return await self.progress.update(
            operator_workpiece_loaded=True,
            operator_outside_workspace=True,
            state=SkillState.RUNNING,
            phase=Phase.PERCEIVING,
            message="Ready for one initial VLM localization and metric RGB-D planning.",
        )

    async def perceive_and_plan(self) -> dict[str, Any]:
        current = await self.progress.snapshot()
        if current["phase"] != Phase.PERCEIVING:
            raise RuntimeError("operator confirmations are incomplete")
        if not self.parameters:
            raise RuntimeError("run parameters are unavailable")
        await self.progress.update(message="Capturing RGB-D and running initial VLM localization.")
        frame = await self._capture_skill_frame()
        frame_transforms = await self._capture_frame_transforms(frame)
        attempts = int(self.config["vlm"]["initial_attempts"])
        last_error: Exception | None = None
        plan: dict[str, Any] | None = None
        scene: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            try:
                scene = await self._vision().locate(frame.rgb)
                plan = await self._build_plan(
                    frame,
                    scene,
                    frame_transforms,
                    reason="INITIAL_VLM",
                )
                break
            except (RuntimeError, ValueError) as error:
                last_error = error
                if attempt < attempts:
                    await self.progress.update(
                        phase=Phase.PERCEIVING,
                        message=(
                            f"Initial VLM geometry attempt {attempt} was rejected; "
                            "retrying the same RGB-D frame."
                        ),
                    )
        if plan is None or scene is None:
            raise RuntimeError(
                f"initial scene localization failed after {attempts} attempts: {last_error}"
            )
        await self._accept_reference(frame, scene, plan)
        return plan

    def _build_session_tool_calibration(
        self,
        *,
        plan_id: str,
        plan_revision: int,
        consistency: dict[str, Any],
    ) -> dict[str, Any]:
        if not bool(consistency.get("eligible_for_operator_review")):
            raise RuntimeError(
                "blade registration is not eligible for session calibration: "
                + "; ".join(consistency.get("quality_reasons") or [])
            )
        position = consistency.get(
            "representative_acting_point_from_tool_m"
        )
        orientation = consistency.get(
            "representative_controlled_frame_rpy_from_tool"
        )
        if not isinstance(position, list) or not isinstance(orientation, list):
            raise RuntimeError(
                "reviewable blade registration lacks a controlled-frame pose"
            )
        self._validate_controlled_frame_offset(position)
        tool = self.config["tool"]
        payload_mass = tool.get("payload_mass_kg")
        payload_com = tool.get("payload_com_tool_m")
        payload_assumption = None
        if payload_mass is None or payload_com is None:
            payload_mass = 0.0
            payload_com = [0.0, 0.0, 0.0]
            payload_assumption = (
                "UNMEASURED_TOOL_PAYLOAD_ASSUMED_ZERO_FOR_INITIAL_BRINGUP"
            )
        calibration = {
            "calibration_id": f"blade-session-{uuid.uuid4()}",
            "registered_at_us": time.time_ns() // 1000,
            "source_plan_id": plan_id,
            "source_plan_revision": plan_revision,
            "source_observations": int(
                consistency.get("required_observations") or 0
            ),
            "controlled_frame_offset_xyz_m": [
                float(value) for value in position
            ],
            "controlled_frame_offset_rpy_rad": [
                float(value) for value in orientation
            ],
            "payload_mass_kg": float(payload_mass),
            "payload_com_tool_m": [
                float(value) for value in payload_com
            ],
            "payload_assumption": payload_assumption,
            "operator_reviewed": False,
            "operator_review_deferred_to_first_cut_approach": True,
            "session_only": True,
        }
        return calibration

    def _build_fixed_tool_calibration(
        self,
        *,
        plan_id: str,
        plan_revision: int,
    ) -> dict[str, Any]:
        tool = self.config["tool"]
        position = [
            float(value)
            for value in tool["fixed_controlled_frame_offset_xyz_m"]
        ]
        orientation = [
            float(value)
            for value in tool["fixed_controlled_frame_offset_rpy_rad"]
        ]
        self._validate_controlled_frame_offset(position)
        payload_mass = tool.get("payload_mass_kg")
        payload_com = tool.get("payload_com_tool_m")
        payload_assumption = None
        if payload_mass is None or payload_com is None:
            payload_mass = 0.0
            payload_com = [0.0, 0.0, 0.0]
            payload_assumption = (
                "UNMEASURED_TOOL_PAYLOAD_ASSUMED_ZERO_FOR_INITIAL_BRINGUP"
            )
        return {
            "calibration_id": f"fixed-blade-{uuid.uuid4()}",
            "registered_at_us": time.time_ns() // 1000,
            "source_plan_id": plan_id,
            "source_plan_revision": plan_revision,
            "source_observations": 0,
            "source": "CONFIGURED_HARD_FIXED_BLADE_OFFSET",
            "controlled_frame_offset_xyz_m": position,
            "controlled_frame_offset_rpy_rad": orientation,
            "payload_mass_kg": float(payload_mass),
            "payload_com_tool_m": [
                float(value) for value in payload_com
            ],
            "payload_assumption": payload_assumption,
            "operator_reviewed": False,
            "operator_review_deferred_to_first_cut_approach": True,
            "session_only": True,
        }

    async def begin_execution(
        self,
        *,
        operator_takeover_confirmed: bool,
    ) -> dict[str, Any]:
        if not operator_takeover_confirmed:
            raise ValueError(
                "explicit operator takeover confirmation is required"
            )
        current = await self.progress.snapshot()
        if current["phase"] != Phase.READY_FOR_OPERATOR_TAKEOVER:
            raise RuntimeError("the Skill is not ready for physical execution")
        if self.accepted_tool_calibration is None:
            raise RuntimeError(
                "session blade calibration is unavailable; rerun VLM planning"
            )
        self._validate_controlled_frame_offset(
            self.accepted_tool_calibration.get(
                "controlled_frame_offset_xyz_m"
            )
        )
        if self.execution_task is not None and not self.execution_task.done():
            raise RuntimeError("a physical execution task is already active")
        plan = current.get("result")
        if not isinstance(plan, dict):
            raise RuntimeError("the current cutting plan is unavailable")
        latest_alignment = await self._alignment_snapshot()
        if latest_alignment.get("alignment_id") != (
            plan.get("alignment") or {}
        ).get("alignment_id"):
            raise RuntimeError(
                "the stationary alignment changed after planning; regenerate the plan"
            )
        self.execution_cancelled = False
        self.execution_translation_arm_base_m = np.zeros(
            3, dtype=np.float64
        )
        self.execution_rotation_rpy_rad = np.zeros(3, dtype=np.float64)
        self.execution_control_rpy_rad = None
        self.execution_cut_centers_arm_base_m = None
        self.first_cut_correction_count = 0
        self.first_cut_alignment_attempt_count = 0
        self.first_cut_alignment_round_count = 0
        self.motion_executor = MotionExecutor(
            fabric=self.fabric,
            integrated=self.integrated,
            config=self.config["execution"],
            skill_id=str(current["skill_id"]),
            calibration=self.accepted_tool_calibration,
            cancelled=lambda: self.execution_cancelled,
            on_event=self._record_execution_event,
        )
        execution = {
            "state": "TRANSFER_TO_FIRST_CUT",
            "motion_submission_enabled": True,
            "operator_takeover_confirmed": True,
            "calibration": self.accepted_tool_calibration,
            "events": list(self.execution_events),
        }
        progress = await self.progress.update(
            state=SkillState.RUNNING,
            phase=Phase.TRANSFER_TO_FIRST_CUT,
            message=(
                "Operator takeover accepted. Configuring the calibrated blade "
                "frame and transferring to the first-cut approach with bounded MIT commits."
            ),
            motion_submission_enabled=True,
            motion_submitted=False,
            execution=execution,
            error=None,
        )
        self.execution_task = asyncio.create_task(
            self._prepare_first_cut(plan),
            name="vegetable-cutting-first-approach",
        )
        return progress

    def _validate_controlled_frame_offset(
        self,
        position: Any,
    ) -> float:
        vector = np.asarray(position, dtype=np.float64)
        if vector.shape != (3,) or not np.all(np.isfinite(vector)):
            raise RuntimeError(
                "the blade controlled-frame offset must contain three finite values"
            )
        distance_m = float(np.linalg.norm(vector))
        if distance_m <= 1e-9:
            raise RuntimeError(
                "the blade acting point cannot coincide with the robot tool origin"
            )
        maximum_m = float(
            self.config["tool"]["observation_registration"][
                "maximum_tool_to_acting_point_m"
            ]
        )
        if distance_m > maximum_m:
            raise RuntimeError(
                "blade registration is transform-inconsistent: the acting "
                f"point is {distance_m * 1000.0:.1f} mm from the robot tool "
                f"frame, above the {maximum_m * 1000.0:.1f} mm physical "
                "limit"
            )
        return distance_m

    async def first_cut_decision(self, decision: str) -> dict[str, Any]:
        normalized = str(decision).strip().upper()
        if normalized not in {"YES", "NO_READJUST", "FULL_STOP_GO_HOME"}:
            raise ValueError(
                "first-cut decision must be YES, NO_READJUST, or FULL_STOP_GO_HOME"
            )
        current = await self.progress.snapshot()
        if current["phase"] != Phase.WAIT_FIRST_CUT_CONFIRMATION:
            raise RuntimeError(
                "the Skill is not waiting for first-cut confirmation"
            )
        if normalized == "FULL_STOP_GO_HOME":
            return await self.abort(
                "operator requested full stop before the first cut"
            )
        if self.execution_task is not None and not self.execution_task.done():
            raise RuntimeError("an execution task is still active")
        plan = current.get("result")
        if not isinstance(plan, dict):
            raise RuntimeError("the current cutting plan is unavailable")
        if normalized == "NO_READJUST":
            progress = await self.progress.update(
                state=SkillState.RUNNING,
                phase=Phase.TRANSFER_TO_FIRST_CUT,
                message=(
                    "Operator requested first-cut readjustment. "
                    "Capturing a fresh alignment image."
                ),
            )
            self.execution_task = asyncio.create_task(
                self._align_first_cut(plan),
                name="vegetable-cutting-first-cut-readjust",
            )
            return progress

        integrated_state = await self.integrated.state()
        self._assert_corrected_cut_workspace(
            plan,
            integrated_state,
            translation_arm_base_m=(
                self.execution_translation_arm_base_m
            ),
        )
        if self.accepted_tool_calibration is not None:
            self.accepted_tool_calibration = {
                **self.accepted_tool_calibration,
                "operator_reviewed": True,
                "operator_reviewed_at_us": time.time_ns() // 1000,
            }
        execution = {
            **(current.get("execution") or {}),
            "state": "CUTTING",
            "calibration": self.accepted_tool_calibration,
            "first_cut_operator_review": "YES",
            "first_cut_vlm_attempt_count": (
                self.first_cut_alignment_attempt_count
            ),
            "events": list(self.execution_events),
        }
        progress = await self.progress.update(
            state=SkillState.RUNNING,
            phase=Phase.CUTTING,
            message=(
                "First-cut location confirmed. Running the MIT cut/retract "
                "sequence without further coordinate checks."
            ),
            execution=execution,
        )
        self.execution_task = asyncio.create_task(
            self._run_cut_sequence(plan),
            name="vegetable-cutting-physical-sequence",
        )
        return progress

    async def confirm_tool_removed_and_safe_terminate(self) -> dict[str, Any]:
        current = await self.progress.snapshot()
        if current["phase"] != Phase.WAIT_TOOL_REMOVAL:
            raise RuntimeError(
                "the Skill is not waiting for tool removal"
            )
        state = await self.integrated.state()
        trajectory = state.get("trajectory") or {}
        if bool(trajectory.get("active")):
            raise RuntimeError(
                "cannot safe-home while an Integrated trajectory is active"
            )
        await self.progress.update(
            state=SkillState.RUNNING,
            phase=Phase.SAFE_TERMINATING,
            message=(
                "Knife removal confirmed. Starting the Integrated safe-termination "
                "helper for safe-home and workspace release."
            ),
            motion_submission_enabled=False,
            operator_tool_loaded=False,
            operator_tool_attachment_confirmed=False,
        )
        result = await self.integrated.safe_terminate()
        termination = result.get("safe_termination") or {}
        result_status = str(result.get("status") or "").lower()
        termination_state = str(termination.get("state") or "")
        if (
            result_status != "accepted"
            or termination_state != "RUNNING"
        ):
            message = str(
                termination.get("message")
                or "the authoritative safe-termination helper did not acknowledge startup"
            )
            execution = {
                **(current.get("execution") or {}),
                "state": "SAFE_TERMINATION_LAUNCH_NOT_CONFIRMED",
                "safe_termination": result,
                "events": list(self.execution_events),
            }
            return await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.WAIT_TOOL_REMOVAL,
                message=(
                    "Knife removal is recorded, but safe-home did not start. "
                    "Use the retry button or the official Stop All command."
                ),
                motion_submission_enabled=False,
                operator_tool_loaded=False,
                operator_tool_attachment_confirmed=False,
                execution=execution,
                error=message,
            )
        prior_execution_state = str(
            (current.get("execution") or {}).get("state") or ""
        )
        abort_recovery = prior_execution_state.startswith("ABORTED_")
        execution = {
            **(current.get("execution") or {}),
            "state": (
                "SAFE_TERMINATION_STARTED_AFTER_ABORT"
                if abort_recovery
                else "SAFE_TERMINATION_STARTED"
            ),
            "safe_termination": result,
            "events": list(self.execution_events),
        }
        return await self.progress.update(
            state=(
                SkillState.ABORTED
                if abort_recovery
                else SkillState.COMPLETED
            ),
            phase=Phase.SAFE_TERMINATING,
            message=(
                (
                    "Abort recovery passed the knife-removal gate and safe "
                    "termination was started."
                )
                if abort_recovery
                else (
                    "Cutting sequence completed and safe termination was "
                    "started after tool removal."
                )
            ),
            motion_submission_enabled=False,
            motion_submitted=bool(current.get("motion_submitted")),
            execution=execution,
            error=current.get("error") if abort_recovery else None,
        )

    async def _transfer_to_first_cut_approach(
        self,
        plan: dict[str, Any],
    ) -> None:
        if self.motion_executor is None:
            raise RuntimeError("motion executor is unavailable")
        await self.motion_executor.configure()
        state = await self.integrated.state()
        measured = (
            (state.get("model_view") or {}).get(
                "measured_controlled_frame"
            )
            or {}
        )
        current_position = np.asarray(
            measured.get("position_m"),
            dtype=np.float64,
        )
        current_rpy = np.asarray(
            measured.get("rpy_rad"),
            dtype=np.float64,
        )
        if (
            current_position.shape != (3,)
            or current_rpy.shape != (3,)
            or not np.all(np.isfinite(current_position))
            or not np.all(np.isfinite(current_rpy))
        ):
            raise RuntimeError(
                "Integrated measured controlled frame is unavailable for "
                "the clearance-first transfer"
            )
        self.execution_control_rpy_rad = current_rpy.copy()
        nearby_target_list, _ = self._cut_target(
            plan,
            cut_index=0,
            approach=True,
            first_cut_review=True,
        )
        nearby_target = np.asarray(
            nearby_target_list,
            dtype=np.float64,
        )
        nearby_reapproach_m = float(
            self.config["execution"]["nearby_review_reapproach_m"]
        )
        if (
            float(np.linalg.norm(nearby_target - current_position))
            <= nearby_reapproach_m
        ):
            await self.motion_executor.move_to(
                label="NEARBY_REAPPROACH_TO_FIRST_REVIEW",
                position_m=nearby_target.tolist(),
                rpy_rad=current_rpy.tolist(),
                requested_speed_m_s=min(
                    0.08,
                    float(
                        self.config["handoff"][
                            "requested_transfer_speed_m_s"
                        ]
                    ),
                ),
                kp_multiplier=float(
                    self.config["execution"]["transfer_kp_multiplier"]
                ),
                minimum_duration_s=float(
                    self.config["execution"]["minimum_transfer_duration_s"]
                ),
                require_arrival=True,
            )
            return
        clearance_z_m = float(current_position[2]) + float(
            self.config["execution"]["initial_clearance_lift_m"]
        )
        maximum_clearance_z_m = float(
            self.config["execution"]["maximum_clearance_z_m"]
        )
        if clearance_z_m > maximum_clearance_z_m:
            raise RuntimeError(
                "clearance-first transfer would exceed the configured "
                f"Z ceiling ({clearance_z_m:.3f} m > "
                f"{maximum_clearance_z_m:.3f} m)"
            )
        transfer_speed_m_s = float(
            self.config["handoff"]["requested_transfer_speed_m_s"]
        )
        transfer_kp = float(
            self.config["execution"]["transfer_kp_multiplier"]
        )
        minimum_duration_s = float(
            self.config["execution"]["minimum_transfer_duration_s"]
        )
        common = {
            "requested_speed_m_s": transfer_speed_m_s,
            "kp_multiplier": transfer_kp,
            "minimum_duration_s": minimum_duration_s,
            "require_arrival": True,
        }
        await self.motion_executor.move_to(
            label="INITIAL_VERTICAL_CLEARANCE_LIFT",
            position_m=[
                float(current_position[0]),
                float(current_position[1]),
                clearance_z_m,
            ],
            rpy_rad=current_rpy.tolist(),
            **common,
        )
        await self._refresh_execution_cut_geometry(
            plan,
            reason="AFTER_INITIAL_CLEARANCE_LIFT",
        )
        await self._prealign_first_cut_at_clearance(plan)
        target_position_list, target_rpy_list = self._cut_target(
            plan,
            cut_index=0,
            approach=True,
            first_cut_review=True,
        )
        target_position = np.asarray(
            target_position_list,
            dtype=np.float64,
        )
        target_rpy = np.asarray(
            target_rpy_list,
            dtype=np.float64,
        )
        required_clearance_z_m = float(target_position[2]) + float(
            self.config["execution"][
                "minimum_clearance_above_approach_m"
            ]
        )
        if required_clearance_z_m > clearance_z_m:
            clearance_z_m = required_clearance_z_m
            if clearance_z_m > maximum_clearance_z_m:
                raise RuntimeError(
                    "refreshed first-cut target would exceed the configured "
                    f"Z ceiling ({clearance_z_m:.3f} m > "
                    f"{maximum_clearance_z_m:.3f} m)"
                )
            await self.motion_executor.move_to(
                label="REFRESHED_VERTICAL_CLEARANCE_LIFT",
                position_m=[
                    float(current_position[0]),
                    float(current_position[1]),
                    clearance_z_m,
                ],
                rpy_rad=current_rpy.tolist(),
                **common,
            )
        await self.motion_executor.move_to(
            label="WRIST_SINGULARITY_ESCAPE_POSITIVE_Y",
            position_m=[
                float(current_position[0]),
                max(
                    float(current_position[1]) + 0.05,
                    float(target_position[1]) + 0.025,
                ),
                clearance_z_m,
            ],
            rpy_rad=current_rpy.tolist(),
            **common,
        )
        singularity_escape_y_m = max(
            float(current_position[1]) + 0.05,
            float(target_position[1]) + 0.025,
        )
        await self.motion_executor.move_to(
            label="CLEARANCE_ARM_BASE_X_TRANSFER",
            position_m=[
                float(target_position[0]),
                singularity_escape_y_m,
                clearance_z_m,
            ],
            rpy_rad=current_rpy.tolist(),
            **common,
        )
        await self.motion_executor.move_to(
            label="CLEARANCE_ARM_BASE_Y_TRANSFER",
            position_m=[
                float(target_position[0]),
                float(target_position[1]),
                clearance_z_m,
            ],
            rpy_rad=current_rpy.tolist(),
            **common,
        )
        await self._refresh_execution_cut_geometry(
            plan,
            reason="BEFORE_FIRST_CUT_REVIEW_APPROACH",
        )
        target_position_list, target_rpy_list = self._cut_target(
            plan,
            cut_index=0,
            approach=True,
            first_cut_review=True,
        )
        target_position = np.asarray(
            target_position_list,
            dtype=np.float64,
        )
        target_rpy = np.asarray(
            target_rpy_list,
            dtype=np.float64,
        )
        required_clearance_z_m = float(target_position[2]) + float(
            self.config["execution"][
                "minimum_clearance_above_approach_m"
            ]
        )
        if required_clearance_z_m > clearance_z_m:
            clearance_z_m = required_clearance_z_m
            if clearance_z_m > maximum_clearance_z_m:
                raise RuntimeError(
                    "recaptured first-cut target would exceed the configured "
                    f"Z ceiling ({clearance_z_m:.3f} m > "
                    f"{maximum_clearance_z_m:.3f} m)"
                )
            await self.motion_executor.move_to(
                label="RECAPTURED_VERTICAL_CLEARANCE_LIFT",
                position_m=[
                    float(target_position[0]),
                    float(target_position[1]),
                    clearance_z_m,
                ],
                rpy_rad=current_rpy.tolist(),
                **common,
            )
        await self.motion_executor.move_to(
            label="RECAPTURED_XY_ALIGNMENT_AT_CLEARANCE",
            position_m=[
                float(target_position[0]),
                float(target_position[1]),
                clearance_z_m,
            ],
            rpy_rad=current_rpy.tolist(),
            **common,
        )
        await self.motion_executor.move_to(
            label="VERTICAL_DESCENT_TO_FIRST_APPROACH",
            position_m=target_position.tolist(),
            rpy_rad=target_rpy.tolist(),
            requested_speed_m_s=min(0.08, transfer_speed_m_s),
            kp_multiplier=transfer_kp,
            minimum_duration_s=minimum_duration_s,
            require_arrival=True,
        )

    async def _prealign_first_cut_at_clearance(
        self,
        plan: dict[str, Any],
    ) -> None:
        frame = await self._capture_skill_frame()
        (
            review_entry_camera,
            review_exit_camera,
            review_frame_transforms,
        ) = await self._first_cut_review_line_camera(plan, frame)
        overlay = render_first_cut_target_overlay(
            frame.rgb,
            entry_camera_m=review_entry_camera,
            exit_camera_m=review_exit_camera,
            intrinsics=frame.intrinsics,
            board_entry_camera_m=plan["cuts"][0]["entry_camera_m"],
            board_exit_camera_m=plan["cuts"][0]["exit_camera_m"],
        )
        overlay_rgb = cv2.cvtColor(
            cv2.imdecode(
                np.frombuffer(overlay, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            ),
            cv2.COLOR_BGR2RGB,
        )
        depth_rgb, depth_evidence = render_registered_depth_evidence(
            frame.depth_m
        )
        depth_overlay = render_first_cut_target_overlay(
            depth_rgb,
            entry_camera_m=review_entry_camera,
            exit_camera_m=review_exit_camera,
            intrinsics=frame.intrinsics,
            board_entry_camera_m=plan["cuts"][0]["entry_camera_m"],
            board_exit_camera_m=plan["cuts"][0]["exit_camera_m"],
        )
        depth_overlay_rgb = cv2.cvtColor(
            cv2.imdecode(
                np.frombuffer(depth_overlay, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            ),
            cv2.COLOR_BGR2RGB,
        )
        target_camera = (
            np.asarray(review_entry_camera, dtype=np.float64)
            + np.asarray(review_exit_camera, dtype=np.float64)
        ) / 2.0
        observation = await self._vision().assess_first_cut_alignment(
            overlay_rgb,
            depth_overlay_rgb,
            depth_near_m=float(depth_evidence["near_m"]),
            depth_far_m=float(depth_evidence["far_m"]),
            target_depth_m=float(target_camera[2]),
        )
        observation, workspace_presence_recheck = (
            await self._resolve_workspace_presence_alert(
                observation,
                frame.rgb,
            )
        )
        pixel_measurement = build_first_cut_pixel_servo_measurement(
            observation,
            image_shape=frame.rgb.shape,
            intrinsics=frame.intrinsics,
            target_camera_m=target_camera,
            no_correction_tolerance_mm=float(
                self.config["handoff"]["first_cut_alignment"][
                    "no_correction_tolerance_mm"
                ]
            ),
        )
        camera_translation_payload = pixel_measurement[
            "translation_offset_camera_mm"
        ]
        camera_translation_mm = np.asarray(
            [
                camera_translation_payload["x"],
                camera_translation_payload["y"],
                camera_translation_payload["z"],
            ],
            dtype=np.float64,
        )
        arm_from_camera_payload = review_frame_transforms[
            "arm_from_camera"
        ]
        arm_from_camera_rotation = transform_matrix(
            arm_from_camera_payload["translation_m"],
            arm_from_camera_payload["rotation_xyzw"],
        )[:3, :3]
        arm_translation_m = (
            arm_from_camera_rotation @ camera_translation_mm
        ) / 1000.0
        converted_observation = {
            **observation,
            "image_plane_alignment_meaningful": bool(
                pixel_measurement["meaningful_without_correction"]
            ),
            "meaningful_without_correction": bool(
                pixel_measurement["meaningful_without_correction"]
            ),
            "translation_offset_arm_base_mm": dict(
                zip(
                    ("x", "y", "z"),
                    (arm_translation_m * 1000.0).tolist(),
                    strict=True,
                )
            ),
            "rotation_offset_arm_base_deg": {
                "roll": 0.0,
                "pitch": 0.0,
                "yaw": 0.0,
            },
        }
        correction = build_first_cut_alignment_correction(
            converted_observation,
            self.config["handoff"]["first_cut_alignment"],
        )
        await self.artifacts.set_images(
            rgb_jpeg=encode_rgb_jpeg(frame.rgb),
            depth_png=encode_depth_png(frame.depth_m),
            overlay_jpeg=overlay,
        )
        if correction["status"] == "REJECTED_OBSERVATION":
            raise RuntimeError(
                "clearance-height first-cut visual prealignment was rejected: "
                + "; ".join(correction["quality_reasons"])
            )

        state = await self.integrated.state()
        measured = (
            (state.get("model_view") or {}).get(
                "measured_controlled_frame"
            )
            or {}
        )
        current_position = np.asarray(
            measured.get("position_m"),
            dtype=np.float64,
        )
        if (
            current_position.shape != (3,)
            or not np.all(np.isfinite(current_position))
        ):
            raise RuntimeError(
                "Integrated measured controlled frame is unavailable after "
                "the clearance lift"
            )
        uncorrected_target_list, _ = self._cut_target(
            plan,
            cut_index=0,
            approach=True,
            first_cut_review=True,
        )
        uncorrected_target = np.asarray(
            uncorrected_target_list,
            dtype=np.float64,
        )
        board_normal = np.asarray(
            plan["board"]["normal_arm_base"],
            dtype=np.float64,
        )
        board_normal /= np.linalg.norm(board_normal)
        tangent_translation = arm_translation_m - board_normal * float(
            arm_translation_m @ board_normal
        )
        visually_aligned_target = current_position + tangent_translation
        visually_aligned_target += board_normal * float(
            (uncorrected_target - visually_aligned_target) @ board_normal
        )
        absolute_transform_correction = (
            visually_aligned_target - uncorrected_target
        )
        maximum_translation_m = (
            float(
                self.config["handoff"]["first_cut_alignment"][
                    "maximum_translation_mm"
                ]
            )
            / 1000.0
        )
        correction_norm_m = float(
            np.linalg.norm(absolute_transform_correction)
        )
        if correction_norm_m > maximum_translation_m:
            raise RuntimeError(
                "clearance-height visual prealignment found an absolute "
                f"translation correction of {correction_norm_m * 1000.0:.1f} "
                "mm, exceeding the configured limit"
            )
        candidate_execution_translation = (
            self.execution_translation_arm_base_m
            + absolute_transform_correction
        )
        self._assert_corrected_cut_workspace(
            plan,
            state,
            translation_arm_base_m=candidate_execution_translation,
        )
        self.execution_translation_arm_base_m = (
            candidate_execution_translation
        )
        await self._record_execution_event(
            {
                "state": "FIRST_CUT_ABSOLUTE_TRANSLATION_VISUALLY_PREALIGNED",
                "capture_timestamp_us": frame.timestamp_us,
                "deterministic_pixel_servo_measurement": pixel_measurement,
                "vlm_observation": observation,
                "workspace_presence_recheck": workspace_presence_recheck,
                "current_controlled_frame_position_m": (
                    current_position.tolist()
                ),
                "uncorrected_first_cut_review_target_m": (
                    uncorrected_target.tolist()
                ),
                "relative_camera_to_arm_translation_m": (
                    arm_translation_m.tolist()
                ),
                "board_tangent_translation_m": (
                    tangent_translation.tolist()
                ),
                "visually_aligned_first_cut_review_target_m": (
                    visually_aligned_target.tolist()
                ),
                "absolute_transform_correction_m": (
                    absolute_transform_correction.tolist()
                ),
                "absolute_transform_correction_norm_mm": (
                    correction_norm_m * 1000.0
                ),
                "motion_submitted": False,
                "semantics": (
                    "REPLACE_UNSTABLE_ABSOLUTE_CAMERA_TRANSLATION_BEFORE_XY_TRANSFER"
                ),
            }
        )

    async def _refresh_execution_cut_geometry(
        self,
        plan: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        frame = await self._capture_skill_frame()
        frame_transforms = await self._capture_frame_transforms(frame)
        arm_from_camera_query = frame_transforms["arm_from_camera"]
        arm_from_camera = transform_matrix(
            arm_from_camera_query["translation_m"],
            arm_from_camera_query["rotation_xyzw"],
        )
        camera_centers = np.asarray(
            [cut["center_camera_m"] for cut in plan["cuts"]],
            dtype=np.float64,
        )
        if (
            camera_centers.ndim != 2
            or camera_centers.shape[1:] != (3,)
            or not np.all(np.isfinite(camera_centers))
        ):
            raise RuntimeError(
                "plan camera-space cut centers are unavailable for "
                "execution-time endpoint refresh"
            )
        raw_refreshed_centers = transform_points(
            arm_from_camera,
            camera_centers,
        )
        refreshed_centers = raw_refreshed_centers
        frozen_centers = np.asarray(
            [cut["center_arm_base_m"] for cut in plan["cuts"]],
            dtype=np.float64,
        )
        self.execution_cut_centers_arm_base_m = refreshed_centers
        await self._record_execution_event(
            {
                "state": "EXECUTION_CUT_GEOMETRY_REFRESHED",
                "reason": reason,
                "capture_timestamp_us": frame.timestamp_us,
                "camera_frame": frame.camera_frame,
                "arm_from_camera_translation_m": (
                    arm_from_camera[:3, 3].tolist()
                ),
                "arm_from_camera_rotation_xyzw": (
                    matrix_quaternion_xyzw(
                        arm_from_camera[:3, :3]
                    ).tolist()
                ),
                "first_cut_frozen_arm_base_m": (
                    frozen_centers[0].tolist()
                ),
                "first_cut_raw_refreshed_arm_base_m": (
                    raw_refreshed_centers[0].tolist()
                ),
                "first_cut_refreshed_arm_base_m": (
                    refreshed_centers[0].tolist()
                ),
                "first_cut_refresh_delta_m": (
                    refreshed_centers[0] - frozen_centers[0]
                ).tolist(),
                "alignment_translation_correction_m": [0.0, 0.0, 0.0],
                "alignment_correction_record": None,
                "controlled_frame_offset_application": (
                    "INTEGRATED_ONLY_NOT_ADDED_TO_TARGET_POSITION"
                ),
                "cut_count": int(refreshed_centers.shape[0]),
                "motion_submitted": False,
            }
        )

    async def _first_cut_review_line_camera(
        self,
        plan: dict[str, Any],
        frame: RgbdFrame,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        frame_transforms = await self._capture_frame_transforms(frame)
        arm_from_camera_payload = frame_transforms["arm_from_camera"]
        arm_from_camera = transform_matrix(
            arm_from_camera_payload["translation_m"],
            arm_from_camera_payload["rotation_xyzw"],
        )
        first_cut = plan["cuts"][0]
        board_normal = np.asarray(
            plan["board"]["normal_arm_base"],
            dtype=np.float64,
        )
        board_normal /= np.linalg.norm(board_normal)
        approach_offset_m = (
            float(
                self.config["handoff"][
                    "first_cut_review_board_offset_mm"
                ]
            )
            / 1000.0
        )
        review_offset_camera = (
            arm_from_camera[:3, :3].T
            @ (board_normal * approach_offset_m)
        )
        entry_arm = (
            np.asarray(first_cut["entry_arm_base_m"], dtype=np.float64)
            + board_normal * approach_offset_m
        )
        exit_arm = (
            np.asarray(first_cut["exit_arm_base_m"], dtype=np.float64)
            + board_normal * approach_offset_m
        )
        entry_camera = (
            np.asarray(first_cut["entry_camera_m"], dtype=np.float64)
            + review_offset_camera
        )
        exit_camera = (
            np.asarray(first_cut["exit_camera_m"], dtype=np.float64)
            + review_offset_camera
        )
        if float(entry_camera[2]) <= 0.0 or float(exit_camera[2]) <= 0.0:
            raise RuntimeError(
                "first-cut review line projects behind the RGB-D camera"
            )
        frame_transforms = {
            **frame_transforms,
            "first_cut_review_target": {
                "semantics": (
                    "CAMERA_SPACE_TARGET_FIXED_TO_ORIGINAL_RGBD_CUT_GEOMETRY"
                ),
                "translation_independent": True,
                "review_offset_camera_m": review_offset_camera.tolist(),
                "entry_arm_base_reference_m": entry_arm.tolist(),
                "exit_arm_base_reference_m": exit_arm.tolist(),
                "execution_translation_excluded": True,
            },
        }
        return entry_camera, exit_camera, frame_transforms

    async def _prepare_first_cut(self, plan: dict[str, Any]) -> None:
        try:
            await self._transfer_to_first_cut_approach(plan)
            frame = await self._capture_skill_frame()
            (
                review_entry_camera,
                review_exit_camera,
                _,
            ) = await self._first_cut_review_line_camera(plan, frame)
            overlay = render_first_cut_target_overlay(
                frame.rgb,
                entry_camera_m=review_entry_camera,
                exit_camera_m=review_exit_camera,
                intrinsics=frame.intrinsics,
                board_entry_camera_m=plan["cuts"][0]["entry_camera_m"],
                board_exit_camera_m=plan["cuts"][0]["exit_camera_m"],
            )
            await self.artifacts.set_images(
                rgb_jpeg=encode_rgb_jpeg(frame.rgb),
                depth_png=encode_depth_png(frame.depth_m),
                overlay_jpeg=overlay,
            )
            current = await self.progress.snapshot()
            execution = {
                **(current.get("execution") or {}),
                "state": "WAIT_FIRST_CUT_CONFIRMATION",
                "first_cut_alignment": {
                    "status": "AWAITING_HUMAN_REVIEW",
                    "vlm_called": False,
                    "operator_confirmation_required": True,
                    "operator_choices": [
                        "YES",
                        "NO_READJUST",
                        "FULL_STOP_GO_HOME",
                    ],
                },
                "automatic_correction_count": 0,
                "first_cut_vlm_attempt_count": 0,
                "events": list(self.execution_events),
            }
            await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.WAIT_FIRST_CUT_CONFIRMATION,
                message=(
                    "The blade is at the first-cut approach. Human review is "
                    "authoritative: select YES to cut, NO_READJUST for the "
                    "bounded VLM capture-move-recapture loop, or "
                    "FULL_STOP_GO_HOME."
                ),
                execution=execution,
                motion_submitted=bool(self.execution_events),
                error=None,
            )
        except asyncio.CancelledError:
            return
        except Exception as error:
            await self._execution_failure(error)
        finally:
            self.execution_task = None

    async def _align_first_cut(self, plan: dict[str, Any]) -> None:
        try:
            if self.motion_executor is None:
                raise RuntimeError("motion executor is unavailable")
            maximum_attempts_per_round = int(
                self.config["execution"]["first_cut_maximum_vlm_attempts"]
            )
            maximum_corrections_per_round = int(
                self.config["execution"][
                    "first_cut_maximum_automatic_corrections"
                ]
            )
            self.first_cut_alignment_round_count = (
                getattr(self, "first_cut_alignment_round_count", 0) + 1
            )
            round_number = self.first_cut_alignment_round_count
            round_attempt_count = 0
            round_correction_count = 0
            loop_history: list[dict[str, Any]] = []
            correction: dict[str, Any] | None = None
            while round_attempt_count < maximum_attempts_per_round:
                frame = await self._capture_skill_frame()
                (
                    review_entry_camera,
                    review_exit_camera,
                    review_frame_transforms,
                ) = await self._first_cut_review_line_camera(plan, frame)
                overlay = render_first_cut_target_overlay(
                    frame.rgb,
                    entry_camera_m=review_entry_camera,
                    exit_camera_m=review_exit_camera,
                    intrinsics=frame.intrinsics,
                    board_entry_camera_m=plan["cuts"][0]["entry_camera_m"],
                    board_exit_camera_m=plan["cuts"][0]["exit_camera_m"],
                )
                overlay_rgb = cv2.cvtColor(
                    cv2.imdecode(
                        np.frombuffer(overlay, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    ),
                    cv2.COLOR_BGR2RGB,
                )
                depth_rgb, depth_evidence = (
                    render_registered_depth_evidence(frame.depth_m)
                )
                depth_overlay = render_first_cut_target_overlay(
                    depth_rgb,
                    entry_camera_m=review_entry_camera,
                    exit_camera_m=review_exit_camera,
                    intrinsics=frame.intrinsics,
                    board_entry_camera_m=plan["cuts"][0]["entry_camera_m"],
                    board_exit_camera_m=plan["cuts"][0]["exit_camera_m"],
                )
                depth_overlay_rgb = cv2.cvtColor(
                    cv2.imdecode(
                        np.frombuffer(depth_overlay, dtype=np.uint8),
                        cv2.IMREAD_COLOR,
                    ),
                    cv2.COLOR_BGR2RGB,
                )
                target_depth_m = float(
                    np.mean(
                        [
                            review_entry_camera[2],
                            review_exit_camera[2],
                        ]
                    )
                )
                self.first_cut_alignment_attempt_count += 1
                round_attempt_count += 1
                observation = (
                    await self._vision().assess_first_cut_alignment(
                        overlay_rgb,
                        depth_overlay_rgb,
                        depth_near_m=float(depth_evidence["near_m"]),
                        depth_far_m=float(depth_evidence["far_m"]),
                        target_depth_m=target_depth_m,
                    )
                )
                observation, workspace_presence_recheck = (
                    await self._resolve_workspace_presence_alert(
                        observation,
                        frame.rgb,
                    )
                )
                pixel_servo_measurement = (
                    build_first_cut_pixel_servo_measurement(
                        observation,
                        image_shape=frame.rgb.shape,
                        intrinsics=frame.intrinsics,
                        target_camera_m=(
                            np.asarray(review_entry_camera)
                            + np.asarray(review_exit_camera)
                        )
                        / 2.0,
                        no_correction_tolerance_mm=float(
                            self.config["handoff"][
                                "first_cut_alignment"
                            ]["no_correction_tolerance_mm"]
                        ),
                    )
                )
                camera_translation_payload = pixel_servo_measurement[
                    "translation_offset_camera_mm"
                ]
                camera_translation_mm = np.asarray(
                    [
                        camera_translation_payload["x"],
                        camera_translation_payload["y"],
                        camera_translation_payload["z"],
                    ],
                    dtype=np.float64,
                )
                arm_from_camera_payload = review_frame_transforms[
                    "arm_from_camera"
                ]
                arm_from_camera_rotation = transform_matrix(
                    arm_from_camera_payload["translation_m"],
                    arm_from_camera_payload["rotation_xyzw"],
                )[:3, :3]
                arm_translation_mm = (
                    arm_from_camera_rotation @ camera_translation_mm
                )
                converted_observation = {
                    **observation,
                    "image_plane_alignment_meaningful": bool(
                        pixel_servo_measurement[
                            "meaningful_without_correction"
                        ]
                    ),
                    "meaningful_without_correction": bool(
                        pixel_servo_measurement[
                            "meaningful_without_correction"
                        ]
                    ),
                    "translation_offset_arm_base_mm": dict(
                        zip(
                            ("x", "y", "z"),
                            arm_translation_mm.tolist(),
                            strict=True,
                        )
                    ),
                    "rotation_offset_arm_base_deg": {
                        "roll": 0.0,
                        "pitch": 0.0,
                        "yaw": 0.0,
                    },
                }
                correction = build_first_cut_alignment_correction(
                    converted_observation,
                    self.config["handoff"]["first_cut_alignment"],
                )
                await self.artifacts.set_images(
                    rgb_jpeg=encode_rgb_jpeg(frame.rgb),
                    depth_png=encode_depth_png(frame.depth_m),
                    overlay_jpeg=overlay,
                )
                loop_entry = {
                    "attempt": self.first_cut_alignment_attempt_count,
                    "round": round_number,
                    "round_attempt": round_attempt_count,
                    "capture_timestamp_us": frame.timestamp_us,
                    "frame_number": frame.frame_number,
                    "depth_evidence": {
                        **depth_evidence,
                        "target_depth_m": target_depth_m,
                        "target_semantics": (
                            "CONTROLLED_BLADE_LINE_AT_REVIEW_CLEARANCE"
                        ),
                        "arm_from_camera": (
                            review_frame_transforms["arm_from_camera"]
                        ),
                        "registered_to_rgb": True,
                        "vlm_image_count": 2,
                    },
                    "vlm_observation": observation,
                    "workspace_presence_recheck": (
                        workspace_presence_recheck
                    ),
                    "deterministic_pixel_servo_measurement": (
                        pixel_servo_measurement
                    ),
                    "camera_to_arm_base_translation_conversion": {
                        "translation_offset_camera_mm": (
                            camera_translation_mm.tolist()
                        ),
                        "arm_from_camera_rotation": (
                            arm_from_camera_rotation.tolist()
                        ),
                        "translation_offset_arm_base_mm": (
                            arm_translation_mm.tolist()
                        ),
                        "orientation_correction_policy": (
                            "PRESERVE_CURRENT_CONTROLLED_FRAME_ORIENTATION"
                        ),
                    },
                    "correction": correction,
                    "move_applied": False,
                }
                loop_history.append(loop_entry)
                current = await self.progress.snapshot()
                await self.progress.update(
                    message=(
                        "Depth-aware first-cut VLM observation captured. "
                        "Validating its bounded 6D correction before motion."
                    ),
                    execution={
                        **(current.get("execution") or {}),
                        "state": "FIRST_CUT_DEPTH_AWARE_VLM_VALIDATION",
                        "first_cut_alignment": correction,
                        "first_cut_alignment_loop": list(loop_history),
                        "first_cut_vlm_attempt_count": (
                            self.first_cut_alignment_attempt_count
                        ),
                        "first_cut_alignment_round": round_number,
                        "first_cut_round_vlm_attempt_count": (
                            round_attempt_count
                        ),
                        "automatic_correction_count": (
                            self.first_cut_correction_count
                        ),
                        "round_automatic_correction_count": (
                            round_correction_count
                        ),
                    },
                )
                if correction["status"] == "REJECTED_OBSERVATION":
                    if bool(
                        observation[
                            "person_or_animal_visible_in_workspace"
                        ]
                    ):
                        raise RuntimeError(
                            "first-cut VLM detected a person or animal in "
                            "the robot workspace"
                        )
                    correction = {
                        **correction,
                        "status": "VLM_UNCERTAIN_HUMAN_REVIEW_REQUIRED",
                        "motion_usable": False,
                        "motion_submission_enabled": False,
                    }
                    loop_entry["correction"] = correction
                    loop_entry["loop_termination"] = {
                        "reason": "VLM_OBSERVATION_REJECTED_NO_MOTION",
                        "quality_reasons": list(
                            correction["quality_reasons"]
                        ),
                        "human_review_required": True,
                    }
                    break
                if correction["status"] != "CORRECTION_REVIEW_REQUIRED":
                    break
                previous_pixel_error_mm = next(
                    (
                        float(
                            entry[
                                "deterministic_pixel_servo_measurement"
                            ]["translation_magnitude_mm"]
                        )
                        for entry in reversed(loop_history[:-1])
                        if entry.get("move_applied")
                    ),
                    None,
                )
                current_pixel_error_mm = float(
                    pixel_servo_measurement[
                        "translation_magnitude_mm"
                    ]
                )
                if previous_pixel_error_mm is not None:
                    minimum_improvement_mm = float(
                        self.config["execution"][
                            "first_cut_minimum_pixel_servo_improvement_mm"
                        ]
                    )
                    actual_improvement_mm = (
                        previous_pixel_error_mm
                        - current_pixel_error_mm
                    )
                    loop_entry["pixel_servo_improvement"] = {
                        "previous_error_mm": previous_pixel_error_mm,
                        "current_error_mm": current_pixel_error_mm,
                        "actual_improvement_mm": actual_improvement_mm,
                        "minimum_improvement_mm": minimum_improvement_mm,
                    }
                    if actual_improvement_mm < minimum_improvement_mm:
                        correction = {
                            **correction,
                            "status": (
                                "VLM_RESIDUAL_NOT_IMPROVING_HUMAN_REVIEW_REQUIRED"
                            ),
                            "motion_usable": False,
                            "motion_submission_enabled": False,
                        }
                        loop_entry["correction"] = correction
                        loop_entry["loop_termination"] = {
                            "reason": (
                                "PIXEL_SERVO_RESIDUAL_DID_NOT_IMPROVE"
                            ),
                            "actual_improvement_mm": actual_improvement_mm,
                            "minimum_improvement_mm": (
                                minimum_improvement_mm
                            ),
                            "human_review_required": True,
                        }
                        break
                if (
                    round_correction_count
                    >= maximum_corrections_per_round
                ):
                    loop_entry["loop_termination"] = {
                        "reason": (
                            "ROUND_AUTOMATIC_CORRECTION_LIMIT_REACHED"
                        ),
                        "maximum_corrections_per_round": (
                            maximum_corrections_per_round
                        ),
                        "human_review_required": True,
                        "another_operator_requested_round_available": True,
                    }
                    break
                requested_translation = np.asarray(
                    correction["translation_arm_base_m"],
                    dtype=np.float64,
                )
                previous_requested_translation = next(
                    (
                        np.asarray(
                            entry["iterative_motion_bound"][
                                "requested_translation_arm_base_m"
                            ],
                            dtype=np.float64,
                        )
                        for entry in reversed(loop_history[:-1])
                        if entry.get("move_applied")
                        and entry.get("iterative_motion_bound")
                    ),
                    None,
                )
                if previous_requested_translation is not None:
                    denominator = float(
                        np.linalg.norm(previous_requested_translation)
                        * np.linalg.norm(requested_translation)
                    )
                    direction_cosine = (
                        float(
                            previous_requested_translation
                            @ requested_translation
                        )
                        / max(denominator, 1e-12)
                    )
                    minimum_direction_cosine = float(
                        self.config["execution"][
                            "first_cut_minimum_consecutive_direction_cosine"
                        ]
                    )
                    loop_entry["direction_consistency"] = {
                        "previous_requested_translation_arm_base_m": (
                            previous_requested_translation.tolist()
                        ),
                        "current_requested_translation_arm_base_m": (
                            requested_translation.tolist()
                        ),
                        "cosine": direction_cosine,
                        "minimum_cosine": minimum_direction_cosine,
                    }
                    if direction_cosine < minimum_direction_cosine:
                        correction = {
                            **correction,
                            "status": (
                                "VLM_DIRECTION_REVERSAL_HUMAN_REVIEW_REQUIRED"
                            ),
                            "motion_usable": False,
                            "motion_submission_enabled": False,
                        }
                        loop_entry["correction"] = correction
                        loop_entry["loop_termination"] = {
                            "reason": (
                                "CONSECUTIVE_VLM_TRANSLATION_DIRECTION_REVERSAL"
                            ),
                            "direction_cosine": direction_cosine,
                            "minimum_cosine": minimum_direction_cosine,
                            "human_review_required": True,
                        }
                        break
                maximum_iteration_translation_m = (
                    float(
                        self.config["execution"][
                            "first_cut_maximum_translation_per_iteration_mm"
                        ]
                    )
                    / 1000.0
                )
                requested_translation_magnitude = float(
                    np.linalg.norm(requested_translation)
                )
                translation_scale = min(
                    1.0,
                    maximum_iteration_translation_m
                    / max(requested_translation_magnitude, 1e-12),
                )
                translation = requested_translation * translation_scale
                board_normal = np.asarray(
                    plan["board"]["normal_arm_base"],
                    dtype=np.float64,
                )
                board_normal /= np.linalg.norm(board_normal)
                review_offset_m = (
                    float(
                        self.config["handoff"][
                            "first_cut_review_board_offset_mm"
                        ]
                    )
                    / 1000.0
                )
                minimum_review_offset_m = (
                    float(
                        self.config["handoff"][
                            "minimum_approach_board_offset_mm"
                        ]
                    )
                    / 1000.0
                )
                minimum_cumulative_normal_m = (
                    minimum_review_offset_m - review_offset_m
                )
                candidate_cumulative_translation = (
                    self.execution_translation_arm_base_m + translation
                )
                candidate_normal_m = float(
                    candidate_cumulative_translation @ board_normal
                )
                review_height_floor_applied = (
                    candidate_normal_m < minimum_cumulative_normal_m
                )
                if review_height_floor_applied:
                    translation += board_normal * (
                        minimum_cumulative_normal_m - candidate_normal_m
                    )
                candidate_cumulative_translation = (
                    self.execution_translation_arm_base_m + translation
                )
                integrated_state = await self.integrated.state()
                try:
                    workspace_report = (
                        self._assert_corrected_cut_workspace(
                            plan,
                            integrated_state,
                            translation_arm_base_m=(
                                candidate_cumulative_translation
                            ),
                        )
                    )
                except RuntimeError as error:
                    correction = {
                        **correction,
                        "status": (
                            "CORRECTED_SEQUENCE_OUTSIDE_WORKSPACE"
                        ),
                        "motion_usable": False,
                        "motion_submission_enabled": False,
                        "quality_reasons": [
                            *correction.get("quality_reasons", []),
                            str(error),
                        ],
                    }
                    loop_entry["correction"] = correction
                    loop_entry["loop_termination"] = {
                        "reason": "CORRECTED_SEQUENCE_OUTSIDE_WORKSPACE",
                        "error": str(error),
                        "human_review_required": True,
                    }
                    break
                rotation = np.asarray(
                    correction["rotation_offset_rpy_rad"],
                    dtype=np.float64,
                )
                loop_entry["iterative_motion_bound"] = {
                    "requested_translation_arm_base_m": (
                        requested_translation.tolist()
                    ),
                    "requested_translation_magnitude_m": (
                        requested_translation_magnitude
                    ),
                    "maximum_translation_per_iteration_m": (
                        maximum_iteration_translation_m
                    ),
                    "applied_translation_arm_base_m": (
                        translation.tolist()
                    ),
                    "applied_translation_scale": translation_scale,
                    "review_height_floor": {
                        "applied": review_height_floor_applied,
                        "board_normal_arm_base": board_normal.tolist(),
                        "review_offset_m": review_offset_m,
                        "minimum_review_offset_m": minimum_review_offset_m,
                        "minimum_cumulative_normal_m": (
                            minimum_cumulative_normal_m
                        ),
                    },
                    "recapture_required_after_move": True,
                    "corrected_sequence_workspace": workspace_report,
                }
                self.execution_translation_arm_base_m = (
                    candidate_cumulative_translation
                )
                self.execution_rotation_rpy_rad += rotation
                self.first_cut_correction_count += 1
                round_correction_count += 1
                position, rpy = self._cut_target(
                    plan,
                    cut_index=0,
                    approach=True,
                    first_cut_review=True,
                )
                await self.motion_executor.move_to(
                    label="FIRST_CUT_VLM_CAMERA_TRANSLATION_CORRECTION",
                    position_m=position,
                    rpy_rad=rpy,
                    requested_speed_m_s=min(
                        0.08,
                        float(
                            self.config["handoff"][
                                "requested_transfer_speed_m_s"
                            ]
                        ),
                    ),
                    kp_multiplier=float(
                        self.config["execution"]["transfer_kp_multiplier"]
                    ),
                    minimum_duration_s=float(
                        self.config["execution"][
                            "minimum_transfer_duration_s"
                        ]
                    ),
                    require_arrival=True,
                )
                loop_entry["move_applied"] = True
                loop_entry["cumulative_translation_arm_base_m"] = (
                    self.execution_translation_arm_base_m.tolist()
                )
                loop_entry["cumulative_rotation_offset_rpy_rad"] = (
                    self.execution_rotation_rpy_rad.tolist()
                )
                current = await self.progress.snapshot()
                await self.progress.update(
                    message=(
                        "One bounded camera-to-arm translation correction "
                        "completed in gravity-float. Recapturing RGB-D."
                    ),
                    execution={
                        **(current.get("execution") or {}),
                        "state": "FIRST_CUT_RECAPTURE_AFTER_CORRECTION",
                        "first_cut_alignment": correction,
                        "first_cut_alignment_loop": list(loop_history),
                        "first_cut_vlm_attempt_count": (
                            self.first_cut_alignment_attempt_count
                        ),
                        "first_cut_alignment_round": round_number,
                        "first_cut_round_vlm_attempt_count": (
                            round_attempt_count
                        ),
                        "automatic_correction_count": (
                            self.first_cut_correction_count
                        ),
                        "round_automatic_correction_count": (
                            round_correction_count
                        ),
                        "global_plan_translation_arm_base_m": (
                            self.execution_translation_arm_base_m.tolist()
                        ),
                        "global_plan_rotation_offset_rpy_rad": (
                            self.execution_rotation_rpy_rad.tolist()
                        ),
                    },
                )
            if correction is None:
                raise RuntimeError(
                    "the configured first-cut VLM round produced no observation"
                )
            current = await self.progress.snapshot()
            moves_applied = sum(
                1 for entry in loop_history if entry.get("move_applied")
            )
            execution = {
                **(current.get("execution") or {}),
                "state": "WAIT_FIRST_CUT_CONFIRMATION",
                "first_cut_alignment": correction,
                "first_cut_alignment_loop": loop_history,
                "first_cut_alignment_round": round_number,
                "first_cut_round_vlm_attempt_count": round_attempt_count,
                "round_automatic_correction_count": (
                    round_correction_count
                ),
                "round_moves_applied": moves_applied,
                "another_operator_requested_vlm_round_available": True,
                "automatic_correction_count": (
                    self.first_cut_correction_count
                ),
                "first_cut_vlm_attempt_count": (
                    self.first_cut_alignment_attempt_count
                ),
                "global_plan_translation_arm_base_m": (
                    self.execution_translation_arm_base_m.tolist()
                ),
                "global_plan_rotation_offset_rpy_rad": (
                    self.execution_rotation_rpy_rad.tolist()
                ),
                "events": list(self.execution_events),
            }
            rejection_reasons = [
                str(reason)
                for entry in loop_history
                for reason in (
                    (entry.get("loop_termination") or {}).get(
                        "quality_reasons"
                    )
                    or []
                )
            ]
            if moves_applied == 0 and rejection_reasons:
                round_message = (
                    f"First-cut VLM round {round_number} made no move because "
                    "the observation was rejected: "
                    f"{'; '.join(dict.fromkeys(rejection_reasons))}. "
                )
            else:
                round_message = (
                    f"First-cut VLM round {round_number} completed with "
                    f"{moves_applied} bounded correction move(s). "
                )
            await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.WAIT_FIRST_CUT_CONFIRMATION,
                message=(
                    f"{round_message}Human "
                    "review is authoritative: select YES to cut, "
                    "NO_READJUST to run another bounded VLM round, or "
                    "FULL_STOP_GO_HOME."
                ),
                execution=execution,
                motion_submitted=bool(self.execution_events),
                error=None,
            )
        except asyncio.CancelledError:
            return
        except Exception as error:
            await self._execution_failure(error)
        finally:
            if asyncio.current_task() is self.execution_task:
                self.execution_task = None

    async def _run_cut_sequence(self, plan: dict[str, Any]) -> None:
        try:
            if self.motion_executor is None:
                raise RuntimeError("motion executor is unavailable")
            cuts = list(plan["cuts"])
            board_normal = np.asarray(
                plan["board"]["normal_arm_base"],
                dtype=np.float64,
            )
            board_normal /= np.linalg.norm(board_normal)
            post_cut_retract_m = float(
                self.config["execution"]["post_cut_retract_m"]
            )
            for index, _ in enumerate(cuts):
                if self.execution_cancelled:
                    raise asyncio.CancelledError
                if index > 0:
                    approach_position, rpy = self._cut_target(
                        plan,
                        cut_index=index,
                        approach=True,
                    )
                    contact_position, _ = self._cut_target(
                        plan,
                        cut_index=index,
                        approach=False,
                    )
                    shift_clearance_position = (
                        np.asarray(contact_position, dtype=np.float64)
                        + board_normal * post_cut_retract_m
                    )
                    await self.motion_executor.move_to(
                        label=f"SHIFT_TO_CUT_{index + 1}_CLEARANCE",
                        position_m=shift_clearance_position.tolist(),
                        rpy_rad=rpy,
                        requested_speed_m_s=float(
                            self.config["handoff"][
                                "requested_transfer_speed_m_s"
                            ]
                        ),
                        kp_multiplier=float(
                            self.config["execution"][
                                "transfer_kp_multiplier"
                            ]
                        ),
                        minimum_duration_s=float(
                            self.config["execution"][
                                "minimum_transfer_duration_s"
                            ]
                        ),
                        require_arrival=True,
                    )
                    await self.motion_executor.move_to(
                        label=f"DESCEND_TO_CUT_{index + 1}_APPROACH",
                        position_m=approach_position,
                        rpy_rad=rpy,
                        requested_speed_m_s=float(
                            self.config["handoff"][
                                "requested_transfer_speed_m_s"
                            ]
                        ),
                        kp_multiplier=float(
                            self.config["execution"][
                                "transfer_kp_multiplier"
                            ]
                        ),
                        minimum_duration_s=float(
                            self.config["execution"][
                                "minimum_transfer_duration_s"
                            ]
                        ),
                        require_arrival=True,
                    )
                contact_position, rpy = self._cut_target(
                    plan,
                    cut_index=index,
                    approach=False,
                )
                approach_position, _ = self._cut_target(
                    plan,
                    cut_index=index,
                    approach=True,
                )
                stroke_distance = float(
                    np.linalg.norm(
                        np.asarray(approach_position)
                        - np.asarray(contact_position)
                    )
                )
                cut_duration = float(
                    self.config["handoff"]["cut_duration_s"]
                )
                await self.motion_executor.move_to(
                    label=f"MIT_CUT_{index + 1}",
                    position_m=contact_position,
                    rpy_rad=rpy,
                    requested_speed_m_s=max(
                        stroke_distance / max(cut_duration, 1e-3),
                        1e-3,
                    ),
                    kp_multiplier=float(
                        self.config["handoff"]["mit_kp_multiplier"]
                    ),
                    minimum_duration_s=cut_duration,
                    require_arrival=False,
                )
                retract_position = (
                    np.asarray(contact_position, dtype=np.float64)
                    + board_normal * post_cut_retract_m
                )
                await self.motion_executor.move_to(
                    label=f"RETRACT_AFTER_CUT_{index + 1}",
                    position_m=retract_position.tolist(),
                    rpy_rad=rpy,
                    requested_speed_m_s=float(
                        self.config["handoff"][
                            "requested_transfer_speed_m_s"
                        ]
                    ),
                    kp_multiplier=float(
                        self.config["execution"]["retract_kp_multiplier"]
                    ),
                    minimum_duration_s=float(
                        self.config["execution"][
                            "minimum_transfer_duration_s"
                        ]
                    ),
                    require_arrival=True,
                )
                await self._record_execution_event(
                    {
                        "state": "CUT_COMPLETED",
                        "cut_index": index,
                        "cut_number": index + 1,
                        "coordinate_recheck": "SKIPPED_AFTER_HUMAN_APPROVAL",
                        "post_cut_retract_m": post_cut_retract_m,
                        "post_cut_retract_kp_multiplier": float(
                            self.config["execution"][
                                "retract_kp_multiplier"
                            ]
                        ),
                        "completed_at_us": time.time_ns() // 1000,
                    }
                )
                await self.progress.update(
                    state=SkillState.RUNNING,
                    phase=Phase.CUTTING,
                    message=(
                        f"Completed cut {index + 1} of {len(cuts)}; "
                        "the arm completed the strong 100 mm unstick lift."
                    ),
                    tracking=None,
                    motion_submitted=True,
                )
            current = await self.progress.snapshot()
            execution = {
                **(current.get("execution") or {}),
                "state": "WAIT_TOOL_REMOVAL",
                "completed_cut_count": len(cuts),
                "events": list(self.execution_events),
            }
            await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.WAIT_TOOL_REMOVAL,
                message=(
                    "All planned cuts completed. Physically detach and remove "
                    "the knife, then confirm tool removal to start safe "
                    "termination."
                ),
                motion_submission_enabled=False,
                motion_submitted=True,
                execution=execution,
                error=None,
            )
        except asyncio.CancelledError:
            return
        except Exception as error:
            await self._execution_failure(error)
        finally:
            self.execution_task = None

    def _cut_target(
        self,
        plan: dict[str, Any],
        *,
        cut_index: int,
        approach: bool,
        first_cut_review: bool = False,
    ) -> tuple[list[float], list[float]]:
        cut = plan["cuts"][cut_index]
        refreshed_centers = getattr(
            self,
            "execution_cut_centers_arm_base_m",
            None,
        )
        if (
            isinstance(refreshed_centers, np.ndarray)
            and refreshed_centers.ndim == 2
            and refreshed_centers.shape[1:] == (3,)
            and cut_index < refreshed_centers.shape[0]
        ):
            center = refreshed_centers[cut_index].copy()
        else:
            center = np.asarray(
                cut["center_arm_base_m"], dtype=np.float64
            )
        normal = np.asarray(
            plan["board"]["normal_arm_base"], dtype=np.float64
        )
        normal /= np.linalg.norm(normal)
        if approach and first_cut_review:
            offset_m = (
                float(
                    self.config["handoff"][
                        "first_cut_review_board_offset_mm"
                    ]
                )
                / 1000.0
            )
        elif approach:
            offset_m = (
                float(plan["execution_preview"]["approach_board_offset_mm"])
                / 1000.0
            )
        else:
            offset_m = 0.0
        position = (
            center
            + normal * offset_m
            + self.execution_translation_arm_base_m
        )
        execution_control_rpy = getattr(
            self, "execution_control_rpy_rad", None
        )
        if execution_control_rpy is not None:
            rpy = execution_control_rpy.copy()
        else:
            blade_frame = next(
                segment["target"]["blade_frame"]
                for segment in plan["execution_preview"]["segments"]
                if segment.get("cut_index") == cut_index
                and isinstance(segment.get("target"), dict)
                and isinstance(
                    segment["target"].get("blade_frame"), dict
                )
            )
            rpy = matrix_rpy(
                np.asarray(
                    blade_frame["rotation_matrix_arm_base"],
                    dtype=np.float64,
                )
            )
        rpy += self.execution_rotation_rpy_rad
        return position.tolist(), rpy.tolist()

    def _assert_corrected_cut_workspace(
        self,
        plan: dict[str, Any],
        integrated_state: dict[str, Any],
        *,
        translation_arm_base_m: np.ndarray,
    ) -> dict[str, Any]:
        workspace = (
            (integrated_state.get("runtime") or {}).get("workspace")
            or integrated_state.get("workspace")
        )
        if not isinstance(workspace, dict):
            raise RuntimeError(
                "Integrated state does not publish its configured workspace; "
                "restart the updated Integrated provider before motion"
            )
        try:
            x_limit = float(workspace["abs_x_max_m"])
            y_limit = float(workspace["abs_y_max_m"])
            z_min = float(workspace["z_min_m"])
            z_max = float(workspace["z_max_m"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                "Integrated published an invalid workspace envelope"
            ) from error
        translation = np.asarray(
            translation_arm_base_m,
            dtype=np.float64,
        )
        if translation.shape != (3,) or not np.all(
            np.isfinite(translation)
        ):
            raise RuntimeError(
                "corrected cutting translation is not a finite 3D vector"
            )
        normal = np.asarray(
            plan["board"]["normal_arm_base"],
            dtype=np.float64,
        )
        normal /= np.linalg.norm(normal)
        approach_offset_m = (
            float(plan["execution_preview"]["approach_board_offset_mm"])
            / 1000.0
        )
        review_offset_m = (
            float(
                self.config["handoff"][
                    "first_cut_review_board_offset_mm"
                ]
            )
            / 1000.0
        )
        retract_offset_m = float(
            self.config["execution"]["post_cut_retract_m"]
        )
        refreshed_centers = getattr(
            self,
            "execution_cut_centers_arm_base_m",
            None,
        )
        checked: list[dict[str, Any]] = []
        for cut_index, cut in enumerate(plan["cuts"]):
            if (
                isinstance(refreshed_centers, np.ndarray)
                and refreshed_centers.ndim == 2
                and refreshed_centers.shape[1:] == (3,)
                and cut_index < refreshed_centers.shape[0]
            ):
                center = refreshed_centers[cut_index].copy()
            else:
                center = np.asarray(
                    cut["center_arm_base_m"],
                    dtype=np.float64,
                )
            contact = center + translation
            waypoints = [
                ("CUT_CONTACT", contact),
                (
                    "CUT_APPROACH",
                    contact + normal * approach_offset_m,
                ),
                (
                    "POST_CUT_RETRACT",
                    contact + normal * retract_offset_m,
                ),
            ]
            if cut_index == 0:
                waypoints.append(
                    (
                        "FIRST_CUT_REVIEW",
                        contact + normal * review_offset_m,
                    )
                )
            for label, position in waypoints:
                x, y, z = (float(value) for value in position)
                violation: str | None = None
                if abs(x) > x_limit:
                    violation = (
                        f"X {x:.4f} m exceeds +/-{x_limit:.4f} m"
                    )
                elif abs(y) > y_limit:
                    violation = (
                        f"Y {y:.4f} m exceeds +/-{y_limit:.4f} m"
                    )
                elif z < z_min or z > z_max:
                    violation = (
                        f"Z {z:.4f} m is outside "
                        f"[{z_min:.4f}, {z_max:.4f}] m"
                    )
                item = {
                    "cut_number": cut_index + 1,
                    "label": label,
                    "position_m": [x, y, z],
                }
                checked.append(item)
                if violation is not None:
                    raise RuntimeError(
                        "corrected cutting sequence leaves the Integrated "
                        f"workspace at cut {cut_index + 1} {label}: "
                        f"{violation}. The correction was not submitted"
                    )
        return {
            "status": "WITHIN_INTEGRATED_WORKSPACE",
            "workspace": {
                "abs_x_max_m": x_limit,
                "abs_y_max_m": y_limit,
                "z_min_m": z_min,
                "z_max_m": z_max,
            },
            "translation_arm_base_m": translation.tolist(),
            "checked_waypoint_count": len(checked),
        }

    async def _record_execution_event(
        self,
        event: dict[str, Any],
    ) -> None:
        self.execution_events.append(dict(event))
        current = await self.progress.snapshot()
        execution = {
            **(current.get("execution") or {}),
            "events": list(self.execution_events),
        }
        await self.progress.update(execution=execution)

    async def _execution_failure(self, error: Exception) -> None:
        self.execution_cancelled = True
        current = await self.progress.snapshot()
        error_text = str(error).strip() or type(error).__name__
        controller_state: dict[str, Any] | None = None
        controller_state_error: str | None = None
        float_result: dict[str, Any] | None = None
        float_error: str | None = None
        try:
            controller_state = await self.integrated.state()
        except Exception as state_exception:
            controller_state_error = str(state_exception)
        trajectory_active = (
            bool((controller_state.get("trajectory") or {}).get("active"))
            if controller_state is not None
            else None
        )
        float_requested = trajectory_active is not False
        if float_requested:
            try:
                float_result = await self.integrated.request_float()
            except Exception as float_exception:
                float_error = str(float_exception)
        failure_evidence = await self.capture_failure_evidence(
            error,
            context="PHYSICAL_EXECUTION",
            progress=current,
        )
        if trajectory_active is False:
            failure_state = "FAILED_NO_ACTIVE_TRAJECTORY_TOOL_PRESERVED"
            message = (
                "Physical execution stopped before another commit. Integrated "
                "reported no active trajectory, so the Skill did not send "
                "Float and preserved the current tool attachment for operator "
                "recovery."
            )
        elif trajectory_active is True:
            failure_state = "FAILED_ACTIVE_TRAJECTORY_FLOAT_REQUESTED"
            message = (
                "Physical execution failed while Integrated reported an active "
                "trajectory, so gravity-float was requested. Keep the operator "
                "present and use the tool-retaining recovery procedure."
            )
        else:
            failure_state = "FAILED_CONTROLLER_STATE_UNKNOWN_FLOAT_REQUESTED"
            message = (
                "Physical execution failed and Integrated state could not be "
                "confirmed, so gravity-float was requested as the conservative "
                "fallback. Keep the operator present."
            )
        execution = {
            **(current.get("execution") or {}),
            "state": failure_state,
            "failure": error_text,
            "failure_evidence": failure_evidence,
            "controller_state_at_failure": controller_state,
            "controller_state_error": controller_state_error,
            "trajectory_active_at_failure": trajectory_active,
            "float_requested": float_requested,
            "float_result": float_result,
            "float_error": float_error,
            "events": list(self.execution_events),
        }
        await self.progress.update(
            state=SkillState.FAILED,
            phase=Phase.FAILED,
            message=message,
            motion_submission_enabled=False,
            motion_submitted=bool(self.execution_events),
            execution=execution,
            error=error_text,
        )

    async def capture_failure_evidence(
        self,
        error: Exception,
        *,
        context: str,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured_at_us = time.time_ns() // 1000
        error_text = str(error).strip() or type(error).__name__
        record: dict[str, Any] = {
            "status": "CAPTURE_FAILED",
            "captured_at_us": captured_at_us,
            "context": str(context),
            "error": error_text,
        }
        try:
            frame = await self.camera.capture(require_vio=False)
            overlay = frame.rgb.copy()
            height, width = overlay.shape[:2]
            banner_height = min(height, 132)
            cv2.rectangle(
                overlay,
                (0, 0),
                (width - 1, banner_height - 1),
                (145, 0, 0),
                thickness=-1,
            )
            title = f"ERROR / {str(context).upper()}"
            cv2.putText(
                overlay,
                title[:100],
                (18, 35),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.82,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            words = error_text.split()
            lines: list[str] = []
            current_line = ""
            for word in words:
                candidate = f"{current_line} {word}".strip()
                if len(candidate) > 92 and current_line:
                    lines.append(current_line)
                    current_line = word
                else:
                    current_line = candidate
            if current_line:
                lines.append(current_line)
            for index, line in enumerate(lines[:3]):
                cv2.putText(
                    overlay,
                    line,
                    (18, 66 + index * 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
            rgb_jpeg = encode_rgb_jpeg(frame.rgb)
            depth_png = encode_depth_png(frame.depth_m)
            overlay_jpeg = encode_rgb_jpeg(overlay)
            await self.artifacts.set_images(
                rgb_jpeg=rgb_jpeg,
                depth_png=depth_png,
                overlay_jpeg=overlay_jpeg,
            )
            failure_root = self.settings.run_root / "failures"
            failure_root.mkdir(parents=True, exist_ok=True)
            stem = (
                f"{captured_at_us}-"
                f"{str(context).lower().replace(' ', '-')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            rgb_path = failure_root / f"{stem}-rgb.jpg"
            depth_path = failure_root / f"{stem}-depth.png"
            overlay_path = failure_root / f"{stem}-overlay.jpg"
            metadata_path = failure_root / f"{stem}-metadata.json"
            rgb_path.write_bytes(rgb_jpeg)
            depth_path.write_bytes(depth_png)
            overlay_path.write_bytes(overlay_jpeg)
            record.update(
                {
                    "status": "CAPTURED",
                    "camera_timestamp_us": frame.timestamp_us,
                    "camera_frame_number": frame.frame_number,
                    "camera_frame": frame.camera_frame,
                    "rgb_path": str(rgb_path.resolve()),
                    "depth_path": str(depth_path.resolve()),
                    "overlay_path": str(overlay_path.resolve()),
                    "metadata_path": str(metadata_path.resolve()),
                    "plan_id": (progress or {}).get("plan_id"),
                    "phase": (progress or {}).get("phase"),
                }
            )
            metadata_path.write_text(
                json.dumps(record, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as capture_error:
            record["capture_error"] = str(capture_error)
        return record

    async def _fast_execution_revalidation(self) -> dict[str, Any]:
        raise RuntimeError(
            "between-cut coordinate revalidation is disabled after human approval"
        )
        if (
            self.tracker is None
            or self.reference_mask is None
            or self.reference_board_mask is None
            or self.reference_plane is None
        ):
            raise RuntimeError("tracking reference is unavailable")
        frame = await self._capture_skill_frame()
        segmentation_source = "RGBD_HEIGHT_ABOVE_BOARD_PLANE"
        depth_refinement: dict[str, Any] | None = None
        try:
            refinement = self.config["planning"][
                "vegetable_depth_refinement"
            ]
            current_mask, depth_refinement = refine_object_mask_above_plane(
                frame.depth_m,
                frame.intrinsics,
                self.reference_board_mask,
                self.reference_plane,
                self.reference_mask,
                minimum_height_m=float(refinement["minimum_height_mm"])
                / 1000.0,
                maximum_height_m=float(refinement["maximum_height_mm"])
                / 1000.0,
                minimum_component_pixels=int(
                    refinement["minimum_component_pixels"]
                ),
                minimum_vlm_overlap_pixels=int(
                    refinement["minimum_vlm_overlap_pixels"]
                ),
            )
            confidence = 1.0
        except RuntimeError as depth_error:
            segmentation_source = "APPEARANCE_FALLBACK"
            current_mask, confidence = self.tracker.segment(frame.rgb)
            depth_refinement = {"error": str(depth_error)}
        reference_center, reference_axis = mask_geometry(
            self.reference_mask
        )
        try:
            current_center, current_axis = mask_geometry(current_mask)
            reference_point = self._pixel_to_plane(
                reference_center[::-1],
                self.reference_plane,
                frame.intrinsics,
            )
            current_point = self._pixel_to_plane(
                current_center[::-1],
                self.reference_plane,
                frame.intrinsics,
            )
            centroid_shift_mm = float(
                np.linalg.norm(current_point - reference_point) * 1000.0
            )
            cosine = float(
                np.clip(abs(reference_axis @ current_axis), -1.0, 1.0)
            )
            axis_change = float(np.degrees(np.arccos(cosine)))
            overlap = mask_iou(self.reference_mask, current_mask)
            depth_values = frame.depth_m[current_mask > 0]
            depth_fraction = float(
                np.count_nonzero(
                    np.isfinite(depth_values) & (depth_values > 0.05)
                )
                / max(depth_values.size, 1)
            )
        except RuntimeError:
            centroid_shift_mm = float("inf")
            axis_change = float("inf")
            overlap = 0.0
            depth_fraction = 0.0
            confidence = 0.0
        decision = evaluate_tracking(
            mask_iou_value=overlap,
            centroid_shift_mm=centroid_shift_mm,
            axis_change_deg=axis_change,
            confidence=confidence,
            valid_depth_fraction=depth_fraction,
            config=self.config["tracking"],
        )
        return {
            **decision.payload(),
            "mask_iou": overlap,
            "centroid_shift_mm": centroid_shift_mm,
            "axis_change_deg": axis_change,
            "valid_depth_fraction": depth_fraction,
            "vlm_called": False,
            "segmentation_source": segmentation_source,
            "depth_refinement": depth_refinement,
        }

    async def revalidate_after_cut(self) -> dict[str, Any]:
        raise RuntimeError(
            "manual coordinate revalidation is disabled in version 0.3.0"
        )
        current = await self.progress.snapshot()
        if current["phase"] != Phase.READY_FOR_OPERATOR_TAKEOVER:
            raise RuntimeError("a published plan is required before tracking revalidation")
        if (
            self.tracker is None
            or self.reference_mask is None
            or self.reference_board_mask is None
            or self.reference_plane is None
        ):
            raise RuntimeError("tracking reference is unavailable")
        await self.progress.update(
            state=SkillState.RUNNING,
            phase=Phase.TRACKING_CHECK,
            message="Running fast RGB-D workpiece tracking without a VLM call.",
        )
        frame = await self._capture_skill_frame()
        segmentation_source = "RGBD_HEIGHT_ABOVE_BOARD_PLANE"
        depth_refinement: dict[str, Any] | None = None
        try:
            refinement_config = self.config["planning"]["vegetable_depth_refinement"]
            current_mask, depth_refinement = refine_object_mask_above_plane(
                frame.depth_m,
                frame.intrinsics,
                self.reference_board_mask,
                self.reference_plane,
                self.reference_mask,
                minimum_height_m=float(refinement_config["minimum_height_mm"]) / 1000.0,
                maximum_height_m=float(refinement_config["maximum_height_mm"]) / 1000.0,
                minimum_component_pixels=int(
                    refinement_config["minimum_component_pixels"]
                ),
                minimum_vlm_overlap_pixels=int(
                    refinement_config["minimum_vlm_overlap_pixels"]
                ),
            )
            confidence = 1.0
        except RuntimeError as depth_error:
            segmentation_source = "APPEARANCE_FALLBACK"
            current_mask, confidence = self.tracker.segment(frame.rgb)
            depth_refinement = {"error": str(depth_error)}
        reference_center, reference_axis = mask_geometry(self.reference_mask)
        try:
            current_center, current_axis = mask_geometry(current_mask)
            reference_point = self._pixel_to_plane(
                reference_center[::-1],
                self.reference_plane,
                frame.intrinsics,
            )
            current_point = self._pixel_to_plane(
                current_center[::-1],
                self.reference_plane,
                frame.intrinsics,
            )
            centroid_shift_mm = float(np.linalg.norm(current_point - reference_point) * 1000.0)
            cosine = float(np.clip(abs(reference_axis @ current_axis), -1.0, 1.0))
            axis_change = float(np.degrees(np.arccos(cosine)))
            overlap = mask_iou(self.reference_mask, current_mask)
            depth_values = frame.depth_m[current_mask > 0]
            depth_fraction = float(
                np.count_nonzero(np.isfinite(depth_values) & (depth_values > 0.05))
                / max(depth_values.size, 1)
            )
        except RuntimeError:
            centroid_shift_mm = float("inf")
            axis_change = float("inf")
            overlap = 0.0
            depth_fraction = 0.0
            confidence = 0.0
        decision = evaluate_tracking(
            mask_iou_value=overlap,
            centroid_shift_mm=centroid_shift_mm,
            axis_change_deg=axis_change,
            confidence=confidence,
            valid_depth_fraction=depth_fraction,
            config=self.config["tracking"],
        )
        tracking = {
            **decision.payload(),
            "mask_iou": overlap,
            "centroid_shift_mm": centroid_shift_mm,
            "axis_change_deg": axis_change,
            "valid_depth_fraction": depth_fraction,
            "vlm_called": False,
            "vlm_requery_count": self.vlm_requery_count,
            "segmentation_empty": not bool(np.count_nonzero(current_mask)),
            "segmentation_source": segmentation_source,
            "depth_refinement": depth_refinement,
        }
        await self.progress.update(tracking=tracking)
        if not decision.request_vlm:
            await self.progress.update(
                state=SkillState.READY_FOR_OPERATOR_TAKEOVER,
                phase=Phase.READY_FOR_OPERATOR_TAKEOVER,
                message="Fast tracking passed; the current plan remains valid without a VLM call.",
                tracking=tracking,
            )
            await self._publish_tracking(tracking)
            return tracking

        maximum_requeries = int(self.config["tracking"]["maximum_vlm_requeries"])
        if self.vlm_requery_count >= maximum_requeries:
            tracking["replan_required"] = True
            tracking["vlm_blocked"] = "maximum VLM requery count reached"
            await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.PERCEIVING,
                message="Tracking degraded and the VLM retry budget is exhausted. Operator review is required.",
                tracking=tracking,
            )
            await self._publish_tracking(tracking)
            return tracking

        self.vlm_requery_count += 1
        try:
            frame_transforms = await self._capture_frame_transforms(frame)
            scene = await self._vision().locate(frame.rgb)
            plan = await self._build_plan(
                frame,
                scene,
                frame_transforms,
                reason="TRACKING_DEGRADATION_VLM",
            )
            await self._accept_reference(frame, scene, plan)
        except (RuntimeError, ValueError) as error:
            tracking.update(
                {
                    "vlm_called": True,
                    "vlm_requery_count": self.vlm_requery_count,
                    "vlm_error": str(error),
                    "replan_required": True,
                }
            )
            await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.PERCEIVING,
                message=(
                    "Fast tracking degraded and the VLM retry was rejected. "
                    "Operator review is required."
                ),
                tracking=tracking,
                error=str(error),
            )
            await self._publish_tracking(tracking)
            return tracking
        tracking.update(
            {
                "vlm_called": True,
                "vlm_requery_count": self.vlm_requery_count,
                "replacement_plan_id": plan["plan_id"],
                "replacement_plan_revision": plan["plan_revision"],
            }
        )
        await self.progress.update(tracking=tracking)
        await self._publish_tracking(tracking)
        return tracking

    async def abort(self, reason: str) -> dict[str, Any]:
        self.execution_cancelled = True
        active_task = self.execution_task
        if active_task is not None and not active_task.done():
            active_task.cancel()
            if active_task is not asyncio.current_task():
                try:
                    await active_task
                except (asyncio.CancelledError, Exception):
                    pass
        float_result: dict[str, Any] | None = None
        float_error: str | None = None
        try:
            float_result = await self.integrated.request_float()
        except Exception as error:
            float_error = str(error)
        integrated_state: dict[str, Any] | None = None
        state_error: str | None = None
        try:
            integrated_state = await self.integrated.state()
        except Exception as error:
            state_error = str(error)
        current = await self.progress.snapshot()
        tool_attached = bool(
            current.get("operator_tool_attachment_confirmed")
        )
        execution = {
            **(current.get("execution") or {}),
            "state": (
                "ABORTED_WAIT_TOOL_REMOVAL"
                if tool_attached
                else "ABORTED_FLOAT_REQUESTED"
            ),
            "abort_reason": str(reason or "operator requested abort"),
            "float_result": float_result,
            "float_error": float_error,
            "integrated_state_error": state_error,
            "events": list(self.execution_events),
        }
        if tool_attached:
            return await self.progress.update(
                state=SkillState.WAITING_FOR_OPERATOR,
                phase=Phase.WAIT_TOOL_REMOVAL,
                message=(
                    "Workflow stopped and Integrated gravity-float was requested. "
                    "Physically detach and remove the knife, then confirm tool "
                    "removal to start safe termination."
                ),
                error=str(reason or "operator requested abort"),
                motion_submission_enabled=False,
                motion_submitted=bool(self.execution_events),
                execution=execution,
            )
        return await self.progress.update(
            state=SkillState.ABORTED,
            phase=Phase.ABORTED,
            message=(
                "Workflow aborted and Integrated gravity-float was requested. "
                "Use the tool-retaining recovery procedure before safe-home."
            ),
            error=str(reason or "operator requested abort"),
            motion_submission_enabled=False,
            motion_submitted=bool(self.execution_events),
            execution=execution,
        )

    async def _alignment_snapshot(self) -> dict[str, Any]:
        stream = str(self.config["alignment"]["result_stream"])
        observation = await self.fabric.latest_optional(stream)
        if not observation:
            raise RuntimeError("no stationary world/arm alignment result is available")
        result = observation.get("data") or {}
        if bool(self.config["alignment"]["require_valid"]) and not bool(result.get("valid")):
            raise RuntimeError("latest stationary alignment result is invalid")
        now_us = time.time_ns() // 1000
        if bool(
            self.config["alignment"].get(
                "require_reviewed_motion_usable",
                True,
            )
        ) and (
            result.get("review_state") != "ACCEPTED"
            or result.get("motion_usable") is not True
        ):
            raise RuntimeError(
                "latest stationary alignment is a non-motion candidate; "
                "an accepted and activated calibration is required"
            )
        expires_at_us = int(result.get("expires_at_us") or 0)
        if expires_at_us <= 0 or now_us > expires_at_us:
            raise RuntimeError("latest stationary alignment has expired")
        created_at_us = int(
            result.get("created_at_us")
            or observation.get("observed_at_us")
            or 0
        )
        age_s = max(0.0, (now_us - created_at_us) / 1_000_000.0)
        if age_s > float(self.config["alignment"]["maximum_age_s"]):
            raise RuntimeError(f"stationary alignment is stale ({age_s:.1f} seconds)")
        if (
            bool(self.config["alignment"]["require_same_camera_calibration_revision"])
            and not result.get("camera_calibration_revision")
        ):
            raise RuntimeError(
                "stationary alignment does not record a camera calibration revision; "
                "rerun alignment before planning"
            )
        return {
            "alignment_id": result.get("alignment_id"),
            "valid": bool(result.get("valid")),
            "review_state": result.get("review_state"),
            "motion_usable": result.get("motion_usable") is True,
            "expires_at_us": expires_at_us,
            "created_at_us": created_at_us,
            "age_s": age_s,
            "world_frame": result.get("world_frame"),
            "vio_world_frame": result.get("vio_world_frame"),
            "vio_session_epoch": result.get("vio_session_epoch"),
            "camera_frame": result.get("camera_frame"),
            "camera_reference_timestamp_us": result.get(
                "camera_reference_timestamp_us"
            ),
            "camera_calibration_revision": result.get("camera_calibration_revision"),
            "vio_from_camera_reference": result.get(
                "vio_from_camera_reference"
            ),
            "world_from_vio": result.get("world_from_vio"),
            "world_from_base": result.get("world_from_base"),
            "mode": result.get("mode"),
        }

    async def alignment_status(self) -> dict[str, Any]:
        try:
            return await self._alignment_snapshot()
        except Exception as error:
            return {
                "valid": False,
                "alignment_id": None,
                "error": str(error),
            }

    def _vision(self) -> SceneVision:
        if self.scene_vision is None:
            self.scene_vision = SceneVision(
                self.settings.openai_api_key,
                self.settings.openai_vision_model,
            )
        return self.scene_vision

    async def _resolve_workspace_presence_alert(
        self,
        observation: dict[str, Any],
        rgb: np.ndarray,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        initially_reported = bool(
            observation["person_or_animal_visible_in_workspace"]
        )
        if not initially_reported:
            return observation, {
                "required": False,
                "initially_reported": False,
                "confirmed": False,
                "policy": "NO_ALERT_NO_RECHECK",
            }
        recheck = await self._vision().recheck_workspace_presence(rgb)
        confirmed = bool(
            recheck["person_or_animal_visible_in_workspace"]
        )
        return {
            **observation,
            "person_or_animal_visible_in_workspace": confirmed,
        }, {
            "required": True,
            "initially_reported": True,
            "confirmed": confirmed,
            "policy": (
                "BLOCK_ONLY_WHEN_FOCUSED_SECOND_VLM_CONFIRMS_PRESENCE"
            ),
            "recheck_observation": recheck,
        }

    async def _capture_skill_frame(self) -> RgbdFrame:
        if getattr(self, "stationary_camera_transform_lock", None) is None:
            return await self.camera.capture()
        return await self.camera.capture(require_vio=False)

    async def _capture_frame_transforms(
        self,
        frame: RgbdFrame,
    ) -> dict[str, Any]:
        alignment = await self._alignment_snapshot()
        if str(alignment.get("camera_frame") or "") != frame.camera_frame:
            raise RuntimeError(
                "stationary alignment camera frame does not match the RGB-D frame"
            )
        camera_lock = getattr(
            self,
            "stationary_camera_transform_lock",
            None,
        )
        lock_was_reused = camera_lock is not None
        live_vio_from_camera: dict[str, Any] | None = None
        if camera_lock is not None:
            if camera_lock["alignment_id"] != alignment.get("alignment_id"):
                raise RuntimeError(
                    "stationary alignment changed after the fixed camera "
                    "transform was locked"
                )
            arm_from_camera = dict(camera_lock["arm_from_camera"])
            arm_from_camera["stationary_camera_pose_lock"] = {
                **dict(
                    arm_from_camera.get("stationary_camera_pose_lock")
                    or {}
                ),
                "reused": True,
                "reuse_capture_timestamp_us": frame.timestamp_us,
                "live_vio_queried": False,
            }
            live_arm_from_camera = None
            camera_arm_parent_chain = camera_lock[
                "camera_arm_parent_chain"
            ]
            camera_vio_parent_chain = camera_lock[
                "camera_vio_parent_chain"
            ]
        else:
            if str(alignment.get("vio_session_epoch") or "") != str(
                frame.session_epoch or ""
            ):
                raise RuntimeError(
                    "stationary alignment VIO epoch does not match the RGB-D frame"
                )
            vio_world_frame = str(alignment.get("vio_world_frame") or "")
            if not vio_world_frame:
                raise RuntimeError(
                    "stationary alignment does not record its VIO world frame"
                )
            query = {
                "at_us": frame.timestamp_us,
                "max_extrapolation_us": int(
                    self.config["alignment"][
                        "transform_max_extrapolation_us"
                    ]
                ),
                "session_epoch": frame.session_epoch,
            }
            try:
                live_arm_from_camera = await self.fabric.transform(
                    from_frame=frame.camera_frame,
                    to_frame=self.config["frames"]["arm_base"],
                    **query,
                )
                camera_arm_parent_chain = (
                    self._validate_transform_parent_chain(
                        live_arm_from_camera,
                        expected_from=frame.camera_frame,
                        expected_to=self.config["frames"]["arm_base"],
                        require_stationary_alignment=True,
                    )
                )
                live_vio_from_camera = await self.fabric.transform(
                    from_frame=frame.camera_frame,
                    to_frame=vio_world_frame,
                    **query,
                )
                camera_vio_parent_chain = (
                    self._validate_transform_parent_chain(
                        live_vio_from_camera,
                        expected_from=frame.camera_frame,
                        expected_to=vio_world_frame,
                        require_stationary_alignment=False,
                    )
                )
                arm_from_camera = (
                    self._freeze_stationary_camera_transform(
                        live_arm_from_camera=live_arm_from_camera,
                        live_vio_from_camera=live_vio_from_camera,
                        alignment=alignment,
                        arm_base_frame=str(
                            self.config["frames"]["arm_base"]
                        ),
                    )
                )
            except httpx.HTTPError as error:
                raise RuntimeError(
                    "camera-to-arm transform was unavailable at RGB-D "
                    f"capture time: {error}"
                ) from error

        arm_from_tool: dict[str, Any] | None = None
        arm_from_tool_error: str | None = None
        try:
            arm_from_tool = await self.fabric.transform(
                from_frame=self.config["frames"]["arm_tool"],
                to_frame=self.config["frames"]["arm_base"],
                at_us=frame.timestamp_us,
                max_extrapolation_us=int(
                    self.config["alignment"]["transform_max_extrapolation_us"]
                ),
                session_epoch=None,
            )
            tool_arm_parent_chain = self._validate_transform_parent_chain(
                arm_from_tool,
                expected_from=self.config["frames"]["arm_tool"],
                expected_to=self.config["frames"]["arm_base"],
                require_stationary_alignment=False,
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            arm_from_tool_error = str(error)
            tool_arm_parent_chain = None
        if not lock_was_reused:
            arm_from_camera = await self._lock_stationary_camera_transform(
                arm_from_camera=arm_from_camera,
                alignment=alignment,
                frame=frame,
                camera_arm_parent_chain=camera_arm_parent_chain,
                camera_vio_parent_chain=camera_vio_parent_chain,
            )
        return {
            "captured_before_vlm": True,
            "timestamp_us": frame.timestamp_us,
            "arm_from_camera": arm_from_camera,
            "live_arm_from_camera": live_arm_from_camera,
            "live_vio_from_camera": live_vio_from_camera,
            "camera_arm_parent_chain": camera_arm_parent_chain,
            "camera_vio_parent_chain": camera_vio_parent_chain,
            "fixed_camera_transform_lock_reused": lock_was_reused,
            "local_vio_stop_result": getattr(
                self,
                "local_vio_stop_result",
                None,
            ),
            "arm_from_tool": arm_from_tool,
            "tool_arm_parent_chain": tool_arm_parent_chain,
            "arm_from_tool_error": arm_from_tool_error,
        }

    async def _lock_stationary_camera_transform(
        self,
        *,
        arm_from_camera: dict[str, Any],
        alignment: dict[str, Any],
        frame: RgbdFrame,
        camera_arm_parent_chain: dict[str, Any],
        camera_vio_parent_chain: dict[str, Any],
    ) -> dict[str, Any]:
        provider_id = str(self.config["providers"]["local_vio"])
        if not bool(
            self.config["alignment"][
                "stop_local_vio_after_transform_lock"
            ]
        ):
            raise RuntimeError(
                "fixed-camera cutting requires local VIO shutdown after "
                "the transform lock"
            )
        try:
            stop_result = await self.manager.stop_provider(provider_id)
        except Exception as error:
            raise RuntimeError(
                "the fixed camera transform was resolved, but local VIO "
                f"could not be stopped: {error}"
            ) from error
        stop_status = str(stop_result.get("status") or "").lower()
        if stop_status not in {"stopped", "killed"}:
            raise RuntimeError(
                "local VIO stop returned an unconfirmed state: "
                f"{stop_result}"
            )
        locked_at_us = time.time_ns() // 1000
        locked_transform = dict(arm_from_camera)
        locked_transform["stationary_camera_pose_lock"] = {
            "applied": True,
            "reused": False,
            "fixed_camera_required": True,
            "source_alignment_id": alignment.get("alignment_id"),
            "source_vio_session_epoch": alignment.get(
                "vio_session_epoch"
            ),
            "source_capture_timestamp_us": frame.timestamp_us,
            "locked_at_us": locked_at_us,
            "local_vio_provider_id": provider_id,
            "local_vio_stop": stop_result,
            "live_vio_queried": True,
            "future_vio_queries_allowed": False,
        }
        self.local_vio_stop_result = dict(stop_result)
        self.stationary_camera_transform_lock = {
            "alignment_id": alignment.get("alignment_id"),
            "vio_session_epoch": alignment.get("vio_session_epoch"),
            "locked_at_us": locked_at_us,
            "arm_from_camera": locked_transform,
            "camera_arm_parent_chain": camera_arm_parent_chain,
            "camera_vio_parent_chain": camera_vio_parent_chain,
        }
        return dict(locked_transform)

    @staticmethod
    def _freeze_stationary_camera_transform(
        *,
        live_arm_from_camera: dict[str, Any],
        live_vio_from_camera: dict[str, Any],
        alignment: dict[str, Any],
        arm_base_frame: str,
    ) -> dict[str, Any]:
        world_from_vio_payload = alignment.get("world_from_vio")
        world_from_base_payload = alignment.get("world_from_base")
        if not isinstance(world_from_vio_payload, dict) or not isinstance(
            world_from_base_payload, dict
        ):
            raise RuntimeError(
                "stationary alignment lacks world-from-VIO or world-from-base"
            )
        world_from_vio = transform_matrix(
            world_from_vio_payload["translation_m"],
            world_from_vio_payload["rotation_xyzw"],
        )
        world_from_base = transform_matrix(
            world_from_base_payload["translation_m"],
            world_from_base_payload["rotation_xyzw"],
        )
        live_vio_matrix = transform_matrix(
            live_vio_from_camera["translation_m"],
            live_vio_from_camera["rotation_xyzw"],
        )

        reference_pose_payload = alignment.get(
            "vio_from_camera_reference"
        )
        if not isinstance(reference_pose_payload, dict):
            raise RuntimeError(
                "stationary alignment lacks vio_from_camera_reference; "
                "vegetable cutting cannot safely substitute live VIO "
                "orientation. Run the updated alignment Skill once"
            )
        reference_vio_from_camera = transform_matrix(
            reference_pose_payload["translation_m"],
            reference_pose_payload["rotation_xyzw"],
        )
        stabilized_vio_from_camera = reference_vio_from_camera.copy()
        orientation_source = "ALIGNMENT_REFERENCE_FULL_POSE"
        frozen_arm_from_camera = (
            np.linalg.inv(world_from_base)
            @ world_from_vio
            @ stabilized_vio_from_camera
        )
        if not np.all(np.isfinite(frozen_arm_from_camera)):
            raise RuntimeError(
                "stationary camera transform stabilization produced non-finite values"
            )

        live_translation = np.asarray(
            live_vio_from_camera["translation_m"],
            dtype=np.float64,
        )
        reference_translation = reference_vio_from_camera[:3, 3]
        discarded_drift = live_translation - reference_translation
        output = dict(live_arm_from_camera)
        output.update(
            {
                "from_frame": str(alignment.get("camera_frame") or ""),
                "to_frame": arm_base_frame,
                "translation_m": frozen_arm_from_camera[:3, 3].tolist(),
                "rotation_xyzw": matrix_quaternion_xyzw(
                    frozen_arm_from_camera[:3, :3]
                ).tolist(),
                "stationary_camera_translation_stabilization": {
                    "applied": True,
                    "source_alignment_id": alignment.get("alignment_id"),
                    "reference_timestamp_us": alignment.get(
                        "camera_reference_timestamp_us"
                    ),
                    "reference_vio_from_camera_translation_m": (
                        reference_translation.tolist()
                    ),
                    "live_vio_from_camera_translation_m": (
                        live_translation.tolist()
                    ),
                    "discarded_vio_translation_drift_m": (
                        discarded_drift.tolist()
                    ),
                    "discarded_vio_translation_drift_norm_m": float(
                        np.linalg.norm(discarded_drift)
                    ),
                    "orientation_source": orientation_source,
                    "alignment_reference_full_pose_available": (
                        isinstance(reference_pose_payload, dict)
                    ),
                },
            }
        )
        return output

    @staticmethod
    def _validate_transform_parent_chain(
        payload: dict[str, Any],
        *,
        expected_from: str,
        expected_to: str,
        require_stationary_alignment: bool,
    ) -> dict[str, Any]:
        if str(payload.get("from_frame") or "") != expected_from:
            raise RuntimeError(
                "Fabric transform response has the wrong from_frame parent chain"
            )
        if str(payload.get("to_frame") or "") != expected_to:
            raise RuntimeError(
                "Fabric transform response has the wrong to_frame parent chain"
            )
        path = payload.get("path")
        if not isinstance(path, list) or not path:
            raise RuntimeError("Fabric transform response has no parent-chain provenance")
        cursor = expected_from
        visited = {cursor}
        for index, step in enumerate(path):
            if not isinstance(step, dict):
                raise RuntimeError(
                    f"Fabric transform path step {index} is not an object"
                )
            step_from = str(step.get("from_frame") or "")
            step_to = str(step.get("to_frame") or "")
            parent = str(step.get("parent_frame") or "")
            child = str(step.get("child_frame") or "")
            direction = str(step.get("direction") or "")
            if step_from != cursor:
                raise RuntimeError(
                    f"Fabric transform path disconnects at step {index}"
                )
            if direction == "child_to_parent":
                direction_valid = step_from == child and step_to == parent
            elif direction == "parent_to_child":
                direction_valid = step_from == parent and step_to == child
            else:
                direction_valid = False
            if not direction_valid:
                raise RuntimeError(
                    "Fabric transform path has reversed parent semantics "
                    f"at step {index}"
                )
            if step_to in visited:
                raise RuntimeError("Fabric transform path contains a frame cycle")
            visited.add(step_to)
            cursor = step_to
        if cursor != expected_to:
            raise RuntimeError(
                "Fabric transform path does not terminate at the requested frame"
            )

        alignment_steps = [
            step
            for step in path
            if str(step.get("authority") or "")
            == "skill.stationary_world_arm_alignment"
        ]
        if require_stationary_alignment:
            if len(alignment_steps) != 2:
                raise RuntimeError(
                    "camera-to-arm path must traverse exactly the stationary "
                    "world-from-VIO and world-from-arm-base calibration edges"
                )
            vio_to_world, world_to_arm = alignment_steps
            world_frame = str(vio_to_world.get("to_frame") or "")
            if not (
                vio_to_world.get("direction") == "child_to_parent"
                and str(vio_to_world.get("child_frame") or "").startswith(
                    "local_vio/"
                )
                and vio_to_world.get("parent_frame") == world_frame
                and world_to_arm.get("direction") == "parent_to_child"
                and world_to_arm.get("from_frame") == world_frame
                and world_to_arm.get("parent_frame") == world_frame
                and world_to_arm.get("child_frame") == expected_to
                and vio_to_world.get("provider_instance_id")
                == world_to_arm.get("provider_instance_id")
            ):
                raise RuntimeError(
                    "stationary alignment edges have inconsistent parent/child "
                    "directions or come from different alignment instances"
                )
        return {
            "valid": True,
            "from_frame": expected_from,
            "to_frame": expected_to,
            "step_count": len(path),
            "stationary_alignment_step_count": len(alignment_steps),
        }

    def _select_blade_axis_hypothesis(
        self,
        *,
        frame: RgbdFrame,
        blade_scene: dict[str, Any],
        published_arm_from_camera: np.ndarray,
        arm_from_tool: np.ndarray,
    ) -> tuple[np.ndarray, dict[str, Any], dict[str, Any], dict[str, Any]]:
        yaw_flip = np.eye(4, dtype=np.float64)
        yaw_flip[:3, :3] = np.diag([-1.0, -1.0, 1.0])
        image_points = {
            "tip": blade_scene["tip_yx_1000"],
            "heel": blade_scene["heel_yx_1000"],
            "spine": blade_scene["spine_yx_1000"],
        }
        registration_config = self.config["tool"]["observation_registration"]
        evaluated: list[
            tuple[
                int,
                np.ndarray,
                dict[str, Any],
                dict[str, Any],
                float,
            ]
        ] = []
        diagnostics: list[dict[str, Any]] = []
        for flip_deg, arm_from_camera in (
            (0, published_arm_from_camera),
            (180, yaw_flip @ published_arm_from_camera),
        ):
            try:
                observation = self._blade_observation(
                    frame,
                    blade_scene,
                    arm_from_camera,
                )
                candidate = build_blade_registration_candidate(
                    observation,
                    arm_from_tool,
                    image_points,
                    registration_config,
                )
                raw_candidate = candidate
                reflective_geometry_reasons = (
                    "observed tip-to-heel length",
                    "spine-to-edge distance",
                    "local depth patch",
                    "tool-to-acting-point distance",
                )
                if (
                    candidate["status"] == "REJECTED_OBSERVATION"
                    and any(
                        token in reason
                        for reason in candidate["quality_reasons"]
                        for token in reflective_geometry_reasons
                    )
                    and bool(
                        registration_config[
                            "allow_reflective_blade_kinematic_fallback"
                        ]
                    )
                ):
                    try:
                        reflective_observation = (
                            self._reflective_blade_observation(
                                frame,
                                blade_scene,
                                arm_from_camera,
                                arm_from_tool,
                                observation,
                            )
                        )
                        reflective_candidate = (
                            build_blade_registration_candidate(
                                reflective_observation,
                                arm_from_tool,
                                image_points,
                                registration_config,
                            )
                        )
                        reflective_candidate[
                            "raw_depth_candidate"
                        ] = {
                            "status": raw_candidate["status"],
                            "quality_reasons": raw_candidate[
                                "quality_reasons"
                            ],
                            "quality_metrics": raw_candidate[
                                "quality_metrics"
                            ],
                        }
                        if (
                            reflective_candidate["status"]
                            == "CANDIDATE_REVIEW_REQUIRED"
                        ):
                            observation = reflective_observation
                            candidate = reflective_candidate
                    except (
                        RuntimeError,
                        ValueError,
                        np.linalg.LinAlgError,
                    ) as fallback_error:
                        candidate[
                            "reflective_fallback_error"
                        ] = str(fallback_error)
                cosine = float(
                    candidate["quality_metrics"]["tool_forward_axis_cosine"]
                )
                evaluated.append(
                    (
                        flip_deg,
                        arm_from_camera,
                        observation,
                        candidate,
                        cosine,
                    )
                )
                diagnostics.append(
                    {
                        "flip_deg": flip_deg,
                        "tool_forward_axis_cosine": cosine,
                        "forward_axis_usable": True,
                        "forward_axis_gate_enabled": False,
                        "registration_status": candidate["status"],
                        "quality_reasons": candidate["quality_reasons"],
                    }
                )
            except (RuntimeError, ValueError, np.linalg.LinAlgError) as error:
                diagnostics.append(
                    {
                        "flip_deg": flip_deg,
                        "forward_axis_usable": False,
                        "error": str(error),
                    }
                )
        if not evaluated:
            raise RuntimeError(
                "neither 0/180-degree blade-axis hypothesis could be evaluated"
            )

        published = next(
            (value for value in evaluated if value[0] == 0),
            None,
        )
        alternate = next(
            (value for value in evaluated if value[0] == 180),
            None,
        )
        if published is not None:
            selected = published
            status = "PUBLISHED_AXIS_ACCEPTED"
        elif alternate is not None:
            selected = alternate
            status = "ALTERNATE_180_AXIS_EVALUATION_FALLBACK"
        selection = {
            "status": status,
            "selected_flip_deg": selected[0],
            "forward_axis_gate_enabled": False,
            "hypotheses": diagnostics,
            "semantics": (
                "PUBLISHED_TRANSFORM_PARENTING_PREFERRED;_180_DEGREE_"
                "HYPOTHESIS_ONLY_IF_PUBLISHED_CANNOT_BE_EVALUATED"
            ),
        }
        selected[3]["axis_hypothesis_selection"] = selection
        return selected[1], selected[2], selected[3], selection

    def _resolve_camera_epoch_binding(
        self,
        *,
        alignment: dict[str, Any],
        frame: RgbdFrame,
    ) -> dict[str, Any]:
        alignment_epoch = str(
            alignment.get("vio_session_epoch") or ""
        )
        capture_epoch = str(frame.session_epoch or "")
        camera_lock = getattr(
            self,
            "stationary_camera_transform_lock",
            None,
        )
        if camera_lock is not None:
            alignment_id = str(alignment.get("alignment_id") or "")
            lock_alignment_id = str(
                camera_lock.get("alignment_id") or ""
            )
            if not alignment_id or lock_alignment_id != alignment_id:
                raise RuntimeError(
                    "fixed camera transform lock belongs to a different "
                    "stationary alignment"
                )
            lock_epoch = str(
                camera_lock.get("vio_session_epoch") or ""
            )
            if lock_epoch and lock_epoch != alignment_epoch:
                raise RuntimeError(
                    "fixed camera transform lock belongs to a different "
                    "VIO epoch than the stationary alignment"
                )
            return {
                "policy": "FIXED_CAMERA_TRANSFORM_LOCK",
                "compatible": True,
                "alignment_id": alignment_id,
                "effective_vio_session_epoch": alignment_epoch,
                "lock_vio_session_epoch": lock_epoch or alignment_epoch,
                "raw_capture_vio_session_epoch": capture_epoch or None,
                "post_lock_capture_requires_vio_epoch": False,
            }
        if (
            bool(self.config["alignment"]["require_same_vio_epoch"])
            and alignment_epoch != capture_epoch
        ):
            raise RuntimeError(
                "alignment and current RGB-D frame use different VIO epochs"
            )
        return {
            "policy": "LIVE_VIO_EPOCH_MATCH",
            "compatible": True,
            "alignment_id": alignment.get("alignment_id"),
            "effective_vio_session_epoch": capture_epoch,
            "lock_vio_session_epoch": None,
            "raw_capture_vio_session_epoch": capture_epoch or None,
            "post_lock_capture_requires_vio_epoch": True,
        }

    async def _build_plan(
        self,
        frame: RgbdFrame,
        scene: dict[str, Any],
        frame_transforms: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        self._validate_scene(scene)
        current = await self.progress.snapshot()
        alignment = current.get("alignment") or await self._alignment_snapshot()
        camera_epoch_binding = self._resolve_camera_epoch_binding(
            alignment=alignment,
            frame=frame,
        )
        if (
            bool(self.config["alignment"]["require_same_camera_calibration_revision"])
            and str(alignment.get("camera_calibration_revision") or "")
            != str(frame.calibration_revision or "")
        ):
            raise RuntimeError(
                "alignment and current RGB-D frame use different camera calibration revisions"
            )

        await self.progress.update(
            phase=Phase.PLANNING,
            message=(
                "Deprojecting the two board-depth endpoints and interpolating "
                "cut centers on their 3D straight line."
            ),
        )
        planning = self.config["planning"]
        endpoint_pixels_normalized = scene["vegetable"][
            "cutting_line_board_endpoints_yx_1000"
        ]
        endpoint_camera: list[np.ndarray] = []
        endpoint_depth_diagnostics: list[dict[str, Any]] = []
        for point_yx_1000 in endpoint_pixels_normalized:
            pixel_yx, depth_m, diagnostics = self._depth_at(
                frame,
                point_yx_1000,
            )
            endpoint_camera.append(
                deproject_pixel(pixel_yx, depth_m, frame.intrinsics)
            )
            endpoint_depth_diagnostics.append(diagnostics)

        arm_from_camera_query = frame_transforms["arm_from_camera"]
        arm_from_camera = transform_matrix(
            arm_from_camera_query["translation_m"],
            arm_from_camera_query["rotation_xyzw"],
        )
        arm_from_tool_query = frame_transforms.get("arm_from_tool")
        blade_axis_selection: dict[str, Any]
        fixed_tool_mode = (
            str(self.config["tool"]["registration_mode"])
            == "FIXED_HARD_MOUNT"
        )
        if fixed_tool_mode:
            blade_observation = {
                "status": "NOT_REQUESTED_FIXED_HARD_MOUNT",
                "source": "CONFIGURED_TOOL_FRAME_OFFSET",
                "visible": None,
                "confidence": None,
            }
            blade_registration_candidate = {
                "status": "FIXED_HARD_MOUNT",
                "source": "CONFIGURED_TOOL_FRAME_OFFSET",
                "eligible_for_operator_review": True,
                "motion_usable": True,
                "acting_point_from_tool_m": [
                    float(value)
                    for value in self.config["tool"][
                        "fixed_controlled_frame_offset_xyz_m"
                    ]
                ],
                "controlled_frame_rpy_from_tool": [
                    float(value)
                    for value in self.config["tool"][
                        "fixed_controlled_frame_offset_rpy_rad"
                    ]
                ],
                "quality_reasons": [],
                "quality_metrics": {
                    "vlm_blade_registration_used": False,
                    "hard_fixed_mount": True,
                },
            }
            blade_axis_selection = {
                "status": "NOT_REQUIRED_FIXED_HARD_MOUNT",
                "selected_flip_deg": 0,
                "semantics": (
                    "PUBLISHED_CAMERA_TO_ARM_TRANSFORM_USED_WITHOUT_"
                    "BLADE_AXIS_HYPOTHESIS"
                ),
            }
        elif isinstance(arm_from_tool_query, dict):
            arm_from_tool = transform_matrix(
                arm_from_tool_query["translation_m"],
                arm_from_tool_query["rotation_xyzw"],
            )
            (
                arm_from_camera,
                blade_observation,
                blade_registration_candidate,
                blade_axis_selection,
            ) = self._select_blade_axis_hypothesis(
                frame=frame,
                blade_scene=scene["blade"],
                published_arm_from_camera=arm_from_camera,
                arm_from_tool=arm_from_tool,
            )
            blade_registration_candidate["arm_from_tool"] = arm_from_tool_query
        else:
            blade_observation = self._blade_observation(
                frame,
                scene["blade"],
                arm_from_camera,
            )
            blade_registration_candidate = {
                "status": "UNAVAILABLE",
                "source": "VLM_RGBD_BLADE_POINTS_PLUS_TOOL_TRANSFORM",
                "eligible_for_operator_review": False,
                "motion_usable": False,
                "quality_reasons": [
                    frame_transforms.get("arm_from_tool_error")
                    or "tool transform was unavailable at RGB-D capture time"
                ],
            }
            blade_axis_selection = {
                "status": "UNAVAILABLE",
                "selected_flip_deg": 0,
                "reason": "arm-from-tool transform unavailable",
            }
        effective_arm_from_camera_query = {
            **arm_from_camera_query,
            "translation_m": arm_from_camera[:3, 3].tolist(),
            "rotation_xyzw": matrix_quaternion_xyzw(
                arm_from_camera[:3, :3]
            ).tolist(),
            "axis_hypothesis_selection": blade_axis_selection,
        }
        endpoint_arm = transform_points(
            arm_from_camera,
            np.asarray(endpoint_camera),
        )
        geometry = plan_cut_points_on_line_3d(
            endpoint_arm[0],
            endpoint_arm[1],
            spacing_m=float(self.parameters.slice_spacing_mm) / 1000.0,
            maximum_cut_count=min(
                int(planning["maximum_cut_count"]),
                int(self.parameters.maximum_cut_count),
            ),
        )
        if len(geometry["cuts"]) < int(planning["minimum_cut_count"]):
            raise RuntimeError("the two-point 3D line produced no cut centers")

        up_arm = np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
        line_axis_arm = np.asarray(geometry["axis"], dtype=np.float64)
        cross_axis_arm = np.cross(up_arm, line_axis_arm)
        cross_norm = float(np.linalg.norm(cross_axis_arm))
        if cross_norm <= 1e-6:
            raise RuntimeError(
                "the two cutting-line endpoints do not define a horizontal cut axis"
            )
        cross_axis_arm /= cross_norm
        half_span_m = float(planning["cut_visual_half_span_mm"]) / 1000.0
        contact_offset_m = (
            float(self.config["handoff"]["cut_target_board_offset_mm"])
            / 1000.0
        )
        camera_from_arm = np.linalg.inv(arm_from_camera)

        def project_camera_point(point_camera_m: np.ndarray) -> list[int]:
            if float(point_camera_m[2]) <= 1e-6:
                raise RuntimeError("planned cut point projects behind the camera")
            x = (
                float(frame.intrinsics["fx"])
                * float(point_camera_m[0])
                / float(point_camera_m[2])
                + float(frame.intrinsics["cx"])
            )
            y = (
                float(frame.intrinsics["fy"])
                * float(point_camera_m[1])
                / float(point_camera_m[2])
                + float(frame.intrinsics["cy"])
            )
            height, width = frame.rgb.shape[:2]
            return [
                int(np.clip(round(y), 0, height - 1)),
                int(np.clip(round(x), 0, width - 1)),
            ]

        cut_pixels: list[dict[str, Any]] = []
        cuts: list[dict[str, Any]] = []
        for cut in geometry["cuts"]:
            center_arm = (
                np.asarray(cut["center_m"], dtype=np.float64)
                + up_arm * contact_offset_m
            )
            arm_points = np.asarray(
                [
                    center_arm - cross_axis_arm * half_span_m,
                    center_arm + cross_axis_arm * half_span_m,
                    center_arm,
                ]
            )
            camera_points = transform_points(camera_from_arm, arm_points)
            entry_pixel = project_camera_point(camera_points[0])
            exit_pixel = project_camera_point(camera_points[1])
            cuts.append(
                {
                    **cut,
                    "entry_camera_m": camera_points[0].tolist(),
                    "exit_camera_m": camera_points[1].tolist(),
                    "center_camera_m": camera_points[2].tolist(),
                    "entry_arm_base_m": arm_points[0].tolist(),
                    "exit_arm_base_m": arm_points[1].tolist(),
                    "center_arm_base_m": arm_points[2].tolist(),
                    "blade_yaw_deg": float(self.parameters.blade_yaw_deg),
                    "execution_status": "PLANNED_NOT_SUBMITTED",
                }
            )
            cut_pixels.append(
                {
                    "index": cut["index"],
                    "entry_yx": entry_pixel,
                    "exit_yx": exit_pixel,
                }
            )

        board_normal_arm_base = up_arm
        execution_preview = build_execution_preview(
            cuts,
            board_normal_arm_base,
            vegetable_maximum_height_mm=0.0,
            blade_yaw_deg=float(self.parameters.blade_yaw_deg),
            handoff=self.config["handoff"],
            execution=self.config["execution"],
        )
        blade_registration_candidate["observation_index"] = (
            len(self.blade_registration_candidates) + 1
        )
        if fixed_tool_mode:
            blade_registration_consistency = {
                "status": "NOT_REQUIRED_FIXED_HARD_MOUNT",
                "eligible_for_operator_review": True,
                "motion_usable": True,
                "required_observations": 0,
                "total_observations": 0,
                "window_observations": 0,
                "valid_window_observations": 0,
                "quality_metrics": {},
                "quality_reasons": [],
            }
            self.blade_registration_candidates = []
        else:
            registration_candidates = [
                *self.blade_registration_candidates,
                blade_registration_candidate,
            ]
            blade_registration_consistency = (
                evaluate_blade_registration_consistency(
                    registration_candidates,
                    self.config["tool"]["observation_registration"][
                        "consistency"
                    ],
                )
            )
            self.blade_registration_candidates = registration_candidates
        self.plan_revision += 1
        plan_id = f"cut-plan-{uuid.uuid4()}"
        tool = self.config["tool"]
        tool_calibrated = fixed_tool_mode or all(
            tool.get(key) is not None
            for key in (
                "calibration_id",
                "tool_to_blade_translation_m",
                "tool_to_blade_rotation_xyzw",
                "payload_mass_kg",
                "payload_com_tool_m",
            )
        )
        session_candidate_ready = bool(
            blade_registration_consistency.get(
                "eligible_for_operator_review"
            )
        )
        session_calibration = (
            self._build_fixed_tool_calibration(
                plan_id=plan_id,
                plan_revision=self.plan_revision,
            )
            if fixed_tool_mode
            else None
        )
        if session_candidate_ready and session_calibration is None:
            session_calibration = self._build_session_tool_calibration(
                plan_id=plan_id,
                plan_revision=self.plan_revision,
                consistency=blade_registration_consistency,
            )
        self.accepted_tool_calibration = session_calibration
        execution_blockers = []
        if session_calibration is None and not tool_calibrated:
            execution_blockers.append(
                "one geometrically reviewable blade-frame observation is required"
            )
        execution_blockers.append(
            "explicit operator takeover is required before physical submission"
        )
        now_us = time.time_ns() // 1000
        plan = {
            "schema": "midbrain.skill.vegetable_cutting.plan",
            "schema_version": 1,
            "skill_id": current["skill_id"],
            "plan_id": plan_id,
            "plan_revision": self.plan_revision,
            "created_at_us": now_us,
            "reason": reason,
            "dry_run": True,
            "operator_takeover_required": True,
            "motion_submission_enabled": False,
            "motion_submitted": False,
            "execution_ready": False,
            "execution_blockers": execution_blockers,
            "alignment": alignment,
            "camera": {
                "frame": frame.camera_frame,
                "timestamp_us": frame.timestamp_us,
                "frame_number": frame.frame_number,
                "calibration_revision": frame.calibration_revision,
                "vio_session_epoch": camera_epoch_binding[
                    "effective_vio_session_epoch"
                ],
                "raw_capture_vio_session_epoch": (
                    camera_epoch_binding[
                        "raw_capture_vio_session_epoch"
                    ]
                ),
                "epoch_binding": camera_epoch_binding,
            },
            "capture_transforms": {
                **frame_transforms,
                "published_arm_from_camera": arm_from_camera_query,
                "arm_from_camera": effective_arm_from_camera_query,
            },
            "arm_from_camera": effective_arm_from_camera_query,
            "blade_axis_selection": blade_axis_selection,
            "board": {
                "visible": bool(scene["board"]["visible"]),
                "confidence": float(scene["board"]["confidence"]),
                "shape_used_for_planning": False,
                "planning_role": "VISIBILITY_ONLY",
                "normal_arm_base": board_normal_arm_base.tolist(),
            },
            "vegetable": {
                "polygon_yx_1000": scene["vegetable"]["polygon_yx_1000"],
                "major_axis_endpoints_yx_1000": scene["vegetable"][
                    "major_axis_endpoints_yx_1000"
                ],
                "cutting_line_board_endpoints_yx_1000": (
                    endpoint_pixels_normalized
                ),
                "cutting_line_endpoint_camera_m": [
                    point.tolist() for point in endpoint_camera
                ],
                "cutting_line_endpoint_arm_base_m": endpoint_arm.tolist(),
                "cutting_line_endpoint_depth_diagnostics": (
                    endpoint_depth_diagnostics
                ),
                "cutting_line": geometry,
            },
            "blade_observation": blade_observation,
            "blade_registration_candidate": blade_registration_candidate,
            "blade_registration_consistency": blade_registration_consistency,
            "first_cut_alignment_contract": build_first_cut_alignment_contract(
                self.config["handoff"]["first_cut_alignment"]
            ),
            "execution_preview": execution_preview,
            "tool_calibration": {
                **tool,
                "complete": bool(tool_calibrated or session_calibration),
                "session_candidate_ready": session_candidate_ready,
                "session_calibration": session_calibration,
                "operator_review_deferred_to_first_cut_approach": True,
            },
            "planning_parameters": {
                "slice_spacing_mm": float(self.parameters.slice_spacing_mm),
                "blade_yaw_deg": float(self.parameters.blade_yaw_deg),
                "path_source": "TWO_RGBD_BOARD_POINTS_STRAIGHT_LINE",
                "board_shape_used": False,
            },
            "cuts": cuts,
            "rejected_cut_stations": geometry["rejected"],
            "handoff_profile": {
                **self.config["handoff"],
                "informational_only": False,
            },
            "perception": {
                "source": "OPENAI_VLM_INITIAL_OR_EXCEPTION",
                "model": self.settings.openai_vision_model,
                "scene": scene,
            },
        }
        overlay = render_plan_overlay(
            frame.rgb,
            scene,
            cut_pixels,
            blade_registration_candidate=blade_registration_candidate,
        )
        await self.artifacts.set_images(
            rgb_jpeg=encode_rgb_jpeg(frame.rgb),
            depth_png=encode_depth_png(frame.depth_m),
            overlay_jpeg=overlay,
        )
        await self.artifacts.set_plan(plan)
        self.store.save(plan)
        await self._publish_plan(plan)
        execution = {
            "state": "READY_FOR_OPERATOR_TAKEOVER",
            "motion_submission_enabled": bool(session_calibration),
            "operator_takeover_confirmed": False,
            "calibration": session_calibration,
            "operator_review_deferred_to_first_cut_approach": True,
            "events": list(self.execution_events),
        }
        await self.progress.update(
            plan_id=plan_id,
            state=SkillState.READY_FOR_OPERATOR_TAKEOVER,
            phase=Phase.READY_FOR_OPERATOR_TAKEOVER,
            message=(
                "Reviewed plan is ready. No motion has been submitted. "
                "Confirm operator takeover to move to the first-cut approach; "
                "the human location review occurs there before any cut."
            ),
            result=plan,
            motion_submission_enabled=bool(session_calibration),
            motion_submitted=False,
            execution=execution,
        )
        return plan

    async def _accept_reference(
        self,
        frame: RgbdFrame,
        scene: dict[str, Any],
        plan: dict[str, Any],
    ) -> None:
        mask = polygon_mask(
            frame.rgb.shape,
            scene["vegetable"]["polygon_yx_1000"],
        )
        self.reference_frame = frame
        self.reference_mask = mask
        self.tracker = AppearanceTracker(
            frame.rgb,
            mask,
            lab_distance_threshold=float(self.config["tracking"]["lab_distance_threshold"]),
        )
        await self.progress.update(
            tracking={
                "baseline": {
                    "source": "VLM_VEGETABLE_APPEARANCE_ONLY",
                    "pixels": int(np.count_nonzero(mask)),
                }
            }
        )

    def _validate_scene(self, scene: dict[str, Any]) -> None:
        if bool(scene.get("person_or_animal_visible_in_workspace")) or bool(
            scene.get("person_visible_in_workspace")
        ):
            raise RuntimeError("a person, hand, or animal is visible in the robot workspace")
        limits = self.config["vlm"]
        checks = (
            ("board", "minimum_board_confidence"),
            ("vegetable", "minimum_vegetable_confidence"),
        )
        for name, key in checks:
            value = scene.get(name) or {}
            if not bool(value.get("visible")):
                raise RuntimeError(f"VLM cannot see the {name}")
            if float(value.get("confidence") or 0.0) < float(limits[key]):
                raise RuntimeError(f"VLM {name} confidence is below the configured limit")
        if not bool(
            self.config["tool"]["vlm_blade_registration_optional"]
        ):
            blade = scene.get("blade") or {}
            if not bool(blade.get("visible")):
                raise RuntimeError("VLM cannot see the blade")
            if float(blade.get("confidence") or 0.0) < float(
                limits["minimum_blade_confidence"]
            ):
                raise RuntimeError(
                    "VLM blade confidence is below the configured limit"
                )

    @staticmethod
    def _depth_at(
        frame: RgbdFrame,
        point_yx_1000: list[int],
    ) -> tuple[tuple[int, int], float, dict[str, Any]]:
        y, x = normalized_yx_to_pixel(point_yx_1000, frame.rgb.shape)
        selection = select_depth_sample(
            frame.depth_m,
            (y, x),
            search_radius_px=3,
            policy="ROBUST_MEDIAN",
            minimum_depth_m=0.05,
        )
        return (
            selection.pixel_yx,
            selection.depth_m,
            selection.as_dict(),
        )

    def _blade_observation(
        self,
        frame: RgbdFrame,
        blade: dict[str, Any],
        arm_from_camera: np.ndarray,
    ) -> dict[str, Any]:
        camera_points: dict[str, list[float]] = {}
        arm_points: dict[str, list[float]] = {}
        depth_diagnostics: dict[str, dict[str, Any]] = {}
        pixels: dict[str, tuple[int, int]] = {}
        raw_depths: dict[str, float] = {}
        for name, key in (
            ("tip", "tip_yx_1000"),
            ("heel", "heel_yx_1000"),
            ("spine", "spine_yx_1000"),
        ):
            (y, x), depth, diagnostics = self._depth_at(frame, blade[key])
            pixels[name] = (y, x)
            raw_depths[name] = depth
            depth_diagnostics[name] = diagnostics

        tip_pixel = np.asarray(
            [pixels["tip"][1], pixels["tip"][0]], dtype=np.float64
        )
        heel_pixel = np.asarray(
            [pixels["heel"][1], pixels["heel"][0]], dtype=np.float64
        )
        spine_pixel = np.asarray(
            [pixels["spine"][1], pixels["spine"][0]], dtype=np.float64
        )
        image_edge = heel_pixel - tip_pixel
        image_edge_norm_sq = float(image_edge @ image_edge)
        if image_edge_norm_sq <= 1e-9:
            raise RuntimeError("blade tip and heel image points are degenerate")
        spine_fraction = float(
            np.clip(
                ((spine_pixel - tip_pixel) @ image_edge)
                / image_edge_norm_sq,
                0.0,
                1.0,
            )
        )
        interpolated_spine_depth = float(
            raw_depths["tip"]
            + spine_fraction
            * (raw_depths["heel"] - raw_depths["tip"])
        )
        spine_residual_mm = abs(
            raw_depths["spine"] - interpolated_spine_depth
        ) * 1000.0
        registration = self.config["tool"]["observation_registration"]
        use_interpolated_spine = bool(
            registration["allow_edge_interpolated_spine_depth"]
        ) and spine_residual_mm > float(
            registration["spine_depth_interpolation_trigger_mm"]
        )
        spine_correction_exceeds_review_limit = spine_residual_mm > float(
            registration["maximum_spine_depth_correction_mm"]
        )
        used_depths = dict(raw_depths)
        if use_interpolated_spine:
            used_depths["spine"] = interpolated_spine_depth
        depth_diagnostics["spine"].update(
            {
                "raw_median_m": raw_depths["spine"],
                "edge_interpolated_m": interpolated_spine_depth,
                "edge_longitudinal_fraction": spine_fraction,
                "raw_interpolation_residual_mm": spine_residual_mm,
                "correction_exceeds_review_limit": (
                    spine_correction_exceeds_review_limit
                ),
                "used_source": (
                    "TIP_HEEL_EDGE_INTERPOLATION"
                    if use_interpolated_spine
                    else "LOCAL_PATCH_MEDIAN"
                ),
                "used_depth_m": used_depths["spine"],
            }
        )

        for name in ("tip", "heel", "spine"):
            y, x = pixels[name]
            depth = used_depths[name]
            point = np.asarray(
                [
                    (x - float(frame.intrinsics["cx"])) * depth
                    / float(frame.intrinsics["fx"]),
                    (y - float(frame.intrinsics["cy"])) * depth
                    / float(frame.intrinsics["fy"]),
                    depth,
                ],
                dtype=np.float64,
            )
            camera_points[name] = point.tolist()
            arm_points[name] = transform_points(arm_from_camera, point).tolist()
        edge = np.asarray(arm_points["tip"]) - np.asarray(arm_points["heel"])
        edge_length = float(np.linalg.norm(edge))
        if edge_length <= 1e-6:
            raise RuntimeError("blade tip and heel observations are degenerate")
        return {
            "source": "VLM_RGBD_THREE_POINT_OBSERVATION",
            "camera_points_m": camera_points,
            "arm_base_points_m": arm_points,
            "depth_diagnostics": depth_diagnostics,
            "depth_geometry": {
                "tip_to_heel_depth_difference_mm": abs(
                    used_depths["tip"] - used_depths["heel"]
                )
                * 1000.0,
                "spine_depth_interpolated": use_interpolated_spine,
                "spine_raw_interpolation_residual_mm": spine_residual_mm,
                "spine_correction_exceeds_review_limit": (
                    spine_correction_exceeds_review_limit
                ),
            },
            "edge_direction_arm_base": (edge / edge_length).tolist(),
            "observed_edge_length_m": edge_length,
            "registration_status": "OBSERVATION_ONLY_REQUIRES_TOOL_CALIBRATION",
            "confidence": float(blade["confidence"]),
        }

    def _reflective_blade_observation(
        self,
        frame: RgbdFrame,
        blade: dict[str, Any],
        arm_from_camera: np.ndarray,
        arm_from_tool: np.ndarray,
        raw_observation: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover blade geometry from image rays and the held-tool kinematic axis."""
        registration = self.config["tool"]["observation_registration"]
        tool_forward = np.asarray(
            registration["tool_forward_axis_xyz"],
            dtype=np.float64,
        )
        tool_forward /= np.linalg.norm(tool_forward)
        camera_origin_arm = np.asarray(
            arm_from_camera[:3, 3],
            dtype=np.float64,
        )
        tool_origin_arm = np.asarray(
            arm_from_tool[:3, 3],
            dtype=np.float64,
        )
        tool_rotation_arm = np.asarray(
            arm_from_tool[:3, :3],
            dtype=np.float64,
        )
        forward_arm = tool_rotation_arm @ tool_forward
        forward_arm /= np.linalg.norm(forward_arm)
        arm_from_camera_rotation = np.asarray(
            arm_from_camera[:3, :3],
            dtype=np.float64,
        )
        tool_from_arm = np.linalg.inv(arm_from_tool)

        pixels: dict[str, tuple[int, int]] = {
            name: normalized_yx_to_pixel(
                blade[f"{name}_yx_1000"],
                frame.rgb.shape,
            )
            for name in ("tip", "heel", "spine")
        }
        handle_point_yx = blade.get("handle_depth_anchor_yx_1000")
        junction_point_yx = blade.get("blade_handle_junction_yx_1000")
        if not isinstance(handle_point_yx, list) or len(handle_point_yx) != 2:
            raise RuntimeError(
                "reflective-blade reconstruction requires a VLM handle "
                "depth anchor"
            )
        if not isinstance(junction_point_yx, list) or len(
            junction_point_yx
        ) != 2:
            raise RuntimeError(
                "reflective-blade reconstruction requires the visible "
                "blade-handle junction"
            )
        junction_pixel = normalized_yx_to_pixel(
            junction_point_yx,
            frame.rgb.shape,
        )
        handle_pixel_for_step = normalized_yx_to_pixel(
            handle_point_yx,
            frame.rgb.shape,
        )
        junction_xy = np.asarray(
            [junction_pixel[1], junction_pixel[0]],
            dtype=np.float64,
        )
        handle_xy = np.asarray(
            [handle_pixel_for_step[1], handle_pixel_for_step[0]],
            dtype=np.float64,
        )
        handle_step_px = float(np.linalg.norm(handle_xy - junction_xy))
        if not float(
            registration["minimum_handle_anchor_from_junction_px"]
        ) <= handle_step_px <= float(
            registration["maximum_handle_anchor_from_junction_px"]
        ):
            raise RuntimeError(
                "the VLM handle depth anchor is not a small bounded step "
                "from the blade-handle junction"
            )
        (
            handle_pixel,
            handle_depth_m,
            handle_depth_diagnostics,
        ) = self._depth_at(frame, handle_point_yx)
        handle_depth_range_mm = (
            float(handle_depth_diagnostics["p90_m"])
            - float(handle_depth_diagnostics["p10_m"])
        ) * 1000.0
        if handle_depth_range_mm > float(
            registration["maximum_local_depth_range_mm"]
        ):
            raise RuntimeError(
                "the non-reflective handle depth patch exceeds the "
                "configured local range"
            )
        handle_camera = deproject_pixel(
            handle_pixel,
            handle_depth_m,
            frame.intrinsics,
        )
        handle_arm = transform_points(
            arm_from_camera,
            handle_camera,
        )
        handle_tool = transform_points(
            tool_from_arm,
            handle_arm,
        )
        handle_longitudinal_m = float(handle_tool @ tool_forward)
        handle_lateral_tool = (
            handle_tool - handle_longitudinal_m * tool_forward
        )
        if float(np.linalg.norm(handle_lateral_tool)) * 1000.0 > float(
            registration["maximum_lateral_offset_magnitude_mm"]
        ):
            raise RuntimeError(
                "the non-reflective handle depth anchor is too far from "
                "the held-tool axis"
            )

        axis_parameters_m: dict[str, float] = {}
        ray_parameters_m: dict[str, float] = {}
        ray_axis_miss_mm: dict[str, float] = {}
        lateral_offsets_tool_m: dict[str, np.ndarray] = {}
        for name in ("tip", "heel"):
            y, x = pixels[name]
            ray_camera = deproject_pixel(
                (y, x),
                1.0,
                frame.intrinsics,
            )
            ray_camera /= np.linalg.norm(ray_camera)
            ray_arm = arm_from_camera_rotation @ ray_camera
            ray_arm /= np.linalg.norm(ray_arm)
            coefficients = np.column_stack([ray_arm, -forward_arm])
            ray_parameter_m, relative_axis_parameter_m = np.linalg.lstsq(
                coefficients,
                handle_arm - camera_origin_arm,
                rcond=None,
            )[0]
            ray_point_arm = (
                camera_origin_arm + float(ray_parameter_m) * ray_arm
            )
            axis_point_arm = (
                handle_arm
                + float(relative_axis_parameter_m) * forward_arm
            )
            ray_parameters_m[name] = float(ray_parameter_m)
            axis_parameters_m[name] = (
                handle_longitudinal_m
                + float(relative_axis_parameter_m)
            )
            ray_axis_miss_mm[name] = (
                float(np.linalg.norm(ray_point_arm - axis_point_arm))
                * 1000.0
            )
            ray_point_tool = transform_points(
                tool_from_arm,
                ray_point_arm,
            )
            lateral_offsets_tool_m[name] = (
                ray_point_tool
                - float(ray_point_tool @ tool_forward) * tool_forward
            )

        if any(value <= 0.0 for value in ray_parameters_m.values()):
            raise RuntimeError(
                "a reflective-blade image ray points behind the camera"
            )
        maximum_miss_mm = float(
            registration["maximum_ray_to_tool_axis_miss_mm"]
        )
        if max(ray_axis_miss_mm.values()) > maximum_miss_mm:
            raise RuntimeError(
                "reflective-blade image rays are too far from the held-tool "
                "forward axis"
            )

        tip_axis_m = axis_parameters_m["tip"]
        heel_axis_m = axis_parameters_m["heel"]
        edge_length_m = tip_axis_m - heel_axis_m
        acting_distance_m = (
            float(registration["acting_point_from_tip_mm"]) / 1000.0
        )
        remaining_edge_m = float(
            registration["minimum_remaining_edge_after_acting_point_mm"]
        ) / 1000.0
        if acting_distance_m + remaining_edge_m > edge_length_m:
            raise RuntimeError(
                "reflective-blade ray reconstruction cannot place the "
                "requested acting point on the visible edge"
            )

        lateral_stack = np.asarray(
            [
                lateral_offsets_tool_m["tip"],
                lateral_offsets_tool_m["heel"],
            ],
            dtype=np.float64,
        )
        lateral_offset_tool = np.median(lateral_stack, axis=0)
        lateral_magnitude_mm = (
            float(np.linalg.norm(lateral_offset_tool)) * 1000.0
        )
        lateral_disagreement_mm = (
            float(
                np.linalg.norm(
                    lateral_offsets_tool_m["tip"]
                    - lateral_offsets_tool_m["heel"]
                )
            )
            * 1000.0
        )
        if lateral_magnitude_mm > float(
            registration["maximum_lateral_offset_magnitude_mm"]
        ):
            raise RuntimeError(
                "reflective-blade reconstruction places the edge too far "
                "from the held-tool axis"
            )
        if lateral_disagreement_mm > float(
            registration["maximum_lateral_offset_disagreement_mm"]
        ):
            raise RuntimeError(
                "reflective-blade tip and heel rays disagree on the blade "
                "lateral offset"
            )

        tip_pixel_xy = np.asarray(
            [pixels["tip"][1], pixels["tip"][0]],
            dtype=np.float64,
        )
        heel_pixel_xy = np.asarray(
            [pixels["heel"][1], pixels["heel"][0]],
            dtype=np.float64,
        )
        spine_pixel_xy = np.asarray(
            [pixels["spine"][1], pixels["spine"][0]],
            dtype=np.float64,
        )
        image_edge = heel_pixel_xy - tip_pixel_xy
        image_edge_norm_sq = float(image_edge @ image_edge)
        if image_edge_norm_sq <= 1e-9:
            raise RuntimeError(
                "reflective-blade tip and heel image points are degenerate"
            )
        spine_fraction = float(
            np.clip(
                (
                    (spine_pixel_xy - tip_pixel_xy)
                    @ image_edge
                )
                / image_edge_norm_sq,
                0.0,
                1.0,
            )
        )
        spine_projection = tip_pixel_xy + spine_fraction * image_edge
        image_width_ratio = (
            float(np.linalg.norm(spine_pixel_xy - spine_projection))
            / float(np.sqrt(image_edge_norm_sq))
        )
        blade_width_m = image_width_ratio * edge_length_m
        down_tool = tool_rotation_arm.T @ np.asarray(
            [0.0, 0.0, -1.0],
            dtype=np.float64,
        )
        down_tool -= float(down_tool @ tool_forward) * tool_forward
        down_norm = float(np.linalg.norm(down_tool))
        if down_norm <= 1e-6:
            raise RuntimeError(
                "tool forward axis is parallel to arm-base gravity"
            )
        down_tool /= down_norm
        spine_axis_m = (
            tip_axis_m + spine_fraction * (heel_axis_m - tip_axis_m)
        )
        tool_points = {
            "tip": (
                tip_axis_m * tool_forward + lateral_offset_tool
            ),
            "heel": (
                heel_axis_m * tool_forward + lateral_offset_tool
            ),
            "spine": (
                spine_axis_m * tool_forward
                + lateral_offset_tool
                - blade_width_m * down_tool
            ),
        }
        arm_points = {
            name: transform_points(arm_from_tool, point)
            for name, point in tool_points.items()
        }
        camera_from_arm = np.linalg.inv(arm_from_camera)
        camera_points = {
            name: transform_points(camera_from_arm, point)
            for name, point in arm_points.items()
        }
        if any(float(point[2]) <= 0.0 for point in camera_points.values()):
            raise RuntimeError(
                "reflective-blade reconstruction projects behind the camera"
            )

        raw_depth_diagnostics = raw_observation.get(
            "depth_diagnostics"
        ) or {}
        depth_diagnostics: dict[str, dict[str, Any]] = {}
        for name in ("tip", "heel", "spine"):
            raw = dict(raw_depth_diagnostics.get(name) or {})
            raw.update(
                {
                    "raw_median_m": raw.get("median_m"),
                    "used_source": (
                        "VLM_IMAGE_RAY_PLUS_HELD_TOOL_KINEMATIC_AXIS"
                    ),
                    "used_depth_m": float(camera_points[name][2]),
                    "raw_reflective_depth_rejected": True,
                }
            )
            depth_diagnostics[name] = raw

        edge_arm = arm_points["tip"] - arm_points["heel"]
        return {
            "source": (
                "VLM_IMAGE_RAYS_PLUS_HELD_TOOL_KINEMATIC_AXIS_"
                "REFLECTIVE_DEPTH_FALLBACK"
            ),
            "camera_points_m": {
                name: point.tolist()
                for name, point in camera_points.items()
            },
            "arm_base_points_m": {
                name: point.tolist()
                for name, point in arm_points.items()
            },
            "depth_diagnostics": depth_diagnostics,
            "depth_geometry": {
                "reflective_fallback_used": True,
                "non_reflective_handle_anchor_used": True,
                "blade_handle_junction_yx_1000": junction_point_yx,
                "blade_handle_junction_pixel_yx": list(junction_pixel),
                "handle_depth_anchor_yx_1000": handle_point_yx,
                "handle_anchor_step_from_junction_px": handle_step_px,
                "handle_depth_anchor_pixel_yx": list(handle_pixel),
                "handle_depth_anchor_depth_m": handle_depth_m,
                "handle_depth_anchor_local_range_mm": (
                    handle_depth_range_mm
                ),
                "handle_depth_anchor_camera_m": handle_camera.tolist(),
                "handle_depth_anchor_arm_base_m": handle_arm.tolist(),
                "handle_depth_anchor_from_tool_m": handle_tool.tolist(),
                "handle_depth_diagnostics": handle_depth_diagnostics,
                "raw_tip_to_heel_depth_difference_mm": (
                    raw_observation.get("depth_geometry") or {}
                ).get("tip_to_heel_depth_difference_mm"),
                "tip_to_heel_depth_difference_mm": abs(
                    float(camera_points["tip"][2])
                    - float(camera_points["heel"][2])
                )
                * 1000.0,
                "ray_to_tool_axis_miss_mm": ray_axis_miss_mm,
                "lateral_offset_tool_m": lateral_offset_tool.tolist(),
                "lateral_offset_magnitude_mm": lateral_magnitude_mm,
                "lateral_offset_disagreement_mm": (
                    lateral_disagreement_mm
                ),
                "image_width_ratio": image_width_ratio,
                "spine_depth_interpolated": False,
                "spine_raw_interpolation_residual_mm": None,
                "spine_correction_exceeds_review_limit": False,
            },
            "edge_direction_arm_base": (
                edge_arm / np.linalg.norm(edge_arm)
            ).tolist(),
            "observed_edge_length_m": edge_length_m,
            "registration_status": (
                "REFLECTIVE_DEPTH_FALLBACK_REQUIRES_FIRST_CUT_REVIEW"
            ),
            "confidence": float(blade["confidence"]),
        }

    @staticmethod
    def _pixel_to_plane(
        point_yx: np.ndarray,
        plane: Plane,
        intrinsics: dict[str, Any],
    ) -> np.ndarray:
        y, x = float(point_yx[0]), float(point_yx[1])
        ray = np.asarray(
            [
                (x - float(intrinsics["cx"])) / float(intrinsics["fx"]),
                (y - float(intrinsics["cy"])) / float(intrinsics["fy"]),
                1.0,
            ]
        )
        denominator = float(plane.normal @ ray)
        if abs(denominator) < 1e-8:
            raise RuntimeError("tracking ray is parallel to board plane")
        distance = float(plane.normal @ plane.origin_m) / denominator
        return plane.project(ray * distance)[0]

    @staticmethod
    def _plane_to_pixel(
        point_uv: np.ndarray,
        plane: Plane,
        intrinsics: dict[str, Any],
    ) -> list[int]:
        point = plane.lift(np.asarray(point_uv).reshape(1, 2))[0]
        if point[2] <= 0:
            raise RuntimeError("planned board point is behind the camera")
        x = int(round(float(intrinsics["fx"]) * point[0] / point[2] + float(intrinsics["cx"])))
        y = int(round(float(intrinsics["fy"]) * point[1] / point[2] + float(intrinsics["cy"])))
        return [y, x]

    async def _publish_plan(self, plan: dict[str, Any]) -> None:
        self.sequence += 1
        await self.fabric.publish(
            {
                "schema": plan["schema"],
                "schema_version": plan["schema_version"],
                "stream": "skills.vegetable_cutting.plan",
                "provider_id": "skill.vegetable_cutting",
                "provider_instance_id": plan["skill_id"],
                "boot_id": plan["skill_id"],
                "sequence": self.sequence,
                "observed_at_us": plan["created_at_us"],
                "freshness_ms": None,
                "related_skill_id": plan["skill_id"],
                "valid": True,
                "coordinate_frame": self.config["frames"]["arm_base"],
                "data": plan,
            }
        )

    async def _publish_tracking(self, tracking: dict[str, Any]) -> None:
        current = await self.progress.snapshot()
        self.sequence += 1
        now_us = time.time_ns() // 1000
        try:
            await self.fabric.publish(
                {
                    "schema": "midbrain.skill.vegetable_cutting.tracking",
                    "schema_version": 1,
                    "stream": "skills.vegetable_cutting.tracking",
                    "provider_id": "skill.vegetable_cutting",
                    "provider_instance_id": current["skill_id"],
                    "boot_id": current["skill_id"],
                    "sequence": self.sequence,
                    "observed_at_us": now_us,
                    "freshness_ms": None,
                    "related_skill_id": current["skill_id"],
                    "valid": bool(tracking.get("accepted_without_vlm")),
                    "data": tracking,
                }
            )
        except Exception:
            pass
