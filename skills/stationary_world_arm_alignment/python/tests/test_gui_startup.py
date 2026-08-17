from PIL import Image

from stationary_world_arm_alignment.app import (
    auto_bootstrap_providers_enabled,
    persisted_alignment_image,
)


def test_gui_startup_is_passive_by_default(monkeypatch):
    monkeypatch.delenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", raising=False)

    assert auto_bootstrap_providers_enabled() is False


def test_gui_provider_bootstrap_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", "true")

    assert auto_bootstrap_providers_enabled() is True


def test_gui_reads_latest_images_persisted_by_agent_runtime(tmp_path):
    alignment_id = "alignment-agent-1"
    run_dir = tmp_path / alignment_id
    run_dir.mkdir()
    expected = {}
    for name, image_format in (
        ("camera.jpg", "JPEG"),
        ("depth.png", "PNG"),
        ("overlay.jpg", "JPEG"),
        ("foundation_pose_attempt_1_selected_overlay.jpg", "JPEG"),
    ):
        path = run_dir / name
        Image.new("RGB", (10, 8), color=(10, 20, 30)).save(
            path,
            format=image_format,
        )
        expected[name] = path.read_bytes()

    latest = {"alignment_id": alignment_id}

    assert persisted_alignment_image(
        "rgb", latest=latest, run_root=tmp_path
    ) == expected["camera.jpg"]
    assert persisted_alignment_image(
        "depth", latest=latest, run_root=tmp_path
    ) == expected["depth.png"]
    assert persisted_alignment_image(
        "overlay", latest=latest, run_root=tmp_path
    ) == expected["foundation_pose_attempt_1_selected_overlay.jpg"]


def test_gui_rejects_persisted_image_path_traversal(tmp_path):
    assert (
        persisted_alignment_image(
            "rgb",
            latest={"alignment_id": "../outside"},
            run_root=tmp_path,
        )
        is None
    )
