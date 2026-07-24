from __future__ import annotations

from stationary_world_arm_alignment.skill import AlignmentSkill


def provider_view() -> dict:
    return {
        "config": {"id": "camera.femto_bolt"},
        "process_state": "running",
        "last_exit": None,
        "report": {
            "residency": "HOT",
            "health": "HEALTHY",
            "ready": True,
            "expired": False,
        },
    }


def test_manager_provider_shape_is_normalized_for_the_gui() -> None:
    normalized = AlignmentSkill._provider_views([provider_view()])
    camera = normalized["camera.femto_bolt"]
    assert camera["process_state"] == "running"
    assert camera["residency"] == "HOT"
    assert camera["ready"] is True


def test_nested_manager_provider_is_detected_as_preexisting_hot() -> None:
    assert AlignmentSkill._provider_was_hot(
        [provider_view()],
        "camera.femto_bolt",
    )


def test_stopped_or_warm_provider_is_not_detected_as_hot() -> None:
    value = provider_view()
    value["process_state"] = "stopped"
    value["report"]["residency"] = "WARM"
    assert not AlignmentSkill._provider_was_hot(
        [value],
        "camera.femto_bolt",
    )


def test_exited_provider_does_not_expose_stale_hot_report() -> None:
    value = provider_view()
    value["process_state"] = "exited"
    value["report"]["expired"] = True
    normalized = AlignmentSkill._provider_views([value])["camera.femto_bolt"]
    assert normalized["residency"] is None
    assert normalized["health"] is None
    assert normalized["ready"] is False
