from __future__ import annotations

from typing import Any
import math


MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD = 0.35


def _finite_vector3(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(
            not isinstance(component, bool)
            and math.isfinite(float(component))
            for component in value
        )
    )


def _rpy_rotation(value: Any) -> list[list[float]]:
    if not _finite_vector3(value):
        raise ValueError("RPY orientation must contain three finite values")
    roll, pitch, yaw = (float(component) for component in value)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _orientation_distance_rad(first: Any, second: Any) -> float:
    first_rotation = _rpy_rotation(first)
    second_rotation = _rpy_rotation(second)
    relative_trace = 0.0
    for row in range(3):
        for column in range(3):
            relative_trace += (
                first_rotation[row][column]
                * second_rotation[row][column]
            )
    cosine = max(-1.0, min(1.0, (relative_trace - 1.0) * 0.5))
    return math.acos(cosine)


def activation_binding(activation: dict[str, Any]) -> dict[str, Any]:
    transforms = activation.get("transforms")
    transforms = transforms if isinstance(transforms, dict) else {}
    world_from_base = transforms.get("world_from_base")
    if not isinstance(world_from_base, dict):
        raise RuntimeError("active calibration has no world_from_base transform")
    translation = world_from_base.get("translation_m")
    rotation = world_from_base.get("rotation_xyzw")
    if not _finite_vector3(translation):
        raise RuntimeError("active calibration world_from_base translation is invalid")
    if (
        not isinstance(rotation, (list, tuple))
        or len(rotation) != 4
        or any(
            isinstance(component, bool)
            or not math.isfinite(float(component))
            for component in rotation
        )
    ):
        raise RuntimeError("active calibration world_from_base rotation is invalid")
    fields = {
        "activation_id": str(activation.get("activation_id") or "").strip(),
        "calibration_revision": str(
            activation.get("calibration_revision") or ""
        ).strip(),
        "world_frame": str(activation.get("world_frame") or "").strip(),
        "arm_base_frame": str(activation.get("arm_base_frame") or "").strip(),
    }
    if any(not value for value in fields.values()):
        raise RuntimeError("active calibration identity is incomplete")
    return {
        **fields,
        "world_from_base": {
            "translation_m": [float(value) for value in translation],
            "rotation_xyzw": [float(value) for value in rotation],
        },
    }


def same_activation_binding(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    tolerance: float = 1e-6,
) -> bool:
    for name in (
        "activation_id",
        "calibration_revision",
        "world_frame",
        "arm_base_frame",
    ):
        if expected.get(name) != actual.get(name):
            return False
    for name in ("translation_m", "rotation_xyzw"):
        first = expected.get("world_from_base", {}).get(name)
        second = actual.get("world_from_base", {}).get(name)
        if (
            not isinstance(first, list)
            or not isinstance(second, list)
            or len(first) != len(second)
            or any(
                abs(float(left) - float(right)) > tolerance
                for left, right in zip(first, second)
            )
        ):
            return False
    return True


async def require_current_calibration(
    manager: Any,
    expected_binding: dict[str, Any],
    *,
    operation_label: str,
) -> None:
    document = await manager.workcell_calibrations()
    activations = [
        value
        for value in document.get("activations", [])
        if isinstance(value, dict)
        and value.get("state") == "ACTIVE"
        and value.get("motion_usable") is True
        and value.get("expires_at") is None
        and value.get("expires_at_us") is None
    ]
    if len(activations) != 1:
        raise RuntimeError(
            f"{operation_label} requires exactly one active motion-usable calibration"
        )
    if not same_activation_binding(
        expected_binding,
        activation_binding(activations[0]),
    ):
        raise RuntimeError(
            f"workcell calibration changed; {operation_label} did not start its next stage"
        )


def measured_controlled_position(observation: dict[str, Any]) -> list[float]:
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    model_view = controller.get("model_view")
    model_view = model_view if isinstance(model_view, dict) else {}
    measured = model_view.get("measured_controlled_frame")
    measured = measured if isinstance(measured, dict) else {}
    position = measured.get("position_m")
    if not _finite_vector3(position):
        raise RuntimeError(
            "Integrated has no finite measured controlled-effector position"
        )
    return [float(value) for value in position]


async def prepare_rotation_only(
    integrated_motion: Any,
    target_rpy_rad: list[float],
    *,
    operation_label: str,
) -> dict[str, Any]:
    preview = await integrated_motion.preview(
        direction="NONE",
        distance_m=0.0,
        reference_frame="ARM_BASE",
        arm_mount_assumption="UNKNOWN",
        camera_level_assumption="UNKNOWN",
        fixed_vio_rig_assumption="UNKNOWN",
        orientation_policy="SET_ARM_BASE_RPY",
        target_orientation_rpy_rad=[float(value) for value in target_rpy_rad],
        execution_backend="IMPEDANCE",
    )
    preview_id = str(preview.get("preview_id") or "").strip()
    if preview.get("status") != "PREVIEW_READY" or not preview_id:
        raise RuntimeError(
            f"Integrated did not produce the {operation_label} rotation preview: "
            f"{preview.get('message') or preview.get('status') or 'unknown reason'}"
        )
    return {"preview": preview, "preview_id": preview_id}


async def execute_rotation_and_capture(
    integrated_motion: Any,
    preview_id: str,
    *,
    operation_label: str,
) -> dict[str, Any]:
    result = await integrated_motion.execute_preview(preview_id=preview_id)
    if (
        result.get("workflow_complete") is not True
        or result.get("physical_motion_completed") is not True
        or result.get("goal_reached") is not True
        or str(result.get("final_state") or "").upper() != "FLOAT"
    ):
        raise RuntimeError(
            f"Integrated {operation_label} rotation did not complete its accepted target in FLOAT"
        )
    observation = await integrated_motion.observation()
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    safety = controller.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    trajectory = controller.get("trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    model_view = controller.get("model_view")
    model_view = model_view if isinstance(model_view, dict) else {}
    measured = model_view.get("measured_controlled_frame")
    measured = measured if isinstance(measured, dict) else {}
    planning = controller.get("planning")
    planning = planning if isinstance(planning, dict) else {}
    completed = planning.get("last_authorized_transit")
    completed = completed if isinstance(completed, dict) else {}
    measured_position = measured.get("position_m")
    measured_rpy = measured.get("rpy_rad")
    completed_plan_id = str(completed.get("plan_id") or "").strip()
    result_plan_id = str(
        result.get("controller_preview_id") or result.get("preview_id") or ""
    ).strip()
    if (
        safety.get("float_confirmed") is not True
        or trajectory.get("active") is not False
        or not _finite_vector3(measured_position)
        or not _finite_vector3(measured_rpy)
        or not result_plan_id
        or completed_plan_id != result_plan_id
    ):
        raise RuntimeError(
            f"Integrated completed {operation_label} rotation without a stable identity-bound FLOAT handoff"
        )
    return {
        "execution": result,
        "handoff": {
            "plan_id": result_plan_id,
            "controller_boot_id": str(controller.get("boot_id") or "") or None,
            "controller_provider_instance_id": (
                str(controller.get("provider_instance_id") or "") or None
            ),
            "measured_position_m": [float(value) for value in measured_position],
            "measured_rpy_rad": [float(value) for value in measured_rpy],
            "float_confirmed": True,
            "trajectory_active": False,
            "maximum_orientation_drift_rad": (
                MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD
            ),
        },
    }


async def handoff_to_contact(
    manager: Any,
    integrated_motion: Any,
    evidence: dict[str, Any],
    *,
    operation_label: str,
) -> dict[str, Any]:
    handoff = evidence.get("handoff")
    handoff = handoff if isinstance(handoff, dict) else {}
    observation = await integrated_motion.observation()
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    safety = controller.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    trajectory = controller.get("trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    model_view = controller.get("model_view")
    model_view = model_view if isinstance(model_view, dict) else {}
    measured = model_view.get("measured_controlled_frame")
    measured = measured if isinstance(measured, dict) else {}
    planning = controller.get("planning")
    planning = planning if isinstance(planning, dict) else {}
    completed = planning.get("last_authorized_transit")
    completed = completed if isinstance(completed, dict) else {}
    measured_rpy = measured.get("rpy_rad")
    expected_rpy = handoff.get("measured_rpy_rad")
    orientation_drift = (
        _orientation_distance_rad(measured_rpy, expected_rpy)
        if _finite_vector3(measured_rpy) and _finite_vector3(expected_rpy)
        else math.inf
    )
    expected_plan_id = str(handoff.get("plan_id") or "").strip()
    current_plan_id = str(completed.get("plan_id") or "").strip()
    expected_boot_id = str(handoff.get("controller_boot_id") or "").strip()
    expected_instance_id = str(
        handoff.get("controller_provider_instance_id") or ""
    ).strip()
    if (
        safety.get("float_confirmed") is not True
        or trajectory.get("active") is not False
        or orientation_drift > MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD
        or current_plan_id != expected_plan_id
        or (expected_boot_id and str(controller.get("boot_id") or "") != expected_boot_id)
        or (
            expected_instance_id
            and str(controller.get("provider_instance_id") or "")
            != expected_instance_id
        )
    ):
        raise RuntimeError(
            f"Integrated-to-Contact {operation_label} preflight rejected the handoff"
        )
    await manager.set_residency("robot_arm.primary.integrated", "warm")
    observation = await integrated_motion.observation()
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    safety = controller.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    trajectory = controller.get("trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    lease = controller.get("lease")
    lease = lease if isinstance(lease, dict) else {}
    residency = str(controller.get("residency") or "").upper()
    if (
        residency != "WARM"
        or safety.get("float_confirmed") is not True
        or trajectory.get("active") is not False
        or lease.get("active") is not False
    ):
        raise RuntimeError(
            f"Integrated-to-Contact {operation_label} lease handoff was not confirmed; Contact was not started"
        )
    await manager.set_hot("robot_arm.primary.contact")
    return {
        "integrated_residency": residency,
        "float_confirmed": True,
        "trajectory_active": False,
        "integrated_basic_lease_active": False,
        "contact_hot_requested": True,
    }


async def handoff_idle_integrated_to_contact(
    manager: Any,
    integrated_motion: Any,
    *,
    operation_label: str,
) -> dict[str, Any]:
    """Release an idle Integrated arm lease before Contact locks the pose."""
    observation = await integrated_motion.observation()
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    safety = controller.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    trajectory = controller.get("trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    if (
        safety.get("float_confirmed") is not True
        or trajectory.get("active") is not False
    ):
        raise RuntimeError(
            f"Integrated-to-Contact {operation_label} preflight requires "
            "verified FLOAT with no active trajectory"
        )

    await manager.set_residency("robot_arm.primary.integrated", "warm")
    observation = await integrated_motion.observation()
    controller = observation.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    safety = controller.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    trajectory = controller.get("trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    lease = controller.get("lease")
    lease = lease if isinstance(lease, dict) else {}
    residency = str(controller.get("residency") or "").upper()
    if (
        residency != "WARM"
        or safety.get("float_confirmed") is not True
        or trajectory.get("active") is not False
        or lease.get("active") is not False
    ):
        raise RuntimeError(
            f"Integrated-to-Contact {operation_label} lease handoff was not "
            "confirmed; Contact was not started"
        )
    await manager.set_hot("robot_arm.primary.contact")
    return {
        "integrated_residency": residency,
        "float_confirmed": True,
        "trajectory_active": False,
        "integrated_basic_lease_active": False,
        "contact_hot_requested": True,
    }
