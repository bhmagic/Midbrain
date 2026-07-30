# FoundationPose Object Localization

FoundationPose is a finite, high-latency perception Skill in the Midbrain
architecture. A caller supplies synchronized RGB-D evidence, camera
intrinsics, a known model ID, and an explicit initialization region. The Skill
owns registration/tracking sessions for one bounded parent operation and
releases them when that operation ends.

The first production caller is Stationary World-Space Arm Finder. Its
`FOUNDATIONPOSE_SKILL` route owns masks, sampling, VIO-epoch checks, and result
validation; this Skill owns FoundationPose backend construction, estimator
sessions, and cleanup.

The existing `perception.object_pose.foundation_pose` Provider remains a
temporary compatibility backend. It is no longer the default Stationary
Alignment route and must never be selected as an automatic fallback. Keeping
that route during migration preserves existing session IDs, streams, and
hardware comparison procedures without making a long-lived Provider the new
architectural owner.
