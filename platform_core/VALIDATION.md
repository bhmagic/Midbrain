# Validation

The target Windows toolchain built the Manager and Fabric release binaries.
The current workspace regression contains 30 passing Rust tests: 22 Manager
and 8 Fabric. Coverage includes advisory binding selection and revalidation,
cold explicit-fallback preservation, promotion of the same activated fallback
to a current advertised capability, shutdown-plan ordering and blockers, gated
asynchronous shutdown acceptance, shutdown fencing, authority rules, exact
reviewed-workcell activation and revocation, current camera/VIO identity gates,
and Fabric transform/revocation behavior.

`cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
and PowerShell parser checks remain part of the final Phase 3 validation pass.
Manager shutdown execution is still disabled by default and is not
hardware-verified merely because the Rust tests pass.
