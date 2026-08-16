from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from physical_agent_test.limited_graph_routing import (
    FastTextGraphRouter,
    VisualEvidenceGraphRouter,
    build_limited_graph_model_route_profiles,
)


class FakeAgentResult:
    def __init__(self, final_output: Any) -> None:
        self.final_output = final_output
        self.last_response_id = "response-1"


def test_fast_text_router_uses_no_tools_structured_agent() -> None:
    observed: dict[str, Any] = {}

    async def run_agent(agent: Any, payload: str, **arguments: Any) -> Any:
        observed["agent"] = agent
        observed["payload"] = json.loads(payload)
        observed["arguments"] = arguments
        return FakeAgentResult({"edge_id": "accept", "confidence": 0.93})

    router = FastTextGraphRouter("fast-test-model", run_agent=run_agent)
    result = asyncio.run(
        router.invoke(
            instruction="Choose the semantically matching edge.",
            inputs={"answer": "yes"},
            routes=[
                {"edge_id": "accept", "description": "Accept"},
                {"edge_id": "reject", "description": "Reject"},
            ],
            context=SimpleNamespace(child_call_id="graph:model:1"),
        )
    )

    assert observed["agent"].tools == []
    assert observed["arguments"]["max_turns"] == 1
    assert observed["payload"]["inputs"] == {"answer": "yes"}
    assert result["edge_id"] == "accept"
    assert result["provenance"]["model"] == "fast-test-model"


class FakeEvidenceStore:
    async def read(self, evidence_id: str, channel_id: str) -> Any:
        assert evidence_id == "evidence-1"
        assert channel_id == "rgb"
        return SimpleNamespace(data=b"image", media_type="image/jpeg")


class FakeVlmRouter:
    async def generate_images(self, **arguments: Any) -> Any:
        assert arguments["images"] == [(b"image", "image/jpeg")]
        assert arguments["request_id"] == "graph:vision:1"
        return SimpleNamespace(
            text='{"edge_id":"clear","confidence":0.88}',
            backend_id="fake-vlm",
            model_id="fast-vision",
            attempt_count=1,
            request_id="request-1",
            response_id="response-1",
            input_sha256="a" * 64,
        )


def test_vision_router_resolves_only_host_evidence_reference() -> None:
    router = VisualEvidenceGraphRouter(FakeVlmRouter(), FakeEvidenceStore())
    evidence = {
        "schema": "midbrain.visual_evidence",
        "schema_version": 1,
        "evidence_id": "evidence-1",
        "default_channel": "rgb",
    }

    result = asyncio.run(
        router.invoke(
            instruction="Determine whether the path is clear.",
            inputs={"observation": evidence},
            routes=[
                {"edge_id": "clear", "description": "Path clear"},
                {"edge_id": "blocked", "description": "Path blocked"},
            ],
            context=SimpleNamespace(child_call_id="graph:vision:1"),
        )
    )

    assert result["edge_id"] == "clear"
    assert result["provenance"]["evidence_channels"] == [
        {"evidence_id": "evidence-1", "channel_id": "rgb"}
    ]


def test_profile_factory_registers_fixed_text_and_vision_names() -> None:
    profiles = build_limited_graph_model_route_profiles(
        fast_text_model="fast-test-model",
        vlm_router=FakeVlmRouter(),
        visual_evidence_store=FakeEvidenceStore(),
    )

    assert set(profiles) == {"FAST_TEXT", "FAST_VISION"}
    assert profiles["FAST_TEXT"].modality == "TEXT"
    assert profiles["FAST_VISION"].modality == "VISION"
