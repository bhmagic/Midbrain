from __future__ import annotations

from dataclasses import dataclass
import copy
import json
from pathlib import Path
from typing import Any
import uuid

from .profile import ModelProfile, canonical_sha256, file_sha256, load_profile_payload


DEFAULT_APPENDIX_KEY = "midbrain.skill.locate_arm_base.v1"


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"{label} resolves outside the Midbrain workspace") from error
    return resolved


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


@dataclass(frozen=True)
class ArmProfileRecord:
    selection_path: Path
    arm_provider_id: str
    model_path: Path
    model_id: str
    model_revision: str
    model_file_sha256: str
    appendix_key: str
    appendix: dict[str, Any]
    model_profile: ModelProfile

    def public(self, root: Path) -> dict[str, Any]:
        return {
            "selection_path": str(self.selection_path),
            "arm_provider_id": self.arm_provider_id,
            "arm_profile_path": str(self.model_path),
            "arm_profile_filename": self.model_path.name,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "model_file_sha256": self.model_file_sha256,
            "appendix_key": self.appendix_key,
            "appendix_sha256": canonical_sha256(self.appendix),
            "appendix": self.appendix,
            "cad": {
                "path": str(self.model_profile.mesh_path),
                "workspace_path": self.model_profile.mesh_path.relative_to(root).as_posix(),
                "filename": self.model_profile.mesh_path.name,
                "sha256": self.model_profile.mesh_sha256,
                "scale_to_m": self.model_profile.mesh_scale_to_m,
                "preview": (
                    {
                        "path": str(self.model_profile.mesh_preview_path),
                        "workspace_path": self.model_profile.mesh_preview_path.relative_to(root).as_posix(),
                        "filename": self.model_profile.mesh_preview_path.name,
                        "sha256": self.model_profile.mesh_preview_sha256,
                        "mesh_sha256": self.model_profile.mesh_sha256,
                        "role": "EXACT_FOUNDATIONPOSE_CAD_PREVIEW",
                        "consumers": ["DEVELOPER_INSPECTION"],
                        "description": "Static preview generated from the exact hash-bound OBJ sent to FoundationPose.",
                    }
                    if self.model_profile.mesh_preview_path is not None
                    else None
                ),
            },
            "reference_images": [
                {
                    "path": str(path),
                    "workspace_path": path.relative_to(root).as_posix(),
                    "filename": path.name,
                    "role": str(source.get("role") or "CAD_ORIENTATION_REFERENCE"),
                    "description": str(source.get("description") or ""),
                    "consumers": list(consumers),
                }
                for path, source, consumers in zip(
                    self.model_profile.reference_paths,
                    self.appendix.get("reference_images", []),
                    self.model_profile.reference_consumers,
                )
            ],
        }


class ArmProfileStore:
    """Resolve the active assembly-selected arm model and its Skill appendix."""

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        self.root = root.resolve()
        selection = config.get("arm_profile_selection")
        if not isinstance(selection, dict):
            raise ValueError("arm_profile_selection configuration is required")
        path = Path(
            str(
                selection.get("selection_path")
                or "config/robot_assemblies/primary_manipulator.json"
            )
        )
        self.selection_path = _inside(
            self.root,
            path if path.is_absolute() else self.root / path,
            "robot assembly selection",
        )
        self.appendix_key = str(
            selection.get("appendix_key") or DEFAULT_APPENDIX_KEY
        ).strip()
        if not self.appendix_key:
            raise ValueError("arm profile appendix key must be non-empty")

    def load(self) -> ArmProfileRecord:
        selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        if selection.get("schema") != "midbrain.robot_assembly_selection":
            raise ValueError("selected robot assembly schema is unsupported")
        provider = selection.get("arm_provider")
        profiles = selection.get("profiles")
        if not isinstance(provider, dict) or not isinstance(profiles, dict):
            raise ValueError("selected robot assembly lacks arm Provider profiles")
        arm_provider_id = str(provider.get("provider_id") or "").strip()
        if not arm_provider_id:
            raise ValueError("selected robot assembly lacks an arm Provider ID")
        reference = profiles.get("arm_model")
        if not isinstance(reference, dict):
            raise ValueError("selected robot assembly lacks an arm_model profile")
        provider_root = _inside(
            self.root,
            self.root / str(provider.get("provider_root") or ""),
            "arm Provider root",
        )
        model_path = _inside(
            self.root,
            provider_root / str(reference.get("relative_path") or ""),
            "selected arm profile",
        )
        document = json.loads(model_path.read_text(encoding="utf-8"))
        if document.get("schema") != reference.get("expected_schema"):
            raise ValueError("selected arm profile schema does not match the assembly")
        model_id = str(document.get("model_id") or "")
        model_revision = str(document.get("model_revision") or "")
        if model_id != str(reference.get("expected_id") or ""):
            raise ValueError("selected arm profile identity does not match the assembly")
        if model_revision != str(reference.get("expected_revision") or ""):
            raise ValueError("selected arm profile revision does not match the assembly")
        digest = file_sha256(model_path)
        expected_digest = reference.get("sha256")
        if expected_digest is not None and digest != str(expected_digest).lower():
            raise ValueError("selected arm profile digest does not match the assembly")
        appendix_root = document.get("appendix")
        if not isinstance(appendix_root, dict):
            raise ValueError("selected arm profile has no flexible appendix object")
        appendix = appendix_root.get(self.appendix_key)
        if not isinstance(appendix, dict):
            raise ValueError(
                f"selected arm profile lacks appendix {self.appendix_key!r}"
            )
        return ArmProfileRecord(
            selection_path=self.selection_path,
            arm_provider_id=arm_provider_id,
            model_path=model_path,
            model_id=model_id,
            model_revision=model_revision,
            model_file_sha256=digest,
            appendix_key=self.appendix_key,
            appendix=appendix,
            model_profile=load_profile_payload(appendix, self.root),
        )

    def save_appendix(self, appendix: dict[str, Any]) -> ArmProfileRecord:
        if not isinstance(appendix, dict):
            raise ValueError("locate_arm_base appendix must be a JSON object")
        appendix = copy.deepcopy(appendix)
        mesh = appendix.get("mesh")
        references = appendix.get("reference_images")
        if not isinstance(mesh, dict) or not isinstance(references, list):
            raise ValueError("locate_arm_base appendix requires mesh and reference_images")
        mesh_path = Path(str(mesh.get("path") or ""))
        mesh_path = _inside(
            self.root,
            mesh_path if mesh_path.is_absolute() else self.root / mesh_path,
            "arm-base CAD mesh",
        )
        if not mesh_path.is_file():
            raise ValueError(f"arm-base CAD mesh is unavailable: {mesh_path}")
        mesh["sha256"] = file_sha256(mesh_path)
        if not references:
            raise ValueError("locate_arm_base appendix requires a reference image")
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("arm-base reference records must be objects")
            path = Path(str(reference.get("path") or ""))
            path = _inside(
                self.root,
                path if path.is_absolute() else self.root / path,
                "arm-base reference image",
            )
            if not path.is_file():
                raise ValueError(f"arm-base reference image is unavailable: {path}")
            reference["sha256"] = file_sha256(path)
            reference.setdefault("role", "CAD_ORIENTATION_REFERENCE")
        load_profile_payload(appendix, self.root)
        current = self.load()
        document = json.loads(current.model_path.read_text(encoding="utf-8"))
        appendix_root = document.setdefault("appendix", {})
        if not isinstance(appendix_root, dict):
            raise ValueError("selected arm profile appendix must remain an object")
        appendix_root[self.appendix_key] = appendix
        _write_json_atomic(current.model_path, document)
        return self.load()
