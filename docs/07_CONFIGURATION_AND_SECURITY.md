# Configuration and Security

## Local configuration ownership

Tracked source contains clean examples plus the sanitized FoundationPose restore profile. Machine-local files belong under `config` and are ignored by Git.

Typical files:

- `config/system.env`
- `config/api_keys.env`
- `config/providers.json`
- `config/calibration/devices/...`
- `config/foundation_pose/...`

`platform_core\scripts\initialize_config.ps1` preserves existing configuration and creates all three top-level files from root examples. It creates `api_keys.env` with blank secret values. The same initializer runs during core setup and workspace launch, so a missing top-level file does not require an interactive repair.

The audited ownership and generation table is in `config\BASELINE_INVENTORY.md`. Serial-bound calibration, alignment results, captures, caches, and run state are generated at runtime and deliberately do not have populated reusable templates.

## API keys

The Test Agent and FoundationPose tracking GUI can use optional hosted-model integrations. Keys must be placed only in `config/api_keys.env`:

```text
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Never place keys in source, screenshots, logs, commit messages, issues, or PowerShell history. The examples intentionally contain empty values.

## Provider configuration

The root recovery template is `config/providers.json.example`. An identical package fallback is kept at `platform_core/config_templates/providers.json.example`. They register:

- `camera.femto_bolt` at control port `7101`, auto-start enabled.
- `localization.local_vio` at control port `7102`, auto-start disabled until requested.

The FoundationPose `scripts\setup.ps1` installer merges its Provider registration into the machine-local configuration without overwriting unrelated entries. Its default control port is `7103`, and its persistent model/target settings live under `config/foundation_pose`.

Every Provider registration script can also create an empty `providers` document when no active file exists, then merge its own canonical `config_templates/provider_entry.json` entry. Registration does not require a hand-written placeholder file.

OpenAI visual localization sends the selected camera image and CAD reference renders to a hosted service. Treat those images as externally disclosed data, review the service retention and privacy terms for the deployment, and use manual boxes or a fully local detector when images cannot leave the machine. SAM2 segmentation runs locally when installed.

The GUI uses `gpt-5.6-luna` by default. Set `OPENAI_VISION_MODEL` in the local `config/api_keys.env` only when intentionally evaluating a different compatible visual model.

Environment placeholders are expanded by the Manager:

- `${PHYSICAL_AGENT_ROOT}`
- `${PHYSICAL_AGENT_PYTHON}`
- `${MANAGER_URL}`
- `${FABRIC_URL}`
- `${CAMERA_MAPPING_NAME}`

The workspace launcher imports `config/system.env` before starting the Manager, so canonical Provider entries inherit these values. A manually launched Manager process must receive the same environment explicitly.

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

Run `scripts\test_config_baselines.ps1` to verify clean examples, blank secrets, generation/preservation behavior, ignore rules, Provider-entry consistency, and FoundationPose registry targets without starting any Provider.
