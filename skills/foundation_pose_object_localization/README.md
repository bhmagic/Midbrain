# FoundationPose Object Localization

FoundationPose is a finite, high-latency perception Skill in the Midbrain
architecture. A caller supplies synchronized RGB-D evidence, camera
intrinsics, a known model ID, and an explicit initialization region. The Skill
owns registration/tracking sessions for one bounded parent operation and
releases them when that operation ends.

The first production caller is Stationary World-Space Arm Finder. Its
`FOUNDATIONPOSE_SKILL` route owns masks, sampling, VIO-epoch checks, result
validation, and the bounded job lifetime. By default, the NVIDIA computation
runs in the FoundationPose Provider process because that isolated environment
owns PyTorch, CUDA, and the pinned NVLabs SDK. The Skill stops its sessions,
requests explicit GPU-resource release, and stops the Provider after completion
when no foreign session remains.

The `perception.object_pose.foundation_pose` Provider is the execution host, not
the workflow owner. It reports generic camera-relative CAD-object poses and
does not decide robot roles, world alignment, calibration validity, or
activation. An explicit `IN_PROCESS` execution host remains available for
dependency-complete development environments, but it is not the default.
