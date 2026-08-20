from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .skill import LocateArmBaseSkill


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate a profiled robot arm base")
    parser.add_argument("--config", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    root = Path(os.environ.get("PHYSICAL_AGENT_ROOT") or Path(__file__).resolve().parents[4])
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    skill = LocateArmBaseSkill(config, root)
    try:
        print(json.dumps(skill.run(request), indent=2, ensure_ascii=False))
    finally:
        skill.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

