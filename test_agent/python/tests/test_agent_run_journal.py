from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from physical_agent_test.agent_event_stream import AgentRunStreamRegistry
from physical_agent_test.agent_run_journal import AgentRunJournal


class AgentRunJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_boot_sessions_parent_runs_and_public_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent_runs.sqlite3"
            journal = AgentRunJournal(path)
            await journal.start(session_id="manager-boot-a")
            await journal.record_turn(
                "run-a",
                prompt="inspect the workcell",
                agent_model="test-agent",
                reasoning_effort="medium",
                vlm_model="test-vlm",
                attachment_count=1,
            )
            registry = AgentRunStreamRegistry(journal=journal)
            channel = await registry.create("run-a")
            await channel.publish(
                "run.started",
                {"midbrain_session_id": "manager-boot-a"},
            )
            await channel.publish(
                "assistant.reasoning_summary.delta",
                {"text": "Checking the visible workspace."},
            )
            await channel.publish(
                "run.completed",
                {"answer": "The workcell is clear."},
            )
            await channel.set_status("COMPLETED")
            await journal.set_active_session("manager-boot-b")
            await journal.record_turn(
                "run-b",
                prompt="check the new location",
                agent_model="test-agent",
                reasoning_effort="low",
                vlm_model="test-vlm",
                attachment_count=0,
            )

            sessions = await journal.list_sessions(limit=10)
            first_session = await journal.get_session(
                "manager-boot-a",
                include_events=True,
            )
            await journal.close()

            self.assertEqual(
                {session["session_id"] for session in sessions},
                {"manager-boot-a", "manager-boot-b"},
            )
            statuses = {
                session["session_id"]: session["status"]
                for session in sessions
            }
            self.assertEqual(statuses["manager-boot-a"], "CLOSED")
            self.assertEqual(statuses["manager-boot-b"], "ACTIVE")
            assert first_session is not None
            self.assertEqual(first_session["runs"][0]["user_prompt"], "inspect the workcell")
            self.assertEqual(first_session["runs"][0]["assistant_answer"], "The workcell is clear.")
            self.assertEqual(
                [event["type"] for event in first_session["runs"][0]["events"]],
                [
                    "run.started",
                    "assistant.reasoning_summary.delta",
                    "run.completed",
                ],
            )

    async def test_v1_database_migrates_existing_runs_to_legacy_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent_runs.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        surface TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        terminal_at TEXT,
                        last_sequence INTEGER NOT NULL DEFAULT 0,
                        event_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE events (
                        run_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        occurred_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, sequence)
                    );
                    INSERT INTO runs (
                        run_id, status, surface, started_at, updated_at,
                        terminal_at, last_sequence, event_count
                    ) VALUES (
                        'old-run', 'COMPLETED', 'autonomous',
                        '2026-07-01T00:00:00Z', '2026-07-01T00:01:00Z',
                        '2026-07-01T00:01:00Z', 0, 0
                    );
                    """
                )
                connection.commit()

            journal = AgentRunJournal(path)
            await journal.start(session_id="manager-boot-new")
            sessions = await journal.list_sessions(limit=10)
            old_run = await journal.get_run("old-run")
            await journal.close()

            self.assertEqual(old_run["session_id"], "legacy")
            statuses = {
                session["session_id"]: session["status"]
                for session in sessions
            }
            self.assertEqual(statuses["legacy"], "HISTORICAL")
            self.assertEqual(statuses["manager-boot-new"], "ACTIVE")

    async def test_registry_persists_sdk_neutral_events_across_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent_runs.sqlite3"
            journal = AgentRunJournal(path)
            await journal.start()
            registry = AgentRunStreamRegistry(journal=journal)
            channel = await registry.create("run-durable")
            await channel.publish(
                "run.started",
                {
                    "surface": "developer",
                    "agent_model": "test-model",
                    "reasoning_effort": "medium",
                },
            )
            await channel.set_status("RUNNING")
            await channel.publish(
                "tool.called",
                {"tool_name": "inspect_midbrain_runtime"},
            )
            await channel.publish(
                "run.completed",
                {"status": "completed", "answer": "done"},
            )
            await channel.set_status("COMPLETED")

            run = await journal.get_run("run-durable")
            events = await journal.events("run-durable")
            await journal.close()

            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run["status"], "COMPLETED")
            self.assertEqual(run["surface"], "developer")
            self.assertEqual(run["event_count"], 3)
            self.assertEqual(
                [event["type"] for event in events],
                ["run.started", "tool.called", "run.completed"],
            )
            self.assertNotIn("sdk", str(events).lower())

            reopened = AgentRunJournal(path)
            await reopened.start()
            restored = await reopened.get_run("run-durable")
            restored_events = await reopened.events("run-durable")
            await reopened.close()

            self.assertEqual(restored, run)
            self.assertEqual(restored_events, events)

    async def test_start_marks_unfinished_prior_process_run_interrupted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent_runs.sqlite3"
            journal = AgentRunJournal(path)
            await journal.start()
            registry = AgentRunStreamRegistry(journal=journal)
            channel = await registry.create("run-interrupted")
            await channel.publish("run.started", {})
            await channel.set_status("RUNNING")
            await journal.flush()
            await journal.close()

            with closing(sqlite3.connect(path)) as connection:
                status_after_close = connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    ("run-interrupted",),
                ).fetchone()[0]

            reopened = AgentRunJournal(path)
            await reopened.start()
            restored = await reopened.get_run("run-interrupted")
            await reopened.close()

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(status_after_close, "INTERRUPTED")
            self.assertEqual(restored["status"], "INTERRUPTED")
            self.assertIsNotNone(restored["terminal_at"])

    async def test_retention_bounds_runs_and_events_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = AgentRunJournal(
                Path(temporary) / "agent_runs.sqlite3",
                maximum_runs=2,
                maximum_events_per_run=3,
            )
            await journal.start()
            registry = AgentRunStreamRegistry(
                maximum_events=3,
                journal=journal,
            )
            for run_index in range(3):
                run_id = f"run-{run_index}"
                channel = await registry.create(run_id)
                for event_index in range(5):
                    await channel.publish(
                        "tool.completed",
                        {"index": event_index},
                    )
                await channel.set_status("COMPLETED")
                await asyncio.sleep(0.002)

            runs = await journal.list_runs(limit=10)
            events = await journal.events("run-2")
            await journal.close()

            self.assertEqual(
                [run["run_id"] for run in runs],
                ["run-2", "run-1"],
            )
            self.assertEqual(
                [event["sequence"] for event in events],
                [3, 4, 5],
            )
            self.assertEqual(runs[0]["event_count"], 3)

    async def test_health_reports_writer_state_without_exposing_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = AgentRunJournal(
                Path(temporary) / "private" / "agent_runs.sqlite3"
            )
            await journal.start()
            health = journal.health_snapshot()
            await journal.close()

            self.assertEqual(health["status"], "ok")
            self.assertTrue(health["started"])
            self.assertEqual(health["database"], "agent_runs.sqlite3")
            self.assertNotIn(temporary, str(health))

    async def test_unavailable_storage_degrades_without_blocking_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocked_parent = Path(temporary) / "not-a-directory"
            blocked_parent.write_text("occupied", encoding="utf-8")
            journal = AgentRunJournal(blocked_parent / "agent_runs.sqlite3")

            await journal.start()
            await journal.record_event(
                {
                    "schema": "midbrain.agent_event",
                    "schema_version": 1,
                    "event_id": "degraded:1",
                    "sequence": 1,
                    "occurred_at": "2026-08-02T00:00:00Z",
                    "run_id": "degraded",
                    "source": "test",
                    "type": "run.started",
                    "payload": {},
                }
            )
            await journal.record_status("degraded", "RUNNING")
            health = journal.health_snapshot()
            await journal.close()

            self.assertEqual(health["status"], "error")
            self.assertFalse(health["started"])
            self.assertIsNotNone(health["last_error"])


if __name__ == "__main__":
    unittest.main()
