# Windows Frame-Metadata Setup

The provider reads every per-frame metadata field exposed by Orbbec SDK 2.8.x and attaches available values to each video `BufferRef` under `frame_metadata`.

On Windows, Orbbec frame metadata may require the vendor's metadata registration script to be run with Administrator privileges. This is an operating-system registration step and is intentionally not run automatically by this package.

A typical SDK source/distribution command is:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\obsensor_metadata_win10.ps1 -op install_all
```

The exact script location depends on the Orbbec SDK distribution. Search the SDK package if necessary:

```powershell
Get-ChildItem "C:\Program Files\OrbbecSDK 2.8.6" -Recurse -Filter obsensor_metadata_win10.ps1
```

After registration, restart Windows if requested, restart the workspace, and inspect:

```powershell
Invoke-RestMethod http://127.0.0.1:7001/v1/capabilities |
    Where-Object capability -eq "camera.frame_metadata"
```

Frame metadata is capability-specific and stream-specific. A camera can remain healthy with RGB, depth, IR, point cloud, calibration, and IMU available while some metadata fields are unavailable.

## BufferRef fields

- `metadata_mask`: bit mask of available SDK metadata fields.
- `frame_metadata`: name-to-integer-value dictionary containing only available fields.
- `global_timestamp_us`: host-domain global timestamp when supported and enabled.
- `flags`: provider transport flags documented in `FULL_CAPABILITY_PROFILE.md`.
