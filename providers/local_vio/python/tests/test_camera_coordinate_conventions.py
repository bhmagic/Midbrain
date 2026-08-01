from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PROVIDER_PATH = Path(__file__).resolve().parents[2] / "provider.py"
SPEC = importlib.util.spec_from_file_location(
    "local_vio_provider_entry_coordinates",
    PROVIDER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


OPTICAL = "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"


def test_vio_accepts_explicit_optical_input_conventions() -> None:
    MODULE.require_camera_coordinate_conventions(
        {
            "coordinate_conventions": {
                "color": OPTICAL,
                "infrared": OPTICAL,
            }
        },
        {
            "coordinate_conventions": {
                "rgb": OPTICAL,
                "aligned_depth": OPTICAL,
            }
        },
        ir_enabled=True,
    )


def test_vio_rejects_anonymous_camera_coordinates() -> None:
    with pytest.raises(RuntimeError, match="explicit native camera optical"):
        MODULE.require_camera_coordinate_conventions(
            {"coordinate_conventions": {"color": OPTICAL}},
            {
                "coordinate_conventions": {
                    "rgb": OPTICAL,
                }
            },
            ir_enabled=True,
        )


def test_vio_does_not_require_ir_metadata_when_ir_is_disabled() -> None:
    MODULE.require_camera_coordinate_conventions(
        {"coordinate_conventions": {"color": OPTICAL}},
        {
            "coordinate_conventions": {
                "rgb": OPTICAL,
                "aligned_depth": OPTICAL,
            }
        },
        ir_enabled=False,
    )
