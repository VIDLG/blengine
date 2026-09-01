set working-directory := "../../examples/windows-central"

# Compile and lint the user-facing Windows Central example without scanning RF.
verify:
    rtk cargo clippy --locked --all-targets -- -D warnings
