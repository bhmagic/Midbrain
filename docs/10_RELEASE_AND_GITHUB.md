# Release and GitHub

## Clean validation

From Developer PowerShell:

```powershell
cd C:\Projects\testing_physical_ai
.\scripts\validate.ps1
```

For a complete CameraHost build, provide SDK paths and enable native validation:

```powershell
.\scripts\validate.ps1 `
  -BuildNativeCamera `
  -OrbbecIncludeDir "C:\Program Files\OrbbecSDK 2.8.6\include" `
  -OrbbecLibrary "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib" `
  -OrbbecBinDir "C:\Program Files\OrbbecSDK 2.8.6\bin"
```

## Review staged content

```powershell
git status --short
git diff --cached --check
git diff --cached --stat
```

Confirm no secrets, local configuration, calibration, serial numbers, SDK binaries, builds, virtual environments, logs, captures, PID files, or unrelated providers are staged.

## Upload

```powershell
.\scripts\publish_github.ps1 `
  -RepositoryUrl "https://github.com/bhmagic/Midbrain.git" `
  -CommitMessage "Publish RGB-D physical AI platform baseline"
```

The script initializes Git if required, validates by default, creates or updates `origin`, stages files, runs staged-file checks, commits when needed, renames the branch to `main`, and pushes.

Authentication is not embedded. Use Git Credential Manager or authenticate the GitHub CLI before running the script.

## Empty remote behavior

The target repository was empty at the time of cleanup. The first push therefore creates the `main` branch. If content is added to the remote independently before upload, fetch and reconcile it instead of forcing a push.

## Release checklist

- [ ] `scripts/validate.ps1` passes on Windows.
- [ ] CameraHost builds with the installed Orbbec SDK.
- [ ] Test Agent tutorial passes on hardware.
- [ ] IMU calibration tutorial passes on hardware.
- [ ] Staged-file secret/config review is clean.
- [ ] README screenshots, if added, contain no serial numbers or private room imagery.
- [x] MIT License added for original project code.
- [ ] Third-party source and dependency-license audit is complete and notices are recorded.
- [ ] Third-party Orbbec SDK redistribution terms have been reviewed.
- [ ] A version tag and release notes are created after the first successful hardware validation.
