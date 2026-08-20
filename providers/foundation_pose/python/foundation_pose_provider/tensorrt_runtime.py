from __future__ import annotations

from pathlib import Path
from typing import Any


class TensorRtPair:
    """Own two TensorRT contexts and execute against caller-owned CUDA memory."""

    def __init__(self, refine_engine: Path, score_engine: Path) -> None:
        try:
            import tensorrt as trt
        except ImportError as exc:
            raise RuntimeError("TensorRT Python runtime is not installed") from exc
        self.trt = trt
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.runtime = trt.Runtime(self.logger)
        self.engines: dict[int, Any] = {}
        self.contexts: dict[int, Any] = {}
        for kind, path in ((1, refine_engine), (2, score_engine)):
            data = Path(path).read_bytes()
            engine = self.runtime.deserialize_cuda_engine(data)
            if engine is None:
                raise RuntimeError(f"TensorRT could not deserialize {path}")
            context = engine.create_execution_context()
            if context is None:
                raise RuntimeError(f"TensorRT could not create an execution context for {path}")
            self._validate_bindings(kind, engine)
            self.engines[kind] = engine
            self.contexts[kind] = context

    def _validate_bindings(self, kind: int, engine: Any) -> None:
        names = {engine.get_tensor_name(index) for index in range(engine.num_io_tensors)}
        required = {"input1", "input2", "output1"}
        if kind == 1:
            required.add("output2")
        missing = required - names
        if missing:
            raise RuntimeError(f"TensorRT engine is missing bindings: {sorted(missing)}")

    def execute(
        self,
        kind: int,
        batch: int,
        height: int,
        width: int,
        channels: int,
        rendered_device: int,
        observed_device: int,
        primary_device: int,
        secondary_device: int,
        cuda_stream: int,
    ) -> None:
        context = self.contexts[kind]
        shape = (batch, height, width, channels)
        if not context.set_input_shape("input1", shape):
            raise RuntimeError(f"TensorRT rejected input1 shape {shape}")
        if not context.set_input_shape("input2", shape):
            raise RuntimeError(f"TensorRT rejected input2 shape {shape}")
        context.set_tensor_address("input1", rendered_device)
        context.set_tensor_address("input2", observed_device)
        context.set_tensor_address("output1", primary_device)
        if kind == 1:
            context.set_tensor_address("output2", secondary_device)
        if not context.execute_async_v3(cuda_stream):
            raise RuntimeError("TensorRT execute_async_v3 returned false")

