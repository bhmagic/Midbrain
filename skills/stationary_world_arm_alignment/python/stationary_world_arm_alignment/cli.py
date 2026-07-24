from __future__ import annotations

import argparse
import asyncio
import json

from .models import PUBLIC_RUN_MODES, RunMode
from .skill import AlignmentSkill


async def _run(args: argparse.Namespace) -> int:
    skill = AlignmentSkill()
    try:
        result = await skill.run(
            RunMode(args.mode),
            arm_is_home=args.arm_is_home,
            allow_active_control_interrupt=args.allow_active_control_interrupt,
        )
        print(json.dumps(result, indent=2))
        return 0
    finally:
        await skill.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Stationary world-space arm finder")
    parser.add_argument(
        "--mode",
        choices=[value.value for value in PUBLIC_RUN_MODES],
        default=RunMode.AUTO.value,
    )
    parser.add_argument("--arm-is-home", action="store_true")
    parser.add_argument("--allow-active-control-interrupt", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
