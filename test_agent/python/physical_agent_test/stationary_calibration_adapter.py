from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image

from stationary_world_arm_alignment.candidate_review import canonical_sha256
from stationary_world_arm_alignment.math3d import YawUnobservableError

from .phase4_policy import (
    extend_current_operation_hard_timeout,
    report_operation_progress,
)


FOUNDATIONPOSE_CANONICAL_INVOCATION = (
    "Use FoundationPose to establish the stationary world-to-arm-base "
    "transform."
)
FOUNDATIONPOSE_CANONICAL_NAME = "foundationpose"


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1]
                    + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def mentions_foundation_pose(request: str) -> bool:
    words = re.findall(r"[a-z0-9]+", str(request).casefold())
    for start in range(len(words)):
        for word_count in (1, 2, 3):
            candidate = "".join(words[start : start + word_count])
            if not candidate:
                continue
            if abs(len(candidate) - len(FOUNDATIONPOSE_CANONICAL_NAME)) > 2:
                continue
            if _edit_distance(candidate, FOUNDATIONPOSE_CANONICAL_NAME) <= 2:
                return True
    return False


class StationaryCalibrationRuntime(Protocol):
    async def run(
        self,
        mode: Any,
        *,
        arm_is_home: bool = False,
        allow_active_control_interrupt: bool = False,
    ) -> dict[str, Any]: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class StationaryCalibrationActivator(Protocol):
    async def review_and_activate(
        self,
        *,
        alignment_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]: ...


class VisualEvidenceRegistrar(Protocol):
    async def register_channels(
        self,
        *,
        channels: list[dict[str, Any]],
        default_channel: str,
        title: str,
        annotations: list[dict[str, Any]],
        confidence: str,
        model: str,
        source_skill: str,
    ) -> dict[str, Any]: ...


def _visual_artifact_directory(
    result: dict[str, Any],
    *,
    run_root: Path,
) -> Path | None:
    alignment_id = str(result.get("alignment_id") or "")
    if re.fullmatch(r"[0-9A-Za-z-]+", alignment_id) is None:
        return None
    root = run_root.resolve()
    expected_run_dir = (root / alignment_id).resolve()
    if expected_run_dir.parent != root:
        return None
    diagnostics = result.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    validations = diagnostics.get("foundation_pose_validation")
    if not isinstance(validations, list):
        return None
    for validation in reversed(validations):
        if not isinstance(validation, dict):
            continue
        overlay = validation.get("overlay")
        if not isinstance(overlay, dict):
            continue
        local_path = overlay.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            continue
        candidate = Path(local_path).resolve()
        if candidate.is_file() and candidate.parent == expected_run_dir:
            return candidate.parent
    return None


def _visual_channel(
    *,
    channel_id: str,
    label: str,
    path: Path,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
    media_type = "image/png" if path.suffix.casefold() == ".png" else "image/jpeg"
    return {
        "id": channel_id,
        "label": label,
        "image_bytes": payload,
        "media_type": media_type,
        "width": width,
        "height": height,
    }


def _visual_channels(
    result: dict[str, Any],
    *,
    run_root: Path,
) -> list[dict[str, Any]]:
    run_dir = _visual_artifact_directory(result, run_root=run_root)
    if run_dir is None:
        return []
    selected_overlays = sorted(
        run_dir.glob("foundation_pose_attempt_*_selected_overlay.jpg")
    )
    candidates = [
        (
            "pose_overlay",
            "FoundationPose Alignment",
            selected_overlays[-1] if selected_overlays else None,
        ),
        ("vlm_overlay", "VLM Localization", run_dir / "overlay.jpg"),
        ("rgb", "RGB", run_dir / "camera.jpg"),
        ("depth", "Depth", run_dir / "depth.png"),
    ]
    channels: list[dict[str, Any]] = []
    for channel_id, label, path in candidates:
        if path is None:
            continue
        channel = _visual_channel(
            channel_id=channel_id,
            label=label,
            path=path,
        )
        if channel is not None:
            channels.append(channel)
    return channels


class StationaryCalibrationSkillAdapter:
    """Deferred, finite wrapper around the maintained calibration Skill."""

    def __init__(
        self,
        runtime_factory: Callable[[], StationaryCalibrationRuntime],
        *,
        operation_hard_timeout_s: float = 600.0,
        activation_service: StationaryCalibrationActivator | None = None,
        visual_evidence_store: VisualEvidenceRegistrar | None = None,
        artifact_run_root: Path | None = None,
    ):
        self.runtime_factory = runtime_factory
        self.operation_hard_timeout_s = float(operation_hard_timeout_s)
        self.activation_service = activation_service
        self.visual_evidence_store = visual_evidence_store
        self.artifact_run_root = (
            artifact_run_root.resolve() if artifact_run_root is not None else None
        )
        if self.operation_hard_timeout_s <= 0.0:
            raise ValueError(
                "stationary calibration hard timeout must be positive"
            )
        self._runtime: StationaryCalibrationRuntime | None = None
        self._lock = asyncio.Lock()
        self.last_result: dict[str, Any] | None = None

    async def run(self, *, request: str) -> dict[str, Any]:
        user_request = str(request).strip()
        if not user_request:
            raise ValueError("request must be non-empty")
        if not mentions_foundation_pose(user_request):
            result = {
                "status": "FOUNDATIONPOSE_EXPLICIT_INVOCATION_REQUIRED",
                "workflow_complete": False,
                "motion_usable": False,
                "reason_code": "FOUNDATIONPOSE_NOT_EXPLICITLY_REQUESTED",
                "message": (
                    "FoundationPose was not started. The regular Agent route "
                    "may invoke this long-running initializer only when the "
                    "operator explicitly names FoundationPose. Case, spacing, "
                    "hyphenation, and minor spelling errors are accepted. Ordinary "
                    "world-to-arm alignment is reserved for the movement-"
                    "based gripper alignment workflow."
                ),
                "required_name_mention": "FoundationPose",
                "canonical_example": FOUNDATIONPOSE_CANONICAL_INVOCATION,
                "physical_motion_submitted": False,
                "agent_request": user_request,
                "agent_adapter": {
                    "adapter_id": (
                        "skill.stationary_world_arm_alignment.cli.v1"
                    ),
                    "execution": "NOT_STARTED_EXPLICIT_INVOCATION_REQUIRED",
                    "mode": None,
                    "arm_is_home_claimed": False,
                    "active_control_interrupt_allowed": False,
                    "physical_motion_submitted_by_adapter": False,
                    "foundationpose_name_match": False,
                },
            }
            self.last_result = result
            return result
        if self._lock.locked():
            raise RuntimeError("stationary calibration is already running")
        async with self._lock:
            extend_current_operation_hard_timeout(
                self.operation_hard_timeout_s,
                stage="STATIONARY_CALIBRATION_EXTENDED_DEADLINE",
            )
            report_operation_progress("LOAD_STATIONARY_CALIBRATION_SKILL")
            runtime = self.runtime_factory()
            self._runtime = runtime
            try:
                report_operation_progress("RUN_STATIONARY_CALIBRATION_AUTO")
                result = await runtime.run(
                    "auto",
                    arm_is_home=False,
                    allow_active_control_interrupt=False,
                )
            except YawUnobservableError as error:
                result = {
                    "status": "CALIBRATION_POSE_REQUIRED",
                    "workflow_complete": False,
                    "motion_usable": False,
                    "reason_code": "BASE_YAW_UNOBSERVABLE",
                    "message": str(error),
                    "diagnostics": dict(error.diagnostics),
                    "required_operator_action": (
                        "Move the end effector sideways until its horizontal "
                        "distance from the base Z axis exceeds the reported "
                        "minimum, keep the camera/base rig stationary, and "
                        "retry the same calibration request."
                    ),
                }
            except asyncio.CancelledError:
                report_operation_progress("CANCEL_STATIONARY_CALIBRATION")
                await runtime.cancel()
                raise
            finally:
                self._runtime = None
                report_operation_progress("CLOSE_STATIONARY_CALIBRATION_SKILL")
                await runtime.close()
            if not isinstance(result, dict):
                raise RuntimeError(
                    "stationary calibration returned a non-object result"
                )
            wrapped = {
                **result,
                "agent_request": user_request,
                "agent_adapter": {
                    "adapter_id": (
                        "skill.stationary_world_arm_alignment.cli.v1"
                    ),
                    "execution": "IN_PROCESS_DEFERRED_FINITE",
                    "mode": "auto",
                    "arm_is_home_claimed": False,
                    "active_control_interrupt_allowed": False,
                    "physical_motion_submitted_by_adapter": False,
                    "foundationpose_name_match": True,
                },
            }
            candidate = wrapped.get("candidate")
            alignment_id = str(wrapped.get("alignment_id") or "")
            if isinstance(candidate, dict) and alignment_id:
                candidate_digest = canonical_sha256(candidate)
                wrapped.update(
                    {
                        "candidate_sha256": candidate_digest,
                        "workflow_complete": False,
                        "required_next_tool": {
                            "name": (
                                "review_and_activate_stationary_calibration"
                            ),
                            "arguments": {
                                "alignment_id": alignment_id,
                                "candidate_sha256": candidate_digest,
                            },
                        },
                        "agent_instruction": (
                            "This candidate is not yet motion-usable. Call "
                            "required_next_tool immediately with unchanged "
                            "arguments. Do not report the world-to-arm "
                            "relationship as established until that tool "
                            "returns motion_usable=true."
                        ),
                    }
                )
            if (
                self.visual_evidence_store is not None
                and self.artifact_run_root is not None
            ):
                try:
                    channels = _visual_channels(
                        wrapped,
                        run_root=self.artifact_run_root,
                    )
                    if channels:
                        channel_ids = {item["id"] for item in channels}
                        default_channel = (
                            "pose_overlay"
                            if "pose_overlay" in channel_ids
                            else channels[0]["id"]
                        )
                        wrapped["visual_evidence"] = (
                            await self.visual_evidence_store.register_channels(
                                channels=channels,
                                default_channel=default_channel,
                                title=(
                                    "Stationary world-to-arm alignment "
                                    f"{alignment_id}"
                                ),
                                annotations=[],
                                confidence=(
                                    "high" if wrapped.get("valid") is True else "review"
                                ),
                                model="FoundationPose + VLM",
                                source_skill="calibrate_stationary_workcell",
                            )
                        )
                        wrapped["agent_adapter"]["visual_evidence_status"] = (
                            "REGISTERED"
                        )
                    else:
                        wrapped["agent_adapter"]["visual_evidence_status"] = (
                            "NO_PERSISTED_ARTIFACTS"
                        )
                except Exception as error:
                    wrapped["agent_adapter"]["visual_evidence_status"] = (
                        "REGISTRATION_FAILED"
                    )
                    wrapped["agent_adapter"]["visual_evidence_error_type"] = (
                        type(error).__name__
                    )
            self.last_result = wrapped
            return wrapped

    async def review_and_activate(
        self,
        *,
        alignment_id: str,
        candidate_sha256: str,
    ) -> dict[str, Any]:
        if self.activation_service is None:
            raise RuntimeError(
                "stationary calibration activation is not configured"
            )
        return await self.activation_service.review_and_activate(
            alignment_id=alignment_id,
            candidate_sha256=candidate_sha256,
        )

    def latest_activation_continuation(self) -> dict[str, Any] | None:
        service = self.activation_service
        if service is None or not hasattr(
            service,
            "latest_activation_continuation",
        ):
            return None
        return service.latest_activation_continuation()

    async def cancel(self) -> None:
        runtime = self._runtime
        if runtime is not None:
            await runtime.cancel()
