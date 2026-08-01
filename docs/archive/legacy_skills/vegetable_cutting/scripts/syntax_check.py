from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    root = Path(sys.argv[1])
    for source in root.rglob("*.py"):
        compile(source.read_text(encoding="utf-8"), str(source), "exec")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
