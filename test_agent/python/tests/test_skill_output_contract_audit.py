from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from physical_agent_test.skill_catalog import (
    declared_schema_pointers,
    discover_agent_skills,
)


WORKSPACE = Path(__file__).resolve().parents[3]


OUTPUT_AUDIT = {
    "grip": {
        "sources": ["skills/grip/python/grip_skill/host_adapter.py"],
        "tokens": ["WAIT_FOR_GRIP_TEMPERATURE", "CARRYING_CURRENT_POSE_LOCKED"],
        "declares": [
            "status",
            "workflow_complete",
            "carry_id",
            "attachment_revision",
            "all_joints_position_effort_limited",
            "retry_after_s",
            "thermal",
        ],
    },
    "plan_no_contact_item_approach": {
        "sources": ["test_agent/python/physical_agent_test/no_contact_approach.py"],
        "tokens": ["build_no_contact_correction_plan", "controller_plan_request"],
        "declares": ["next_target_arm_base_m", "source_evidence", "controller_preview"],
    },
    "derive_fabric_world_point": {
        "sources": ["test_agent/python/physical_agent_test/fabric_world_point.py"],
        "tokens": ["WORLD_POINT_READY", "source_age_ms_at_completion"],
        "declares": ["target_position_world_m", "scene_revision_disposition", "skill_completed_at_us"],
    },
    "establish_world_axis": {
        "sources": ["test_agent/python/physical_agent_test/initialize_space_cognition_skill.py"],
        "tokens": [
            "ensure_tracking",
            "tracking_ready",
            "EXISTING_TRACKING_EPOCH",
            "GLOBAL_MOTION_INHIBIT",
        ],
        "declares": ["status", "result"],
        "excludes": ["workflow_complete", "required_next_tool"],
    },
    "execute_reviewed_observation_motion": {
        "sources": ["test_agent/python/physical_agent_test/reviewed_observation_execution.py"],
        "tokens": ["reviewed_observation_motion_execution", "integrated_controller"],
        "declares": ["decision_id", "reviewed_scene_refreshed", "integrated_controller"],
        "excludes": ["controller_result", "physical_motion_submitted"],
    },
    "grip_object": {
        "sources": [
            "skills/grip-object/python/grip_object_skill/host_adapter.py",
            "skills/grip-object/python/grip_object_skill/skill.py",
        ],
        "tokens": ["WAIT_FOR_GRIP_TEMPERATURE", "CARRYING_POSITION_EFFORT_LIMITED"],
        "declares": [
            "status",
            "workflow_complete",
            "carry_id",
            "attachment_revision",
            "all_joints_position_effort_limited",
            "retry_after_s",
            "thermal",
        ],
    },
    "identify_pointed_object": {
        "sources": ["test_agent/python/physical_agent_test/gemini_pointing_skill.py"],
        "tokens": ["PROVIDER_ACTIVATION_REQUIRED", "CAMERA_FRAME_UNAVAILABLE"],
        "declares": ["answer", "annotation_processing", "retry_history", "capability_binding"],
    },
    "reinitialize_space_cognition": {
        "sources": [
            "test_agent/python/physical_agent_test/initialize_space_cognition_skill.py",
            "test_agent/python/physical_agent_test/app.py",
        ],
        "tokens": ["invalidated_workcell_activation_ids", "point_cloud_resumed"],
        "declares": ["requested_reason", "point_cloud_resumed", "result"],
        "excludes": ["workflow_complete", "required_next_tool"],
    },
    "inspect_arm_semantic_scene": {
        "sources": ["test_agent/python/physical_agent_test/semantic_scene_inspector.py"],
        "tokens": ["SCENE_MAPPING_PENDING", "visible_surface_aabbs"],
        "declares": ["mapping_failure", "sphere_type_counts", "expired_visible_surface_aabb_count"],
        "excludes": ["session_epoch"],
    },
    "lay_flat": {
        "sources": ["skills/lay-flat/python/lay_flat_skill/host_adapter.py"],
        "tokens": ["OBJECT_LAID_FLAT_RELEASED_AND_RETRACTED", "ENTER_MIT_FLOAT"],
        "declares": ["status", "workflow_complete", "released_carry_id"],
    },
    "let_go": {
        "sources": ["skills/let-go/python/let_go_skill/host_adapter.py"],
        "tokens": ["OBJECT_RELEASED_AND_ARM_RELAXED", "ENTER_MIT_FLOAT"],
        "declares": ["status", "workflow_complete", "released_carry_id"],
    },
    "perform_relative_effector_motion": {
        "sources": [
            "test_agent/python/physical_agent_test/integrated_motion_adapter.py",
            "test_agent/python/physical_agent_test/prepared_action.py",
        ],
        "tokens": ["MOTION_COMPLETED", "PREPARED_ACTION_STATE_UNAVAILABLE"],
        "declares": ["start_position_m", "target_position_m", "commit", "new_continuation_submitted"],
        "excludes": ["start_position_arm_base_m", "final_position_arm_base_m", "controller_result"],
    },
    "run_limited_graph": {
        "sources": [
            "skills/limited-graph/python/limited_graph/runner.py",
            "skills/limited-graph/schemas/limited_graph_result.v1.schema.json",
        ],
        "tokens": ["graph_run_id", "retained_result_bytes"],
        "declares": ["graph_sha256", "terminal_node", "trace", "node_results"],
    },
    "locate_effector_front": {
        "sources": [
            "test_agent/python/physical_agent_test/effector_front_adapter.py",
            "skills/locate-effector-front/python/locate_effector_front/landmark.py",
        ],
        "tokens": ["control_reference", "CONTROLLER_FK_REFERENCE_READY"],
        "declares": ["front_points", "control_reference", "controller_consistency", "input_temporal_evidence"],
        "excludes": ["target_point", "point_m", "pixel_yx", "uncertainty_m"],
    },
    "offset_world_point": {
        "sources": [
            "test_agent/python/physical_agent_test/fabric_spatial_translation.py"
        ],
        "tokens": ["WORLD_POINT_OFFSET_READY", "offset_vector_world_m"],
        "declares": [
            "source_position_world_m",
            "offset_vector_world_m",
            "target_position_world_m",
        ],
    },
    "move_effector_to_world_point": {
        "sources": [
            "test_agent/python/physical_agent_test/integrated_motion_adapter.py",
            "test_agent/python/physical_agent_test/prepared_action.py",
        ],
        "tokens": ["ALREADY_AT_WORLD_POINT", "absolute_world_point"],
        "declares": ["target_position_world_m", "world_point_resolution", "target_position_m", "commit"],
        "excludes": ["final_position_arm_base_m"],
    },
    "move_carried_object": {
        "sources": [
            "skills/move-carried-object/python/move_carried_object_skill/host_adapter.py"
        ],
        "tokens": ["CARRIED_OBJECT_MOVED_AND_HELD", "behavior=\"CONTINUE\""],
        "declares": [
            "status",
            "workflow_complete",
            "carry_id",
            "attachment_revision",
            "all_joints_position_effort_limited",
        ],
    },
    "locate_item": {
        "sources": [
            "test_agent/python/physical_agent_test/item_locator_adapter.py",
            "skills/observe_pointed_object/python/observe_pointed_object/locator.py",
        ],
        "tokens": ["resolve_item_location", "semantic_scene_assertion"],
        "declares": ["location", "bearing", "visual_box_support", "semantic_scene_assertion"],
        "excludes": ["target_point", "point_m", "bearing_only", "fabric_assertion"],
    },
    "refine_arm_root_translation": {
        "sources": [
            "skills/refine-arm-root-translation/python/refine_arm_root_translation/host_adapter.py",
            "skills/refine-arm-root-translation/python/refine_arm_root_translation/runtime.py",
            "skills/refine-arm-root-translation/python/refine_arm_root_translation/refinement.py",
        ],
        "tokens": ["raw_translation_delta_m", "multi_sample_refinement"],
        "declares": ["raw_translation_delta_m", "adopted_translation_delta_m", "multi_sample_refinement", "dependency"],
        "excludes": ["sample_count_requested", "raw_correction_world_m", "manager_update"],
    },
    "register_tool_to_control_frame": {
        "sources": [
            "test_agent/python/physical_agent_test/tool_registration_adapter.py",
            "skills/register_tool_to_control_frame/python/register_tool_to_control_frame/registration.py",
        ],
        "tokens": ["tool_from_control_frame", "registered_landmarks"],
        "declares": ["control_origin_from_tool_m", "registered_landmarks", "camera_capability_binding"],
        "excludes": ["candidate_id", "control_frame", "landmarks", "confidence"],
    },
    "slice_with_blade": {
        "sources": [
            "skills/slicing/python/slicing_skill/host_adapter.py",
            "skills/slicing/python/slicing_skill/skill.py",
        ],
        "tokens": ["_completed_result", "planned_retract_endpoint_world_m"],
        "declares": ["alignment", "contact", "plan", "result_semantics"],
        "excludes": ["physical_motion_authorized", "physical_motion_submitted"],
    },
    "register_rgbd_pixel_to_world": {
        "sources": [
            "test_agent/python/physical_agent_test/spatial_registration_adapter.py",
            "skills/spatial_registration_rgbd/python/spatial_registration_rgbd/registration.py",
        ],
        "tokens": ["registered_depth_pixel_yx", "target_point_m"],
        "declares": ["rgb_pixel_yx", "camera_system_point_m", "target_point_m", "input_temporal_evidence"],
        "excludes": ["pixel_yx", "target_point", "point_m", "transform_path"],
    },
    "locate_arm_base": {
        "sources": [
            "test_agent/python/physical_agent_test/arm_base_localization_adapter.py",
            "skills/locate_arm_base/python/locate_arm_base/skill.py",
        ],
        "tokens": ["review_and_activate_arm_base", "candidate_sha256"],
        "declares": [
            "world_from_arm_base",
            "candidate_sha256",
            "quality_provenance",
            "timing",
            "failed_stage",
            "error",
            "visual_evidence",
        ],
        "excludes": ["activation_id", "calibration_revision"],
    },
    "translate_fabric_direction_to_world": {
        "sources": ["test_agent/python/physical_agent_test/fabric_spatial_translation.py"],
        "tokens": ["WORLD_DIRECTION_READY", "framed_direction_world"],
        "declares": ["direction_world", "framed_direction_world", "source_world_binding"],
        "excludes": ["transform_path"],
    },
    "translate_fabric_pose_to_world": {
        "sources": ["test_agent/python/physical_agent_test/fabric_spatial_translation.py"],
        "tokens": ["WORLD_POSE_READY", "framed_pose_world"],
        "declares": ["target_position_world_m", "target_orientation_world_xyzw", "framed_pose_world"],
        "excludes": ["transform_path"],
    },
    "verify_rgbd_image_alignment": {
        "sources": ["test_agent/python/physical_agent_test/rgbd_alignment.py"],
        "tokens": ["rgbd_alignment_validation", "numeric_quality"],
        "declares": ["motion_usable", "numeric_quality", "vlm_review", "artifacts"],
        "excludes": ["alignment_valid", "numeric_verdict", "vlm_verdict", "visual_evidence"],
    },
    "analyze_visual_scene": {
        "sources": ["test_agent/python/physical_agent_test/gemini_pointing_skill.py"],
        "tokens": ["annotation_processing", "vlm_route"],
        "declares": ["answer", "annotation_processing", "retry_history", "capability_binding"],
    },
}


def test_all_installed_skill_outputs_have_source_backed_audit_entries() -> None:
    descriptors = {
        item.tool_name: item
        for item in discover_agent_skills(WORKSPACE, include_disabled=True)
    }
    assert set(OUTPUT_AUDIT) == set(descriptors)

    for tool_name, audit in OUTPUT_AUDIT.items():
        descriptor = descriptors[tool_name]
        schema = descriptor.output_schema
        Draft202012Validator.check_schema(schema)
        assert descriptor.schema_version == 3
        assert schema["x-midbrain-result-tiers"]["schema_version"] == 1
        assert set(descriptor.result_tiers.compact_pointers) <= set(
            declared_schema_pointers(schema)
        )
        assert "/authorization" not in descriptor.result_tiers.compact_pointers
        properties = set(schema.get("properties", {}))
        assert set(schema.get("required", [])) <= properties
        assert set(audit.get("declares", [])) <= properties
        assert not (set(audit.get("excludes", [])) & properties)
        if audit.get("empty_direct_contract"):
            assert descriptor.discoverable is False
            assert properties == set()
            assert descriptor.result_tiers.detail_policy == "NONE"
        else:
            assert (
                descriptor.result_tiers.detail_policy
                == "HOST_SANITIZED_REFERENCE"
            )

        source_text = "\n".join(
            (WORKSPACE / relative_path).read_text(encoding="utf-8")
            for relative_path in audit["sources"]
        )
        for token in audit["tokens"]:
            assert token in source_text, f"{tool_name} lost source token {token}"


def test_limited_graph_manifest_tracks_its_canonical_result_schema() -> None:
    descriptor = next(
        item
        for item in discover_agent_skills(WORKSPACE, include_disabled=True)
        if item.tool_name == "run_limited_graph"
    )
    canonical = json.loads(
        (
            WORKSPACE
            / "skills/limited-graph/schemas/limited_graph_result.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert set(descriptor.output_schema["properties"]) == set(
        canonical["properties"]
    )
    assert set(descriptor.output_schema["required"]) == set(
        canonical["required"]
    )
