from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import copy
import json

from .modes import SUPPORTED_EXECUTION_MODES, normalize_execution_mode


MANAGED_POLICY_REVISION = 10
LEGACY_ENDPOINT_JOINT_DELTA_RAD = (0.80, 0.80, 0.80, 1.00, 1.00, 1.00)
LEGACY_ARRIVAL_POSITION_TOLERANCE_RAD = (
    0.025,
    0.025,
    0.025,
    0.035,
    0.035,
    0.035,
)


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
    if int(config.get("managed_policy_revision", 0)) != MANAGED_POLICY_REVISION:
        raise ValueError("unsupported Integrated Controller managed policy revision")
    role_policy = config.get("role_policy")
    if not isinstance(role_policy, dict) or role_policy.get("role") != "FREE_SPACE":
        raise ValueError("Integrated Controller role_policy.role must be FREE_SPACE")
    for key in ("embedded_contact_enabled", "embedded_gripper_enabled"):
        if not isinstance(role_policy.get(key), bool):
            raise ValueError(f"role_policy.{key} must be boolean")
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
    if maximum <= 0.0 or maximum > 1.20 + 1e-12:
        raise ValueError("trajectory.maximum_translation_per_commit_m must be in (0, 1.20]")
    if minimum < 0.0 or minimum >= maximum:
        raise ValueError("trajectory.minimum_translation_per_commit_m must be non-negative and below the maximum")
    link_radii = config["trajectory"]["link_radii_m"]
    if not isinstance(link_radii, list) or len(link_radii) != 7 or any(float(value) <= 0.0 for value in link_radii):
        raise ValueError("trajectory.link_radii_m must contain seven positive radii")
    if int(config["trajectory"]["preview_sample_count"]) < 3:
        raise ValueError("trajectory.preview_sample_count must be at least 3")
    if int(config["trajectory"].get("arrival_stable_samples", 10)) < 2:
        raise ValueError("trajectory.arrival_stable_samples must be at least 2")
    if float(
        config["trajectory"].get("one_shot_arrival_settle_timeout_s", 0.0)
    ) <= 0.0:
        raise ValueError(
            "trajectory.one_shot_arrival_settle_timeout_s must be positive"
        )
    settle_kp_multiplier = float(
        config["trajectory"].get(
            "one_shot_arrival_settle_kp_multiplier",
            1.0,
        )
    )
    if not 1.0 <= settle_kp_multiplier <= float(
        config["runtime_limits"]["kp_multiplier_max"]
    ):
        raise ValueError(
            "trajectory.one_shot_arrival_settle_kp_multiplier must be "
            "between 1 and runtime_limits.kp_multiplier_max"
        )
    stage_timeout_multiplier = float(
        config["trajectory"].get(
            "authorized_stage_timeout_multiplier",
            4.0,
        )
    )
    stage_timeout_min_s = float(
        config["trajectory"].get(
            "authorized_stage_timeout_min_s",
            3.0,
        )
    )
    stage_timeout_max_s = float(
        config["trajectory"].get(
            "authorized_stage_timeout_max_s",
            8.0,
        )
    )
    if stage_timeout_multiplier < 1.0:
        raise ValueError(
            "trajectory.authorized_stage_timeout_multiplier must be "
            "at least 1"
        )
    if (
        stage_timeout_min_s <= 0.0
        or stage_timeout_max_s < stage_timeout_min_s
    ):
        raise ValueError(
            "trajectory authorized stage timeout bounds are invalid"
        )
    stage_stall_timeout_s = float(
        config["trajectory"].get(
            "authorized_stage_stall_timeout_s",
            2.5,
        )
    )
    if (
        stage_stall_timeout_s <= 0.0
        or stage_stall_timeout_s > stage_timeout_max_s
    ):
        raise ValueError(
            "trajectory.authorized_stage_stall_timeout_s must be "
            "positive and no greater than the maximum stage timeout"
        )
    if float(
        config["trajectory"].get(
            "authorized_stage_progress_epsilon_rad",
            0.002,
        )
    ) <= 0.0:
        raise ValueError(
            "trajectory.authorized_stage_progress_epsilon_rad must be "
            "positive"
        )
    if float(config["trajectory"]["arrival_cartesian_position_tolerance_m"]) <= 0.0:
        raise ValueError(
            "trajectory.arrival_cartesian_position_tolerance_m must be positive"
        )
    if float(config["trajectory"]["arrival_cartesian_orientation_tolerance_rad"]) <= 0.0:
        raise ValueError(
            "trajectory.arrival_cartesian_orientation_tolerance_rad must be positive"
        )
    if not 0.0 <= float(config["teleop"]["deadzone"]) < 1.0:
        raise ValueError("teleop.deadzone must be in [0, 1)")
    if float(config["teleop"]["translation_rate_m_s"]) <= 0.0:
        raise ValueError("teleop.translation_rate_m_s must be positive")
    if float(config["teleop"]["rotation_rate_rad_s"]) <= 0.0:
        raise ValueError("teleop.rotation_rate_rad_s must be positive")
    if not isinstance(config["workspace"].get("enforce_cartesian_bounds"), bool):
        raise ValueError("workspace.enforce_cartesian_bounds must be boolean")
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
    if int(planning["maximum_cartesian_waypoints_per_leg"]) < int(
        planning["cartesian_waypoint_count"]
    ):
        raise ValueError(
            "planning.maximum_cartesian_waypoints_per_leg must be at least "
            "planning.cartesian_waypoint_count"
        )
    if float(planning["minimum_jacobian_sigma"]) <= 0.0 or float(planning["maximum_waypoint_joint_step_rad"]) <= 0.0:
        raise ValueError("planning singularity and continuity thresholds must be positive")
    if not bool(planning["transit_shadow_enabled"]):
        raise ValueError("planning.transit_shadow_enabled must remain true in Phase 2")
    endpoint_delta = planning["maximum_endpoint_joint_delta_rad"]
    if not isinstance(endpoint_delta, list) or len(endpoint_delta) != 6 or any(float(value) <= 0.0 for value in endpoint_delta):
        raise ValueError("planning.maximum_endpoint_joint_delta_rad must contain six positive values")
    transit_endpoint_delta = planning[
        "maximum_transit_endpoint_joint_delta_rad"
    ]
    if (
        not isinstance(transit_endpoint_delta, list)
        or len(transit_endpoint_delta) != 6
        or any(float(value) <= 0.0 for value in transit_endpoint_delta)
    ):
        raise ValueError(
            "planning.maximum_transit_endpoint_joint_delta_rad must contain "
            "six positive values"
        )
    if float(planning["maximum_transit_total_joint_travel_rad"]) <= 0.0:
        raise ValueError(
            "planning.maximum_transit_total_joint_travel_rad must be positive"
        )
    if not 0.0 < float(
        planning["maximum_transit_joint_velocity_rad_s"]
    ) <= 20.0:
        raise ValueError(
            "planning.maximum_transit_joint_velocity_rad_s must be in (0, 20]"
        )
    authentication_threshold = float(
        planning["joint_speed_authentication_threshold_rad_s"]
    )
    hard_limit = float(planning["joint_speed_hard_limit_rad_s"])
    if not 0.0 < authentication_threshold < hard_limit <= 20.0:
        raise ValueError(
            "planning joint-speed policy must satisfy 0 < authentication "
            "threshold < hard limit <= 20 rad/s"
        )
    if not 0.02 <= float(planning["transit_stage_min_duration_s"]) <= 0.25:
        raise ValueError(
            "planning.transit_stage_min_duration_s must be in [0.02, 0.25]"
        )
    if float(planning["maximum_transit_start_joint_drift_rad"]) <= 0.0:
        raise ValueError(
            "planning.maximum_transit_start_joint_drift_rad must be positive"
        )
    if float(planning["maximum_transit_duration_s"]) <= 0.0:
        raise ValueError(
            "planning.maximum_transit_duration_s must be positive"
        )
    if not 1000 <= int(planning["transit_preview_ttl_ms"]) <= 30000:
        raise ValueError(
            "planning.transit_preview_ttl_ms must be in [1000, 30000]"
        )
    if not 0.1 <= float(planning["shadow_planning_time_budget_s"]) <= 3.0:
        raise ValueError(
            "planning.shadow_planning_time_budget_s must be in [0.1, 3.0]"
        )
    scene_input = config["scene_input"]
    if not str(scene_input["stream"]).strip() or not str(scene_input["schema"]).strip():
        raise ValueError("scene_input stream and schema must be non-empty")
    if int(scene_input["poll_ms"]) < 20 or int(scene_input["maximum_spheres"]) <= 0:
        raise ValueError("scene_input polling and sphere limits are invalid")
    if not isinstance(
        scene_input["ignore_pushable_for_collision_planning"], bool
    ):
        raise ValueError(
            "scene_input.ignore_pushable_for_collision_planning must be boolean"
        )
    clearance_margins = scene_input["clearance_margin_by_type_m"]
    if not isinstance(clearance_margins, dict) or set(clearance_margins) != {
        "KEEP_OUT",
        "PUSHABLE",
        "WORK_OBJECT",
    }:
        raise ValueError(
            "scene_input.clearance_margin_by_type_m must define exactly "
            "KEEP_OUT, PUSHABLE, and WORK_OBJECT"
        )
    if any(
        not 0.0 <= float(value) <= 0.25
        for value in clearance_margins.values()
    ):
        raise ValueError(
            "scene_input clearance margins must be in [0, 0.25] metres"
        )
    idle_profiles = config["idle_profiles"]
    lease_min_ms = int(idle_profiles["lease_min_ms"])
    lease_max_ms = int(idle_profiles["lease_max_ms"])
    lease_default_ms = int(idle_profiles["lease_default_ms"])
    if not 100 <= lease_min_ms <= lease_default_ms <= lease_max_ms <= 60_000:
        raise ValueError("idle profile lease limits are invalid")
    compliant_min = float(idle_profiles["compliant_kp_multiplier_min"])
    compliant_max = float(idle_profiles["compliant_kp_multiplier_max"])
    if not 1.0 <= compliant_min <= compliant_max <= float(
        config["runtime_limits"]["kp_multiplier_max"]
    ):
        raise ValueError("idle compliant Kp multiplier limits are invalid")
    if not 2.0 <= float(idle_profiles["keepalive_hz"]) <= 50.0:
        raise ValueError("idle profile keepalive_hz must be in [2, 50]")
    if float(idle_profiles["position_lock_velocity_limit_rad_s"]) <= 0.0:
        raise ValueError("idle position-lock velocity limit must be positive")
    control_audit = config["control_audit"]
    if str(control_audit["mode"]) not in {"SHADOW_BEST_EFFORT", "STRICT_LOCAL"}:
        raise ValueError("control_audit.mode is unsupported")
    for key in ("fabric_stream", "path", "cursor_path"):
        if not str(control_audit[key]).strip():
            raise ValueError(f"control_audit.{key} must be non-empty")
    if int(control_audit["maximum_pending"]) < 64:
        raise ValueError("control_audit.maximum_pending must be at least 64")
    if int(control_audit["maximum_replay"]) < 0:
        raise ValueError("control_audit.maximum_replay must be non-negative")
    manager_authority = config["manager_authority"]
    if str(manager_authority["mode"]) != "SHADOW_OBSERVE":
        raise ValueError(
            "manager_authority.mode must remain SHADOW_OBSERVE during Phase 2 evaluation"
        )
    if not str(manager_authority["resource_id"]).strip():
        raise ValueError("manager_authority.resource_id must be non-empty")
    if int(manager_authority["poll_ms"]) < 100:
        raise ValueError("manager_authority.poll_ms must be at least 100")


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
        for key in ("trajectory", "runtime", "runtime_limits", "idle_profiles", "teleop", "gripper", "ik", "workspace", "planning", "safety", "contact", "hybrid_approach", "ui", "manager_authority", "control_audit", "scene_input"):
            if isinstance(active.get(key), dict):
                preserved[key] = active[key]
    if isinstance(active.get("platform"), dict):
        preserved["platform"] = {
            key: active["platform"][key]
            for key in ("motion_inhibit_poll_ms", "audited_midbrain_commit")
            if key in active["platform"]
        }

    merged = _merge(defaults, preserved)
    active_policy_revision = int(active.get("managed_policy_revision", 0))
    default_policy_revision = int(defaults.get("managed_policy_revision", 0))
    active_planning = active.get("planning")
    active_planning = active_planning if isinstance(active_planning, dict) else {}
    active_endpoint_delta = active_planning.get(
        "maximum_endpoint_joint_delta_rad"
    )
    active_trajectory = active.get("trajectory")
    active_trajectory = (
        active_trajectory if isinstance(active_trajectory, dict) else {}
    )
    active_arrival_tolerance = active_trajectory.get(
        "arrival_position_tolerance_rad"
    )
    if (
        active_policy_revision < default_policy_revision
        and isinstance(active_endpoint_delta, list)
        and tuple(float(value) for value in active_endpoint_delta)
        == LEGACY_ENDPOINT_JOINT_DELTA_RAD
    ):
        merged["planning"]["maximum_endpoint_joint_delta_rad"] = copy.deepcopy(
            defaults["planning"]["maximum_endpoint_joint_delta_rad"]
        )
    if (
        active_policy_revision < default_policy_revision
        and isinstance(active_arrival_tolerance, list)
        and tuple(float(value) for value in active_arrival_tolerance)
        == LEGACY_ARRIVAL_POSITION_TOLERANCE_RAD
    ):
        merged["trajectory"]["arrival_position_tolerance_rad"] = copy.deepcopy(
            defaults["trajectory"]["arrival_position_tolerance_rad"]
        )
    if active_policy_revision < default_policy_revision:
        merged["manager_authority"]["resource_id"] = str(
            defaults["manager_authority"]["resource_id"]
        )
        merged["trajectory"]["maximum_translation_per_commit_m"] = float(
            defaults["trajectory"]["maximum_translation_per_commit_m"]
        )
        merged["runtime"]["execution_mode"] = str(
            defaults["runtime"]["execution_mode"]
        )
        merged["planning"]["maximum_transit_joint_velocity_rad_s"] = float(
            defaults["planning"]["maximum_transit_joint_velocity_rad_s"]
        )
        merged["runtime_limits"]["duration_min_s"] = float(
            defaults["runtime_limits"]["duration_min_s"]
        )
        merged["runtime_limits"]["duration_max_s"] = float(
            defaults["runtime_limits"]["duration_max_s"]
        )
        merged["planning"]["maximum_total_joint_travel_rad"] = float(
            defaults["planning"]["maximum_total_joint_travel_rad"]
        )
        merged["planning"]["maximum_endpoint_joint_delta_rad"] = copy.deepcopy(
            defaults["planning"]["maximum_endpoint_joint_delta_rad"]
        )
        merged["planning"][
            "maximum_transit_endpoint_joint_delta_rad"
        ] = copy.deepcopy(
            defaults["planning"][
                "maximum_transit_endpoint_joint_delta_rad"
            ]
        )
        merged["planning"]["maximum_transit_total_joint_travel_rad"] = float(
            defaults["planning"][
                "maximum_transit_total_joint_travel_rad"
            ]
        )
        merged["planning"][
            "joint_speed_authentication_threshold_rad_s"
        ] = float(
            defaults["planning"][
                "joint_speed_authentication_threshold_rad_s"
            ]
        )
        merged["planning"]["joint_speed_hard_limit_rad_s"] = float(
            defaults["planning"]["joint_speed_hard_limit_rad_s"]
        )
        merged["workspace"]["enforce_cartesian_bounds"] = bool(
            defaults["workspace"]["enforce_cartesian_bounds"]
        )
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
