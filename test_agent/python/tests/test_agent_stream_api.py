from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from physical_agent_test import app as app_module
from physical_agent_test.agent_driver import InteractiveAgentResult


class AgentStreamApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_session_projects_the_shared_public_transcript(self) -> None:
        retained = {
            "session": {
                "session_id": "manager-boot-test",
                "status": "ACTIVE",
            },
            "runs": [
                {
                    "run_id": "run-chat",
                    "session_id": "manager-boot-test",
                    "user_prompt": "inspect the scene",
                    "assistant_answer": "The table is clear.",
                    "agent_model": "test-agent",
                    "reasoning_effort": "medium",
                    "vlm_model": "test-vlm",
                    "attachment_count": 0,
                    "started_at": "2026-08-02T12:00:00Z",
                    "updated_at": "2026-08-02T12:00:01Z",
                    "status": "COMPLETED",
                    "events": [
                        {
                            "type": "tool.called",
                            "occurred_at": "2026-08-02T12:00:00Z",
                            "payload": {"tool_name": "inspect_midbrain_runtime"},
                        },
                        {
                            "type": "assistant.reasoning_summary.delta",
                            "occurred_at": "2026-08-02T12:00:01Z",
                            "payload": {"text": "Checking current state."},
                        },
                    ],
                }
            ],
        }
        transport = httpx.ASGITransport(app=app_module.app)
        with (
            patch.object(
                app_module,
                "_refresh_midbrain_session_identity",
                new=AsyncMock(),
            ),
            patch.object(
                app_module.agent_run_journal,
                "get_session",
                new=AsyncMock(return_value=retained),
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get("/api/chat-session")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session"]["session_id"], "manager-boot-test")
        self.assertEqual(payload["turns"][0]["prompt"], "inspect the scene")
        self.assertEqual(payload["turns"][0]["answer"], "The table is clear.")
        self.assertEqual(payload["turns"][0]["reasoning"], "Checking current state.")
        self.assertEqual(
            payload["turns"][0]["progress"],
            [
                {
                    "label": "Started inspect_midbrain_runtime",
                    "occurred_at": "2026-08-02T12:00:00Z",
                }
            ],
        )

    async def asyncSetUp(self) -> None:
        self._journal_temporary = tempfile.TemporaryDirectory()
        self._original_journal_path = app_module.agent_run_journal.path
        app_module.agent_run_journal.path = (
            Path(self._journal_temporary.name) / "agent_runs.sqlite3"
        )

    async def asyncTearDown(self) -> None:
        await app_module.agent_run_stream_registry.shutdown()
        await app_module.agent_run_journal.close()
        app_module.agent_run_journal.path = self._original_journal_path
        self._journal_temporary.cleanup()

    async def test_uploaded_image_reaches_agent_as_multimodal_input(
        self,
    ) -> None:
        image_buffer = io.BytesIO()
        Image.new("RGB", (16, 10), color=(30, 90, 150)).save(
            image_buffer,
            format="PNG",
        )
        image_bytes = image_buffer.getvalue()
        completed = InteractiveAgentResult(
            answer="image received",
            state=None,
            approvals=[],
        )
        transport = httpx.ASGITransport(app=app_module.app)
        with patch.object(
            app_module.driver,
            "run_interactive",
            new=AsyncMock(return_value=completed),
        ) as run_interactive:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                uploaded = await client.post(
                    "/api/agent-attachments",
                    json={
                        "filename": "operator-scene.png",
                        "media_type": "image/png",
                        "data_base64": base64.b64encode(
                            image_bytes
                        ).decode("ascii"),
                    },
                )
                self.assertEqual(uploaded.status_code, 201)
                attachment = uploaded.json()
                preview = await client.get(attachment["preview_url"])
                self.assertEqual(preview.content, image_bytes)
                self.assertEqual(preview.headers["content-type"], "image/png")

                started = await client.post(
                    "/api/streaming-runs",
                    json={
                        "prompt": "Describe the attached scene",
                        "attachment_ids": [attachment["attachment_id"]],
                        "agent_model": app_module.settings.openai_model,
                        "reasoning_effort": (
                            app_module.settings.openai_agent_reasoning_effort
                        ),
                        "vlm_model": "auto",
                    },
                )
                self.assertEqual(started.status_code, 202)
                run_id = started.json()["run_id"]
                for _ in range(20):
                    status = await client.get(
                        f"/api/streaming-runs/{run_id}"
                    )
                    if status.json()["status"] == "COMPLETED":
                        break
                    await asyncio.sleep(0)
                event_response = await client.get(
                    started.json()["events_url"]
                )

        run_interactive.assert_awaited_once()
        input_value = run_interactive.await_args.args[0]
        self.assertIsInstance(input_value, list)
        content = input_value[0]["content"]
        self.assertEqual(content[0]["text"], "Describe the attached scene")
        self.assertEqual(content[1]["type"], "input_image")
        encoded_url = content[1]["image_url"]
        self.assertEqual(
            base64.b64decode(encoded_url.split(",", 1)[1]),
            image_bytes,
        )
        events = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["payload"]["attachment_count"], 1)
        self.assertNotIn("attachment_id", str(events))

    async def test_expired_attachment_reference_is_rejected(self) -> None:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/streaming-runs",
                json={
                    "prompt": "Describe the missing image",
                    "attachment_ids": ["a" * 32],
                },
            )

        self.assertEqual(response.status_code, 410)
        self.assertIn("upload it again", response.json()["detail"])

    async def test_visual_evidence_endpoint_serves_exact_retained_bytes(
        self,
    ) -> None:
        evidence = await app_module.visual_evidence_store.register_rgb(
            image_bytes=b"retained-camera-frame",
            media_type="image/jpeg",
            width=64,
            height=48,
            title="Visual evidence",
            annotations=[],
            confidence="unknown",
            model="test",
            source_skill="test.skill",
        )
        channel_url = evidence["channels"][0]["url"]
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get(channel_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"retained-camera-frame")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    async def test_streaming_route_completes_without_browser_owned_execution(
        self,
    ) -> None:
        completed = InteractiveAgentResult(
            answer="streamed answer",
            state=None,
            approvals=[],
        )
        transport = httpx.ASGITransport(app=app_module.app)
        with patch.object(
            app_module.driver,
            "run_interactive",
            new=AsyncMock(return_value=completed),
        ) as run_interactive:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                started = await client.post(
                    "/api/streaming-runs",
                    json={
                        "prompt": "inspect the camera",
                        "agent_model": app_module.settings.openai_model,
                        "reasoning_effort": (
                            app_module.settings.openai_agent_reasoning_effort
                        ),
                        "vlm_model": "auto",
                    },
                )
                self.assertEqual(started.status_code, 202)
                start_payload = started.json()
                run_id = start_payload["run_id"]

                for _ in range(20):
                    status = await client.get(
                        f"/api/streaming-runs/{run_id}"
                    )
                    if status.json()["status"] == "COMPLETED":
                        break
                    await asyncio.sleep(0)
                self.assertEqual(status.json()["status"], "COMPLETED")

                event_response = await client.get(
                    start_payload["events_url"]
                )
                journal_run = await app_module.agent_run_journal.get_run(
                    run_id
                )

        self.assertEqual(event_response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["run.started", "run.completed"],
        )
        self.assertEqual(
            events[-1]["payload"]["answer"],
            "streamed answer",
        )
        self.assertIsNotNone(journal_run)
        assert journal_run is not None
        self.assertEqual(journal_run["status"], "COMPLETED")
        self.assertEqual(journal_run["event_count"], 2)
        run_interactive.assert_awaited_once()
        self.assertIsNotNone(
            run_interactive.await_args.kwargs["event_sink"]
        )

    async def test_streaming_approval_resumes_on_the_same_event_channel(
        self,
    ) -> None:
        interruption = SimpleNamespace(
            tool_name="execute_basic_safe_home",
            tool_namespace=None,
            raw_item={"call_id": "safe-home-1", "arguments": "{}"},
        )

        class State:
            approved = False

            def get_interruptions(self):
                return [interruption]

            def approve(self, item) -> None:
                self.approved = item is interruption

            def reject(self, _item, *, rejection_message=None) -> None:
                self.approved = False

        state = State()
        approval = app_module.PrototypeAgentDriver._approval_description(
            interruption
        )
        results = [
            InteractiveAgentResult(
                answer=None,
                state=state,
                approvals=[approval],
            ),
            InteractiveAgentResult(
                answer="safe-home completed",
                state=None,
                approvals=[],
            ),
        ]
        transport = httpx.ASGITransport(app=app_module.app)
        with patch.object(
            app_module.driver,
            "run_interactive",
            new=AsyncMock(side_effect=results),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                started = await client.post(
                    "/api/streaming-runs",
                    json={
                        "prompt": "return to safe home",
                        "agent_model": app_module.settings.openai_model,
                        "reasoning_effort": (
                            app_module.settings.openai_agent_reasoning_effort
                        ),
                        "vlm_model": "auto",
                    },
                )
                self.assertEqual(started.status_code, 202)
                run_id = started.json()["run_id"]
                for _ in range(20):
                    status = await client.get(
                        f"/api/streaming-runs/{run_id}"
                    )
                    if status.json()["status"] == "AWAITING_APPROVAL":
                        break
                    await asyncio.sleep(0)
                self.assertEqual(
                    status.json()["status"],
                    "AWAITING_APPROVAL",
                )

                decision = await client.post(
                    f"/api/streaming-runs/{run_id}/decision",
                    json={"approve": True, "approval_mode": "MANUAL"},
                )
                self.assertEqual(decision.status_code, 202)
                for _ in range(20):
                    status = await client.get(
                        f"/api/streaming-runs/{run_id}"
                    )
                    if status.json()["status"] == "COMPLETED":
                        break
                    await asyncio.sleep(0)
                event_response = await client.get(
                    f"/api/streaming-runs/{run_id}/events"
                )

        self.assertTrue(state.approved)
        self.assertEqual(status.json()["status"], "COMPLETED")
        events = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            [
                "run.started",
                "approval.required",
                "approval.resolved",
                "run.completed",
            ],
        )

    async def test_all_pages_use_the_canonical_replayable_stream_contract(
        self,
    ) -> None:
        completed = InteractiveAgentResult(
            answer="shared autonomous answer",
            state=None,
            approvals=[],
        )
        transport = httpx.ASGITransport(app=app_module.app)
        with patch.object(
            app_module.driver,
            "run_interactive",
            new=AsyncMock(return_value=completed),
        ) as run_interactive:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                legacy_dev = await client.post(
                    "/api/dev/streaming-runs",
                    json={"prompt": "obsolete alias"},
                )
                legacy_sync = await client.post(
                    "/api/run",
                    json={"prompt": "obsolete synchronous route"},
                )
                self.assertEqual(legacy_dev.status_code, 404)
                self.assertEqual(legacy_sync.status_code, 404)
                started = await client.post(
                    "/api/streaming-runs",
                    json={
                        "prompt": "inspect development state",
                        "agent_model": app_module.settings.openai_model,
                        "reasoning_effort": (
                            app_module.settings.openai_agent_reasoning_effort
                        ),
                        "vlm_model": "auto",
                    },
                )
                self.assertEqual(started.status_code, 202)
                start_payload = started.json()
                self.assertIn(
                    "/api/streaming-runs/",
                    start_payload["decision_url"],
                )
                run_id = start_payload["run_id"]

                for _ in range(20):
                    status = await client.get(
                        f"/api/streaming-runs/{run_id}"
                    )
                    if status.json()["status"] == "COMPLETED":
                        break
                    await asyncio.sleep(0)
                self.assertEqual(status.json()["status"], "COMPLETED")
                event_response = await client.get(
                    start_payload["events_url"]
                )

        events = [
            json.loads(line.removeprefix("data: "))
            for line in event_response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [event["type"] for event in events],
            ["run.started", "run.completed"],
        )
        self.assertEqual(events[0]["payload"]["surface"], "autonomous")
        self.assertEqual(
            events[-1]["payload"]["answer"],
            "shared autonomous answer",
        )
        run_interactive.assert_awaited_once()
        self.assertIsNotNone(
            run_interactive.await_args.kwargs["event_sink"]
        )


if __name__ == "__main__":
    unittest.main()
