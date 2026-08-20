from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .skill_catalog import AgentSkillDescriptor


STATE_SCHEMA = "midbrain.agent_skill_installation_decisions"
STATE_SCHEMA_VERSION = 1
DECISION_ENABLED = "ENABLED"
DECISION_DISABLED = "DISABLED"
SUPPORTED_DECISIONS = {DECISION_ENABLED, DECISION_DISABLED}


class AgentSkillInstallationRegistry:
    """Persist Agent-owned eligibility decisions without editing Skill manifests."""

    def __init__(
        self,
        path: Path,
        *,
        configured_enabled_tool_names: Iterable[str],
    ) -> None:
        self.path = Path(path)
        self.configured_enabled_tool_names = {
            str(value).strip()
            for value in configured_enabled_tool_names
            if str(value).strip()
        }
        self._lock = threading.RLock()
        self._decisions = self._load()

    def effective_enabled_tool_names(self) -> set[str]:
        with self._lock:
            enabled = set(self.configured_enabled_tool_names)
            enabled.update(
                tool_name
                for tool_name, record in self._decisions.items()
                if record["state"] == DECISION_ENABLED
            )
            enabled.difference_update(
                tool_name
                for tool_name, record in self._decisions.items()
                if record["state"] == DECISION_DISABLED
            )
            return enabled

    def snapshot(
        self,
        descriptors: Iterable[AgentSkillDescriptor],
        *,
        runtime_tool_names: Iterable[str],
    ) -> dict[str, Any]:
        descriptor_list = [
            descriptor for descriptor in descriptors if descriptor.discoverable
        ]
        runtime = {
            str(value).strip()
            for value in runtime_tool_names
            if str(value).strip()
        }
        with self._lock:
            decisions = {
                tool_name: dict(record)
                for tool_name, record in self._decisions.items()
            }
            unresolved = []
            pending_restart = []
            for descriptor in sorted(
                descriptor_list,
                key=lambda item: item.tool_name,
            ):
                tool_name = descriptor.tool_name
                decision = decisions.get(tool_name)
                record = self._descriptor_record(descriptor)
                if (
                    tool_name in runtime
                    or tool_name in self.configured_enabled_tool_names
                ):
                    continue
                if decision is None:
                    unresolved.append(record)
                elif decision["state"] == DECISION_ENABLED:
                    pending_restart.append(record)
            disabled = sorted(
                tool_name
                for tool_name, record in decisions.items()
                if record["state"] == DECISION_DISABLED
            )
            return {
                "schema": "midbrain.agent_skill_installation_status",
                "schema_version": 1,
                "configured_tool_names": sorted(
                    self.configured_enabled_tool_names
                ),
                "effective_enabled_tool_names": sorted(
                    self.effective_enabled_tool_names()
                ),
                "runtime_tool_names": sorted(runtime),
                "disabled_tool_names": disabled,
                "unresolved": unresolved,
                "pending_restart": pending_restart,
                "prompt_required": bool(unresolved),
                "restart_required": bool(pending_restart),
                "decision_path": str(self.path),
                "manifest_mutation_allowed": False,
            }

    def decide(
        self,
        tool_name: str,
        *,
        action: str,
        descriptors: Iterable[AgentSkillDescriptor],
        runtime_tool_names: Iterable[str],
    ) -> dict[str, Any]:
        normalized_tool_name = str(tool_name).strip()
        normalized_action = str(action).strip().upper()
        state = {
            "ADD": DECISION_ENABLED,
            "DISABLE": DECISION_DISABLED,
        }.get(normalized_action)
        if state is None:
            raise ValueError("action must be ADD or DISABLE")
        descriptor_by_name = {
            descriptor.tool_name: descriptor
            for descriptor in descriptors
            if descriptor.discoverable
        }
        if normalized_tool_name not in descriptor_by_name:
            raise KeyError(normalized_tool_name)
        runtime = {
            str(value).strip()
            for value in runtime_tool_names
            if str(value).strip()
        }
        if normalized_tool_name in runtime:
            raise RuntimeError(
                "Skill is already active in the current Agent tool list"
            )
        with self._lock:
            existing = self._decisions.get(normalized_tool_name)
            if existing is not None and existing["state"] != state:
                raise RuntimeError(
                    "Skill already has a different persisted installation decision"
                )
            self._decisions[normalized_tool_name] = {
                "state": state,
                "decided_at_us": time.time_ns() // 1000,
            }
            self._write_locked()
        return self.snapshot(
            descriptor_by_name.values(),
            runtime_tool_names=runtime,
        )

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema") != STATE_SCHEMA
            or value.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(value.get("decisions"), dict)
        ):
            raise ValueError(
                f"invalid Agent Skill installation decision file: {self.path}"
            )
        decisions: dict[str, dict[str, Any]] = {}
        for tool_name, record in value["decisions"].items():
            if (
                not isinstance(tool_name, str)
                or not tool_name.strip()
                or not isinstance(record, dict)
                or record.get("state") not in SUPPORTED_DECISIONS
            ):
                raise ValueError(
                    f"invalid Agent Skill installation decision: {tool_name!r}"
                )
            decisions[tool_name.strip()] = {
                "state": str(record["state"]),
                "decided_at_us": int(record.get("decided_at_us") or 0),
            }
        return decisions

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": STATE_SCHEMA,
            "schema_version": STATE_SCHEMA_VERSION,
            "decisions": {
                tool_name: self._decisions[tool_name]
                for tool_name in sorted(self._decisions)
            },
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _descriptor_record(
        descriptor: AgentSkillDescriptor,
    ) -> dict[str, Any]:
        return {
            "tool_name": descriptor.tool_name,
            "skill_type": descriptor.skill_type,
            "display_name": descriptor.display_name,
            "skill_version": descriptor.skill_version,
            "manifest_path": descriptor.manifest_path,
            "description": descriptor.description,
            "safety_class": descriptor.safety_class,
            "expected_latency": descriptor.expected_latency,
            "side_effects": list(descriptor.side_effects),
            "execution_adapter_id": descriptor.execution_adapter_id,
            "execution_adapter_kind": descriptor.execution_adapter_kind,
        }
