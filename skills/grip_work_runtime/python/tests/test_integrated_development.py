from __future__ import annotations

from typing import Any
import time

from grip_work_runtime.development_execution import (
    IntegratedDevelopmentRuntime,
)


class FakeIntegratedClient:
    def __init__(self) -> None:
        self.committed = False
        self.warm = False
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    @staticmethod
    def identity() -> dict[str, str]:
        return {
            "provider_id": "robot_arm.primary.integrated",
            "provider_instance_id": "integrated-instance",
            "boot_id": "integrated-boot",
            "configuration_sha256": "configuration",
        }

    def get(self, url: str) -> dict[str, Any]:
        assert url == "http://integrated/v1/state"
        state: dict[str, Any] = {
            "ready": True,
            "residency": "WARM" if self.warm else "HOT",
            "controller_identity": self.identity(),
            "model_view": {
                "measured_controlled_frame": {
                    "position_m": [0.4, 0.5, 0.6],
                    "rpy_rad": [0.1, 0.2, 0.3],
                }
            },
            "planning": {},
            "safety": {"float_confirmed": self.committed},
            "trajectory": {"active": False},
            "lease": {"active": False if self.warm else self.committed},
        }
        if self.committed:
            state["planning"] = {
                "last_authorized_transit": {
                    "plan_id": "plan-1",
                    "status": "COMPLETED_FLOAT",
                }
            }
        return state

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.posts.append((url, payload, headers or {}))
        if url == "http://integrated/v1/motion/path-plan":
            return {
                "status": "PLANNED",
                "plan_id": "plan-1",
                "closest_safe": False,
                "selected_plan": {
                    "planning_valid": True,
                    "preview": {"collision_free": True},
                },
                "preview_contract": {
                    "request_context_complete": True,
                    "request_sha256": "request",
                    "preview_sha256": "preview",
                    "controller_provider_id": "robot_arm.primary.integrated",
                    "controller_provider_instance_id": "integrated-instance",
                    "controller_boot_id": "integrated-boot",
                    "controller_configuration_sha256": "configuration",
                    "issued_at_us": time.time_ns() // 1000,
                    "expires_at_us": time.time_ns() // 1000 + 30_000_000,
                    "scene_revision": "scene",
                },
            }
        if url == "http://authorization/api/authorizations":
            return {"decision_id": "decision-1", "status": "PENDING"}
        if url.endswith("/decision-1/resolve"):
            return {"decision_id": "decision-1", "status": "APPROVED"}
        if url.endswith("/decision-1/execution-assertion"):
            return {
                "assertion": "signed-assertion",
                "assertion_sha256": "assertion-sha256",
            }
        if url == "http://integrated/v1/motion/path-commit":
            assert (headers or {}).get("X-Midbrain-Authorization") == (
                "signed-assertion"
            )
            self.committed = True
            return {
                "status": "EXECUTING",
                "planned_duration_s": 0.1,
                "final_state": "FLOAT",
            }
        if url == "http://integrated/v1/motion/path-release":
            return {"released": True}
        raise AssertionError(f"unexpected POST {url}")


class FakeManager:
    def __init__(self, client: FakeIntegratedClient) -> None:
        self.client = client
        self.events: list[tuple[str, str]] = []

    def set_residency(self, provider_id: str, action: str) -> None:
        self.events.append((action, provider_id))
        if provider_id == "robot_arm.primary.integrated" and action == "warm":
            self.client.warm = True

    def set_hot(self, provider_id: str) -> None:
        self.events.append(("hot", provider_id))


def test_integrated_development_uses_existing_authorization_api() -> None:
    client = FakeIntegratedClient()
    runtime = IntegratedDevelopmentRuntime(
        "http://integrated",
        "http://authorization",
        client=client,
    )

    prepared = runtime.preview_rotation(
        target_rpy_rad=[0.0, 1.0, 0.0],
        calibration_binding={"activation_id": "activation"},
    )
    result = runtime.execute(prepared)

    preview_request = next(
        payload
        for url, payload, _headers in client.posts
        if url.endswith("/v1/motion/path-plan")
    )
    assert preview_request["ik_mode"] == "POSE_6DOF"
    assert preview_request["final_state"] == "FLOAT"
    assert preview_request["allowed_contact_object_ids"] == []
    assert preview_request["target"] == {
        "position_m": [0.4, 0.5, 0.6],
        "rpy_rad": [0.0, 1.0, 0.0],
    }
    resolution = preview_request["request_context"]["spatial_resolution"]
    assert resolution["schema"] == "physical_agent.semantic_direction_resolution"
    assert resolution["direction"] == "NONE"
    assert resolution["reference_frame"] == "CONTROLLED_FRAME"
    assert resolution["resolved_unit_vector"] == [0.0, 0.0, 0.0]
    assert resolution["provenance"]["resolution_source"] == (
        "ROTATION_ONLY_NO_TRANSLATION"
    )
    approval = next(
        payload
        for url, payload, _headers in client.posts
        if url.endswith("/decision-1/resolve")
    )
    assert approval["resolved_by"] == "grip.development-operator"
    assert result["final_state"] == "FLOAT"
    assert result["goal_reached"] is True
    assert runtime.measured_controlled_frame_position(runtime.state()) == [
        0.4,
        0.5,
        0.6,
    ]
    assert runtime.measured_controlled_frame_pose(runtime.state()) == {
        "position_m": [0.4, 0.5, 0.6],
        "rpy_rad": [0.1, 0.2, 0.3],
    }


def test_measured_controlled_frame_position_accepts_wrapped_observation() -> None:
    assert IntegratedDevelopmentRuntime.measured_controlled_frame_position(
        {
            "controller": {
                "model_view": {
                    "measured_controlled_frame": {
                        "position_m": [0.7, 0.8, 0.9]
                    }
                }
            }
        }
    ) == [0.7, 0.8, 0.9]


def test_integrated_development_uses_slicing_style_warm_handoff() -> None:
    client = FakeIntegratedClient()
    client.committed = True
    runtime = IntegratedDevelopmentRuntime(
        "http://integrated",
        "http://authorization",
        client=client,
    )
    manager = FakeManager(client)

    result = runtime.handoff_to_contact(
        manager,
        "robot_arm.primary.contact",
    )

    assert manager.events == [
        ("warm", "robot_arm.primary.integrated"),
        ("hot", "robot_arm.primary.contact"),
    ]
    assert result["integrated_basic_lease_active"] is False
