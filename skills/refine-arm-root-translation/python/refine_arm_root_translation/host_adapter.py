from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol

import numpy as np
from PIL import Image


class ManagerProtocol(Protocol):
    async def providers(self) -> list[dict[str, Any]]: ...

    async def workcell_calibrations(self) -> dict[str, Any]: ...

    async def refine_workcell_calibration_translation(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]: ...


class FabricProtocol(Protocol):
    async def latest_optional(self, stream: str) -> dict[str, Any] | None: ...

    async def transform(self, **kwargs: Any) -> dict[str, Any]: ...


class SpatialProtocol(Protocol):
    async def prepare_context(self, **kwargs: Any) -> Any: ...

    async def revalidate_context_binding(self, context: Any) -> Any: ...


class VlmRouterProtocol(Protocol):
    async def generate_images(
        self,
        *,
        images: list[tuple[bytes, str]],
        prompt: str,
        request_id: str | None = None,
    ) -> Any: ...


class VisualEvidenceProtocol(Protocol):
    async def register_channels(self, **kwargs: Any) -> dict[str, Any]: ...


class ArmDataDependencyError(RuntimeError):
    """Identify a recoverable local arm-observation dependency failure."""

    def __init__(
        self,
        reason: str,
        *,
        provider_id: str | None = None,
    ) -> None:
        super().__init__(str(reason))
        self.provider_id = str(provider_id or "").strip() or None


class ArmRootTranslationRefinementAdapter:
    """Host a profile-driven refinement Skill in its private Python venv."""

    def __init__(
        self,
        *,
        skill_root: Path,
        manager: ManagerProtocol,
        fabric: FabricProtocol,
        spatial: SpatialProtocol,
        vlm_router: VlmRouterProtocol,
        visual_evidence_store: VisualEvidenceProtocol,
        profile_path: Path,
        runtime_landmark_bindings: dict[str, Any] | None = None,
        reference_assets: dict[str, Path] | None = None,
    ) -> None:
        self.skill_root = Path(skill_root).resolve()
        self.manager = manager
        self.fabric = fabric
        self.spatial = spatial
        self.vlm_router = vlm_router
        self.visual_evidence_store = visual_evidence_store
        self.profile_path = Path(profile_path).resolve()
        profiles_root = (self.skill_root / "profiles").resolve()
        if profiles_root not in self.profile_path.parents:
            raise ValueError(
                "effector profile must be inside the Skill profiles directory"
            )
        venv_python = (
            self.skill_root / ".venv" / "Scripts" / "python.exe"
            if os.name == "nt"
            else self.skill_root / ".venv" / "bin" / "python"
        )
        self.python_path = venv_python.resolve()
        self.entrypoint_path = (
            self.skill_root
            / "python"
            / "refine_arm_root_translation"
            / "rpc_entrypoint.py"
        ).resolve()
        self.runtime_landmark_bindings = copy.deepcopy(
            runtime_landmark_bindings or {}
        )
        self.reference_assets = {
            str(asset_id): Path(path).resolve()
            for asset_id, path in (reference_assets or {}).items()
        }
        self.profile = self._read_profile()
        self._context: Any | None = None
        self._session_dir: Path | None = None
        self._arm_dependency_error: ArmDataDependencyError | None = None
        self._lock = asyncio.Lock()
        self.last_result: dict[str, Any] | None = None

    async def invoke(self, arguments: dict[str, Any]) -> str:
        result = await self.run(
            adoption_factor=arguments.get("adoption_factor", 1.0),
            sample_count=arguments.get("sample_count", 1),
            landmark_id=arguments.get("landmark_id"),
        )
        return json.dumps(result, ensure_ascii=False, default=str)

    async def run(
        self,
        *,
        adoption_factor: float = 1.0,
        sample_count: int = 1,
        landmark_id: str | None = None,
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise RuntimeError("arm-root translation refinement is already running")
        if not self.python_path.is_file():
            raise RuntimeError(
                "translation-refinement private venv is missing; run its scripts/setup.ps1"
            )
        if not self.entrypoint_path.is_file() or not self.profile_path.is_file():
            raise RuntimeError("translation-refinement Skill installation is incomplete")
        arguments = {
            "adoption_factor": adoption_factor,
            "sample_count": sample_count,
            "landmark_id": landmark_id,
        }
        async with self._lock:
            self._arm_dependency_error = None
            try:
                await self._preflight_arm_data()
                with tempfile.TemporaryDirectory(
                    prefix="midbrain-arm-root-refinement-"
                ) as temporary:
                    self._session_dir = Path(temporary).resolve()
                    try:
                        result = await self._run_process(arguments)
                    finally:
                        self._context = None
                        self._session_dir = None
            except ArmDataDependencyError as error:
                result = await self._dependency_unavailable(
                    arguments=arguments,
                    error=error,
                )
            except RuntimeError:
                if self._arm_dependency_error is None:
                    raise
                result = await self._dependency_unavailable(
                    arguments=arguments,
                    error=self._arm_dependency_error,
                )
        if not isinstance(result, dict):
            raise RuntimeError("translation-refinement Skill returned invalid data")
        self.last_result = result
        return result

    async def _preflight_arm_data(self) -> None:
        compatibility = self.profile.get("robot_compatibility")
        if not isinstance(compatibility, dict):
            raise RuntimeError("effector profile robot compatibility is missing")
        arm_base_frame = self._text(
            compatibility,
            "arm_base_frame",
            "effector profile robot compatibility",
        )
        controlled_frame = self._text(
            compatibility,
            "controlled_frame",
            "effector profile robot compatibility",
        )
        try:
            identity = await self._arm_identity({})
        except Exception as error:
            raise ArmDataDependencyError(
                f"current arm model identity is unavailable: {error}"
            ) from error
        joint_state = await self.fabric.latest_optional("robot_arm.joint_state")
        if not isinstance(joint_state, dict):
            raise ArmDataDependencyError(
                "current robot_arm.joint_state observation is unavailable",
                provider_id=identity["arm_provider_id"],
            )
        observed_at_us = joint_state.get("observed_at_us")
        if not isinstance(observed_at_us, int) or observed_at_us <= 0:
            raise ArmDataDependencyError(
                "current robot_arm.joint_state has no usable timestamp",
                provider_id=identity["arm_provider_id"],
            )
        self._validate_arm_observation_identity(
            joint_state,
            identity=identity,
        )
        bracket_timestamp_us = time.time_ns() // 1000
        try:
            await self._bracketed_transform(
                from_frame=controlled_frame,
                to_frame=arm_base_frame,
                at_us=bracket_timestamp_us,
                session_epoch=None,
                label="arm FK readiness preflight",
            )
        except Exception as error:
            raise ArmDataDependencyError(
                str(error),
                provider_id=identity["arm_provider_id"],
            ) from error

    @staticmethod
    def _validate_arm_observation_identity(
        observation: dict[str, Any],
        *,
        identity: dict[str, Any],
    ) -> None:
        fields = (
            ("provider_id", "arm_provider_id"),
            ("provider_instance_id", "arm_provider_instance_id"),
            ("boot_id", "arm_boot_id"),
        )
        for observation_field, identity_field in fields:
            observed = str(observation.get(observation_field) or "").strip()
            expected = str(identity.get(identity_field) or "").strip()
            if observed and expected and observed != expected:
                raise ArmDataDependencyError(
                    "robot_arm.joint_state identity does not match the current "
                    f"arm model ({observation_field})",
                    provider_id=str(identity.get("arm_provider_id") or ""),
                )

    async def _dependency_unavailable(
        self,
        *,
        arguments: dict[str, Any],
        error: ArmDataDependencyError,
    ) -> dict[str, Any]:
        provider_id = error.provider_id
        manager_error: str | None = None
        providers: list[dict[str, Any]] = []
        try:
            value = await self.manager.providers()
            if isinstance(value, list):
                providers = [item for item in value if isinstance(item, dict)]
        except Exception as manager_failure:
            manager_error = str(manager_failure)
        configured = next(
            (
                provider
                for provider in providers
                if str(provider.get("config", {}).get("id") or "")
                == provider_id
            ),
            None,
        )
        provider_snapshot = self._provider_snapshot(configured)
        required_capability = "robot_arm.joint_state"
        if provider_id and configured is not None:
            required_next_tool = {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": provider_id,
                    "action": "hot",
                    "required_capability": required_capability,
                },
            }
            next_step = (
                "Call required_next_tool once, then retry the preserved "
                "refinement request. If the same dependency result returns "
                "after that HOT recovery, stop and report an arm-transform "
                "publication fault; do not repeat the lifecycle transition."
            )
        else:
            required_next_tool = {
                "name": "inspect_midbrain_runtime",
                "arguments": {},
            }
            next_step = (
                "Call required_next_tool, identify the configured Provider "
                "that supplies robot_arm.joint_state, make it HOT through the "
                "existing lifecycle tool, and then retry the preserved "
                "refinement request once."
            )
        return {
            "schema": "midbrain.arm_root_translation_refinement",
            "schema_version": 1,
            "status": "DEPENDENCY_UNAVAILABLE",
            "workflow_complete": False,
            "eligible_for_state_update": False,
            "state_update_applied": False,
            "retry_same_tool": False,
            "reason": str(error),
            "multi_sample_refinement": {
                "feature_name": "MULTI_SAMPLE_REFINEMENT",
                "requested_sample_count": int(arguments["sample_count"]),
                "completed_sample_count": 0,
                "accepted_sample_count": 0,
                "excluded_sample_count": 0,
                "accepted_sample_indexes": [],
                "excluded_sample_indexes": [],
                "aggregation": "NOT_STARTED_DEPENDENCY_UNAVAILABLE",
                "aggregation_population": "NO_ACCEPTED_SAMPLES",
                "threshold_scale": 0,
                "threshold_scale_basis": "ACCEPTED_SAMPLE_COUNT",
                "samples": [],
            },
            "landmark_depth_reselection": {
                "required": False,
                "attempt_count": 1,
                "outcome": "NOT_REQUIRED",
                "initial_invalid_points": [],
            },
            "required_capability": required_capability,
            "dependency": {
                "kind": "LOCAL_ARM_FK_STREAM",
                "provider_id": provider_id,
                "provider": provider_snapshot,
                "manager_inspection_error": manager_error,
            },
            "required_next_tool": required_next_tool,
            "retry_after_prerequisite": {
                "name": "refine_arm_root_translation",
                "arguments": copy.deepcopy(arguments),
            },
            "message": next_step,
            "physical_motion_submitted": False,
            "physical_motion_authorized": False,
            "rotation_change_allowed": False,
            "rotation_change_rad": 0.0,
        }

    @staticmethod
    def _provider_snapshot(provider: Any) -> dict[str, Any] | None:
        if not isinstance(provider, dict):
            return None
        config = provider.get("config")
        config = config if isinstance(config, dict) else {}
        report = provider.get("report")
        report = report if isinstance(report, dict) else {}
        return {
            "provider_id": str(
                config.get("id") or report.get("provider_id") or ""
            ),
            "process_state": provider.get("process_state"),
            "residency": report.get("residency"),
            "health": report.get("health"),
            "ready": bool(report.get("ready")),
            "expired": bool(report.get("expired")),
            "instance_id": report.get("instance_id"),
            "boot_id": report.get("boot_id"),
            "last_seen": report.get("last_seen"),
        }

    async def _run_process(self, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._session_dir is not None
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        process = await asyncio.create_subprocess_exec(
            str(self.python_path),
            str(self.entrypoint_path),
            "--profile",
            str(self.profile_path),
            "--session-dir",
            str(self._session_dir),
            cwd=str(self.skill_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            limit=8 * 1024 * 1024,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            (json.dumps({"type": "invoke", "arguments": arguments}) + "\n").encode(
                "utf-8"
            )
        )
        await process.stdin.drain()
        writer_lock = asyncio.Lock()
        response_tasks: list[asyncio.Task[None]] = []
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    stderr = await self._stderr(process)
                    raise RuntimeError(
                        "translation-refinement Skill exited without a result"
                        + (f": {stderr}" if stderr else "")
                    )
                message = json.loads(line.decode("utf-8"))
                if message.get("type") == "request":
                    response_tasks.append(
                        asyncio.create_task(
                            self._answer_request(
                                process,
                                message,
                                writer_lock=writer_lock,
                            )
                        )
                    )
                    continue
                if message.get("type") != "result":
                    raise RuntimeError("translation-refinement Skill emitted invalid RPC")
                if response_tasks:
                    await asyncio.gather(*response_tasks)
                await process.wait()
                if message.get("ok") is not True:
                    error = message.get("error") or {}
                    raise RuntimeError(str(error.get("message") or error))
                return message.get("result")
        except BaseException:
            for task in response_tasks:
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _answer_request(
        self,
        process: asyncio.subprocess.Process,
        message: dict[str, Any],
        *,
        writer_lock: asyncio.Lock | None = None,
    ) -> None:
        assert process.stdin is not None
        request_id = message.get("id")
        try:
            result = await self._dispatch(
                str(message.get("method") or ""),
                message.get("parameters") or {},
            )
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as error:
            if isinstance(error, ArmDataDependencyError):
                self._arm_dependency_error = error
            error_payload: dict[str, Any] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            http_response = getattr(error, "response", None)
            status_code = getattr(http_response, "status_code", None)
            if isinstance(status_code, int):
                error_payload["status_code"] = status_code
                try:
                    error_payload["response_body"] = http_response.json()
                except Exception:
                    response_text = str(getattr(http_response, "text", "")).strip()
                    if response_text:
                        error_payload["response_body"] = response_text[:2000]
            response = {
                "id": request_id,
                "ok": False,
                "error": error_payload,
            }
        if writer_lock is None:
            process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
            await process.stdin.drain()
        else:
            async with writer_lock:
                process.stdin.write((json.dumps(response) + "\n").encode("utf-8"))
                await process.stdin.drain()

    async def _dispatch(self, method: str, parameters: dict[str, Any]) -> Any:
        if method == "manager.workcell_calibrations":
            return await self.manager.workcell_calibrations()
        if method == "manager.refine_workcell_translation":
            return await self.manager.refine_workcell_calibration_translation(
                parameters["request"]
            )
        if method == "arm.identity":
            return await self._arm_identity(parameters)
        if method == "observation.capture":
            return await self._capture(parameters)
        if method == "observation.revalidate":
            return await self._revalidate(parameters)
        if method == "vlm.invoke":
            return await self._invoke_vlm(parameters)
        if method == "visual_evidence.register":
            return await self._register_visual_evidence(parameters)
        if method == "assets.resolve_images":
            return self._resolve_assets(parameters)
        raise RuntimeError(f"unsupported translation-refinement RPC method: {method}")

    async def _arm_identity(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        observation = await self.fabric.latest_optional("robot_arm.model")
        if not isinstance(observation, dict):
            raise RuntimeError("current robot_arm.model observation is unavailable")
        data = observation.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("current robot_arm.model data is unavailable")
        return {
            "arm_provider_id": self._text(observation, "provider_id", "arm model"),
            "arm_provider_instance_id": self._text(
                observation,
                "provider_instance_id",
                "arm model",
            ),
            "arm_boot_id": self._text(observation, "boot_id", "arm model"),
            "arm_model_id": self._text(data, "model_id", "arm model data"),
            "arm_model_revision": self._text(
                data,
                "model_revision",
                "arm model data",
            ),
        }

    async def _capture(self, parameters: dict[str, Any]) -> dict[str, Any]:
        assert self._session_dir is not None
        capture_context_started_at_us = time.time_ns() // 1000
        world_frame = self._text(parameters, "world_frame", "capture request")
        arm_base_frame = self._text(
            parameters,
            "arm_base_frame",
            "capture request",
        )
        controlled_frame = self._text(
            parameters,
            "controlled_frame",
            "capture request",
        )
        context = await self.spatial.prepare_context(
            target_frame=world_frame,
            skill_id=f"arm-root-translation-refinement-{time.time_ns()}",
        )
        capture_context_ready_at_us = time.time_ns() // 1000
        frame = context.frame
        rgbd_timing = self._rgbd_timing(frame)
        joint_state = await self.fabric.latest_optional("robot_arm.joint_state")
        joint_state_read_at_us = time.time_ns() // 1000
        feedback_age = self._arm_feedback_age(joint_state)
        policy = self.profile["capture_motion_policy"]
        timing_margin_us = int(policy["additional_camera_timing_margin_us"])
        rgb_timestamp_us = int(rgbd_timing["rgb_timestamp_us"])
        depth_timestamp_us = int(rgbd_timing["registered_depth_timestamp_us"])
        feedback_age_us = int(feedback_age["age_us"])
        window_start_us = max(
            1,
            min(rgb_timestamp_us, depth_timestamp_us) - timing_margin_us,
        )
        window_end_us = (
            max(rgb_timestamp_us, depth_timestamp_us)
            + feedback_age_us
            + timing_margin_us
        )
        sample_count = int(policy["temporal_sample_count"])
        sample_times = self._ordered_sample_times(
            window_start_us,
            window_end_us,
            sample_count,
        )
        transforms_by_time: dict[int, dict[str, Any]] = {}
        try:
            transforms_by_time[window_end_us] = await self._bracketed_transform(
                from_frame=controlled_frame,
                to_frame=arm_base_frame,
                at_us=window_end_us,
                session_epoch=None,
                label="arm FK capture-window end",
            )
            for sample_time in sample_times:
                if sample_time not in transforms_by_time:
                    transforms_by_time[sample_time] = (
                        await self._bracketed_transform(
                            from_frame=controlled_frame,
                            to_frame=arm_base_frame,
                            at_us=sample_time,
                            session_epoch=None,
                            label="arm FK capture-window sample",
                        )
                    )
            fk_reference_timestamp_us = depth_timestamp_us + feedback_age_us
            base_from_tool = transforms_by_time.get(fk_reference_timestamp_us)
            if base_from_tool is None:
                base_from_tool = await self._bracketed_transform(
                    from_frame=controlled_frame,
                    to_frame=arm_base_frame,
                    at_us=fk_reference_timestamp_us,
                    session_epoch=None,
                    label="registered-depth FK reference",
                )
        except Exception as error:
            provider_id = (
                str(joint_state.get("provider_id") or "").strip()
                if isinstance(joint_state, dict)
                else ""
            )
            if not provider_id:
                try:
                    model = await self.fabric.latest_optional("robot_arm.model")
                except Exception:
                    model = None
                provider_id = (
                    str(model.get("provider_id") or "").strip()
                    if isinstance(model, dict)
                    else ""
                )
            raise ArmDataDependencyError(
                str(error),
                provider_id=provider_id or None,
            ) from error
        world_from_camera = await self._bracketed_transform(
            from_frame=str(frame.camera_frame),
            to_frame=world_frame,
            at_us=depth_timestamp_us,
            session_epoch=(
                str(frame.session_epoch) if frame.session_epoch else None
            ),
            label="registered-depth camera pose",
        )
        tracking_state = self._captured_tracking_state(frame)
        rgb_path = self._session_dir / "captured-rgb.npy"
        depth_path = self._session_dir / "captured-registered-depth.npy"
        np.save(rgb_path, np.asarray(frame.rgb), allow_pickle=False)
        np.save(depth_path, np.asarray(frame.depth_m), allow_pickle=False)
        self._context = context
        return {
            "coherent_snapshot": True,
            "tracking_state": tracking_state,
            "rgb_path": str(rgb_path),
            "registered_depth_path": str(depth_path),
            "intrinsics": dict(frame.intrinsics),
            "world_from_camera": self._transform_matrix(
                world_from_camera
            ).tolist(),
            "base_from_tool": self._transform_matrix(base_from_tool).tolist(),
            "temporal_alignment": {
                "policy_id": "TEMPORAL_FK_LANDMARK_MOTION_BOUND_V1",
                **rgbd_timing,
                "fk_reference_timestamp_us": fk_reference_timestamp_us,
                "capture_window_start_us": window_start_us,
                "capture_window_end_us": window_end_us,
                "additional_camera_timing_margin_us": timing_margin_us,
                "arm_feedback_age_us": feedback_age_us,
                "arm_feedback_age_source": feedback_age["source"],
                "arm_feedback_observation": {
                    key: value
                    for key, value in feedback_age.items()
                    if key not in {"age_us", "source"}
                },
                "capture_context_started_at_us": capture_context_started_at_us,
                "capture_context_ready_at_us": capture_context_ready_at_us,
                "joint_state_read_at_us": joint_state_read_at_us,
                "context_preparation_duration_us": (
                    capture_context_ready_at_us - capture_context_started_at_us
                ),
                "base_from_tool_samples": [
                    self._timestamped_fk_sample(
                        at_us=sample_time,
                        transform=transforms_by_time[sample_time],
                    )
                    for sample_time in sample_times
                ],
                "world_from_camera_temporal_provenance": (
                    self._transform_temporal_provenance(world_from_camera)
                ),
            },
            "runtime_landmark_bindings": copy.deepcopy(
                self.runtime_landmark_bindings
            ),
            "provenance": {
                "observed_at_us": int(frame.timestamp_us),
                "registered_depth_observed_at_us": depth_timestamp_us,
                "fk_reference_timestamp_us": fk_reference_timestamp_us,
                "frame_number": int(frame.frame_number),
                "rgb_sha256": hashlib.sha256(
                    np.asarray(frame.rgb).tobytes(order="C")
                ).hexdigest(),
                "registered_depth_sha256": hashlib.sha256(
                    np.asarray(frame.depth_m).tobytes(order="C")
                ).hexdigest(),
                "camera_frame": str(frame.camera_frame),
                "world_frame": world_frame,
                "controlled_frame": controlled_frame,
                "arm_base_frame": arm_base_frame,
            },
        }

    async def _revalidate(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        if self._context is None:
            raise RuntimeError("no captured spatial context is available")
        await self.spatial.revalidate_context_binding(self._context)
        vio = await self.fabric.latest_optional("localization.vio.status")
        vio_data = vio.get("data") if isinstance(vio, dict) else None
        tracking_state = (
            str(vio_data.get("tracking_state") or "UNKNOWN")
            if isinstance(vio_data, dict)
            else "UNKNOWN"
        )
        identities = await self._current_identities()
        return {
            "tracking_state": tracking_state,
            "identities": identities,
            "checked_at_us": time.time_ns() // 1000,
        }

    async def _current_identities(self) -> dict[str, Any]:
        catalog = await self.manager.workcell_calibrations()
        records = catalog.get("activations") if isinstance(catalog, dict) else None
        active = [
            record
            for record in (records or [])
            if isinstance(record, dict)
            and record.get("state") == "ACTIVE"
            and record.get("motion_usable") is True
            and record.get("enforcement") == "ENFORCED"
        ]
        if len(active) != 1:
            raise RuntimeError("active alignment changed during VLM inference")
        record = active[0]
        arm = await self._arm_identity({})
        return {
            "world_frame": self._text(record, "world_frame", "active alignment"),
            "vio_session_epoch": self._text(
                record,
                "session_epoch",
                "active alignment",
            ),
            "spatial_convention": self._text(
                record,
                "convention_id",
                "active alignment",
            ),
            "camera_provider_id": self._text(
                record,
                "camera_provider_id",
                "active alignment",
            ),
            "camera_provider_instance_id": self._text(
                record,
                "camera_provider_instance_id",
                "active alignment",
            ),
            "camera_boot_id": self._text(
                record,
                "camera_boot_id",
                "active alignment",
            ),
            "camera_calibration_revision": self._text(
                record,
                "camera_calibration_revision",
                "active alignment",
            ),
            **arm,
            "effector_profile_revision": self.profile["profile_revision"],
        }

    async def _invoke_vlm(self, parameters: dict[str, Any]) -> dict[str, Any]:
        images: list[tuple[bytes, str]] = []
        labels: list[str] = []
        for item in parameters.get("images") or []:
            path = self._session_path(item.get("path"))
            images.append((path.read_bytes(), str(item.get("media_type") or "")))
            labels.append(f"{item.get('id')}: {item.get('label')}")
        if not images:
            raise RuntimeError("VLM invocation contains no images")
        prompt = (
            "Image order and channel meaning:\n- "
            + "\n- ".join(labels)
            + "\n\n"
            + str(parameters.get("prompt") or "")
        )
        inference = await self.vlm_router.generate_images(
            images=images,
            prompt=prompt,
            request_id=str(parameters.get("request_id") or "") or None,
        )
        return {
            "text": str(inference.text),
            "route": inference.as_dict(),
        }

    async def _register_visual_evidence(
        self,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        channels: list[dict[str, Any]] = []
        for channel in parameters.get("channels") or []:
            path = self._session_path(channel.get("path"))
            channels.append(
                {
                    **{key: value for key, value in channel.items() if key != "path"},
                    "image_bytes": path.read_bytes(),
                }
            )
        return await self.visual_evidence_store.register_channels(
            **{
                **{
                    key: value
                    for key, value in parameters.items()
                    if key != "channels"
                },
                "channels": channels,
            }
        )

    def _resolve_assets(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        assert self._session_dir is not None
        results: list[dict[str, Any]] = []
        for index, asset_id in enumerate(parameters.get("asset_ids") or []):
            source = self.reference_assets.get(str(asset_id))
            if source is None or not source.is_file():
                raise RuntimeError(f"reference image asset is unavailable: {asset_id}")
            suffix = source.suffix.lower() or ".image"
            destination = self._session_dir / f"asset-{index:02d}{suffix}"
            shutil.copyfile(source, destination)
            with Image.open(destination) as image:
                width, height = image.size
                media_type = Image.MIME.get(image.format or "", "image/png")
            results.append(
                {
                    "id": f"reference_asset_{index}",
                    "label": str(asset_id),
                    "path": str(destination),
                    "media_type": media_type,
                    "width": int(width),
                    "height": int(height),
                }
            )
        return results

    def _arm_feedback_age(self, observation: Any) -> dict[str, Any]:
        policy = self.profile["capture_motion_policy"]
        fallback_ms = float(policy["fallback_arm_feedback_age_ms"])
        maximum_ms = float(policy["maximum_arm_feedback_age_ms"])
        preferred_observation_age_ms = float(
            policy["preferred_arm_feedback_observation_age_ms"]
        )
        evaluated_at_us = time.time_ns() // 1000
        if not isinstance(observation, dict):
            return {
                "age_us": int(round(maximum_ms * 1000.0)),
                "source": "PROFILE_CONSERVATIVE_MAXIMUM_NO_JOINT_STATE",
                "observed_at_us": None,
                "observation_age_us": None,
                "preferred_observation_age_us": int(
                    round(preferred_observation_age_ms * 1000.0)
                ),
                "fresh_for_feedback_age": False,
            }
        observed_at_us = observation.get("observed_at_us")
        if not isinstance(observed_at_us, int) or observed_at_us <= 0:
            return {
                "age_us": int(round(maximum_ms * 1000.0)),
                "source": (
                    "PROFILE_CONSERVATIVE_MAXIMUM_INVALID_JOINT_TIMESTAMP"
                ),
                "observed_at_us": None,
                "observation_age_us": None,
                "preferred_observation_age_us": int(
                    round(preferred_observation_age_ms * 1000.0)
                ),
                "fresh_for_feedback_age": False,
            }
        observation_age_us = max(0, evaluated_at_us - observed_at_us)
        preferred_observation_age_us = int(
            round(preferred_observation_age_ms * 1000.0)
        )
        if observation_age_us > preferred_observation_age_us:
            return {
                "age_us": int(round(maximum_ms * 1000.0)),
                "source": (
                    "PROFILE_CONSERVATIVE_MAXIMUM_STALE_JOINT_STATE"
                ),
                "observed_at_us": observed_at_us,
                "observation_age_us": observation_age_us,
                "preferred_observation_age_us": preferred_observation_age_us,
                "fresh_for_feedback_age": False,
            }
        timestamp_semantics = policy.get(
            "arm_transform_timestamp_semantics",
            "SNAPSHOT_TIME_WITH_FEEDBACK_AGE",
        )
        if timestamp_semantics == "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE":
            data = observation.get("data")
            timing = data.get("feedback_timing") if isinstance(data, dict) else None
            if not isinstance(timing, dict):
                raise RuntimeError(
                    "arm profile requires measured acquisition timestamps but joint-state timing metadata is unavailable"
                )
            if timing.get("timestamp_semantics") != timestamp_semantics:
                raise RuntimeError(
                    "arm joint-state timestamp semantics do not match the effector profile"
                )
            if timing.get("freshness_verified") is not True:
                raise RuntimeError("arm joint-state freshness is not verified")
            return {
                "age_us": 0,
                "source": "JOINT_STATE_TIMESTAMP_IS_MEASURED_ACQUISITION",
                "observed_at_us": observed_at_us,
                "observation_age_us": observation_age_us,
                "preferred_observation_age_us": preferred_observation_age_us,
                "fresh_for_feedback_age": True,
                "timestamp_uncertainty_us": timing.get("timestamp_uncertainty_us"),
            }
        value: Any = observation
        for field in policy["arm_feedback_age_field_path"]:
            if not isinstance(value, dict) or field not in value:
                return {
                    "age_us": int(round(fallback_ms * 1000.0)),
                    "source": "PROFILE_FALLBACK_FIELD_UNAVAILABLE",
                    "observed_at_us": observed_at_us,
                    "observation_age_us": observation_age_us,
                    "preferred_observation_age_us": (
                        preferred_observation_age_us
                    ),
                    "fresh_for_feedback_age": True,
                }
            value = value[field]
        try:
            feedback_age_ms = float(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError("arm feedback age is not numeric") from error
        if not math.isfinite(feedback_age_ms) or feedback_age_ms < 0.0:
            raise RuntimeError("arm feedback age is invalid")
        if feedback_age_ms > maximum_ms:
            raise RuntimeError("arm feedback age exceeds the profile limit")
        return {
            "age_us": int(round(feedback_age_ms * 1000.0)),
            "source": "JOINT_STATE_PROFILE_FIELD",
            "observed_at_us": observed_at_us,
            "observation_age_us": observation_age_us,
            "preferred_observation_age_us": preferred_observation_age_us,
            "fresh_for_feedback_age": True,
        }

    def _rgbd_timing(self, frame: Any) -> dict[str, int]:
        observations = (
            frame.observations if isinstance(frame.observations, dict) else {}
        )
        bundle = observations.get("bundle")
        data = bundle.get("data") if isinstance(bundle, dict) else None
        if not isinstance(data, dict) or data.get("synchronized") is not True:
            raise RuntimeError("capture requires a synchronized RGB-D bundle")
        rgb = data.get("rgb")
        depth = data.get("depth_aligned_to_rgb")
        if not isinstance(rgb, dict) or not isinstance(depth, dict):
            raise RuntimeError("captured RGB-D bundle is missing channel references")
        rgb_timestamp_us = self._reference_timestamp(rgb)
        depth_timestamp_us = self._reference_timestamp(depth)
        if rgb_timestamp_us <= 0 or depth_timestamp_us <= 0:
            raise RuntimeError("captured RGB-D channels have no usable timestamps")
        if int(frame.timestamp_us) != rgb_timestamp_us:
            raise RuntimeError("RGB frame timestamp does not match its copied reference")
        delta_us = depth_timestamp_us - rgb_timestamp_us
        maximum_delta_us = int(data.get("max_delta_us") or 0)
        if maximum_delta_us <= 0:
            raise RuntimeError("RGB-D bundle has no synchronization limit")
        if abs(delta_us) > maximum_delta_us:
            raise RuntimeError("RGB-D timestamp delta exceeds the bundle limit")
        return {
            "rgb_timestamp_us": rgb_timestamp_us,
            "registered_depth_timestamp_us": depth_timestamp_us,
            "rgb_depth_delta_us": delta_us,
            "rgb_depth_maximum_delta_us": maximum_delta_us,
        }

    @staticmethod
    def _reference_timestamp(reference: dict[str, Any]) -> int:
        return int(
            reference.get("global_timestamp_us")
            or reference.get("system_timestamp_us")
            or reference.get("device_timestamp_us")
            or 0
        )

    @staticmethod
    def _ordered_sample_times(
        start_us: int,
        end_us: int,
        count: int,
    ) -> list[int]:
        if start_us <= 0 or end_us < start_us or count < 3:
            raise RuntimeError("capture timing window is invalid")
        if start_us == end_us:
            raise RuntimeError("capture timing window has no duration")
        span = end_us - start_us
        times = [
            start_us + (span * index + (count - 1) // 2) // (count - 1)
            for index in range(count)
        ]
        ordered = sorted(set(times))
        if len(ordered) < 3:
            raise RuntimeError("capture timing window has too few unique samples")
        return ordered

    async def _bracketed_transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        session_epoch: str | None,
        label: str,
    ) -> dict[str, Any]:
        policy = self.profile["capture_motion_policy"]
        wait_s = float(policy["maximum_transform_wait_ms"]) / 1000.0
        retry_s = float(policy["transform_retry_interval_ms"]) / 1000.0
        deadline = time.monotonic() + wait_s
        last_reason = "transform is unavailable"
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                transform = await self.fabric.transform(
                    from_frame=from_frame,
                    to_frame=to_frame,
                    at_us=int(at_us),
                    max_extrapolation_us=750_000,
                    session_epoch=session_epoch,
                    wait_for_bracket_ms=max(0, int(remaining * 1000.0)),
                )
                self._validate_timestamped_transform(
                    transform,
                    from_frame=from_frame,
                    to_frame=to_frame,
                    at_us=at_us,
                )
                extrapolation_us = self._maximum_extrapolation_us(transform)
                if extrapolation_us == 0:
                    return transform
                last_reason = f"still extrapolated by {extrapolation_us} us"
            except Exception as error:
                last_reason = str(error)
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise RuntimeError(
                    f"{label} could not bracket timestamp {at_us}: {last_reason}"
                )
            await asyncio.sleep(min(retry_s, remaining))

    @classmethod
    def _validate_timestamped_transform(
        cls,
        transform: dict[str, Any],
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
    ) -> None:
        if not isinstance(transform, dict):
            raise RuntimeError("Fabric transform response is invalid")
        if str(transform.get("from_frame") or "") != from_frame:
            raise RuntimeError("Fabric transform source frame changed")
        if str(transform.get("to_frame") or "") != to_frame:
            raise RuntimeError("Fabric transform target frame changed")
        if int(transform.get("at_us") or 0) != int(at_us):
            raise RuntimeError("Fabric transform timestamp changed")
        path = transform.get("path")
        if not isinstance(path, list) or (from_frame != to_frame and not path):
            raise RuntimeError("Fabric transform has no path provenance")
        cls._transform_matrix(transform)

    @staticmethod
    def _maximum_extrapolation_us(transform: dict[str, Any]) -> int:
        path = transform.get("path")
        if not isinstance(path, list):
            raise RuntimeError("Fabric transform has no path provenance")
        values: list[int] = []
        for step in path:
            if not isinstance(step, dict):
                raise RuntimeError("Fabric transform path contains invalid metadata")
            value = int(step.get("extrapolated_by_us") or 0)
            if value < 0:
                raise RuntimeError("Fabric transform extrapolation is invalid")
            values.append(value)
        return max(values, default=0)

    @classmethod
    def _timestamped_fk_sample(
        cls,
        *,
        at_us: int,
        transform: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "at_us": int(at_us),
            "maximum_extrapolation_us": cls._maximum_extrapolation_us(
                transform
            ),
            "base_from_tool": cls._transform_matrix(transform).tolist(),
        }

    @classmethod
    def _transform_temporal_provenance(
        cls,
        transform: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "at_us": int(transform.get("at_us") or 0),
            "maximum_extrapolation_us": cls._maximum_extrapolation_us(
                transform
            ),
            "path": copy.deepcopy(transform.get("path") or []),
        }

    @staticmethod
    def _captured_tracking_state(frame: Any) -> str:
        observations = frame.observations if isinstance(frame.observations, dict) else {}
        vio = observations.get("vio_status")
        data = vio.get("data") if isinstance(vio, dict) else None
        return (
            str(data.get("tracking_state") or "UNKNOWN")
            if isinstance(data, dict)
            else "UNKNOWN"
        )

    @staticmethod
    def _transform_matrix(value: dict[str, Any]) -> np.ndarray:
        translation = np.asarray(value.get("translation_m"), dtype=np.float64)
        quaternion = np.asarray(value.get("rotation_xyzw"), dtype=np.float64)
        if translation.shape != (3,) or quaternion.shape != (4,):
            raise RuntimeError("Fabric transform has invalid geometry")
        norm = float(np.linalg.norm(quaternion))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise RuntimeError("Fabric transform quaternion is invalid")
        x, y, z, w = quaternion / norm
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] = [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
        matrix[:3, 3] = translation
        return matrix

    def _session_path(self, value: Any) -> Path:
        if self._session_dir is None:
            raise RuntimeError("translation-refinement session is unavailable")
        path = Path(str(value or "")).resolve()
        if self._session_dir.resolve() not in path.parents:
            raise RuntimeError("Skill RPC path escaped the session directory")
        if not path.is_file():
            raise RuntimeError("Skill RPC file is unavailable")
        return path

    def _read_profile(self) -> dict[str, Any]:
        if not self.profile_path.is_file():
            raise RuntimeError(f"effector profile is unavailable: {self.profile_path}")
        value = json.loads(self.profile_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("effector profile must be an object")
        return value

    @staticmethod
    def _text(source: dict[str, Any], field: str, scope: str) -> str:
        value = source.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{scope} {field} is missing")
        return value.strip()


def create_host_adapter(
    *,
    skill_root: Path,
    manifest: dict[str, Any],
    services: Any,
) -> ArmRootTranslationRefinementAdapter:
    """Create the Skill-owned bridge against neutral platform services."""

    profiles = manifest.get("effector_profiles")
    if not isinstance(profiles, list) or len(profiles) != 1:
        raise RuntimeError(
            "translation-refinement manifest must declare exactly one default "
            "effector profile"
        )
    profiles_root = (Path(skill_root) / "profiles").resolve()
    profile_path = (Path(skill_root) / str(profiles[0])).resolve()
    if profiles_root not in profile_path.parents:
        raise RuntimeError(
            "translation-refinement effector profile must remain inside the "
            "Skill profiles directory"
        )
    return ArmRootTranslationRefinementAdapter(
        skill_root=skill_root,
        profile_path=profile_path,
        manager=services.manager,
        fabric=services.fabric,
        spatial=services.spatial,
        vlm_router=services.vlm_router,
        visual_evidence_store=services.visual_evidence_store,
    )

    @staticmethod
    async def _stderr(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        data = await process.stderr.read()
        return data.decode("utf-8", errors="replace").strip()
