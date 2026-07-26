# Local Configuration

Most of this directory is intentionally excluded from Git. The exceptions are this guidance, blank templates, and the sanitized FoundationPose runtime/restore profile.

The setup scripts create machine-local files here, including:

- `system.env`
- `api_keys.env`
- `providers.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, generated PID files, or absolute workstation paths.

Safe starting points:

- Copy `config/api_keys.env.example` to `config/api_keys.env` and fill the local copy only.
- Copy `platform_core/config_templates/system.env.example` and `platform_core/config_templates/providers.json.example` when creating a machine-local runtime configuration.
- The FoundationPose Provider already reads `config/foundation_pose/models.json`; a complete sanitized registry, CAD profile, and reference-atlas copy is shipped at that location.
- If the shipped profile is deleted or damaged in a Git checkout, restore it with `git restore --source=HEAD -- config/foundation_pose`. The Provider seeding script remains a secondary repair path for missing files.

The canonical reusable FoundationPose assets remain under `providers/foundation_pose/defaults/rebot_b601_dm`. The checked-in `config/foundation_pose` tree deliberately duplicates that sanitized profile so a fresh checkout already satisfies the runtime path and users can restore it locally without reconstructing assets.

Only the named restore-profile files are allowed by `.gitignore`. Active `providers.json`, `system.env`, API keys, serial-bound calibration, runtime captures, masks, backups, installation caches, generated archives, and metadata containing absolute workstation paths remain machine-local.
