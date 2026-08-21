from __future__ import annotations

import asyncio

from grip_work_runtime.slicing_flow import (
    execute_rotation_and_capture,
    handoff_idle_integrated_to_contact,
    handoff_to_contact,
    prepare_rotation_only,
)


class FakeIntegratedMotion:
    def __init__(self) -> None:
        self.preview_arguments = None
        self.residency = "HOT"
        self.lease_active = True

    async def preview(self, **arguments):
        self.preview_arguments = arguments
        return {"status": "PREVIEW_READY", "preview_id": "plan-1"}

    async def execute_preview(self, *, preview_id):
        assert preview_id == "plan-1"
        return {
            "workflow_complete": True,
            "physical_motion_completed": True,
            "goal_reached": True,
            "final_state": "FLOAT",
            "preview_id": preview_id,
        }

    async def observation(self):
        return {
            "controller": {
                "residency": self.residency,
                "boot_id": "integrated-boot",
                "provider_instance_id": "integrated-instance",
                "safety": {"float_confirmed": True},
                "trajectory": {"active": False},
                "lease": {"active": self.lease_active},
                "model_view": {
                    "measured_controlled_frame": {
                        "position_m": [0.4, 0.5, 0.6],
                        "rpy_rad": [0.1, 0.2, 0.3],
                    }
                },
                "planning": {
                    "last_authorized_transit": {
                        "plan_id": "plan-1",
                        "status": "COMPLETED_FLOAT",
                    }
                },
            }
        }


class FakeManager:
    def __init__(self, integrated: FakeIntegratedMotion) -> None:
        self.integrated = integrated
        self.events = []

    async def set_residency(self, provider_id, action):
        self.events.append((action, provider_id))
        self.integrated.residency = "WARM"
        self.integrated.lease_active = False

    async def set_hot(self, provider_id):
        self.events.append(("hot", provider_id))


def test_slicing_flow_submits_rotation_only_then_verified_handoff() -> None:
    asyncio.run(_exercise_slicing_flow())


def test_idle_integrated_handoff_releases_lease_before_contact_hot() -> None:
    async def exercise() -> None:
        integrated = FakeIntegratedMotion()
        manager = FakeManager(integrated)

        handoff = await handoff_idle_integrated_to_contact(
            manager,
            integrated,
            operation_label="generic grip",
        )

        assert manager.events == [
            ("warm", "robot_arm.primary.integrated"),
            ("hot", "robot_arm.primary.contact"),
        ]
        assert handoff["integrated_basic_lease_active"] is False

    asyncio.run(exercise())


async def _exercise_slicing_flow() -> None:
    integrated = FakeIntegratedMotion()
    manager = FakeManager(integrated)

    prepared = await prepare_rotation_only(
        integrated,
        [0.1, 0.2, 0.3],
        operation_label="test",
    )
    evidence = await execute_rotation_and_capture(
        integrated,
        prepared["preview_id"],
        operation_label="test",
    )
    handoff = await handoff_to_contact(
        manager,
        integrated,
        evidence,
        operation_label="test",
    )

    assert integrated.preview_arguments == {
        "direction": "NONE",
        "distance_m": 0.0,
        "reference_frame": "ARM_BASE",
        "arm_mount_assumption": "UNKNOWN",
        "camera_level_assumption": "UNKNOWN",
        "fixed_vio_rig_assumption": "UNKNOWN",
        "orientation_policy": "SET_ARM_BASE_RPY",
        "target_orientation_rpy_rad": [0.1, 0.2, 0.3],
        "execution_backend": "IMPEDANCE",
    }
    assert manager.events == [
        ("warm", "robot_arm.primary.integrated"),
        ("hot", "robot_arm.primary.contact"),
    ]
    assert handoff["integrated_basic_lease_active"] is False
