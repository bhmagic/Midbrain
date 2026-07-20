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


@dataclass(frozen=True)
class Settings:
    workspace_root: Path = WORKSPACE_ROOT
    package_root: Path = PACKAGE_ROOT
    manager_url: str = os.getenv("MANAGER_URL", "http://127.0.0.1:7001")
    fabric_url: str = os.getenv("FABRIC_URL", "http://127.0.0.1:7002")
    ui_host: str = os.getenv("UI_HOST", "127.0.0.1")
    ui_port: int = int(os.getenv("UI_PORT", "8000"))
    openai_model: str = os.getenv("OPENAI_AGENT_MODEL", "gpt-5-mini")
    gemini_model: str = os.getenv("GEMINI_ROBOTICS_MODEL", "gemini-robotics-er-1.6-preview")
    head_camera_provider_id: str = os.getenv("HEAD_CAMERA_PROVIDER_ID", "camera.femto_bolt")
    local_vio_provider_id: str = os.getenv("LOCAL_VIO_PROVIDER_ID", "localization.local_vio")
    space_cognition_timeout_s: float = float(os.getenv("SPACE_COGNITION_TIMEOUT_S", "45"))
    auto_initialize_space_cognition: bool = os.getenv("AUTO_INITIALIZE_SPACE_COGNITION", "true").lower() in {"1", "true", "yes", "on"}
    point_cloud_retention_s: float = float(os.getenv("POINT_CLOUD_RETENTION_S", "10"))
    point_cloud_sample_stride: int = int(os.getenv("POINT_CLOUD_SAMPLE_STRIDE", "10"))
    point_cloud_hz: float = float(os.getenv("POINT_CLOUD_HZ", "5"))
    point_cloud_max_points: int = int(os.getenv("POINT_CLOUD_MAX_POINTS", "180000"))

    @property
    def screenshot_dir(self) -> Path:
        path = self.package_root / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path
