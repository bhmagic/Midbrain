from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import json

from .modes import SUPPORTED_EXECUTION_MODES, normalize_execution_mode


@dataclass(frozen=True)
class ConfigRepairResult:
    config: dict[str, Any]
    repaired: bool
    source: str


def _merge(defaults: Any, active: Any) -> Any:
    if isinstance(defaults, dict):
        result: dict[str, Any] = {}
        active_dict = active if isinstance(active, dict) else {}
        for key, value in defaults.items():
            result[key] = _merge(value, active_dict.get(key))
        return result
    if isinstance(defaults, list):
        if isinstance(active, list) and len(active) == len(defaults):
            return copy.deepcopy(active)
        return copy.deepcopy(defaults)
    return copy.deepcopy(defaults if active is None else active)


def validate_controller_config(config: dict[str, Any]) -> None:
    if config.get("schema") != "physical_agent.arm_integrated_mit_bringup_config":
        raise ValueError("unsupported Integrated Controller configuration schema")
    if int(config.get("schema_version", 0)) != 3:
        raise ValueError("unsupported Integrated Controller configuration version")
    runtime = config["runtime"]
    limits = config["runtime_limits"]
    normalize_execution_mode(runtime["execution_mode"])
    if str(runtime["interaction_mode"]).upper() not in {"ONE_SHOT", "HOLD_LB"}:
        raise ValueError("runtime.interaction_mode must be ONE_SHOT or HOLD_LB")
    if str(runtime["ik_mode"]).upper() not in {"POSITION_3DOF", "POSE_6DOF"}:
        raise ValueError("runtime.ik_mode must be POSITION_3DOF or POSE_6DOF")
    duration = float(runtime["duration_s"])
    if not float(limits["duration_min_s"]) <= duration <= float(limits["duration_max_s"]):
        raise ValueError("runtime.duration_s is outside runtime_limits")
    replan = float(runtime["replan_interval_s"])
    if not float(limits["replan_interval_min_s"]) <= replan <= float(limits["replan_interval_max_s"]):
        raise ValueError("runtime.replan_interval_s is outside runtime_limits")
    multiplier = float(runtime["kp_multiplier"])
    if not float(limits["kp_multiplier_min"]) <= multiplier <= float(limits["kp_multiplier_max"]):
        raise ValueError("runtime.kp_multiplier is outside runtime_limits")
    send_rate = float(config["trajectory"]["send_rate_hz"])
    if send_rate < 20.0 or send_rate > 200.0:
        raise ValueError("trajectory.send_rate_hz must be in [20, 200]")
    maximum = float(config["trajectory"]["maximum_translation_per_commit_m"])
    minimum = float(config["trajectory"].get("minimum_translation_per_commit_m", 0.0005))
    if maximum <= 0.0 or maximum > 0.20 + 1e-12:
        raise ValueError("trajectory.maximum_translation_per_commit_m must be in (0, 0.20]")
    if minimum < 0.0 or minimum >= maximum:
        raise ValueError("trajectory.minimum_translation_per_commit_m must be non-negative and below the maximum")
    link_radii = config["trajectory"]["link_radii_m"]
    if not isinstance(link_radii, list) or len(link_radii) != 7 or any(float(value) <= 0.0 for value in link_radii):
        raise ValueError("trajectory.link_radii_m must contain seven positive radii")
    if int(config["trajectory"]["preview_sample_count"]) < 3:
        raise ValueError("trajectory.preview_sample_count must be at least 3")
    if int(config["trajectory"].get("arrival_stable_samples", 10)) < 2:
        raise ValueError("trajectory.arrival_stable_samples must be at least 2")
    if not 0.0 <= float(config["teleop"]["deadzone"]) < 1.0:
        raise ValueError("teleop.deadzone must be in [0, 1)")
    if float(config["teleop"]["translation_rate_m_s"]) <= 0.0:
        raise ValueError("teleop.translation_rate_m_s must be positive")
    if float(config["teleop"]["rotation_rate_rad_s"]) <= 0.0:
        raise ValueError("teleop.rotation_rate_rad_s must be positive")
    gripper = config["gripper"]
    if str(gripper["mode"]).upper() not in {"MIT", "POS_TOR"}:
        raise ValueError("gripper.mode must be MIT or POS_TOR")
    if abs(float(gripper["open_position_rad"]) - float(gripper["closed_position_rad"])) < 1e-6:
        raise ValueError("gripper open and closed targets must be distinct")
    if float(gripper["velocity_limit_rad_s"]) <= 0.0:
        raise ValueError("gripper.velocity_limit_rad_s must be positive")
    if not 0.0 < float(gripper["torque_limit_ratio"]) <= 1.0:
        raise ValueError("gripper.torque_limit_ratio must be in (0, 1]")
    if float(gripper["mit_kp"]) <= 0.0 or float(gripper["mit_kd"]) < 0.0:
        raise ValueError("gripper MIT gains are invalid")
    if not 2.0 <= float(gripper["keepalive_hz"]) <= 20.0:
        raise ValueError("gripper.keepalive_hz must be in [2, 20]")
    for key in ("controlled_frame_offset_xyz_m", "controlled_frame_offset_rpy_rad", "payload_com_tool_m"):
        value = runtime[key]
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"runtime.{key} must contain three values")
    if float(runtime["payload_mass_kg"]) < 0.0:
        raise ValueError("runtime.payload_mass_kg must be non-negative")
    safety = config["safety"]
    for key in ("physically_enabled_execution_modes", "preview_required_execution_modes", "scene_required_execution_modes"):
        values = safety[key]
        if not isinstance(values, list) or any(str(value).upper() not in SUPPORTED_EXECUTION_MODES for value in values):
            raise ValueError(f"safety.{key} contains an unsupported execution mode")
    contact = config["contact"]
    if str(contact["budget_mode"]).upper() not in {"JOINT_6", "WRENCH_6", "ISOTROPIC_2"}:
        raise ValueError("contact.budget_mode must be JOINT_6, WRENCH_6, or ISOTROPIC_2")
    if contact["task_torque_budget_nm"] is not None:
        budget = contact["task_torque_budget_nm"]
        if not isinstance(budget, list) or len(budget) != 6 or any(float(value) <= 0.0 for value in budget):
            raise ValueError("contact.task_torque_budget_nm must be null or six positive values")
    for key in ("wrench_force_budget_n", "wrench_torque_budget_nm"):
        budget = contact[key]
        if budget is not None and (
            not isinstance(budget, list)
            or len(budget) != 3
            or any(float(value) < 0.0 for value in budget)
        ):
            raise ValueError(f"contact.{key} must be null or three non-negative values")
    for key in ("isotropic_force_budget_n", "isotropic_torque_budget_nm"):
        value = contact[key]
        if value is not None and float(value) < 0.0:
            raise ValueError(f"contact.{key} must be null or non-negative")
    minimum_mapped = contact["minimum_mapped_joint_budget_nm"]
    if (
        not isinstance(minimum_mapped, list)
        or len(minimum_mapped) != 6
        or any(float(value) <= 0.0 for value in minimum_mapped)
    ):
        raise ValueError("contact.minimum_mapped_joint_budget_nm must contain six positive values")
    if not 0.0 < float(contact["maximum_translation_m"]) <= 0.20:
        raise ValueError("contact.maximum_translation_m must be in (0, 0.20]")
    if not 0.5 <= float(contact["baseline_duration_s"]) <= 1.0:
        raise ValueError("contact.baseline_duration_s must be in [0.5, 1.0]")
    hybrid = config["hybrid_approach"]
    for key in ("handoff_position_error_rad", "handoff_velocity_rad_s", "completion_position_error_rad", "completion_velocity_rad_s"):
        values = hybrid[key]
        if not isinstance(values, list) or len(values) != 6 or any(float(value) <= 0.0 for value in values):
            raise ValueError(f"hybrid_approach.{key} must contain six positive values")
    if int(hybrid["required_stable_samples"]) < 2:
        raise ValueError("hybrid_approach.required_stable_samples must be at least 2")
    if int(hybrid.get("post_switch_stable_samples", 10)) < 2:
        raise ValueError("hybrid_approach.post_switch_stable_samples must be at least 2")
    if float(hybrid["mit_settle_duration_s"]) <= 0.0 or float(hybrid["approach_timeout_multiplier"]) < 1.0:
        raise ValueError("hybrid settle duration and approach timeout multiplier are invalid")
    planning = config["planning"]
    if int(planning["cartesian_waypoint_count"]) < 2:
        raise ValueError("planning.cartesian_waypoint_count must be at least 2")
    if float(planning["minimum_jacobian_sigma"]) <= 0.0 or float(planning["maximum_waypoint_joint_step_rad"]) <= 0.0:
        raise ValueError("planning singularity and continuity thresholds must be positive")
    endpoint_delta = planning["maximum_endpoint_joint_delta_rad"]
    if not isinstance(endpoint_delta, list) or len(endpoint_delta) != 6 or any(float(value) <= 0.0 for value in endpoint_delta):
        raise ValueError("planning.maximum_endpoint_joint_delta_rad must contain six positive values")
    fabric_input = config["fabric_input"]
    if not str(fabric_input["stream"]).strip():
        raise ValueError("fabric_input.stream must be non-empty")
    if not str(fabric_input["schema"]).strip():
        raise ValueError("fabric_input.schema must be non-empty")
    if int(fabric_input["poll_ms"]) < 20:
        raise ValueError("fabric_input.poll_ms must be at least 20 ms")
    if int(fabric_input["max_age_ms"]) <= 0:
        raise ValueError("fabric_input.max_age_ms must be positive")
    scene_input = config["scene_input"]
    if not str(scene_input["stream"]).strip() or not str(scene_input["schema"]).strip():
        raise ValueError("scene_input stream and schema must be non-empty")
    if int(scene_input["poll_ms"]) < 20 or int(scene_input["maximum_spheres"]) <= 0:
        raise ValueError("scene_input polling and sphere limits are invalid")


def ensure_controller_config(provider_root: Path, active_path: Path) -> ConfigRepairResult:
    template_path = provider_root / "config_templates" / "controller.default.json"
    defaults = json.loads(template_path.read_text(encoding="utf-8-sig"))
    active: dict[str, Any] = {}
    if active_path.exists():
        try:
            loaded = json.loads(active_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                active = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            active = {}

    preserved: dict[str, Any] = {}
    for key in ("provider_id", "display_name", "arm_id", "basic_controller_url", "listen_host", "listen_port"):
        if key in active:
            preserved[key] = active[key]
    if active.get("schema") == defaults.get("schema"):
        for key in ("trajectory", "runtime", "runtime_limits", "teleop", "gripper", "ik", "workspace", "planning", "safety", "contact", "hybrid_approach", "ui", "fabric_input", "scene_input"):
            if isinstance(active.get(key), dict):
                preserved[key] = active[key]
    if isinstance(active.get("platform"), dict):
        preserved["platform"] = {
            key: active["platform"][key]
            for key in ("motion_inhibit_poll_ms", "audited_midbrain_commit")
            if key in active["platform"]
        }

    merged = _merge(defaults, preserved)
    validate_controller_config(merged)
    canonical = json.dumps(merged, indent=2) + "\n"
    existing = active_path.read_text(encoding="utf-8-sig") if active_path.exists() else ""
    repaired = existing != canonical
    if repaired:
        active_path.parent.mkdir(parents=True, exist_ok=True)
        active_path.write_text(canonical, encoding="utf-8")
    return ConfigRepairResult(merged, repaired, str(template_path))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate or repair the MIT bring-up Integrated Controller configuration")
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = Path(args.config).resolve() if args.config else root / "config" / "controller.json"
    result = ensure_controller_config(root, config_path)
    print(f"Controller configuration validated; source={result.source}; repaired={result.repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    normalize_execution_mode(runtime["execution_mode"])
