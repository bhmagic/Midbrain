from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import struct
import time
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import numpy as np
from orbbec_femto_provider.shared_memory_access import CameraSharedMemory

from .compiler import (
    build_layered_scene,
    build_profile_self_exclusion_spheres,
    build_scene_observation,
)


class FabricProtocol(Protocol):
    def latest_optional(self, stream: str) -> dict[str, Any] | None: ...

    def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        max_extrapolation_us: int,
    ) -> dict[str, Any]: ...

    def publish(self, observation: dict[str, Any]) -> dict[str, Any]: ...


class FabricClient:
    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.http = httpx.Client(timeout=timeout_s)

    def latest_optional(self, stream: str) -> dict[str, Any] | None:
        response = self.http.get(
            f"{self.base_url}/v1/latest/{quote(stream, safe='')}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Fabric latest response must be an object")
        return value

    def transform(
        self,
        *,
        from_frame: str,
        to_frame: str,
        at_us: int,
        max_extrapolation_us: int,
    ) -> dict[str, Any]:
        response = self.http.get(
            f"{self.base_url}/v1/transform",
            params={
                "from_frame": from_frame,
                "to_frame": to_frame,
                "at_us": int(at_us),
                "max_extrapolation_us": int(max_extrapolation_us),
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Fabric transform response must be an object")
        return value

    def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{self.base_url}/v1/observations",
            json=observation,
        )
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) else {"result": value}

    def close(self) -> None:
        self.http.close()


def _quaternion_rotation_xyzw(value: Any) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("rotation_xyzw must contain four finite values")
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("rotation_xyzw has zero norm")
    x, y, z, w = quaternion / norm
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def transform_points(points: np.ndarray, transform: dict[str, Any]) -> np.ndarray:
    translation = np.asarray(transform.get("translation_m"), dtype=np.float64)
    if translation.shape != (3,) or not np.all(np.isfinite(translation)):
        raise ValueError("transform translation_m must contain three finite values")
    rotation = _quaternion_rotation_xyzw(transform.get("rotation_xyzw"))
    return points @ rotation.T + translation


class PointCloudReader:
    def __init__(self) -> None:
        self._readers: dict[str, CameraSharedMemory] = {}

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()

    def read(self, observation: dict[str, Any]) -> np.ndarray:
        data = observation.get("data")
        if not isinstance(data, dict):
            raise ValueError("point-cloud observation data must be an object")
        buffer_ref = data.get("buffer_ref")
        if isinstance(buffer_ref, dict):
            data = {**buffer_ref, "units": data.get("units", "mm")}
        inline = data.get("points_m")
        if inline is not None:
            points = np.asarray(inline, dtype=np.float64)
            if points.size == 0:
                return np.empty((0, 3), dtype=np.float64)
            if points.ndim != 2 or points.shape[1] != 3:
                raise ValueError("inline points_m must have shape Nx3")
            return points[np.all(np.isfinite(points), axis=1)]

        mapping_name = str(data.get("mapping_name") or "").strip()
        if not mapping_name:
            raise ValueError("point cloud requires points_m or a BufferRef")
        reader = self._readers.get(mapping_name)
        if reader is None:
            reader = CameraSharedMemory(mapping_name).open()
            self._readers[mapping_name] = reader
        payload = reader.read_ref(data)
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        count = width * height
        if count <= 0:
            count = len(payload) // 12
        if count <= 0:
            return np.empty((0, 3), dtype=np.float64)
        record_bytes = len(payload) // count
        if record_bytes < 12 or record_bytes * count > len(payload):
            raise ValueError("point-cloud BufferRef has an unsupported layout")
        dtype = np.dtype(
            {
                "names": ["x", "y", "z"],
                "formats": ["<f4", "<f4", "<f4"],
                "offsets": [0, 4, 8],
                "itemsize": record_bytes,
            }
        )
        records = np.frombuffer(payload, dtype=dtype, count=count)
        points = np.column_stack((records["x"], records["y"], records["z"]))
        units = str(data.get("units") or "mm").strip().lower()
        if units in {"mm", "millimeter", "millimeters"}:
            points = points.astype(np.float64) * 0.001
        elif units in {"m", "meter", "meters"}:
            points = points.astype(np.float64)
        else:
            raise ValueError(f"unsupported point-cloud units {units!r}")
        finite = np.all(np.isfinite(points), axis=1)
        nonzero = np.linalg.norm(points, axis=1) > 1e-9
        return points[finite & nonzero]


def _is_fresh(observation: dict[str, Any], *, now_us: int, maximum_age_ms: int) -> bool:
    if observation.get("valid") is False:
        return False
    observed = int(observation.get("observed_at_us") or 0)
    if observed <= 0 or now_us < observed:
        return False
    allowed = int(maximum_age_ms)
    freshness = observation.get("freshness_ms")
    if freshness is not None:
        allowed = min(allowed, int(freshness))
    expires = int(observation.get("expires_at_us") or 0)
    return (now_us - observed) <= allowed * 1000 and (
        expires <= 0 or now_us <= expires
    )


class SceneCompilerEngine:
    def __init__(
        self,
        *,
        fabric: FabricProtocol,
        point_reader: PointCloudReader,
        config: dict[str, Any],
        provider_id: str,
        provider_instance_id: str,
        boot_id: str,
    ) -> None:
        self.fabric = fabric
        self.point_reader = point_reader
        self.config = config
        self.provider_id = provider_id
        self.provider_instance_id = provider_instance_id
        self.boot_id = boot_id
        self.sequence = 0
        self.last_source_key: tuple[Any, ...] | None = None
        self.last_observation: dict[str, Any] | None = None
        self.last_diagnostics: dict[str, Any] = {}

    def compile_once(self, *, force: bool = False) -> dict[str, Any] | None:
        now_us = time.time_ns() // 1000
        semantic_objects, semantic_diagnostics = self._semantic_objects(
            now_us=now_us,
            max_extrapolation_us=int(
                self.config.get("maximum_transform_extrapolation_us", 750_000)
            ),
        )
        required_coverage = {
            str(value)
            for value in self.config.get("required_semantic_coverage_streams") or []
        }
        accepted_coverage = {
            str(value.get("stream") or "")
            for value in semantic_diagnostics.get("accepted_sources", [])
            if isinstance(value, dict) and value.get("coverage_ready") is True
        }
        missing_coverage = sorted(required_coverage - accepted_coverage)
        if missing_coverage:
            raise RuntimeError(
                "REQUIRED_SEMANTIC_COVERAGE_UNAVAILABLE: "
                + ", ".join(missing_coverage)
            )
        candidate_error: Exception | None = None
        if bool(self.config.get("ingest_unclaimed_point_cloud", False)):
            try:
                candidates = self._point_cloud_candidates(now_us)
            except Exception as error:
                candidate_error = error
                candidates = []
        else:
            candidates = []
        base_frame = str(self.config.get("arm_base_frame") or "rebot_arm_base")
        max_extrapolation_us = int(
            self.config.get("maximum_transform_extrapolation_us", 750_000)
        )
        source = None
        raw_points = None
        points_base = None
        source_frame = ""
        source_errors: list[str] = []
        empty_candidate: tuple[
            dict[str, Any], np.ndarray, np.ndarray, str
        ] | None = None
        for original_candidate in candidates:
            candidate = original_candidate
            candidate_frame = str(
                candidate.get("coordinate_frame")
                or (candidate.get("data") or {}).get("coordinate_frame")
                or candidate.get("frame_id")
                or ""
            ).strip()
            try:
                if not candidate_frame:
                    raise RuntimeError("POINT_CLOUD_FRAME_UNAVAILABLE")
                candidate, candidate_points = self._read_point_cloud_with_latest_retry(
                    candidate,
                    now_us=now_us,
                )
                candidate_frame = str(
                    candidate.get("coordinate_frame")
                    or (candidate.get("data") or {}).get("coordinate_frame")
                    or candidate.get("frame_id")
                    or ""
                ).strip()
                if not candidate_frame:
                    raise RuntimeError("POINT_CLOUD_FRAME_UNAVAILABLE")
                maximum_source_points = int(
                    self.config.get("maximum_source_points", 120_000)
                )
                if candidate_points.shape[0] > maximum_source_points:
                    stride = int(
                        math.ceil(candidate_points.shape[0] / maximum_source_points)
                    )
                    candidate_points = candidate_points[::stride]
                candidate_transform = self.fabric.transform(
                    from_frame=candidate_frame,
                    to_frame=base_frame,
                    at_us=int(candidate.get("observed_at_us") or 0),
                    max_extrapolation_us=max_extrapolation_us,
                )
                transformed = transform_points(candidate_points, candidate_transform)
            except Exception as error:
                source_errors.append(f"{candidate.get('stream')}: {error}")
                continue
            if transformed.size == 0:
                if empty_candidate is None:
                    empty_candidate = (
                        candidate,
                        candidate_points,
                        transformed,
                        candidate_frame,
                    )
                continue
            source = candidate
            raw_points = candidate_points
            points_base = transformed
            source_frame = candidate_frame
            break
        if source is None and empty_candidate is not None:
            source, raw_points, points_base, source_frame = empty_candidate
        if source is None or raw_points is None or points_base is None:
            if not semantic_objects:
                detail = "; ".join(source_errors)
                if candidate_error is not None:
                    detail = str(candidate_error)
                raise RuntimeError("POINT_CLOUD_UNUSABLE: " + detail)
            semantic_times = [
                int(value.get("observed_at_us") or 0)
                for value in semantic_diagnostics.get("accepted_sources", [])
                if isinstance(value, dict)
            ]
            observed_at_us = max(semantic_times, default=now_us)
            source = {
                "stream": "semantic-only-fallback",
                "provider_id": self.provider_id,
                "provider_instance_id": self.provider_instance_id,
                "boot_id": self.boot_id,
                # The complete semantic source key below owns change
                # detection. Using the compiler's sequence here caused a new
                # scene at every poll even when no upstream data changed.
                "sequence": 0,
                "observed_at_us": observed_at_us,
                "coordinate_frame": base_frame,
            }
            raw_points = np.empty((0, 3), dtype=np.float64)
            points_base = raw_points
            source_frame = base_frame
        source_ready_at_us = time.time_ns() // 1000
        semantic_source_key = tuple(
            sorted(
                (
                    str(value.get("stream") or ""),
                    str(value.get("provider_instance_id") or ""),
                    str(value.get("boot_id") or ""),
                    int(value.get("sequence") or 0),
                    str(value.get("policy_revision") or ""),
                )
                for value in semantic_diagnostics.get(
                    "accepted_sources", []
                )
                if isinstance(value, dict)
            )
        )
        collision_geometry = self._current_profile_collision_geometry(
            now_us=now_us,
            observed_at_us=int(source.get("observed_at_us") or now_us),
            max_extrapolation_us=max_extrapolation_us,
        )
        source_key = (
            str(source.get("provider_instance_id") or source.get("provider_id") or ""),
            str(source.get("boot_id") or ""),
            int(source.get("sequence") or 0),
            semantic_source_key,
            collision_geometry["assembly_fingerprint"],
        )
        if not force and source_key == self.last_source_key:
            return None
        observed_at_us = int(source.get("observed_at_us") or 0)
        link_centers = collision_geometry["link_centers_m"]
        effector_spheres = collision_geometry["effector_spheres"]
        self_spheres, self_revision = build_profile_self_exclusion_spheres(
            link_centers,
            collision_geometry["segment_radii_m"],
            effector_spheres,
            assembly_fingerprint=collision_geometry["assembly_fingerprint"],
            maximum_spacing_m=float(self.config.get("self_filter_spacing_m", 0.025)),
        )
        self_filter_ready_at_us = time.time_ns() // 1000
        semantics_ready_at_us = time.time_ns() // 1000
        if points_base.size == 0 and not semantic_objects:
            raise RuntimeError(
                "DEPTH_UNAVAILABLE_NO_SEMANTIC_FALLBACK: point cloud has no valid "
                "geometry and no fresh upstream semantic assertions are available"
            )

        self.sequence += 1
        scene_revision = f"scene-{self.boot_id[:8]}-{self.sequence:012d}"
        scene = build_layered_scene(
            raw_points_arm_base_m=points_base,
            gripper_center_arm_base_m=collision_geometry[
                "controlled_frame_center_m"
            ],
            self_exclusion_spheres=self_spheres,
            self_filter_revision=self_revision,
            semantic_objects=semantic_objects,
            source_provenance={
                "point_stream": source.get("stream"),
                "point_provider_id": source.get("provider_id"),
                "point_provider_instance_id": source.get("provider_instance_id"),
                "point_boot_id": source.get("boot_id"),
                "point_sequence": source.get("sequence"),
                "point_observed_at_us": observed_at_us,
                "point_coordinate_frame": source_frame,
                "semantic_assertion_streams": list(
                    self.config.get("semantic_assertion_streams") or []
                ),
                "semantic_assertions": semantic_diagnostics,
            },
            maximum_spheres=int(self.config.get("maximum_spheres", 20_000)),
            self_filter_margin_m=float(
                self.config.get("self_filter_margin_m", 0.01)
            ),
            publish_unclaimed_pushable_geometry=bool(
                self.config.get("publish_unclaimed_pushable_geometry", False)
            ),
            robot_collision_geometry={
                "frame_id": str(
                    self.config.get("arm_base_frame") or "rebot_arm_base"
                ),
                "assembly_fingerprint": collision_geometry[
                    "assembly_fingerprint"
                ],
                "collision_geometry_profile_revision": collision_geometry[
                    "collision_geometry_profile_revision"
                ],
                "mounted_effector_profile_revision": collision_geometry[
                    "mounted_effector_profile_revision"
                ],
                "effector_spheres": effector_spheres,
            },
            scene_revision=scene_revision,
        )
        scene_built_at_us = time.time_ns() // 1000
        freshness_ms = int(self.config.get("scene_freshness_ms", 2000))
        prepared_at_us = time.time_ns() // 1000
        source_age_at_publish_ms = max(
            0.0,
            (prepared_at_us - observed_at_us) / 1000.0,
        )
        if source_age_at_publish_ms >= freshness_ms:
            raise RuntimeError(
                "SCENE_EXPIRED_DURING_COMPILE: source age at publication "
                f"was {source_age_at_publish_ms:.1f} ms for a "
                f"{freshness_ms} ms scene policy"
            )
        observation = build_scene_observation(
            scene,
            provider_id=self.provider_id,
            provider_instance_id=self.provider_instance_id,
            boot_id=self.boot_id,
            sequence=self.sequence,
            observed_at_us=observed_at_us,
            freshness_ms=freshness_ms,
        )
        publish_started_at_us = time.time_ns() // 1000
        self.fabric.publish(observation)
        published_at_us = time.time_ns() // 1000
        self.last_source_key = source_key
        self.last_observation = observation
        self.last_diagnostics = {
            "compiled_at_us": published_at_us,
            "compile_duration_ms": max(
                0.0,
                (published_at_us - now_us) / 1000.0,
            ),
            "source_age_at_publish_ms": max(
                0.0,
                (published_at_us - observed_at_us) / 1000.0,
            ),
            "remaining_scene_freshness_ms": max(
                0.0,
                freshness_ms
                - (published_at_us - observed_at_us) / 1000.0,
            ),
            "phase_duration_ms": {
                "source_decode_and_transform": max(
                    0.0,
                    (source_ready_at_us - now_us) / 1000.0,
                ),
                "self_filter_transform_queries": max(
                    0.0,
                    (self_filter_ready_at_us - source_ready_at_us) / 1000.0,
                ),
                "semantic_assertion_merge": max(
                    0.0,
                    (semantics_ready_at_us - self_filter_ready_at_us) / 1000.0,
                ),
                "voxel_scene_build": max(
                    0.0,
                    (scene_built_at_us - semantics_ready_at_us) / 1000.0,
                ),
                "observation_build": max(
                    0.0,
                    (publish_started_at_us - scene_built_at_us) / 1000.0,
                ),
                "fabric_publish": max(
                    0.0,
                    (published_at_us - publish_started_at_us) / 1000.0,
                ),
            },
            "source_key": list(source_key),
            "source_point_count": int(raw_points.shape[0]),
            "transformed_point_count": int(points_base.shape[0]),
            "semantic_object_count": len(semantic_objects),
            "semantic_assertions": semantic_diagnostics,
            "scene_sphere_count": len(scene["spheres"]),
            "depth_mode": scene["production"]["depth_mode"],
            "self_filter_revision": self_revision,
        }
        return observation

    def _read_point_cloud_with_latest_retry(
        self,
        candidate: dict[str, Any],
        *,
        now_us: int,
    ) -> tuple[dict[str, Any], np.ndarray]:
        """Copy a finite-retention BufferRef, refreshing it after slot races."""
        stream = str(candidate.get("stream") or "").strip()
        attempts = max(1, int(self.config.get("buffer_ref_read_attempts", 4)))
        current = candidate
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return current, self.point_reader.read(current)
            except Exception as error:
                last_error = error
                message = str(error).lower()
                transient = (
                    "bufferref has expired" in message
                    or "slot was recycled" in message
                    or "consistent shared-memory payload" in message
                )
                if not transient or not stream or attempt + 1 >= attempts:
                    raise
                refreshed = self.fabric.latest_optional(stream)
                refreshed_now_us = time.time_ns() // 1000
                if refreshed is None or not _is_fresh(
                    refreshed,
                    now_us=refreshed_now_us,
                    maximum_age_ms=int(
                        self.config.get("point_cloud_max_age_ms", 1000)
                    ),
                ):
                    continue
                current = refreshed
        raise RuntimeError(f"point-cloud BufferRef copy failed: {last_error}")

    def _point_cloud_candidates(self, now_us: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        max_age_ms = int(self.config.get("point_cloud_max_age_ms", 1000))
        for stream in self.config.get("point_cloud_streams") or []:
            try:
                observation = self.fabric.latest_optional(str(stream))
            except Exception as error:
                errors.append(f"{stream}: {error}")
                continue
            if observation is not None and _is_fresh(
                observation,
                now_us=now_us,
                maximum_age_ms=max_age_ms,
            ):
                candidates.append(observation)
        if not candidates:
            suffix = "; ".join(errors)
            raise RuntimeError(
                "POINT_CLOUD_UNAVAILABLE: no fresh configured point-cloud "
                f"observation{': ' + suffix if suffix else ''}"
            )
        return sorted(
            candidates,
            key=lambda value: int(value.get("observed_at_us") or 0),
            reverse=True,
        )

    def _current_frame_transforms(
        self,
        frames: list[str],
        *,
        observed_at_us: int,
        max_extrapolation_us: int,
    ) -> dict[str, dict[str, Any]]:
        base_frame = str(self.config.get("arm_base_frame") or "rebot_arm_base")
        normalized_frames = [str(value).strip() for value in frames]
        if not normalized_frames or any(not value for value in normalized_frames):
            raise ValueError("collision frame IDs must be non-empty")
        if len(normalized_frames) != len(set(normalized_frames)):
            raise ValueError("collision frame IDs must be unique")
        transforms: dict[str, dict[str, Any]] = {
            base_frame: {
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
        }

        def query(frame: str) -> tuple[str, dict[str, Any]]:
            transform = self.fabric.transform(
                from_frame=frame,
                to_frame=base_frame,
                at_us=observed_at_us,
                max_extrapolation_us=max_extrapolation_us,
            )
            translation = np.asarray(transform.get("translation_m"), dtype=np.float64)
            if translation.shape != (3,) or not np.all(np.isfinite(translation)):
                raise RuntimeError(f"ARM_SELF_FILTER_TRANSFORM_INVALID: {frame}")
            _quaternion_rotation_xyzw(transform.get("rotation_xyzw"))
            return frame, transform

        query_frames = [
            frame for frame in normalized_frames if frame != base_frame
        ]
        workers = min(
            max(1, len(query_frames)),
            max(
                1,
                int(self.config.get("self_filter_transform_workers", 8)),
            ),
        )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="arm-self-filter-transform",
        ) as executor:
            transforms.update(executor.map(query, query_frames))
        return transforms

    def _current_profile_collision_geometry(
        self,
        *,
        now_us: int,
        observed_at_us: int,
        max_extrapolation_us: int,
    ) -> dict[str, Any]:
        stream = str(
            self.config.get("assembly_state_stream")
            or "robot_arm.assembly_state"
        )
        observation = self.fabric.latest_optional(stream)
        if observation is None or not _is_fresh(
            observation,
            now_us=now_us,
            maximum_age_ms=int(
                self.config.get("assembly_state_max_age_ms", 5000)
            ),
        ):
            raise RuntimeError(
                "ASSEMBLY_COLLISION_PROFILE_UNAVAILABLE: no fresh active assembly"
            )
        assembly = observation.get("data")
        if not isinstance(assembly, dict):
            raise RuntimeError(
                "ASSEMBLY_COLLISION_PROFILE_INVALID: assembly data is absent"
            )
        fingerprint = str(assembly.get("assembly_fingerprint") or "").strip()
        collision = assembly.get("collision_geometry")
        collision = collision if isinstance(collision, dict) else {}
        effector = assembly.get("mounted_effector")
        effector = effector if isinstance(effector, dict) else {}
        point_frames = collision.get("polyline_point_frames")
        capsules = collision.get("polyline_capsules")
        primitives = effector.get("collision_primitives")
        controlled = effector.get("controlled_frame")
        controlled = controlled if isinstance(controlled, dict) else {}
        controlled_frame = str(controlled.get("frame_id") or "").strip()
        if (
            not fingerprint
            or not isinstance(point_frames, list)
            or len(point_frames) < 2
            or not isinstance(capsules, list)
            or len(capsules) != len(point_frames) - 1
            or not isinstance(primitives, list)
            or not controlled_frame
        ):
            raise RuntimeError(
                "ASSEMBLY_COLLISION_PROFILE_INVALID: incomplete profile geometry"
            )
        ordered_capsules = sorted(
            capsules,
            key=lambda value: int(value.get("segment_index", -1)),
        )
        if [int(value.get("segment_index", -1)) for value in ordered_capsules] != list(
            range(len(ordered_capsules))
        ):
            raise RuntimeError(
                "ASSEMBLY_COLLISION_PROFILE_INVALID: capsule indices are not contiguous"
            )
        radii = [float(value.get("radius_m") or 0.0) for value in ordered_capsules]
        if any(not math.isfinite(value) or value <= 0.0 for value in radii):
            raise RuntimeError(
                "ASSEMBLY_COLLISION_PROFILE_INVALID: capsule radii are invalid"
            )
        frame_ids = [str(value).strip() for value in point_frames]
        primitive_frames: list[str] = []
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise RuntimeError(
                    "ASSEMBLY_COLLISION_PROFILE_INVALID: primitive is not an object"
                )
            primitive_frame = str(primitive.get("frame_id") or "").strip()
            if primitive_frame != controlled_frame:
                raise RuntimeError(
                    "ASSEMBLY_COLLISION_PROFILE_INVALID: effector primitive is not in the controlled frame"
                )
            primitive_frames.append(primitive_frame)
        transforms = self._current_frame_transforms(
            list(dict.fromkeys([*frame_ids, controlled_frame, *primitive_frames])),
            observed_at_us=observed_at_us,
            max_extrapolation_us=max_extrapolation_us,
        )
        centers = np.asarray(
            [transforms[frame]["translation_m"] for frame in frame_ids],
            dtype=np.float64,
        )
        effector_spheres: list[dict[str, Any]] = []
        primitive_ids: set[str] = set()
        for primitive in primitives:
            primitive_id = str(primitive.get("primitive_id") or "").strip()
            shape = primitive.get("shape")
            shape = shape if isinstance(shape, dict) else {}
            primitive_transform = primitive.get("transform")
            primitive_transform = (
                primitive_transform
                if isinstance(primitive_transform, dict)
                else {}
            )
            if (
                not primitive_id
                or primitive_id in primitive_ids
                or shape.get("type") != "SPHERE"
            ):
                raise RuntimeError(
                    "ASSEMBLY_COLLISION_PROFILE_INVALID: effector sphere identity or type is invalid"
                )
            offset = np.asarray(
                primitive_transform.get("translation_m"),
                dtype=np.float64,
            )
            radius = float(shape.get("radius_m") or 0.0)
            if (
                offset.shape != (3,)
                or not np.all(np.isfinite(offset))
                or not math.isfinite(radius)
                or radius <= 0.0
            ):
                raise RuntimeError(
                    "ASSEMBLY_COLLISION_PROFILE_INVALID: effector sphere geometry is invalid"
                )
            primitive_ids.add(primitive_id)
            frame_transform = transforms[controlled_frame]
            center = transform_points(offset.reshape(1, 3), frame_transform)[0]
            effector_spheres.append(
                {
                    "primitive_id": primitive_id,
                    "center_m": center.tolist(),
                    "radius_m": radius,
                    "profile_translation_m": offset.tolist(),
                }
            )
        return {
            "assembly_fingerprint": fingerprint,
            "collision_geometry_profile_revision": str(
                collision.get("profile_revision") or ""
            ),
            "mounted_effector_profile_revision": str(
                effector.get("profile_revision") or ""
            ),
            "link_centers_m": centers,
            "controlled_frame_center_m": list(
                transforms[controlled_frame]["translation_m"]
            ),
            "segment_radii_m": radii,
            "effector_spheres": effector_spheres,
        }

    def _semantic_objects(
        self,
        *,
        now_us: int,
        max_extrapolation_us: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        base_frame = str(self.config.get("arm_base_frame") or "rebot_arm_base")
        max_age_ms = int(self.config.get("semantic_assertion_max_age_ms", 5000))
        output: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        accepted_sources: list[dict[str, Any]] = []
        for stream in self.config.get("semantic_assertion_streams") or []:
            observation = self.fabric.latest_optional(str(stream))
            if observation is None or not _is_fresh(
                observation,
                now_us=now_us,
                maximum_age_ms=max_age_ms,
            ):
                continue
            data = observation.get("data")
            data = data if isinstance(data, dict) else {}
            assertions = data.get("assertions", data.get("objects", []))
            if not isinstance(assertions, list):
                skipped.append({"stream": str(stream), "reason": "assertions_not_array"})
                continue
            source_frame = str(
                data.get("frame_id")
                or observation.get("coordinate_frame")
                or base_frame
            ).strip()
            transform = None
            if source_frame != base_frame:
                transform = self.fabric.transform(
                    from_frame=source_frame,
                    to_frame=base_frame,
                    at_us=int(observation.get("observed_at_us") or now_us),
                    max_extrapolation_us=max_extrapolation_us,
                )
            accepted_before = len(output)
            for raw in assertions:
                if not isinstance(raw, dict):
                    skipped.append({"stream": str(stream), "reason": "assertion_not_object"})
                    continue
                expires = int(raw.get("expires_at_us") or 0)
                if expires > 0 and now_us > expires:
                    continue
                try:
                    center = np.asarray(raw.get("center_m"), dtype=np.float64).reshape(1, 3)
                    if transform is not None:
                        center = transform_points(center, transform)
                    output.append(
                        {
                            **raw,
                            "center_m": center[0].tolist(),
                            "semantic_source": str(
                                raw.get("semantic_source")
                                or observation.get("provider_id")
                                or "UPSTREAM_EXPLICIT"
                            ),
                        }
                    )
                except Exception as error:
                    skipped.append({"stream": str(stream), "reason": str(error)})
            if len(output) > accepted_before:
                coverage = data.get("coverage")
                coverage = coverage if isinstance(coverage, dict) else {}
                source_policy = data.get("policy")
                source_policy = (
                    source_policy if isinstance(source_policy, dict) else {}
                )
                accepted_sources.append(
                    {
                        "stream": str(stream),
                        "provider_id": str(
                            observation.get("provider_id") or ""
                        ),
                        "provider_instance_id": str(
                            observation.get("provider_instance_id") or ""
                        ),
                        "boot_id": str(observation.get("boot_id") or ""),
                        "sequence": int(observation.get("sequence") or 0),
                        "observed_at_us": int(
                            observation.get("observed_at_us") or now_us
                        ),
                        "coverage_ready": bool(coverage.get("ready", True)),
                        "policy_id": str(
                            source_policy.get("policy_id") or ""
                        ),
                        "policy_revision": str(
                            source_policy.get("revision") or ""
                        ),
                    }
                )
        by_id: dict[str, dict[str, Any]] = {}
        for value in output:
            geometry_id = str(
                value.get("sphere_id")
                or value.get("assertion_id")
                or value.get("object_id")
                or ""
            )
            if geometry_id:
                by_id[geometry_id] = value
        return list(by_id.values()), {
            "accepted": len(by_id),
            "accepted_sources": accepted_sources,
            "skipped": skipped,
        }
