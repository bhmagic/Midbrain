"""Nominal gravity and calibrated first-order joint dynamics."""
from __future__ import annotations

from typing import Any
import numpy as np

from .kinematics import RebotKinematics
from .models import ArmConfiguration


class RebotDynamics:
    """Compute arm and tool-payload gravity torques from the configured model."""

    def __init__(self, configuration: ArmConfiguration, kinematics: RebotKinematics):
        self.configuration = configuration
        self.kinematics = kinematics
        self.links = configuration.model["links"]
        self.gravity_vector = np.array([0.0, 0.0, -9.80665], dtype=float)
        self.payload_mass_kg = 0.0
        self.payload_com_tool_m = np.zeros(3, dtype=float)

    def set_payload(self, mass_kg: float, com_tool_m: Any) -> None:
        mass = float(mass_kg)
        com = np.asarray(com_tool_m, dtype=float)
        if not np.isfinite(mass) or mass < 0.0:
            raise ValueError("payload mass must be finite and non-negative")
        if com.shape != (3,) or not np.all(np.isfinite(com)):
            raise ValueError("payload COM must contain three finite tool-frame coordinates")
        self.payload_mass_kg = mass
        self.payload_com_tool_m = com.copy()

    def payload_snapshot(self) -> dict[str, Any]:
        return {
            "mass_kg": float(self.payload_mass_kg),
            "com_tool_m": self.payload_com_tool_m.tolist(),
            "enabled": bool(self.payload_mass_kg > 0.0),
        }

    def arm_potential_energy(self, positions_rad: Any) -> float:
        frames = self.kinematics.frames(positions_rad)
        total = 0.0
        # base_link is frame 0; link1..link6 are frames 1..6; end_link is frame 7.
        frame_indices = [0, 1, 2, 3, 4, 5, 6, 7]
        for link, frame_index in zip(self.links, frame_indices):
            frame = frames[frame_index]
            local = np.ones(4, dtype=float)
            local[:3] = np.asarray(link["center_of_mass_m"], dtype=float)
            world = frame @ local
            total -= float(link["mass_kg"]) * float(self.gravity_vector @ world[:3])
        return total

    def payload_potential_energy(self, positions_rad: Any) -> float:
        if self.payload_mass_kg <= 0.0:
            return 0.0
        tool = self.kinematics.controlled_frame(positions_rad)
        local = np.ones(4, dtype=float)
        local[:3] = self.payload_com_tool_m
        world = tool @ local
        return -float(self.payload_mass_kg) * float(self.gravity_vector @ world[:3])

    def potential_energy(self, positions_rad: Any) -> float:
        return self.arm_potential_energy(positions_rad) + self.payload_potential_energy(positions_rad)

    @staticmethod
    def _gradient_component(function, positions_rad: Any, index: int) -> float:
        if index >= 6:
            return 0.0
        q = np.asarray(positions_rad, dtype=float)
        epsilon = 1e-5
        plus = q.copy()
        minus = q.copy()
        plus[index] += epsilon
        minus[index] -= epsilon
        return float((function(plus) - function(minus)) / (2.0 * epsilon))

    def nominal_gravity_component(self, positions_rad: Any, index: int) -> float:
        return self._gradient_component(self.arm_potential_energy, positions_rad, index)

    def payload_gravity_component(self, positions_rad: Any, index: int) -> float:
        return self._gradient_component(self.payload_potential_energy, positions_rad, index)

    def nominal_gravity_gradient_component(self, positions_rad: Any, index: int) -> float:
        """Derivative of one arm gravity-torque component with respect to its joint."""
        if index >= 6:
            return 0.0
        q = np.asarray(positions_rad, dtype=float)
        epsilon = 2e-4
        plus = q.copy()
        minus = q.copy()
        plus[index] += epsilon
        minus[index] -= epsilon
        return float(
            (self.nominal_gravity_component(plus, index) - self.nominal_gravity_component(minus, index))
            / (2.0 * epsilon)
        )

    def nominal_gravity_torque(self, positions_rad: Any) -> np.ndarray:
        q = np.asarray(positions_rad, dtype=float)
        result = np.zeros(7, dtype=float)
        for index in range(6):
            result[index] = self.nominal_gravity_component(q, index)
        return result

    def payload_gravity_torque(self, positions_rad: Any) -> np.ndarray:
        q = np.asarray(positions_rad, dtype=float)
        result = np.zeros(7, dtype=float)
        if self.payload_mass_kg <= 0.0:
            return result
        for index in range(6):
            result[index] = self.payload_gravity_component(q, index)
        return result

    def calibrated_gravity_torque(self, positions_rad: Any) -> np.ndarray:
        positions = np.asarray(positions_rad, dtype=float)
        gravity_positions = positions.copy()
        for index, joint in enumerate(self.configuration.joints):
            calibration = self.configuration.calibration_by_name[joint.name]
            gravity_positions[index] += float(calibration.get("gravity_phase_offset_rad", 0.0))
        nominal = self.nominal_gravity_torque(gravity_positions)
        output = nominal.copy()
        for index, joint in enumerate(self.configuration.joints):
            calibration = self.configuration.calibration_by_name[joint.name]
            output[index] = (
                nominal[index] * float(calibration.get("gravity_scale", 1.0))
                + float(calibration.get("gravity_offset_nm", 0.0))
            )
        return output

    def compensated_gravity_torque(self, positions_rad: Any) -> np.ndarray:
        """Return calibrated arm gravity plus the currently declared tool payload."""
        return self.calibrated_gravity_torque(positions_rad) + self.payload_gravity_torque(positions_rad)

    def predicted_passive_torque(self, positions_rad: Any, velocities_rad_s: Any) -> np.ndarray:
        velocity = np.asarray(velocities_rad_s, dtype=float)
        output = self.compensated_gravity_torque(positions_rad)
        for index, joint in enumerate(self.configuration.joints):
            calibration = self.configuration.calibration_by_name[joint.name]
            fallback = float(calibration.get("coulomb_friction_nm", 0.0))
            positive = float(calibration.get("coulomb_friction_positive_nm", fallback))
            negative = float(calibration.get("coulomb_friction_negative_nm", fallback))
            smooth_sign = float(np.tanh(velocity[index] / 0.03))
            coulomb = positive * max(smooth_sign, 0.0) + negative * min(smooth_sign, 0.0)
            output[index] += coulomb
            output[index] += float(calibration.get("viscous_friction_nm_per_rad_s", 0.0)) * velocity[index]
            output[index] += float(calibration.get("torque_bias_nm", 0.0))
        return output
