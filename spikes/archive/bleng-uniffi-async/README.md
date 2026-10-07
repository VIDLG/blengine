# BLeng / UniFFI async mechanism probe

This is a **standalone, non-workspace, non-production** UniFFI 0.32.0 probe. It
links the existing `blehub` component, a toy async object, and the real BLeng
actor behind the opt-in `crates/bleng` `foreign-probe` feature into **one
experimental** native image. Neither `crates/blehub-uniffi` nor the public
BLeng API is changed. No production or long-lived test path may depend on this
directory or the feature-gated probe surface.

On the real Mac, the pinned local candidate passed:

- offline Rust unit test: an owned Tokio runtime completes a oneshot without a
  caller-owned Tokio runtime; shutdown rejects a new request;
- one debug `.dylib` generated `blehub`, `bleng`, and the toy
  `blehub_bleng_uniffi_async_spike` Swift/Kotlin components. The same static
  archive linked the macOS Swift executable **once**;
- the toy's generated Swift methods are `async throws`; the real BLeng probe's
  `assembleBackend`, `initialize`, `startScan`, and `shutdown` are also
  `async throws`. Kotlin generated `suspend` counterparts import types from
  `uniffi.blehub`. The three generated Kotlin components compiled together
  with Pixi JVM Kotlin and JNA 5.18.1 and ran on the macOS ARM64 JVM; an
  exported async `close()` conflicted with UniFFI Kotlin object's own `close()` and was
  renamed to `shutdown()` **for this probe**, not as a public API decision;
- the toy invoked a Swift `BleBackend`'s `capabilities()`/`dispose()` from a Rust
  blocking lane. Separately, the **real BLeng** feature-gated owner assembled
  a Swift backend through the production BleHub ticket/Hub path, completed
  real actor initialization and scan with synchronous Swift ingress, shut down
  twice with one backend dispose, rejected late ingress and a post-shutdown call.
  The Kotlin/JVM host independently exercised the same assembly, synchronous
  Kotlin ingress, initialization, scan, repeated shutdown, one dispose and
  post-shutdown/late-ingress rejection;
- a pending BLeng initialization remained pending after Swift `Task.cancel()`
  but was terminated by explicit owner `shutdown()`. A releasable toy pending
  future returned its value after Swift Task cancellation and release; its
  drop observer was still false before release. On Kotlin/JVM, coroutine
  cancellation of the pending toy future completed the coroutine and triggered
  the Rust future's drop observer **without** release; cancelling a pending
  BLeng initialization and explicitly shutting down disposed the backend once.
  Neither result claims cancellation of an in-flight native BLE command.

Reproduce from the BleHub root (no device, signing, or RF required):

```sh
pixi run cargo test --offline --locked --manifest-path spikes/bleng-uniffi-async/Cargo.toml
pixi run cargo build --offline --locked --manifest-path spikes/bleng-uniffi-async/Cargo.toml
mkdir -p .work/bleng-uniffi-async-bindings .work/bleng-uniffi-async-kotlin
pixi run cargo run --offline --locked --manifest-path spikes/bleng-uniffi-async/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_bleng_uniffi_async_spike.dylib --language swift --out-dir .work/bleng-uniffi-async-bindings --no-format
pixi run cargo run --offline --locked --manifest-path spikes/bleng-uniffi-async/Cargo.toml --features bindgen --bin uniffi-bindgen -- generate .work/cargo-target/debug/libblehub_bleng_uniffi_async_spike.dylib --language kotlin --out-dir .work/bleng-uniffi-async-kotlin --no-format
xcrun swiftc -parse-as-library -I .work/bleng-uniffi-async-bindings -Xcc -fmodule-map-file=.work/bleng-uniffi-async-bindings/blehubFFI.modulemap -Xcc -fmodule-map-file=.work/bleng-uniffi-async-bindings/blengFFI.modulemap -Xcc -fmodule-map-file=.work/bleng-uniffi-async-bindings/blehub_bleng_uniffi_async_spikeFFI.modulemap .work/bleng-uniffi-async-bindings/blehub.swift .work/bleng-uniffi-async-bindings/bleng.swift .work/bleng-uniffi-async-bindings/blehub_bleng_uniffi_async_spike.swift spikes/bleng-uniffi-async/swift/Smoke.swift .work/cargo-target/debug/libblehub_bleng_uniffi_async_spike.a -o .work/bleng-uniffi-async-bindings/swift-smoke
.work/bleng-uniffi-async-bindings/swift-smoke
```

The JVM gate uses the **JVM** JNA 5.18.1 artifact (the cached Android AAR
contains no macOS `jnidispatch`); the JAR's SHA-256 observed here is
`260c4b1e22b1db9e110ee441c4f13ce115f841fa48c41d78750986214b395557`.
After reviewing that artifact, run:

```sh
mkdir -p .work/bleng-uniffi-async-kotlin-jvm
curl --fail --location --silent --show-error https://repo.maven.apache.org/maven2/net/java/dev/jna/jna/5.18.1/jna-5.18.1.jar --output .work/bleng-uniffi-async-kotlin-jvm/jna-5.18.1-jvm.jar
shasum -a 256 .work/bleng-uniffi-async-kotlin-jvm/jna-5.18.1-jvm.jar
pixi run -e jvm kotlinc .work/bleng-uniffi-async-kotlin/uniffi/blehub/blehub.kt .work/bleng-uniffi-async-kotlin/uniffi/bleng/bleng.kt .work/bleng-uniffi-async-kotlin/uniffi/blehub_bleng_uniffi_async_spike/blehub_bleng_uniffi_async_spike.kt spikes/bleng-uniffi-async/kotlin/Smoke.kt -cp .work/bleng-uniffi-async-kotlin-jvm/jna-5.18.1-jvm.jar:.pixi/envs/jvm/libexec/kotlin/lib/kotlinx-coroutines-core-jvm.jar -d .work/bleng-uniffi-async-kotlin-jvm/smoke.jar
pixi run -e jvm java -cp .work/bleng-uniffi-async-kotlin-jvm/smoke.jar:.work/bleng-uniffi-async-kotlin-jvm/jna-5.18.1-jvm.jar:.pixi/envs/jvm/libexec/kotlin/lib/kotlin-stdlib.jar:.pixi/envs/jvm/libexec/kotlin/lib/kotlinx-coroutines-core-jvm.jar SmokeKt
```

This fetches a public artifact to the ignored `.work` directory; it does not
install a project dependency. The smoke overrides generated component library
names only for its own JVM process to load the same experimental `.dylib`.

The feature-gated owner is **mechanism-only**: its deadline bound and coarse
error mapping are not a public ABI design. The isolated macOS executable is
not the production package, iOS runtime, Android ART, RN, RF, foreign GC/ARC
release or reload, or final app single-image evidence. Kotlin/JVM is a distinct
host gate, not Android ART. The Apple production
packager still expects one component; it cannot stage these generated bindings
as-is. Do not use this extra native image in any production/RN package.

Pinned UniFFI 0.32.0 source confirms the distinction: generated Swift
`Async.swift` uses `withUnsafeContinuation`, then calls `freeFunc` in `defer`
when the async method exits, but has no `Task.cancel()` handler invoking Rust
`rust_future_cancel`. Kotlin's generated `Async.kt` uses
`suspendCancellableCoroutine` and `finally { freeFunc(...) }`; the JVM smoke
also observes its Rust pending-future drop after cancellation, without release. Core's
`ffi/rustfuture/future.rs` describes cancellation/free, but availability of a
Rust cancel symbol does not mean the foreign wrapper calls it. BLeng's upper
waiter timeout/cancellation never implies native BLE cancellation; the explicit
BLeng close coordinator remains responsible for shutdown. Next verify Android ART and iOS device runtime, then package the designed BLeng
component in the **one** production image with exact component allowlists and clean consumers, and test
foreign lifetime/reload independently before public API review.
