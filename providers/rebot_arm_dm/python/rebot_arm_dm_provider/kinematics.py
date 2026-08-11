"""Minimal NumPy forward kinematics for the official fixed-end reBot model."""
from __future__ import annotations

from typing import Any
import math
import numpy as np


def rot_x(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1,0,0],[0,c,-s],[0,s,c]], dtype=float)


def rot_y(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c,0,s],[0,1,0],[-s,0,c]], dtype=float)


def rot_z(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c,-s,0],[s,c,0],[0,0,1]], dtype=float)


def rpy_matrix(rpy: list[float] | tuple[float, float, float]) -> np.ndarray:
    roll, pitch, yaw = (float(v) for v in rpy)
    return rot_z(yaw) @ rot_y(pitch) @ rot_x(roll)


def transform(translation: Any = (0,0,0), rotation: np.ndarray | None = None) -> np.ndarray:
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = np.eye(3) if rotation is None else np.asarray(rotation, dtype=float)
    matrix[:3, 3] = np.asarray(translation, dtype=float)
    return matrix


def quaternion_xyzw(rotation: np.ndarray) -> list[float]:
    r = np.asarray(rotation, dtype=float)
    trace = float(np.trace(r))
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s; x = (r[2,1]-r[1,2])/s; y=(r[0,2]-r[2,0])/s; z=(r[1,0]-r[0,1])/s
    elif r[0,0] > r[1,1] and r[0,0] > r[2,2]:
        s = math.sqrt(1.0+r[0,0]-r[1,1]-r[2,2])*2
        w=(r[2,1]-r[1,2])/s; x=.25*s; y=(r[0,1]+r[1,0])/s; z=(r[0,2]+r[2,0])/s
    elif r[1,1] > r[2,2]:
        s = math.sqrt(1.0+r[1,1]-r[0,0]-r[2,2])*2
        w=(r[0,2]-r[2,0])/s; x=(r[0,1]+r[1,0])/s; y=.25*s; z=(r[1,2]+r[2,1])/s
    else:
        s = math.sqrt(1.0+r[2,2]-r[0,0]-r[1,1])*2
        w=(r[1,0]-r[0,1])/s; x=(r[0,2]+r[2,0])/s; y=(r[1,2]+r[2,1])/s; z=.25*s
    value = np.array([x,y,z,w], dtype=float)
    value /= max(np.linalg.norm(value), 1e-12)
    return value.tolist()


class RebotKinematics:
    """Forward kinematics from the official fixed-end URDF values."""

    def __init__(self, model: dict[str, Any]):
        self.model = model
        self.origins = [
            ((-0.00008416, 0.0, 0.08465), (0.0,0.0,0.0), (0,0,1)),
            ((0.020084, 0.031625, 0.05555), (-1.5708,0.0,0.0), (0,0,-1)),
            ((-0.264, 0.0, 0.0), (0.0,0.0,0.0), (0,0,1)),
            ((0.2426, -0.054, -0.001625), (0.0,0.0,0.0), (0,0,1)),
            ((0.078308, -0.0375, -0.03), (-1.5708,0.0,0.0), (0,0,1)),
            ((0.028008, 0.0, 0.04), (0.0,1.5708,0.0), (0,0,1)),
        ]
        fixed = model["fixed_tool"]
        self.fixed_tool = transform(fixed["translation_m"], rpy_matrix(fixed["rpy_rad"]))
        controlled = model.get("controlled_frame", {})
        self.controlled_tool = transform(
            controlled.get("translation_m", (0.0, 0.0, 0.0)),
            rpy_matrix(controlled.get("rpy_rad", (0.0, 0.0, 0.0))),
        )
        self.base_frame = str(model.get("frames", {}).get("base", "rebot_arm_base"))
        self.tool_frame = str(model.get("frames", {}).get("tool", "rebot_arm_tool"))

    @staticmethod
    def axis_rotation(axis: Any, angle: float) -> np.ndarray:
        axis = np.asarray(axis, dtype=float)
        axis = axis / np.linalg.norm(axis)
        x,y,z = axis; c=math.cos(angle); s=math.sin(angle); C=1-c
        return np.array([
            [x*x*C+c, x*y*C-z*s, x*z*C+y*s],
            [y*x*C+z*s, y*y*C+c, y*z*C-x*s],
            [z*x*C-y*s, z*y*C+x*s, z*z*C+c],
        ], dtype=float)

    def frames(self, positions_rad: Any) -> list[np.ndarray]:
        q = np.asarray(positions_rad, dtype=float)
        if q.shape != (7,):
            raise ValueError("positions_rad must contain seven values")
        frames = [np.eye(4, dtype=float)]
        current = frames[0]
        for i, (xyz, rpy, axis) in enumerate(self.origins):
            current = current @ transform(xyz, rpy_matrix(rpy)) @ transform(rotation=self.axis_rotation(axis, float(q[i])))
            frames.append(current.copy())
        frames.append(current @ self.fixed_tool)
        return frames

    def points(self, positions_rad: Any) -> list[list[float]]:
        frames = self.frames(positions_rad)
        controlled = frames[-1] @ self.controlled_tool
        return [
            frame[:3, 3].tolist()
            for frame in [*frames[:-1], controlled]
        ]

    def controlled_frame(self, positions_rad: Any) -> np.ndarray:
        """Return the assembly-selected free-space controlled frame."""

        return self.frames(positions_rad)[-1] @ self.controlled_tool

    def public_transforms(self, positions_rad: Any) -> list[dict[str, Any]]:
        frames = self.frames(positions_rad)
        frames[-1] = frames[-1] @ self.controlled_tool
        names = [self.base_frame, "link1", "link2", "link3", "link4", "link5", "link6", self.tool_frame]
        output=[]
        for index in range(1, len(frames)):
            relative = np.linalg.inv(frames[index-1]) @ frames[index]
            output.append({
                "parent_frame": names[index-1], "child_frame": names[index],
                "translation_m": relative[:3,3].tolist(), "rotation_xyzw": quaternion_xyzw(relative[:3,:3]),
                "is_static": False,
            })
        return output
