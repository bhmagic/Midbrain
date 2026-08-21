from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import importlib.util
import json
import sys
import uuid


SKILL_ID = "grip.grip_object"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _active_activation(document: dict[str, Any]) -> dict[str, Any]:
    values = [
        value
        for value in document.get("activations", [])
        if isinstance(value, dict)
        and value.get("state") == "ACTIVE"
        and value.get("motion_usable") is True
        and value.get("expires_at") is None
        and value.get("expires_at_us") is None
    ]
    if len(values) != 1:
        raise RuntimeError("grip requires exactly one active motion-usable calibration")
    return values[0]


class GripObjectHostAdapter:
    def __init__(self, *, manager: Any, integrated_motion: Any, contact_url: str, grip_url: str, effector_path: Path, profiles_path: Path, vector_profiles_path: Path):
        if integrated_motion is None:
            raise RuntimeError("grip-object requires the bound Integrated motion adapter")
        self.manager = manager
        self.integrated_motion = integrated_motion
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.effector_path = effector_path
        self.profiles_path = profiles_path
        self.vector_profiles_path = vector_profiles_path
        self.lock = asyncio.Lock()

    async def _cleanup_failed_grip(
        self,
        *,
        grip: GripRuntime,
        contact: ContactCarryRuntime,
        session_id: str,
        execution_id: str,
        release: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        cleanup: dict[str, Any] = {"errors": []}
        try:
            cleanup["gripper_release"] = await asyncio.to_thread(
                grip.open_and_float,
                skill_id=SKILL_ID,
                execution_id=execution_id,
                position_rad=release["position_rad"],
                velocity_limit_rad_s=release["velocity_limit_rad_s"],
                torque_limit_nm=release["torque_limit_nm"],
                position_tolerance_rad=release["position_tolerance_rad"],
                open_timeout_s=7.0,
                mit_delta_time_s=release["mit_delta_time_s"],
            )
        except Exception as exc:
            cleanup["errors"].append(f"gripper release: {exc}")
        try:
            cleanup["contact_relax"] = await asyncio.to_thread(
                contact.relax, session_id, reason
            )
        except Exception as exc:
            cleanup["errors"].append(f"Contact relax: {exc}")
        return cleanup

    def _profile(self, requested: Any) -> dict[str, Any]:
        document = _load(self.profiles_path)
        number = document["default_profile_number"] if requested is None else int(requested)
        result = next((value for value in document["profiles"] if int(value["profile_number"]) == number), None)
        if not isinstance(result, dict):
            raise ValueError("requested grip motion profile is unavailable")
        return result

    def _vector_profile(self, requested: Any) -> dict[str, Any]:
        document = _load(self.vector_profiles_path)
        number = document["default_profile_number"] if requested is None else int(requested)
        result = next((value for value in document["profiles"] if int(value["profile_number"]) == number), None)
        if not isinstance(result, dict):
            raise ValueError("requested scrap-grip gripper vector profile is unavailable")
        return result

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from grip_object_skill.skill import build_plan
        from grip_work_runtime import (
            ContactCarryRuntime,
            GripRuntime,
            HttpStatusError,
            activation_binding,
            contact_step,
            execute_rotation_and_capture,
            failed_grip_result,
            handoff_to_contact,
            measured_controlled_position,
            normalize_point_mode,
            prepare_rotation_only,
            require_current_calibration,
            resolve_approach_point,
        )

        async with self.lock:
            await self.manager.set_hot("robot_arm.primary.grip")
            grip = GripRuntime(
                self.grip_url,
                signing_secret_env="MIDBRAIN_GRIP_OBJECT_SECRET",
            )
            grip_preflight = await asyncio.to_thread(grip.state)
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
            activation = _active_activation(await self.manager.workcell_calibrations())
            workcell_binding = activation_binding(activation)
            point_mode = normalize_point_mode(
                arguments.get("point_mode") or "ABSOLUTE_WORLD"
            )
            current_effector_base_m = None
            if point_mode == "RELATIVE_TO_CURRENT_EFFECTOR_WORLD":
                current_effector_base_m = measured_controlled_position(
                    await self.integrated_motion.observation()
                )
            point_resolution = resolve_approach_point(
                arguments["approach_begin_point_world_m"],
                point_mode=point_mode,
                world_from_base=activation["transforms"]["world_from_base"],
                current_effector_base_m=current_effector_base_m,
            )
            resolved_arguments = {
                **arguments,
                "approach_begin_point_world_m": point_resolution[
                    "resolved_approach_begin_point_world_m"
                ],
            }
            plan = build_plan(
                resolved_arguments,
                effector_profile=_load(self.effector_path),
                gripper_vector_profile=self._vector_profile(
                    arguments.get("gripper_vector_profile_number")
                ),
                motion_profile=self._profile(arguments.get("motion_profile_number")),
                world_from_base=activation["transforms"]["world_from_base"],
            )
            plan["approach_point_resolution"] = point_resolution
            prepared_alignment = await prepare_rotation_only(
                self.integrated_motion,
                plan["target_orientation_rpy_rad"],
                operation_label="Scrap Grip",
            )
            await require_current_calibration(
                self.manager,
                workcell_binding,
                operation_label="Scrap Grip",
            )
            grip_stage_one_state = await asyncio.to_thread(grip.state)
            stage_one_thermal = grip_stage_one_state.get("thermal")
            if (
                not isinstance(stage_one_thermal, dict)
                or stage_one_thermal.get("ready_for_new_grip") is not True
            ):
                return {
                    "status": "WAIT_FOR_GRIP_TEMPERATURE",
                    "workflow_complete": False,
                    "physical_motion_requested": False,
                    "retry_after_s": (
                        stage_one_thermal.get("retry_after_s", 60.0)
                        if isinstance(stage_one_thermal, dict)
                        else 60.0
                    ),
                    "thermal": stage_one_thermal,
                    "task_success_assessed": False,
                }
            opening_execution_id = str(uuid.uuid4())
            opening = await asyncio.to_thread(
                grip.command,
                skill_id=SKILL_ID,
                execution_id=opening_execution_id,
                operation="SET_MIT_POSITION",
                intent="OPEN",
                position_rad=plan["approach_open"]["position_rad"],
                duration_s=plan["approach_open"]["duration_s"],
                kp=plan["approach_open"]["kp"],
                kd=plan["approach_open"]["kd"],
            )
            try:
                alignment = await execute_rotation_and_capture(
                    self.integrated_motion,
                    prepared_alignment["preview_id"],
                    operation_label="Scrap Grip",
                )
                await require_current_calibration(
                    self.manager,
                    workcell_binding,
                    operation_label="Scrap Grip",
                )
                controller_handoff = await handoff_to_contact(
                    self.manager,
                    self.integrated_motion,
                    alignment,
                    operation_label="Scrap Grip",
                )
                approach_ready = await asyncio.to_thread(
                    grip.wait_for,
                    lambda state: state.get("ready_for_approach") is True,
                    timeout_s=7.0,
                    description="thermally ready, functionally open gripper MIT hold",
                )
            except Exception:
                try:
                    await asyncio.to_thread(
                        grip.command,
                        skill_id=SKILL_ID,
                        execution_id=opening_execution_id,
                        operation="ENTER_MIT_FLOAT",
                        delta_time_s=plan["release"]["mit_delta_time_s"],
                    )
                except Exception:
                    pass
                raise
            carry_id = str(uuid.uuid4())
            attachment_revision = str(uuid.uuid4())
            contact = ContactCarryRuntime(
                self.contact_url,
                self.manager.base_url,
                signing_secret_env="MIDBRAIN_CONTACT_GRIP_OBJECT_SECRET",
            )
            contact_result = await asyncio.to_thread(
                contact.execute,
                skill_id=SKILL_ID,
                steps=[
                    contact_step(
                        position_m=[
                            plan["approach_begin_point_base_m"][index]
                            + plan["table_delta_base_m"][index]
                            for index in range(3)
                        ],
                        orientation_xyzw=plan["orientation_xyzw"],
                        position_mode="ABSOLUTE_ROOT",
                        delay_after_accept_s=plan["stage_waits_s"]["lower"],
                        next_command_timeout_s=max(
                            6.0,
                            plan["stage_waits_s"]["lower"] + 1.0,
                        ),
                    ),
                    contact_step(
                        position_m=plan["insertion_delta_base_m"],
                        orientation_xyzw=plan["orientation_xyzw"],
                        delay_after_accept_s=plan["stage_waits_s"]["scrap"],
                        next_command_timeout_s=max(
                            20.0,
                            plan["stage_waits_s"]["scrap"]
                            + plan["grip"]["contact_timeout_s"]
                            + plan["stage_waits_s"]["grip"]
                            + 7.0,
                        ),
                    ),
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
                    position_rad=plan["grip"]["position_rad"],
                    velocity_limit_rad_s=plan["grip"]["velocity_limit_rad_s"],
                    torque_limit_nm=plan["grip"]["torque_limit_nm"],
                )
                inferred = await asyncio.to_thread(
                    grip.wait_for,
                    lambda state: state.get("contact_inferred") is True,
                    timeout_s=plan["grip"]["contact_timeout_s"],
                    description="stable gripper contact inference",
                )
                await asyncio.sleep(plan["stage_waits_s"]["grip"])
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
                    attachment=plan["attachment"],
                )
            except HttpStatusError as exc:
                await self._cleanup_failed_grip(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                    release=plan["release"],
                    reason="grip command failed before carry transfer",
                )
                if exc.payload.get("error_code") == "THERMAL_GATE":
                    return {
                        "status": "WAIT_FOR_GRIP_TEMPERATURE",
                        "workflow_complete": False,
                        "physical_motion_requested": True,
                        "retry_after_s": exc.payload.get("retry_after_s", 60.0),
                        "thermal": exc.payload,
                        "task_success_assessed": False,
                    }
                raise
            except TimeoutError as exc:
                cleanup = await self._cleanup_failed_grip(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                    release=plan["release"],
                    reason="no stable gripper contact before close endpoint",
                )
                return failed_grip_result(
                    target_position_rad=plan["grip"]["position_rad"],
                    failure=exc,
                    cleanup=cleanup,
                )
            except Exception:
                await self._cleanup_failed_grip(
                    grip=grip,
                    contact=contact,
                    session_id=contact_result["session_id"],
                    execution_id=execution_id,
                    release=plan["release"],
                    reason="grip workflow failed before carry transfer",
                )
                raise
            return {
                "status": "CARRYING_POSITION_EFFORT_LIMITED",
                "workflow_complete": True,
                "carry_id": carry_id,
                "attachment_revision": attachment_revision,
                "all_joints_position_effort_limited": True,
                "alignment": alignment,
                "gripper_opening": opening,
                "gripper_approach_ready": approach_ready,
                "gripper_opened_concurrently": True,
                "controller_handoff": controller_handoff,
                "contact": contact_result,
                "grip_contact": inferred,
                "grip_confirmation": confirmed,
                "plan": plan,
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
    if getattr(services, "integrated_motion", None) is None:
        raise RuntimeError("grip-object requires the bound Integrated motion adapter")
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
) -> GripObjectHostAdapter:
    workspace = skill_root.resolve().parents[1]
    assembly = _load(workspace / "config/robot_assemblies/primary_manipulator.json")
    provider_root = workspace / assembly["arm_provider"]["provider_root"]
    effector_path = provider_root / "profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
    return GripObjectHostAdapter(
        manager=services.manager,
        integrated_motion=services.integrated_motion,
        contact_url=str(context["contact_url"]),
        grip_url=str(context["grip_url"]),
        effector_path=effector_path.resolve(),
        profiles_path=(skill_root / "config/motion_profiles.json").resolve(),
        vector_profiles_path=(
            skill_root / "config/gripper_vector_profiles.json"
        ).resolve(),
    )


if __name__ == "__main__":
    if "--private-worker" not in sys.argv:
        raise SystemExit("grip-object host adapter is only a private worker entrypoint")
    from grip_work_runtime.private_worker import run_private_worker

    run_private_worker(_build_private_workflow)
