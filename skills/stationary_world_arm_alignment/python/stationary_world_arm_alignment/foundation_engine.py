from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

from .math3d import transform_from_payload


PROVIDER_COMPATIBILITY_ROUTE = "PROVIDER_COMPATIBILITY"
SKILL_LOCAL_ROUTE = "SKILL_LOCAL"
BASE_POSE_ENGINE_ROUTES = {
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
}


def normalize_base_pose_engine_route(config: dict[str, Any]) -> str:
    route = str(
        (config.get("base_pose_engine") or {}).get("active_route")
        or PROVIDER_COMPATIBILITY_ROUTE
    ).upper()
    if route not in BASE_POSE_ENGINE_ROUTES:
        raise ValueError(f"unsupported stationary base-pose engine route: {route}")
    return route


class LocalFoundationPoseEngine:
    """Finite Skill-owned adapter around a FoundationPose-compatible backend."""

    def __init__(self, backend: Any, registry: Any):
        self.backend = backend
        self.registry = registry
        self.last_diagnostics: dict[str, Any] = {}

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        workspace_root: Path,
    ) -> "LocalFoundationPoseEngine":
        local = (config.get("base_pose_engine") or {}).get("skill_local") or {}
        try:
            from foundation_pose_provider.backend import (
                MockFoundationPoseBackend,
                NvLabsFoundationPoseBackend,
            )
            from foundation_pose_provider.model_registry import ObjectModelRegistry
        except ImportError as error:
            raise RuntimeError(
                "the temporary FoundationPose backend library route is unavailable; "
                "run the stationary Skill setup before selecting SKILL_LOCAL"
            ) from error

        registry_path = Path(
            str(local.get("model_registry") or "config/foundation_pose/models.json")
        )
        if not registry_path.is_absolute():
            registry_path = workspace_root / registry_path
        backend_name = str(local.get("backend") or "nvlabs").lower()
        if backend_name == "mock":
            backend = MockFoundationPoseBackend()
        elif backend_name == "nvlabs":
            root_value = str(
                local.get("foundationpose_root")
                or "providers/foundation_pose/nvlabs/FoundationPose"
            )
            root = Path(root_value)
            if not root.is_absolute():
                root = workspace_root / root
            backend = NvLabsFoundationPoseBackend(
                root,
                estimate_iterations=int(local.get("estimate_iterations") or 5),
                track_iterations=int(local.get("track_iterations") or 2),
                debug_level=int(local.get("debug_level") or 0),
                debug_dir=workspace_root
                / str(local.get("debug_dir") or "debug/stationary_foundation_pose"),
                prepared_model_cache_size=int(
                    local.get("prepared_model_cache_size") or 4
                ),
            )
        else:
            raise ValueError(f"unsupported local FoundationPose backend: {backend_name}")
        return cls(backend, ObjectModelRegistry(registry_path))

    @staticmethod
    def _camera_matrix(intrinsics: dict[str, Any]) -> np.ndarray:
        matrix = np.asarray(
            [
                [float(intrinsics["fx"]), 0.0, float(intrinsics["cx"])],
                [0.0, float(intrinsics["fy"]), float(intrinsics["cy"])],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        if not np.all(np.isfinite(matrix)) or min(matrix[0, 0], matrix[1, 1]) <= 0:
            raise ValueError("camera intrinsics are invalid")
        return matrix

    async def collect_samples(
        self,
        *,
        skill_id: str,
        attempt: int,
        initial_frame: Any,
        capture: Any,
        fabric: Any,
        masks: dict[str, np.ndarray],
        model_ids: dict[str, str],
        required_counts: dict[str, int],
        hard_timeout_s: float,
        minimum_sample_interval_s: float,
        guard: Callable[[], Awaitable[None]],
        progress: Callable[[dict[str, int], dict[str, Any]], Awaitable[None]]
        | None = None,
    ) -> tuple[
        dict[str, dict[str, list[np.ndarray]]],
        dict[str, str],
    ]:
        roles = tuple(required_counts)
        if set(roles) != set(model_ids) or set(roles) != set(masks):
            raise ValueError("local base-pose roles, masks, and models must match")
        if any(int(required_counts[role]) < 1 for role in roles):
            raise ValueError("every local base-pose role needs at least one sample")

        sessions = {
            role: f"{skill_id}-local-{role}-attempt-{attempt}"
            for role in roles
        }
        models = {
            role: self.registry.get(
                model_ids[role],
                require_mesh=getattr(self.backend, "name", "") != "mock",
            )
            for role in roles
        }
        samples: dict[str, dict[str, list[np.ndarray]]] = {
            role: {"vio": [], "camera": []} for role in roles
        }
        initialized = {role: False for role in roles}
        camera_matrix = self._camera_matrix(initial_frame.intrinsics)
        deadline = time.monotonic() + float(hard_timeout_s)
        frame = initial_frame
        last_frame_number: int | None = None
        started = time.monotonic()
        try:
            while time.monotonic() < deadline:
                await guard()
                if (
                    frame.world_frame != initial_frame.world_frame
                    or frame.session_epoch != initial_frame.session_epoch
                ):
                    raise RuntimeError(
                        "stationary world/VIO epoch changed during local base-pose estimation"
                    )
                if last_frame_number == int(frame.frame_number):
                    await asyncio.sleep(max(0.01, minimum_sample_interval_s))
                    frame = await capture.capture(attempts=3)
                    continue
                last_frame_number = int(frame.frame_number)
                for role in roles:
                    if len(samples[role]["camera"]) >= int(required_counts[role]):
                        continue
                    session_id = sessions[role]
                    if not initialized[role]:
                        mask = np.asarray(masks[role])
                        if mask.shape != frame.depth_m.shape:
                            raise ValueError(
                                f"{role} initialization mask does not match depth grid"
                            )
                        result = await asyncio.to_thread(
                            self.backend.initialize,
                            session_id,
                            models[role],
                            frame.rgb,
                            frame.depth_m,
                            camera_matrix,
                            mask,
                        )
                        initialized[role] = True
                    else:
                        result = await asyncio.to_thread(
                            self.backend.track,
                            session_id,
                            frame.rgb,
                            frame.depth_m,
                            camera_matrix,
                        )
                    camera_from_semantic = (
                        np.asarray(result.camera_from_mesh, dtype=np.float64)
                        @ np.asarray(models[role].mesh_from_semantic, dtype=np.float64)
                    )
                    world_from_camera = transform_from_payload(
                        await fabric.transform(
                            from_frame=frame.camera_frame,
                            to_frame=frame.world_frame,
                            at_us=frame.timestamp_us,
                            max_extrapolation_us=750_000,
                            session_epoch=frame.session_epoch,
                        )
                    )
                    samples[role]["camera"].append(camera_from_semantic)
                    samples[role]["vio"].append(
                        world_from_camera @ camera_from_semantic
                    )
                counts = {
                    role: len(samples[role]["camera"])
                    for role in roles
                }
                diagnostics = {
                    "engine_route": SKILL_LOCAL_ROUTE,
                    "backend": getattr(self.backend, "name", type(self.backend).__name__),
                    "attempt": int(attempt),
                    "counts": counts,
                    "required_counts": {
                        role: int(required_counts[role]) for role in roles
                    },
                    "elapsed_s": time.monotonic() - started,
                    "frame_number": int(frame.frame_number),
                    "sessions": dict(sessions),
                }
                self.last_diagnostics = diagnostics
                if progress is not None:
                    await progress(counts, diagnostics)
                if all(
                    counts[role] >= int(required_counts[role])
                    for role in roles
                ):
                    return samples, sessions
                if minimum_sample_interval_s > 0:
                    await asyncio.sleep(minimum_sample_interval_s)
                frame = await capture.capture(attempts=3)
            raise TimeoutError(
                "Skill-local FoundationPose did not produce enough samples "
                "before the hard timeout"
            )
        finally:
            for session_id in sessions.values():
                try:
                    self.backend.reset(session_id)
                except Exception:
                    pass

    async def close(self) -> None:
        await asyncio.to_thread(self.backend.close)
