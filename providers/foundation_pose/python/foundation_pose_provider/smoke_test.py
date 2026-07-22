"""Native backend smoke test used before starting the Midbrain Provider."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any


def run(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"FoundationPose repository not found: {root}")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    result: dict[str, Any] = {"foundationpose_root": root_text}
    torch = importlib.import_module("torch")
    result["torch_version"] = str(torch.__version__)
    result["cuda_available"] = bool(torch.cuda.is_available())
    result["cuda_version"] = str(torch.version.cuda)
    if not result["cuda_available"]:
        raise RuntimeError("PyTorch CUDA is not available")
    result["gpu_name"] = str(torch.cuda.get_device_name(0))
    importlib.import_module("trimesh")
    importlib.import_module("cv2")
    dr = importlib.import_module("nvdiffrast.torch")
    estimater = importlib.import_module("estimater")
    for symbol in ("FoundationPose", "ScorePredictor", "PoseRefinePredictor"):
        if not hasattr(estimater, symbol):
            raise RuntimeError(f"estimater module does not expose {symbol}")
    context = dr.RasterizeCudaContext()
    result["raster_context"] = type(context).__name__
    result["status"] = "PASS"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--foundationpose-root", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(Path(args.foundationpose_root)), indent=2))
        return 0
    except Exception as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
