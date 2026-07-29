"""Configuration models and validation for the reBot Arm DM Basic Controller."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import hashlib
import json
import math

import numpy as np

CONTROL_MODES = {
    "IMPEDANCE",
    "POSITION_VELOCITY_LIMITED",
    "VELOCITY",
    "POSITION_EFFORT_LIMITED",
}


class ConfigurationError(ValueError):
    """Raised when a provider configuration is internally inconsistent."""


def _finite_vector(value: Any, length: int, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != (length,) or not np.all(np.isfinite(array)):
        raise ConfigurationError(f"{name} must contain {length} finite numbers")
    return array


@dataclass(frozen=True)
class JointDefinition:
    name: str
    index: int
    motor_id: int
    feedback_id: int
    motor_model: str
    hard_min: float
    hard_max: float
    operational_min: float
    operational_max: float
    home: float
    configured_tmax_nm: float
    configured_vmax_rad_s: float
    kp_min: float
    kp_max: float
    kd_min: float
    kd_max: float
    default_kp: float
    default_kd: float
    default_vlim: float
    default_torque_ratio: float


class ArmConfiguration:
    """Loaded factory model plus assembly-specific calibration."""

    def __init__(self, model: dict[str, Any], calibration: dict[str, Any]):
        self.model = copy.deepcopy(model)
        self.calibration = copy.deepcopy(calibration)
        self._validate()
        self.joints = tuple(self._joint_definition(j) for j in self.model["joints"])
        self.calibration_by_name = {j["name"]: j for j in self.calibration["joints"]}

    @classmethod
    def load(cls, model_path: str | Path, calibration_path: str | Path) -> "ArmConfiguration":
        with Path(model_path).open("r", encoding="utf-8") as stream:
            model = json.load(stream)
        with Path(calibration_path).open("r", encoding="utf-8") as stream:
            calibration = json.load(stream)
        return cls(model, calibration)

    def _validate(self) -> None:
        if self.model.get("schema") != "physical_agent.robot_arm_model":
            raise ConfigurationError("invalid arm model schema")
        if self.calibration.get("schema") != "physical_agent.robot_arm_calibration":
            raise ConfigurationError("invalid arm calibration schema")
        if self.model.get("model_id") != self.calibration.get("model_id"):
            raise ConfigurationError("model and calibration IDs differ")
        joints = self.model.get("joints", [])
        calibrated = self.calibration.get("joints", [])
        if len(joints) != 7 or len(calibrated) != 7:
            raise ConfigurationError("the reBot arm configuration must contain seven joints")
        names = [str(j.get("name")) for j in joints]
        if len(set(names)) != 7:
            raise ConfigurationError("joint names must be unique")
        if {str(j.get("name")) for j in calibrated} != set(names):
            raise ConfigurationError("calibration joint names do not match the model")
        for index, joint in enumerate(joints):
            if int(joint.get("index", -1)) != index:
                raise ConfigurationError("joint indices must be ordered from zero")
            hard = _finite_vector(joint["hard_limit_rad"], 2, f"{joint['name']} hard limits")
            operation = _finite_vector(joint["operational_limit_rad"], 2, f"{joint['name']} operational limits")
            if not hard[0] <= operation[0] < operation[1] <= hard[1]:
                raise ConfigurationError(f"{joint['name']} operational range must be inside hard range")
            if not hard[0] <= float(joint["home_position_rad"]) <= hard[1]:
                raise ConfigurationError(f"{joint['name']} home is outside the hard range")
            calibrated_joint = next(item for item in calibrated if str(item.get("name")) == str(joint["name"]))
            minimum_load_bearing_kp = float(joint.get("provider_test_caps", {}).get("min_kp", joint["default_test"]["kp"]))
            if float(calibrated_joint.get("safe_float_kp", 0.0)) < minimum_load_bearing_kp:
                raise ConfigurationError(
                    f"{joint['name']} safe_float_kp must be at least {minimum_load_bearing_kp}; "
                    "low spring stiffness is prohibited for load-bearing MIT states"
                )
            if float(calibrated_joint.get("safe_float_kd", 0.0)) < 0.0:
                raise ConfigurationError(f"{joint['name']} safe_float_kd must be nonnegative")
        safe_home_kp = _finite_vector(self.model.get("control", {}).get("safe_home_kp"), 7, "safe_home_kp")
        safe_home_kd = _finite_vector(self.model.get("control", {}).get("safe_home_kd"), 7, "safe_home_kd")
        pos_vel_caps = _finite_vector(
            self.model.get("control", {}).get("physical_test_pos_vel_cap_rad_s"),
            7,
            "physical_test_pos_vel_cap_rad_s",
        )
        pos_tor_ratio_caps = _finite_vector(
            self.model.get("control", {}).get("physical_test_pos_tor_ratio_cap"),
            7,
            "physical_test_pos_tor_ratio_cap",
        )
        endpoint_keepalive_hz = float(
            self.model.get("control", {}).get("motor_endpoint_keepalive_hz", 0.0)
        )
        retry_count = int(
            self.model.get("control", {}).get("transient_serial_retry_count", -1)
        )
        retry_delay_ms = float(
            self.model.get("control", {}).get("transient_serial_retry_delay_ms", -1.0)
        )
        mit_mode_confirmation_timeout_ms = int(
            self.model.get("control", {}).get("mit_mode_confirmation_timeout_ms", 0)
        )
        gripper_force_position_native_velocity_scale = float(
            self.model.get("control", {}).get(
                "gripper_force_position_native_velocity_scale",
                1.0,
            )
        )
        gripper_force_position_velocity_guard_resume_ratio = float(
            self.model.get("control", {}).get(
                "gripper_force_position_velocity_guard_resume_ratio",
                0.75,
            )
        )
        if not 2.0 <= endpoint_keepalive_hz <= float(self.model["control"]["internal_rate_hz"]):
            raise ConfigurationError(
                "motor_endpoint_keepalive_hz must be in [2, internal_rate_hz]"
            )
        if retry_count < 0 or retry_count > 2:
            raise ConfigurationError("transient_serial_retry_count must be in [0, 2]")
        if retry_delay_ms < 0.0 or retry_delay_ms > 20.0:
            raise ConfigurationError(
                "transient_serial_retry_delay_ms must be in [0, 20]"
            )
        if not 20 <= mit_mode_confirmation_timeout_ms <= 1000:
            raise ConfigurationError(
                "mit_mode_confirmation_timeout_ms must be in [20, 1000]"
            )
        if not 0.0 < gripper_force_position_native_velocity_scale <= 1.0:
            raise ConfigurationError(
                "gripper_force_position_native_velocity_scale must be in (0, 1]"
            )
        if not 0.0 < gripper_force_position_velocity_guard_resume_ratio < 1.0:
            raise ConfigurationError(
                "gripper_force_position_velocity_guard_resume_ratio must be in (0, 1)"
            )
        for index, joint in enumerate(joints):
            minimum_load_bearing_kp = float(joint.get("provider_test_caps", {}).get("min_kp", joint["default_test"]["kp"]))
            if safe_home_kp[index] < minimum_load_bearing_kp:
                raise ConfigurationError(
                    f"{joint['name']} safe_home_kp must be at least {minimum_load_bearing_kp}; "
                    "safe-home must never cross a low-spring MIT state"
                )
            if safe_home_kd[index] < 0.0:
                raise ConfigurationError(f"{joint['name']} safe_home_kd must be nonnegative")
            configured_vmax = float(joint["motor_limits"]["configured_vmax_rad_s"])
            if not 0.0 < pos_vel_caps[index] <= configured_vmax:
                raise ConfigurationError(
                    f"{joint['name']} physical-test POS_VEL cap must be in "
                    f"(0, configured VMAX {configured_vmax}]"
                )
            if not 0.0 < pos_tor_ratio_caps[index] <= 1.0:
                raise ConfigurationError(
                    f"{joint['name']} physical-test POS_TOR ratio cap must be in (0, 1]"
                )

    def _joint_definition(self, joint: dict[str, Any]) -> JointDefinition:
        controls = joint["default_test"]
        limits = joint["motor_limits"]
        return JointDefinition(
            name=joint["name"], index=int(joint["index"]), motor_id=int(joint["motor_id"]),
            feedback_id=int(joint["feedback_id"]), motor_model=str(joint["motor_model"]),
            hard_min=float(joint["hard_limit_rad"][0]), hard_max=float(joint["hard_limit_rad"][1]),
            operational_min=float(joint["operational_limit_rad"][0]), operational_max=float(joint["operational_limit_rad"][1]),
            home=float(joint["home_position_rad"]), configured_tmax_nm=float(limits["configured_tmax_nm"]),
            configured_vmax_rad_s=float(limits["configured_vmax_rad_s"]),
            kp_min=float(limits["mit_kp_protocol_range"][0]), kp_max=float(limits["mit_kp_protocol_range"][1]),
            kd_min=float(limits["mit_kd_protocol_range"][0]), kd_max=float(limits["mit_kd_protocol_range"][1]),
            default_kp=float(controls["kp"]), default_kd=float(controls["kd"]),
            default_vlim=float(controls["velocity_limit_rad_s"]), default_torque_ratio=float(controls["torque_limit_ratio"]),
        )

    @property
    def model_revision(self) -> str:
        return str(self.model["model_revision"])

    @property
    def calibration_revision(self) -> str:
        return str(self.calibration["calibration_revision"])

    @property
    def hard_limits(self) -> np.ndarray:
        return np.asarray([[j.hard_min, j.hard_max] for j in self.joints], dtype=float)

    @property
    def operational_limits(self) -> np.ndarray:
        values = []
        for joint in self.joints:
            calibrated = self.calibration_by_name[joint.name]["operational_limit_rad"]
            values.append([max(joint.hard_min, float(calibrated[0])), min(joint.hard_max, float(calibrated[1]))])
        return np.asarray(values, dtype=float)

    @property
    def home_positions(self) -> np.ndarray:
        return _finite_vector(self.model["home"]["positions_rad"], 7, "home positions")

    def clip_hard(self, positions: np.ndarray) -> np.ndarray:
        limits = self.hard_limits
        return np.clip(np.asarray(positions, dtype=float), limits[:, 0], limits[:, 1])

    def assert_positions_allowed(self, positions: Any, *, use_operational: bool = True) -> np.ndarray:
        values = _finite_vector(positions, 7, "joint positions")
        limits = self.operational_limits if use_operational else self.hard_limits
        bad = np.where((values < limits[:, 0]) | (values > limits[:, 1]))[0]
        if bad.size:
            i = int(bad[0])
            raise ValueError(f"{self.joints[i].name} target {values[i]:.4f} is outside [{limits[i,0]:.4f}, {limits[i,1]:.4f}]")
        return values

    def validate_joint_command(self, index: int, mode: str, payload: dict[str, Any]) -> dict[str, float]:
        if mode not in CONTROL_MODES:
            raise ValueError(f"unknown control mode {mode}")
        if index < 0 or index >= 7:
            raise ValueError("joint index must be from 0 through 6")
        joint = self.joints[index]
        raw_joint = self.model["joints"][index]
        test_caps = raw_joint.get("provider_test_caps", {})
        calibrated = self.calibration_by_name[joint.name]
        output: dict[str, float] = {}
        if mode in {"IMPEDANCE", "POSITION_VELOCITY_LIMITED", "POSITION_EFFORT_LIMITED"}:
            position = float(payload["position_rad"])
            limits = self.operational_limits[index]
            if not limits[0] <= position <= limits[1]:
                raise ValueError(f"{joint.name} target is outside its operational range")
            output["position_rad"] = position
        if mode == "IMPEDANCE":
            output["velocity_rad_s"] = float(payload.get("velocity_rad_s", 0.0))
            output["kp"] = float(payload.get("kp", joint.default_kp))
            output["kd"] = float(payload.get("kd", joint.default_kd))
            output["feedforward_torque_nm"] = float(payload.get("feedforward_torque_nm", 0.0))
            rate_cap = min(joint.configured_vmax_rad_s, float(calibrated["provider_velocity_cap_rad_s"]))
            output["target_rate_limit_rad_s"] = float(payload.get("target_rate_limit_rad_s", joint.default_vlim))
            if not 0.0 < output["target_rate_limit_rad_s"] <= rate_cap:
                raise ValueError(f"{joint.name} MIT target rate must be in (0, {rate_cap}]")
            # The official reBot configuration uses kd=8 for DM-J4340P even though
            # some older Damiao protocol tables document a smaller encoded range.
            # For this provider, the reviewed per-joint calibration cap is authoritative.
            kp_floor = float(test_caps.get("min_kp", joint.default_kp))
            kp_cap = float(test_caps.get("max_kp", joint.default_kp))
            kd_cap = float(test_caps.get("max_kd", joint.default_kd))
            if not kp_floor <= output["kp"] <= kp_cap:
                raise ValueError(
                    f"{joint.name} kp must be in [{kp_floor}, {kp_cap}]; "
                    "low spring stiffness is prohibited for load-bearing MIT control"
                )
            if not 0.0 <= output["kd"] <= kd_cap:
                raise ValueError(f"{joint.name} kd exceeds the calibration cap {kd_cap}")
            if abs(output["feedforward_torque_nm"]) > joint.configured_tmax_nm:
                raise ValueError(f"{joint.name} feed-forward torque exceeds configured TMAX")
        elif mode == "POSITION_VELOCITY_LIMITED":
            physical_caps = self.model["control"]["physical_test_pos_vel_cap_rad_s"]
            cap = min(joint.configured_vmax_rad_s, float(physical_caps[index]))
            output["velocity_limit_rad_s"] = float(payload.get("velocity_limit_rad_s", joint.default_vlim))
            if not 0.0 < output["velocity_limit_rad_s"] <= cap:
                raise ValueError(f"{joint.name} velocity limit must be in (0, {cap}]")
        elif mode == "VELOCITY":
            cap = min(joint.configured_vmax_rad_s, float(calibrated["provider_velocity_cap_rad_s"]))
            output["velocity_rad_s"] = float(payload["velocity_rad_s"])
            if abs(output["velocity_rad_s"]) > cap:
                raise ValueError(f"{joint.name} velocity exceeds provider cap {cap}")
        elif mode == "POSITION_EFFORT_LIMITED":
            vcap = min(joint.configured_vmax_rad_s, float(calibrated["provider_velocity_cap_rad_s"]))
            physical_caps = self.model["control"]["physical_test_pos_tor_ratio_cap"]
            tcap = min(1.0, float(physical_caps[index]))
            output["velocity_limit_rad_s"] = float(payload.get("velocity_limit_rad_s", joint.default_vlim))
            output["torque_limit_ratio"] = float(payload.get("torque_limit_ratio", joint.default_torque_ratio))
            if not 0.0 < output["velocity_limit_rad_s"] <= vcap:
                raise ValueError(f"{joint.name} velocity limit must be in (0, {vcap}]")
            if not 0.0 <= output["torque_limit_ratio"] <= tcap:
                raise ValueError(f"{joint.name} torque ratio must be in [0, {tcap}]")
        if not all(math.isfinite(value) for value in output.values()):
            raise ValueError("command values must be finite")
        return output

    def public_model(self) -> dict[str, Any]:
        result = copy.deepcopy(self.model)
        result["active_calibration_revision"] = self.calibration_revision
        result["active_calibration_quality"] = copy.deepcopy(self.calibration.get("quality", {}))
        for joint in result["joints"]:
            calibrated = self.calibration_by_name[joint["name"]]
            joint["calibrated"] = copy.deepcopy(calibrated)
        return result

    def fingerprint(self) -> str:
        encoded = json.dumps({"model": self.model, "calibration": self.calibration}, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def save_calibration(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(self.calibration, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
