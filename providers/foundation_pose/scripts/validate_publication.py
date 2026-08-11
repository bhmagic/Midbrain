"""Static publication checks for the FoundationPose Provider Git tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path


FORBIDDEN_DIRECTORY_NAMES = {
    ".venv",
    "nvlabs",
    "sam2",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "captures",
    "backups",
    "installations",
    "debug",
    "logs",
}

REQUIRED_DEFAULT_MODELS = {
    "robot_arm_root": {
        "role": "robot_base",
        "default_child_frame": "observed_object/rebot_b601_dm/base",
    },
    "robot_gripper_slider_support": {
        "role": "robot_gripper",
        "default_child_frame": "observed_object/rebot_b601_dm/gripper_slider_support",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


CANONICAL_TEXT_EXTENSIONS = {
    ".bat", ".c", ".cc", ".cfg", ".cmake", ".cmd", ".cpp", ".css",
    ".csv", ".env", ".example", ".gitattributes", ".gitignore", ".h",
    ".hpp", ".html", ".ini", ".js", ".json", ".jsx", ".lock", ".md",
    ".obj", ".ps1", ".py", ".rs", ".schema", ".sh", ".sha256", ".step",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
CANONICAL_TEXT_NAMES = {
    "cargo.lock", "cargo.toml", "cmakelists.txt", "dockerfile", "license",
    "notice", "version",
}


def manifest_sha256(path: Path) -> str:
    """Match the repository manifest generator's cross-platform text hashing."""

    if (
        path.suffix.lower() not in CANONICAL_TEXT_EXTENSIONS
        and path.name.lower() not in CANONICAL_TEXT_NAMES
    ):
        return sha256(path)
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        bom, payload, encoding = b"\xef\xbb\xbf", data[3:], "utf-8"
    elif data.startswith(b"\xff\xfe"):
        bom, payload, encoding = b"\xff\xfe", data[2:], "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        bom, payload, encoding = b"\xfe\xff", data[2:], "utf-16-be"
    else:
        bom, payload, encoding = b"", data, "utf-8"
    text = payload.decode(encoding, errors="replace").replace("\r\n", "\n")
    return hashlib.sha256(bom + text.encode(encoding)).hexdigest()



def validate_release_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
    if not version:
        raise RuntimeError("VERSION is empty")

    manifest = json.loads(
        (root / "manifest.json").read_text(encoding="utf-8-sig")
    )
    manifest_version = str(manifest.get("version", "")).strip()

    with (root / "python" / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    package_version = str(pyproject.get("project", {}).get("version", "")).strip()

    provider_source = (root / "provider.py").read_text(encoding="utf-8-sig")
    match = re.search(
        r'^PROVIDER_VERSION\s*=\s*"([^"]+)"\s*$',
        provider_source,
        flags=re.MULTILINE,
    )
    runtime_version = match.group(1).strip() if match else ""

    versions = {
        "VERSION": version,
        "manifest.json": manifest_version,
        "python/pyproject.toml": package_version,
        "provider.py": runtime_version,
    }

    mismatches = [
        f"{name}={value!r}"
        for name, value in versions.items()
        if value != version
    ]
    if mismatches:
        raise RuntimeError(
            "Release version mismatch; expected "
            f"{version!r}: " + ", ".join(mismatches)
        )

    return version

def validate_json_files(root: Path) -> int:
    count = 0
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {
            ".venv",
            "nvlabs",
            "sam2",
            "debug",
            "logs",
        }:
            continue
        json.loads(path.read_text(encoding="utf-8-sig"))
        count += 1
    return count


def validate_hygiene(root: Path, *, allow_runtime: bool, allow_generated: bool) -> None:
    violations: list[str] = []

    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if allow_runtime and relative.parts and relative.parts[0] in {
            ".venv",
            "nvlabs",
            "sam2",
            "debug",
            "logs",
        }:
            continue
        if path.is_dir() and path.name in FORBIDDEN_DIRECTORY_NAMES:
            if allow_generated and path.name in {"__pycache__", ".pytest_cache"}:
                continue
            violations.append(str(relative))
        if path.is_file() and path.suffix.lower() in {".pyc", ".pyo"}:
            if allow_generated:
                continue
            violations.append(str(relative))

    if violations:
        joined = "\n  - ".join(sorted(violations))
        raise RuntimeError(f"Publication hygiene violations:\n  - {joined}")


def validate_default_profile(root: Path) -> None:
    profile_root = root / "defaults" / "rebot_b601_dm"
    registry_path = profile_root / "models.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    models = {
        str(item["model_id"]): item
        for item in payload.get("models", [])
        if isinstance(item, dict) and item.get("model_id")
    }

    for model_id, expected in REQUIRED_DEFAULT_MODELS.items():
        if model_id not in models:
            raise RuntimeError(f"Default model is missing: {model_id}")
        model = models[model_id]
        if model.get("role") != expected["role"]:
            raise RuntimeError(f"Unexpected role for {model_id}: {model.get('role')}")
        if model.get("default_child_frame") != expected["default_child_frame"]:
            raise RuntimeError(
                f"Unexpected default_child_frame for {model_id}: "
                f"{model.get('default_child_frame')}"
            )
        mesh_path = profile_root / str(model["mesh_path"])
        if not mesh_path.is_file():
            raise RuntimeError(f"Default mesh is missing: {mesh_path}")

    required_source = [
        profile_root / "source" / "01_BASE_Plate.step",
        profile_root / "source" / "01_BASE_Link.step",
        profile_root / "source" / "01_Rail_Bracket.step",
        profile_root / "licenses" / "CERN-OHL-W-2.0.txt",
        profile_root / "UPSTREAM.md",
        profile_root / "MODIFICATIONS.md",
    ]
    for path in required_source:
        if not path.is_file():
            raise RuntimeError(f"Default profile provenance/source file is missing: {path}")


def validate_default_manifest(root: Path) -> None:
    profile_root = root / "defaults" / "rebot_b601_dm"
    manifest_path = profile_root / "FILE_MANIFEST.sha256"
    if not manifest_path.is_file():
        raise RuntimeError("Default profile FILE_MANIFEST.sha256 is missing")

    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        expected, relative = line.split("  ", 1)
        path = profile_root / relative
        if not path.is_file():
            raise RuntimeError(f"Manifest file is missing: {relative}")
        actual = manifest_sha256(path)
        if actual.lower() != expected.lower():
            raise RuntimeError(
                f"Manifest checksum mismatch for {relative}: {actual} != {expected}"
            )




def validate_weight_helper_files(root: Path) -> None:
    helper = root / "python" / "foundation_pose_provider" / "weights.py"
    tests = root / "python" / "tests" / "test_weights.py"
    if not helper.is_file() or not tests.is_file():
        raise RuntimeError("Weight-install helper/test files are missing")

    bundled_root = (
        root
        / "third_party"
        / "nvlabs_foundationpose_weights"
        / "weights"
    )

    expected = {
        "2023-10-28-18-33-37/config.yml": (
            708,
            "28a6ba94a33230ee5fc3c51939486281578b0972542bd9e38ca6123e75605686",
        ),
        "2023-10-28-18-33-37/model_best.pth": (
            68220109,
            "774700586ddc435d408fc01c9809c43e151232936369dfbea0f0f964ba471d60",
        ),
        "2024-01-11-20-02-45/config.yml": (
            778,
            "a79db4de3b95885dd5ae86833b37b8698a75dad81e87d1086cd50b2fcd8dda3f",
        ),
        "2024-01-11-20-02-45/model_best.pth": (
            190229389,
            "81924d384bf5c26c646ee4783104982ae3d1e049c181c36641b6a7aeae494c26",
        ),
    }

    for relative, (expected_size, expected_sha) in expected.items():
        path = bundled_root / relative
        if not path.is_file():
            raise RuntimeError(f"Bundled checkpoint file is missing: {path}")
        if path.stat().st_size != expected_size:
            raise RuntimeError(
                f"Bundled checkpoint size mismatch: {relative}"
            )
        if sha256(path) != expected_sha:
            raise RuntimeError(
                f"Bundled checkpoint SHA-256 mismatch: {relative}"
            )

    provenance = root / "third_party" / "nvlabs_foundationpose_weights" / "README.md"
    license_path = (
        root
        / "third_party"
        / "nvlabs_foundationpose_weights"
        / "NVIDIA_SOURCE_CODE_LICENSE.txt"
    )
    digest_manifest = (
        root
        / "third_party"
        / "nvlabs_foundationpose_weights"
        / "WEIGHTS_MANIFEST.sha256"
    )
    if not provenance.is_file() or not digest_manifest.is_file() or not license_path.is_file():
        raise RuntimeError("Bundled checkpoint provenance or license files are missing")
    license_text = license_path.read_text(encoding="utf-8")
    required_license_phrases = (
        "NVIDIA Corporation & affiliates",
        "3.1 Redistribution",
        "3.3 Use Limitation",
        "non-commercially",
    )
    if any(phrase not in license_text for phrase in required_license_phrases):
        raise RuntimeError("Bundled NVIDIA license text is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-runtime",
        action="store_true",
        help="Allow Provider-local .venv, nvlabs, and sam2 directories in an installed tree.",
    )
    parser.add_argument(
        "--allow-generated",
        action="store_true",
        help="Allow Python/pytest caches created while validating an installed tree.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]

    version = validate_release_version(root)

    validate_hygiene(
        root,
        allow_runtime=args.allow_runtime,
        allow_generated=args.allow_generated,
    )
    json_count = validate_json_files(root)
    validate_default_profile(root)
    validate_default_manifest(root)
    validate_weight_helper_files(root)

    print(f"Provider version: {version}")
    print("Release version consistency: PASS")
    print(f"JSON files parsed: {json_count}")
    print("Publication hygiene: PASS")
    print("Default reBot profile: PASS")
    print("Default profile integrity manifest: PASS")
    print("Bundled checkpoint integrity/provenance: PASS")
    print("Static publication validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
