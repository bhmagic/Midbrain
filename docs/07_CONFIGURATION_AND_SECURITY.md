# Configuration and Security

## Local configuration ownership

Tracked source contains examples only. Machine-local files belong under `config` and are ignored by Git.

Typical files:

- `config/system.env`
- `config/api_keys.env`
- `config/providers.json`
- `config/calibration/devices/...`

Setup scripts preserve existing configuration and create missing files from examples.

## API keys

The Test Agent can use optional OpenAI and Gemini integrations. Keys must be placed only in `config/api_keys.env`:

```text
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Never place keys in source, screenshots, logs, commit messages, issues, or PowerShell history. The examples intentionally contain empty values.

## Provider configuration

The canonical template is `platform_core/config_templates/providers.json.example`. It registers:

- `camera.femto_bolt` at control port `7101`, auto-start enabled.
- `localization.local_vio` at control port `7102`, auto-start disabled until requested.

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
