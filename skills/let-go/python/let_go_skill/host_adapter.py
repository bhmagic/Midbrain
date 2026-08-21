from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import importlib.util
import json
import sys
import uuid


SKILL_ID = "grip.let_go"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


class LetGoHostAdapter:
    def __init__(
        self,
        *,
        manager: Any,
        contact_url: str,
        grip_url: str,
        effector_path: Path,
    ):
        self.manager = manager
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.effector_path = effector_path
        self.lock = asyncio.Lock()

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from grip_work_runtime import ContactCarryRuntime, GripRuntime

        async with self.lock:
            await self.manager.set_hot("robot_arm.primary.grip")
            grip = GripRuntime(
                self.grip_url,
                signing_secret_env="MIDBRAIN_GRIP_LET_GO_SECRET",
            )
            before = await asyncio.to_thread(grip.state)
            carry = before.get("carry")
            joint = _load(self.effector_path)["joint_control"]
            contact = None
            session_id = ""
            if isinstance(carry, dict):
                contact = ContactCarryRuntime(
                    self.contact_url,
                    self.manager.base_url,
                    signing_secret_env="MIDBRAIN_CONTACT_MOVE_CARRIED_OBJECT_SECRET",
                )
                contact_state = await asyncio.to_thread(contact.state)
                session_id = str(contact_state.get("session_id") or "")
            execution_id = str(uuid.uuid4())
            if isinstance(carry, dict):
                release = await asyncio.to_thread(
                    grip.command,
                    skill_id=SKILL_ID,
                    execution_id=execution_id,
                    operation="RELEASE_OBJECT",
                    carry_id=str(carry["carry_id"]),
                )
            else:
                release = await asyncio.to_thread(
                    grip.command,
                    skill_id=SKILL_ID,
                    execution_id=execution_id,
                    operation="SET_POSITION_EFFORT",
                    intent="OPEN",
                    position_rad=float(joint["open_position_rad"]),
                    velocity_limit_rad_s=float(joint["default_velocity_rad_s"]),
                    torque_limit_nm=float(joint["release_torque_limit_nm"]),
                )
            open_position = float(
                release.get("target_position_rad", joint["open_position_rad"])
            )
            opened = await asyncio.to_thread(
                grip.wait_for,
                lambda state: state.get("gripper_position_rad") is not None
                and abs(float(state["gripper_position_rad"]) - open_position) <= 0.08
                and state.get("gripper_velocity_rad_s") is not None
                and abs(float(state["gripper_velocity_rad_s"])) <= 0.08,
                timeout_s=float(arguments.get("open_timeout_s", 10.0)),
                description="measured open gripper",
            )
            transition = await asyncio.to_thread(
                grip.command,
                skill_id=SKILL_ID,
                execution_id=execution_id,
                operation="ENTER_MIT_FLOAT",
                delta_time_s=float(arguments.get("mit_delta_time_s", 0.5)),
            )
            floated = await asyncio.to_thread(
                grip.wait_for,
                lambda state: state.get("state") == "MIT_FLOAT",
                timeout_s=float(arguments.get("mit_delta_time_s", 0.5)) + 2.0,
                description="gripper MIT float",
            )
            relaxed = None
            if contact is not None:
                relaxed = await asyncio.to_thread(
                    contact.relax,
                    session_id,
                    "let-go completed measured opening and gripper MIT float",
                )
            released_carry_id = (
                str(carry["carry_id"])
                if isinstance(carry, dict)
                else None
            )
            return {
                "status": (
                    "OBJECT_RELEASED_AND_ARM_RELAXED"
                    if released_carry_id is not None
                    else "GRIPPER_OPENED_AND_FLOATED"
                ),
                "workflow_complete": True,
                "released_carry_id": released_carry_id,
                "release": release,
                "opened": opened,
                "transition": transition,
                "gripper_float": floated,
                "contact_relax": relaxed,
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
) -> LetGoHostAdapter:
    workspace = skill_root.resolve().parents[1]
    assembly = _load(workspace / "config/robot_assemblies/primary_manipulator.json")
    provider_root = workspace / assembly["arm_provider"]["provider_root"]
    return LetGoHostAdapter(
        manager=services.manager,
        contact_url=str(context["contact_url"]),
        grip_url=str(context["grip_url"]),
        effector_path=(
            provider_root
            / "profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ).resolve(),
    )


if __name__ == "__main__":
    if "--private-worker" not in sys.argv:
        raise SystemExit("let-go host adapter is only a private worker entrypoint")
    from grip_work_runtime.private_worker import run_private_worker

    run_private_worker(_build_private_workflow)
