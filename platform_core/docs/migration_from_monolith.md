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

Also remove the old root `logs`, `run`, and `screenshots` folders when their contents are no longer needed. Keep `.venv` unless you want a clean Python reinstall. Always keep `config`.

Do not delete the entire `providers` directory because it now contains `providers/orbbec_femto_bolt`.
