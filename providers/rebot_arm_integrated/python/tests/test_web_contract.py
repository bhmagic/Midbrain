from pathlib import Path


def test_developer_duration_input_matches_runtime_envelope() -> None:
    page = (
        Path(__file__).resolve().parents[1]
        / "rebot_arm_integrated"
        / "web"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert (
        'id="duration" type="number" min="0.05" max="60" step="0.05"'
        in page
    )
