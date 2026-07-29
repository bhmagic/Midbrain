# Migration from the Original Monolithic Prototype

The split packages use these fixed folders:

- `platform_core`
- `test_agent`
- `providers/orbbec_femto_bolt`
- persistent shared `config`

After the split workspace has been set up and verified, the original prototype folders may be removed:

- `core`
- `python`
- `scripts`
- `providers/femto_bolt`

Also remove the old root `logs`, `run`, `screenshots`, and root `.venv` folders
when their contents are no longer needed. Always keep `config`. Recreate each
Python component's private `.venv` with its setup script.

Do not delete the entire `providers` directory because it now contains `providers/orbbec_femto_bolt`.
