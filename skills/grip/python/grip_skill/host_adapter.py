from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import importlib.util
import json
import math
import sys
import uuid


SKILL_ID = "grip.grip"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _finite(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def measured_hold_step(contact_state: dict[str, Any]) -> dict[str, Any]:
    pose = contact_state.get("measured_acting_frame_pose")
    if not isinstance(pose, dict):
        raise RuntimeError("Contact measured acting-frame pose is unavailable")
    orientation = pose.get("orientation_xyzw")
    if not isinstance(orientation, list) or len(orientation) != 4:
        raise RuntimeError("Contact measured acting-frame orientation is unavailable")
    values = [_finite(value, "measured acting-frame orientation") for value in orientation]
    norm = math.sqrt(sum(value * value for value in values))
    if norm < 1e-8:
        raise RuntimeError("Contact measured acting-frame orientation is invalid")
    return {
        "position_m": [0.0, 0.0, 0.0],
        "orientation_xyzw": [value / norm for value in values],
    }


class GripHostAdapter:
    def __init__(
        self,
        *,
        manager: Any,
        integrated_motion: Any,
        contact_url: str,
        grip_url: str,
        profiles_path: Path,
        effector_path: Path,
    ) -> None:
        self.manager = manager
        if integrated_motion is None:
            raise RuntimeError("generic grip requires the bound Integrated motion adapter")
        self.integrated_motion = integrated_motion
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.profiles_path = profiles_path
        self.effector_path = effector_path
        self.lock = asyncio.Lock()

    def _profile(self, requested: Any) -> dict[str, Any]:
        document = _load(self.profiles_path)
        number = document["default_profile_number"] if requested is None else int(requested)
        profile = next(
            (
                value
                for value in document["profiles"]
                if int(value["profile_number"]) == number
            ),
            None,
        )
        if not isinstance(profile, dict):
            raise ValueError("requested generic grip motion profile is unavailable")
        return profile

    async def _cleanup(
        self,
        *,
        grip: Any,
        contact: Any,
        session_id: str,
        execution_id: str,
    ) -> dict[str, Any]:
        joint = _load(self.effector_path)["joint_control"]
        cleanup: dict[str, Any] = {"errors": []}
        try:
            cleanup["gripper_release"] = await asyncio.to_thread(
                grip.open_and_float,
                skill_id=SKILL_ID,
                execution_id=execution_id,
                position_rad=float(joint["open_position_rad"]),
                velocity_limit_rad_s=float(joint["default_velocity_rad_s"]),
                torque_limit_nm=float(joint["release_torque_limit_nm"]),
                position_tolerance_rad=float(joint["open_position_tolerance_rad"]),
                open_timeout_s=7.0,
                mit_delta_time_s=0.5,
            )
        except Exception as exc:
            cleanup["errors"].append(f"gripper release: {exc}")
        try:
            cleanup["contact_relax"] = await asyncio.to_thread(
                contact.relax,
                session_id,
                "generic grip failed before carry confirmation",
            )
        except Exception as exc:
            cleanup["errors"].append(f"Contact relax: {exc}")
        return cleanup

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from grip_work_runtime import (
            ContactCarryRuntime,
            GripRuntime,
            HttpStatusError,
            contact_step,
            failed_grip_result,
            handoff_idle_integrated_to_contact,
        )

        async with self.lock:
            await self.manager.set_hot("robot_arm.primary.grip")
            grip = GripRuntime(
                self.grip_url,
                signing_secret_env="MIDBRAIN_GRIP_GENERIC_SECRET",
            )
            grip_preflight = await asyncio.to_thread(grip.state)
            if isinstance(grip_preflight.get("carry"), dict):
                raise RuntimeError("generic grip cannot replace an existing confirmed carry")
            thermal = grip_preflight.get("thermal")
            if not isinstance(thermal, dict) or thermal.get("ready_for_new_grip") is not True:
                return {
                    "status": "WAIT_FOR_GRIP_TEMPERATURE",
                    "workflow_complete": False,
                    "physical_motion_requested": False,
                    "retry_after_s": (
                        thermal.get("retry_after_s", 60.0)
                        if isinstance(thermal, dict)
                        else 60.0
                    ),
                    "thermal": thermal,
                    "task_success_assessed": False,
                }

            await self.manager.set_hot("robot_arm.primary.integrated")
            controller_handoff = await handoff_idle_integrated_to_contact(
                self.manager,
                self.integrated_motion,
                operation_label="Generic Grip",
            )

            contact = ContactCarryRuntime(
                self.contact_url,
                self.manager.base_url,
                signing_secret_env="MIDBRAIN_CONTACT_GRIP_GENERIC_SECRET",
            )
            hold = measured_hold_step(await asyncio.to_thread(contact.state))
            profile = self._profile(arguments.get("motion_profile_number"))
            position = _finite(
                profile["grip_position_rad"]
                if arguments.get("grip_position_rad") is None
                else arguments["grip_position_rad"],
                "grip_position_rad",
            )
            velocity = _finite(
                profile["grip_velocity_rad_s"]
                if arguments.get("grip_velocity_rad_s") is None
                else arguments["grip_velocity_rad_s"],
                "grip_velocity_rad_s",
            )
            torque = _finite(
                profile["gripping_torque_limit_nm"]
                if arguments.get("gripping_torque_limit_nm") is None
                else arguments["gripping_torque_limit_nm"],
                "gripping_torque_limit_nm",
            )
            timeout_s = _finite(
                profile["contact_timeout_s"]
                if arguments.get("contact_timeout_s") is None
                else arguments["contact_timeout_s"],
                "contact_timeout_s",
            )
            if velocity <= 0.0 or torque <= 0.0 or not 0.1 <= timeout_s <= 30.0:
                raise ValueError(
                    "generic grip velocity, torque, or timeout is outside its finite bounds"
                )
            carry_id = str(uuid.uuid4())
            attachment_revision = str(uuid.uuid4())
            contact_result = await asyncio.to_thread(
                contact.execute,
                skill_id=SKILL_ID,
                steps=[
                    contact_step(
                        position_m=hold["position_m"],
                        orientation_xyzw=hold["orientation_xyzw"],
                        next_command_timeout_s=max(12.0, timeout_s + 7.0),
                    )
                ],
                carry_id=carry_id,
                attachment_revision=attachment_revision,
                behavior="PREPARE",
            )

            execution_id = str(uuid.uuid4())
            try:
                await asyncio.to_thread(
                    grip.command,
                    skill_id=SKILL_ID,
                    execution_id=execution_id,
                    operation="SET_POSITION_EFFORT",
                    intent="GRIP",
                    position_rad=position,
                    velocity_limit_rad_s=velocity,
                    torque_limit_nm=torque,
                )
                inferred = await asyncio.to_thread(
                    grip.wait_for,
                    lambda state: state.get("contact_inferred") is True,
                    timeout_s=timeout_s,
                    description="stable gripper contact inference",
                )
                await asyncio.to_thread(
                    contact.confirm_carry,
                    contact_result["session_id"],
                    carry_id,
                    attachment_revision,
                )
                confirmed = await asyncio.to_thread(
                    grip.command,
                    skill_id=SKILL_ID,
                    execution_id=execution_id,
                    operation="CONFIRM_CARRY",
                    carry_id=carry_id,
                    attachment_revision=attachment_revision,
                    attachment={
                        "object_binding": arguments["object_binding"],
                        "payload": arguments.get("payload"),
                        "collision_geometry": arguments.get("collision_geometry"),
                    },
                )
                final_state = await asyncio.to_thread(grip.state)
            except HttpStatusError as error:
                await self._cleanup(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                )
                if error.payload.get("error_code") == "THERMAL_GATE":
                    return {
                        "status": "WAIT_FOR_GRIP_TEMPERATURE",
                        "workflow_complete": False,
                        "physical_motion_requested": True,
                        "retry_after_s": error.payload.get("retry_after_s", 60.0),
                        "thermal": error.payload,
                        "task_success_assessed": False,
                    }
                raise
            except TimeoutError as error:
                cleanup = await self._cleanup(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                )
                return failed_grip_result(
                    target_position_rad=position,
                    failure=error,
                    cleanup=cleanup,
                )
            except Exception:
                await self._cleanup(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                )
                raise

            if final_state.get("all_active_joints_position_effort_limited") is not True:
                raise RuntimeError("generic grip carry confirmation did not lock every active joint")
            return {
                "status": "CARRYING_CURRENT_POSE_LOCKED",
                "workflow_complete": True,
                "carry_id": carry_id,
                "attachment_revision": attachment_revision,
                "all_joints_position_effort_limited": True,
                "contact": contact_result,
                "grip_contact": inferred,
                "grip_confirmation": confirmed,
                "grip_state": final_state,
                "controller_handoff": controller_handoff,
                "task_success_assessed": False,
            }


def _load_host_bridge(skill_root: Path) -> Any:
    module_name = "midbrain_grip_work_host_bridge"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    workspace = skill_root.resolve().parents[1]
    bridge_path = workspace / "skills/grip_work_runtime/host_bridge.py"
    specification = importlib.util.spec_from_file_location(module_name, bridge_path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load Grip Skill host bridge: {bridge_path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def build_host_adapter(*, skill_root: Path, manifest: dict[str, Any], services: Any) -> Any:
    bridge = _load_host_bridge(skill_root)
    return bridge.build_private_adapter(
        skill_root=skill_root,
        worker_entrypoint=Path(__file__),
        services=services,
    )


def _build_private_workflow(
    skill_root: Path,
    context: dict[str, Any],
    services: Any,
) -> GripHostAdapter:
    workspace = skill_root.resolve().parents[1]
    assembly = _load(workspace / "config/robot_assemblies/primary_manipulator.json")
    provider_root = workspace / assembly["arm_provider"]["provider_root"]
    return GripHostAdapter(
        manager=services.manager,
        integrated_motion=services.integrated_motion,
        contact_url=str(context["contact_url"]),
        grip_url=str(context["grip_url"]),
        profiles_path=(skill_root / "config/motion_profiles.json").resolve(),
        effector_path=(
            provider_root
            / "profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ).resolve(),
    )


if __name__ == "__main__":
    if "--private-worker" not in sys.argv:
        raise SystemExit("generic grip host adapter is only a private worker entrypoint")
    from grip_work_runtime.private_worker import run_private_worker

    run_private_worker(_build_private_workflow)
