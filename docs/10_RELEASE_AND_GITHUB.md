# Release and GitHub

## Clean validation

From Developer PowerShell in the repository root:

```powershell
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

## Branch workflow

Use a temporary branch for every change. Do not push or merge until the owner
explicitly authorizes that remote action.

```powershell
git switch main
git pull --ff-only
git switch -c codex/<short-description>
```

Stage only reviewed files. Do not use `git add --all` until ignored and untracked
content has been inspected.

```powershell
git status --short --ignored
git add <reviewed-paths>
git diff --cached --check
git diff --cached --stat
git commit -m "<area>: <outcome>"
git push -u origin HEAD
```

Open a pull request and merge only after required validation succeeds. `main` is
the accepted baseline and must not be force-pushed.

The historical `scripts/publish_github.ps1` helper exists only for the original
one-time publication path. It is not the normal development or release workflow.
Authentication is not embedded; use Git Credential Manager or GitHub CLI.

## Large assets

- Keep required FoundationPose checkpoints in Git LFS using the exact tracked paths.
- Put installers, release bundles, compiled binaries, and validation reports in a GitHub Release.
- Keep captures, point clouds, generated plans, logs, and replay payloads outside Git.
- Commit checksums, provenance, licenses, and download instructions for externally stored artifacts.

## Release checklist

- [ ] `scripts/validate.ps1` passes on Windows.
- [ ] CameraHost builds with the installed Orbbec SDK.
- [ ] Main GUI portal acceptance flow passes on the target workstation.
- [ ] Required Provider observation, activation, Agent, and shutdown paths pass
  on hardware.
- [ ] Staged-file secret/config review is clean.
- [ ] README screenshots, if added, contain no serial numbers or private workcell imagery.
- [x] MIT License added for original project code.
- [ ] Third-party source and dependency-license audit is complete and notices are recorded.
- [ ] Third-party Orbbec SDK redistribution terms have been reviewed.
- [ ] Package, manifest, documentation, and changelog versions are consistent.
- [ ] Active documentation passes link, navigation, and stale-path checks.
- [ ] A near-stable checkpoint names its accepted live path, unchanged duty and
  authorization boundaries, open qualification, implementation commit, and
  rollback/investigation evidence in the owning status and development docs.
- [ ] Dated incidents and performance reports remain historical; current
  architecture, operation, validation, roadmap, and component documents have
  been reconciled rather than relying on changelog entries alone.
- [ ] A version tag and release notes are created only for an explicitly approved release.
