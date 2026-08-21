from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import json
import math
import os
import tempfile


ProfileValidator = Callable[[dict[str, Any]], dict[str, Any]]


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def finite_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None:
        if exclusive_minimum and result <= minimum:
            raise ValueError(f"{name} must be greater than {minimum}")
        if not exclusive_minimum and result < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def vector3(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    result = [finite_number(component, name) for component in value]
    norm = math.sqrt(sum(component * component for component in result))
    if norm < 1e-8:
        raise ValueError(f"{name} must be non-zero")
    return result


def normalized_profile_name(value: Any, fallback: str) -> str:
    result = " ".join(str(value or "").split())
    return (result or fallback)[:80]


class NumberedProfileStore:
    def __init__(
        self,
        path: Path,
        *,
        expected_schema: str,
        validator: ProfileValidator,
    ) -> None:
        self.path = path
        self.expected_schema = expected_schema
        self.validator = validator

    def snapshot(self) -> dict[str, Any]:
        document = load_json_object(self.path)
        if document.get("schema") != self.expected_schema or document.get(
            "schema_version"
        ) != 1:
            raise ValueError(f"{self.path} has an unsupported profile schema")
        profiles = document.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            raise ValueError(f"{self.path} must contain at least one profile")
        numbers = [int(profile["profile_number"]) for profile in profiles]
        if any(number <= 0 for number in numbers) or len(numbers) != len(set(numbers)):
            raise ValueError(f"{self.path} has invalid profile numbers")
        default_number = int(document.get("default_profile_number", 0))
        if default_number not in numbers:
            raise ValueError(f"{self.path} default profile is unavailable")
        return document

    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = self.snapshot()
        used = {int(profile["profile_number"]) for profile in document["profiles"]}
        number = next(candidate for candidate in range(1, len(used) + 2) if candidate not in used)
        profile = self.validator(dict(payload))
        profile["profile_number"] = number
        profile["name"] = normalized_profile_name(
            profile.get("name"),
            f"Profile #{number}",
        )
        document["profiles"].append(profile)
        atomic_write_json(self.path, document)
        return profile

    def set_default(self, number: int) -> dict[str, Any]:
        document = self.snapshot()
        available = {int(profile["profile_number"]) for profile in document["profiles"]}
        if int(number) not in available:
            raise ValueError("requested default profile is unavailable")
        document["default_profile_number"] = int(number)
        atomic_write_json(self.path, document)
        return document

    def delete(self, number: int) -> dict[str, Any]:
        document = self.snapshot()
        number = int(number)
        if len(document["profiles"]) <= 1:
            raise ValueError("at least one profile must remain")
        if number == int(document["default_profile_number"]):
            raise ValueError("the default profile cannot be deleted")
        retained = [
            profile
            for profile in document["profiles"]
            if int(profile["profile_number"]) != number
        ]
        if len(retained) == len(document["profiles"]):
            raise ValueError("requested profile is unavailable")
        document["profiles"] = retained
        atomic_write_json(self.path, document)
        return document
