from __future__ import annotations

import argparse
from pathlib import Path


def build_engine(onnx_path: Path, output_path: Path, maximum_batch: int) -> None:
    import tensorrt as trt

    logger = trt.Logger(trt.Logger.WARNING)
    builder = trt.Builder(logger)
    network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    parser = trt.OnnxParser(network, logger)
    if not parser.parse(onnx_path.read_bytes()):
        messages = [str(parser.get_error(index)) for index in range(parser.num_errors)]
        raise RuntimeError("TensorRT ONNX parse failed:\n" + "\n".join(messages))
    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 8 * 1024**3)
    profile = builder.create_optimization_profile()
    for input_name in ("input1", "input2"):
        profile.set_shape(
            input_name,
            (1, 160, 160, 6),
            (1, 160, 160, 6),
            (maximum_batch, 160, 160, 6),
        )
    config.add_optimization_profile(profile)
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError(f"TensorRT failed to build {onnx_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bytes(serialized))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--engine-dir", type=Path, required=True)
    args = parser.parse_args()
    build_engine(args.model_dir / "refine_model.onnx", args.engine_dir / "refine.plan", 42)
    build_engine(args.model_dir / "score_model.onnx", args.engine_dir / "score.plan", 252)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

