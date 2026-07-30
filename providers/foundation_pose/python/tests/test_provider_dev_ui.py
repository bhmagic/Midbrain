from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


PROVIDER_PATH = Path(__file__).resolve().parents[2] / "provider.py"
SPEC = importlib.util.spec_from_file_location(
    "foundation_pose_provider_dev_ui_entry",
    PROVIDER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_provider_serves_duty_aligned_browser_development_ui() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "models.json"
        registry.write_text(
            json.dumps(
                {
                    "revision": "dev-ui-test",
                    "models": [
                        {
                            "model_id": "generic_fixture",
                            "mesh_path": "not-required.obj",
                            "semantic_frame": "fixture/origin",
                            "mesh_from_semantic": [
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                                0,
                                0,
                                0,
                                0,
                                1,
                            ],
                            "scale_to_m": 1.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            manager_url="http://127.0.0.1:1",
            fabric_url="http://127.0.0.1:1",
            control_port=0,
            backend="mock",
            foundationpose_root=None,
            model_registry=str(registry),
            poll_interval=0.01,
            default_update_hz=3.0,
            default_track_duration_s=30.0,
            pose_freshness_ms=750,
            minimum_mask_pixels=4,
            max_consecutive_failures=10,
            estimate_iterations=5,
            track_iterations=2,
            prepared_model_cache_size=4,
            debug_level=0,
            debug_dir=str(root / "debug"),
        )
        provider = MODULE.FoundationPoseProvider(args)
        MODULE.ControlHandler.provider = provider
        server = ThreadingHTTPServer(("127.0.0.1", 0), MODULE.ControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/dev",
                timeout=3,
            ) as response:
                html = response.read().decode("utf-8")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/dev/developer.css",
                timeout=3,
            ) as response:
                css = response.read().decode("utf-8")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/dev/models",
                timeout=3,
            ) as response:
                models = json.loads(response.read().decode("utf-8"))

            assert "FoundationPose Object Pose" in html
            assert "Latest camera-relative poses" in html
            assert "--mb-bg: #090909" in css
            assert models["models"][0]["model_id"] == "generic_fixture"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            provider.http.close()
