set working-directory := "../.."

android_project := "tests/peripherals/android"
android_run := "pixi run -e android"
android_control := android_run + " python -m tools.peripheral_fixture.android"
vm_control := "python -m tools.peripheral_fixture.bless"

import "android.just"
import "vm.just"

# Verify provider profiles have aligned GATT semantics and isolated RF identities.
profiles-check:
    python tools/check_peripheral_profiles.py
