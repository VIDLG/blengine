# UniFFI Kotlin error-field collision probe

This expected-failure fixture checks whether UniFFI avoids the Kotlin
`Throwable.message` / `Throwable.cause` collision represented by BoltFFI issue
#871.

Generate the stable Kotlin/JNA bindings, then compile them:

```text
pixi run just ffi-spike-check-uniffi-error-field-collision
```

The raw Kotlin compilation is expected to fail. The recipe verifies the
specific `Throwable.message`/`Throwable.cause` diagnostics and returns success
only when that known generator limitation is reproduced. If the generated code
starts compiling, the fixture must be converted into a positive regression test.
