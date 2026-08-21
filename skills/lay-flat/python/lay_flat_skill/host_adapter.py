from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import importlib.util
import json
import math
import sys
import uuid


SKILL_ID = "grip.lay_flat"
EXTENSION_ID = "midbrain.provider.grip_control.v1"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _matvec(matrix, vector):
    return [sum(matrix[row][column] * vector[column] for column in range(3)) for row in range(3)]


def _distance(value: Any, name: str, maximum: float = 0.25) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0.0 < result <= maximum:
        raise ValueError(f"{name} must be in (0, {maximum}] m")
    return result


class LayFlatHostAdapter:
    def __init__(self, *, manager: Any, integrated_motion: Any, contact_url: str, grip_url: str, effector_path: Path, vector_profiles_path: Path, motion_profiles_path: Path):
        if integrated_motion is None:
            raise RuntimeError("lay-flat requires the bound Integrated motion adapter")
        self.manager = manager
        self.integrated_motion = integrated_motion
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.effector_path = effector_path
        self.vector_profiles_path = vector_profiles_path
        self.motion_profiles_path = motion_profiles_path
        self.lock = asyncio.Lock()

    @staticmethod
    def _profile(path: Path, requested: Any, label: str) -> dict[str, Any]:
        document = _load(path)
        number = document["default_profile_number"] if requested is None else int(requested)
        result = next((value for value in document["profiles"] if int(value["profile_number"]) == number), None)
        if not isinstance(result, dict):
            raise ValueError(f"requested lay-flat {label} profile is unavailable")
        return result

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from grip_work_runtime import (
            ContactCarryRuntime,
            GripRuntime,
            activation_binding,
            contact_step,
            execute_rotation_and_capture,
            handoff_to_contact,
            measured_controlled_position,
            prepare_rotation_only,
            quaternion_rpy,
            require_current_calibration,
            two_vector_orientation,
        )

        async with self.lock:
            await self.manager.set_hot("robot_arm.primary.grip")
            grip = GripRuntime(self.grip_url, signing_secret_env="MIDBRAIN_GRIP_LAY_FLAT_SECRET")
            before = await asyncio.to_thread(grip.state)
            carry = before.get("carry")
            if not isinstance(carry, dict):
                raise RuntimeError("lay-flat requires a confirmed carried object")
            await self.manager.set_hot("robot_arm.primary.integrated")
            activation_document = await self.manager.workcell_calibrations()
            activations = [value for value in activation_document.get("activations", []) if isinstance(value, dict) and value.get("state") == "ACTIVE" and value.get("motion_usable") is True and value.get("expires_at") is None and value.get("expires_at_us") is None]
            if len(activations) != 1:
                raise RuntimeError("lay-flat requires exactly one active motion-usable calibration")
            activation = activations[0]
            workcell_binding = activation_binding(activation)
            world_from_base = activation["transforms"]["world_from_base"]
            extension = _load(self.effector_path)
            if extension.get("schema") != "midbrain.grip_effector_control_profile":
                raise RuntimeError("active effector has no compatible grip-control profile")
            vector_profile = self._profile(
                self.vector_profiles_path,
                arguments.get("gripper_vector_profile_number"),
                "gripper vector",
            )
            motion_profile = self._profile(
                self.motion_profiles_path,
                arguments.get("motion_profile_number"),
                "motion",
            )
            orientation = two_vector_orientation(
                effector_first=vector_profile["table_inward_direction_effector"],
                effector_second=vector_profile["insertion_direction_effector"],
                world_first=arguments["table_inward_direction_world"],
                world_second=arguments["insertion_direction_world"],
                world_from_base_quaternion=world_from_base["rotation_xyzw"],
            )
            table_distance = _distance(
                motion_profile["table_inward_distance_m"]
                if arguments.get("table_inward_distance_m") is None
                else arguments["table_inward_distance_m"],
                "table_inward_distance_m",
            )
            negative_insertion = _distance(
                motion_profile["negative_insertion_distance_m"]
                if arguments.get("negative_insertion_distance_m") is None
                else arguments["negative_insertion_distance_m"],
                "negative_insertion_distance_m",
            )
            retreat_distance = _distance(
                motion_profile["retreat_distance_m"]
                if arguments.get("retreat_distance_m") is None
                else arguments["retreat_distance_m"],
                "retreat_distance_m",
            )
            combined_world = [
                orientation["world_first"][index] * table_distance
                - orientation["world_second"][index] * negative_insertion
                for index in range(3)
            ]
            retreat_world = [-value * retreat_distance for value in orientation["world_second"]]
            combined_base = _matvec(orientation["base_world_rotation"], combined_world)
            retreat_base = _matvec(orientation["base_world_rotation"], retreat_world)
            measured_start = measured_controlled_position(
                await self.integrated_motion.observation()
            )
            prepared_alignment = await prepare_rotation_only(
                self.integrated_motion,
                quaternion_rpy(orientation["orientation_arm_base_xyzw"]),
                operation_label="Lay Flat",
            )
            await require_current_calibration(
                self.manager,
                workcell_binding,
                operation_label="Lay Flat",
            )
            alignment = await execute_rotation_and_capture(
                self.integrated_motion,
                prepared_alignment["preview_id"],
                operation_label="Lay Flat",
            )
            await require_current_calibration(
                self.manager,
                workcell_binding,
                operation_label="Lay Flat",
            )
            controller_handoff = await handoff_to_contact(
                self.manager,
                self.integrated_motion,
                alignment,
                operation_label="Lay Flat",
            )
            contact = ContactCarryRuntime(
                self.contact_url,
                self.manager.base_url,
                signing_secret_env="MIDBRAIN_CONTACT_LAY_FLAT_SECRET",
            )
            placement = await asyncio.to_thread(
                contact.execute,
                skill_id=SKILL_ID,
                steps=[
                    contact_step(
                        position_m=[
                            measured_start[index] + combined_base[index]
                            for index in range(3)
                        ],
                        orientation_xyzw=orientation[
                            "orientation_arm_base_xyzw"
                        ],
                        position_mode="ABSOLUTE_ROOT",
                    )
                ],
                carry_id=str(carry["carry_id"]),
                attachment_revision=str(carry["attachment_revision"]),
                behavior="CONTINUE",
            )
            execution_id = str(uuid.uuid4())
            release = await asyncio.to_thread(
                grip.command,
                skill_id=SKILL_ID,
                execution_id=execution_id,
                operation="RELEASE_OBJECT",
                carry_id=str(carry["carry_id"]),
            )
            open_position = float(release["target_position_rad"])
            opened = await asyncio.to_thread(
                grip.wait_for,
                lambda state: state.get("gripper_position_rad") is not None and abs(float(state["gripper_position_rad"]) - open_position) <= 0.08,
                timeout_s=float(
                    motion_profile["open_timeout_s"]
                    if arguments.get("open_timeout_s") is None
                    else arguments["open_timeout_s"]
                ),
                description="measured open gripper",
            )
            await asyncio.to_thread(
                grip.command,
                skill_id=SKILL_ID,
                execution_id=execution_id,
                operation="ENTER_MIT_FLOAT",
                delta_time_s=float(
                    motion_profile["mit_delta_time_s"]
                    if arguments.get("mit_delta_time_s") is None
                    else arguments["mit_delta_time_s"]
                ),
            )
            await asyncio.to_thread(
                grip.wait_for,
                lambda state: state.get("state") == "MIT_FLOAT",
                timeout_s=float(
                    motion_profile["mit_delta_time_s"]
                    if arguments.get("mit_delta_time_s") is None
                    else arguments["mit_delta_time_s"]
                ) + 2.0,
                description="gripper MIT float",
            )
            retreat = await asyncio.to_thread(
                contact.execute,
                skill_id=SKILL_ID,
                steps=[contact_step(position_m=retreat_base, orientation_xyzw=orientation["orientation_arm_base_xyzw"])],
                carry_id=str(carry["carry_id"]),
                attachment_revision=str(carry["attachment_revision"]),
                behavior="CONTINUE",
            )
            relaxed = await asyncio.to_thread(contact.relax, retreat["session_id"], "lay-flat release and retreat complete")
            return {
                "status": "OBJECT_LAID_FLAT_RELEASED_AND_RETRACTED",
                "workflow_complete": True,
                "released_carry_id": carry["carry_id"],
                "alignment": alignment,
                "controller_handoff": controller_handoff,
                "placement": placement,
                "release": release,
                "opened": opened,
                "retreat": retreat,
                "contact_relax": relaxed,
                "gripper_vector_profile": vector_profile,
                "motion_profile": motion_profile,
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
        raise RuntimeError("lay-flat requires the bound Integrated motion adapter")
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
) -> LayFlatHostAdapter:
    workspace = skill_root.resolve().parents[1]
    assembly = _load(workspace / "config/robot_assemblies/primary_manipulator.json")
    provider_root = workspace / assembly["arm_provider"]["provider_root"]
    effector_path = provider_root / "profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
    return LayFlatHostAdapter(
        manager=services.manager,
        integrated_motion=services.integrated_motion,
        contact_url=str(context["contact_url"]),
        grip_url=str(context["grip_url"]),
        effector_path=effector_path.resolve(),
        vector_profiles_path=(
            skill_root / "config/gripper_vector_profiles.json"
        ).resolve(),
        motion_profiles_path=(skill_root / "config/motion_profiles.json").resolve(),
    )


if __name__ == "__main__":
    if "--private-worker" not in sys.argv:
        raise SystemExit("lay-flat host adapter is only a private worker entrypoint")
    from grip_work_runtime.private_worker import run_private_worker

    run_private_worker(_build_private_workflow)
