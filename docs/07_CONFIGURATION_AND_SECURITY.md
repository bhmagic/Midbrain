# Configuration and Security

## Local configuration ownership

Tracked source contains examples only. Machine-local files belong under `config` and are ignored by Git.

Typical files:

- `config/system.env`
- `config/api_keys.env`
- `config/providers.json`
- `config/calibration/devices/...`
- `config/foundation_pose/...`

Setup scripts preserve existing configuration and create missing files from examples.

## API keys

The Test Agent and FoundationPose tracking GUI can use optional hosted-model integrations. Keys must be placed only in `config/api_keys.env`:

```text
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Never place keys in source, screenshots, logs, commit messages, issues, or PowerShell history. The examples intentionally contain empty values.

## Provider configuration

The canonical template is `platform_core/config_templates/providers.json.example`. It registers:

- `camera.femto_bolt` at control port `7101`, auto-start enabled.
- `localization.local_vio` at control port `7102`, auto-start disabled until requested.

The FoundationPose `scripts\setup.ps1` installer merges its Provider registration into the machine-local configuration without overwriting unrelated entries. Its default control port is `7103`, and its persistent model/target settings live under `config/foundation_pose`.

OpenAI visual localization sends the selected camera image and CAD reference renders to a hosted service. Treat those images as externally disclosed data, review the service retention and privacy terms for the deployment, and use manual boxes or a fully local detector when images cannot leave the machine. SAM2 segmentation runs locally when installed.

The GUI uses `gpt-5.6-luna` by default. Set `OPENAI_VISION_MODEL` in the local environment only when intentionally evaluating a different compatible visual model.

Environment placeholders are expanded by the Manager:

- `${PHYSICAL_AGENT_ROOT}`
- `${PHYSICAL_AGENT_PYTHON}`

## Device calibration

Physical calibration is owned by the camera/IMU Provider and bound to manufacturer, model, and serial. Runtime VIO bias estimates are session state and must not overwrite device calibration.

Do not publish device serial numbers or measured calibration unless intentionally releasing a sanitized dataset.

## Large payloads and shared memory

RGB, depth, IR, aligned depth, and point cloud bytes remain in Windows named shared memory. Fabric observations contain BufferRefs. Shared-memory mapping names and references should remain within the local trust boundary unless an authenticated access layer is added.

## Pre-push review

```powershell
git status --short
git diff --cached --check
git diff --cached --name-only
```

Review for:

- secret or `.env` files
- calibration and serial paths
- captures, images, point clouds, or logs
- native SDK binaries
- `target`, `build`, `.venv`, `__pycache__`, and package metadata
- unrelated providers or backup trees
- raw FoundationPose captures, mask diagnostics, and hosted-model response logs
