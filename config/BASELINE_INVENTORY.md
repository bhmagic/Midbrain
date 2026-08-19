# Configuration Baseline Inventory

This inventory distinguishes source-controlled recovery material from machine-local state. A clean checkout must either contain a safe baseline or have a deterministic, non-interactive generator for every file required before a component can start.

## Workspace configuration

| Active path | Clean source | Creation behavior | Local ownership |
|---|---|---|---|
| `config/system.env` | `config/system.env.example` | `platform_core/scripts/initialize_config.ps1` copies it when missing and preserves an existing file. | Local endpoint and runtime tuning overrides. |
| `config/api_keys.env` | `config/api_keys.env.example` | The core initializer creates an empty-key copy when missing. Test Agent setup uses the same root template, with a package-local fallback. | Secrets and optional hosted-model selections. Never commit the active file. |
| `config/providers.json` | `config/providers.json.example` | The core initializer copies it when missing. Provider registration scripts create the file if necessary and merge only their own entry. | Installed Provider set and machine-specific command arguments. |
| `config/robot_assemblies/primary_manipulator.json` | `config/robot_assemblies/primary_manipulator.example.json` | Basic setup copies it when missing and preserves an existing selection. The central file points once to the installed arm Provider root, then references that Provider's model, calibration, mounted-effector, and collision profiles by relative path plus identity and revision. | Installed arm and effector selection. Never commit the active file. |
| `config/foundation_pose/...` | Complete sanitized runtime/restore profile in this repository | A clean checkout already satisfies the configured model-registry path. FoundationPose setup seeds only missing default-profile files and preserves a custom registry. | Custom object models, runtime captures, caches, and local registry edits remain local. |

The copies under `platform_core/config_templates` and `test_agent/config_templates` are package-level fallbacks. Validation requires them to remain byte-identical to the corresponding root examples so setup order cannot change the generated configuration.

The Agent model catalog uses compatibility key names beginning with
`OPENAI_AGENT_`, but values may select either the Gemini adapter or native GPT
resolution. Gemini and OpenAI credentials remain separate blank template
fields, and active values remain machine-local.

## Provider-local configuration

| Component and active path | Clean source or generator | Why it is safe |
|---|---|---|
| Basic arm `config/arm_model.json` | `providers/rebot_arm_dm/config_templates/arm_model.factory.json` | Provider setup copies the nominal factory model only when the active file is missing. |
| Basic arm `config/arm_calibration.json` | `providers/rebot_arm_dm/config_templates/arm_calibration.initial.json` | The baseline is explicitly marked unverified/unassigned and must not be mistaken for measured calibration. Setup preserves an existing measured file. |
| Basic arm `config/calibration_collision_model.json` | `providers/rebot_arm_dm/config_templates/calibration_collision_model.json` | Provider setup copies the conservative calibration-only model when missing. |
| Integrated arm `config/controller.json` | `providers/rebot_arm_integrated/config_templates/controller.default.json` | Setup creates it when missing. Provider startup also validates, merges safe operator-owned values, and repairs a missing, invalid, or obsolete active file. |
| Contact arm `config/controller.json` | `providers/rebot_arm_contact/config_templates/controller.default.json` | Setup creates it when missing. The environment contains no signing secret; each allowlisted Contact Skill has a separate `MIDBRAIN_CONTACT_*_SECRET` in ignored local secret configuration. |
| Slicing Skill `config/motion_profiles.json` | `skills/slicing/config_templates/motion_profiles.default.json` | Skill setup creates the local numbered load/retract/timing registry when missing and preserves developer-added profiles. Numbered blade-use profiles live in the selected mounted-effector profile's namespaced Slicing extension. |
| Stationary alignment `config/alignment.json` | `skills/stationary_world_arm_alignment/config_templates/alignment.default.json` | The Skill loads the default directly. The active file is only an optional deep-merge override, so no empty local file is required. |

Provider registration templates under each `config_templates/provider_entry.json` are source-controlled. The camera and Local VIO entries in `config/providers.json.example` are validated against their canonical Provider entries.

## State that must not have a reusable populated template

| State | Behavior | Reason no populated template is shipped |
|---|---|---|
| `config/calibration/devices/.../imu-accelerometer.json` | The camera Provider creates an identity `UNCALIBRATED` document after it reads a valid device serial; the calibration GUI later replaces it atomically. | The path and document are bound to a real manufacturer, model, and serial. |
| Alignment calibration revisions | The alignment Skill creates its calibration directory and writes bounded run results as they are accepted. | Results are tied to the physical camera, stand, arm, VIO epoch, and observation evidence. |
| Captures, masks, screenshots, debug bundles, Basic calibration sessions, logs, and PID files | Their writers create output directories on demand. | These are runtime evidence or transient process state, not startup configuration. |
| FoundationPose native checkout, compiled CUDA extensions, and install caches | The release/setup procedure installs or rebuilds them. | They are external or generated runtime dependencies, not configuration templates. |
| Orbbec `CameraHost.exe` and SDK DLLs | The native build/setup procedure creates or supplies them. | They are platform- and SDK-specific binaries. |
| Python virtual environments and Rust/native build output | Setup/build scripts create them. | Generated artifacts are reproducible from source and dependency installation. |

## Clean-checkout contract

`scripts/test_config_baselines.ps1` verifies the tracked examples, blank secret values, JSON structure, Provider-entry consistency, FoundationPose registry targets, ignore rules, non-interactive initialization, and preservation of existing local files. Repository validation runs this audit without starting Providers or submitting robot motion.
