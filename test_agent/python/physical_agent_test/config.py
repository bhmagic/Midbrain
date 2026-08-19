from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    configured = os.getenv("PHYSICAL_AGENT_ROOT")
    if configured:
        return Path(configured).resolve()
    return package_root().parent


PACKAGE_ROOT = package_root()
WORKSPACE_ROOT = workspace_root()
load_dotenv(WORKSPACE_ROOT / "config" / "api_keys.env", override=False)
load_dotenv(WORKSPACE_ROOT / "config" / "system.env", override=False)


def _csv_environment(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            item.strip() for item in os.getenv(name, default).split(",")
        )
        if value
    )


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = WORKSPACE_ROOT
    package_root: Path = PACKAGE_ROOT
    manager_url: str = os.getenv("MANAGER_URL", "http://127.0.0.1:7001")
    fabric_url: str = os.getenv("FABRIC_URL", "http://127.0.0.1:7002")
    basic_controller_url: str = os.getenv(
        "BASIC_CONTROLLER_URL",
        "http://127.0.0.1:8791",
    )
    basic_operation_timeout_s: float = float(
        os.getenv("BASIC_OPERATION_TIMEOUT_S", "60")
    )
    integrated_controller_url: str = os.getenv(
        "INTEGRATED_CONTROLLER_URL",
        "http://127.0.0.1:8793",
    )
    contact_controller_url: str = os.getenv(
        "CONTACT_CONTROLLER_URL",
        "http://127.0.0.1:8794",
    )
    sam2_scene_tracker_url: str = os.getenv(
        "SAM2_SCENE_TRACKER_URL",
        "http://127.0.0.1:7105",
    )
    integrated_preview_timeout_s: float = float(
        os.getenv("INTEGRATED_PREVIEW_TIMEOUT_S", "5")
    )
    ui_host: str = os.getenv("UI_HOST", "127.0.0.1")
    ui_port: int = int(os.getenv("UI_PORT", "8000"))
    openai_model: str = os.getenv(
        "OPENAI_AGENT_MODEL",
        "gemini-3.7-flash",
    )
    openai_agent_models: tuple[str, ...] = _csv_environment(
        "OPENAI_AGENT_MODELS",
        "gemini-3.7-flash,gpt-5.6-terra,gpt-5.6-sol,gpt-5.6-luna",
    )
    openai_agent_reasoning_effort: str = os.getenv(
        "OPENAI_AGENT_REASONING_EFFORT",
        "medium",
    ).strip().lower()
    openai_agent_tool_choice: str = os.getenv(
        "OPENAI_AGENT_TOOL_CHOICE",
        "auto",
    )
    openai_agent_max_turns: int = int(
        os.getenv("OPENAI_AGENT_MAX_TURNS", "16")
    )
    openai_agent_session_history_items: int = int(
        os.getenv("OPENAI_AGENT_SESSION_HISTORY_ITEMS", "32")
    )
    limited_graph_fast_text_model: str | None = (
        os.getenv("LIMITED_GRAPH_FAST_TEXT_MODEL", "gpt-5.6-luna").strip()
        or None
    )
    limited_graph_enable_vision_router: bool = os.getenv(
        "LIMITED_GRAPH_ENABLE_VISION_ROUTER",
        "true",
    ).lower() in {"1", "true", "yes", "on"}
    agent_run_journal_max_runs: int = int(
        os.getenv("AGENT_RUN_JOURNAL_MAX_RUNS", "500")
    )
    agent_run_journal_max_events_per_run: int = int(
        os.getenv("AGENT_RUN_JOURNAL_MAX_EVENTS_PER_RUN", "2048")
    )
    agent_run_journal_retention_days: float = float(
        os.getenv("AGENT_RUN_JOURNAL_RETENTION_DAYS", "30")
    )
    skill_result_detail_max_results: int = int(
        os.getenv("SKILL_RESULT_DETAIL_MAX_RESULTS", "1000")
    )
    skill_result_detail_max_result_bytes: int = int(
        os.getenv("SKILL_RESULT_DETAIL_MAX_RESULT_BYTES", "1048576")
    )
    skill_result_detail_max_total_bytes: int = int(
        os.getenv("SKILL_RESULT_DETAIL_MAX_TOTAL_BYTES", "67108864")
    )
    skill_result_detail_retention_days: float = float(
        os.getenv("SKILL_RESULT_DETAIL_RETENTION_DAYS", "7")
    )
    authorization_signing_secret: str = os.getenv(
        "MIDBRAIN_AUTHORIZATION_SECRET",
        "",
    )
    review_auth_secret: str = os.getenv(
        "MIDBRAIN_REVIEW_AUTH_SECRET",
        "",
    )
    gemini_model: str = os.getenv(
        "GEMINI_ROBOTICS_MODEL",
        "gemini-robotics-er-2-preview",
    )
    head_camera_provider_id: str = os.getenv("HEAD_CAMERA_PROVIDER_ID", "camera.femto_bolt")
    camera_first_frame_timeout_s: float = float(
        os.getenv("CAMERA_FIRST_FRAME_TIMEOUT_S", "12")
    )
    camera_skill_capture_attempts: int = int(
        os.getenv("CAMERA_SKILL_CAPTURE_ATTEMPTS", "2")
    )
    camera_skill_retry_backoff_s: float = float(
        os.getenv("CAMERA_SKILL_RETRY_BACKOFF_S", "0.25")
    )
    provider_hot_readiness_timeout_s: float = float(
        os.getenv("PROVIDER_HOT_READINESS_TIMEOUT_S", "45")
    )
    scene_mapping_readiness_timeout_s: float = float(
        os.getenv("SCENE_MAPPING_READINESS_TIMEOUT_S", "180")
    )
    local_vio_provider_id: str = os.getenv("LOCAL_VIO_PROVIDER_ID", "localization.local_vio")
    space_cognition_timeout_s: float = float(os.getenv("SPACE_COGNITION_TIMEOUT_S", "45"))
    auto_initialize_space_cognition: bool = os.getenv("AUTO_INITIALIZE_SPACE_COGNITION", "true").lower() in {"1", "true", "yes", "on"}
    point_cloud_retention_s: float = float(os.getenv("POINT_CLOUD_RETENTION_S", "10"))
    point_cloud_sample_stride: int = int(os.getenv("POINT_CLOUD_SAMPLE_STRIDE", "10"))
    point_cloud_hz: float = float(os.getenv("POINT_CLOUD_HZ", "5"))
    point_cloud_max_points: int = int(os.getenv("POINT_CLOUD_MAX_POINTS", "180000"))
    phase4_agent_run_timeout_s: float = float(
        os.getenv("PHASE4_AGENT_RUN_TIMEOUT_S", "300")
    )
    stationary_calibration_timeout_s: float = float(
        os.getenv("STATIONARY_CALIBRATION_TIMEOUT_S", "600")
    )
    phase5_spatial_binding_mode: str = os.getenv(
        "PHASE5_SPATIAL_BINDING_MODE",
        "SHADOW",
    ).strip().upper()
    phase5_spatial_generic_route_mode: str = os.getenv(
        "PHASE5_SPATIAL_GENERIC_ROUTE_MODE",
        "SHADOW",
    ).strip().upper()
    head_camera_frame: str = os.getenv(
        "HEAD_CAMERA_FRAME",
        "femto_bolt_color_optical_frame",
    )
    arm_transform_provider_id: str = os.getenv(
        "ARM_TRANSFORM_PROVIDER_ID",
        "robot_arm.rebot_dm",
    )
    arm_base_frame: str = os.getenv("ARM_BASE_FRAME", "rebot_arm_base")
    arm_tool_frame: str = os.getenv("ARM_TOOL_FRAME", "rebot_arm_tool")
    agent_skill_defer_loading: bool = os.getenv(
        "AGENT_SKILL_DEFER_LOADING",
        "true",
    ).lower() in {"1", "true", "yes", "on"}
    phase4_eligible_tools: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv(
            "PHASE4_ELIGIBLE_TOOLS",
            (
                "identify_pointed_object,"
                "analyze_visual_scene,"
                "establish_world_axis,"
                "locate_item,"
                "locate_effector_front,"
                "plan_no_contact_item_approach,"
                "inspect_arm_semantic_scene,"
                "derive_fabric_world_point,"
                "translate_fabric_direction_to_world,"
                "translate_fabric_pose_to_world,"
                "offset_world_point,"
                "verify_rgbd_image_alignment,"
                "reinitialize_space_cognition,"
                "refine_arm_root_translation,"
                "slice_with_blade,"
                "run_limited_graph"
            ),
        ).split(",")
        if item.strip()
    )

    @property
    def screenshot_dir(self) -> Path:
        path = self.package_root / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def scene_policy_state_path(self) -> Path:
        path = self.package_root / "run"
        path.mkdir(parents=True, exist_ok=True)
        return path / "scene_segmentation_policy.v1.json"

    @property
    def replay_bundle_dir(self) -> Path:
        path = self.package_root / "replay_bundles"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def agent_run_journal_path(self) -> Path:
        configured = os.getenv("AGENT_RUN_JOURNAL_PATH", "").strip()
        if configured:
            return Path(configured).resolve()
        return (
            self.workspace_root
            / "test_agent"
            / "run"
            / "agent_run_journal.v1.sqlite3"
        )

    @property
    def skill_result_detail_store_path(self) -> Path:
        configured = os.getenv("SKILL_RESULT_DETAIL_STORE_PATH", "").strip()
        if configured:
            return Path(configured).resolve()
        return (
            self.workspace_root
            / "test_agent"
            / "run"
            / "skill_result_details.v1.sqlite3"
        )
