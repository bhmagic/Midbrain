# Femto Bolt SDK, installation, and distribution

The source package does not bundle the complete Orbbec development SDK. The
Provider is designed around the Orbbec SDK 2.8.x interface on Windows. The
installed SDK, driver, firmware, device revision, operating system, and
architecture must be recorded with deployment validation rather than inferred
from this document.

## Build-time dependency

Building `CameraHost.exe` requires:

- Orbbec SDK headers
- `OrbbecSDK.lib`
- Visual Studio 2022 C++ build tools
- CMake

`scripts\setup.ps1` checks these files before the native build. It is the
supported installation entry point; do not copy unreviewed headers, libraries,
or DLLs into the package to bypass a failed dependency check.

## Runtime dependency

The build copies `OrbbecSDK.dll` and the complete SDK `extensions` directory beside `CameraHost.exe`, then validates the frame-processor and depth-engine DLLs. After a successful build, the Provider normally runs from that release directory without searching development headers or import libraries.

Per-frame metadata on Windows may require the Orbbec metadata registration
script to be run separately with Administrator privileges. See
[Windows frame-metadata setup](WINDOWS_FRAME_METADATA_SETUP.md).

Drivers, firmware, USB host configuration, and other vendor-level requirements may still apply.

## Packaging and distribution policy

A hardware Resource Provider may be distributed as source, as a prebuilt
binary with redistributable runtime dependencies, or through an installer that
detects vendor prerequisites. This prototype uses the source-build path and
copies runtime files beside `CameraHost.exe` only after locating the installed
SDK.

Do not assume every vendor SDK component may be redistributed. A production
package should declare exact build and runtime dependencies, license
identifiers, supported devices, firmware range, checksums, and an installer or
dependency resolver. Vendor files should be bundled only after reviewing the
applicable licenses and notices.

## Official references

- [Orbbec SDK v2 repository](https://github.com/orbbec/OrbbecSDK_v2)
- [Orbbec SDK v2 application guide](https://orbbec.github.io/docs/OrbbecSDKv2_API_User_Guide/source/3_Application_Guide/Application_Guide.html)
- [Orbbec SDK v2 examples](https://github.com/orbbec/OrbbecSDK_v2/blob/main/examples/README.md)
- [Femto Bolt documentation](https://doc.orbbec.com/documentation/Orbbec%20Femto%20Bolt%20Documentation)
- [Femto Bolt specifications](https://www.orbbec.com/products/tof-camera/femto-bolt/)

These references describe the vendor surface. Midbrain's actual exposed
capabilities remain those declared by `manifest.json` and confirmed by current
Provider readiness.

## Deliberately excluded device-control scope

The current Provider exposes the always-on sensory data plane. State-changing
or administrative functions remain outside the active interface until they
have explicit permissions, deadlines, audit records, and rollback behavior.

| SDK area | Intended boundary |
|---|---|
| Exposure, gain, white balance, laser, and flood controls | Versioned Provider command with range discovery and rollback |
| Recording and playback | Separate recorder/playback Resource Providers with deterministic timestamps |
| Triggered or multi-camera capture | Explicit camera command plus coordination and hardware status |
| Device-time synchronization | Time-sync command plus clock-domain observations |
| Firmware update | Offline administrative tool; never an autonomous default |
| Post-processing filters | Derived processing Provider or explicit camera profile |
| Hot-plug recovery | Manager restart policy plus Provider device-change handling |
