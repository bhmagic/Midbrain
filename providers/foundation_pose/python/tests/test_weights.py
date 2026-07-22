from __future__ import annotations

from pathlib import Path

from foundation_pose_provider.weights import (
    DriveEntry,
    REQUIRED_WEIGHT_SETS,
    find_required_drive_entries,
    missing_weight_files,
    weight_set_is_valid,
)


def _write_valid_set(root: Path, weight_set: str) -> None:
    directory = root / weight_set
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yml").write_text(
        "model:\n  name: foundationpose\n" * 4,
        encoding="utf-8",
    )
    with (directory / "model_best.pth").open("wb") as handle:
        handle.truncate(1024 * 1024 + 1)


def test_find_required_drive_entries_matches_nested_paths() -> None:
    entries = [
        DriveEntry(
            url=f"https://drive.example/{weight_set}/config",
            path=f"no_diffusion/{weight_set}/config.yml",
        )
        for weight_set in REQUIRED_WEIGHT_SETS
    ] + [
        DriveEntry(
            url=f"https://drive.example/{weight_set}/model",
            path=f"no_diffusion/{weight_set}/model_best.pth",
        )
        for weight_set in REQUIRED_WEIGHT_SETS
    ]

    selected = find_required_drive_entries(entries)

    assert len(selected) == 4
    for weight_set in REQUIRED_WEIGHT_SETS:
        assert selected[(weight_set, "config.yml")].path.endswith(
            f"{weight_set}/config.yml"
        )
        assert selected[(weight_set, "model_best.pth")].path.endswith(
            f"{weight_set}/model_best.pth"
        )


def test_weight_set_validation_rejects_missing_or_tiny_files(tmp_path: Path) -> None:
    weight_set = REQUIRED_WEIGHT_SETS[0]
    directory = tmp_path / weight_set
    directory.mkdir(parents=True)

    (directory / "config.yml").write_text("x", encoding="utf-8")
    (directory / "model_best.pth").write_bytes(b"tiny")

    assert not weight_set_is_valid(tmp_path, weight_set)
    assert set(missing_weight_files(tmp_path, weight_set)) == {
        "config.yml",
        "model_best.pth",
    }


def test_weight_set_validation_accepts_bundled_files() -> None:
    root = Path(__file__).resolve().parents[2]
    bundled = root / "third_party" / "nvlabs_foundationpose_weights" / "weights"
    weight_set = REQUIRED_WEIGHT_SETS[1]

    assert weight_set_is_valid(bundled, weight_set)
    assert missing_weight_files(bundled, weight_set) == []


def test_release_expected_hashes_are_declared() -> None:
    from foundation_pose_provider.weights import EXPECTED_FILES

    assert EXPECTED_FILES["2023-10-28-18-33-37"]["model_best.pth"]["size"] == 68220109
    assert (
        EXPECTED_FILES["2024-01-11-20-02-45"]["model_best.pth"]["sha256"]
        == "81924d384bf5c26c646ee4783104982ae3d1e049c181c36641b6a7aeae494c26"
    )


def test_bundled_weight_directory_shape_is_publication_relative() -> None:
    root = Path(__file__).resolve().parents[2]
    bundled = root / "third_party" / "nvlabs_foundationpose_weights" / "weights"

    assert (bundled / "2023-10-28-18-33-37" / "config.yml").is_file()
    assert (bundled / "2023-10-28-18-33-37" / "model_best.pth").is_file()
    assert (bundled / "2024-01-11-20-02-45" / "config.yml").is_file()
    assert (bundled / "2024-01-11-20-02-45" / "model_best.pth").is_file()
