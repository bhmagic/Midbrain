from __future__ import annotations

import asyncio
import json
from pathlib import Path

from physical_agent_test.agent_driver import PrototypeAgentDriver
from physical_agent_test.result_projection import (
    finalize_skill_result,
    select_result_detail,
)
from physical_agent_test.skill_catalog import discover_agent_skills
from physical_agent_test.skill_result_details import SkillResultDetailStore


WORKSPACE = Path(__file__).resolve().parents[3]


class _VisualSkill:
    async def run(self, question: str) -> str:
        return json.dumps(
            {
                "answer": question,
                "confidence": "high",
                "annotation_processing": {"route": "complete-only"},
            }
        )


def _visual_descriptor():
    return next(
        item
        for item in discover_agent_skills(WORKSPACE, include_disabled=True)
        if item.tool_name == "analyze_visual_scene"
    )


def test_complete_result_is_sanitized_retained_and_compacted(
    tmp_path: Path,
) -> None:
    descriptor = _visual_descriptor()
    store = SkillResultDetailStore(
        tmp_path / "details.sqlite3",
        session_id="session-one",
        maximum_results=10,
        maximum_result_bytes=64 * 1024,
        maximum_total_bytes=256 * 1024,
        retention_days=1,
    )
    complete = {
        "status": "COMPLETED",
        "answer": "the requested object",
        "confidence": "high",
        "annotations": [],
        "annotation_processing": {"large_diagnostic": "x" * 4096},
        "capability_binding": {
            "provider_id": "vision.primary",
            "api_key": "must-not-be-retained",
        },
        "physical_action_submitted": False,
        "message": "done",
    }

    compact = asyncio.run(
        finalize_skill_result(complete, descriptor, store)
    )

    assert compact["answer"] == "the requested object"
    assert "annotation_processing" not in compact
    assert "capability_binding" not in compact
    assert compact["detail_ref"]["available"] is True
    record = asyncio.run(store.retrieve(compact["detail_ref"]["result_id"]))
    assert record is not None
    assert record["payload"]["annotation_processing"] == {
        "large_diagnostic": "x" * 4096
    }
    assert record["payload"]["capability_binding"]["api_key"] == "[REDACTED]"
    selected = select_result_detail(record, "/annotation_processing")
    assert selected["selected_pointer"] == "/annotation_processing"
    assert selected["detail"] == {"large_diagnostic": "x" * 4096}


def test_detail_store_is_session_scoped_and_prunes_oldest_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "details.sqlite3"
    first_session = SkillResultDetailStore(
        path,
        session_id="session-one",
        maximum_results=1,
        maximum_result_bytes=4096,
        maximum_total_bytes=8192,
        retention_days=1,
    )
    schema = {"type": "object", "additionalProperties": True}
    first = asyncio.run(
        first_session.store(
            {"value": 1},
            tool_name="example_tool",
            skill_type="example",
            skill_version="1.0.0",
            output_schema=schema,
        )
    )
    second = asyncio.run(
        first_session.store(
            {"value": 2},
            tool_name="example_tool",
            skill_type="example",
            skill_version="1.0.0",
            output_schema=schema,
        )
    )

    assert asyncio.run(first_session.retrieve(first["result_id"])) is None
    assert asyncio.run(first_session.retrieve(second["result_id"])) is not None
    other_session = SkillResultDetailStore(
        path,
        session_id="session-two",
        maximum_results=1,
        maximum_result_bytes=4096,
        maximum_total_bytes=8192,
        retention_days=1,
    )
    assert asyncio.run(other_session.retrieve(second["result_id"])) is None


def test_unconfigured_detail_store_still_returns_bounded_compact_result() -> None:
    descriptor = _visual_descriptor()
    compact = asyncio.run(
        finalize_skill_result(
            {
                "answer": "available",
                "annotation_processing": {"diagnostic": "not compact"},
            },
            descriptor,
            None,
        )
    )

    assert compact["answer"] == "available"
    assert "annotation_processing" not in compact
    assert compact["detail_ref"] == {
        "schema": "midbrain.skill_result_detail_ref",
        "schema_version": 1,
        "available": False,
        "reason": "DETAIL_STORE_NOT_CONFIGURED",
    }


def test_oversized_selected_diagnostic_preserves_outcome_and_detail_ref(
    tmp_path: Path,
) -> None:
    descriptor = _visual_descriptor()
    store = SkillResultDetailStore(
        tmp_path / "details.sqlite3",
        session_id="session-one",
        maximum_results=10,
        maximum_result_bytes=256 * 1024,
        maximum_total_bytes=512 * 1024,
        retention_days=1,
    )
    compact = asyncio.run(
        finalize_skill_result(
            {
                "status": "COMPLETED",
                "answer": "available",
                "annotations": [{"label": "x" * 100_000}],
                "physical_action_submitted": False,
                "message": "done",
            },
            descriptor,
            store,
        )
    )

    assert compact["status"] == "COMPLETED"
    assert compact["physical_action_submitted"] is False
    assert compact["detail_ref"]["available"] is True
    assert "annotations" not in compact
    assert compact["compact_projection"]["reason"] == "MAX_COMPACT_BYTES"
    assert "/annotations" in compact["compact_projection"]["omitted_pointers"]


def test_top_level_agent_can_explicitly_inspect_full_skill_detail(
    tmp_path: Path,
) -> None:
    store = SkillResultDetailStore(
        tmp_path / "details.sqlite3",
        session_id="session-one",
        maximum_results=10,
        maximum_result_bytes=64 * 1024,
        maximum_total_bytes=256 * 1024,
        retention_days=1,
    )
    driver = PrototypeAgentDriver(
        _VisualSkill(),
        "gpt-test",
        workspace_root=WORKSPACE,
        eligible_tool_names={"identify_pointed_object"},
        skill_result_detail_store=store,
        defer_loading=False,
    )
    tools = {tool.name: tool for tool in driver.agent.tools}

    compact = json.loads(
        asyncio.run(
            tools["identify_pointed_object"].on_invoke_tool(
                None,
                '{"question":"which object"}',
            )
        )
    )
    detail = json.loads(
        asyncio.run(
            tools["inspect_skill_result_detail"].on_invoke_tool(
                None,
                json.dumps(
                    {
                        "result_id": compact["detail_ref"]["result_id"],
                        "json_pointer": "/annotation_processing",
                    }
                ),
            )
        )
    )

    assert "annotation_processing" not in compact
    assert detail["selected_pointer"] == "/annotation_processing"
    assert detail["detail"] == {"route": "complete-only"}
    assert "inspect_skill_result_detail" not in {
        item.tool_name for item in driver.offered_skill_descriptors
    }
