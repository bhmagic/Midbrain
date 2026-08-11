from pathlib import Path


def test_developer_page_exposes_only_free_space_observation_and_safe_controls() -> None:
    web_root = (
        Path(__file__).resolve().parents[1]
        / "rebot_arm_integrated"
        / "web"
    )
    page = (web_root / "index.html").read_text(encoding="utf-8")
    app = (web_root / "app.js").read_text(encoding="utf-8")

    assert "/v1/motion/path-plan → path-commit" in page
    assert 'id="floatButton"' in page
    assert 'id="safeTerminateButton"' in page
    for retired_path in (
        "/v1/engage",
        "/v1/teleop",
        "/v1/settings",
        "/v1/gripper",
        "/v1/contact-baseline",
    ):
        assert retired_path not in page
        assert retired_path not in app
