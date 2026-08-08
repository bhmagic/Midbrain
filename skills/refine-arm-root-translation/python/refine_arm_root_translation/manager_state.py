from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

import numpy as np

from .geometry import rigid_transform, transform_from_pose
from .profile import validate_effector_profile


class WorkcellCalibrationManager(Protocol):
    async def workcell_calibrations(self) -> dict[str, Any]: ...

    async def refine_workcell_calibration_translation(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]: ...


ArmIdentitySource = Callable[
    [dict[str, Any]],
    Awaitable[dict[str, Any]],
]


class ManagerCompactAlignmentStore:
    """Map one Manager activation to the compact translation-only state."""

    def __init__(
        self,
        manager: WorkcellCalibrationManager,
        *,
        profile: dict[str, Any],
        arm_identity_source: ArmIdentitySource,
        updated_by: str = "skill.refine_arm_root_translation.v1",
    ) -> None:
        self.manager = manager
        self.profile = validate_effector_profile(profile)
        self.arm_identity_source = arm_identity_source
        self.updated_by = str(updated_by).strip()
        if not self.updated_by:
            raise ValueError("updated_by must be non-empty")
        self._active_record: dict[str, Any] | None = None
        self._source_pose: dict[str, Any] | None = None
        self._identities: dict[str, Any] | None = None

    @property
    def active_record(self) -> dict[str, Any]:
        if self._active_record is None:
            raise RuntimeError("active alignment snapshot was not captured")
        return copy.deepcopy(self._active_record)

    @property
    def active_identities(self) -> dict[str, Any]:
        if self._identities is None:
            raise RuntimeError("active alignment identities were not captured")
        return copy.deepcopy(self._identities)

    async def snapshot(self) -> dict[str, Any]:
        catalog = await self.manager.workcell_calibrations()
        records = catalog.get("activations") if isinstance(catalog, dict) else None
        active = [
            record
            for record in (records or [])
            if isinstance(record, dict)
            and record.get("state") == "ACTIVE"
            and record.get("motion_usable") is True
            and record.get("enforcement") == "ENFORCED"
        ]
        if len(active) != 1:
            raise RuntimeError(
                "translation refinement requires exactly one enforced motion-usable active alignment"
            )
        record = copy.deepcopy(active[0])
        transforms = record.get("transforms")
        if not isinstance(transforms, dict):
            raise RuntimeError("active alignment transforms are unavailable")
        source_pose = transforms.get("world_from_base")
        source_matrix = transform_from_pose(
            source_pose,
            "active.transforms.world_from_base",
        )
        revision = record.get("translation_refinement_revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RuntimeError("active translation-refinement revision is invalid")
        arm_identity = await self.arm_identity_source(record)
        if not isinstance(arm_identity, dict):
            raise RuntimeError("arm identity source returned invalid data")
        compatibility = self.profile["robot_compatibility"]
        if self._record_text(record, "arm_base_frame") != compatibility[
            "arm_base_frame"
        ]:
            raise RuntimeError(
                "active arm-base frame does not match the effector profile"
            )
        required_arm_identity = {
            "arm_provider_id": arm_identity.get("arm_provider_id"),
            "arm_provider_instance_id": arm_identity.get("arm_provider_instance_id"),
            "arm_boot_id": arm_identity.get("arm_boot_id"),
            "arm_model_id": arm_identity.get("arm_model_id"),
            "arm_model_revision": arm_identity.get("arm_model_revision"),
        }
        for field, value in required_arm_identity.items():
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"current arm identity {field} is missing")
        if required_arm_identity["arm_model_id"] != compatibility["model_id"]:
            raise RuntimeError("current arm model does not match the effector profile")
        if (
            required_arm_identity["arm_model_revision"]
            != compatibility["model_revision"]
        ):
            raise RuntimeError(
                "current arm model revision does not match the effector profile"
            )
        identities = {
            "world_frame": self._record_text(record, "world_frame"),
            "vio_session_epoch": self._record_text(record, "session_epoch"),
            "spatial_convention": self._record_text(record, "convention_id"),
            "camera_provider_id": self._record_text(record, "camera_provider_id"),
            "camera_provider_instance_id": self._record_text(
                record,
                "camera_provider_instance_id",
            ),
            "camera_boot_id": self._record_text(record, "camera_boot_id"),
            "camera_calibration_revision": self._record_text(
                record,
                "camera_calibration_revision",
            ),
            **required_arm_identity,
            "effector_profile_revision": self.profile["profile_revision"],
        }
        self._active_record = record
        self._source_pose = copy.deepcopy(source_pose)
        self._identities = copy.deepcopy(identities)
        return {
            "schema": "midbrain.compact_arm_root_alignment_state",
            "schema_version": 1,
            "revision": revision,
            "world_from_base": source_matrix.tolist(),
            "identities": identities,
            "last_update": copy.deepcopy(record.get("last_translation_refinement")),
        }

    async def compare_and_swap(
        self,
        *,
        expected_revision: int,
        state: dict[str, Any],
        refinement: dict[str, Any],
    ) -> bool:
        if self._active_record is None or self._source_pose is None:
            raise RuntimeError("active alignment snapshot was not captured")
        if expected_revision != int(
            self._active_record.get("translation_refinement_revision", 0)
        ):
            return False
        proposed = rigid_transform(
            state.get("world_from_base"),
            "compact_state.world_from_base",
        )
        source = transform_from_pose(
            self._source_pose,
            "active.transforms.world_from_base",
        )
        if not np.array_equal(source[:3, :3], proposed[:3, :3]):
            raise RuntimeError("Manager update attempted to change active rotation")
        proposed_pose = {
            "translation_m": proposed[:3, 3].tolist(),
            "rotation_xyzw": copy.deepcopy(self._source_pose["rotation_xyzw"]),
        }
        request = {
            "request_id": f"translation-refinement-{uuid4()}",
            "updated_by": self.updated_by,
            "activation_id": self._record_text(self._active_record, "activation_id"),
            "expected_refinement_revision": int(expected_revision),
            "source_world_from_base": copy.deepcopy(self._source_pose),
            "proposed_world_from_base": proposed_pose,
            "refinement": copy.deepcopy(refinement),
        }
        try:
            updated = await self.manager.refine_workcell_calibration_translation(
                request
            )
        except Exception as error:
            response = getattr(error, "response", None)
            status_code = getattr(error, "status_code", None)
            if status_code == 409 or getattr(response, "status_code", None) == 409:
                return False
            raise
        if not isinstance(updated, dict):
            raise RuntimeError("Manager returned invalid refinement activation")
        if updated.get("activation_id") != self._active_record.get("activation_id"):
            raise RuntimeError("Manager changed activation identity during refinement")
        if updated.get("translation_refinement_revision") != expected_revision + 1:
            raise RuntimeError("Manager returned an unexpected refinement revision")
        updated_pose = (updated.get("transforms") or {}).get("world_from_base")
        if updated_pose != proposed_pose:
            raise RuntimeError("Manager activated a different translation refinement")
        self._active_record = copy.deepcopy(updated)
        self._source_pose = copy.deepcopy(updated_pose)
        return True

    @staticmethod
    def _record_text(record: dict[str, Any], field: str) -> str:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"active alignment {field} is missing")
        return value.strip()
