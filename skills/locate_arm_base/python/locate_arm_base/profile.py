from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .math3d import matrix4, quaternion_xyzw, z_rotation


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    def typed(item: object) -> object:
        if item is None:
            return ["null"]
        if isinstance(item, bool):
            return ["boolean", "1" if item else "0"]
        if isinstance(item, int):
            return ["integer", str(item)]
        if isinstance(item, float):
            if not __import__("math").isfinite(item):
                raise ValueError("canonical JSON cannot contain non-finite numbers")
            token = json.dumps(item, allow_nan=False).lower()
            sign = "-" if token.startswith("-") else ""
            unsigned = token[1:] if sign else token
            mantissa, separator, exponent_text = unsigned.partition("e")
            exponent = int(exponent_text) if separator else 0
            whole, dot, fraction = mantissa.partition(".")
            exponent -= len(fraction) if dot else 0
            significant = (whole + fraction).lstrip("0")
            if not significant:
                normalized = "0e+0"
            else:
                digits = significant.rstrip("0")
                exponent += len(significant) - len(digits)
                normalized = f"{sign}{digits}e{exponent:+d}"
            return ["decimal", normalized]
        if isinstance(item, str):
            return ["utf8", item.encode("utf-8").hex()]
        if isinstance(item, list):
            return ["array", [typed(entry) for entry in item]]
        if isinstance(item, tuple):
            return ["array", [typed(entry) for entry in item]]
        if isinstance(item, dict):
            entries = [[str(key).encode("utf-8").hex(), typed(entry)] for key, entry in item.items()]
            entries.sort(key=lambda entry: entry[0])
            return ["object", entries]
        raise TypeError(f"unsupported canonical JSON type {type(item).__name__}")

    payload = json.dumps(typed(value), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class OrientationCandidate:
    candidate_id: str
    axis: str
    degrees: int
    matrix: Any
    rotation_xyzw: list[float]


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    semantic_frame: str
    mesh_path: Path
    mesh_sha256: str
    mesh_scale_to_m: float
    mesh_preview_path: Path | None
    mesh_preview_sha256: str | None
    reference_paths: tuple[Path, ...]
    segmentation_reference_paths: tuple[Path, ...]
    orientation_reference_paths: tuple[Path, ...]
    reference_consumers: tuple[tuple[str, ...], ...]
    reference_set_sha256: str
    vlm_seed_guidance: str
    centered_mesh_from_arm_base: Any
    candidates: tuple[OrientationCandidate, ...]
    profile_sha256: str


def _workspace_asset(path_value: object, root: Path, label: str) -> Path:
    path = Path(str(path_value))
    path = path if path.is_absolute() else root / path
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} must remain inside the Midbrain workspace") from error
    if not path.is_file():
        raise ValueError(f"{label} is unavailable: {path}")
    return path


def load_profile_payload(payload: dict[str, Any], root: Path) -> ModelProfile:
    if payload.get("schema") != "midbrain.skill.locate_arm_base.model_profile":
        raise ValueError("arm profile appendix has an unsupported locate_arm_base schema")
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("arm profile appendix has an unsupported locate_arm_base version")
    mesh = payload["mesh"]
    mesh_path = _workspace_asset(mesh["path"], root, "arm-base CAD mesh")
    actual_mesh_hash = file_sha256(mesh_path)
    if actual_mesh_hash != str(mesh["sha256"]).lower():
        raise ValueError("model profile mesh SHA-256 does not match its asset")
    mesh_preview_path: Path | None = None
    mesh_preview_sha256: str | None = None
    preview = mesh.get("preview")
    if preview is not None:
        if not isinstance(preview, dict):
            raise ValueError("arm-base CAD preview must be an object")
        mesh_preview_path = _workspace_asset(
            preview["path"], root, "arm-base CAD preview"
        )
        mesh_preview_sha256 = file_sha256(mesh_preview_path)
        if mesh_preview_sha256 != str(preview["sha256"]).lower():
            raise ValueError("model profile CAD preview SHA-256 does not match its asset")
        if str(preview.get("mesh_sha256") or "").lower() != actual_mesh_hash:
            raise ValueError("model profile CAD preview is not bound to the selected mesh")
    reference_paths: list[Path] = []
    segmentation_reference_paths: list[Path] = []
    orientation_reference_paths: list[Path] = []
    reference_consumers: list[tuple[str, ...]] = []
    reference_records: list[dict[str, str]] = []
    for reference in payload["reference_images"]:
        reference_path = _workspace_asset(
            reference["path"], root, "arm-base reference image"
        )
        actual_hash = file_sha256(reference_path)
        if actual_hash != str(reference["sha256"]).lower():
            raise ValueError("model profile reference SHA-256 does not match its asset")
        consumers_value = reference.get("consumers")
        consumers = tuple(
            str(value).strip().upper()
            for value in (
                consumers_value
                if isinstance(consumers_value, list)
                else ["VLM_SEED_LOCALIZATION", "VLM_ORIENTATION_SELECTION"]
            )
            if str(value).strip()
        )
        allowed_consumers = {
            "VLM_SEED_LOCALIZATION",
            "VLM_ORIENTATION_SELECTION",
        }
        if not consumers or any(value not in allowed_consumers for value in consumers):
            raise ValueError("arm-base reference image has unsupported consumers")
        reference_paths.append(reference_path)
        reference_consumers.append(consumers)
        if "VLM_SEED_LOCALIZATION" in consumers:
            segmentation_reference_paths.append(reference_path)
        if "VLM_ORIENTATION_SELECTION" in consumers:
            orientation_reference_paths.append(reference_path)
        reference_records.append(
            {
                "sha256": actual_hash,
                "role": str(reference["role"]),
                "consumers": ",".join(consumers),
            }
        )
    if not segmentation_reference_paths:
        raise ValueError("model profile requires a VLM seed-localization reference")
    if not orientation_reference_paths:
        raise ValueError("model profile requires a VLM orientation-selection reference")
    vlm_seed_guidance = str(payload.get("vlm_seed_guidance") or "").strip()
    if len(vlm_seed_guidance) > 2000:
        raise ValueError("arm-base VLM seed guidance cannot exceed 2000 characters")
    candidates: list[OrientationCandidate] = []
    for entry in payload["orientation_candidates"]:
        axis = str(entry["axis"]).upper()
        if axis != "Z":
            raise ValueError("v1 model profiles support bounded local-Z candidates only")
        matrix = z_rotation(int(entry["degrees"]))
        candidates.append(
            OrientationCandidate(
                candidate_id=str(entry["candidate_id"]),
                axis=axis,
                degrees=int(entry["degrees"]),
                matrix=matrix,
                rotation_xyzw=quaternion_xyzw(matrix[:3, :3]),
            )
        )
    if not candidates or len({value.candidate_id for value in candidates}) != len(candidates):
        raise ValueError("orientation candidates must have unique IDs")
    return ModelProfile(
        profile_id=str(payload["profile_id"]),
        semantic_frame=str(payload["semantic_frame"]),
        mesh_path=mesh_path,
        mesh_sha256=actual_mesh_hash,
        mesh_scale_to_m=float(mesh["scale_to_m"]),
        mesh_preview_path=mesh_preview_path,
        mesh_preview_sha256=mesh_preview_sha256,
        reference_paths=tuple(reference_paths),
        segmentation_reference_paths=tuple(segmentation_reference_paths),
        orientation_reference_paths=tuple(orientation_reference_paths),
        reference_consumers=tuple(reference_consumers),
        reference_set_sha256=canonical_sha256(reference_records),
        vlm_seed_guidance=vlm_seed_guidance,
        centered_mesh_from_arm_base=matrix4(
            payload["centered_mesh_from_arm_base"], "centered_mesh_from_arm_base"
        ),
        candidates=tuple(candidates),
        profile_sha256=canonical_sha256(payload),
    )


def load_profile(path: Path, root: Path) -> ModelProfile:
    profile_path = path if path.is_absolute() else root / path
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("locate_arm_base profile must be a JSON object")
    return load_profile_payload(payload, root)
