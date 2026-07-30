from __future__ import annotations

from types import SimpleNamespace

import pytest

from foundation_pose_object_localization import FiniteFoundationPoseRuntime


class _Backend:
    name = "test"

    def __init__(self) -> None:
        self.closed = 0
        self.reset_sessions: list[str] = []

    def reset(self, session_id: str) -> None:
        self.reset_sessions.append(session_id)

    def close(self) -> None:
        self.closed += 1

    def diagnostics(self):
        return {"runtime_loaded": self.closed == 0}


class _Registry:
    def __init__(self) -> None:
        self.calls = []

    def get(self, model_id: str, *, require_mesh: bool):
        self.calls.append((model_id, require_mesh))
        return SimpleNamespace(model_id=model_id)


def test_runtime_owns_model_lookup_sessions_and_idempotent_close() -> None:
    backend = _Backend()
    registry = _Registry()
    runtime = FiniteFoundationPoseRuntime(backend, registry)

    model = runtime.model("robot-base")
    runtime.reset("session-1")
    before = runtime.diagnostics()
    runtime.close()
    runtime.close()

    assert model.model_id == "robot-base"
    assert registry.calls == [("robot-base", True)]
    assert backend.reset_sessions == ["session-1"]
    assert before["owner"] == "skill.foundation_pose_object_localization"
    assert backend.closed == 1
    with pytest.raises(RuntimeError, match="already closed"):
        runtime.model("robot-base")
