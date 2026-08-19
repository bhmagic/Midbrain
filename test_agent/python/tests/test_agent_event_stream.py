from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import Agent, RunConfig
from jsonschema import validate

from physical_agent_test.agent_driver import (
    PrototypeAgentDriver,
    _deduplicating_agent_event_sink,
    consume_openai_agent_stream,
)
from physical_agent_test.agent_event_stream import (
    AgentRunChannel,
    AgentRunStreamRegistry,
    parse_event_sequence,
    stream_sse,
)
from physical_agent_test.agent_events import (
    translate_openai_sdk_event,
    translate_openai_sdk_events,
)


class AgentEventTranslationTests(unittest.TestCase):
    def test_translates_text_and_reasoning_summary_deltas(self) -> None:
        text_event = SimpleNamespace(
            type="raw_response_event",
            data=SimpleNamespace(
                type="response.output_text.delta",
                delta="hello",
                item_id="message-1",
                output_index=0,
                content_index=0,
            ),
        )
        reasoning_event = SimpleNamespace(
            type="raw_response_event",
            data=SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="checking the camera",
                item_id="reasoning-1",
                output_index=0,
                summary_index=0,
            ),
        )

        self.assertEqual(
            translate_openai_sdk_event(text_event),
            (
                "assistant.message.delta",
                {
                    "text": "hello",
                    "item_id": "message-1",
                    "output_index": 0,
                    "content_index": 0,
                },
            ),
        )
        self.assertEqual(
            translate_openai_sdk_event(reasoning_event),
            (
                "assistant.reasoning_summary.delta",
                {
                    "text": "checking the camera",
                    "item_id": "reasoning-1",
                    "output_index": 0,
                    "summary_index": 0,
                },
            ),
        )

    def test_tool_event_exposes_identity_but_not_arguments(self) -> None:
        event = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_called",
            item=SimpleNamespace(
                type="tool_call_item",
                raw_item={
                    "name": "inspect_midbrain_runtime",
                    "call_id": "call-1",
                    "arguments": '{"secret":"must-not-stream"}',
                },
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=None,
                title=None,
            ),
        )

        translated = translate_openai_sdk_event(event)

        self.assertIsNotNone(translated)
        assert translated is not None
        event_type, payload = translated
        self.assertEqual(event_type, "tool.called")
        self.assertEqual(payload["tool_name"], "inspect_midbrain_runtime")
        self.assertEqual(payload["call_id"], "call-1")
        self.assertNotIn("arguments", payload)
        self.assertNotIn("must-not-stream", str(payload))

    def test_local_tool_search_uses_canonical_search_events(self) -> None:
        called = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_called",
            item=SimpleNamespace(
                type="tool_call_item",
                raw_item={
                    "name": "tool_search",
                    "call_id": "search-1",
                    "arguments": '{"tool_names":["skill_a"]}',
                },
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=None,
                title=None,
            ),
        )
        completed = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "search-1", "output": "redacted"},
                output=json.dumps(
                    {
                        "type": "tool_search_output",
                        "execution": "client",
                        "call_id": "search-1",
                        "status": "completed",
                        "tools": [],
                    }
                ),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=None,
                title=None,
            ),
        )

        self.assertEqual(
            translate_openai_sdk_event(called)[0],
            "tool.search.called",
        )
        self.assertEqual(
            translate_openai_sdk_event(completed)[0],
            "tool.search.completed",
        )
        self.assertEqual(
            translate_openai_sdk_event(completed)[1]["tool_name"],
            "tool_search",
        )

    def test_generic_tool_output_cannot_spoof_search_completion(self) -> None:
        completed = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "other-1"},
                output=json.dumps(
                    {
                        "type": "tool_search_output",
                        "execution": "client",
                        "call_id": "different-call",
                        "status": "completed",
                        "tools": [],
                    }
                ),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=None,
                title=None,
            ),
        )

        translated = translate_openai_sdk_event(completed)

        self.assertIsNotNone(translated)
        assert translated is not None
        self.assertEqual(translated[0], "tool.completed")
        self.assertNotIn("tool_name", translated[1])

    def test_tool_output_emits_only_allowlisted_visual_evidence(self) -> None:
        evidence_id = "evidence-1"
        output = {
            "answer": "The target is visible.",
            "screenshot": "C:/private/camera-frame.jpg",
            "visual_evidence": {
                "schema": "midbrain.visual_evidence",
                "schema_version": 1,
                "evidence_id": evidence_id,
                "title": "Pointing identification",
                "default_channel": "rgb",
                "channels": [
                    {
                        "id": "rgb",
                        "label": "RGB",
                        "url": (
                            f"/api/visual-evidence/{evidence_id}/channels/rgb"
                        ),
                        "media_type": "image/jpeg",
                        "width": 640,
                        "height": 480,
                        "sha256": "a" * 64,
                    }
                ],
                "annotation_space": {
                    "units": "normalized",
                    "origin": "top_left",
                    "x_axis": "right",
                    "y_axis": "down",
                },
                "annotations": [],
                "confidence": "high",
                "model": "vlm-test",
                "source_skill": "test.skill",
            },
        }
        event = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "call-visual", "output": "redacted"},
                output=json.dumps(output),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=SimpleNamespace(tool_name="identify_pointed_object"),
                title=None,
            ),
        )

        translated = translate_openai_sdk_events(event)

        self.assertEqual(
            [event_type for event_type, _payload in translated],
            ["tool.completed", "visual.evidence.created"],
        )
        visual_payload = translated[-1][1]
        self.assertEqual(visual_payload["evidence_id"], evidence_id)
        self.assertNotIn("screenshot", visual_payload)
        self.assertNotIn("private", str(visual_payload))

    def test_graph_tool_output_emits_nested_visuals_from_python_repr(self) -> None:
        def evidence(evidence_id: str) -> dict:
            return {
                "schema": "midbrain.visual_evidence",
                "schema_version": 1,
                "evidence_id": evidence_id,
                "title": "Graph child evidence",
                "default_channel": "rgb",
                "channels": [
                    {
                        "id": "rgb",
                        "label": "RGB",
                        "url": (
                            f"/api/visual-evidence/{evidence_id}/channels/rgb"
                        ),
                        "media_type": "image/jpeg",
                        "width": 640,
                        "height": 480,
                        "sha256": "b" * 64,
                        "private_path": "C:/private/frame.jpg",
                    }
                ],
                "annotations": [],
                "confidence": "high",
                "model": "graph-child-test",
                "source_skill": "test.graph.child",
            }

        output = {
            "schema": "midbrain.limited_graph.result",
            "node_results": {
                "inspect": {"visual_evidence": evidence("sam2-1")},
                "refine": {
                    "visual_evidence": [
                        evidence("vlm-1"),
                        {"schema": "private.invalid", "secret": "hidden"},
                    ]
                },
            },
            "private_graph_state": "must-not-stream",
        }
        event = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "call-graph", "output": "redacted"},
                output=str(output),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=SimpleNamespace(tool_name="run_limited_graph"),
                title=None,
            ),
        )

        translated = translate_openai_sdk_events(event)

        self.assertEqual(
            [event_type for event_type, _payload in translated],
            [
                "tool.completed",
                "visual.evidence.created",
                "visual.evidence.created",
            ],
        )
        self.assertEqual(
            [payload["evidence_id"] for _, payload in translated[1:]],
            ["sam2-1", "vlm-1"],
        )
        self.assertNotIn("private", str(translated))
        self.assertNotIn("secret", str(translated))

    def test_tool_output_projects_safe_capture_retry_recovery(self) -> None:
        output = {
            "answer": "Recovered target",
            "retry_history": {
                "scope": "CAPTURE_RGB_ONLY",
                "attempt_count": 2,
                "maximum_attempts": 2,
                "recovered": True,
                "exhausted": False,
                "requires_fresh_evidence": True,
                "physical_action_submitted": False,
                "attempts": [
                    {"attempt": 1, "error": "private transport detail"},
                    {"attempt": 2, "outcome": "succeeded"},
                ],
            },
        }
        event = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "call-retry", "output": "redacted"},
                output=json.dumps(output),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=SimpleNamespace(
                    tool_name="identify_pointed_object"
                ),
                title=None,
            ),
        )

        translated = translate_openai_sdk_events(event)

        self.assertEqual(
            [event_type for event_type, _payload in translated],
            ["tool.completed", "skill.retry.recovered"],
        )
        retry_payload = translated[-1][1]
        self.assertEqual(retry_payload["attempt_count"], 2)
        self.assertEqual(
            retry_payload["tool_name"], "identify_pointed_object"
        )
        self.assertNotIn("attempts", retry_payload)
        self.assertNotIn("private", str(retry_payload))

    def test_retry_projection_rejects_physical_action_history(self) -> None:
        event = SimpleNamespace(
            type="run_item_stream_event",
            name="tool_output",
            item=SimpleNamespace(
                type="tool_call_output_item",
                raw_item={"call_id": "call-unsafe", "output": "redacted"},
                output=json.dumps(
                    {
                        "retry_history": {
                            "scope": "CAPTURE_RGB_ONLY",
                            "attempt_count": 2,
                            "maximum_attempts": 2,
                            "recovered": False,
                            "exhausted": True,
                            "requires_fresh_evidence": True,
                            "physical_action_submitted": True,
                        }
                    }
                ),
                agent=SimpleNamespace(name="Physical Agent"),
                tool_origin=None,
                title=None,
            ),
        )

        translated = translate_openai_sdk_events(event)

        self.assertEqual(
            [event_type for event_type, _payload in translated],
            ["tool.completed"],
        )


class AgentEventRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_and_final_graph_visuals_are_deduplicated(self) -> None:
        observed: list[tuple[str, dict]] = []

        async def sink(event_type: str, payload: dict) -> None:
            observed.append((event_type, payload))

        relay = _deduplicating_agent_event_sink(sink)
        evidence = {"evidence_id": "graph-visual-1"}

        await relay("visual.evidence.created", evidence)
        await relay("visual.evidence.created", dict(evidence))
        await relay("tool.completed", {"call_id": "graph-call"})

        self.assertEqual(
            observed,
            [
                ("visual.evidence.created", evidence),
                ("tool.completed", {"call_id": "graph-call"}),
            ],
        )


class AgentRunChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_replays_ordered_versioned_events(self) -> None:
        channel = AgentRunChannel("run-1")
        first = await channel.publish("run.started", {})
        second = await channel.publish(
            "assistant.message.delta",
            {"text": "hello"},
        )

        events, status = await channel.events_after(0, timeout_s=0.01)

        self.assertEqual(status, "STARTING")
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual(first["schema"], "midbrain.agent_event")
        self.assertEqual(second["schema_version"], 1)
        self.assertEqual(second["event_id"], "run-1:2")
        schema_path = (
            Path(__file__).resolve().parents[3]
            / "contracts"
            / "schemas"
            / "agent_event.v1.schema.json"
        )
        validate(
            instance=second,
            schema=json.loads(schema_path.read_text(encoding="utf-8")),
        )

    async def test_sse_replay_finishes_after_terminal_event(self) -> None:
        channel = AgentRunChannel("run-2")
        await channel.publish("run.completed", {"answer": "done"})
        await channel.set_status("COMPLETED")

        chunks = [chunk async for chunk in stream_sse(channel)]

        self.assertEqual(len(chunks), 1)
        self.assertIn("id: 1", chunks[0])
        data_line = next(
            line for line in chunks[0].splitlines() if line.startswith("data: ")
        )
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(payload["type"], "run.completed")

    async def test_background_run_does_not_require_an_sse_subscriber(self) -> None:
        registry = AgentRunStreamRegistry()
        channel = await registry.create("run-3")
        gate = asyncio.Event()

        async def background() -> None:
            await channel.set_status("RUNNING")
            await gate.wait()
            await channel.publish("run.completed", {"answer": "done"})
            await channel.set_status("COMPLETED")

        task = await registry.launch("run-3", background())
        gate.set()
        await task

        snapshot = await channel.snapshot()
        self.assertEqual(snapshot["status"], "COMPLETED")
        self.assertEqual(snapshot["last_sequence"], 1)

    async def test_registry_cancels_every_task_owned_by_one_run(self) -> None:
        registry = AgentRunStreamRegistry()
        channel = await registry.create("run-cancel")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def background() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = await registry.launch("run-cancel", background())
        await started.wait()

        cancelled_count = await registry.cancel("run-cancel")
        await channel.publish("run.cancelled", {"status": "cancelled"})
        await channel.set_status("CANCELLED")
        chunks = [chunk async for chunk in stream_sse(channel)]

        self.assertEqual(cancelled_count, 1)
        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(chunks), 1)
        self.assertIn('"type":"run.cancelled"', chunks[0])

    def test_event_sequence_parser_accepts_sse_and_event_ids(self) -> None:
        self.assertEqual(parse_event_sequence("17"), 17)
        self.assertEqual(parse_event_sequence("run-1:18"), 18)
        self.assertEqual(parse_event_sequence("invalid"), 0)


class StreamConsumptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_is_fully_consumed_and_returns_original_result(self) -> None:
        sdk_events = [
            SimpleNamespace(
                type="raw_response_event",
                data=SimpleNamespace(
                    type="response.output_text.delta",
                    delta="a",
                    item_id="message-1",
                    output_index=0,
                    content_index=0,
                ),
            ),
            SimpleNamespace(
                type="raw_response_event",
                data=SimpleNamespace(
                    type="response.output_text.delta",
                    delta="b",
                    item_id="message-1",
                    output_index=0,
                    content_index=0,
                ),
            ),
        ]

        class Result:
            consumed = False

            async def stream_events(self):
                for event in sdk_events:
                    yield event
                self.consumed = True

        result = Result()
        received: list[tuple[str, dict[str, object]]] = []

        async def sink(
            event_type: str,
            payload: dict[str, object],
        ) -> None:
            received.append((event_type, payload))

        returned = await consume_openai_agent_stream(result, sink)

        self.assertIs(returned, result)
        self.assertTrue(result.consumed)
        self.assertEqual(
            [payload["text"] for _, payload in received],
            ["a", "b"],
        )

    async def test_driver_uses_streamed_runner_only_when_sink_is_present(
        self,
    ) -> None:
        class Result:
            interruptions: list[object] = []
            final_output = "streamed answer"

            async def stream_events(self):
                yield SimpleNamespace(
                    type="raw_response_event",
                    data=SimpleNamespace(
                        type="response.output_text.delta",
                        delta="streamed answer",
                        item_id="message-1",
                        output_index=0,
                        content_index=0,
                    ),
                )

        driver = object.__new__(PrototypeAgentDriver)
        driver.agent = Agent(name="stream-test", instructions="test")
        driver.max_turns = 3
        driver.run_config = RunConfig(workflow_name="stream-test")
        driver.session = None
        received: list[str] = []

        async def sink(
            event_type: str,
            _payload: dict[str, object],
        ) -> None:
            received.append(event_type)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test"}), patch(
            "physical_agent_test.agent_driver.Runner.run_streamed",
            return_value=Result(),
        ) as run_streamed, patch(
            "physical_agent_test.agent_driver.Runner.run",
        ) as run_legacy:
            result = await driver.run_interactive(
                "hello",
                reasoning_effort="medium",
                event_sink=sink,
            )

        self.assertEqual(result.answer, "streamed answer")
        self.assertEqual(received, ["assistant.message.delta"])
        run_streamed.assert_called_once()
        model_settings = run_streamed.call_args.kwargs[
            "run_config"
        ].model_settings
        self.assertEqual(model_settings.reasoning.effort, "medium")
        self.assertEqual(model_settings.reasoning.summary, "auto")
        run_legacy.assert_not_called()

    async def test_driver_preserves_legacy_runner_without_sink(self) -> None:
        result_value = SimpleNamespace(
            interruptions=[],
            final_output="legacy answer",
        )
        driver = object.__new__(PrototypeAgentDriver)
        driver.agent = Agent(name="legacy-test", instructions="test")
        driver.max_turns = 3
        driver.run_config = RunConfig(workflow_name="legacy-test")
        driver.session = None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test"}), patch(
            "physical_agent_test.agent_driver.Runner.run",
            new=AsyncMock(return_value=result_value),
        ) as run_legacy, patch(
            "physical_agent_test.agent_driver.Runner.run_streamed",
        ) as run_streamed:
            result = await driver.run_interactive("hello")

        self.assertEqual(result.answer, "legacy answer")
        run_legacy.assert_awaited_once()
        run_streamed.assert_not_called()

    async def test_driver_passes_multimodal_input_items_unchanged(self) -> None:
        result_value = SimpleNamespace(
            interruptions=[],
            final_output="image answer",
        )
        input_value = [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AA==",
                        "detail": "auto",
                    },
                ],
            }
        ]
        driver = object.__new__(PrototypeAgentDriver)
        driver.agent = Agent(name="multimodal-test", instructions="test")
        driver.max_turns = 3
        driver.run_config = RunConfig(workflow_name="multimodal-test")
        driver.session = None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test"}), patch(
            "physical_agent_test.agent_driver.Runner.run",
            new=AsyncMock(return_value=result_value),
        ) as run_legacy:
            result = await driver.run_interactive(input_value)

        self.assertEqual(result.answer, "image answer")
        self.assertEqual(run_legacy.await_args.args[1], input_value)


if __name__ == "__main__":
    unittest.main()
