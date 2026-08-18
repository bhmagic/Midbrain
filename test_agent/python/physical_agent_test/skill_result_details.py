from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class SkillResultDetailStore:
    """Bounded local storage for sanitized complete Skill results."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        maximum_results: int = 1000,
        maximum_result_bytes: int = 1_048_576,
        maximum_total_bytes: int = 64 * 1024 * 1024,
        retention_days: float = 7.0,
    ) -> None:
        if maximum_results < 1:
            raise ValueError("maximum_results must be positive")
        if maximum_result_bytes < 1024:
            raise ValueError("maximum_result_bytes must be at least 1024")
        if maximum_total_bytes < maximum_result_bytes:
            raise ValueError(
                "maximum_total_bytes must be at least maximum_result_bytes"
            )
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        normalized_session_id = str(session_id).strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")
        self.path = Path(path)
        self.session_id = normalized_session_id
        self.maximum_results = int(maximum_results)
        self.maximum_result_bytes = int(maximum_result_bytes)
        self.maximum_total_bytes = int(maximum_total_bytes)
        self.retention_days = float(retention_days)
        self._last_error: str | None = None

    async def store(
        self,
        payload: dict[str, Any],
        *,
        tool_name: str,
        skill_type: str,
        skill_version: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._store,
                payload,
                str(tool_name),
                str(skill_type),
                str(skill_version),
                output_schema,
            )
        except Exception as error:
            self._last_error = str(error)
            return {
                "schema": "midbrain.skill_result_detail_ref",
                "schema_version": 1,
                "available": False,
                "reason": "DETAIL_STORE_UNAVAILABLE",
            }

    async def retrieve(
        self,
        result_id: str,
    ) -> dict[str, Any] | None:
        normalized = str(result_id).strip()
        if not normalized:
            raise ValueError("result_id must not be empty")
        return await asyncio.to_thread(self._retrieve, normalized)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "status": "error" if self._last_error else "ok",
            "database": self.path.name,
            "maximum_results": self.maximum_results,
            "maximum_result_bytes": self.maximum_result_bytes,
            "maximum_total_bytes": self.maximum_total_bytes,
            "retention_days": self.retention_days,
            "session_id": self.session_id,
            "last_error": self._last_error,
        }

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_result_details (
                result_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                skill_type TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                schema_sha256 TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_skill_result_details_created "
            "ON skill_result_details(created_at_us)"
        )
        return connection

    def _store(
        self,
        payload: dict[str, Any],
        tool_name: str,
        skill_type: str,
        skill_version: str,
        output_schema: dict[str, Any],
    ) -> dict[str, Any]:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        encoded = serialized.encode("utf-8")
        if len(encoded) > self.maximum_result_bytes:
            return {
                "schema": "midbrain.skill_result_detail_ref",
                "schema_version": 1,
                "available": False,
                "reason": "DETAIL_RESULT_TOO_LARGE",
                "size_bytes": len(encoded),
            }
        schema_encoded = json.dumps(
            output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        result_id = uuid.uuid4().hex
        created_at_us = int(time.time() * 1_000_000)
        expires_at_us = created_at_us + int(
            self.retention_days * 86_400 * 1_000_000
        )
        payload_sha256 = hashlib.sha256(encoded).hexdigest()
        schema_sha256 = hashlib.sha256(schema_encoded).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO skill_result_details (
                    result_id, session_id, created_at_us, expires_at_us,
                    tool_name, skill_type, skill_version, schema_sha256,
                    payload_sha256, size_bytes, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    self.session_id,
                    created_at_us,
                    expires_at_us,
                    tool_name,
                    skill_type,
                    skill_version,
                    schema_sha256,
                    payload_sha256,
                    len(encoded),
                    serialized,
                ),
            )
            self._prune(connection, created_at_us)
        self._last_error = None
        return {
            "schema": "midbrain.skill_result_detail_ref",
            "schema_version": 1,
            "available": True,
            "result_id": result_id,
            "tool_name": tool_name,
            "skill_type": skill_type,
            "skill_version": skill_version,
            "schema_sha256": schema_sha256,
            "payload_sha256": payload_sha256,
            "size_bytes": len(encoded),
            "created_at_us": created_at_us,
            "expires_at_us": expires_at_us,
        }

    def _retrieve(self, result_id: str) -> dict[str, Any] | None:
        now_us = int(time.time() * 1_000_000)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, created_at_us, expires_at_us, tool_name,
                       skill_type, skill_version, schema_sha256,
                       payload_sha256, size_bytes, payload_json
                FROM skill_result_details
                WHERE result_id = ?
                """,
                (result_id,),
            ).fetchone()
            if row is None:
                return None
            if str(row[0]) != self.session_id:
                return None
            if int(row[2]) < now_us:
                connection.execute(
                    "DELETE FROM skill_result_details WHERE result_id = ?",
                    (result_id,),
                )
                return None
        return {
            "result_id": result_id,
            "session_id": row[0],
            "created_at_us": int(row[1]),
            "expires_at_us": int(row[2]),
            "tool_name": row[3],
            "skill_type": row[4],
            "skill_version": row[5],
            "schema_sha256": row[6],
            "payload_sha256": row[7],
            "size_bytes": int(row[8]),
            "payload": json.loads(row[9]),
        }

    def _prune(
        self,
        connection: sqlite3.Connection,
        now_us: int,
    ) -> None:
        connection.execute(
            "DELETE FROM skill_result_details WHERE expires_at_us < ?",
            (now_us,),
        )
        connection.execute(
            """
            DELETE FROM skill_result_details
            WHERE result_id IN (
                SELECT result_id FROM skill_result_details
                ORDER BY created_at_us DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.maximum_results,),
        )
        while True:
            total = connection.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM skill_result_details"
            ).fetchone()[0]
            if int(total) <= self.maximum_total_bytes:
                break
            deleted = connection.execute(
                """
                DELETE FROM skill_result_details
                WHERE result_id = (
                    SELECT result_id FROM skill_result_details
                    ORDER BY created_at_us ASC
                    LIMIT 1
                )
                """
            ).rowcount
            if not deleted:
                break
