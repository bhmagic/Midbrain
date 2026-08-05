from __future__ import annotations

import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOTS = (
    "providers/rebot_arm_dm/python",
    "providers/rebot_arm_integrated/python",
    "skills/locate-effector-front/python",
    "skills/observe_pointed_object/python",
    "skills/register_tool_to_control_frame/python",
    "skills/spatial_registration_rgbd/python",
    "skills/stationary_world_arm_alignment/python",
)

for relative_path in reversed(SOURCE_ROOTS):
    source_root = str(WORKSPACE_ROOT / relative_path)
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
