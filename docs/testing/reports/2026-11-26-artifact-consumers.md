# Artifact consumers：两端六个消费者重新编译，暴露并修掉两处漂移

日期：2026-11-26
范围：`abi-freeze-readiness.md` §4 第 2 步里「iOS/Android artifact consumer 编译」这一项
产物：Android App/测试 APK `a1fcb8ef…` / `0578d9b2…`（后续测试 APK 因消费者修复未变）

本文记录编译与运行事实。它的价值不只是「编译通过」：这些消费者是**独立于 RN 的 ABI 检查**，
所以它们编译不过的地方，恰好是产品面长大而没人重新消费的地方。

## 结果

| 消费者 | 命令 | 结果 |
|---|---|---|
| Android BleHub（AAR + APK） | `pixi run just ffi android-backend-consumer-build` | `BUILD SUCCESSFUL` |
| Android BleHub 设备运行 | `pixi run just ffi android-backend-consumer` | `android_instrumentation=passed`（`3ac5cc09`） |
| Android BLeng（AAR + artifact APK） | `pixi run just ffi android-bleng-build` | `bleng_android_consumer=static-build-passed`，`bleng_elf_audit_arm64-v8a/x86_64=blehub+bleng+bleng_supervisor single-image` |
| iOS BleHub（unsigned，device + simulator） | `pixi run just apple consumer-ios` | `apple_ios_artifact_unsigned_build=passed` ×2 |
| macOS BleHub（运行完整生命周期） | `pixi run just apple consumer-macos` | `apple_artifact_consumer=passed`，`backend_resources scope=macos-native-non-rf-not-iphone` |
| macOS BLeng（artifact-only 生命周期） | `pixi run just apple consumer-bleng-macos` | `supervisor_typed_operations=passed`，`apple_bleng_artifact_consumer=passed` |
| iOS BLeng（unsigned，device + simulator） | `pixi run just apple consumer-bleng-ios-unsigned` | `apple_bleng_ios_unsigned_ios-device=passed`、`…ios-simulator=passed` |

## 漂移 1：所有假 backend 少实现了 10 个 `BleBackend` 成员

Android 消费者 `:app:compileDebugAndroidTestKotlin` 直接失败：

```text
e: PublishedArtifactConsumerTest.kt:178:13 Class 'PublishedArtifactConsumerTest.ConsumerBackend'
   is not abstract and does not implement abstract members:
   fun setAdapterEnabled(command: SetAdapterEnabledCommand): Unit
   … queryDevices, associateCompanionDevice, readDescriptor, writeDescriptor,
   mtu, setConnectionPriority, createBond, removeBond, refreshGattCache
```

iOS 侧同一原因（`FakeBackend does not conform to protocol 'BleBackend'`，xcodebuild 逐条列出
同样的 10 个 note）。这就是「产品面长大了、消费者没跟着长」：这些能力是 2026-11-25/26 加进
`crates/blehub/src/backend.rs` 的，而消费者的假 backend 只有很少被编译，所以一直没人发现。

修法按各自文件的语义分三类，而不是一律空实现：

- 纯形态消费者（`tests/consumers/android/blehub/.../PublishedArtifactConsumerTest.kt`、
  `tests/consumers/apple/blehub/{ios,macos}`、`tests/ffi/runtime/swift-host/main.swift`）：
  空实现，它们证明的是 AAR/包的形状与装配，不驱动 BLE。
- BLeng 的 `fatalError("unused")` 假件（`tests/consumers/apple/bleng/{ios,macos}`）：保持原有
  「这条通道没被这条场景用过」的写法。
- 有严格调用序列的假件（`tests/consumers/apple/bleng/macos/.../TypedOperations.swift`）：新命令
  按名字拒绝（`unexpected command: mtu`），因为它的 `expectedCalls` 是逐条断言的，静默接受会
  把「意外命令」变成通过。
- 委托式假件（`tests/hil/apple/bleng-ios/Sources/SystemLossWitnessBackend.swift`）：透传给
  真实 delegate，保持它只观测 bind/initialize/dispose 的原意。
- RF 场景假件（`tests/hil/apple/support/{AppleCoreBluetoothRfScenarioTests,SystemLossWitnessTests}.swift`）：
  `try unexpected()`，与它们已有的写法一致。

顺带把 `just apple::test` 第二段长期存在的「HIL support 假件不符合新 `BleBackend`」也一起关了
（这批文件就是那几份）。

## 漂移 2：消费者把资源清单的**数量**写死了

编译修好后设备运行失败：

```text
java.lang.AssertionError: expected:<11> but was:<12>
  at PublishedArtifactConsumerTest.realBackendIngressAndLeaseAreObservedBeforeDroppingTheDisposedInstance
```

`AndroidBleBackend.snapshotRfResources()` 的键从 11 个长到 12 个（`adapterReceivers` 是
adapter 状态流带来的），而消费者断言 `assertEquals(11, disposed.size)`。

这条断言本来想证明的是「dispose 之后每个原生根的计数都归零」，数量只是它当时的副作用。改法
是把不变式写清楚，而不是把 11 改成 12：

```kotlin
// The inventory is an owner diagnostic, not frozen ABI: pin its shape and its
// zeroed values, never a counter count that grows with each new owned root.
assertEquals(active.keys, disposed.keys)
assertTrue("disposed native roots: $disposed", disposed.values.all { count -> count == 0 })
```

这样下一根计数出现时消费者不会因此变红，而真正要守的两件事仍在：清单形状不因 dispose 改变，
所有计数归零。改完后同一份 APK 重装重跑：`android_instrumentation=passed`。

## 诚实边界

- 这些是**非 RF** 证据。`consumer-macos` 自己的收据就写着
  `backend_resources scope=macos-native-non-rf-not-iphone`；它们不证明任何扫描/连接/IO。
- iOS/macOS 消费者是 unsigned 构建（device + simulator），不是真机运行。
- Android BLeng 消费者只是静态构建 + 单 image 审计，`ART instrumentation not run`。
- 消费者修复只改了测试侧文件，产品 crate/ABI 未动，所以九份 API 快照不应变化（见本轮
  `snapshot-check` 结果）。
