peripheral_android_project := "tests/peripherals/android"
peripheral_android_run := "pixi run -e android"
peripheral_android_control := peripheral_android_run + " python -m tools.peripheral_fixture.android"

# Build the physical-device Android BLE Peripheral fixture.
peripheral-android-build:
    {{peripheral_android_run}} python -m tools.run_android -- rtk gradle -p {{peripheral_android_project}} :app:assembleDebug --no-daemon

# Run Android lint and build the debug fixture APK.
peripheral-android-check:
    {{peripheral_android_run}} python -m tools.run_android -- rtk gradle -p {{peripheral_android_project}} :app:lintDebug :app:assembleDebug --no-daemon

peripheral-android-install: peripheral-android-build
    {{peripheral_android_control}} install

peripheral-android-start: peripheral-android-build
    {{peripheral_android_control}} start

peripheral-android-stop:
    {{peripheral_android_control}} stop

peripheral-android-notify value="deadbeef":
    {{peripheral_android_control}} notify {{value}}

peripheral-android-indicate value="deadbeef":
    {{peripheral_android_control}} indicate {{value}}

peripheral-android-disconnect:
    {{peripheral_android_control}} disconnect

peripheral-android-status:
    {{peripheral_android_control}} status

peripheral-android-log:
    {{peripheral_android_control}} log
