from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any, Protocol
from uuid import uuid4

import numpy as np
from spatial_registration_rgbd import register_rgbd_point

from .phase4_policy import report_operation_progress
from .route_resolver import (
    GENERIC_RGBD_ROUTE_CAPABILITY,
    routes_from_observation,
    select_rgbd_route,
)
from .temporal_policy import evaluate_observation_temporal_policy


CAMERA_CAPABILITIES = (
    "camera.rgb",
    "camera.depth_aligned_to_rgb",
    "camera.rgbd_geometry",
    "camera.rgbd.bundle",
)
_POLICY_MODES = {"SHADOW", "ENFORCED", "FALLBACK"}
SPATIAL_INPUT_TEMPORAL_POLICY_ID = "spatial.registration.rgbd.input.v1"
DEFAULT_MAXIMUM_SOURCE_AGE_MS: dict[str, float | None] = {
    "route": 15_000.0,
    "bundle": 1_000.0,
    "calibration": None,
    "body_pose": 500.0,
    "vio_status": 1_000.0,
}
WORLD_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)


class RgbdFrameSource(Protocol):
    async def capture(self) -> Any:
        """Copy one synchronized RGB-D frame out of provider-owned memory."""


class SpatialManager(Protocol):
    async def bind_capabilities(
        self,
        required_capabilities: list[str],
        *,
        fallback_provider_ids: dict[str, str] | None = None,
        allowed_provider_ids: list[str] | None = None,
        excluded_provider_ids: list[str] | None = None,
        request_id: str | None = None,
        related_skill_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def capability_binding(self, binding_id: str) -> dict[str, Any]: ...


class SpatialFabric(Protocol):
    async def latest_optional(self, stream: str) -> dict[str, Any] | None: ...

    async def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int | None = None,
        max_extrapolation_us: int = 500_000,
        session_epoch: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BindingSnapshot:
    provider_id: str
    provider_instance_id: str | None
    boot_id: str | None
    binding: dict[str, Any]
    enforcement_issues: tuple[str, ...]
    configured_fallback_provider_ids: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "enforcement_issues": list(self.enforcement_issues),
        }


@dataclass(frozen=True)
class SpatialFrameContext:
    skill_id: str
    target_frame: str
    frame: Any
    binding: BindingSnapshot
    selection: Any
    selected_route: dict[str, Any]
    transform: dict[str, Any]
    target_from_camera: np.ndarray
    valid_region: dict[str, Any] | None
    temporal_evidence: dict[str, dict[str, Any]]


class SpatialRegistrationSkillAdapter:
    """Bind, copy, validate, and register one RGB pixel without moving hardware."""

    def __init__(
        self,
        capture: RgbdFrameSource,
        fabric: SpatialFabric,
        *,
        manager: SpatialManager | None,
        fallback_camera_provider_id: str,
        binding_mode: str = "SHADOW",
        generic_route_mode: str = "SHADOW",
        maximum_transform_extrapolation_us: int = 750_000,
        maximum_source_age_ms: dict[str, float | None] | None = None,
    ):
        normalized_binding = str(binding_mode).strip().upper()
        normalized_route = str(generic_route_mode).strip().upper()
        if normalized_binding not in _POLICY_MODES:
            raise ValueError("binding_mode must be SHADOW, ENFORCED, or FALLBACK")
        if normalized_route not in _POLICY_MODES:
            raise ValueError(
                "generic_route_mode must be SHADOW, ENFORCED, or FALLBACK"
            )
        if int(maximum_transform_extrapolation_us) <= 0:
            raise ValueError("maximum transform extrapolation must be positive")
        self.capture = capture
        self.fabric = fabric
        self.manager = manager
        self.fallback_camera_provider_id = str(fallback_camera_provider_id)
        self.binding_mode = normalized_binding
        self.generic_route_mode = normalized_route
        self.maximum_transform_extrapolation_us = int(
            maximum_transform_extrapolation_us
        )
        self.maximum_source_age_ms = dict(DEFAULT_MAXIMUM_SOURCE_AGE_MS)
        if maximum_source_age_ms is not None:
            unknown = set(maximum_source_age_ms) - set(
                self.maximum_source_age_ms
            )
            if unknown:
                raise ValueError(
                    "unknown spatial temporal-policy inputs: "
                    + ", ".join(sorted(unknown))
                )
            self.maximum_source_age_ms.update(maximum_source_age_ms)
        for name, maximum_age in self.maximum_source_age_ms.items():
            if maximum_age is not None and float(maximum_age) <= 0.0:
                raise ValueError(
                    f"maximum source age for {name} must be positive"
                )
        self.last_result: dict[str, Any] | None = None
        self.last_binding: dict[str, Any] | None = None

    async def run(
        self,
        *,
        pixel_yx: list[float] | tuple[float, float],
        target_frame: str,
        depth_policy: str,
    ) -> dict[str, Any]:
        if len(pixel_yx) != 2:
            raise ValueError("pixel_yx must contain [y, x]")
        pixel = (float(pixel_yx[0]), float(pixel_yx[1]))
        if not np.all(np.isfinite(pixel)):
            raise ValueError("pixel_yx must contain finite values")
        requested_target = str(target_frame).strip()
        if not requested_target:
            raise ValueError("target_frame must not be empty")

        skill_id = f"spatial-registration-rgbd-{uuid4()}"
        context = await self.prepare_context(
            target_frame=requested_target,
            skill_id=skill_id,
        )
        frame = context.frame

        report_operation_progress("REGISTER_RGBD_POINT")
        registration = register_rgbd_point(
            rgb_pixel_yx=pixel,
            rgb_grid=tuple(int(value) for value in frame.rgb.shape[:2]),
            registered_depth_m=frame.depth_m,
            registered_depth_grid=tuple(
                int(value) for value in frame.depth_m.shape[:2]
            ),
            intrinsics=dict(frame.intrinsics),
            target_from_camera=context.target_from_camera,
            observed_at_us=int(frame.timestamp_us),
            source_frame=str(frame.camera_frame),
            target_frame=requested_target,
            calibration_revision=frame.calibration_revision,
            route_provenance=context.selection.as_dict(),
            depth_policy=str(depth_policy),
            valid_region=context.valid_region,
        )
        result = {
            **registration,
            "skill_id": skill_id,
            "safety_class": "READ_ONLY",
            "physical_action_submitted": False,
            "capability_binding": context.binding.as_dict(),
            "binding_mode": self.binding_mode,
            "generic_route_mode": self.generic_route_mode,
            "camera_capture": self.capture_provenance(context),
            "transform_provenance": self.transform_provenance(context),
            "selected_route_metadata": self.route_metadata(context),
            "source_convention_id": CAMERA_OPTICAL_CONVENTION_ID,
            "target_convention_id": WORLD_CONVENTION_ID,
            "input_temporal_evidence": {
                "policy_id": SPATIAL_INPUT_TEMPORAL_POLICY_ID,
                "evaluated_inputs": dict(context.temporal_evidence),
                "skill_completed_at_us": time.time_ns() // 1000,
            },
        }
        self.last_result = result
        return result

    async def prepare_context(
        self,
        *,
        target_frame: str,
        skill_id: str | None = None,
    ) -> SpatialFrameContext:
        requested_target = str(target_frame).strip()
        if not requested_target:
            raise ValueError("target_frame must not be empty")
        context_skill_id = skill_id or f"spatial-context-{uuid4()}"
        report_operation_progress("BIND_RGBD_CAMERA")
        binding = await self._bind_camera(context_skill_id)
        self.last_binding = dict(binding.binding)

        report_operation_progress("COPY_SYNCHRONIZED_RGBD")
        frame = await self.capture.capture()

        report_operation_progress("READ_RGBD_ROUTE_SET")
        route_observation = await self.fabric.latest_optional(
            "camera.rgbd.data_routes"
        )
        routes = routes_from_observation(route_observation)
        selection = select_rgbd_route(
            routes,
            provider_id=binding.provider_id,
        )
        if selection is None:
            raise RuntimeError(
                "no compatible RGB-D route exists for the bound camera provider"
            )
        selected_route = next(
            (
                route
                for route in routes
                if str(route.get("route_id") or "") == selection.route_id
            ),
            None,
        )
        if not isinstance(selected_route, dict):
            raise RuntimeError("selected RGB-D route metadata disappeared")
        self._validate_coordinate_conventions(
            selected_route=selected_route,
            frame=frame,
        )

        report_operation_progress("REVALIDATE_RGBD_BINDING")
        binding = await self._revalidate_binding(binding)
        self.last_binding = dict(binding.binding)
        temporal_evidence = self._validate_data_plane_identity(
            binding=binding,
            route_observation=route_observation,
            selected_route=selected_route,
            frame=frame,
        )
        temporal_evidence.update(self._validate_vio_context(frame))
        if (
            self.generic_route_mode == "ENFORCED"
            and selection.capability != GENERIC_RGBD_ROUTE_CAPABILITY
        ):
            raise RuntimeError(
                "generic RGB-D route enforcement rejected the direct provider fallback"
            )

        report_operation_progress("QUERY_TIMESTAMPED_TRANSFORM")
        transform = await self.fabric.transform(
            from_frame=str(frame.camera_frame),
            to_frame=requested_target,
            at_us=int(frame.timestamp_us),
            max_extrapolation_us=self.maximum_transform_extrapolation_us,
            session_epoch=str(frame.session_epoch),
        )
        self._validate_transform(
            transform,
            source_frame=str(frame.camera_frame),
            target_frame=requested_target,
            timestamp_us=int(frame.timestamp_us),
            session_epoch=str(frame.session_epoch),
        )
        valid_region = self._registered_depth_valid_region(selected_route)
        return SpatialFrameContext(
            skill_id=context_skill_id,
            target_frame=requested_target,
            frame=frame,
            binding=binding,
            selection=selection,
            selected_route=selected_route,
            transform=transform,
            target_from_camera=self._transform_matrix(transform),
            valid_region=valid_region,
            temporal_evidence=temporal_evidence,
        )

    def capture_provenance(
        self,
        context: SpatialFrameContext,
    ) -> dict[str, Any]:
        frame = context.frame
        return {
            "frame_number": int(frame.frame_number),
            "rgb_grid": [
                int(frame.rgb.shape[0]),
                int(frame.rgb.shape[1]),
            ],
            "registered_depth_grid": [
                int(frame.depth_m.shape[0]),
                int(frame.depth_m.shape[1]),
            ],
            "session_epoch": str(frame.session_epoch),
            "world_frame": str(frame.world_frame),
            "copy": dict(
                (frame.observations.get("capture") or {})
                if isinstance(frame.observations, dict)
                else {}
            ),
        }

    async def revalidate_context_binding(
        self,
        context: SpatialFrameContext,
    ) -> BindingSnapshot:
        current = await self._revalidate_binding(context.binding)
        original_identity = (
            context.binding.provider_id,
            context.binding.provider_instance_id,
            context.binding.boot_id,
        )
        current_identity = (
            current.provider_id,
            current.provider_instance_id,
            current.boot_id,
        )
        if current_identity != original_identity:
            raise RuntimeError(
                "camera binding identity changed after synchronized capture"
            )
        self.last_binding = dict(current.binding)
        return current

    def transform_provenance(
        self,
        context: SpatialFrameContext,
    ) -> dict[str, Any]:
        return {
            "at_us": int(context.transform["at_us"]),
            "path": list(context.transform.get("path") or []),
            "maximum_extrapolation_us": self.maximum_transform_extrapolation_us,
        }

    def route_metadata(
        self,
        context: SpatialFrameContext,
    ) -> dict[str, Any]:
        return {
            "valid_region": context.valid_region,
            "alignment": self._registered_depth_alignment(
                context.selected_route
            ),
        }

    async def _bind_camera(self, skill_id: str) -> BindingSnapshot:
        if self.manager is None:
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    "spatial binding enforcement requires an available Manager"
                )
            binding = {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.fallback_camera_provider_id,
                "reason": "Manager client is not configured",
            }
            return BindingSnapshot(
                provider_id=self.fallback_camera_provider_id,
                provider_instance_id=None,
                boot_id=None,
                binding=binding,
                enforcement_issues=("MANAGER_BINDING_UNAVAILABLE",),
                configured_fallback_provider_ids={
                    capability: self.fallback_camera_provider_id
                    for capability in CAMERA_CAPABILITIES
                },
            )
        try:
            fallback = {
                capability: self.fallback_camera_provider_id
                for capability in CAMERA_CAPABILITIES
            }
            binding = await self.manager.bind_capabilities(
                list(CAMERA_CAPABILITIES),
                fallback_provider_ids=fallback,
                related_skill_id=skill_id,
            )
            binding_id = binding.get("binding_id")
            if isinstance(binding_id, str) and binding_id:
                binding = await self.manager.capability_binding(binding_id)
            return self._binding_snapshot(binding)
        except Exception as error:
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    f"spatial binding enforcement rejected fallback: {error}"
                ) from error
            binding = {
                "status": "EXPLICIT_PROVIDER_FALLBACK",
                "validity": "FALLBACK_REQUIRES_ACTIVATION",
                "provider_id": self.fallback_camera_provider_id,
                "reason": f"Manager binding unavailable: {error}",
            }
            return BindingSnapshot(
                provider_id=self.fallback_camera_provider_id,
                provider_instance_id=None,
                boot_id=None,
                binding=binding,
                enforcement_issues=("MANAGER_BINDING_UNAVAILABLE",),
                configured_fallback_provider_ids={
                    capability: self.fallback_camera_provider_id
                    for capability in CAMERA_CAPABILITIES
                },
            )

    async def _revalidate_binding(
        self,
        current: BindingSnapshot,
    ) -> BindingSnapshot:
        binding_id = current.binding.get("binding_id")
        if (
            self.manager is None
            or not isinstance(binding_id, str)
            or not binding_id
        ):
            if self.binding_mode == "ENFORCED":
                raise RuntimeError(
                    "spatial binding enforcement requires a revalidatable binding"
                )
            return current
        return self._binding_snapshot(
            await self.manager.capability_binding(binding_id)
        )

    def _binding_snapshot(self, binding: dict[str, Any]) -> BindingSnapshot:
        selections = binding.get("selections")
        if not isinstance(selections, list):
            selections = []
        by_capability = {
            str(selection.get("capability")): selection
            for selection in selections
            if isinstance(selection, dict) and selection.get("capability")
        }
        issues: list[str] = []
        missing = [
            capability
            for capability in CAMERA_CAPABILITIES
            if capability not in by_capability
        ]
        if missing:
            issues.append("MISSING_CAPABILITIES:" + ",".join(missing))

        selected = [
            by_capability[capability]
            for capability in CAMERA_CAPABILITIES
            if capability in by_capability
        ]
        provider_ids = {
            str(selection.get("provider_id") or "") for selection in selected
        }
        provider_ids.discard("")
        instance_ids = {
            str(selection.get("provider_instance_id") or "")
            for selection in selected
        }
        instance_ids.discard("")
        boot_ids = {
            str(selection.get("boot_id") or "") for selection in selected
        }
        boot_ids.discard("")
        if any(
            not selection.get("provider_id")
            or not selection.get("provider_instance_id")
            or not selection.get("boot_id")
            for selection in selected
        ):
            issues.append("CAMERA_SELECTION_IDENTITY_INCOMPLETE")
        if any(not bool(selection.get("available")) for selection in selected):
            issues.append("CAMERA_CAPABILITY_NOT_AVAILABLE")
        if any(
            selection.get("compatibility_verified") is False
            for selection in selected
        ):
            issues.append("CAMERA_COMPATIBILITY_UNVERIFIED")
        if any(
            bool(selection.get("requires_activation"))
            for selection in selected
        ):
            issues.append("CAMERA_PROVIDER_REQUIRES_ACTIVATION")
        if len(provider_ids) != 1:
            issues.append("CAMERA_CAPABILITIES_NOT_COLOCATED")
        if len(instance_ids) != 1:
            issues.append("CAMERA_INSTANCE_NOT_UNIQUE")
        if len(boot_ids) != 1:
            issues.append("CAMERA_BOOT_NOT_UNIQUE")
        validity = str(binding.get("validity") or "")
        if validity != "CURRENT":
            issues.append(f"BINDING_NOT_CURRENT:{validity or 'UNKNOWN'}")
        if str(binding.get("status") or "") != "RESOLVED":
            issues.append(
                f"BINDING_NOT_RESOLVED:{binding.get('status') or 'UNKNOWN'}"
            )

        fallback_provider = str(
            binding.get("provider_id") or self.fallback_camera_provider_id
        )
        provider_id = next(iter(provider_ids), fallback_provider)
        snapshot = BindingSnapshot(
            provider_id=provider_id,
            provider_instance_id=next(iter(instance_ids), None),
            boot_id=next(iter(boot_ids), None),
            binding=dict(binding),
            enforcement_issues=tuple(issues),
            configured_fallback_provider_ids={
                capability: self.fallback_camera_provider_id
                for capability in CAMERA_CAPABILITIES
            },
        )
        if self.binding_mode == "ENFORCED" and issues:
            raise RuntimeError(
                "spatial binding enforcement failed: " + "; ".join(issues)
            )
        return snapshot

    @staticmethod
    def _observation_identity(
        observation: dict[str, Any] | None,
    ) -> tuple[str, str, str]:
        if not isinstance(observation, dict):
            return "", "", ""
        return (
            str(observation.get("provider_id") or ""),
            str(observation.get("provider_instance_id") or ""),
            str(observation.get("boot_id") or ""),
        )

    def _validate_data_plane_identity(
        self,
        *,
        binding: BindingSnapshot,
        route_observation: dict[str, Any] | None,
        selected_route: dict[str, Any],
        frame: Any,
    ) -> dict[str, dict[str, Any]]:
        route_identity = (
            str(selected_route.get("provider_id") or ""),
            str(selected_route.get("provider_instance_id") or ""),
            str(selected_route.get("boot_id") or ""),
        )
        if not all(route_identity):
            raise RuntimeError("selected RGB-D route has incomplete provider identity")
        expected = (
            binding.provider_id,
            binding.provider_instance_id,
            binding.boot_id,
        )
        if expected[0] and route_identity[0] != expected[0]:
            raise RuntimeError("RGB-D route provider does not match binding")
        if expected[1] and route_identity[1] != expected[1]:
            raise RuntimeError("RGB-D route instance does not match current binding")
        if expected[2] and route_identity[2] != expected[2]:
            raise RuntimeError("RGB-D route boot does not match current binding")

        observations = (
            frame.observations if isinstance(frame.observations, dict) else {}
        )
        named_observations = {
            "route": route_observation,
            "bundle": observations.get("bundle"),
            "calibration": observations.get("calibration"),
        }
        temporal_evidence: dict[str, dict[str, Any]] = {}
        for name, observation in named_observations.items():
            identity = self._observation_identity(observation)
            if identity != route_identity:
                raise RuntimeError(
                    f"{name} observation identity does not match selected RGB-D route"
                )
            temporal_evidence[name] = self._evaluate_observation_time(
                name,
                observation,
            )
        self._validate_calibration_revision(
            selected_route=selected_route,
            frame=frame,
        )
        return temporal_evidence

    @staticmethod
    def _validate_calibration_revision(
        *,
        selected_route: dict[str, Any],
        frame: Any,
    ) -> None:
        expected = str(frame.calibration_revision or "")
        if not expected:
            raise RuntimeError("captured RGB-D frame has no calibration revision")
        observations = (
            frame.observations if isinstance(frame.observations, dict) else {}
        )
        calibration_observation = observations.get("calibration")
        calibration_data = (
            calibration_observation.get("data")
            if isinstance(calibration_observation, dict)
            else None
        )
        observed = str(
            (
                calibration_data.get("calibration_revision")
                if isinstance(calibration_data, dict)
                else None
            )
            or (
                calibration_observation.get("calibration_revision")
                if isinstance(calibration_observation, dict)
                else None
            )
            or ""
        )
        if observed != expected:
            raise RuntimeError(
                "camera calibration revision changed during RGB-D capture"
            )

        products = selected_route.get("products")
        route_calibration = (
            products.get("calibration")
            if isinstance(products, dict)
            else None
        )
        route_revision = str(
            (
                route_calibration.get("revision")
                if isinstance(route_calibration, dict)
                else None
            )
            or ""
        )
        if route_revision and route_revision != expected:
            raise RuntimeError(
                "RGB-D route calibration revision does not match the captured frame"
            )

        alignments = selected_route.get("alignments")
        if not isinstance(alignments, list):
            return
        for alignment in alignments:
            if (
                not isinstance(alignment, dict)
                or alignment.get("output_channel")
                != "depth_registered_to_rgb"
            ):
                continue
            alignment_revision = str(
                alignment.get("calibration_revision") or ""
            )
            if alignment_revision and alignment_revision != expected:
                raise RuntimeError(
                    "registered-depth alignment revision does not match "
                    "the captured frame"
                )
            return

    def _validate_vio_context(
        self,
        frame: Any,
    ) -> dict[str, dict[str, Any]]:
        observations = (
            frame.observations if isinstance(frame.observations, dict) else {}
        )
        body_pose = observations.get("body_pose")
        vio_status = observations.get("vio_status")
        temporal_evidence = {
            "body_pose": self._evaluate_observation_time(
                "body_pose",
                body_pose,
            ),
            "vio_status": self._evaluate_observation_time(
                "vio_status",
                vio_status,
            ),
        }
        pose_data = (
            body_pose.get("data") if isinstance(body_pose, dict) else None
        )
        vio_data = (
            vio_status.get("data") if isinstance(vio_status, dict) else None
        )
        if not isinstance(pose_data, dict) or not isinstance(vio_data, dict):
            raise RuntimeError("VIO pose or status data is unavailable")
        if str(vio_data.get("tracking_state") or "") != "TRACKING":
            raise RuntimeError("VIO is not in TRACKING state")
        if (
            vio_data.get("convention_id") != WORLD_CONVENTION_ID
            or pose_data.get("convention_id") != WORLD_CONVENTION_ID
        ):
            raise RuntimeError(
                "VIO pose/status do not declare the convention-V2 Z-up world"
            )
        expected_epoch = str(frame.session_epoch)
        if str(pose_data.get("session_epoch") or "") != expected_epoch:
            raise RuntimeError("body pose does not match the captured VIO epoch")
        if str(vio_data.get("session_epoch") or "") != expected_epoch:
            raise RuntimeError("VIO status does not match the captured epoch")
        if str(pose_data.get("world_frame") or "") != str(frame.world_frame):
            raise RuntimeError("body pose world frame changed during RGB-D capture")
        return temporal_evidence

    @staticmethod
    def _validate_coordinate_conventions(
        *,
        selected_route: dict[str, Any],
        frame: Any,
    ) -> None:
        products = selected_route.get("products")
        if not isinstance(products, dict):
            raise RuntimeError("RGB-D route has no product metadata")
        channels = products.get("channels")
        descriptors: list[dict[str, Any]] = []
        if isinstance(channels, dict):
            for name in ("rgb", "depth_registered_to_rgb"):
                descriptor = channels.get(name)
                if not isinstance(descriptor, dict):
                    raise RuntimeError(
                        f"RGB-D route has no {name} channel descriptor"
                    )
                descriptors.append(descriptor)
        else:
            for name in ("rgb", "depth"):
                descriptor = products.get(name)
                if not isinstance(descriptor, dict):
                    raise RuntimeError(
                        f"RGB-D route has no {name} product descriptor"
                    )
                descriptors.append(descriptor)
        if any(
            descriptor.get("coordinate_convention_id")
            != CAMERA_OPTICAL_CONVENTION_ID
            for descriptor in descriptors
        ):
            raise RuntimeError(
                "RGB-D route does not explicitly declare native camera "
                "optical X-right/Y-down/Z-forward coordinates"
            )

        observations = (
            frame.observations if isinstance(frame.observations, dict) else {}
        )
        bundle = observations.get("bundle")
        bundle_data = (
            bundle.get("data") if isinstance(bundle, dict) else None
        )
        conventions = (
            bundle_data.get("coordinate_conventions")
            if isinstance(bundle_data, dict)
            else None
        )
        if (
            not isinstance(conventions, dict)
            or conventions.get("rgb") != CAMERA_OPTICAL_CONVENTION_ID
            or conventions.get("aligned_depth")
            != CAMERA_OPTICAL_CONVENTION_ID
        ):
            raise RuntimeError(
                "captured RGB-D bundle has no explicit optical coordinate "
                "convention"
            )

    def _evaluate_observation_time(
        self,
        name: str,
        observation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return evaluate_observation_temporal_policy(
            observation_name=name,
            observation=observation,
            policy_id=SPATIAL_INPUT_TEMPORAL_POLICY_ID,
            maximum_source_age_ms=self.maximum_source_age_ms[name],
        ).as_dict()

    def _validate_transform(
        self,
        transform: dict[str, Any],
        *,
        source_frame: str,
        target_frame: str,
        timestamp_us: int,
        session_epoch: str | None,
    ) -> None:
        if str(transform.get("from_frame") or "") != source_frame:
            raise RuntimeError("Fabric transform source frame changed")
        if str(transform.get("to_frame") or "") != target_frame:
            raise RuntimeError("Fabric transform target frame changed")
        if int(transform.get("at_us") or 0) != timestamp_us:
            raise RuntimeError("Fabric transform timestamp does not match RGB frame")
        path = transform.get("path")
        if not isinstance(path, list):
            raise RuntimeError("Fabric transform has no path provenance")
        if source_frame != target_frame and not path:
            raise RuntimeError("Fabric transform path is empty")
        for step in path:
            if not isinstance(step, dict):
                raise RuntimeError("Fabric transform path contains invalid metadata")
            step_epoch = step.get("session_epoch")
            if (
                session_epoch is not None
                and step_epoch is not None
                and str(step_epoch) != session_epoch
            ):
                raise RuntimeError("Fabric transform path crossed a VIO session epoch")
            extrapolated = int(step.get("extrapolated_by_us") or 0)
            if extrapolated > self.maximum_transform_extrapolation_us:
                raise RuntimeError("Fabric transform exceeded extrapolation limit")

    @staticmethod
    def _transform_matrix(transform: dict[str, Any]) -> np.ndarray:
        translation = np.asarray(transform.get("translation_m"), dtype=np.float64)
        quaternion = np.asarray(transform.get("rotation_xyzw"), dtype=np.float64)
        if translation.shape != (3,) or not np.all(np.isfinite(translation)):
            raise RuntimeError("Fabric transform translation is invalid")
        if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
            raise RuntimeError("Fabric transform quaternion is invalid")
        norm = float(np.linalg.norm(quaternion))
        if norm <= 1e-12:
            raise RuntimeError("Fabric transform quaternion has zero norm")
        x, y, z, w = quaternion / norm
        matrix = np.eye(4, dtype=np.float64)
        matrix[:3, :3] = np.asarray(
            [
                [
                    1 - 2 * (y * y + z * z),
                    2 * (x * y - z * w),
                    2 * (x * z + y * w),
                ],
                [
                    2 * (x * y + z * w),
                    1 - 2 * (x * x + z * z),
                    2 * (y * z - x * w),
                ],
                [
                    2 * (x * z - y * w),
                    2 * (y * z + x * w),
                    1 - 2 * (x * x + y * y),
                ],
            ],
            dtype=np.float64,
        )
        matrix[:3, 3] = translation
        return matrix

    @staticmethod
    def _registered_depth_valid_region(
        selected_route: dict[str, Any],
    ) -> dict[str, Any] | None:
        products = selected_route.get("products")
        if not isinstance(products, dict):
            return None
        channels = products.get("channels")
        if not isinstance(channels, dict):
            return None
        channel = channels.get("depth_registered_to_rgb")
        if not isinstance(channel, dict):
            return None
        region = channel.get("valid_region")
        return dict(region) if isinstance(region, dict) else None

    @staticmethod
    def _registered_depth_alignment(
        selected_route: dict[str, Any],
    ) -> dict[str, Any] | None:
        alignments = selected_route.get("alignments")
        if not isinstance(alignments, list):
            return None
        for alignment in alignments:
            if (
                isinstance(alignment, dict)
                and alignment.get("output_channel")
                == "depth_registered_to_rgb"
            ):
                return dict(alignment)
        return None
