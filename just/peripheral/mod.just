set working-directory := "../.."

android_project := "tests/peripherals/archive/android"
android_run := "pixi run -e android"
android_control := android_run + " python -m tests.peripherals.archive.android.provider"
vm_control := "python -m tests.peripherals.archive.bless.provider"

import "android.just"
import "nrf.just"
import "bless.just"

# Verify provider profiles have aligned GATT semantics and isolated RF identities.
profiles-check:
    python tools/check_peripheral_profiles.py
