from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import importlib.util
import math
import sys


SKILL_ID = "contact.move_carried_object"


def _vector(value: Any, length: int, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain {length} values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must be finite")
    return result


class MoveCarriedObjectHostAdapter:
    def __init__(self, *, manager: Any, contact_url: str, grip_url: str):
        self.manager = manager
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.lock = asyncio.Lock()

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from grip_work_runtime import ContactCarryRuntime, GripRuntime, contact_step

        async with self.lock:
            await self.manager.set_hot("robot_arm.primary.grip")
            grip = GripRuntime(self.grip_url, signing_secret_env="MIDBRAIN_GRIP_OBJECT_SECRET")
            grip_state = await asyncio.to_thread(grip.state)
            carry = grip_state.get("carry")
            if not isinstance(carry, dict):
                raise RuntimeError("move-carried-object requires a confirmed Grip Provider carry")
            if grip_state.get("all_active_joints_position_effort_limited") is not True:
                raise RuntimeError("all active joints must be POSITION_EFFORT_LIMITED before carrying motion")
            delta = _vector(arguments["translation_vector_arm_base_m"], 3, "translation_vector_arm_base_m")
            distance = math.sqrt(sum(value * value for value in delta))
            if not 0.001 <= distance <= 1.2:
                raise ValueError("carrying translation norm must be in [0.001, 1.2] m")
            orientation = _vector(arguments["target_orientation_arm_base_xyzw"], 4, "target_orientation_arm_base_xyzw")
            await self.manager.set_hot("robot_arm.primary.contact")
            contact = ContactCarryRuntime(
                self.contact_url,
                self.manager.base_url,
                signing_secret_env="MIDBRAIN_CONTACT_MOVE_CARRIED_OBJECT_SECRET",
            )
            result = await asyncio.to_thread(
                contact.execute,
                skill_id=SKILL_ID,
                steps=[contact_step(position_m=delta, orientation_xyzw=orientation)],
                carry_id=str(carry["carry_id"]),
                attachment_revision=str(carry["attachment_revision"]),
                behavior="CONTINUE",
            )
            after = await asyncio.to_thread(grip.state)
            if after.get("all_active_joints_position_effort_limited") is not True:
                raise RuntimeError("carrying mode invariant failed after Contact motion")
            return {
                "status": "CARRIED_OBJECT_MOVED_AND_HELD",
                "workflow_complete": True,
                "carry_id": carry["carry_id"],
                "attachment_revision": carry["attachment_revision"],
                "all_joints_position_effort_limited": True,
                "contact": result,
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
) -> MoveCarriedObjectHostAdapter:
    return MoveCarriedObjectHostAdapter(
        manager=services.manager,
        contact_url=str(context["contact_url"]),
        grip_url=str(context["grip_url"]),
    )


if __name__ == "__main__":
    if "--private-worker" not in sys.argv:
        raise SystemExit(
            "move-carried-object host adapter is only a private worker entrypoint"
        )
    from grip_work_runtime.private_worker import run_private_worker

    run_private_worker(_build_private_workflow)
