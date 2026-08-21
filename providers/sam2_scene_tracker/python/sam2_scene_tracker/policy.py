from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


SUPPORTED_TYPES = {"KEEP_OUT", "PUSHABLE", "WORK_OBJECT"}
TYPE_ALIASES = {
    "OBS": "KEEP_OUT",
    "OBSTACLE": "KEEP_OUT",
    "KEEP_OUT": "KEEP_OUT",
    "PUSHABLE": "PUSHABLE",
    "WORKPIECE": "WORK_OBJECT",
    "WORK_PIECE": "WORK_OBJECT",
    "WORK_OBJECT": "WORK_OBJECT",
}


@dataclass(frozen=True)
class SceneObjectDescription:
    object_id: str
    description: str
    object_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "object_id": self.object_id,
            "description": self.description,
            "type": self.object_type,
        }


@dataclass(frozen=True)
class SceneSegmentationPolicy:
    policy_id: str
    revision: str
    objects: tuple[SceneObjectDescription, ...]
    arm_description: str

    @property
    def blocking_objects(self) -> tuple[SceneObjectDescription, ...]:
        return tuple(
            value for value in self.objects if value.object_type == "KEEP_OUT"
        )

    @property
    def identity(self) -> str:
        return f"{self.policy_id}:{self.revision}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "policy_id": self.policy_id,
            "revision": self.revision,
            "arm_description": self.arm_description,
            "objects": [value.as_dict() for value in self.objects],
            "unclaimed_visible_type": "PUSHABLE",
            "pushable_collision_policy": "IGNORE",
        }


def _stable_revision(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def parse_policy(payload: dict[str, Any]) -> SceneSegmentationPolicy:
    """Validate an explicit user/upstream semantic segmentation policy."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if int(data.get("contract_version") or 0) != 1:
        raise ValueError("segmentation policy contract_version must be 1")
    policy_id = str(data.get("policy_id") or "").strip()
    if not policy_id:
        raise ValueError("segmentation policy requires policy_id")
    arm_description = str(
        data.get("arm_description")
        or (
            "the complete robot arm, including base, links, joints, cables, "
            "wrist, gripper, and attached tooling"
        )
    ).strip()
    if not arm_description:
        raise ValueError("segmentation policy requires arm_description")

    raw_objects = data.get("objects")
    if not isinstance(raw_objects, list):
        raise ValueError("segmentation policy objects must be a list")
    objects: list[SceneObjectDescription] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_objects):
        if not isinstance(raw, dict):
            raise ValueError(f"segmentation policy object {index} must be an object")
        object_id = str(raw.get("object_id") or "").strip()
        description = str(raw.get("description") or "").strip()
        requested_type = str(raw.get("type") or "").strip().upper()
        object_type = TYPE_ALIASES.get(requested_type)
        if not object_id or object_id in seen:
            raise ValueError("segmentation policy object_id values must be unique")
        if object_type not in SUPPORTED_TYPES:
            raise ValueError(f"unsupported segmentation object type {requested_type!r}")
        if not description:
            raise ValueError(
                f"segmentation object {object_id!r} requires a user/upstream description"
            )
        seen.add(object_id)
        objects.append(SceneObjectDescription(object_id, description, object_type))

    canonical = {
        "policy_id": policy_id,
        "arm_description": arm_description,
        "objects": [value.as_dict() for value in objects],
    }
    revision = str(data.get("revision") or _stable_revision(canonical)).strip()
    if not revision:
        raise ValueError("segmentation policy revision is invalid")
    return SceneSegmentationPolicy(
        policy_id=policy_id,
        revision=revision,
        objects=tuple(objects),
        arm_description=arm_description,
    )
