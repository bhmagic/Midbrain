# Local Configuration

Most of this directory is intentionally excluded from Git. The exceptions are this guidance, blank templates, and the sanitized FoundationPose runtime/restore profile.

The non-interactive core initializer creates missing machine-local files here and preserves existing ones:

- `system.env`
- `api_keys.env`
- `providers.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, generated PID files, or absolute workstation paths.

Safe starting points:

- `config/system.env.example`, `config/api_keys.env.example`, and `config/providers.json.example` are the root recovery copies used by the initializer.
- `platform_core/config_templates` and `test_agent/config_templates` contain validated package-level fallback copies for partial-package setup.
- The generated `config/api_keys.env` contains blank keys. Fill the active local file only when a hosted-model feature is intentionally enabled.
- The FoundationPose Provider already reads `config/foundation_pose/models.json`; a complete sanitized registry, CAD profile, and reference-atlas copy is shipped at that location.
- If the shipped profile is deleted or damaged in a Git checkout, restore it with `git restore --source=HEAD -- config/foundation_pose`. The Provider seeding script remains a secondary repair path for missing files.

See `config/BASELINE_INVENTORY.md` for the audited path-by-path contract, including Provider-local arm configuration, generated device calibration, optional Skill overrides, and runtime state that must not be templated.

The canonical reusable FoundationPose assets remain under `providers/foundation_pose/defaults/rebot_b601_dm`. The checked-in `config/foundation_pose` tree deliberately duplicates that sanitized profile so a fresh checkout already satisfies the runtime path and users can restore it locally without reconstructing assets.

Only the named clean examples, audit documentation, and restore-profile files are allowed by `.gitignore`. Active `providers.json`, `system.env`, API keys, serial-bound calibration, runtime captures, masks, backups, installation caches, generated archives, and metadata containing absolute workstation paths remain machine-local.
