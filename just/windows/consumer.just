set working-directory := "../../tests/consumers/windows"

# Compile and run the standalone version-only consumer from staged crates.
verify:
    cargo clippy --quiet --locked --offline -- -D warnings
    cargo run --quiet --locked --offline
