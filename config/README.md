# Local Configuration

Most of this directory is intentionally excluded from Git. The exceptions are this guidance, blank templates, and the sanitized FoundationPose runtime/restore profile.

The non-interactive core initializer and Provider setup scripts create missing
machine-local files here and preserve existing ones:

- `system.env`
- `api_keys.env`
- `providers.json`
- `robot_assemblies/primary_manipulator.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, generated PID files, or absolute workstation paths.

Safe starting points:

- `config/system.env.example`, `config/api_keys.env.example`, and `config/providers.json.example` are the root recovery copies used by the initializer.
- `config/robot_assemblies/primary_manipulator.example.json` is the clean central assembly-selection template. Basic setup copies it to the ignored active path when missing; its profile paths remain relative to the selected arm Provider root.
- The arm collision profile owns arm-link geometry. The selected mounted-effector profile owns gripper or fixed-tool collision primitives, so an effector replacement normally changes only the mounted-effector reference and any role qualification that the replacement invalidates.
- A mounted-effector profile may carry namespaced optional `extensions`. Basic consumes the strict core and preserves extension objects; a Skill consumes and validates only its own namespace. The VLM translation aligner's descriptions, required point set, complete-point arithmetic-mean rule, controlled-frame offset, and future reference-image policy live in `extensions.midbrain.skill.refine_arm_root_translation.v1`.
- The main Midbrain page can select among compatible Provider-owned effector profiles while the arm Provider and its dependents are stopped. It writes only the ignored active `primary_manipulator.json`; the clean example remains the installation seed.
- `platform_core/config_templates` and `test_agent/config_templates` contain validated package-level fallback copies for partial-package setup.
- The generated `config/api_keys.env` contains blank keys. Fill the active local file only when a hosted-model feature is intentionally enabled.
- The clean Agent catalog currently selects Gemini through `GEMINI_API_KEY`
  and retains GPT alternatives through `OPENAI_API_KEY`. The legacy
  `OPENAI_AGENT_MODEL` and `OPENAI_AGENT_MODELS` names configure the complete
  multi-provider Agent catalog; setup preserves an existing ignored local
  selection.
- The FoundationPose Provider already reads `config/foundation_pose/models.json`; a complete sanitized registry, CAD profile, and reference-atlas copy is shipped at that location.
- If the shipped profile is deleted or damaged in a Git checkout, restore it with `git restore --source=HEAD -- config/foundation_pose`. The Provider seeding script remains a secondary repair path for missing files.

See `config/BASELINE_INVENTORY.md` for the audited path-by-path contract, including Provider-local arm configuration, generated device calibration, optional Skill overrides, and runtime state that must not be templated.

The canonical reusable FoundationPose assets remain under `providers/foundation_pose/defaults/rebot_b601_dm`. The checked-in `config/foundation_pose` tree deliberately duplicates that sanitized profile so a fresh checkout already satisfies the runtime path and users can restore it locally without reconstructing assets.

Only the named clean examples, audit documentation, and restore-profile files are allowed by `.gitignore`. Active `providers.json`, `system.env`, API keys, serial-bound calibration, runtime captures, masks, backups, installation caches, generated archives, and metadata containing absolute workstation paths remain machine-local.
