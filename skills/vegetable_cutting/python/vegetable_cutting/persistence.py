from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class PlanStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, result: dict[str, Any]) -> Path:
        plan_id = str(result["plan_id"])
        target = self.root / f"{plan_id}.json"
        self._atomic_json(target, result)
        self._atomic_json(self.root / "latest.json", result)
        return target

    def latest(self) -> dict[str, Any] | None:
        path = self.root / "latest.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            if path.name == "latest.json":
                continue
            try:
                output.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return output

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
