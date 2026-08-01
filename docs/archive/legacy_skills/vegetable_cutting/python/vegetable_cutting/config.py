from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    configured = os.getenv("PHYSICAL_AGENT_ROOT")
    if configured:
        return Path(configured).resolve()
    return skill_root().parent.parent


SKILL_ROOT = skill_root()
WORKSPACE_ROOT = workspace_root()
load_dotenv(WORKSPACE_ROOT / "config" / "api_keys.env", override=False)
load_dotenv(WORKSPACE_ROOT / "config" / "system.env", override=False)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_skill_config(path: Path | None = None) -> dict[str, Any]:
    template = SKILL_ROOT / "config_templates" / "cutting.default.json"
    config = json.loads(template.read_text(encoding="utf-8"))
    local_config = SKILL_ROOT / "config" / "cutting.json"
    legacy_config = WORKSPACE_ROOT / "config" / "skills" / "vegetable_cutting" / "cutting.json"
    configured = path or (local_config if local_config.is_file() else legacy_config)
    if configured.is_file():
        config = _deep_merge(config, json.loads(configured.read_text(encoding="utf-8")))
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    alignment = config["alignment"]
    planning = config["planning"]
    tracking = config["tracking"]
    handoff = config["handoff"]
    execution = config["execution"]
    registration = config["tool"]["observation_registration"]
    tool = config["tool"]
    alignment_gui_url = str(config["gui"]["alignment_url"]).rstrip("/")
    if alignment_gui_url != "http://127.0.0.1:8011":
        raise ValueError(
            "gui.alignment_url must remain the local stationary-alignment GUI"
        )
    spacing = float(planning["slice_spacing_mm"])
    if not 1.0 <= spacing <= 200.0:
        raise ValueError("planning.slice_spacing_mm must be in [1, 200]")
    if int(planning["minimum_cut_count"]) < 1:
        raise ValueError("planning.minimum_cut_count must be positive")
    if int(planning["maximum_cut_count"]) < int(planning["minimum_cut_count"]):
        raise ValueError("planning.maximum_cut_count is below minimum_cut_count")
    if not 0.0 <= float(tracking["minimum_mask_iou"]) <= 1.0:
        raise ValueError("tracking.minimum_mask_iou must be in [0, 1]")
    if bool(tracking["vlm_after_each_cut"]):
        raise ValueError("tracking.vlm_after_each_cut must remain false in this release")
    if not bool(alignment["require_same_camera_calibration_revision"]):
        raise ValueError(
            "alignment.require_same_camera_calibration_revision must remain true"
        )
    if not bool(alignment["require_reviewed_motion_usable"]):
        raise ValueError(
            "alignment.require_reviewed_motion_usable must remain true"
        )
    if not bool(alignment["fixed_camera"]):
        raise ValueError(
            "alignment.fixed_camera must remain true for vegetable cutting"
        )
    if not bool(alignment["stop_local_vio_after_transform_lock"]):
        raise ValueError(
            "the vegetable-cutting Skill must stop local VIO after locking "
            "the fixed camera transform"
        )
    if not bool(handoff["dry_run_required"]):
        raise ValueError("a reviewed plan must precede physical execution")
    if not bool(handoff["operator_takeover_required"]):
        raise ValueError("handoff.operator_takeover_required must remain true")
    if bool(handoff["allow_below_board_target"]):
        raise ValueError("below-board cutting targets are disabled in this release")
    if str(handoff["motion_backend"]) != "PRESS_MIT":
        raise ValueError("the execution backend must remain PRESS_MIT")
    nearby_reapproach_m = float(execution["nearby_review_reapproach_m"])
    if not 0.0 < nearby_reapproach_m <= float(
        execution["maximum_translation_per_commit_m"]
    ):
        raise ValueError(
            "execution nearby review reapproach must be positive and no "
            "larger than one bounded commit"
        )
    if float(handoff["minimum_approach_board_offset_mm"]) <= 0.0:
        raise ValueError("handoff minimum approach offset must be positive")
    first_review_offset_mm = float(
        handoff["first_cut_review_board_offset_mm"]
    )
    if not (
        float(handoff["minimum_approach_board_offset_mm"])
        <= first_review_offset_mm
        <= 500.0
    ):
        raise ValueError(
            "first-cut review offset must be at least the normal approach "
            "offset and no more than 500 mm"
        )
    if float(handoff["approach_clearance_above_vegetable_mm"]) <= 0.0:
        raise ValueError("handoff approach clearance must be positive")
    first_cut_alignment = handoff["first_cut_alignment"]
    if not 0.0 < float(first_cut_alignment["minimum_vlm_confidence"]) <= 1.0:
        raise ValueError("first-cut VLM confidence must be in (0, 1]")
    tolerance_mm = float(first_cut_alignment["no_correction_tolerance_mm"])
    maximum_translation_mm = float(first_cut_alignment["maximum_translation_mm"])
    if not 0.0 < tolerance_mm < maximum_translation_mm <= 500.0:
        raise ValueError("first-cut translation limits are invalid")
    rotation_tolerance_deg = float(
        first_cut_alignment["no_correction_rotation_tolerance_deg"]
    )
    maximum_rotation_deg = float(
        first_cut_alignment["maximum_rotation_deg"]
    )
    if not 0.0 < rotation_tolerance_deg < maximum_rotation_deg <= 30.0:
        raise ValueError("first-cut rotation limits are invalid")
    if not bool(first_cut_alignment["operator_confirmation_required"]):
        raise ValueError("first-cut correction must require operator confirmation")
    if str(tool["registration_mode"]) != "FIXED_HARD_MOUNT":
        raise ValueError("tool.registration_mode must be FIXED_HARD_MOUNT")
    if not bool(tool["vlm_blade_registration_optional"]):
        raise ValueError(
            "tool.vlm_blade_registration_optional must remain true in "
            "fixed hard-mount mode"
        )
    fixed_position = tool["fixed_controlled_frame_offset_xyz_m"]
    fixed_orientation = tool["fixed_controlled_frame_offset_rpy_rad"]
    if not isinstance(fixed_position, list) or len(fixed_position) != 3:
        raise ValueError(
            "tool.fixed_controlled_frame_offset_xyz_m must contain three values"
        )
    if not isinstance(fixed_orientation, list) or len(fixed_orientation) != 3:
        raise ValueError(
            "tool.fixed_controlled_frame_offset_rpy_rad must contain three values"
        )
    if not all(
        float("-inf") < float(value) < float("inf")
        for value in [*fixed_position, *fixed_orientation]
    ):
        raise ValueError("fixed tool offset values must be finite")
    if not 0.05 <= sum(float(value) ** 2 for value in fixed_position) ** 0.5 <= 0.8:
        raise ValueError("fixed tool offset distance must be in [0.05, 0.8] m")
    if not 0.0 <= float(tool["payload_mass_kg"]) <= 2.0:
        raise ValueError("tool.payload_mass_kg must be in [0, 2] kg")
    payload_com = tool["payload_com_tool_m"]
    if not isinstance(payload_com, list) or len(payload_com) != 3:
        raise ValueError("tool.payload_com_tool_m must contain three values")
    acting_point_mm = float(registration["acting_point_from_tip_mm"])
    minimum_edge_mm = float(registration["minimum_edge_length_mm"])
    maximum_edge_mm = float(registration["maximum_edge_length_mm"])
    minimum_width_mm = float(registration["minimum_blade_width_mm"])
    maximum_width_mm = float(registration["maximum_blade_width_mm"])
    if not 1.0 <= acting_point_mm <= 200.0:
        raise ValueError("tool acting_point_from_tip_mm must be in [1, 200]")
    if not 1.0 <= minimum_edge_mm < maximum_edge_mm <= 1000.0:
        raise ValueError("tool blade edge-length limits are invalid")
    if not 0.0 < minimum_width_mm < maximum_width_mm <= 300.0:
        raise ValueError("tool blade-width limits are invalid")
    if float(registration["maximum_local_depth_range_mm"]) <= 0.0:
        raise ValueError("tool maximum_local_depth_range_mm must be positive")
    if float(registration["spine_depth_interpolation_trigger_mm"]) <= 0.0:
        raise ValueError(
            "tool spine_depth_interpolation_trigger_mm must be positive"
        )
    if (
        float(registration["maximum_spine_depth_correction_mm"])
        <= float(registration["spine_depth_interpolation_trigger_mm"])
    ):
        raise ValueError(
            "maximum spine-depth correction must exceed its interpolation trigger"
        )
    for key in (
        "maximum_ray_to_tool_axis_miss_mm",
        "maximum_lateral_offset_magnitude_mm",
        "maximum_lateral_offset_disagreement_mm",
    ):
        if float(registration[key]) <= 0.0:
            raise ValueError(f"tool {key} must be positive")
    minimum_handle_step_px = float(
        registration["minimum_handle_anchor_from_junction_px"]
    )
    maximum_handle_step_px = float(
        registration["maximum_handle_anchor_from_junction_px"]
    )
    if not 0.0 < minimum_handle_step_px < maximum_handle_step_px:
        raise ValueError(
            "tool handle-anchor pixel step limits are invalid"
        )
    if not 0.05 <= float(registration["maximum_tool_to_acting_point_m"]) <= 1.0:
        raise ValueError("tool maximum_tool_to_acting_point_m must be in [0.05, 1.0]")
    forward_axis = registration["tool_forward_axis_xyz"]
    if (
        not isinstance(forward_axis, list)
        or len(forward_axis) != 3
        or sum(float(value) ** 2 for value in forward_axis) <= 1e-12
    ):
        raise ValueError("tool_forward_axis_xyz must be a non-zero 3-vector")
    if not -1.0 <= float(
        registration["minimum_tool_forward_axis_cosine"]
    ) <= 1.0:
        raise ValueError(
            "minimum_tool_forward_axis_cosine must be in [-1, 1]"
        )
    consistency = registration["consistency"]
    if not 1 <= int(consistency["required_observations"]) <= 10:
        raise ValueError("tool consistency observations must be in [1, 10]")
    for key in (
        "maximum_acting_point_deviation_mm",
        "maximum_edge_length_range_mm",
        "maximum_blade_width_range_mm",
        "maximum_orientation_deviation_deg",
    ):
        if float(consistency[key]) <= 0.0:
            raise ValueError(f"tool consistency {key} must be positive")
    if not bool(execution["enabled"]):
        raise ValueError("execution.enabled must remain true in this release")
    if str(execution["command_stream"]) != "robot_arm.primary.integrated.command":
        raise ValueError("execution command stream must target the Integrated provider")
    if str(execution["command_schema"]) != "physical_agent.arm_integrated_command":
        raise ValueError("execution command schema is invalid")
    controller_shadow = execution["controller_path_planning_shadow"]
    if not bool(controller_shadow["enabled"]):
        raise ValueError(
            "execution.controller_path_planning_shadow.enabled must remain true "
            "during Phase 2 evaluation"
        )
    if bool(controller_shadow["required"]):
        raise ValueError(
            "execution.controller_path_planning_shadow.required must remain false "
            "until shadow comparison is complete"
        )
    if str(controller_shadow["transport"]) != "DIRECT_HTTP":
        raise ValueError("controller path shadow must use direct HTTP")
    if bool(controller_shadow["physical_motion_authorized"]):
        raise ValueError("controller path shadow cannot authorize physical motion")
    maximum_commit = float(execution["maximum_translation_per_commit_m"])
    if not 0.005 <= maximum_commit <= 0.2:
        raise ValueError(
            "execution.maximum_translation_per_commit_m must be in [0.005, 0.2]"
        )
    maximum_orientation = float(execution["maximum_orientation_per_commit_deg"])
    if not 1.0 <= maximum_orientation <= 45.0:
        raise ValueError(
            "execution.maximum_orientation_per_commit_deg must be in [1, 45]"
        )
    initial_clearance_lift_m = float(
        execution["initial_clearance_lift_m"]
    )
    if not 0.05 <= initial_clearance_lift_m <= 0.30:
        raise ValueError(
            "execution.initial_clearance_lift_m must be in [0.05, 0.30]"
        )
    minimum_clearance_above_approach_m = float(
        execution["minimum_clearance_above_approach_m"]
    )
    if not 0.05 <= minimum_clearance_above_approach_m <= 0.30:
        raise ValueError(
            "execution.minimum_clearance_above_approach_m must be in "
            "[0.05, 0.30]"
        )
    if not 0.30 <= float(execution["maximum_clearance_z_m"]) <= 1.0:
        raise ValueError(
            "execution.maximum_clearance_z_m must be in [0.30, 1.0]"
        )
    cut_kp_multiplier = float(handoff["mit_kp_multiplier"])
    transfer_kp_multiplier = float(execution["transfer_kp_multiplier"])
    retract_kp_multiplier = float(execution["retract_kp_multiplier"])
    if not 1.0 <= cut_kp_multiplier <= 10.0:
        raise ValueError("handoff.mit_kp_multiplier must be in [1, 10]")
    if not 1.0 <= transfer_kp_multiplier <= 10.0:
        raise ValueError(
            "execution.transfer_kp_multiplier must be in [1, 10]"
        )
    if not 1.0 <= retract_kp_multiplier <= 10.0:
        raise ValueError(
            "execution.retract_kp_multiplier must be in [1, 10]"
        )
    post_cut_retract_m = float(execution["post_cut_retract_m"])
    if not 0.06 <= post_cut_retract_m <= 0.20:
        raise ValueError(
            "execution.post_cut_retract_m must be in [0.06, 0.20]"
        )
    first_cut_iteration_translation_mm = float(
        execution["first_cut_maximum_translation_per_iteration_mm"]
    )
    if not 5.0 <= first_cut_iteration_translation_mm <= 50.0:
        raise ValueError(
            "execution first-cut translation per iteration must be in "
            "[5, 50] mm"
        )
    first_cut_direction_cosine = float(
        execution["first_cut_minimum_consecutive_direction_cosine"]
    )
    if not -1.0 <= first_cut_direction_cosine <= 1.0:
        raise ValueError(
            "execution first-cut direction cosine must be in [-1, 1]"
        )
    first_cut_minimum_improvement_mm = float(
        execution["first_cut_minimum_pixel_servo_improvement_mm"]
    )
    if not 0.0 <= first_cut_minimum_improvement_mm <= 20.0:
        raise ValueError(
            "execution first-cut pixel-servo improvement must be in "
            "[0, 20] mm"
        )
    if int(execution["first_cut_maximum_automatic_corrections"]) not in range(1, 5):
        raise ValueError(
            "execution first-cut automatic corrections must be in [1, 4]"
        )
    first_cut_vlm_attempts = int(
        execution["first_cut_maximum_vlm_attempts"]
    )
    if first_cut_vlm_attempts not in range(2, 7):
        raise ValueError(
            "execution first-cut VLM attempts must be in [2, 6]"
        )
    if int(
        execution["first_cut_maximum_automatic_corrections"]
    ) >= first_cut_vlm_attempts:
        raise ValueError(
            "first-cut correction count must leave one final recapture"
        )


@dataclass(frozen=True)
class Settings:
    manager_url: str = os.getenv("MANAGER_URL", "http://127.0.0.1:7001")
    fabric_url: str = os.getenv("FABRIC_URL", "http://127.0.0.1:7002")
    integrated_url: str = os.getenv("INTEGRATED_ARM_URL", "http://127.0.0.1:8793")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_vision_model: str = os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-luna")

    @property
    def run_root(self) -> Path:
        path = SKILL_ROOT / "run"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def plan_root(self) -> Path:
        path = SKILL_ROOT / "config" / "plans"
        path.mkdir(parents=True, exist_ok=True)
        return path
