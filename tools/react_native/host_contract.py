"""Shared identity for the private React Native Nitro test host."""

from __future__ import annotations

SCHEME = "BleHubNitroHost"
MODULE_NAME = "BleHubNitroHost"
BUNDLE_ID = "org.reactjs.native.example.BleHubNitroHost"
ANDROID_PACKAGE = "com.blehubnitrohost"
ANDROID_ACTIVITY = f"{ANDROID_PACKAGE}.MainActivity"
APP_NAME = f"{SCHEME}.app"
UI_TEST_TARGET = (
    "BleHubNitroHostUITests/BleHubNitroHostUITests/"
    "testContractLabCompletesAndRecreatesProbe"
)
RUNTIME_MARKER_PREFIX = "BLEHUB_NITRO_RUNTIME"
