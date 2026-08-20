"""Resolve and validate the selected robot assembly without Provider-specific path copies."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any


class AssemblyConfigurationError(ValueError):
    """The selected assembly is incomplete, inconsistent, or unsafe to activate."""


_IDENTITY_FIELDS = {
    "physical_agent.robot_arm_model": ("model_id", "model_revision"),
    "physical_agent.robot_arm_calibration": ("model_id", "calibration_revision"),
    "midbrain.mounted_effector_profile": ("profile_id", "profile_revision"),
    "midbrain.robot_collision_geometry_profile": ("profile_id", "profile_revision"),
}


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise AssemblyConfigurationError(f"{label} resolves outside {resolved_root}") from error
    return resolved_candidate


def _finite_vector3(value: Any, label: str, *, positive: bool = False) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise AssemblyConfigurationError(f"{label} must contain exactly three numbers")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise AssemblyConfigurationError(f"{label} must contain exactly three numbers") from error
    if not all(math.isfinite(item) for item in vector):
        raise AssemblyConfigurationError(f"{label} must contain finite numbers")
    if positive and any(item <= 0.0 for item in vector):
        raise AssemblyConfigurationError(f"{label} must contain positive dimensions")
    return vector


def _validate_frame_primitives(
    primitives: Any,
    known_frames: set[str],
    label: str,
) -> None:
    if not isinstance(primitives, list):
        raise AssemblyConfigurationError(f"{label} must be an array")
    primitive_ids: set[str] = set()
    for primitive in primitives:
        if not isinstance(primitive, dict):
            raise AssemblyConfigurationError(f"{label} entries must be objects")
        primitive_id = str(primitive.get("primitive_id", ""))
        if not primitive_id or primitive_id in primitive_ids:
            raise AssemblyConfigurationError(
                f"{label} IDs must be non-empty and unique"
            )
        primitive_ids.add(primitive_id)
        frame_id = str(primitive.get("frame_id", ""))
        if frame_id not in known_frames:
            raise AssemblyConfigurationError(
                f"{label} primitive {primitive_id!r} references unknown frame {frame_id!r}"
            )
        primitive_transform = primitive.get("transform", {})
        if not isinstance(primitive_transform, dict):
            raise AssemblyConfigurationError(
                f"{label} primitive {primitive_id!r} transform must be an object"
            )
        _finite_vector3(
            primitive_transform.get("translation_m"),
            f"{label} primitive {primitive_id!r} translation_m",
        )
        _finite_vector3(
            primitive_transform.get("rpy_rad"),
            f"{label} primitive {primitive_id!r} rpy_rad",
        )
        shape = primitive.get("shape", {})
        if not isinstance(shape, dict):
            raise AssemblyConfigurationError(
                f"{label} primitive {primitive_id!r} shape must be an object"
            )
        shape_type = str(shape.get("type", ""))
        if shape_type == "BOX":
            _finite_vector3(
                shape.get("size_m"),
                f"{label} primitive {primitive_id!r} size_m",
                positive=True,
            )
        elif shape_type == "SPHERE":
            try:
                radius = float(shape.get("radius_m"))
            except (TypeError, ValueError) as error:
                raise AssemblyConfigurationError(
                    f"{label} primitive {primitive_id!r} radius_m must be positive"
                ) from error
            if not math.isfinite(radius) or radius <= 0.0:
                raise AssemblyConfigurationError(
                    f"{label} primitive {primitive_id!r} radius_m must be positive"
                )
        elif shape_type == "CAPSULE":
            try:
                radius = float(shape.get("radius_m"))
                length = float(shape.get("length_m"))
            except (TypeError, ValueError) as error:
                raise AssemblyConfigurationError(
                    f"{label} primitive {primitive_id!r} capsule dimensions are invalid"
                ) from error
            if (
                not math.isfinite(radius)
                or radius <= 0.0
                or not math.isfinite(length)
                or length < 0.0
                or shape.get("axis") not in {"X", "Y", "Z"}
            ):
                raise AssemblyConfigurationError(
                    f"{label} primitive {primitive_id!r} capsule dimensions are invalid"
                )
        else:
            raise AssemblyConfigurationError(
                f"{label} primitive {primitive_id!r} has unsupported shape {shape_type!r}"
            )


class RobotAssemblyConfiguration:
    """A normalized, identity-checked view of one installed robot assembly."""

    def __init__(self, selection_path: Path, workspace_root: Path, selection: dict[str, Any]):
        self.selection_path = selection_path.resolve()
        self.workspace_root = workspace_root.resolve()
        self.selection = copy.deepcopy(selection)
        self.provider_root = self._resolve_provider_root()
        self.profile_paths: dict[str, Path] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.profile_file_sha256: dict[str, str] = {}
        self._validate_and_load()

    @classmethod
    def load(
        cls,
        selection_path: str | Path,
        workspace_root: str | Path | None = None,
    ) -> "RobotAssemblyConfiguration":
        path = Path(selection_path).resolve()
        if workspace_root is None:
            if path.parent.name != "robot_assemblies" or path.parent.parent.name != "config":
                raise AssemblyConfigurationError(
                    "workspace_root is required when the selection is not under config/robot_assemblies"
                )
            root = path.parents[2]
        else:
            root = Path(workspace_root).resolve()
        _inside(root, path, "assembly selection")
        try:
            selection = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AssemblyConfigurationError(f"cannot load assembly selection {path}: {error}") from error
        if not isinstance(selection, dict):
            raise AssemblyConfigurationError("assembly selection must be a JSON object")
        return cls(path, root, selection)

    def _resolve_provider_root(self) -> Path:
        if self.selection.get("schema") != "midbrain.robot_assembly_selection":
            raise AssemblyConfigurationError("unsupported robot assembly selection schema")
        if self.selection.get("schema_version") != 1:
            raise AssemblyConfigurationError("unsupported robot assembly selection version")
        provider = self.selection.get("arm_provider")
        if not isinstance(provider, dict):
            raise AssemblyConfigurationError("arm_provider must be an object")
        provider_root = Path(str(provider.get("provider_root", "")))
        if not str(provider_root) or provider_root.is_absolute():
            raise AssemblyConfigurationError("arm_provider.provider_root must be workspace-relative")
        resolved = _inside(
            self.workspace_root,
            self.workspace_root / provider_root,
            "arm Provider root",
        )
        if not resolved.is_dir():
            raise AssemblyConfigurationError(f"arm Provider root does not exist: {resolved}")
        provider_id = str(provider.get("provider_id", "")).strip()
        if provider_id != "robot_arm.rebot_dm":
            raise AssemblyConfigurationError(
                "this Basic Provider can only resolve its own robot_arm.rebot_dm assembly"
            )
        manifest_path = resolved / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AssemblyConfigurationError(
                f"cannot validate arm Provider manifest {manifest_path}: {error}"
            ) from error
        if str(manifest.get("provider_type", "")) != provider_id:
            raise AssemblyConfigurationError(
                "arm_provider.provider_id does not match the selected Provider manifest"
            )
        return resolved

    def _validate_and_load(self) -> None:
        for field in ("assembly_id", "assembly_revision", "arm_resource_id"):
            if not str(self.selection.get(field, "")).strip():
                raise AssemblyConfigurationError(f"{field} must be non-empty")
        profiles = self.selection.get("profiles")
        if not isinstance(profiles, dict):
            raise AssemblyConfigurationError("profiles must be an object")
        required = ("arm_model", "calibration", "mounted_effector", "collision_geometry")
        if set(profiles) != set(required):
            raise AssemblyConfigurationError(
                "profiles must contain exactly arm_model, calibration, mounted_effector, and collision_geometry"
            )
        for name in required:
            self._load_profile(name, profiles[name])
        self._validate_compatibility()

    def _load_profile(self, name: str, reference: Any) -> None:
        if not isinstance(reference, dict):
            raise AssemblyConfigurationError(f"profiles.{name} must be an object")
        required = {"relative_path", "expected_schema", "expected_id", "expected_revision"}
        if not required.issubset(reference):
            missing = sorted(required.difference(reference))
            raise AssemblyConfigurationError(f"profiles.{name} is missing {', '.join(missing)}")
        relative = Path(str(reference["relative_path"]))
        if relative.is_absolute():
            raise AssemblyConfigurationError(f"profiles.{name}.relative_path must be Provider-relative")
        path = _inside(self.provider_root, self.provider_root / relative, f"profiles.{name}")
        if not path.is_file():
            raise AssemblyConfigurationError(f"profiles.{name} does not exist: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = reference.get("sha256")
        if expected_digest is not None and str(expected_digest).lower() != digest:
            raise AssemblyConfigurationError(f"profiles.{name} SHA-256 does not match the selection")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AssemblyConfigurationError(f"profiles.{name} is not valid UTF-8 JSON: {error}") from error
        if not isinstance(document, dict):
            raise AssemblyConfigurationError(f"profiles.{name} must resolve to a JSON object")
        schema = str(document.get("schema", ""))
        if schema != str(reference["expected_schema"]):
            raise AssemblyConfigurationError(f"profiles.{name} schema {schema!r} does not match selection")
        fields = _IDENTITY_FIELDS.get(schema)
        if fields is None:
            raise AssemblyConfigurationError(f"profiles.{name} uses unsupported schema {schema!r}")
        identity_field, revision_field = fields
        if str(document.get(identity_field, "")) != str(reference["expected_id"]):
            raise AssemblyConfigurationError(f"profiles.{name} identity does not match the selection")
        if str(document.get(revision_field, "")) != str(reference["expected_revision"]):
            raise AssemblyConfigurationError(f"profiles.{name} revision does not match the selection")
        self.profile_paths[name] = path
        self.profiles[name] = document
        self.profile_file_sha256[name] = digest

    def _validate_compatibility(self) -> None:
        model = self.profiles["arm_model"]
        calibration = self.profiles["calibration"]
        effector = self.profiles["mounted_effector"]
        collision = self.profiles["collision_geometry"]
        model_id = str(model["model_id"])
        model_revision = str(model["model_revision"])
        appendix = model.get("appendix", {})
        if not isinstance(appendix, dict):
            raise AssemblyConfigurationError("arm model appendix must be an object")
        if str(calibration.get("model_id")) != model_id:
            raise AssemblyConfigurationError("calibration model_id does not match the arm model")
        effector_robot = effector.get("robot_compatibility", {})
        if (
            str(effector_robot.get("model_id")) != model_id
            or str(effector_robot.get("model_revision")) != model_revision
        ):
            raise AssemblyConfigurationError("mounted effector is incompatible with the selected arm model")
        attachment = effector.get("kinematic_attachment", {})
        controlled_frame = effector.get("controlled_frame", {})
        if not isinstance(attachment, dict) or not isinstance(controlled_frame, dict):
            raise AssemblyConfigurationError(
                "mounted effector attachment and controlled frame must be objects"
            )
        if (
            not str(attachment.get("parent_frame", ""))
            or not str(attachment.get("child_frame", ""))
            or attachment.get("parent_frame") == attachment.get("child_frame")
        ):
            raise AssemblyConfigurationError(
                "mounted effector attachment frames must be non-empty and distinct"
            )
        for label, transform_value in (
            ("mounted effector attachment", attachment.get("transform")),
            ("mounted effector controlled frame", controlled_frame.get("transform")),
        ):
            if not isinstance(transform_value, dict):
                raise AssemblyConfigurationError(f"{label} transform must be an object")
            _finite_vector3(
                transform_value.get("translation_m"),
                f"{label} translation_m",
            )
            _finite_vector3(
                transform_value.get("rpy_rad"),
                f"{label} rpy_rad",
            )
        if str(effector_robot.get("terminal_frame")) != str(attachment.get("parent_frame")):
            raise AssemblyConfigurationError(
                "mounted effector attachment parent does not match its compatible terminal frame"
            )
        if str(controlled_frame.get("parent_frame")) != str(attachment.get("child_frame")):
            raise AssemblyConfigurationError(
                "mounted effector controlled frame must be attached to the effector child frame"
            )
        inertial = effector.get("inertial", {})
        if not isinstance(inertial, dict):
            raise AssemblyConfigurationError("mounted effector inertial must be an object")
        if str(inertial.get("reference_frame")) != str(attachment.get("child_frame")):
            raise AssemblyConfigurationError(
                "mounted effector inertial reference frame must match the effector child frame"
            )
        try:
            inertial_mass = float(inertial.get("mass_kg"))
        except (TypeError, ValueError) as error:
            raise AssemblyConfigurationError(
                "mounted effector inertial mass must be finite and non-negative"
            ) from error
        if not math.isfinite(inertial_mass) or inertial_mass < 0.0:
            raise AssemblyConfigurationError(
                "mounted effector inertial mass must be finite and non-negative"
            )
        _finite_vector3(
            inertial.get("center_of_mass_m"),
            "mounted effector inertial center_of_mass_m",
        )
        inertia = inertial.get("inertia_kg_m2")
        if not isinstance(inertia, dict):
            raise AssemblyConfigurationError(
                "mounted effector inertia_kg_m2 must be an object"
            )
        try:
            inertia_values = {
                name: float(inertia.get(name))
                for name in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
            }
        except (TypeError, ValueError) as error:
            raise AssemblyConfigurationError(
                "mounted effector inertia tensor must contain finite numbers"
            ) from error
        if not all(math.isfinite(value) for value in inertia_values.values()) or any(
            inertia_values[name] < 0.0 for name in ("ixx", "iyy", "izz")
        ):
            raise AssemblyConfigurationError(
                "mounted effector inertia tensor must be finite with non-negative diagonal"
            )
        model_fixed_tool = model.get("fixed_tool", {})
        if str(model_fixed_tool.get("parent_link")) != str(effector_robot.get("terminal_frame")):
            raise AssemblyConfigurationError(
                "arm model fixed-tool parent does not match the mounted effector terminal frame"
            )
        collision_robot = collision.get("robot_compatibility", {})
        if (
            str(collision_robot.get("model_id")) != model_id
            or str(collision_robot.get("model_revision")) != model_revision
        ):
            raise AssemblyConfigurationError("collision geometry is incompatible with the selected arm model")
        collision_effector = collision.get("mounted_effector_compatibility")
        if collision_effector is not None:
            if not isinstance(collision_effector, dict) or (
                str(collision_effector.get("profile_id")) != str(effector.get("profile_id"))
                or str(collision_effector.get("profile_revision"))
                != str(effector.get("profile_revision"))
            ):
                raise AssemblyConfigurationError(
                    "collision geometry is incompatible with the mounted effector"
                )

        joint_names = [str(joint.get("name", "")) for joint in model.get("joints", [])]
        if not joint_names or any(not name for name in joint_names) or len(set(joint_names)) != len(joint_names):
            raise AssemblyConfigurationError("arm model joint names must be non-empty and unique")
        effector_joint_names: list[str] = []
        group_ids: set[str] = set()
        group_resources: set[str] = set()
        for group in effector.get("actuator_groups", []):
            if not isinstance(group, dict):
                raise AssemblyConfigurationError("effector actuator groups must be objects")
            group_id = str(group.get("group_id", ""))
            resource_id = str(group.get("resource_id", ""))
            if (
                not group_id
                or group_id in group_ids
                or not resource_id
                or resource_id in group_resources
            ):
                raise AssemblyConfigurationError(
                    "effector actuator-group IDs and resources must be non-empty and unique"
                )
            group_ids.add(group_id)
            group_resources.add(resource_id)
            names = [str(value) for value in group.get("joint_names", [])]
            overlap = set(names).intersection(effector_joint_names)
            if overlap:
                raise AssemblyConfigurationError(f"effector actuator groups overlap at {sorted(overlap)}")
            effector_joint_names.extend(names)
        unknown = set(effector_joint_names).difference(joint_names)
        if unknown:
            raise AssemblyConfigurationError(f"effector references unknown joints {sorted(unknown)}")
        inactive_joint_names = effector.get("inactive_joint_names")
        if not isinstance(inactive_joint_names, list) or any(
            not isinstance(name, str) or not name.strip()
            for name in inactive_joint_names
        ):
            raise AssemblyConfigurationError(
                "mounted effector inactive_joint_names must be an array of non-empty strings"
            )
        inactive_joint_names = [str(name) for name in inactive_joint_names]
        if len(set(inactive_joint_names)) != len(inactive_joint_names):
            raise AssemblyConfigurationError(
                "mounted effector inactive_joint_names must be unique"
            )
        unknown_inactive = set(inactive_joint_names).difference(joint_names)
        if unknown_inactive:
            raise AssemblyConfigurationError(
                f"mounted effector marks unknown joints inactive {sorted(unknown_inactive)}"
            )
        inactive_actuator_overlap = set(inactive_joint_names).intersection(
            effector_joint_names
        )
        if inactive_actuator_overlap:
            raise AssemblyConfigurationError(
                "mounted effector joints cannot be both inactive and actuator-group members"
            )
        self.inactive_joint_names = tuple(inactive_joint_names)
        unavailable_joint_names = set(effector_joint_names).union(
            inactive_joint_names
        )
        self.arm_joint_names = tuple(
            name for name in joint_names if name not in unavailable_joint_names
        )
        if not self.arm_joint_names:
            raise AssemblyConfigurationError("assembly must retain at least one arm motion joint")
        resource_root = str(self.selection["arm_resource_id"])
        for group in effector.get("actuator_groups", []):
            resource_id = str(group.get("resource_id", ""))
            if not resource_id.startswith(resource_root + "/"):
                raise AssemblyConfigurationError(
                    "effector actuator-group resources must be children of arm_resource_id"
                )

        capsules = collision.get("polyline_capsules")
        if not isinstance(capsules, list) or not capsules or any(
            not isinstance(item, dict) for item in capsules
        ):
            raise AssemblyConfigurationError(
                "collision polyline capsules must be a non-empty array of objects"
            )
        try:
            indices = [int(item.get("segment_index", -1)) for item in capsules]
            radii = [float(item.get("radius_m")) for item in capsules]
        except (TypeError, ValueError) as error:
            raise AssemblyConfigurationError(
                "collision polyline capsule indices and radii must be numeric"
            ) from error
        if indices != list(range(len(indices))):
            raise AssemblyConfigurationError("collision polyline capsule indices must be contiguous from zero")
        if any(not math.isfinite(radius) or radius <= 0.0 for radius in radii):
            raise AssemblyConfigurationError(
                "collision polyline capsule radii must be finite and positive"
            )
        point_frames = collision.get("polyline_point_frames")
        if not isinstance(point_frames, list) or len(point_frames) != len(indices) + 1:
            raise AssemblyConfigurationError(
                "collision polyline point frames must contain exactly one more entry than capsules"
            )
        root_frame = str(model.get("coordinate_convention", {}).get("root_frame", ""))
        child_frames_by_joint = {
            str(joint.get("name", "")): str(joint.get("kinematics", {}).get("child_link", ""))
            for joint in model.get("joints", [])
            if isinstance(joint, dict) and isinstance(joint.get("kinematics"), dict)
        }
        arm_point_frames = [root_frame]
        arm_point_frames.extend(child_frames_by_joint[name] for name in self.arm_joint_names)
        legacy_effector_point_frames = arm_point_frames + [
            str(controlled_frame.get("frame_id", ""))
        ]
        if point_frames not in (arm_point_frames, legacy_effector_point_frames):
            raise AssemblyConfigurationError(
                "collision polyline point frames do not match the selected arm kinematic chain"
            )

        known_frames = set(legacy_effector_point_frames)
        known_frames.add(str(attachment.get("child_frame", "")))
        acting_frame_ids: set[str] = set()
        for acting_frame in effector.get("acting_frames", []):
            if not isinstance(acting_frame, dict):
                raise AssemblyConfigurationError("effector acting frames must be objects")
            frame_id = str(acting_frame.get("frame_id", ""))
            parent_frame = str(acting_frame.get("parent_frame", ""))
            if not frame_id or frame_id in known_frames or frame_id in acting_frame_ids:
                raise AssemblyConfigurationError(
                    "effector acting-frame IDs must be non-empty and unique"
                )
            if parent_frame not in known_frames:
                raise AssemblyConfigurationError(
                    f"effector acting frame {frame_id!r} references unknown parent {parent_frame!r}"
                )
            acting_transform = acting_frame.get("transform")
            if not isinstance(acting_transform, dict):
                raise AssemblyConfigurationError(
                    f"effector acting frame {frame_id!r} transform must be an object"
                )
            _finite_vector3(
                acting_transform.get("translation_m"),
                f"effector acting frame {frame_id!r} translation_m",
            )
            _finite_vector3(
                acting_transform.get("rpy_rad"),
                f"effector acting frame {frame_id!r} rpy_rad",
            )
            acting_frame_ids.add(frame_id)
            known_frames.add(frame_id)
        _validate_frame_primitives(
            effector.get("collision_primitives"),
            known_frames,
            "mounted effector collision_primitives",
        )
        _validate_frame_primitives(
            collision.get("frame_primitives"),
            known_frames,
            "arm collision frame_primitives",
        )

    @property
    def model_path(self) -> Path:
        return self.profile_paths["arm_model"]

    @property
    def calibration_path(self) -> Path:
        return self.profile_paths["calibration"]

    @property
    def fingerprint(self) -> str:
        return _canonical_sha256(
            {
                "selection": self.selection,
                "profile_file_sha256": self.profile_file_sha256,
            }
        )

    def resource_groups(self) -> list[dict[str, Any]]:
        groups = [
            {
                "group_id": "arm",
                "resource_id": f"{self.selection['arm_resource_id']}/arm",
                "joint_names": list(self.arm_joint_names),
                "capabilities": ["robot_arm.control.joint_group.v1"],
            }
        ]
        groups.extend(copy.deepcopy(self.profiles["mounted_effector"].get("actuator_groups", [])))
        return groups

    def normalized_arm_model(self) -> dict[str, Any]:
        """Bind the selected physical effector into the legacy Basic model shape."""

        model = copy.deepcopy(self.profiles["arm_model"])
        effector = self.profiles["mounted_effector"]
        attachment = effector["kinematic_attachment"]
        controlled = effector["controlled_frame"]
        inertial = effector["inertial"]
        previous_child = str(model["fixed_tool"]["child_link"])
        model["fixed_tool"] = {
            "parent_link": attachment["parent_frame"],
            "child_link": attachment["child_frame"],
            "translation_m": copy.deepcopy(
                attachment["transform"]["translation_m"]
            ),
            "rpy_rad": copy.deepcopy(attachment["transform"]["rpy_rad"]),
        }
        model["controlled_frame"] = {
            "frame_id": controlled["frame_id"],
            "parent_frame": controlled["parent_frame"],
            "translation_m": copy.deepcopy(
                controlled["transform"]["translation_m"]
            ),
            "rpy_rad": copy.deepcopy(controlled["transform"]["rpy_rad"]),
        }
        model.setdefault("frames", {})["tool"] = controlled["frame_id"]
        terminal_links = [
            link for link in model.get("links", [])
            if str(link.get("name")) == previous_child
        ]
        if len(terminal_links) != 1:
            raise AssemblyConfigurationError(
                "arm model must contain exactly one legacy fixed-tool child link"
            )
        terminal = terminal_links[0]
        terminal.update(
            {
                "name": attachment["child_frame"],
                "mass_kg": inertial["mass_kg"],
                "weight_n_at_standard_gravity": (
                    float(inertial["mass_kg"]) * 9.80665
                ),
                "center_of_mass_m": copy.deepcopy(inertial["center_of_mass_m"]),
                "inertia_kg_m2": copy.deepcopy(inertial["inertia_kg_m2"]),
                "quality": inertial["qualification"],
            }
        )
        return model

    def public_state(self) -> dict[str, Any]:
        provider = self.selection["arm_provider"]
        return {
            "schema": "midbrain.robot_assembly_state",
            "schema_version": 1,
            "assembly_id": self.selection["assembly_id"],
            "assembly_revision": self.selection["assembly_revision"],
            "assembly_fingerprint": self.fingerprint,
            "arm_resource_id": self.selection["arm_resource_id"],
            "arm_provider_id": provider["provider_id"],
            "profile_references": copy.deepcopy(self.selection["profiles"]),
            "profile_file_sha256": dict(self.profile_file_sha256),
            "arm_model_identity": {
                "model_id": self.profiles["arm_model"]["model_id"],
                "model_revision": self.profiles["arm_model"]["model_revision"],
                "calibration_revision": self.profiles["calibration"]["calibration_revision"],
            },
            "arm_model_appendix": copy.deepcopy(
                self.profiles["arm_model"].get("appendix", {})
            ),
            "mounted_effector": copy.deepcopy(self.profiles["mounted_effector"]),
            "collision_geometry": copy.deepcopy(self.profiles["collision_geometry"]),
            "resource_groups": self.resource_groups(),
            "qualified_control_roles": copy.deepcopy(self.selection["qualified_control_roles"]),
        }
