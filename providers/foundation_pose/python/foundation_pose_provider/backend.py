from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Any

import numpy as np

from .tensorrt_runtime import TensorRtPair


@dataclass(frozen=True)
class EstimateInput:
    rgb: np.ndarray
    depth_m: np.ndarray
    mask: np.ndarray
    intrinsics: tuple[float, ...]
    mesh_path: Path
    mesh_scale_to_m: float


@dataclass(frozen=True)
class EstimateOutput:
    camera_from_centered_mesh: list[list[float]]
    score: float
    hypothesis_count: int
    elapsed_ms: float


InferenceCallback = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_uint64,
    ctypes.c_uint64,
    ctypes.c_uint64,
    ctypes.c_uint64,
    ctypes.POINTER(ctypes.c_char),
    ctypes.c_size_t,
)


class CreateConfig(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("max_hypotheses", ctypes.c_uint32),
        ("refine_iterations", ctypes.c_uint32),
        ("resized_height", ctypes.c_uint32),
        ("resized_width", ctypes.c_uint32),
        ("min_depth_m", ctypes.c_float),
        ("max_depth_m", ctypes.c_float),
        ("refine_crop_ratio", ctypes.c_float),
        ("score_crop_ratio", ctypes.c_float),
        ("rotation_normalizer", ctypes.c_float),
        ("inference_callback", InferenceCallback),
        ("inference_user_data", ctypes.c_void_p),
    ]


class EstimateRequest(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("rgb_host", ctypes.POINTER(ctypes.c_uint8)),
        ("depth_m_host", ctypes.POINTER(ctypes.c_float)),
        ("mask_host", ctypes.POINTER(ctypes.c_uint8)),
        ("height", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("camera_intrinsics_row_major", ctypes.c_float * 9),
        ("mesh_path_utf8", ctypes.c_char_p),
        ("mesh_scale_to_m", ctypes.c_float),
    ]


class EstimateResult(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("camera_from_centered_mesh_column_major", ctypes.c_float * 16),
        ("score", ctypes.c_float),
        ("hypothesis_count", ctypes.c_uint32),
        ("elapsed_ms", ctypes.c_float),
    ]


class NativeFoundationPoseBackend:
    def __init__(self, config: dict[str, Any], root: Path) -> None:
        self.lock = threading.Lock()
        self.root = root
        self.library_path = self._resolve(config["native_library"])
        self.refine_engine_path = self._resolve(config["refine_engine"])
        self.score_engine_path = self._resolve(config["score_engine"])
        for path in (self.library_path, self.refine_engine_path, self.score_engine_path):
            if not path.is_file():
                raise RuntimeError(f"FoundationPose runtime artifact is unavailable: {path}")
        self.dll_directories: list[Any] = []
        if os.name == "nt":
            self.dll_directories.append(os.add_dll_directory(str(self.library_path.parent)))
            cuda_root = str(os.environ.get("CUDA_PATH") or r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8")
            cuda_bin = Path(cuda_root) / "bin"
            if cuda_bin.is_dir():
                self.dll_directories.append(os.add_dll_directory(str(cuda_bin)))
        self.tensorrt = TensorRtPair(self.refine_engine_path, self.score_engine_path)
        self.callback = InferenceCallback(self._inference_callback)
        self.library = ctypes.CDLL(str(self.library_path))
        self.library.midbrain_foundation_pose_create.argtypes = [
            ctypes.POINTER(CreateConfig), ctypes.POINTER(ctypes.c_char), ctypes.c_size_t
        ]
        self.library.midbrain_foundation_pose_create.restype = ctypes.c_void_p
        self.library.midbrain_foundation_pose_estimate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(EstimateRequest),
            ctypes.POINTER(EstimateResult),
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_size_t,
        ]
        self.library.midbrain_foundation_pose_estimate.restype = ctypes.c_int
        self.library.midbrain_foundation_pose_destroy.argtypes = [ctypes.c_void_p]
        create = CreateConfig(
            struct_size=ctypes.sizeof(CreateConfig),
            max_hypotheses=int(config.get("max_hypotheses", 252)),
            refine_iterations=int(config.get("refine_iterations", 2)),
            resized_height=int(config.get("network_height", 160)),
            resized_width=int(config.get("network_width", 160)),
            min_depth_m=float(config.get("min_depth_m", 0.1)),
            max_depth_m=float(config.get("max_depth_m", 4.0)),
            refine_crop_ratio=float(config.get("refine_crop_ratio", 1.2)),
            score_crop_ratio=float(config.get("score_crop_ratio", 1.4)),
            rotation_normalizer=float(config.get("rotation_normalizer", 0.3490658504)),
            inference_callback=self.callback,
            inference_user_data=None,
        )
        error = ctypes.create_string_buffer(2048)
        self.handle = self.library.midbrain_foundation_pose_create(
            ctypes.byref(create), error, len(error)
        )
        if not self.handle:
            raise RuntimeError(error.value.decode("utf-8", errors="replace"))

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    def _inference_callback(
        self,
        _user_data: int,
        kind: int,
        batch: int,
        height: int,
        width: int,
        channels: int,
        rendered: int,
        observed: int,
        primary: int,
        secondary: int,
        stream: int,
        error_message: Any,
        error_capacity: int,
    ) -> int:
        try:
            self.tensorrt.execute(
                kind, batch, height, width, channels, rendered, observed,
                primary, secondary, stream
            )
            return 0
        except Exception as exc:
            encoded = str(exc).encode("utf-8")[: max(0, error_capacity - 1)]
            if error_capacity:
                ctypes.memset(error_message, 0, error_capacity)
                ctypes.memmove(error_message, encoded, len(encoded))
            return 1

    def estimate(self, inputs: EstimateInput) -> EstimateOutput:
        rgb = np.ascontiguousarray(inputs.rgb, dtype=np.uint8)
        depth = np.ascontiguousarray(inputs.depth_m, dtype=np.float32)
        mask = np.ascontiguousarray(inputs.mask, dtype=np.uint8)
        if len(inputs.intrinsics) != 9:
            raise ValueError("camera intrinsics must contain nine row-major values")
        encoded_mesh = str(inputs.mesh_path.resolve()).encode("utf-8")
        request = EstimateRequest(
            struct_size=ctypes.sizeof(EstimateRequest),
            rgb_host=rgb.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            depth_m_host=depth.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            mask_host=mask.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            height=rgb.shape[0],
            width=rgb.shape[1],
            camera_intrinsics_row_major=(ctypes.c_float * 9)(*inputs.intrinsics),
            mesh_path_utf8=encoded_mesh,
            mesh_scale_to_m=float(inputs.mesh_scale_to_m),
        )
        result = EstimateResult(struct_size=ctypes.sizeof(EstimateResult))
        error = ctypes.create_string_buffer(2048)
        with self.lock:
            status = self.library.midbrain_foundation_pose_estimate(
                self.handle, ctypes.byref(request), ctypes.byref(result), error, len(error)
            )
        if status != 0:
            raise RuntimeError(error.value.decode("utf-8", errors="replace"))
        flat = list(result.camera_from_centered_mesh_column_major)
        matrix = [[float(flat[column * 4 + row]) for column in range(4)] for row in range(4)]
        return EstimateOutput(
            camera_from_centered_mesh=matrix,
            score=float(result.score),
            hypothesis_count=int(result.hypothesis_count),
            elapsed_ms=float(result.elapsed_ms),
        )

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.library.midbrain_foundation_pose_destroy(self.handle)
            self.handle = None
        for directory in getattr(self, "dll_directories", []):
            directory.close()
        self.dll_directories = []
