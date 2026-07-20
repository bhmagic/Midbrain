# Local Configuration

This directory is intentionally excluded from Git except for this file and `.gitkeep`.

The setup scripts create machine-local files here, including:

- `system.env`
- `api_keys.env`
- `providers.json`
- serial-bound calibration under `calibration/devices/...`

Do not commit API keys, device serial numbers, calibration measurements, runtime logs, captures, or generated PID files. Use the example files under `platform_core/config_templates` and `test_agent/config_templates` as starting points.
