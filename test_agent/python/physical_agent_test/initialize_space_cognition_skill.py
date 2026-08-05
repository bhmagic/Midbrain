from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .fabric_client import FabricClient
from .manager_client import ManagerClient
from .spatial_frames import WORLD_CONVENTION_ID


@dataclass(frozen=True)
class InitializationResult:
    skill_id: str
    session_epoch: str
    world_frame: str
    body_frame: str
    selected_providers: dict[str, str]
    reused_physical_provider: bool
    motion_inhibit: dict[str, Any]
    operation: str
    previous_session_epoch: str | None
    invalidated_workcell_activation_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "session_epoch": self.session_epoch,
            "world_frame": self.world_frame,
            "body_frame": self.body_frame,
            "selected_providers": self.selected_providers,
            "reused_physical_provider": self.reused_physical_provider,
            "motion_inhibit": self.motion_inhibit,
            "operation": self.operation,
            "previous_session_epoch": self.previous_session_epoch,
            "invalidated_workcell_activation_ids": list(
                self.invalidated_workcell_activation_ids
            ),
        }


class InitializeSpaceCognitionSkill:
    """Finite Skill that establishes or deliberately replaces the local origin."""

    def __init__(
        self,
        manager: ManagerClient,
        fabric: FabricClient,
        *,
        camera_provider_id: str,
        vio_provider_id: str,
        timeout_s: float,
    ):
        self.manager = manager
        self.fabric = fabric
        self.camera_provider_id = camera_provider_id
        self.vio_provider_id = vio_provider_id
        self.timeout_s = timeout_s
        self._lock = asyncio.Lock()

    async def ensure_tracking(self) -> dict[str, Any]:
        """Establish a usable current world epoch without resetting its origin."""

        async with self._lock:
            skill_id = str(uuid.uuid4())
            inhibit_owner = f"skill:{skill_id}"
            selected = {
                "head_camera": self.camera_provider_id,
                "head_depth": self.camera_provider_id,
                "head_imu": self.camera_provider_id,
                "local_vio": self.vio_provider_id,
            }
            started_at_us = int(time.time() * 1_000_000)
            inhibit_status: dict[str, Any] | None = None
            await self._publish_status(
                skill_id,
                "RUNNING",
                "ensure_current_world_tracking",
                selected,
                started_at_us=started_at_us,
                details={
                    "operation": "ENSURE_EXISTING_EPOCH",
                    "epoch_reset_allowed": False,
                    "workcell_calibration_revocation_allowed": False,
                },
            )
            try:
                await self.manager.set_hot(self.camera_provider_id)
                await self._wait_for_streams(
                    [
                        "camera.rgb.frame_ref",
                        "camera.depth_aligned_to_rgb.frame_ref",
                        "camera.imu.accel",
                        "camera.imu.gyro",
                        "camera.calibration",
                        "camera.device_info",
                    ]
                )
                await self.manager.set_hot(self.vio_provider_id)

                tracking = await self._current_tracking_context()
                stationary_gate = "EXISTING_TRACKING_EPOCH"
                if tracking is None:
                    inhibit_status = await self.manager.acquire_motion_inhibit(
                        owner_id=inhibit_owner,
                        reason=(
                            "stationary gravity initialization for current "
                            "Local VIO world epoch"
                        ),
                        related_skill_id=skill_id,
                    )
                    await self._wait_for_vio_motion_inhibit()
                    tracking = await self._wait_for_tracking_context(
                        expected_epoch=None
                    )
                    stationary_gate = "GLOBAL_MOTION_INHIBIT"

                result = {
                    "skill_id": skill_id,
                    "operation": "ENSURE_EXISTING_EPOCH",
                    **tracking,
                    "selected_providers": selected,
                    "stationary_gate": stationary_gate,
                    "motion_inhibit": inhibit_status,
                    "global_motion_inhibit_acquired": (
                        inhibit_status is not None
                    ),
                    "epoch_reset_performed": False,
                    "workcell_calibrations_revoked": False,
                    "physical_motion_submitted": False,
                }
                await self._publish_status(
                    skill_id,
                    "SUCCEEDED",
                    "current_world_tracking_ready",
                    selected,
                    started_at_us=started_at_us,
                    details=result,
                    result=result,
                )
                return {"status": "tracking_ready", "result": result}
            except Exception as error:
                await self._publish_status(
                    skill_id,
                    "FAILED",
                    "ensure_current_world_tracking",
                    selected,
                    started_at_us=started_at_us,
                    details={
                        "operation": "ENSURE_EXISTING_EPOCH",
                        "epoch_reset_performed": False,
                        "workcell_calibrations_revoked": False,
                        "error": str(error),
                    },
                )
                raise
            finally:
                if inhibit_status is not None:
                    try:
                        await self.manager.release_motion_inhibit(
                            owner_id=inhibit_owner
                        )
                    except Exception:
                        pass

    async def verify_tracking(
        self,
        *,
        fixed_rig_confirmed: bool,
    ) -> dict[str, Any]:
        """Verify or recover tracking without resetting the VIO epoch."""

        if fixed_rig_confirmed is not True:
            raise ValueError(
                "fixed VIO rig confirmation is required before tracking check"
            )
        async with self._lock:
            skill_id = str(uuid.uuid4())
            selected = {
                "head_camera": self.camera_provider_id,
                "head_depth": self.camera_provider_id,
                "head_imu": self.camera_provider_id,
                "local_vio": self.vio_provider_id,
            }
            started_at_us = int(time.time() * 1_000_000)
            await self._publish_status(
                skill_id,
                "RUNNING",
                "verify_fixed_vio_rig",
                selected,
                started_at_us=started_at_us,
                details={
                    "operation": "VERIFY_EXISTING_EPOCH",
                    "fixed_rig_confirmed": True,
                    "epoch_reset_allowed": False,
                    "global_motion_inhibit_allowed": False,
                },
            )
            try:
                await self.manager.set_hot(self.camera_provider_id)
                await self._wait_for_streams(
                    [
                        "camera.rgb.frame_ref",
                        "camera.depth_aligned_to_rgb.frame_ref",
                        "camera.imu.accel",
                        "camera.imu.gyro",
                        "camera.calibration",
                        "camera.device_info",
                    ]
                )
                await self.manager.set_hot(self.vio_provider_id)
                current = await self.fabric.latest_optional(
                    "localization.vio.status"
                )
                current_data = (current or {}).get("data") or {}
                attestation_result: dict[str, Any] | None = None
                if not (
                    current_data.get("tracking_state") == "TRACKING"
                    and current_data.get("convention_id")
                    == WORLD_CONVENTION_ID
                    and current_data.get("session_epoch")
                    and current_data.get("world_frame")
                ):
                    attestation_result = await self.manager.provider_request(
                        self.vio_provider_id,
                        action="attest_fixed_rig_stationary",
                        payload={
                            "fixed_rig_confirmed": True,
                            "duration_s": min(
                                120.0,
                                max(5.0, self.timeout_s + 5.0),
                            ),
                        },
                        request_id=str(uuid.uuid4()),
                        related_skill_id=skill_id,
                    )
                vio_status = await self._wait_for_vio_tracking(
                    expected_epoch=None
                )
                if (
                    vio_status.get("convention_id")
                    != WORLD_CONVENTION_ID
                ):
                    raise RuntimeError(
                        "VIO reached TRACKING with a legacy or unknown "
                        "coordinate convention"
                    )
                session_epoch = str(
                    vio_status.get("session_epoch") or ""
                )
                world_frame = str(vio_status.get("world_frame") or "")
                if not session_epoch or not world_frame:
                    raise RuntimeError(
                        "VIO reached TRACKING without complete epoch identity"
                    )
                result = {
                    "skill_id": skill_id,
                    "operation": "VERIFY_EXISTING_EPOCH",
                    "session_epoch": session_epoch,
                    "world_frame": world_frame,
                    "tracking_state": "TRACKING",
                    "convention_id": WORLD_CONVENTION_ID,
                    "epoch_reset_performed": False,
                    "workcell_calibrations_revoked": False,
                    "selected_providers": selected,
                    "global_motion_inhibit_acquired": False,
                    "stationary_gate": (
                        "EXISTING_TRACKING_EPOCH"
                        if attestation_result is None
                        else "FIXED_RIG_OPERATOR_ATTESTATION"
                    ),
                    "vio_attestation_result": attestation_result,
                    "fixed_rig_attestation": {
                        "confirmed": True,
                        "statement": (
                            "camera and IMU are rigidly fixed together and "
                            "the rig remained stationary during verification"
                        ),
                    },
                }
                await self._publish_status(
                    skill_id,
                    "SUCCEEDED",
                    "verify_fixed_vio_rig",
                    selected,
                    started_at_us=started_at_us,
                    details=result,
                    result=result,
                )
                return {
                    "status": "tracking_ready",
                    "result": result,
                }
            except Exception as error:
                await self._publish_status(
                    skill_id,
                    "FAILED",
                    "verify_fixed_vio_rig",
                    selected,
                    started_at_us=started_at_us,
                    details={
                        "operation": "VERIFY_EXISTING_EPOCH",
                        "fixed_rig_confirmed": True,
                        "epoch_reset_performed": False,
                        "global_motion_inhibit_acquired": False,
                        "error": str(error),
                    },
                )
                raise

    async def run(self, *, force_reset: bool = False) -> dict[str, Any]:
        async with self._lock:
            existing = await self.fabric.latest_optional("skills.initialize_space_cognition.status")
            existing_data = (existing or {}).get("data") or {}
            if (
                not force_reset
                and existing_data.get("state") == "SUCCEEDED"
                and existing_data.get("session_epoch")
            ):
                current = await self._current_tracking_context()
                existing_result = existing_data.get("result") or {}
                if (
                    current is not None
                    and current["session_epoch"]
                    == str(existing_data.get("session_epoch") or "")
                    and current["session_epoch"]
                    == str(existing_result.get("session_epoch") or "")
                ):
                    return {
                        "status": "already_initialized",
                        "result": existing_result,
                    }

            skill_id = str(uuid.uuid4())
            inhibit_owner = f"skill:{skill_id}"
            previous_vio = await self.fabric.latest_optional(
                "localization.vio.status"
            )
            previous_vio_data = (previous_vio or {}).get("data") or {}
            previous_session_epoch = str(
                previous_vio_data.get("session_epoch") or ""
            ) or None
            selected = {
                "head_camera": self.camera_provider_id,
                "head_depth": self.camera_provider_id,
                "head_imu": self.camera_provider_id,
                "local_vio": self.vio_provider_id,
            }
            inhibit_status: dict[str, Any] = {}
            started_at_us = int(time.time() * 1_000_000)
            await self._publish_status(
                skill_id,
                "RUNNING",
                "pause_robot_motion",
                selected,
                started_at_us=started_at_us,
            )
            try:
                inhibit_status = await self.manager.acquire_motion_inhibit(
                    owner_id=inhibit_owner,
                    reason="stationary gravity initialization for local VIO",
                    related_skill_id=skill_id,
                )
                await self._publish_status(
                    skill_id,
                    "RUNNING",
                    "initialize_head_camera",
                    selected,
                    started_at_us=started_at_us,
                    details={"motion_inhibit": inhibit_status},
                )

                await self.manager.set_hot(self.camera_provider_id)
                await self._wait_for_streams(
                    [
                        "camera.rgb.frame_ref",
                        "camera.depth_aligned_to_rgb.frame_ref",
                        "camera.imu.accel",
                        "camera.imu.gyro",
                        "camera.calibration",
                        "camera.device_info",
                    ]
                )
                await self._publish_status(
                    skill_id,
                    "RUNNING",
                    "initialize_head_depth_and_scanners",
                    selected,
                    started_at_us=started_at_us,
                    details={"physical_provider_reused": True},
                )
                await self._publish_status(
                    skill_id,
                    "RUNNING",
                    "initialize_head_imu",
                    selected,
                    started_at_us=started_at_us,
                    details={"physical_provider_reused": True},
                )

                await self.manager.set_hot(self.vio_provider_id)
                await self._wait_for_vio_motion_inhibit()
                invalidated_activation_ids: tuple[str, ...] = ()
                if force_reset:
                    invalidated_activation_ids = (
                        await self._revoke_active_workcell_calibrations(
                            skill_id=skill_id,
                            previous_session_epoch=previous_session_epoch,
                        )
                    )
                await self._publish_status(
                    skill_id,
                    "RUNNING",
                    "initialize_local_vio",
                    selected,
                    started_at_us=started_at_us,
                    details={
                        "operation": (
                            "REESTABLISH_ORIGIN"
                            if force_reset
                            else "INITIALIZE_IF_NEEDED"
                        ),
                        "previous_session_epoch": previous_session_epoch,
                        "invalidated_workcell_activation_ids": list(
                            invalidated_activation_ids
                        ),
                    },
                )
                reset_result = await self._request_vio_initialization(
                    force_reset=force_reset,
                    related_skill_id=skill_id,
                )
                vio_status = await self._wait_for_vio_tracking(
                    expected_epoch=reset_result.get("session_epoch")
                )
                if force_reset:
                    late_invalidations = (
                        await self._revoke_active_workcell_calibrations(
                            skill_id=skill_id,
                            previous_session_epoch=previous_session_epoch,
                            preserve_session_epoch=str(
                                vio_status["session_epoch"]
                            ),
                        )
                    )
                    invalidated_activation_ids = tuple(
                        dict.fromkeys(
                            (
                                *invalidated_activation_ids,
                                *late_invalidations,
                            )
                        )
                    )
                pose = await self.fabric.latest("localization.body.pose")
                pose_data = pose.get("data") or {}
                result = InitializationResult(
                    skill_id=skill_id,
                    session_epoch=str(vio_status["session_epoch"]),
                    world_frame=str(vio_status["world_frame"]),
                    body_frame=str(pose_data.get("body_frame") or "body_base"),
                    selected_providers=selected,
                    reused_physical_provider=True,
                    motion_inhibit=inhibit_status,
                    operation=(
                        "REESTABLISH_ORIGIN"
                        if force_reset
                        else "INITIALIZE_IF_NEEDED"
                    ),
                    previous_session_epoch=previous_session_epoch,
                    invalidated_workcell_activation_ids=(
                        invalidated_activation_ids
                    ),
                )
                await self._publish_status(
                    skill_id,
                    "SUCCEEDED",
                    "initialize_body_pose",
                    selected,
                    started_at_us=started_at_us,
                    details={
                        "session_epoch": result.session_epoch,
                        "world_frame": result.world_frame,
                        "body_position_m": pose_data.get("position_m"),
                        "yaw_definition": "initial_body_forward_is_zero",
                        "previous_session_epoch": previous_session_epoch,
                        "invalidated_workcell_activation_ids": list(
                            invalidated_activation_ids
                        ),
                        "initialization_control_status": reset_result.get("status"),
                        "control_response_warning": reset_result.get("control_response_warning")
                        or reset_result.get("status_publish_warning"),
                    },
                    result=result.to_dict(),
                )
                return {"status": "initialized", "result": result.to_dict()}
            except Exception as error:
                await self._publish_status(
                    skill_id,
                    "FAILED",
                    "failed",
                    selected,
                    started_at_us=started_at_us,
                    details={"error": str(error)},
                )
                raise
            finally:
                try:
                    await self.manager.release_motion_inhibit(owner_id=inhibit_owner)
                except Exception:
                    pass

    async def _revoke_active_workcell_calibrations(
        self,
        *,
        skill_id: str,
        previous_session_epoch: str | None,
        preserve_session_epoch: str | None = None,
    ) -> tuple[str, ...]:
        catalog = await self.manager.workcell_calibrations()
        active = [
            activation
            for activation in catalog.get("activations") or []
            if isinstance(activation, dict)
            and str(activation.get("state") or "").upper() == "ACTIVE"
            and activation.get("motion_usable") is True
            and (
                preserve_session_epoch is None
                or str(activation.get("session_epoch") or "")
                != preserve_session_epoch
            )
        ]
        revoked: list[str] = []
        for activation in active:
            activation_id = str(activation.get("activation_id") or "").strip()
            if not activation_id:
                raise RuntimeError(
                    "Manager returned an active workcell calibration without "
                    "an activation_id"
                )
            await self.manager.revoke_workcell_calibration(
                activation_id,
                request_id=f"{skill_id}:revoke:{activation_id}",
                revoked_by="skill.initialize_space_cognition",
                reason=(
                    "Local spatial origin is being re-established"
                    + (
                        f" from VIO epoch {previous_session_epoch}"
                        if previous_session_epoch
                        else ""
                    )
                ),
            )
            revoked.append(activation_id)
        return tuple(revoked)

    async def _wait_for_vio_motion_inhibit(self) -> None:
        """Wait until the VIO Provider has observed the Manager inhibit state.

        Releasing the inhibit before the Provider sees it leaves startup gravity
        initialization with zero accepted samples. The wait is short because the
        Provider polls Fabric continuously once camera inputs are available.
        """
        deadline = asyncio.get_running_loop().time() + min(5.0, self.timeout_s)
        last_status: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.fabric.latest_optional("localization.vio.status")
            last_status = (observation or {}).get("data") or {}
            if last_status.get("motion_inhibited") is True:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(
            "VIO Provider did not observe motion inhibit before initialization: "
            f"{last_status}"
        )

    async def _request_vio_initialization(
        self,
        *,
        force_reset: bool,
        related_skill_id: str,
    ) -> dict[str, Any]:
        before = await self.fabric.latest_optional("localization.vio.status")
        previous_epoch = str(((before or {}).get("data") or {}).get("session_epoch") or "")
        action = "force_reset" if force_reset else "initialize"
        last_error: Exception | None = None

        for attempt in range(2):
            request_id = str(uuid.uuid4())
            try:
                return await self.manager.provider_request(
                    self.vio_provider_id,
                    action=action,
                    payload={"body_position_m": [0.0, 0.0, 0.0], "yaw_rad": 0.0},
                    request_id=request_id,
                    related_skill_id=related_skill_id,
                )
            except Exception as error:
                last_error = error
                recovered = await self._wait_for_reset_acceptance(
                    previous_epoch=previous_epoch,
                    timeout_s=3.0,
                )
                if recovered is not None:
                    recovered["status"] = "accepted_after_control_response_error"
                    recovered["control_response_warning"] = str(error)
                    return recovered
                if attempt == 0:
                    await asyncio.sleep(0.25)
                    await self.manager.set_hot(self.vio_provider_id)

        assert last_error is not None
        raise last_error

    async def _wait_for_reset_acceptance(
        self,
        *,
        previous_epoch: str,
        timeout_s: float,
    ) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.fabric.latest_optional("localization.vio.status")
            data = (observation or {}).get("data") or {}
            epoch = str(data.get("session_epoch") or "")
            if epoch and epoch != previous_epoch and data.get("tracking_state") in {
                "INITIALIZING",
                "TRACKING",
                "DEGRADED",
            }:
                return {
                    "session_epoch": epoch,
                    "world_frame": data.get("world_frame"),
                }
            await asyncio.sleep(0.1)
        return None

    async def _wait_for_streams(self, streams: list[str]) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        missing = list(streams)
        while asyncio.get_running_loop().time() < deadline:
            missing = []
            for stream in streams:
                observation = await self.fabric.latest_optional(stream)
                if observation is None or observation.get("valid") is False:
                    missing.append(stream)
            if not missing:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"timed out waiting for streams: {', '.join(missing)}")

    async def _wait_for_vio_tracking(self, *, expected_epoch: str | None) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        last_status: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.fabric.latest_optional("localization.vio.status")
            last_status = (observation or {}).get("data") or {}
            epoch_matches = expected_epoch is None or last_status.get("session_epoch") == expected_epoch
            if epoch_matches and last_status.get("tracking_state") == "TRACKING":
                return last_status
            await asyncio.sleep(0.1)
        raise TimeoutError(f"VIO did not reach TRACKING: {last_status}")

    async def _wait_for_tracking_context(
        self,
        *,
        expected_epoch: str | None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        last_status: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            context = await self._current_tracking_context()
            if context is not None:
                if (
                    expected_epoch is None
                    or context["session_epoch"] == expected_epoch
                ):
                    return context
            observation = await self.fabric.latest_optional(
                "localization.vio.status"
            )
            last_status = (observation or {}).get("data") or {}
            await asyncio.sleep(0.1)
        raise TimeoutError(
            "VIO did not publish a current TRACKING body pose: "
            f"{last_status}"
        )

    async def _current_tracking_context(self) -> dict[str, Any] | None:
        status_observation, pose_observation = await asyncio.gather(
            self.fabric.latest_optional("localization.vio.status"),
            self.fabric.latest_optional("localization.body.pose"),
        )
        if not self._observation_is_current(status_observation):
            return None
        if not self._observation_is_current(pose_observation):
            return None
        status = (status_observation or {}).get("data") or {}
        pose = (pose_observation or {}).get("data") or {}
        if status.get("tracking_state") != "TRACKING":
            return None
        if (
            status.get("convention_id") != WORLD_CONVENTION_ID
            or pose.get("convention_id") != WORLD_CONVENTION_ID
        ):
            return None
        session_epoch = str(status.get("session_epoch") or "")
        world_frame = str(status.get("world_frame") or "")
        if not session_epoch or not world_frame:
            return None
        if str(pose.get("session_epoch") or "") != session_epoch:
            return None
        if str(pose.get("world_frame") or "") != world_frame:
            return None
        return {
            "session_epoch": session_epoch,
            "world_frame": world_frame,
            "body_frame": str(pose.get("body_frame") or "body_base"),
            "tracking_state": "TRACKING",
            "convention_id": WORLD_CONVENTION_ID,
            "body_position_m": pose.get("position_m"),
        }

    @staticmethod
    def _observation_is_current(
        observation: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(observation, dict):
            return False
        if observation.get("valid") is False:
            return False
        observed_at_us = int(observation.get("observed_at_us") or 0)
        freshness_ms = observation.get("freshness_ms")
        if observed_at_us <= 0 or freshness_ms is None:
            return observed_at_us > 0
        maximum_age_us = max(0.0, float(freshness_ms)) * 1000.0
        return time.time() * 1_000_000 - observed_at_us <= maximum_age_us

    async def _publish_status(
        self,
        skill_id: str,
        state: str,
        subskill: str,
        selected_providers: dict[str, str],
        *,
        started_at_us: int,
        details: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        now_us = int(time.time() * 1_000_000)
        payload = {
            "skill_id": skill_id,
            "skill": "initialize_space_cognition",
            "state": state,
            "current_subskill": subskill,
            "started_at_us": started_at_us,
            "updated_at_us": now_us,
            "selected_providers": selected_providers,
            "details": details or {},
            "result": result,
        }
        if result:
            payload.update(
                {
                    "session_epoch": result.get("session_epoch"),
                    "world_frame": result.get("world_frame"),
                }
            )
        await self.fabric.publish(
            {
                "schema": "physical_agent.skill_status",
                "schema_version": 1,
                "stream": "skills.initialize_space_cognition.status",
                "provider_id": "physical-agent-test-scaffold",
                "provider_instance_id": "agent-ui-local",
                "boot_id": "agent-ui-local",
                "sequence": now_us,
                "observed_at_us": now_us,
                "freshness_ms": None,
                "related_skill_id": skill_id,
                "valid": state != "FAILED",
                "data": payload,
            }
        )
