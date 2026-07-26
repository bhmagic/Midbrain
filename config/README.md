# Local Configuration

This directory is intentionally excluded from Git except for this file and `.gitkeep`.

The setup scripts create machine-local files here, including:

- `system.env`
- `api_keys.env`
- `providers.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, generated PID files, or absolute workstation paths.

Safe starting points:

- Copy `config/api_keys.env.example` to `config/api_keys.env` and fill the local copy only.
- Copy `platform_core/config_templates/system.env.example` and `platform_core/config_templates/providers.json.example` when creating a machine-local runtime configuration.
- Run `providers/foundation_pose/scripts/seed_default_models.ps1` to seed the sanitized reBot CAD profile into `config/foundation_pose`. Do not commit the seeded copy.

The canonical reusable FoundationPose assets are tracked under `providers/foundation_pose/defaults/rebot_b601_dm`. That profile contains the retained STEP/OBJ sources, prepared meshes, portable metadata, model registry, provenance, licenses, and rendered reference atlases. Machine-local FoundationPose metadata may contain absolute paths, and captures may contain private room imagery, so neither belongs in Git.
