from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

from .math3d import transform_from_payload


PROVIDER_COMPATIBILITY_ROUTE = "PROVIDER_COMPATIBILITY"
FOUNDATIONPOSE_SKILL_ROUTE = "FOUNDATIONPOSE_SKILL"
PROVIDER_EXECUTION_HOST = "PROVIDER"
IN_PROCESS_EXECUTION_HOST = "IN_PROCESS"
# Backward-compatible import name for callers written during the route trial.
SKILL_LOCAL_ROUTE = FOUNDATIONPOSE_SKILL_ROUTE
BASE_POSE_ENGINE_ROUTES = {
    PROVIDER_COMPATIBILITY_ROUTE,
    FOUNDATIONPOSE_SKILL_ROUTE,
}
FOUNDATIONPOSE_SKILL_PACKAGE = "foundation_pose_object_localization"


def _load_finite_foundation_pose_runtime(
    workspace_root: Path,
) -> type[Any]:
    try:
        module = importlib.import_module(FOUNDATIONPOSE_SKILL_PACKAGE)
    except ModuleNotFoundError as error:
        if error.name != FOUNDATIONPOSE_SKILL_PACKAGE:
            raise RuntimeError(
                "the finite FoundationPose Skill has a missing dependency; "
                "run the stationary Skill setup"
            ) from error

        package_dir = (
            workspace_root
            / "skills"
            / "foundation_pose_object_localization"
            / "python"
            / FOUNDATIONPOSE_SKILL_PACKAGE
        )
        package_init = package_dir / "__init__.py"
        if not package_init.is_file():
            raise RuntimeError(
                "the finite FoundationPose Skill runtime is neither installed "
                f"nor present in this checkout at {package_init}; run the "
                "stationary Skill setup"
            ) from error

        spec = importlib.util.spec_from_file_location(
            FOUNDATIONPOSE_SKILL_PACKAGE,
            package_init,
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(
                "the checked-out finite FoundationPose Skill package could "
                f"not be loaded from {package_init}"
            ) from error
        module = importlib.util.module_from_spec(spec)
        sys.modules[FOUNDATIONPOSE_SKILL_PACKAGE] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if sys.modules.get(FOUNDATIONPOSE_SKILL_PACKAGE) is module:
                del sys.modules[FOUNDATIONPOSE_SKILL_PACKAGE]
            raise

    runtime_type = getattr(module, "FiniteFoundationPoseRuntime", None)
    if runtime_type is None:
        raise RuntimeError(
            "the finite FoundationPose Skill package does not export "
            "FiniteFoundationPoseRuntime"
        )
    return runtime_type


def normalize_base_pose_engine_route(config: dict[str, Any]) -> str:
    route = str(
        (config.get("base_pose_engine") or {}).get("active_route")
        or FOUNDATIONPOSE_SKILL_ROUTE
    ).upper()
    if route == "SKILL_LOCAL":
        route = FOUNDATIONPOSE_SKILL_ROUTE
    if route not in BASE_POSE_ENGINE_ROUTES:
        raise ValueError(f"unsupported stationary base-pose engine route: {route}")
    return route


def normalize_foundation_pose_execution_host(config: dict[str, Any]) -> str:
    route = normalize_base_pose_engine_route(config)
    if route == PROVIDER_COMPATIBILITY_ROUTE:
        return PROVIDER_EXECUTION_HOST
    engine_config = config.get("base_pose_engine") or {}
    skill_config = (
        engine_config.get("foundation_pose_skill")
        or engine_config.get("skill_local")
        or {}
    )
    host = str(
        skill_config.get("execution_host") or PROVIDER_EXECUTION_HOST
    ).upper()
    if host not in {PROVIDER_EXECUTION_HOST, IN_PROCESS_EXECUTION_HOST}:
        raise ValueError(
            "unsupported FoundationPose Skill execution host: "
            f"{host}"
        )
    return host


class LocalFoundationPoseEngine:
    """Stationary sampling adapter around the finite FoundationPose Skill runtime."""

    def __init__(self, backend: Any, registry: Any, *, runtime: Any | None = None):
        self.backend = backend
        self.registry = registry
        self.runtime = runtime
        self.last_diagnostics: dict[str, Any] = {}

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        workspace_root: Path,
    ) -> "LocalFoundationPoseEngine":
        engine_config = config.get("base_pose_engine") or {}
        local = (
            engine_config.get("foundation_pose_skill")
            or engine_config.get("skill_local")
            or {}
        )
        FiniteFoundationPoseRuntime = _load_finite_foundation_pose_runtime(
            workspace_root
        )
        runtime = FiniteFoundationPoseRuntime.from_config(local, workspace_root)
        return cls(
            runtime.backend,
            runtime.registry,
            runtime=runtime,
        )

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
            role: (
                self.runtime.model(model_ids[role])
                if self.runtime is not None
                else self.registry.get(
                    model_ids[role],
                    require_mesh=getattr(self.backend, "name", "") != "mock",
                )
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
                    "engine_route": FOUNDATIONPOSE_SKILL_ROUTE,
                    "skill_type": "foundation_pose_object_localization",
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
        close = self.runtime.close if self.runtime is not None else self.backend.close
        await asyncio.to_thread(close)
