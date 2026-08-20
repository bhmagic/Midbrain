# Local Configuration

Most of this directory is intentionally excluded from Git. The exceptions are this guidance and blank recovery templates.

The non-interactive core initializer and Provider setup scripts create missing
machine-local files here and preserve existing ones:

- `system.env`
- `api_keys.env`
- `providers.json`
- `agent_skill_installation.json` after the first Developer Agent startup
  reconciliation decision
- `robot_assemblies/primary_manipulator.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, generated PID files, or absolute workstation paths.

Safe starting points:

- `config/system.env.example`, `config/api_keys.env.example`, and `config/providers.json.example` are the root recovery copies used by the initializer.
- `config/robot_assemblies/primary_manipulator.example.json` is the clean central assembly-selection template. Basic setup copies it to the ignored active path when missing; its profile paths remain relative to the selected arm Provider root.
- The arm collision profile owns arm-link geometry. The selected mounted-effector profile owns gripper or fixed-tool collision primitives, so an effector replacement normally changes only the mounted-effector reference and any role qualification that the replacement invalidates.
- A mounted-effector profile may carry namespaced optional `extensions`. Basic consumes the strict core and preserves extension objects; a Skill consumes and validates only its own namespace. The VLM translation aligner's descriptions, required point set, complete-point arithmetic-mean rule, controlled-frame offset, and future reference-image policy live in `extensions.midbrain.skill.refine_arm_root_translation.v1`.
- An arm-model profile may carry a flexible `appendix` with arbitrary field names and JSON values. The assembly state preserves and publishes it; each consumer validates only its namespaced entry. `appendix.midbrain.skill.locate_arm_base.v1` selects the arm-base CAD, VLM reference images, semantic transform, and bounded rotations.
- The main Midbrain page can select either a Provider-owned arm profile from `providers/rebot_arm_dm/config/arm_profiles` or a compatible mounted-effector profile while the arm Provider and its dependents are stopped. Both guarded selectors write only the ignored active `primary_manipulator.json`; the clean example remains the installation seed. Arm selection uses the profile file as its key so multiple physical arm configurations may share a model identity while carrying different flexible Skill appendices.
- `platform_core/config_templates` and `test_agent/config_templates` contain validated package-level fallback copies for partial-package setup.
- The generated `config/api_keys.env` contains blank keys. Fill the active local file only when a hosted-model feature is intentionally enabled.
- The clean Agent catalog currently selects Gemini through `GEMINI_API_KEY`
  and retains GPT alternatives through `OPENAI_API_KEY`. The legacy
  `OPENAI_AGENT_MODEL` and `OPENAI_AGENT_MODELS` names configure the complete
  multi-provider Agent catalog; setup preserves an existing ignored local
  selection.
- The Developer Agent reconciles every installed discoverable Skill against
  its configured and current runtime tool lists before accepting a task. The
  ignored `agent_skill_installation.json` stores only Agent-owned add/disable
  decisions; it never changes a Skill manifest. Adding takes effect after an
  Agent restart, while disabling suppresses future startup prompts. Set
  `AGENT_SKILL_INSTALLATION_STATE_PATH` only when the machine-local decision
  file needs a non-default location.
- Robot-specific FoundationPose CAD and reference assets belong to the calling
  Skill, but their exact selection is saved in the active arm-model appendix.
  The generic Provider keeps only generated ONNX/TensorRT runtime files in
  ignored local directories.

See `config/BASELINE_INVENTORY.md` for the audited path-by-path contract, including Provider-local arm configuration, generated device calibration, optional Skill overrides, and runtime state that must not be templated.

Only the named clean examples and audit documentation are allowed by
`.gitignore`. Active `providers.json`, `system.env`, API keys, serial-bound
calibration, runtime captures, masks, candidates/reviews, backups,
installation caches, generated archives, and metadata containing absolute
workstation paths remain machine-local.
