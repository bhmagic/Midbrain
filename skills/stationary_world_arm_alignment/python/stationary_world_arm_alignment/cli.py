from __future__ import annotations

import argparse
import asyncio
import json

from .models import PUBLIC_RUN_MODES, RunMode
from .skill import AlignmentSkill
from .vlm import OPENAI_API_ROUTE, REVIEWED_FILE_ROUTE


async def _run(args: argparse.Namespace) -> int:
    skill = AlignmentSkill()
    try:
        result = await skill.run(
            RunMode(args.mode),
            arm_is_home=args.arm_is_home,
            allow_active_control_interrupt=args.allow_active_control_interrupt,
            vision_route=args.vision_route,
            review_timeout_s=args.review_timeout_s,
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
    parser.add_argument(
        "--vision-route",
        choices=[OPENAI_API_ROUTE, REVIEWED_FILE_ROUTE],
        default=OPENAI_API_ROUTE,
    )
    parser.add_argument(
        "--review-timeout-s",
        type=float,
        default=300.0,
        help="Bounded wait for each REVIEWED_FILE response.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
