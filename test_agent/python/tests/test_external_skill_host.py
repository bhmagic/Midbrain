from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from physical_agent_test.external_skill_host import (
    ExternalSkillHostServices,
    load_external_skill_host_adapters,
)
from physical_agent_test.skill_catalog import discover_agent_skills


def test_manifest_declared_host_adapter_loads_from_skill_directory(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "example-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "host_adapter.py").write_text(
        """
from dataclasses import dataclass

@dataclass
class Adapter:
    marker: str

    async def invoke(self, arguments):
        return arguments

def build_adapter(*, skill_root, manifest, services):
    assert skill_root.name == "example-skill"
    assert manifest["skill_type"] == "example"
    assert services.manager == "manager"
    return Adapter(marker="loaded")
""".lstrip(),
        encoding="utf-8",
    )
    manifest = {
        "skill_type": "example",
        "version": "1.0.0",
        "display_name": "Example Skill",
        "agent_discovery": {
            "schema_version": 1,
            "discoverable": True,
            "tool_name": "example_tool",
            "description": "Exercise a generic manifest-owned host adapter.",
            "when_to_use": ["A loader contract needs verification."],
            "when_not_to_use": [],
            "side_effects": [],
            "safety_class": "READ_ONLY",
            "expected_latency": "LOW",
            "required_permissions": [],
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "execution_adapter": {
                "adapter_id": "skill.example.v1",
                "kind": "EXTERNAL_SKILL_ENTRYPOINT",
                "entrypoint": "runtime.py",
                "host_adapter": {
                    "entrypoint": "host_adapter.py",
                    "factory": "build_adapter",
                },
                "invocation_requires_approval": False,
            },
        },
        "required_capabilities": [],
        "optional_capabilities": [],
    }
    (skill_root / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    descriptors = discover_agent_skills(tmp_path)
    services = ExternalSkillHostServices(
        manager="manager",
        fabric=SimpleNamespace(),
        spatial=SimpleNamespace(),
        vlm_router=SimpleNamespace(),
        visual_evidence_store=SimpleNamespace(),
    )

    adapters = load_external_skill_host_adapters(
        descriptors,
        eligible_tool_names={"example_tool"},
        services=services,
    )

    assert set(adapters) == {"skill.example.v1"}


def test_ineligible_external_skill_host_adapter_is_not_loaded(
    tmp_path: Path,
) -> None:
    descriptors = []
    adapters = load_external_skill_host_adapters(
        descriptors,
        eligible_tool_names=set(),
        services=ExternalSkillHostServices(
            manager=None,
            fabric=None,
            spatial=None,
            vlm_router=None,
            visual_evidence_store=None,
        ),
    )

    assert adapters == {}


def test_repository_slicing_host_adapter_loads_when_eligible() -> None:
    workspace = Path(__file__).resolve().parents[3]
    descriptors = discover_agent_skills(workspace)
    manager = SimpleNamespace(base_url="http://127.0.0.1:7001")
    integrated_motion = SimpleNamespace()

    adapters = load_external_skill_host_adapters(
        descriptors,
        eligible_tool_names={"slice_with_blade"},
        services=ExternalSkillHostServices(
            manager=manager,
            fabric=SimpleNamespace(),
            spatial=SimpleNamespace(),
            vlm_router=SimpleNamespace(),
            visual_evidence_store=SimpleNamespace(),
            integrated_motion=integrated_motion,
            contact_provider_url="http://127.0.0.1:8794",
        ),
    )

    adapter = adapters["skill.slicing.host.v1"]
    assert adapter.manager is manager
    assert adapter.integrated_motion is integrated_motion
    assert adapter.contact_provider_url == "http://127.0.0.1:8794"
