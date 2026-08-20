from __future__ import annotations

import json
from pathlib import Path
import shutil

from locate_arm_base.arm_profile import ArmProfileStore
from locate_arm_base.profile import canonical_sha256


ROOT = Path(__file__).resolve().parents[4]


def test_profile_contains_only_bounded_local_z_candidates() -> None:
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    record = ArmProfileStore(ROOT, config).load()
    profile = record.model_profile
    assert [value.candidate_id for value in profile.candidates] == ["z0", "z90", "z180", "z270"]
    assert all(value.axis == "Z" for value in profile.candidates)
    assert profile.semantic_frame == "rebot_arm_base"
    assert [path.name for path in profile.segmentation_reference_paths] == [
        "Base_reference_atlas.png",
        "Arm_axis_reference_no_effector_4views.png",
    ]
    assert [path.name for path in profile.orientation_reference_paths] == [
        "Base_reference_atlas.png",
        "Arm_axis_reference_no_effector_4views.png",
    ]
    assert profile.mesh_preview_path is not None
    assert profile.mesh_preview_sha256 is not None
    assert profile.vlm_seed_guidance == (
        "We are annotating the base block of a robot arm. It contains the first motor "
        "and is colored black. It may be partially covered by the first joint (silver) "
        "and the connecting cables (black). Red or green indicator lights may be present "
        "and should not affect the selection."
    )
    assert record.model_id == "rebot_arm_b601_dm"
    assert record.arm_provider_id == "robot_arm.rebot_dm"
    assert record.appendix_key == "midbrain.skill.locate_arm_base.v1"


def test_canonical_hash_is_independent_of_object_key_order() -> None:
    left = {"a": 1, "b": [True, 0.25]}
    right = json.loads('{"b":[true,0.25],"a":1}')
    assert canonical_sha256(left) == canonical_sha256(right)


def test_profile_rejects_unbounded_vlm_seed_guidance() -> None:
    config = json.loads(
        (ROOT / "skills/locate_arm_base/config_templates/skill.default.json").read_text(
            encoding="utf-8"
        )
    )
    record = ArmProfileStore(ROOT, config).load()
    payload = json.loads(json.dumps(record.appendix))
    payload["vlm_seed_guidance"] = "x" * 2001
    from locate_arm_base.profile import load_profile_payload

    try:
        load_profile_payload(payload, ROOT)
    except ValueError as error:
        assert "cannot exceed 2000" in str(error)
    else:
        raise AssertionError("oversized VLM guidance was accepted")


def test_arm_profile_appendix_save_preserves_arbitrary_fields(tmp_path: Path) -> None:
    source_model = json.loads(
        (
            ROOT
            / "providers/rebot_arm_dm/config/arm_profiles/rebot_arm_b601_dm.v1.json"
        ).read_text(encoding="utf-8")
    )
    source_appendix = source_model["appendix"][
        "midbrain.skill.locate_arm_base.v1"
    ]
    mesh_source = ROOT / source_appendix["mesh"]["path"]
    mesh_target = tmp_path / source_appendix["mesh"]["path"]
    mesh_target.parent.mkdir(parents=True)
    shutil.copy2(mesh_source, mesh_target)
    preview = source_appendix["mesh"].get("preview")
    if isinstance(preview, dict):
        preview_source = ROOT / preview["path"]
        preview_target = tmp_path / preview["path"]
        preview_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview_source, preview_target)
    for reference in source_appendix["reference_images"]:
        reference_source = ROOT / reference["path"]
        reference_target = tmp_path / reference["path"]
        reference_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reference_source, reference_target)

    provider_root = tmp_path / "providers/test_arm"
    model_path = provider_root / "profiles/arm.json"
    model_path.parent.mkdir(parents=True)
    model = {
        "schema": "physical_agent.robot_arm_model",
        "schema_version": 1,
        "model_id": "test_arm",
        "model_revision": "test-arm-v1",
        "appendix": {
            "unrelated.consumer": {"free-form value": [1, "two", None]},
            "midbrain.skill.locate_arm_base.v1": source_appendix,
        },
    }
    model_path.write_text(json.dumps(model), encoding="utf-8")
    selection_path = tmp_path / "config/robot_assemblies/primary_manipulator.json"
    selection_path.parent.mkdir(parents=True)
    selection_path.write_text(
        json.dumps(
            {
                "schema": "midbrain.robot_assembly_selection",
                "arm_provider": {
                    "provider_id": "test.arm",
                    "provider_root": "providers/test_arm",
                },
                "profiles": {
                    "arm_model": {
                        "relative_path": "profiles/arm.json",
                        "expected_schema": "physical_agent.robot_arm_model",
                        "expected_id": "test_arm",
                        "expected_revision": "test-arm-v1",
                        "sha256": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = ArmProfileStore(
        tmp_path,
        {
            "arm_profile_selection": {
                "selection_path": "config/robot_assemblies/primary_manipulator.json",
                "appendix_key": "midbrain.skill.locate_arm_base.v1",
            }
        },
    )
    updated = json.loads(json.dumps(store.load().appendix))
    updated["arbitrary field name !"] = {
        "nested unknown": [True, 4.5, {"also unknown": "preserved"}]
    }
    saved = store.save_appendix(updated)
    assert saved.appendix["arbitrary field name !"]["nested unknown"][2] == {
        "also unknown": "preserved"
    }
    document = json.loads(model_path.read_text(encoding="utf-8"))
    assert document["appendix"]["unrelated.consumer"] == {
        "free-form value": [1, "two", None]
    }
