from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest

import arm_scene_compiler.service as service_module
from arm_scene_compiler.service import (
    FabricClient,
    SceneCompilerEngine,
    bounded_failure_retry_delay_s,
)


def test_fabric_publish_preserves_rejection_detail() -> None:
    def reject(
        request: service_module.httpx.Request,
    ) -> service_module.httpx.Response:
        return service_module.httpx.Response(
            400,
            request=request,
            json={"error": "unsupported semantic ROI scope HAND_ANGULAR_4PI"},
        )

    client = FabricClient("http://fabric")
    client.http.close()
    client.http = service_module.httpx.Client(
        transport=service_module.httpx.MockTransport(reject)
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="unsupported semantic ROI scope HAND_ANGULAR_4PI",
        ):
            client.publish({"schema": "test"})
    finally:
        client.close()


def test_compile_failure_retry_delay_is_bounded_exponential() -> None:
    delays = [
        bounded_failure_retry_delay_s(
            failures,
            initial_s=0.5,
            maximum_s=5.0,
        )
        for failures in range(1, 7)
    ]

    assert delays == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


def _assembly_observation() -> dict[str, Any]:
    now_us = time.time_ns() // 1000
    return {
        "schema": "physical_agent.robot_assembly_state",
        "schema_version": 1,
        "stream": "robot_arm.assembly_state",
        "provider_id": "robot_arm.rebot_dm",
        "provider_instance_id": "arm-instance",
        "boot_id": "arm-boot",
        "sequence": 1,
        "observed_at_us": now_us,
        "freshness_ms": 5000,
        "expires_at_us": now_us + 5_000_000,
        "coordinate_frame": "rebot_arm_base",
        "valid": True,
        "data": {
            "assembly_fingerprint": "test-assembly-fingerprint",
            "collision_geometry": {
                "profile_revision": "test-arm-capsules-v2",
                "polyline_point_frames": [
                    "rebot_arm_base",
                    "link1",
                    "link2",
                    "link3",
                    "link4",
                    "link5",
                    "link6",
                ],
                "polyline_capsules": [
                    {"segment_index": index, "radius_m": 0.04}
                    for index in range(6)
                ],
            },
            "mounted_effector": {
                "profile_revision": "test-effector-v1",
                "controlled_frame": {"frame_id": "rebot_arm_tool"},
                "collision_primitives": [
                    {
                        "primitive_id": "test-effector-rear",
                        "frame_id": "rebot_arm_tool",
                        "transform": {"translation_m": [0.0, 0.0, -0.1]},
                        "shape": {"type": "SPHERE", "radius_m": 0.035},
                    }
                ],
            },
        },
    }


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
        self.observations = {
            "robot_arm.assembly_state": _assembly_observation(),
            **observations,
        }
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
        "assembly_state_stream": "robot_arm.assembly_state",
        "assembly_state_max_age_ms": 5000,
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
    assert result["data"]["robot_collision_geometry"]["effector_spheres"] == [
        {
            "primitive_id": "test-effector-rear",
            "center_m": [0.0, 0.0, 0.6],
            "radius_m": 0.035,
            "profile_translation_m": [0.0, 0.0, -0.1],
        }
    ]
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
            *(started_us * 1000 for _ in range(6)),
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
                    "roi_scope": "HAND_ANGULAR_4PI",
                    "angular_bin_index": 1,
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
                    "roi_scope": "HAND_ANGULAR_4PI",
                    "angular_bin_index": 2,
                },
            ],
            "angular_projection": {
                "profile_id": "SPHERICAL_FIBONACCI_NEAR_UNIFORM_V1",
                "roi_scope": "HAND_ANGULAR_4PI",
                "origin_frame_id": "rebot_arm_base",
                "origin_m": [0.0, 0.0, 0.0],
                "observed_at_us": now_us,
                "direction_count": 4096,
                "occupied_direction_count": 2,
                "nominal_half_angle_rad": 0.031,
                "covering_half_angle_rad": 0.047,
                "angular_radius_scale": 1.5,
                "minimum_radius_m": 0.005,
                "radial_padding_m": 0.003,
                "maximum_range_m": 1.2,
                "hit_selection": (
                    "NEAREST_SURFACE_HIT_PER_OCCUPIED_DIRECTION"
                ),
                "keep_out_boundary_mode": "HAND_RAY_TANGENT",
            },
            "visible_surface_aabbs": [
                {
                    "extent_kind": "VISIBLE_SURFACE_AABB",
                    "object_id": "table",
                    "description": "the table is the only obstacle",
                    "type": "KEEP_OUT",
                    "frame_id": "rebot_arm_base",
                    "observed_at_us": now_us,
                    "freshness_ms": 5000,
                    "expires_at_us": now_us + 5_000_000,
                    "minimum_m": [0.3, -0.2, 0.0],
                    "maximum_m": [0.8, 0.2, 0.1],
                }
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
    assert result["data"]["visible_surface_aabbs"] == []
    angular_layer = next(
        value
        for value in result["data"]["roi_layers"]
        if value["scope"] == "HAND_ANGULAR_4PI"
    )
    assert angular_layer["projection"]["direction_count"] == 4096
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
