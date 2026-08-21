from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from locate_arm_base.app import (
    DEVELOPER_SHUTDOWN_CONFIRMATION,
    ExclusiveThreadingHTTPServer,
    Handler,
    PAGE,
)


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
    assert "half-of-all-acquired-masks, once-dilated mask" in PAGE
    assert "No post-SAM2 VLM review is performed" in PAGE
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


def test_development_server_requires_confirmation_and_shuts_down() -> None:
    server = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), Handler)
    previous_app = getattr(Handler, "app", None)
    Handler.app = SimpleNamespace(
        status=lambda: {
            "skill_id": "locate_arm_base",
            "running": False,
        }
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    host, port = server.server_address
    try:
        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request(
            "POST",
            "/v1/developer/shutdown",
            body=json.dumps({"confirmation": "wrong"}),
            headers={"Content-Type": "application/json"},
        )
        rejected = connection.getresponse()
        rejected.read()
        connection.close()
        assert rejected.status == 400
        assert thread.is_alive()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request(
            "POST",
            "/v1/developer/shutdown",
            body=json.dumps(
                {"confirmation": DEVELOPER_SHUTDOWN_CONFIRMATION}
            ),
        )
        rejected_content_type = connection.getresponse()
        rejected_content_type.read()
        connection.close()
        assert rejected_content_type.status == 400
        assert thread.is_alive()

        connection = http.client.HTTPConnection(host, port, timeout=2)
        connection.request(
            "POST",
            "/v1/developer/shutdown",
            body=json.dumps(
                {"confirmation": DEVELOPER_SHUTDOWN_CONFIRMATION}
            ),
            headers={"Content-Type": "application/json"},
        )
        accepted = connection.getresponse()
        response = json.loads(accepted.read().decode("utf-8"))
        connection.close()
        assert accepted.status == 202
        assert response["status"] == "SHUTDOWN_REQUESTED"
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        if previous_app is None:
            del Handler.app
        else:
            Handler.app = previous_app
