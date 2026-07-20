# Femto Bolt Provider SDK Requirements

The source package does not bundle the complete Orbbec development SDK. The provider is designed and statically reviewed against Orbbec SDK 2.8.6 on Windows 10.

## Build-time dependency

Building `CameraHost.exe` requires:

- Orbbec SDK headers
- `OrbbecSDK.lib`
- Visual Studio 2022 C++ build tools
- CMake

`scripts\setup.ps1` checks these files before the native build.

## Runtime dependency

The build copies `OrbbecSDK.dll` and the complete SDK `extensions` directory beside `CameraHost.exe`, then validates the frame-processor and depth-engine DLLs. After a successful build, the Provider normally runs from that release directory without searching development headers or import libraries.

Per-frame metadata on Windows may require the Orbbec metadata registration script to be run separately with Administrator privileges. See `WINDOWS_FRAME_METADATA_SETUP.md`.

Drivers, firmware, USB host configuration, and other vendor-level requirements may still apply.

## Distribution policy

Do not assume every vendor SDK component may be redistributed. A production package should declare exact build/runtime dependencies, license identifiers, supported devices, firmware range, checksums, and an installer or dependency resolver. Vendor files should be bundled only after reviewing applicable licenses and notices.
