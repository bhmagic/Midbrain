from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from physical_agent_test.agent_skill_installation import (
    AgentSkillInstallationRegistry,
)


def _descriptor(tool_name: str, manifest_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name=tool_name,
        skill_type=f"skill.{tool_name}",
        display_name=tool_name.replace("_", " ").title(),
        skill_version="1.0.0",
        manifest_path=str(manifest_path),
        description=f"Use {tool_name} for its bounded test operation.",
        safety_class="READ_ONLY",
        expected_latency="LOW",
        side_effects=(),
        execution_adapter_id=f"adapter.{tool_name}",
        execution_adapter_kind="IN_PROCESS_BOUND_INSTANCE",
        discoverable=True,
    )


def test_prompts_only_for_discoverable_skills_outside_agent_lists(
    tmp_path: Path,
) -> None:
    configured = _descriptor("configured_skill", tmp_path / "configured.json")
    runtime = _descriptor("runtime_skill", tmp_path / "runtime.json")
    new = _descriptor("new_skill", tmp_path / "new.json")
    registry = AgentSkillInstallationRegistry(
        tmp_path / "decisions.json",
        configured_enabled_tool_names={"configured_skill"},
    )

    snapshot = registry.snapshot(
        [configured, runtime, new],
        runtime_tool_names={"runtime_skill"},
    )

    assert [item["tool_name"] for item in snapshot["unresolved"]] == [
        "new_skill"
    ]
    assert snapshot["prompt_required"] is True
    assert snapshot["manifest_mutation_allowed"] is False


def test_disable_is_immediate_and_does_not_change_skill_manifest(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"agent_discovery": {}}\n', encoding="utf-8")
    before = manifest.read_bytes()
    descriptor = _descriptor("new_skill", manifest)
    registry = AgentSkillInstallationRegistry(
        tmp_path / "decisions.json",
        configured_enabled_tool_names=set(),
    )

    snapshot = registry.decide(
        "new_skill",
        action="DISABLE",
        descriptors=[descriptor],
        runtime_tool_names=set(),
    )

    assert snapshot["prompt_required"] is False
    assert snapshot["restart_required"] is False
    assert snapshot["disabled_tool_names"] == ["new_skill"]
    assert manifest.read_bytes() == before


def test_add_persists_and_requires_restart_until_skill_is_loaded(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("new_skill", tmp_path / "manifest.json")
    decision_path = tmp_path / "decisions.json"
    registry = AgentSkillInstallationRegistry(
        decision_path,
        configured_enabled_tool_names=set(),
    )

    pending = registry.decide(
        "new_skill",
        action="ADD",
        descriptors=[descriptor],
        runtime_tool_names=set(),
    )

    assert pending["prompt_required"] is False
    assert pending["restart_required"] is True
    assert [item["tool_name"] for item in pending["pending_restart"]] == [
        "new_skill"
    ]
    persisted = json.loads(decision_path.read_text(encoding="utf-8"))
    assert persisted["decisions"]["new_skill"]["state"] == "ENABLED"

    restarted = AgentSkillInstallationRegistry(
        decision_path,
        configured_enabled_tool_names=set(),
    )
    assert restarted.effective_enabled_tool_names() == {"new_skill"}
    loaded = restarted.snapshot(
        [descriptor],
        runtime_tool_names={"new_skill"},
    )
    assert loaded["prompt_required"] is False
    assert loaded["restart_required"] is False


def test_decision_rejects_unknown_or_currently_active_skill(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor("known_skill", tmp_path / "manifest.json")
    registry = AgentSkillInstallationRegistry(
        tmp_path / "decisions.json",
        configured_enabled_tool_names=set(),
    )

    with pytest.raises(KeyError):
        registry.decide(
            "unknown_skill",
            action="ADD",
            descriptors=[descriptor],
            runtime_tool_names=set(),
        )
    with pytest.raises(RuntimeError, match="already active"):
        registry.decide(
            "known_skill",
            action="DISABLE",
            descriptors=[descriptor],
            runtime_tool_names={"known_skill"},
        )
