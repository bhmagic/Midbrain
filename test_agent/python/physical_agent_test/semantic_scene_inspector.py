from __future__ import annotations

import asyncio
import struct
import time
from typing import Any, Protocol

import httpx


SEMANTIC_SCENE_STREAM = "robot_arm.primary.integrated.scene"
SEMANTIC_SCENE_SCHEMA = "physical_agent.arm_semantic_sphere_scene"
TRACKED_ASSERTION_STREAM = "robot_arm.scene.tracked_semantic_assertions"
TRACKED_ASSERTION_PROVIDER_ID = "perception.sam2_scene_tracker"
TRACKED_ASSERTION_CAPABILITY = "perception.scene.semantic_obstacles"
SEMANTIC_SCENE_CAPABILITY = "world_model.arm.semantic_scene"
SCENE_POLICY_STREAM = "robot_arm.scene.segmentation_policy"
SCENE_POLICY_SCHEMA = "physical_agent.arm_scene_segmentation_policy"


class FabricSceneProtocol(Protocol):
    async def latest_optional(self, stream: str) -> dict[str, Any] | None: ...


class VisualEvidenceStoreProtocol(Protocol):
    async def register_channels(self, **kwargs: Any) -> dict[str, Any]: ...


class SemanticSceneInspector:
    """Read and summarize the current Fabric-hosted arm semantic scene."""

    def __init__(
        self,
        fabric: FabricSceneProtocol,
        *,
        stream: str = SEMANTIC_SCENE_STREAM,
        provider_id: str = "world_model.arm_scene_compiler",
        tracked_assertion_stream: str = TRACKED_ASSERTION_STREAM,
        tracked_assertion_provider_id: str = TRACKED_ASSERTION_PROVIDER_ID,
        policy_stream: str = SCENE_POLICY_STREAM,
        tracker_base_url: str | None = None,
        visual_evidence_store: VisualEvidenceStoreProtocol | None = None,
        mapping_wait_s: float = 24.0,
    ) -> None:
        self.fabric = fabric
        self.stream = stream
        self.provider_id = provider_id
        self.tracked_assertion_stream = tracked_assertion_stream
        self.tracked_assertion_provider_id = tracked_assertion_provider_id
        self.policy_stream = str(policy_stream)
        self.tracker_base_url = str(tracker_base_url or "").rstrip("/")
        self.visual_evidence_store = visual_evidence_store
        self.mapping_wait_s = max(0.0, float(mapping_wait_s))

    @staticmethod
    def _policy_revision(observation: dict[str, Any] | None) -> str:
        if (
            not isinstance(observation, dict)
            or observation.get("schema") != SCENE_POLICY_SCHEMA
        ):
            return ""
        data = observation.get("data")
        data = data if isinstance(data, dict) else {}
        return str(data.get("revision") or "").strip()

    @staticmethod
    def _tracked_policy_revision(
        observation: dict[str, Any] | None,
    ) -> str:
        data = (
            observation.get("data")
            if isinstance(observation, dict)
            else None
        )
        data = data if isinstance(data, dict) else {}
        policy = data.get("policy")
        policy = policy if isinstance(policy, dict) else {}
        return str(policy.get("revision") or "").strip()

    @staticmethod
    def _compiled_policy_revisions(
        observation: dict[str, Any] | None,
    ) -> set[str]:
        data = (
            observation.get("data")
            if isinstance(observation, dict)
            else None
        )
        data = data if isinstance(data, dict) else {}
        production = data.get("production")
        production = production if isinstance(production, dict) else {}
        provenance = production.get("source_provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        semantics = provenance.get("semantic_assertions")
        semantics = semantics if isinstance(semantics, dict) else {}
        sources = semantics.get("accepted_sources")
        sources = sources if isinstance(sources, list) else []
        return {
            str(value.get("policy_revision") or "").strip()
            for value in sources
            if isinstance(value, dict) and value.get("policy_revision")
        }

    async def _scene_for_current_policy(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        deadline = time.monotonic() + self.mapping_wait_s
        while True:
            scene, policy, tracked = await asyncio.gather(
                self.fabric.latest_optional(self.stream),
                self.fabric.latest_optional(self.policy_stream),
                self.fabric.latest_optional(self.tracked_assertion_stream),
            )
            current_revision = self._policy_revision(policy)
            if not current_revision:
                return scene, None
            if current_revision in self._compiled_policy_revisions(scene):
                return scene, None

            tracked_data = (
                tracked.get("data") if isinstance(tracked, dict) else None
            )
            tracked_data = (
                tracked_data if isinstance(tracked_data, dict) else {}
            )
            mapping_failure = tracked_data.get("mapping_failure")
            mapping_failure = (
                mapping_failure
                if isinstance(mapping_failure, dict)
                else {}
            )
            if (
                self._tracked_policy_revision(tracked) == current_revision
                and mapping_failure.get("status")
            ):
                failure_status = str(mapping_failure.get("status") or "")
                if "CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE" in failure_status:
                    prerequisite = mapping_failure.get("blocking_prerequisite")
                    prerequisite = (
                        prerequisite if isinstance(prerequisite, dict) else {}
                    )
                    return None, {
                        "status": "ARM_BASE_TRANSFORM_REQUIRED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "policy_revision": current_revision,
                        "reason": failure_status,
                        "required_transform": {
                            "from_frame": (
                                prerequisite.get("from_frame")
                                or mapping_failure.get("from_frame")
                            ),
                            "to_frame": (
                                prerequisite.get("to_frame")
                                or mapping_failure.get("to_frame")
                                or "rebot_arm_base"
                            ),
                        },
                        "mapping_failure": mapping_failure,
                        "message": (
                            "SAM2 accepted the 2D masks, but no current "
                            "camera-to-arm-base transform is available. "
                            "Establish or restore the arm-base calibration, "
                            "then run scene mapping again."
                        ),
                    }
                return None, {
                    "status": "SCENE_MAPPING_FAILED",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "policy_revision": current_revision,
                    "reason": failure_status,
                    "mapping_failure": mapping_failure,
                    "message": (
                        "The VLM rejected the SAM2 mapping after three "
                        "annotation-segmentation-review attempts. No spheres "
                        "from this policy revision were published."
                    ),
                }
            if time.monotonic() >= deadline:
                return scene, {
                    "status": "SCENE_MAPPING_PENDING",
                    "workflow_complete": False,
                    "physical_motion_authorized": False,
                    "policy_revision": current_revision,
                    "compiled_policy_revisions": sorted(
                        self._compiled_policy_revisions(scene)
                    ),
                    "message": (
                        "The latest user policy has not yet produced a "
                        "VLM-reviewed compiled scene. The previous scene is "
                        "not accepted as the result of this mapping request."
                    ),
                }
            await asyncio.sleep(0.25)

    @staticmethod
    def _png_dimensions(payload: bytes) -> tuple[int, int]:
        if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("tracker visualization is not a PNG image")
        width, height = struct.unpack(">II", payload[16:24])
        if width <= 0 or height <= 0:
            raise ValueError("tracker visualization dimensions are invalid")
        return int(width), int(height)

    async def _visual_evidence(
        self,
        *,
        scene_revision: str,
        sphere_count: int,
    ) -> dict[str, Any] | None:
        if self.visual_evidence_store is None or not self.tracker_base_url:
            return None
        paths = (
            ("rgb", "RGB", "/v1/visualization/rgb.png"),
            ("registered_depth", "Registered Depth", "/v1/visualization/depth.png"),
            ("segmentation", "Reviewed SAM2 Mask", "/v1/visualization/composite.png"),
        )
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                responses = await asyncio.gather(
                    *[
                        client.get(self.tracker_base_url + path)
                        for _, _, path in paths
                    ]
                )
            channels = []
            for (channel_id, label, _), response in zip(
                paths,
                responses,
                strict=True,
            ):
                response.raise_for_status()
                payload = bytes(response.content)
                width, height = self._png_dimensions(payload)
                channels.append(
                    {
                        "id": channel_id,
                        "label": label,
                        "image_bytes": payload,
                        "media_type": "image/png",
                        "width": width,
                        "height": height,
                    }
                )
            return await self.visual_evidence_store.register_channels(
                channels=channels,
                default_channel="segmentation",
                title=(
                    f"Obstacle mapping: {sphere_count} collision spheres | "
                    f"{scene_revision}"
                ),
                annotations=[],
                confidence="reviewed",
                model="SAM2 + routed robotic VLM mask review",
                source_skill="inspect_arm_semantic_scene",
            )
        except Exception:
            return None

    async def run(
        self,
        *,
        include_spheres: bool = False,
        maximum_spheres: int = 100,
        include_visual_evidence: bool = True,
    ) -> dict[str, Any]:
        limit = int(maximum_spheres)
        if not 1 <= limit <= 500:
            raise ValueError("maximum_spheres must be between 1 and 500")
        observation, mapping_status = await self._scene_for_current_policy()
        if mapping_status is not None:
            return mapping_status
        if observation is None:
            tracked = await self.fabric.latest_optional(
                self.tracked_assertion_stream
            )
            tracked_data = (
                tracked.get("data")
                if isinstance(tracked, dict)
                and isinstance(tracked.get("data"), dict)
                else {}
            )
            coverage = tracked_data.get("coverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            tracker_ready = (
                isinstance(tracked, dict)
                and tracked.get("valid") is not False
                and coverage.get("ready") is True
            )
            required_provider_id = (
                self.provider_id
                if tracker_ready
                else self.tracked_assertion_provider_id
            )
            required_capability = (
                SEMANTIC_SCENE_CAPABILITY
                if tracker_ready
                else TRACKED_ASSERTION_CAPABILITY
            )
            return {
                "status": (
                    "NO_SCENE"
                    if tracker_ready
                    else "TRACKER_COVERAGE_REQUIRED"
                ),
                "workflow_complete": False,
                "physical_motion_authorized": False,
                "stream": self.stream,
                "tracked_assertion_stream": self.tracked_assertion_stream,
                "tracked_coverage": coverage,
                "required_provider_id": required_provider_id,
                "required_provider_residency": "HOT",
                "required_capability": required_capability,
                "required_next_tool": {
                    "name": "set_provider_residency",
                    "arguments": {
                        "provider_id": required_provider_id,
                        "action": "hot",
                        "required_capability": required_capability,
                    },
                },
                "message": (
                    (
                        "Tracked SAM2 coverage is ready, but no compiled arm "
                        "scene is hosted by Fabric. Activate the HOT arm scene "
                        "compiler, then retry this inspection."
                    )
                    if tracker_ready
                    else (
                        "No ready user-described SAM2 obstacle coverage is "
                        "hosted by Fabric. Publish the scene policy, activate "
                        "the HOT SAM2 tracker, and retry this inspection."
                    )
                ),
            }
        now_us = time.time_ns() // 1000
        observed_at_us = int(observation.get("observed_at_us") or 0)
        age_ms = (
            None
            if observed_at_us <= 0
            else max(0.0, (now_us - observed_at_us) / 1000.0)
        )
        freshness_ms = int(observation.get("freshness_ms") or 0)
        expires_at_us = int(observation.get("expires_at_us") or 0)
        stale = (
            observed_at_us <= 0
            or freshness_ms <= 0
            or (age_ms is not None and age_ms > freshness_ms)
            or (expires_at_us > 0 and now_us > expires_at_us)
        )
        data = observation.get("data")
        data = data if isinstance(data, dict) else {}
        spheres = data.get("spheres")
        spheres = spheres if isinstance(spheres, list) else []
        layers = data.get("roi_layers")
        layers = layers if isinstance(layers, list) else []
        raw_aabbs = data.get("visible_surface_aabbs")
        raw_aabbs = raw_aabbs if isinstance(raw_aabbs, list) else []
        visible_surface_aabbs: list[dict[str, Any]] = []
        expired_aabb_count = 0
        for value in raw_aabbs:
            if not isinstance(value, dict):
                continue
            if str(value.get("type") or "").strip().upper() != "WORK_OBJECT":
                continue
            aabb_observed_at_us = int(value.get("observed_at_us") or 0)
            aabb_freshness_ms = int(value.get("freshness_ms") or 0)
            aabb_expires_at_us = int(value.get("expires_at_us") or 0)
            aabb_fresh = (
                aabb_observed_at_us > 0
                and aabb_freshness_ms > 0
                and now_us - aabb_observed_at_us <= aabb_freshness_ms * 1000
                and aabb_expires_at_us > 0
                and now_us <= aabb_expires_at_us
            )
            if aabb_fresh:
                visible_surface_aabbs.append(value)
            else:
                expired_aabb_count += 1
        type_counts: dict[str, int] = {}
        scope_counts: dict[str, int] = {}
        object_counts: dict[str, int] = {}
        for sphere in spheres:
            if not isinstance(sphere, dict):
                continue
            object_type = str(sphere.get("type") or "UNKNOWN")
            scope = str(sphere.get("roi_scope") or "UNKNOWN")
            type_counts[object_type] = type_counts.get(object_type, 0) + 1
            scope_counts[scope] = scope_counts.get(scope, 0) + 1
            object_id = str(sphere.get("object_id") or "UNKNOWN")
            object_counts[object_id] = object_counts.get(object_id, 0) + 1
        contract_valid = (
            observation.get("schema") == SEMANTIC_SCENE_SCHEMA
            and data.get("contract_version") == 2
            and data.get("frame_id") == "rebot_arm_base"
        )
        status = "SCENE_READY"
        if stale:
            status = "SCENE_STALE"
        elif not contract_valid or observation.get("valid") is False:
            status = "SCENE_INVALID"
        result: dict[str, Any] = {
            "status": status,
            "workflow_complete": status == "SCENE_READY",
            "physical_motion_authorized": False,
            "stream": self.stream,
            "provider_id": observation.get("provider_id"),
            "provider_instance_id": observation.get("provider_instance_id"),
            "boot_id": observation.get("boot_id"),
            "sequence": observation.get("sequence"),
            "observed_at_us": observed_at_us,
            "age_ms": age_ms,
            "freshness_ms": freshness_ms,
            "expires_at_us": expires_at_us,
            "scene_revision": data.get("scene_revision"),
            "frame_id": data.get("frame_id"),
            "contract_valid": contract_valid,
            "roi_layers": layers,
            "sphere_count": len(spheres),
            "sphere_type_counts": type_counts,
            "sphere_scope_counts": scope_counts,
            "sphere_object_counts": object_counts,
            "visible_surface_aabb_count": len(visible_surface_aabbs),
            "expired_visible_surface_aabb_count": expired_aabb_count,
            "visible_surface_aabbs": visible_surface_aabbs,
            "production": data.get("production"),
            "truncated": bool(include_spheres and len(spheres) > limit),
            "message": (
                "The scene is fresh and contract-valid."
                if status == "SCENE_READY"
                else "The scene must not be used for new motion until it is fresh and valid."
            ),
        }
        if include_spheres:
            result["spheres"] = spheres[:limit]
        if status == "SCENE_READY" and include_visual_evidence:
            result["visual_evidence"] = await self._visual_evidence(
                scene_revision=str(data.get("scene_revision") or "unknown"),
                sphere_count=len(spheres),
            )
        return result
