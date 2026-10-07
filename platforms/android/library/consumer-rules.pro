# Keep the backend's exported construction surface available to consumers that
# enable R8. The canonical UniFFI binding and JNA rules come from blehub-uniffi.
-keep class dev.blehub.android.** { *; }
