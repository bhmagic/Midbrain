"""Compatibility helpers for the pinned NVLabs FoundationPose checkout."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


PATCH_MARKER = "MIDBRAIN_WINDOWS_TEMP_COMPAT_V1"
UPSTREAM_MESH_PATH_LINE = "      self.mesh_path = f'/tmp/{uuid.uuid4()}.obj'"
PATCHED_MESH_PATH_BLOCK = "\n".join(
    [
        f"      # {PATCH_MARKER}",
        "      temp_dir = os.environ.get('FOUNDATIONPOSE_TEMP_DIR') or tempfile.gettempdir()",
        "      os.makedirs(temp_dir, exist_ok=True)",
        "      self.mesh_path = os.path.join(temp_dir, f'{uuid.uuid4()}.obj')",
    ]
)


@dataclass(frozen=True)
class PatchResult:
    """Result of checking or applying the Windows temp-path patch."""

    estimater_path: Path
    status: str


def _estimater_path(foundationpose_root: Path) -> Path:
    root = foundationpose_root.resolve()
    path = root / "estimater.py"
    if not path.is_file():
        raise FileNotFoundError(f"NVLabs estimater.py was not found: {path}")
    return path


def is_windows_temp_patch_applied(foundationpose_root: Path) -> bool:
    """Return True when the pinned checkout contains our Windows temp fix."""

    path = _estimater_path(foundationpose_root)
    text = path.read_text(encoding="utf-8-sig")
    return PATCH_MARKER in text and "tempfile.gettempdir()" in text


def patch_windows_temp_path(foundationpose_root: Path) -> PatchResult:
    """Replace upstream's Linux-only /tmp mesh export with a portable temp path.

    The pinned upstream FoundationPose reset_object() writes a temporary centered
    mesh to '/tmp/<uuid>.obj'. On native Windows, that path resolves to a drive-root
    directory such as C:\\tmp and fails when that directory does not exist.

    This patch keeps the upstream behavior (a unique temporary OBJ) while choosing
    FOUNDATIONPOSE_TEMP_DIR when explicitly configured, otherwise Python's normal
    per-user temporary directory.
    """

    path = _estimater_path(foundationpose_root)
    text = path.read_text(encoding="utf-8-sig")

    if PATCH_MARKER in text:
        if "tempfile.gettempdir()" not in text:
            raise RuntimeError(
                f"{path} contains the Midbrain patch marker but not the expected temp-path code"
            )
        return PatchResult(estimater_path=path, status="already_patched")

    if UPSTREAM_MESH_PATH_LINE not in text:
        raise RuntimeError(
            "Pinned NVLabs estimater.py no longer contains the expected upstream "
            "'/tmp/<uuid>.obj' line; refusing to apply an unverified patch."
        )

    if "import yaml" not in text:
        raise RuntimeError(
            "Pinned NVLabs estimater.py no longer contains the expected 'import yaml' line; "
            "refusing to apply an unverified patch."
        )

    imports = "import yaml\nimport os\nimport tempfile"
    text = text.replace("import yaml", imports, 1)
    text = text.replace(UPSTREAM_MESH_PATH_LINE, PATCHED_MESH_PATH_BLOCK, 1)
    path.write_text(text, encoding="utf-8")

    if not is_windows_temp_patch_applied(foundationpose_root):
        raise RuntimeError(f"Windows temp compatibility patch verification failed: {path}")

    return PatchResult(estimater_path=path, status="patched")


def verify_windows_temp_path(foundationpose_root: Path) -> PatchResult:
    """Fail clearly when the pinned NVLabs checkout is missing the Windows patch."""

    path = _estimater_path(foundationpose_root)
    if not is_windows_temp_patch_applied(foundationpose_root):
        raise RuntimeError(
            "NVLabs FoundationPose Windows temp compatibility patch is missing. "
            "Run the matching release fast update or clean reinstall script before starting the Provider."
        )
    return PatchResult(estimater_path=path, status="verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--foundationpose-root", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    if args.verify_only:
        result = verify_windows_temp_path(args.foundationpose_root)
    else:
        result = patch_windows_temp_path(args.foundationpose_root)

    print(f"status={result.status}")
    print(f"estimater={result.estimater_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
