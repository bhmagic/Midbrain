"""CAD object model registry for generic object-pose sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .math3d import as_transform


@dataclass(frozen=True)
class ObjectModel:
    """One CAD model and its semantic-frame metadata."""

    model_id: str
    mesh_path: Path
    semantic_frame: str
    mesh_from_semantic: np.ndarray
    scale_to_m: float
    symmetry: dict[str, Any]
    enabled: bool
    revision: str
    role: str = "generic_object"
    description: str = ""
    default_child_frame: str | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "mesh_path": str(self.mesh_path),
            "semantic_frame": self.semantic_frame,
            "mesh_from_semantic": self.mesh_from_semantic.reshape(-1).tolist(),
            "scale_to_m": self.scale_to_m,
            "symmetry": self.symmetry,
            "role": self.role,
            "description": self.description,
            "default_child_frame": self.default_child_frame,
            "enabled": self.enabled,
            "revision": self.revision,
            "mesh_exists": self.mesh_path.is_file(),
        }


class ObjectModelRegistry:
    """Load and validate models from a JSON registry."""

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path.resolve()
        self.revision = ""
        self.models: dict[str, ObjectModel] = {}
        self.reload()

    def reload(self) -> None:
        payload = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("model registry root must be a JSON object")
        entries = payload.get("models")
        if not isinstance(entries, list):
            raise ValueError("model registry must contain a models array")
        revision = str(payload.get("revision") or "unversioned")
        loaded: dict[str, ObjectModel] = {}
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise ValueError(f"models[{index}] must be a JSON object")
            model_id = str(raw.get("model_id") or "").strip()
            if not model_id:
                raise ValueError(f"models[{index}].model_id is required")
            if model_id in loaded:
                raise ValueError(f"duplicate model_id: {model_id}")
            mesh_value = str(raw.get("mesh_path") or "").strip()
            if not mesh_value:
                raise ValueError(f"models[{index}].mesh_path is required")
            mesh_path = Path(mesh_value)
            if not mesh_path.is_absolute():
                mesh_path = (self.registry_path.parent / mesh_path).resolve()
            semantic_frame = str(raw.get("semantic_frame") or model_id).strip()
            transform = as_transform(
                raw.get("mesh_from_semantic", np.eye(4).reshape(-1).tolist()),
                field_name=f"models[{index}].mesh_from_semantic",
            )
            scale_to_m = float(raw.get("scale_to_m", 1.0))
            if not np.isfinite(scale_to_m) or scale_to_m <= 0.0:
                raise ValueError(f"models[{index}].scale_to_m must be positive")
            symmetry = raw.get("symmetry") or {"type": "NONE"}
            if not isinstance(symmetry, dict):
                raise ValueError(f"models[{index}].symmetry must be an object")
            role = str(raw.get("role") or "generic_object").strip()
            if not role:
                role = "generic_object"
            description = str(raw.get("description") or "").strip()
            default_child_frame_value = raw.get("default_child_frame")
            default_child_frame = (
                str(default_child_frame_value).strip()
                if default_child_frame_value is not None
                else None
            )
            if default_child_frame == "":
                default_child_frame = None
            loaded[model_id] = ObjectModel(
                model_id=model_id,
                mesh_path=mesh_path,
                semantic_frame=semantic_frame,
                mesh_from_semantic=transform,
                scale_to_m=scale_to_m,
                symmetry=symmetry,
                role=role,
                description=description,
                default_child_frame=default_child_frame,
                enabled=bool(raw.get("enabled", True)),
                revision=str(raw.get("revision") or revision),
            )
        self.revision = revision
        self.models = loaded

    def get(self, model_id: str, *, require_mesh: bool = True) -> ObjectModel:
        try:
            model = self.models[model_id]
        except KeyError as error:
            raise KeyError(f"unknown object model: {model_id}") from error
        if not model.enabled:
            raise ValueError(f"object model is disabled: {model_id}")
        if require_mesh and not model.mesh_path.is_file():
            raise FileNotFoundError(f"mesh does not exist: {model.mesh_path}")
        return model

    def public_payload(self) -> dict[str, Any]:
        return {
            "registry_path": str(self.registry_path),
            "revision": self.revision,
            "models": [self.models[key].public_payload() for key in sorted(self.models)],
        }
