# Current Package Set

- `physical_ai_manager_fabric_v0_3_0_source.zip`
- `physical_ai_orbbec_femto_bolt_provider_v0_3_1_source.zip`
- `physical_ai_local_vio_provider_v0_2_2_source.zip`
- `physical_ai_test_agent_v0_2_9_source.zip`
- `physical_ai_contracts_v0_3_8_working_draft.zip`
- `physical_ai_project_docs_v0_3_10.zip`
- `physical_ai_space_cognition_workspace_overlay_v0_3_10_source.zip`
- `physical_ai_space_cognition_bundle_v0_3_10_source.zip`

v0.3.10 fixes the remaining 50 Hz startup deadlock. Initialization now selects the newest required sample count instead of requiring 80 samples to fit inside a fixed 1.5-second interval; it also reports the selected window counts and inferred IMU rates.
