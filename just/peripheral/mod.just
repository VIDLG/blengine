set working-directory := "../.."

android_project := "tests/peripherals/android"
android_run := "pixi run -e android"
android_control := android_run + " python -m tests.peripherals.android.provider"

import "android.just"
import "nrf.just"

# Verify provider profiles have aligned GATT semantics and isolated RF identities.
profiles-check:
    python tools/check_peripheral_profiles.py
