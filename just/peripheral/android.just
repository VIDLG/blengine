# Build the physical-device Android BLE Peripheral fixture.
android-build:
    {{android_run}} python -m tools.run_android -- rtk gradle -p {{android_project}} :app:assembleDebug --no-daemon

# Run Android lint and build the debug fixture APK.
android-check:
    {{android_run}} python -m tools.run_android -- rtk gradle -p {{android_project}} :app:lintDebug :app:assembleDebug --no-daemon

android-install: android-build
    {{android_control}} install

android-start: android-build
    {{android_control}} start

android-stop:
    {{android_control}} stop

android-notify value="deadbeef":
    {{android_control}} notify {{value}}

# Send a callback-serialized burst of deterministic 20-byte notifications.
android-notification-burst burst_id count payload_bytes="20":
    {{android_control}} notification-burst {{burst_id}} {{count}} {{payload_bytes}}

# Reset Android's callback-side sequence ledger for one write benchmark batch.
android-write-batch-start batch_id count:
    {{android_control}} write-batch-start {{batch_id}} {{count}}

# Query Android's callback-side sequence ledger once after a write benchmark batch.
android-write-batch-status batch_id:
    {{android_control}} write-batch-status {{batch_id}}

android-indicate value="deadbeef":
    {{android_control}} indicate {{value}}

android-disconnect:
    {{android_control}} disconnect

# Disable the physical Android device Bluetooth radio and wait for STATE_OFF.
android-radio-disable:
    {{android_control}} radio-disable

# Enable the physical Android device Bluetooth radio and wait for STATE_ON.
android-radio-enable:
    {{android_control}} radio-enable

android-status:
    {{android_control}} status

android-log:
    {{android_control}} log
