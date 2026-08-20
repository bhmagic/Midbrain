from __future__ import annotations

import pytest

from locate_arm_base.app import ExclusiveThreadingHTTPServer, Handler, PAGE


def test_generated_developer_page_preserves_javascript_newline_escapes() -> None:
    assert r"join('\n')" in PAGE
    assert r"split(/\r?\n/)" in PAGE


def test_developer_page_uses_manager_grayscale_theme() -> None:
    assert "--bg:#090909" in PAGE
    assert "#0b5ed7" not in PAGE


def test_developer_page_exposes_independent_mask_and_fit_counts() -> None:
    assert 'id="maskCount"' in PAGE
    assert 'id="fitCount"' in PAGE
    assert "request.mask_attempt_count=maskCount" in PAGE
    assert "request.fit_candidate_count=fitCount" in PAGE
    assert "The two counts are independent" in PAGE
    assert "half-voted, once-dilated mask" in PAGE
    assert 'id="maskCount" type="number" min="1" max="8" step="1" value="2"' in PAGE
    assert 'id="fitCount" type="number" min="1" max="8" step="1" value="2"' in PAGE
    assert "selected post-rotation pose" in PAGE


def test_developer_page_exposes_profile_backed_first_vlm_guidance() -> None:
    assert 'id="vlmSeedGuidance"' in PAGE
    assert "appendix.vlm_seed_guidance=$('vlmSeedGuidance').value.trim()" in PAGE
    assert "profile.appendix.vlm_seed_guidance||''" in PAGE
    assert "sent to every independent VLM seed-localization attempt" in PAGE


def test_development_server_refuses_a_duplicate_port_listener() -> None:
    first = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), Handler)
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            ExclusiveThreadingHTTPServer(("127.0.0.1", port), Handler)
    finally:
        first.server_close()
