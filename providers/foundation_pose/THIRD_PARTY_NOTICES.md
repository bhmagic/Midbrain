# Third-Party Notices

## NVIDIA Isaac ROS FoundationPose

- Source: `https://github.com/NVIDIA-ISAAC-ROS/isaac_ros_pose_estimation`
- Commit: `ab389aea93376402cfd9bb093dd3dd38d12574b6`
- Retained: FoundationPose implementation and CUDA nvdiffrast rasterizer.
- License: Apache License 2.0.
- Local license: `native/upstream/LICENSE.apache-2.0`.
- Local provenance and modifications: `native/upstream/UPSTREAM.json`.

ROS 2 nodes/messages, NITROS transport, ament/colcon logic, CV-CUDA, Assimp,
and OpenCV mesh loading are not included in the extracted build.

## Eigen

- Version: 3.4.0.
- Source: `https://gitlab.com/libeigen/eigen/-/tree/3.4.0`.
- License: Mozilla Public License 2.0 for the retained headers.
- Local license: `native/third_party/eigen/COPYING.MPL2`.

## NVIDIA NGC FoundationPose ONNX models

- Model version: `1.0.1_onnx`.
- Files: `refine_model.onnx` and `score_model.onnx`.
- Source: NVIDIA NGC, downloaded by `scripts/setup.ps1`.
- Distribution: not committed; users obtain the models directly from NVIDIA.

TensorRT and CUDA are NVIDIA runtime/toolkit dependencies and are not bundled
as source in this repository.
