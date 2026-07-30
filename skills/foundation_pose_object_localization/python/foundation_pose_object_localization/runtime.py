from __future__ import annotations

from pathlib import Path
from typing import Any


class FiniteFoundationPoseRuntime:
    """Own one bounded FoundationPose backend lifetime for a parent Skill."""

    def __init__(self, backend: Any, registry: Any):
        self.backend = backend
        self.registry = registry
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        workspace_root: Path,
    ) -> "FiniteFoundationPoseRuntime":
        try:
            from foundation_pose_provider.backend import (
                MockFoundationPoseBackend,
                NvLabsFoundationPoseBackend,
            )
            from foundation_pose_provider.model_registry import ObjectModelRegistry
        except ImportError as error:
            raise RuntimeError(
                "FoundationPose compatibility backend library is unavailable; "
                "run the FoundationPose Skill setup"
            ) from error

        registry_path = Path(
            str(config.get("model_registry") or "config/foundation_pose/models.json")
        )
        if not registry_path.is_absolute():
            registry_path = workspace_root / registry_path
        backend_name = str(config.get("backend") or "nvlabs").lower()
        if backend_name == "mock":
            backend = MockFoundationPoseBackend()
        elif backend_name == "nvlabs":
            root = Path(
                str(
                    config.get("foundationpose_root")
                    or "providers/foundation_pose/nvlabs/FoundationPose"
                )
            )
            if not root.is_absolute():
                root = workspace_root / root
            backend = NvLabsFoundationPoseBackend(
                root,
                estimate_iterations=int(config.get("estimate_iterations") or 5),
                track_iterations=int(config.get("track_iterations") or 2),
                debug_level=int(config.get("debug_level") or 0),
                debug_dir=workspace_root
                / str(config.get("debug_dir") or "debug/stationary_foundation_pose"),
                prepared_model_cache_size=int(
                    config.get("prepared_model_cache_size") or 4
                ),
            )
        else:
            raise ValueError(f"unsupported FoundationPose Skill backend: {backend_name}")
        return cls(backend, ObjectModelRegistry(registry_path))

    def model(self, model_id: str) -> Any:
        self._ensure_open()
        return self.registry.get(
            model_id,
            require_mesh=getattr(self.backend, "name", "") != "mock",
        )

    def reset(self, session_id: str) -> None:
        self._ensure_open()
        self.backend.reset(session_id)

    def diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.backend, "diagnostics", None)
        backend_details = diagnostics() if callable(diagnostics) else {}
        return {
            "owner": "skill.foundation_pose_object_localization",
            "lifecycle": "FINITE",
            "closed": self._closed,
            "backend": getattr(
                self.backend,
                "name",
                type(self.backend).__name__,
            ),
            "backend_details": backend_details,
        }

    def close(self) -> None:
        if self._closed:
            return
        self.backend.close()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("FoundationPose Skill runtime is already closed")
