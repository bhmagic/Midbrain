from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np

from foundation_pose_provider.backend import EstimateInput, NativeFoundationPoseBackend


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a synthetic execution benchmark of the native FoundationPose path."
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0:
        raise ValueError("runs must be positive and warmup cannot be negative")

    provider_root = Path(__file__).resolve().parents[1]
    workspace_root = provider_root.parents[1]
    config = json.loads(
        (provider_root / "config_templates" / "provider.default.json").read_text(
            encoding="utf-8"
        )
    )
    mesh_path = (
        workspace_root
        / "skills"
        / "locate_arm_base"
        / "assets"
        / "rebot_b601_dm"
        / "models"
        / "Base_clean_centered.obj"
    )
    width, height = int(args.width), int(args.height)
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[..., 1] = 96
    depth_m = np.full((height, width), 0.7, dtype=np.float32)
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4] = 1
    focal = float(max(width, height))
    inputs = EstimateInput(
        rgb=rgb,
        depth_m=depth_m,
        mask=mask,
        intrinsics=(
            focal,
            0.0,
            (width - 1) / 2.0,
            0.0,
            focal,
            (height - 1) / 2.0,
            0.0,
            0.0,
            1.0,
        ),
        mesh_path=mesh_path,
        mesh_scale_to_m=1.0,
    )

    backend = NativeFoundationPoseBackend(config, workspace_root)
    measurements: list[dict[str, float | int]] = []
    try:
        for index in range(args.warmup + args.runs):
            started = time.perf_counter()
            result = backend.estimate(inputs)
            wall_ms = (time.perf_counter() - started) * 1000.0
            if index >= args.warmup:
                measurements.append(
                    {
                        "wall_ms": round(wall_ms, 3),
                        "native_elapsed_ms": round(result.elapsed_ms, 3),
                        "score": round(result.score, 6),
                        "hypothesis_count": result.hypothesis_count,
                    }
                )
    finally:
        backend.close()

    wall_values = [float(item["wall_ms"]) for item in measurements]
    print(
        json.dumps(
            {
                "benchmark": "SYNTHETIC_EXECUTION_ONLY",
                "qualification": "NOT_REAL_SCENE_ACCURACY_OR_OLD_NEW_COMPARISON",
                "warmup_runs": args.warmup,
                "measured_runs": args.runs,
                "image_size": [width, height],
                "measurements": measurements,
                "mean_wall_ms": round(sum(wall_values) / len(wall_values), 3),
                "minimum_wall_ms": round(min(wall_values), 3),
                "maximum_wall_ms": round(max(wall_values), 3),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
