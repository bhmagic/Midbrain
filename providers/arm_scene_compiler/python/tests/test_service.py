from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest

import arm_scene_compiler.service as service_module
from arm_scene_compiler.service import SceneCompilerEngine


class FakePointReader:
    def __init__(self, points_by_stream: dict[str, Any]) -> None:
        self.points_by_stream = points_by_stream

    def read(self, observation: dict[str, Any]) -> np.ndarray:
        value = self.points_by_stream[str(observation["stream"])]
        if isinstance(value, Exception):
            raise value
        return np.asarray(value, dtype=np.float64).reshape((-1, 3))


class FakeFabric:
    def __init__(self, observations: dict[str, dict[str, Any]]) -> None:
        self.observations = observations
        self.published: list[dict[str, Any]] = []
        self.link_z = {
            "link1": 0.1,
            "link2": 0.2,
            "link3": 0.3,
            "link4": 0.4,
            "link5": 0.5,
            "link6": 0.6,
            "rebot_arm_tool": 0.7,
        }

    def latest_optional(self, stream: str) -> dict[str, Any] | None:
        return self.observations.get(stream)

    def transform(self, *, from_frame: str, to_frame: str, **_: Any) -> dict[str, Any]:
        translation = [0.0, 0.0, self.link_z.get(from_frame, 0.0)]
        return {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "translation_m": translation,
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        }

    def publish(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.published.append(observation)
        return {"accepted": True}


def _observation(stream: str, *, sequence: int = 1, data: dict[str, Any] | None = None):
    now_us = time.time_ns() // 1000
    return {
        "schema": "physical_agent.arm_point_cloud",
        "schema_version": 1,
        "stream": stream,
        "provider_id": "test.points",
        "provider_instance_id": "points-instance",
        "boot_id": "points-boot",
        "sequence": sequence,
        "observed_at_us": now_us,
        "freshness_ms": 1000,
        "expires_at_us": now_us + 1_000_000,
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": data or {"contract_version": 1, "points_m": []},
    }


def _config() -> dict[str, Any]:
    return {
        "arm_base_frame": "rebot_arm_base",
        "point_cloud_streams": ["external.points", "camera.points"],
        "semantic_assertion_streams": ["semantic.objects"],
        "link_frames": [
            "rebot_arm_base",
            "link1",
            "link2",
            "link3",
            "link4",
            "link5",
            "link6",
            "rebot_arm_tool",
        ],
        "segment_radii_m": [0.04] * 7,
        "point_cloud_max_age_ms": 1000,
        "semantic_assertion_max_age_ms": 5000,
        "maximum_transform_extrapolation_us": 750000,
        "maximum_source_points": 120000,
        "maximum_spheres": 20000,
        "scene_freshness_ms": 2000,
        "ingest_unclaimed_point_cloud": True,
        "publish_unclaimed_pushable_geometry": True,
    }


def test_engine_falls_back_from_unusable_external_cloud_to_camera() -> None:
    external = _observation("external.points", sequence=2)
    camera = _observation("camera.points", sequence=1)
    external["observed_at_us"] += 10
    fabric = FakeFabric({"external.points": external, "camera.points": camera})
    reader = FakePointReader(
        {
            "external.points": RuntimeError("expired BufferRef"),
            "camera.points": [[0.3, 0.0, 0.65], [0.8, 0.0, 0.1]],
        }
    )
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=reader,
        config=_config(),
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )
    result = engine.compile_once()
    assert result is not None
    assert result["data"]["production"]["source_provenance"]["point_stream"] == "camera.points"
    assert fabric.published == [result]


def test_engine_publishes_semantic_only_fallback_for_reflective_workpiece() -> None:
    points = _observation("camera.points")
    now_us = time.time_ns() // 1000
    semantics = {
        "schema": "physical_agent.arm_semantic_assertions",
        "schema_version": 1,
        "stream": "semantic.objects",
        "provider_id": "test.locator",
        "provider_instance_id": "locator-instance",
        "boot_id": "locator-boot",
        "sequence": 1,
        "observed_at_us": now_us,
        "freshness_ms": 5000,
        "expires_at_us": now_us + 5_000_000,
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": {
            "contract_version": 1,
            "frame_id": "rebot_arm_base",
            "assertions": [
                {
                    "object_id": "toilet-paper",
                    "center_m": [0.2, 0.0, 0.68],
                    "radius_m": 0.06,
                    "type": "WORKPIECE",
                }
            ],
        },
    }
    fabric = FakeFabric({"camera.points": points, "semantic.objects": semantics})
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=FakePointReader({"camera.points": []}),
        config=_config(),
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )
    result = engine.compile_once()
    assert result is not None
    assert result["data"]["production"]["depth_mode"] == "SEMANTIC_ONLY"
    assert result["data"]["spheres"][0]["type"] == "WORK_OBJECT"


def test_engine_publishes_semantic_only_when_point_cloud_is_unavailable() -> None:
    now_us = time.time_ns() // 1000
    semantics = {
        "schema": "physical_agent.arm_semantic_assertions",
        "schema_version": 1,
        "stream": "semantic.objects",
        "provider_id": "test.locator",
        "provider_instance_id": "locator-instance",
        "boot_id": "locator-boot",
        "sequence": 1,
        "observed_at_us": now_us,
        "freshness_ms": 5000,
        "expires_at_us": now_us + 5_000_000,
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": {
            "frame_id": "rebot_arm_base",
            "assertions": [
                {
                    "object_id": "toilet-paper",
                    "center_m": [0.4, 0.1, 0.2],
                    "radius_m": 0.06,
                    "type": "WORKPIECE",
                }
            ],
        },
    }
    fabric = FakeFabric({"semantic.objects": semantics})
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=FakePointReader({}),
        config=_config(),
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )

    result = engine.compile_once()

    assert result is not None
    assert result["data"]["production"]["depth_mode"] == "SEMANTIC_ONLY"
    assert result["data"]["production"]["source_provenance"][
        "point_stream"
    ] == "semantic-only-fallback"
    assert result["data"]["spheres"][0]["type"] == "WORK_OBJECT"


def test_engine_refuses_empty_depth_without_semantic_fallback() -> None:
    points = _observation("camera.points")
    fabric = FakeFabric({"camera.points": points})
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=FakePointReader({"camera.points": []}),
        config=_config(),
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )
    with pytest.raises(RuntimeError, match="DEPTH_UNAVAILABLE_NO_SEMANTIC_FALLBACK"):
        engine.compile_once()


def test_engine_never_publishes_a_scene_that_expired_during_compile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = _observation("camera.points")
    started_us = int(points["observed_at_us"])
    ticks_ns = iter(
        [
            *(started_us * 1000 for _ in range(5)),
            (started_us + 2_100_000) * 1000,
        ]
    )
    monkeypatch.setattr(service_module.time, "time_ns", lambda: next(ticks_ns))
    fabric = FakeFabric({"camera.points": points})
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=FakePointReader(
            {"camera.points": [[0.3, 0.0, 0.65]]}
        ),
        config=_config(),
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )

    with pytest.raises(RuntimeError, match="SCENE_EXPIRED_DURING_COMPILE"):
        engine.compile_once()

    assert fabric.published == []


def _tracked_table_observation(*, coverage_ready: bool) -> dict[str, Any]:
    now_us = time.time_ns() // 1000
    return {
        "schema": "physical_agent.arm_semantic_assertions",
        "schema_version": 1,
        "stream": "robot_arm.scene.tracked_semantic_assertions",
        "provider_id": "perception.sam2_scene_tracker",
        "provider_instance_id": "tracker-instance",
        "boot_id": "tracker-boot",
        "sequence": 7,
        "observed_at_us": now_us,
        "freshness_ms": 3000,
        "expires_at_us": now_us + 3_000_000,
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": {
            "frame_id": "rebot_arm_base",
            "policy": {"policy_id": "table-only", "revision": "policy-7"},
            "coverage": {"ready": coverage_ready},
            "assertions": [
                {
                    "assertion_id": "table:cell:1",
                    "sphere_id": "table:cell:1",
                    "object_id": "table",
                    "center_m": [0.45, -0.08, 0.08],
                    "radius_m": 0.02,
                    "type": "KEEP_OUT",
                    "description": "the table is the only obstacle",
                    "semantic_source": "SAM2_TRACKED_KEEP_OUT",
                },
                {
                    "assertion_id": "table:cell:2",
                    "sphere_id": "table:cell:2",
                    "object_id": "table",
                    "center_m": [0.45, 0.08, 0.08],
                    "radius_m": 0.02,
                    "type": "KEEP_OUT",
                    "description": "the table is the only obstacle",
                    "semantic_source": "SAM2_TRACKED_KEEP_OUT",
                },
            ],
        },
    }


def test_tracked_table_cells_remain_distinct_and_satisfy_required_coverage() -> None:
    stream = "robot_arm.scene.tracked_semantic_assertions"
    tracked = _tracked_table_observation(coverage_ready=True)
    config = _config()
    config.update(
        {
            "semantic_assertion_streams": [stream],
            "required_semantic_coverage_streams": [stream],
            "ingest_unclaimed_point_cloud": False,
            "publish_unclaimed_pushable_geometry": False,
        }
    )
    fabric = FakeFabric({stream: tracked})
    engine = SceneCompilerEngine(
        fabric=fabric,
        point_reader=FakePointReader({}),
        config=config,
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )

    result = engine.compile_once()

    assert result is not None
    table_cells = [
        value
        for value in result["data"]["spheres"]
        if value["object_id"] == "table"
    ]
    assert {value["sphere_id"] for value in table_cells} == {
        "table:cell:1",
        "table:cell:2",
    }
    assert all(value["type"] == "KEEP_OUT" for value in table_cells)
    sources = result["data"]["production"]["source_provenance"][
        "semantic_assertions"
    ]["accepted_sources"]
    assert sources[0]["policy_revision"] == "policy-7"

    assert engine.compile_once() is None
    tracked["sequence"] += 1
    tracked["observed_at_us"] = time.time_ns() // 1000
    tracked["expires_at_us"] = tracked["observed_at_us"] + 3_000_000
    assert engine.compile_once() is not None


def test_required_tracker_coverage_rejects_an_unready_mask() -> None:
    stream = "robot_arm.scene.tracked_semantic_assertions"
    tracked = _tracked_table_observation(coverage_ready=False)
    config = _config()
    config.update(
        {
            "semantic_assertion_streams": [stream],
            "required_semantic_coverage_streams": [stream],
            "ingest_unclaimed_point_cloud": False,
        }
    )
    engine = SceneCompilerEngine(
        fabric=FakeFabric({stream: tracked}),
        point_reader=FakePointReader({}),
        config=config,
        provider_id="world_model.arm_scene_compiler",
        provider_instance_id="compiler-instance",
        boot_id="compiler-boot",
    )

    with pytest.raises(RuntimeError, match="REQUIRED_SEMANTIC_COVERAGE_UNAVAILABLE"):
        engine.compile_once()
