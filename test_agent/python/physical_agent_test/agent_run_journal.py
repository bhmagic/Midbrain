from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "INTERRUPTED"}


@dataclass(frozen=True)
class _JournalOperation:
    kind: str
    value: Any = None
    completion: asyncio.Future[None] | None = None


class AgentRunJournal:
    """Bounded SQLite journal for SDK-neutral Midbrain Agent events."""

    def __init__(
        self,
        path: Path,
        *,
        maximum_runs: int = 500,
        maximum_events_per_run: int = 2048,
        retention_days: float = 30.0,
        maximum_pending_operations: int = 8192,
        batch_size: int = 128,
    ) -> None:
        if maximum_runs < 1:
            raise ValueError("maximum_runs must be positive")
        if maximum_events_per_run < 1:
            raise ValueError("maximum_events_per_run must be positive")
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        if maximum_pending_operations < 1:
            raise ValueError("maximum_pending_operations must be positive")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.path = Path(path)
        self.maximum_runs = int(maximum_runs)
        self.maximum_events_per_run = int(maximum_events_per_run)
        self.retention_days = float(retention_days)
        self.maximum_pending_operations = int(maximum_pending_operations)
        self.batch_size = int(batch_size)
        self._queue: asyncio.Queue[_JournalOperation] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._connection: sqlite3.Connection | None = None
        self._database_lock = threading.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._start_attempted = False
        self._active_session_id = "legacy"

    async def start(self, *, session_id: str | None = None) -> None:
        async with self._lifecycle_lock:
            if session_id is not None:
                self._active_session_id = _normalize_session_id(session_id)
            if self._writer_task is not None and not self._writer_task.done():
                return
            self._start_attempted = True
            try:
                self._connection = await asyncio.to_thread(
                    self._open_connection
                )
            except Exception as error:
                self._connection = None
                self._last_error = str(error)
                return
            self._queue = asyncio.Queue(
                maxsize=self.maximum_pending_operations
            )
            self._last_error = None
            self._writer_task = asyncio.create_task(
                self._writer_loop(),
                name="midbrain-agent-run-journal",
            )

    async def record_turn(
        self,
        run_id: str,
        *,
        prompt: str,
        agent_model: str,
        reasoning_effort: str,
        vlm_model: str,
        attachment_count: int,
        surface: str = "autonomous",
        session_id: str | None = None,
    ) -> None:
        if not await self._ensure_started():
            return
        await self._require_queue().put(
            _JournalOperation(
                "turn",
                {
                    "run_id": str(run_id),
                    "session_id": _normalize_session_id(
                        session_id or self._active_session_id
                    ),
                    "prompt": str(prompt),
                    "agent_model": str(agent_model),
                    "reasoning_effort": str(reasoning_effort),
                    "vlm_model": str(vlm_model),
                    "attachment_count": max(0, int(attachment_count)),
                    "surface": str(surface or "autonomous"),
                },
            )
        )

    async def set_active_session(self, session_id: str) -> None:
        normalized = _normalize_session_id(session_id)
        self._active_session_id = normalized
        if not await self._ensure_started():
            return
        await self._require_queue().put(
            _JournalOperation("session", normalized)
        )

    async def record_event(self, event: dict[str, Any]) -> None:
        if not await self._ensure_started():
            return
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        await self._require_queue().put(
            _JournalOperation("event", serialized)
        )

    async def record_status(self, run_id: str, status: str) -> None:
        if not await self._ensure_started():
            return
        await self._require_queue().put(
            _JournalOperation("status", (str(run_id), str(status)))
        )

    async def flush(self) -> None:
        if self._writer_task is None:
            return
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[None] = loop.create_future()
        await self._require_queue().put(
            _JournalOperation("flush", completion=completion)
        )
        await completion

    async def close(self) -> None:
        async with self._lifecycle_lock:
            task = self._writer_task
            if task is None:
                return
            loop = asyncio.get_running_loop()
            completion: asyncio.Future[None] = loop.create_future()
            await self._require_queue().put(
                _JournalOperation("stop", completion=completion)
            )
            try:
                await completion
                await task
            except Exception as error:
                self._last_error = str(error)
            connection = self._connection
            self._writer_task = None
            self._queue = None
            self._connection = None
            if connection is not None:
                await asyncio.to_thread(self._close_connection, connection)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        await self.flush()
        return await asyncio.to_thread(self._read_run, str(run_id))

    async def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        await self.flush()
        return await asyncio.to_thread(self._read_runs, int(limit))

    async def list_sessions(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        await self.flush()
        return await asyncio.to_thread(self._read_sessions, int(limit))

    async def get_session(
        self,
        session_id: str,
        *,
        run_limit: int = 500,
        include_events: bool = False,
    ) -> dict[str, Any] | None:
        if run_limit < 1:
            raise ValueError("run_limit must be positive")
        await self.flush()
        return await asyncio.to_thread(
            self._read_session,
            _normalize_session_id(session_id),
            int(run_limit),
            bool(include_events),
        )

    async def events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        await self.flush()
        return await asyncio.to_thread(
            self._read_events,
            str(run_id),
            max(0, int(after_sequence)),
        )

    def health_snapshot(self) -> dict[str, Any]:
        task = self._writer_task
        queue = self._queue
        return {
            "status": "error" if self._last_error else "ok",
            "started": task is not None and not task.done(),
            "database": self.path.name,
            "pending_operations": queue.qsize() if queue is not None else 0,
            "maximum_runs": self.maximum_runs,
            "maximum_events_per_run": self.maximum_events_per_run,
            "retention_days": self.retention_days,
            "active_session_id": self._active_session_id,
            "last_error": self._last_error,
        }

    async def _ensure_started(self) -> bool:
        if (
            self._start_attempted
            and self._last_error is not None
            and self._writer_task is None
        ):
            return False
        if self._writer_task is None or self._writer_task.done():
            await self.start()
        return (
            self._writer_task is not None
            and not self._writer_task.done()
        )

    def _require_queue(self) -> asyncio.Queue[_JournalOperation]:
        if self._queue is None:
            raise RuntimeError("Agent run journal is not started")
        return self._queue

    async def _writer_loop(self) -> None:
        queue = self._require_queue()
        while True:
            first = await queue.get()
            operations = [first]
            while len(operations) < self.batch_size:
                try:
                    operations.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            data_operations = [
                operation
                for operation in operations
                if operation.kind in {
                    "session",
                    "turn",
                    "event",
                    "status",
                    "stop",
                }
            ]
            error: Exception | None = None
            if data_operations:
                try:
                    await asyncio.to_thread(
                        self._apply_operations,
                        data_operations,
                    )
                    self._last_error = None
                except Exception as caught:
                    error = caught
                    self._last_error = str(caught)
            should_stop = False
            for operation in operations:
                if operation.completion is not None:
                    if error is None:
                        operation.completion.set_result(None)
                    else:
                        operation.completion.set_exception(error)
                if operation.kind == "stop":
                    should_stop = True
                queue.task_done()
            if should_stop:
                return

    def _open_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT 'legacy',
                status TEXT NOT NULL,
                surface TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                terminal_at TEXT,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                event_count INTEGER NOT NULL DEFAULT 0,
                user_prompt TEXT,
                agent_model TEXT,
                reasoning_effort TEXT,
                vlm_model TEXT,
                attachment_count INTEGER NOT NULL DEFAULT 0,
                assistant_answer TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    ON DELETE CASCADE
            );
            """
        )
        self._migrate_runs_schema(connection)
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated
                ON sessions(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_session
                ON runs(session_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_updated
                ON runs(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_events_type
                ON events(event_type, occurred_at);
            PRAGMA user_version=2;
            """
        )
        now = _utc_now()
        self._activate_session(connection, self._active_session_id, now)
        connection.execute(
            """
            UPDATE runs
            SET status = 'INTERRUPTED', updated_at = ?, terminal_at = ?
            WHERE status NOT IN ('COMPLETED', 'FAILED', 'INTERRUPTED')
            """,
            (now, now),
        )
        connection.commit()
        return connection

    def _migrate_runs_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(runs)")
        }
        additions = {
            "session_id": "TEXT NOT NULL DEFAULT 'legacy'",
            "user_prompt": "TEXT",
            "agent_model": "TEXT",
            "reasoning_effort": "TEXT",
            "vlm_model": "TEXT",
            "attachment_count": "INTEGER NOT NULL DEFAULT 0",
            "assistant_answer": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE runs ADD COLUMN {name} {declaration}"
                )
        legacy = connection.execute(
            """
            SELECT MIN(started_at) AS started_at, MAX(updated_at) AS updated_at
            FROM runs WHERE session_id = 'legacy'
            """
        ).fetchone()
        if legacy is not None and legacy["started_at"] is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO sessions (
                    session_id, status, started_at, updated_at, ended_at
                ) VALUES ('legacy', 'HISTORICAL', ?, ?, ?)
                """,
                (
                    str(legacy["started_at"]),
                    str(legacy["updated_at"]),
                    str(legacy["updated_at"]),
                ),
            )

    def _activate_session(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE sessions
            SET status = 'CLOSED', ended_at = COALESCE(ended_at, updated_at)
            WHERE status = 'ACTIVE' AND session_id <> ?
            """,
            (session_id,),
        )
        connection.execute(
            """
            INSERT INTO sessions (
                session_id, status, started_at, updated_at, ended_at
            ) VALUES (?, 'ACTIVE', ?, ?, NULL)
            ON CONFLICT(session_id) DO UPDATE SET
                status = 'ACTIVE',
                updated_at = MAX(sessions.updated_at, excluded.updated_at),
                ended_at = NULL
            """,
            (session_id, now, now),
        )

    def _close_connection(self, connection: sqlite3.Connection) -> None:
        with self._database_lock:
            connection.close()

    def _apply_operations(
        self,
        operations: list[_JournalOperation],
    ) -> None:
        connection = self._require_connection()
        with self._database_lock, connection:
            touched_runs: set[str] = set()
            terminal_update = False
            for operation in operations:
                if operation.kind == "session":
                    self._activate_session(
                        connection,
                        _normalize_session_id(operation.value),
                        _utc_now(),
                    )
                elif operation.kind == "turn":
                    run_id = self._write_turn(connection, operation.value)
                    touched_runs.add(run_id)
                elif operation.kind == "event":
                    run_id = self._write_event(
                        connection,
                        str(operation.value),
                    )
                    touched_runs.add(run_id)
                elif operation.kind == "status":
                    run_id, status = operation.value
                    self._write_status(connection, run_id, status)
                    touched_runs.add(run_id)
                    terminal_update = terminal_update or (
                        status in TERMINAL_RUN_STATUSES
                    )
                elif operation.kind == "stop":
                    self._interrupt_active_runs(connection)
                    terminal_update = True
            for run_id in touched_runs:
                self._prune_run_events(connection, run_id)
            if terminal_update:
                self._prune_runs(connection)

    def _write_turn(
        self,
        connection: sqlite3.Connection,
        turn: dict[str, Any],
    ) -> str:
        run_id = str(turn["run_id"])
        session_id = _normalize_session_id(turn["session_id"])
        now = _utc_now()
        self._activate_session(connection, session_id, now)
        connection.execute(
            """
            INSERT INTO runs (
                run_id, session_id, status, surface, started_at, updated_at,
                user_prompt, agent_model, reasoning_effort, vlm_model,
                attachment_count
            ) VALUES (?, ?, 'STARTING', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                session_id = excluded.session_id,
                surface = excluded.surface,
                updated_at = excluded.updated_at,
                user_prompt = excluded.user_prompt,
                agent_model = excluded.agent_model,
                reasoning_effort = excluded.reasoning_effort,
                vlm_model = excluded.vlm_model,
                attachment_count = excluded.attachment_count
            """,
            (
                run_id,
                session_id,
                str(turn["surface"]),
                now,
                now,
                str(turn["prompt"]),
                str(turn["agent_model"]),
                str(turn["reasoning_effort"]),
                str(turn["vlm_model"]),
                int(turn["attachment_count"]),
            ),
        )
        return run_id

    def _write_event(
        self,
        connection: sqlite3.Connection,
        serialized: str,
    ) -> str:
        event = json.loads(serialized)
        run_id = str(event["run_id"])
        sequence = int(event["sequence"])
        occurred_at = str(event["occurred_at"])
        payload = event.get("payload")
        event_type = str(event["type"])
        surface = (
            str(payload.get("surface") or "autonomous")
            if isinstance(payload, dict)
            else "autonomous"
        )
        requested_session_id = (
            _normalize_session_id(payload.get("midbrain_session_id"))
            if isinstance(payload, dict)
            and payload.get("midbrain_session_id")
            else None
        )
        existing = connection.execute(
            "SELECT session_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        session_id = (
            str(existing["session_id"])
            if existing is not None
            else requested_session_id or self._active_session_id
        )
        if existing is None:
            self._activate_session(connection, session_id, occurred_at)
        connection.execute(
            """
            INSERT INTO runs (
                run_id, session_id, status, surface, started_at, updated_at,
                last_sequence, event_count
            ) VALUES (?, ?, 'STARTING', ?, ?, ?, ?, 0)
            ON CONFLICT(run_id) DO UPDATE SET
                surface = CASE
                    WHEN excluded.surface <> 'autonomous'
                    THEN excluded.surface
                    ELSE runs.surface
                END,
                updated_at = excluded.updated_at,
                last_sequence = MAX(runs.last_sequence, excluded.last_sequence)
            """,
            (
                run_id,
                session_id,
                surface,
                occurred_at,
                occurred_at,
                sequence,
            ),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO events (
                run_id, sequence, occurred_at, source, event_type, event_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                occurred_at,
                str(event["source"]),
                event_type,
                serialized,
            ),
        )
        if event_type == "run.completed" and isinstance(payload, dict):
            connection.execute(
                "UPDATE runs SET assistant_answer = ? WHERE run_id = ?",
                (str(payload.get("answer") or ""), run_id),
            )
        elif event_type == "run.failed" and isinstance(payload, dict):
            connection.execute(
                "UPDATE runs SET assistant_answer = ? WHERE run_id = ?",
                (str(payload.get("error") or ""), run_id),
            )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (occurred_at, session_id),
        )
        return run_id

    def _write_status(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        status: str,
    ) -> None:
        now = _utc_now()
        terminal_at = now if status in TERMINAL_RUN_STATUSES else None
        connection.execute(
            """
            INSERT INTO runs (
                run_id, session_id, status, surface, started_at, updated_at,
                terminal_at
            ) VALUES (?, ?, ?, 'autonomous', ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                terminal_at = excluded.terminal_at
            """,
            (
                run_id,
                self._active_session_id,
                status,
                now,
                now,
                terminal_at,
            ),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (
                now,
                str(
                    connection.execute(
                        "SELECT session_id FROM runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()["session_id"]
                ),
            ),
        )

    def _prune_run_events(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM events
            WHERE run_id = ? AND sequence <= (
                SELECT COALESCE(MAX(sequence), 0) - ?
                FROM events WHERE run_id = ?
            )
            """,
            (run_id, self.maximum_events_per_run, run_id),
        )
        connection.execute(
            """
            UPDATE runs SET event_count = (
                SELECT COUNT(*) FROM events WHERE events.run_id = runs.run_id
            ) WHERE run_id = ?
            """,
            (run_id,),
        )

    def _interrupt_active_runs(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        now = _utc_now()
        connection.execute(
            """
            UPDATE runs
            SET status = 'INTERRUPTED', updated_at = ?, terminal_at = ?
            WHERE status NOT IN ('COMPLETED', 'FAILED', 'INTERRUPTED')
            """,
            (now, now),
        )

    def _prune_runs(self, connection: sqlite3.Connection) -> None:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=self.retention_days)
        ).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            DELETE FROM runs
            WHERE status IN ('COMPLETED', 'FAILED', 'INTERRUPTED')
              AND updated_at < ?
            """,
            (cutoff,),
        )
        connection.execute(
            """
            DELETE FROM runs WHERE run_id IN (
                SELECT run_id FROM runs
                WHERE status IN ('COMPLETED', 'FAILED', 'INTERRUPTED')
                ORDER BY updated_at DESC, run_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.maximum_runs,),
        )
        connection.execute(
            """
            DELETE FROM sessions
            WHERE status <> 'ACTIVE'
              AND NOT EXISTS (
                  SELECT 1 FROM runs WHERE runs.session_id = sessions.session_id
              )
            """
        )

    def _read_run(self, run_id: str) -> dict[str, Any] | None:
        connection = self._require_connection()
        with self._database_lock:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _read_runs(self, limit: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        with self._database_lock:
            rows = connection.execute(
                """
                SELECT * FROM runs
                ORDER BY updated_at DESC, run_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _read_sessions(self, limit: int) -> list[dict[str, Any]]:
        connection = self._require_connection()
        with self._database_lock:
            rows = connection.execute(
                """
                SELECT
                    sessions.*,
                    COUNT(runs.run_id) AS run_count,
                    COALESCE(MAX(runs.updated_at), sessions.updated_at)
                        AS latest_run_at
                FROM sessions
                LEFT JOIN runs ON runs.session_id = sessions.session_id
                GROUP BY sessions.session_id
                ORDER BY latest_run_at DESC, sessions.session_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _read_session(
        self,
        session_id: str,
        run_limit: int,
        include_events: bool,
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        with self._database_lock:
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                return None
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE session_id = ?
                ORDER BY started_at ASC, run_id ASC
                LIMIT ?
                """,
                (session_id, run_limit),
            ).fetchall()
            runs = [dict(row) for row in rows]
            if include_events and runs:
                run_ids = [str(run["run_id"]) for run in runs]
                placeholders = ",".join("?" for _ in run_ids)
                event_rows = connection.execute(
                    f"""
                    SELECT run_id, event_json FROM events
                    WHERE run_id IN ({placeholders})
                    ORDER BY run_id ASC, sequence ASC
                    """,
                    run_ids,
                ).fetchall()
                events_by_run: dict[str, list[dict[str, Any]]] = {
                    run_id: [] for run_id in run_ids
                }
                for event_row in event_rows:
                    events_by_run[str(event_row["run_id"])].append(
                        json.loads(str(event_row["event_json"]))
                    )
                for run in runs:
                    run["events"] = events_by_run[str(run["run_id"])]
        return {"session": dict(session), "runs": runs}

    def _read_events(
        self,
        run_id: str,
        after_sequence: int,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        with self._database_lock:
            rows = connection.execute(
                """
                SELECT event_json FROM events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [json.loads(str(row["event_json"])) for row in rows]

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Agent run journal is not started")
        return self._connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_session_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "legacy"
    return normalized[:200]
