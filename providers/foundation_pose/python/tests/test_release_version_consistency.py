from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


def test_release_version_surfaces_match() -> None:
    provider_root = Path(__file__).resolve().parents[2]

    version = (provider_root / "VERSION").read_text(
        encoding="utf-8-sig"
    ).strip()

    manifest = json.loads(
        (provider_root / "manifest.json").read_text(
            encoding="utf-8-sig"
        )
    )

    with (provider_root / "python" / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    provider_source = (provider_root / "provider.py").read_text(
        encoding="utf-8-sig"
    )
    match = re.search(
        r'^PROVIDER_VERSION\s*=\s*["\']([^"\']+)["\']\s*$',
        provider_source,
        flags=re.MULTILINE,
    )

    assert match is not None
    assert manifest["version"] == version
    assert pyproject["project"]["version"] == version
    assert match.group(1) == version
