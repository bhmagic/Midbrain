# FoundationPose Native Known-Object Pose Provider

Release history is recorded in [CHANGELOG.md](CHANGELOG.md).

This is a Windows-native Resource Provider for one generic domain function:
`perception.known_object_pose.estimate`. It accepts immutable RGB, aligned
depth, binary mask, camera intrinsics, and geometry-only OBJ evidence and
returns `camera_from_centered_mesh`. It does not know robot semantics, choose
objects, segment images, resolve 90/180-degree meaning, track sessions, own
calibration policy, or activate transforms.

The native CUDA path is extracted from NVIDIA Isaac ROS FoundationPose rather
than running ROS, Docker, WSL, NVLabs Python rendering, or Linux compatibility
layers. TensorRT inference stays in this Provider's private Python 3.11
environment. The native library owns sampling, CUDA rendering, preprocessing,
pose transforms, and decoding; it invokes the Provider-owned TensorRT contexts
through a narrow callback on the same CUDA stream.

## Supported host

- Windows 11 x86-64.
- Visual Studio 2022 C++ Build Tools and CMake.
- CUDA Toolkit 12.8 or a compatible newer 12.x installation.
- NVIDIA GPU with at least 8 GB VRAM. The current setup defaults to CUDA
  architecture 120 and can be overridden with `-CudaArchitectures`.
- Python 3.11. TensorRT engines are machine/runtime-specific generated output.

Official NVIDIA guidance for the current ONNX integration is retained in the
setup: model version `1.0.1_onnx`, refine batch capacity 42, score batch
capacity 252, and FP32 engine generation for current TensorRT releases. The
generated engines and downloaded ONNX files remain untracked runtime data.

## Setup

```powershell
.\providers\foundation_pose\scripts\setup.ps1
```

Setup resolves Python and Visual Studio from the Windows installation,
creates `providers/foundation_pose/.venv`, builds the native DLL, downloads the
official NVIDIA NGC ONNX models when absent, builds FP32 TensorRT engines, and
runs Provider tests. It installs only the provider-neutral BufferRef consumer
from `contracts/python`; it does not install the camera, SAM2, or Skill package
and never modifies another component's environment.

The Provider is registered through
`config_templates/provider_entry.json`. Manager owns process residency. `WARM`
keeps the process but releases TensorRT/native state; `HOT` loads one resident
pipeline. Requests are forwarded through Manager and use the Provider's single
`estimate` action.

The Manager-guarded Provider development page resolves from the running
control endpoint at `/dev`. It shows residency, readiness, errors, timing, and
the exact CAD/evidence paths from the most recent generic estimate. Robot
profile editing and VLM evidence inspection remain in the calling Skill UI.

## Evidence and outputs

Large RGB-D inputs are copied into Skill-owned run artifacts before this
Provider is called. Every request validates image/depth/mask dimensions, finite
intrinsics and depth, mesh existence and hash, and the bounded hypothesis
count. The result includes measurement identity, timing, mesh/evidence hash
provenance, the raw camera-from-centered-mesh transform, and the score-network
output labeled `ranking_score_raw` with semantics
`RAW_MODEL_RANKING_ONLY`. The Provider uses this value only to rank hypotheses
inside one request. It defines no zero threshold or calibrated cross-request
confidence scale. The compatibility `quality.score` field currently mirrors
the same raw value. Robot-frame composition and independent acceptance policy
belong to the calling Skill.

## Validation boundary

The implementation has completed a clean native compile/probe and synthetic
resident inference on the development RTX 5070 Ti. After one warmup, five
full 640 x 480 runs with all 252 hypotheses measured 731, 731, 761, 842, and
734 ms wall time (760 ms mean). Reproduce this execution-only check with
`scripts/benchmark_synthetic.py`. It proves the Windows native/TensorRT path
executes; it is not a matched real-scene accuracy qualification and is not an
old-versus-new end-to-end comparison.

Before physical use, validate the exact camera, CAD scale/origin, lighting,
mask route, depth range, and model profile against measured ground truth. A
Provider measurement is never motion authority.

## Provenance and licenses

Midbrain-authored code is covered by the repository MIT license. Retained
NVIDIA Isaac ROS FoundationPose and nvdiffrast sources are Apache-2.0; their
license and exact commit are under `native/upstream`. Eigen 3.4.0 is MPL-2.0;
its license is under `native/third_party/eigen`. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
