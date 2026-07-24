from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import cv2

from stationary_world_arm_alignment.config import Settings, WORKSPACE_ROOT
from stationary_world_arm_alignment.vlm import GripperVision


async def diagnose(image_path: Path) -> None:
    bgr = cv2.imread(str(image_path))
    if bgr is None:
        raise RuntimeError(f"could not read image: {image_path}")
    image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    settings = Settings()
    vision = GripperVision(
        settings.openai_api_key,
        settings.openai_vision_model,
        WORKSPACE_ROOT,
    )
    try:
        result = await vision.locate(image, require_base=True)
        print(json.dumps(result, indent=2))
    finally:
        await vision.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    asyncio.run(diagnose(args.image.resolve()))


if __name__ == "__main__":
    main()
