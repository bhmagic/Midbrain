# NVLabs FoundationPose Windows compatibility source

This directory preserves small Midbrain compatibility sources applied to a
separately installed NVLabs FoundationPose checkout.

`mycpp.py` is the pure-Python fallback for the upstream `mycpp` pose-clustering
extension used by the validated native-Windows runtime. Installation tooling
copies it to the root of the pinned upstream checkout when the native extension
is unavailable.

The portable temporary-mesh-path adjustment for upstream `estimater.py` remains
implemented and tested in
`python/foundation_pose_provider/nvlabs_compat.py`. The upstream checkout,
compiled extensions, runtime environments, and generated files remain outside
the Midbrain Git tree.

These compatibility files are original Midbrain code under the repository MIT
License. NVLabs FoundationPose itself remains governed by its upstream license.
