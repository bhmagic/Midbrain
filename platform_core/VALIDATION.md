# Validation

The v0.3 Rust source passed grammar-level syntax parsing and was structurally reviewed but not compiled in the delivery container because Cargo/rustc were unavailable. Run `cargo fmt --check`, `cargo test`, `cargo clippy --workspace --all-targets -- -D warnings`, and `cargo build --release` on the target Windows toolchain.
