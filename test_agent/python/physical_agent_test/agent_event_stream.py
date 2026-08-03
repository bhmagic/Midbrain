from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


EVENT_SCHEMA = "midbrain.agent_event"
EVENT_SCHEMA_VERSION = 1


class AgentEventJournal(Protocol):
    async def record_event(self, event: dict[str, Any]) -> None: ...

    async def record_status(self, run_id: str, status: str) -> None: ...


@dataclass
class AgentRunChannel:
    run_id: str
    maximum_events: int = 2048
    journal: AgentEventJournal | None = None
    status: str = "STARTING"
    result: dict[str, Any] | None = None
    updated_monotonic: float = field(default_factory=time.monotonic)
    _events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _sequence: int = field(default=0, repr=False)
    _condition: asyncio.Condition = field(
        default_factory=asyncio.Condition,
        repr=False,
    )

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._condition:
            self._sequence += 1
            event = {
                "schema": EVENT_SCHEMA,
                "schema_version": EVENT_SCHEMA_VERSION,
                "event_id": f"{self.run_id}:{self._sequence}",
                "sequence": self._sequence,
                "occurred_at": _utc_now(),
                "run_id": self.run_id,
                "source": "openai_agents",
                "type": str(event_type),
                "payload": dict(payload or {}),
            }
            self._events.append(event)
            if len(self._events) > self.maximum_events:
                del self._events[: len(self._events) - self.maximum_events]
            self.updated_monotonic = time.monotonic()
            self._condition.notify_all()
        if self.journal is not None:
            await self.journal.record_event(event)
        return dict(event)

    async def set_status(
        self,
        status: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        async with self._condition:
            self.status = str(status)
            if result is not None:
                self.result = dict(result)
            self.updated_monotonic = time.monotonic()
            self._condition.notify_all()
        if self.journal is not None:
            await self.journal.record_status(self.run_id, self.status)

    async def events_after(
        self,
        sequence: int,
        *,
        timeout_s: float = 15.0,
    ) -> tuple[list[dict[str, Any]], str]:
        async with self._condition:
            def ready() -> bool:
                return (
                    any(
                        int(event["sequence"]) > sequence
                        for event in self._events
                    )
                    or self.status in {"COMPLETED", "FAILED"}
                )

            if not ready():
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(ready),
                        timeout=float(timeout_s),
                    )
                except TimeoutError:
                    pass
            return (
                [
                    dict(event)
                    for event in self._events
                    if int(event["sequence"]) > sequence
                ],
                self.status,
            )

    async def snapshot(self) -> dict[str, Any]:
        async with self._condition:
            oldest_sequence = (
                int(self._events[0]["sequence"]) if self._events else None
            )
            return {
                "run_id": self.run_id,
                "status": self.status,
                "last_sequence": self._sequence,
                "oldest_sequence": oldest_sequence,
                "result": None if self.result is None else dict(self.result),
            }


class AgentRunStreamRegistry:
    def __init__(
        self,
        *,
        retention_s: float = 1800.0,
        maximum_events: int = 2048,
        journal: AgentEventJournal | None = None,
    ):
        self.retention_s = float(retention_s)
        self.maximum_events = int(maximum_events)
        self.journal = journal
        self._channels: dict[str, AgentRunChannel] = {}
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._lock = asyncio.Lock()

    async def create(self, run_id: str) -> AgentRunChannel:
        async with self._lock:
            self._prune_locked()
            if run_id in self._channels:
                raise ValueError(f"agent run already exists: {run_id}")
            channel = AgentRunChannel(
                run_id=run_id,
                maximum_events=self.maximum_events,
                journal=self.journal,
            )
            self._channels[run_id] = channel
            return channel

    async def get(self, run_id: str) -> AgentRunChannel:
        async with self._lock:
            channel = self._channels.get(run_id)
            if channel is None:
                raise KeyError(run_id)
            return channel

    async def launch(
        self,
        run_id: str,
        coroutine: Coroutine[Any, Any, Any],
    ) -> asyncio.Task[Any]:
        async with self._lock:
            if run_id not in self._channels:
                coroutine.close()
                raise KeyError(run_id)
            task = asyncio.create_task(
                coroutine,
                name=f"midbrain-agent-run:{run_id}",
            )
            tasks = self._tasks.setdefault(run_id, set())
            tasks.add(task)

            def discard(completed: asyncio.Task[Any]) -> None:
                tasks.discard(completed)
                if not tasks:
                    self._tasks.pop(run_id, None)

            task.add_done_callback(discard)
            return task

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = tuple(
                task
                for run_tasks in self._tasks.values()
                for task in run_tasks
                if not task.done()
            )
            self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            run_id
            for run_id, channel in self._channels.items()
            if channel.status in {"COMPLETED", "FAILED"}
            and now - channel.updated_monotonic > self.retention_s
            and not self._tasks.get(run_id)
        ]
        for run_id in expired:
            self._channels.pop(run_id, None)
            self._tasks.pop(run_id, None)


async def stream_sse(
    channel: AgentRunChannel,
    *,
    after_sequence: int = 0,
) -> AsyncIterator[str]:
    sequence = max(0, int(after_sequence))
    while True:
        events, status = await channel.events_after(sequence)
        if events:
            for event in events:
                sequence = max(sequence, int(event["sequence"]))
                yield encode_sse(event)
        elif status not in {"COMPLETED", "FAILED"}:
            yield ": keep-alive\n\n"
        if status in {"COMPLETED", "FAILED"} and not events:
            return


def encode_sse(event: dict[str, Any]) -> str:
    data = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return f"id: {int(event['sequence'])}\ndata: {data}\n\n"


def parse_event_sequence(value: str | None) -> int:
    if value is None:
        return 0
    candidate = str(value).strip()
    if not candidate:
        return 0
    if ":" in candidate:
        candidate = candidate.rsplit(":", 1)[-1]
    try:
        return max(0, int(candidate))
    except ValueError:
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
