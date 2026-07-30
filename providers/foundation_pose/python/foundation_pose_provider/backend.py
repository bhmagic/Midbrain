"""Backend adapters for FoundationPose-compatible pose estimation."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import time
import gc
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .math3d import as_transform
from .model_registry import ObjectModel
from .nvlabs_compat import verify_windows_temp_path


@dataclass(frozen=True)
class BackendResult:
    """A camera-from-mesh pose returned by a backend."""

    camera_from_mesh: np.ndarray
    score: float | None
    latency_ms: float
    backend_details: dict[str, Any]


class FoundationPoseBackend(ABC):
    """Minimal backend interface kept separate from Midbrain contracts."""

    name = "abstract"

    @abstractmethod
    def initialize(
        self,
        session_id: str,
        model: ObjectModel,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
        mask: np.ndarray,
    ) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def track(
        self,
        session_id: str,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
    ) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def reset(self, session_id: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release optional backend resources."""

    def diagnostics(self) -> dict[str, Any]:
        return {"backend": self.name}


class MockFoundationPoseBackend(FoundationPoseBackend):
    """Deterministic backend for Provider integration tests without CUDA."""

    name = "mock"

    def __init__(self, translation_m: tuple[float, float, float] = (0.0, 0.0, 0.75)):
        self.translation_m = translation_m
        self.initialized: set[str] = set()

    def initialize(
        self,
        session_id: str,
        model: ObjectModel,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
        mask: np.ndarray,
    ) -> BackendResult:
        del model, rgb, camera_matrix
        if mask.shape != depth_m.shape:
            raise ValueError("mask and depth dimensions must match")
        if not np.any(mask):
            raise ValueError("initialization mask is empty")
        start = time.perf_counter()
        pose = np.eye(4, dtype=np.float64)
        valid = depth_m[(mask > 0) & np.isfinite(depth_m) & (depth_m > 0.0)]
        z = float(np.median(valid)) if valid.size else self.translation_m[2]
        pose[:3, 3] = [self.translation_m[0], self.translation_m[1], z]
        self.initialized.add(session_id)
        return BackendResult(
            camera_from_mesh=pose,
            score=1.0,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            backend_details={"mock": True},
        )

    def track(
        self,
        session_id: str,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
    ) -> BackendResult:
        del rgb, depth_m, camera_matrix
        if session_id not in self.initialized:
            raise RuntimeError("session has not been initialized")
        start = time.perf_counter()
        pose = np.eye(4, dtype=np.float64)
        pose[:3, 3] = self.translation_m
        return BackendResult(
            camera_from_mesh=pose,
            score=1.0,
            latency_ms=(time.perf_counter() - start) * 1000.0,
            backend_details={"mock": True},
        )

    def reset(self, session_id: str) -> None:
        self.initialized.discard(session_id)

    def close(self) -> None:
        self.initialized.clear()


class NvLabsFoundationPoseBackend(FoundationPoseBackend):
    """Thin adapter around the official NVLabs FoundationPose Python API.

    The adapter intentionally imports the NVIDIA repository at runtime so the
    Midbrain package does not redistribute or silently install its dependencies.
    Native Windows compatibility depends on the user's local CUDA build of those
    dependencies and is verified by the supplied backend smoke test.
    """

    name = "nvlabs"

    def __init__(
        self,
        foundationpose_root: Path,
        *,
        estimate_iterations: int = 5,
        track_iterations: int = 2,
        debug_level: int = 0,
        debug_dir: Path | None = None,
        prepared_model_cache_size: int = 4,
    ):
        self.foundationpose_root = foundationpose_root.resolve()
        self.estimate_iterations = estimate_iterations
        self.track_iterations = track_iterations
        self.debug_level = debug_level
        if prepared_model_cache_size < 0:
            raise ValueError("prepared_model_cache_size cannot be negative")
        self.prepared_model_cache_size = prepared_model_cache_size
        self.debug_dir = (debug_dir or Path("debug/foundation_pose")).resolve()
        self.lock = threading.RLock()
        self._loaded = False
        self._torch: Any = None
        self._trimesh: Any = None
        self._dr: Any = None
        self._FoundationPose: Any = None
        self._ScorePredictor: Any = None
        self._PoseRefinePredictor: Any = None
        self._scorer: Any = None
        self._refiner: Any = None
        self._glctx: Any = None
        self._estimators: dict[str, Any] = {}
        self._model_ids: dict[str, str] = {}
        self._model_cache_keys: dict[str, str] = {}
        self._idle_estimators: dict[str, list[Any]] = {}

    def _load_runtime(self) -> None:
        if self._loaded:
            return
        if not self.foundationpose_root.is_dir():
            raise FileNotFoundError(
                f"FOUNDATIONPOSE_ROOT does not exist: {self.foundationpose_root}"
            )
        if os.name == "nt":
            verify_windows_temp_path(self.foundationpose_root)
        root_text = str(self.foundationpose_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        estimater = importlib.import_module("estimater")
        self._torch = importlib.import_module("torch")
        self._trimesh = importlib.import_module("trimesh")
        self._dr = importlib.import_module("nvdiffrast.torch")
        self._FoundationPose = getattr(estimater, "FoundationPose")
        self._ScorePredictor = getattr(estimater, "ScorePredictor")
        self._PoseRefinePredictor = getattr(estimater, "PoseRefinePredictor")
        if not bool(self._torch.cuda.is_available()):
            raise RuntimeError("PyTorch CUDA is not available")
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self._scorer = self._ScorePredictor()
        self._refiner = self._PoseRefinePredictor()
        self._glctx = self._dr.RasterizeCudaContext()
        self._loaded = True

    def _model_cache_key(self, model: ObjectModel) -> str:
        mesh_path = model.mesh_path.resolve()
        digest = hashlib.sha256()
        with mesh_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        mesh_digest = digest.hexdigest()
        descriptor = json.dumps(
            {
                "mesh_sha256": mesh_digest,
                "scale_to_m": model.scale_to_m,
                "revision": model.revision,
                "symmetry": model.symmetry,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()

    @staticmethod
    def _clear_estimator_session_state(estimator: Any) -> None:
        for attribute in (
            "pose_last",
            "gt_pose",
            "H",
            "W",
            "K",
            "ob_id",
            "ob_mask",
            "poses",
            "scores",
            "best_id",
        ):
            if hasattr(estimator, attribute):
                setattr(estimator, attribute, None)

    def _build_estimator(
        self, session_id: str, model: ObjectModel
    ) -> tuple[Any, bool]:
        cache_key = self._model_cache_key(model)
        idle = self._idle_estimators.get(cache_key)
        if idle:
            estimator = idle.pop()
            if not idle:
                self._idle_estimators.pop(cache_key, None)
            self._clear_estimator_session_state(estimator)
            estimator.debug_dir = str(self.debug_dir / session_id)
            Path(estimator.debug_dir).mkdir(parents=True, exist_ok=True)
            cache_hit = True
        else:
            estimator = self._create_estimator(session_id, model)
            cache_hit = False
        self._estimators[session_id] = estimator
        self._model_ids[session_id] = model.model_id
        self._model_cache_keys[session_id] = cache_key
        return estimator, cache_hit

    def _create_estimator(self, session_id: str, model: ObjectModel) -> Any:
        mesh = self._trimesh.load(str(model.mesh_path), force="mesh", process=False)
        if model.scale_to_m != 1.0:
            mesh.apply_scale(model.scale_to_m)
        if getattr(mesh, "vertices", None) is None or len(mesh.vertices) == 0:
            raise ValueError(f"mesh has no vertices: {model.mesh_path}")
        estimator = self._FoundationPose(
            model_pts=np.asarray(mesh.vertices),
            model_normals=np.asarray(mesh.vertex_normals),
            mesh=mesh,
            scorer=self._scorer,
            refiner=self._refiner,
            debug_dir=str(self.debug_dir / session_id),
            debug=self.debug_level,
            glctx=self._glctx,
        )
        return estimator

    def initialize(
        self,
        session_id: str,
        model: ObjectModel,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
        mask: np.ndarray,
    ) -> BackendResult:
        with self.lock:
            self._load_runtime()
            estimator = self._estimators.get(session_id)
            cache_hit = False
            if estimator is None or self._model_ids.get(session_id) != model.model_id:
                estimator, cache_hit = self._build_estimator(session_id, model)
            start = time.perf_counter()
            pose = estimator.register(
                K=np.ascontiguousarray(camera_matrix.astype(np.float64)),
                rgb=np.ascontiguousarray(rgb.astype(np.uint8)),
                depth=np.ascontiguousarray(depth_m.astype(np.float32)),
                ob_mask=np.ascontiguousarray(mask.astype(bool)),
                iteration=self.estimate_iterations,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
            return BackendResult(
                camera_from_mesh=as_transform(pose, field_name="FoundationPose register result"),
                score=None,
                latency_ms=latency_ms,
                backend_details={
                    "operation": "register",
                    "prepared_model_cache_hit": cache_hit,
                },
            )

    def track(
        self,
        session_id: str,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        camera_matrix: np.ndarray,
    ) -> BackendResult:
        with self.lock:
            self._load_runtime()
            estimator = self._estimators.get(session_id)
            if estimator is None:
                raise RuntimeError("session has not been initialized")
            start = time.perf_counter()
            pose = estimator.track_one(
                rgb=np.ascontiguousarray(rgb.astype(np.uint8)),
                depth=np.ascontiguousarray(depth_m.astype(np.float32)),
                K=np.ascontiguousarray(camera_matrix.astype(np.float64)),
                iteration=self.track_iterations,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
            return BackendResult(
                camera_from_mesh=as_transform(pose, field_name="FoundationPose track result"),
                score=None,
                latency_ms=latency_ms,
                backend_details={"operation": "track_one"},
            )

    def reset(self, session_id: str) -> None:
        with self.lock:
            estimator = self._estimators.pop(session_id, None)
            self._model_ids.pop(session_id, None)
            cache_key = self._model_cache_keys.pop(session_id, None)
            if estimator is not None and cache_key is not None:
                self._clear_estimator_session_state(estimator)
                if self.prepared_model_cache_size > 0 and cache_key not in self._idle_estimators:
                    while len(self._idle_estimators) >= self.prepared_model_cache_size:
                        oldest_key = next(iter(self._idle_estimators))
                        self._idle_estimators.pop(oldest_key, None)
                    self._idle_estimators[cache_key] = [estimator]

    def close(self) -> None:
        with self.lock:
            self._estimators.clear()
            self._model_ids.clear()
            self._model_cache_keys.clear()
            self._idle_estimators.clear()
            torch_module = self._torch
            self._scorer = None
            self._refiner = None
            self._glctx = None
            self._FoundationPose = None
            self._ScorePredictor = None
            self._PoseRefinePredictor = None
            self._trimesh = None
            self._dr = None
            self._torch = None
            self._loaded = False
        # Drop Python references before asking PyTorch to return unused CUDA
        # allocations. This makes close() a reusable, explicit resource
        # boundary rather than relying on process exit or eventual GC.
        gc.collect()
        if torch_module is not None and bool(torch_module.cuda.is_available()):
            torch_module.cuda.empty_cache()
            ipc_collect = getattr(torch_module.cuda, "ipc_collect", None)
            if callable(ipc_collect):
                ipc_collect()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "foundationpose_root": str(self.foundationpose_root),
            "runtime_loaded": self._loaded,
            "active_estimators": len(self._estimators),
            "cached_prepared_estimators": sum(
                len(estimators) for estimators in self._idle_estimators.values()
            ),
            "prepared_model_cache_size": self.prepared_model_cache_size,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        }
