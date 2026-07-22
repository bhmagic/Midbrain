from __future__ import annotations

import argparse
import json
from pathlib import Path

import trimesh


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a candidate rigid mesh before FoundationPose preparation."
    )
    parser.add_argument("mesh")
    parser.add_argument("--scale-to-m", type=float, default=1.0)
    args = parser.parse_args()

    path = Path(args.mesh).expanduser().resolve()
    mesh = trimesh.load(str(path), force="mesh", process=False)

    if getattr(mesh, "vertices", None) is None or len(mesh.vertices) == 0:
        raise RuntimeError(f"Mesh has no vertices: {path}")

    bounds = mesh.bounds
    extents = mesh.extents

    report = {
        "mesh": str(path),
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "bounds_input_units": bounds.tolist(),
        "extents_input_units": extents.tolist(),
        "scale_to_m": float(args.scale_to_m),
        "extents_m": (extents * args.scale_to_m).tolist(),
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
