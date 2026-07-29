from __future__ import annotations

from pathlib import Path


WEB_ROOT = (
    Path(__file__).resolve().parents[1]
    / "vegetable_cutting"
    / "web"
)


def test_gui_exposes_supervised_execution_and_persistent_errors() -> None:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="alignmentButton"' in html
    assert 'id="restartGuiButton"' in html
    assert 'id="resetFailedSessionButton"' in html
    assert "Reload GUI" in html
    assert "Reset failed session" in html
    assert 'id="repeatBladeButton"' not in html
    assert 'id="persistentError"' in html
    assert 'id="executionSequence"' in html
    assert 'id="acceptCalibrationButton"' not in html
    assert 'id="toolAttached"' in html
    assert 'id="executeButton"' in html
    assert 'id="firstCutYesButton"' in html
    assert 'id="toolRemovedButton"' in html
    assert "/api/alignment/gui" in script
    assert "/api/gui/restart" in script
    assert "/api/session/reset-failed" in script
    assert "window.location.reload()" in script
    assert "progress.state === \"FAILED\"" in script
    assert "previous_process_id" not in script
    assert "/api/session/repeat-blade-observation" not in script
    assert "Last error:" in script
    assert "/api/session/accept-blade-calibration" not in script
    assert "operator_confirms_knife_attached" in script
    assert "/api/session/execute" in script
    assert "/api/session/first-cut-decision" in script
    assert "/api/session/revalidate" not in script
    assert "/api/session/tool-removed-safe-terminate" in script
    assert "TAKEOVER GATED" in script
