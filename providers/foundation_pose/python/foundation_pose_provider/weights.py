"""FoundationPose checkpoint verification and installation.

Release packages may carry the two official NVLabs model-based checkpoint sets
under ``third_party/nvlabs_foundationpose_weights/weights``. Installers use the
bundled copy first, populate the persistent Midbrain install cache, and never
need network access for the packaged release path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


OFFICIAL_WEIGHTS_FOLDER_URL = (
    "https://drive.google.com/drive/folders/"
    "1DFezOAD0oD1BblsXVxqDsl8fj0qzB82i?usp=sharing"
)

REQUIRED_WEIGHT_SETS = (
    "2023-10-28-18-33-37",
    "2024-01-11-20-02-45",
)

REQUIRED_FILES = (
    "config.yml",
    "model_best.pth",
)

EXPECTED_FILES = {
    "2023-10-28-18-33-37": {
        "config.yml": {
            "size": 708,
            "sha256": "28a6ba94a33230ee5fc3c51939486281578b0972542bd9e38ca6123e75605686",
        },
        "model_best.pth": {
            "size": 68220109,
            "sha256": "774700586ddc435d408fc01c9809c43e151232936369dfbea0f0f964ba471d60",
        },
    },
    "2024-01-11-20-02-45": {
        "config.yml": {
            "size": 778,
            "sha256": "a79db4de3b95885dd5ae86833b37b8698a75dad81e87d1086cd50b2fcd8dda3f",
        },
        "model_best.pth": {
            "size": 190229389,
            "sha256": "81924d384bf5c26c646ee4783104982ae3d1e049c181c36641b6a7aeae494c26",
        },
    },
}


@dataclass(frozen=True)
class DriveEntry:
    """One file discovered by gdown's folder JSON listing."""

    url: str
    path: str


def sha256(path: Path) -> str:
    """Return a file SHA-256 without loading a checkpoint into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_drive_path(value: str) -> str:
    """Normalize a Google Drive listing path for deterministic suffix matching."""
    return value.replace("\\", "/").strip("/")


def missing_weight_files(root: Path, weight_set: str) -> list[str]:
    """Return files that fail the release size/hash contract."""
    directory = root / weight_set
    missing: list[str] = []

    for filename in REQUIRED_FILES:
        path = directory / filename
        expected = EXPECTED_FILES[weight_set][filename]

        if not path.is_file():
            missing.append(filename)
            continue

        try:
            if path.stat().st_size != int(expected["size"]):
                missing.append(filename)
                continue
        except OSError:
            missing.append(filename)
            continue

        if sha256(path) != str(expected["sha256"]):
            missing.append(filename)

    return missing


def weight_set_is_valid(root: Path, weight_set: str) -> bool:
    """Return whether one required checkpoint set exactly matches the bundle."""
    return not missing_weight_files(root, weight_set)


def all_weight_sets_are_valid(root: Path) -> bool:
    """Return whether all required checkpoint sets exactly match expected hashes."""
    return all(weight_set_is_valid(root, item) for item in REQUIRED_WEIGHT_SETS)


def find_required_drive_entries(
    entries: Iterable[DriveEntry],
    weight_sets: Iterable[str] = REQUIRED_WEIGHT_SETS,
) -> dict[tuple[str, str], DriveEntry]:
    """Select required files from a recursive gdown folder listing."""
    selected: dict[tuple[str, str], DriveEntry] = {}
    normalized = [
        DriveEntry(url=item.url, path=normalize_drive_path(item.path))
        for item in entries
    ]

    for weight_set in weight_sets:
        for filename in REQUIRED_FILES:
            suffix = f"{weight_set}/{filename}"
            matches = [
                item
                for item in normalized
                if item.path == suffix or item.path.endswith("/" + suffix)
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "Official weight listing did not contain exactly one "
                    f"{suffix!r}; found {len(matches)}."
                )
            selected[(weight_set, filename)] = matches[0]

    return selected


def _replace_weight_set(
    source_root: Path,
    destination_root: Path,
    weight_set: str,
) -> None:
    """Compatibility wrapper around the validated copy operation."""
    _copy_valid_set(source_root, destination_root, weight_set)

def _copy_valid_set(source_root: Path, destination_root: Path, weight_set: str) -> None:
    """Copy one exact checkpoint set and verify the destination."""
    if not weight_set_is_valid(source_root, weight_set):
        invalid = ", ".join(missing_weight_files(source_root, weight_set))
        raise RuntimeError(
            f"Source weight set {weight_set} is invalid: {invalid}"
        )

    destination = destination_root / weight_set
    temporary = destination_root / f".{weight_set}.tmp-{uuid.uuid4().hex}"
    destination_root.mkdir(parents=True, exist_ok=True)

    if temporary.exists():
        shutil.rmtree(temporary, ignore_errors=True)

    temporary.mkdir(parents=True)
    try:
        for filename in REQUIRED_FILES:
            shutil.copy2(
                source_root / weight_set / filename,
                temporary / filename,
            )

        for filename in REQUIRED_FILES:
            copied = temporary / filename
            expected = EXPECTED_FILES[weight_set][filename]
            if copied.stat().st_size != expected["size"]:
                raise RuntimeError(f"Copied size mismatch: {copied}")
            if sha256(copied) != expected["sha256"]:
                raise RuntimeError(f"Copied SHA-256 mismatch: {copied}")

        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)

    if not weight_set_is_valid(destination_root, weight_set):
        raise RuntimeError(f"Destination weight set failed validation: {weight_set}")


def _run_gdown(arguments: list[str], *, attempts: int, label: str) -> subprocess.CompletedProcess[str]:
    """Run gdown with bounded retry for non-packaged development recovery."""
    last: subprocess.CompletedProcess[str] | None = None
    delays = (5, 10, 20, 40, 60)

    for attempt in range(1, attempts + 1):
        command = [sys.executable, "-m", "gdown", *arguments]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        last = result

        if result.returncode == 0:
            return result

        detail = (result.stderr or result.stdout or "").strip()
        print(
            f"[WEIGHTS] {label} attempt {attempt}/{attempts} failed "
            f"(exit {result.returncode}).",
            flush=True,
        )
        if detail:
            print(detail[-4000:], flush=True)

        if attempt < attempts:
            delay = delays[min(attempt - 1, len(delays) - 1)]
            time.sleep(delay)

    assert last is not None
    raise RuntimeError(f"{label} failed after {attempts} attempts.")


def _parse_drive_listing(text: str) -> list[DriveEntry]:
    """Parse gdown --folder --json output."""
    stripped = text.strip()
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError("gdown folder listing did not contain a JSON array.")

    payload = json.loads(stripped[start : end + 1])
    entries: list[DriveEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        path = str(item.get("path", "")).strip()
        if url and path:
            entries.append(DriveEntry(url=url, path=path))
    return entries


def _download_missing_sets(runtime_root: Path, missing_sets: list[str]) -> None:
    """Optional development-only network fallback."""
    listing = _run_gdown(
        [OFFICIAL_WEIGHTS_FOLDER_URL, "--folder", "--json", "--quiet"],
        attempts=3,
        label="Listing official FoundationPose weight folder",
    )
    entries = _parse_drive_listing(listing.stdout)
    selected = find_required_drive_entries(entries, missing_sets)

    with tempfile.TemporaryDirectory(
        prefix="foundationpose-weight-stage-"
    ) as temporary_directory:
        stage_root = Path(temporary_directory)

        for weight_set in missing_sets:
            for filename in REQUIRED_FILES:
                entry = selected[(weight_set, filename)]
                destination = stage_root / weight_set / filename
                destination.parent.mkdir(parents=True, exist_ok=True)

                _run_gdown(
                    [entry.url, "-O", str(destination), "--continue", "--quiet"],
                    attempts=3,
                    label=f"Downloading {weight_set}/{filename}",
                )

            if not weight_set_is_valid(stage_root, weight_set):
                invalid = ", ".join(missing_weight_files(stage_root, weight_set))
                raise RuntimeError(
                    f"Downloaded weight set {weight_set} is invalid: {invalid}"
                )

            _copy_valid_set(stage_root, runtime_root, weight_set)


def ensure_weights(
    *,
    foundationpose_root: Path,
    persistent_config_root: Path,
    bundled_weights_root: Path | None,
    allow_download: bool,
) -> None:
    """Ensure runtime and persistent cache have both exact checkpoint sets."""
    foundationpose_root = foundationpose_root.resolve()
    persistent_config_root = persistent_config_root.resolve()

    runtime_root = foundationpose_root / "weights"
    cache_root = (
        persistent_config_root
        / "install_cache"
        / "nvlabs"
        / "FoundationPose"
        / "weights"
    )

    runtime_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    bundled_root = bundled_weights_root.resolve() if bundled_weights_root else None

    if bundled_root is not None:
        for weight_set in REQUIRED_WEIGHT_SETS:
            if not weight_set_is_valid(bundled_root, weight_set):
                invalid = ", ".join(missing_weight_files(bundled_root, weight_set))
                raise RuntimeError(
                    f"Bundled weight set {weight_set} failed validation: {invalid}"
                )
        print("[WEIGHTS] Bundled checkpoint payload: PASS", flush=True)

    # Preserve a known-good runtime into the persistent cache.
    for weight_set in REQUIRED_WEIGHT_SETS:
        if weight_set_is_valid(runtime_root, weight_set):
            if not weight_set_is_valid(cache_root, weight_set):
                _copy_valid_set(runtime_root, cache_root, weight_set)
                print(f"[WEIGHTS] Cached runtime set {weight_set}.", flush=True)

    # Restore from persistent cache.
    for weight_set in REQUIRED_WEIGHT_SETS:
        if not weight_set_is_valid(runtime_root, weight_set):
            if weight_set_is_valid(cache_root, weight_set):
                _copy_valid_set(cache_root, runtime_root, weight_set)
                print(
                    f"[WEIGHTS] Restored {weight_set} from persistent cache.",
                    flush=True,
                )

    # Restore from packaged payload and populate cache.
    if bundled_root is not None:
        for weight_set in REQUIRED_WEIGHT_SETS:
            if not weight_set_is_valid(runtime_root, weight_set):
                _copy_valid_set(bundled_root, runtime_root, weight_set)
                print(
                    f"[WEIGHTS] Installed bundled set {weight_set}.",
                    flush=True,
                )
            if not weight_set_is_valid(cache_root, weight_set):
                _copy_valid_set(bundled_root, cache_root, weight_set)
                print(
                    f"[WEIGHTS] Cached bundled set {weight_set}.",
                    flush=True,
                )

    missing_sets = [
        item
        for item in REQUIRED_WEIGHT_SETS
        if not weight_set_is_valid(runtime_root, item)
    ]

    if missing_sets and allow_download:
        print(
            "[WEIGHTS] Packaged/cache recovery incomplete; using official "
            "network fallback for: " + ", ".join(missing_sets),
            flush=True,
        )
        _download_missing_sets(runtime_root, missing_sets)

    missing_sets = [
        item
        for item in REQUIRED_WEIGHT_SETS
        if not weight_set_is_valid(runtime_root, item)
    ]
    if missing_sets:
        raise RuntimeError(
            "Required FoundationPose checkpoint sets remain missing/invalid: "
            + ", ".join(missing_sets)
        )

    for weight_set in REQUIRED_WEIGHT_SETS:
        if not weight_set_is_valid(cache_root, weight_set):
            _copy_valid_set(runtime_root, cache_root, weight_set)

    print("[WEIGHTS] Required FoundationPose weights are valid.", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify/install required FoundationPose checkpoints."
    )
    parser.add_argument(
        "--foundationpose-root",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--persistent-config-root",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--bundled-weights-root",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Disable network fallback. Release installers use this mode.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    ensure_weights(
        foundationpose_root=args.foundationpose_root,
        persistent_config_root=args.persistent_config_root,
        bundled_weights_root=args.bundled_weights_root,
        allow_download=not args.offline,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
