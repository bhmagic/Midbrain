# Orbbec SDK Packaging Strategy

This source package intentionally does not embed the complete Orbbec SDK.

A hardware Resource Provider is commonly distributed in three forms:

1. **Source package** — provider code, manifest, build scripts, and dependency declarations.
2. **Binary provider package** — prebuilt provider executable plus runtime DLLs that may legally be redistributed.
3. **Installer/bootstrapper** — detects drivers, firmware, runtime libraries, permissions, and supported hardware.

For this prototype, the source build requires Orbbec headers, the import library, and runtime files. The build copies the runtime DLL and extensions next to `CameraHost.exe`. Before redistributing those files, review all vendor and third-party licence notices.

A smoother production installation should either:

- ship a signed prebuilt provider with permitted runtime dependencies, or
- detect the official Orbbec installation and guide the operator to install it before enabling the provider.

The provider manifest should eventually declare tested SDK, driver, firmware, device, OS, and architecture ranges, plus hashes and licence identifiers.
