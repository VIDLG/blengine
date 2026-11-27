# BleHub 的 React Native / Nitro 入门笔记

状态：给本仓库开发者的概念和现状说明；发布边界以[已批准的计划](../../../.plan/PLAN.md)为准，不把内部 probe 变成公开 API。

## 先看全链路

```text
RN App (JS/TS)
  → @blehub/react-native-internal 的 createBleHubProbe()
  → Nitro HybridObject / Nitrogen 生成的 JSI 桥
  → iOS Swift 或 Android Kotlin 适配器
  → Stable UniFFI 生成的语言绑定 → Rust BleHub
      ├─ iOS: AppleBleBackend → CoreBluetooth（已有真机 RF 证据）
      └─ Android: DeterministicBackend（仅测试 backend，尚非 Android Framework BLE）
```

反方向的事件经过 `BackendEventIngressHandle` → Rust `BleHub` → `BleEventSink` → 原生适配器 → Nitro JS listener。**Nitro 负责 JS ↔ 原生桥，UniFFI 负责 Swift/Kotlin ↔ Rust 桥；二者都不是 BLE backend。** 详见 [集成边界](react-native-integration-boundary.md)及[包内 README](../../../integrations/react-native-internal/README.md)。

### 哪些证据能把链路接起来？

- **代码装配（iOS）：** [`createBleHubProbe()`](../../../integrations/react-native-internal/src/index.ts) 使用与 [`nitro.json`](../../../integrations/react-native-internal/nitro.json) 对应的对象名；[`HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) 构造生产 [`AppleBleBackend`](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift)、`BleEventSink` 和 UniFFI `BleHub`，调用 `BleHub.initialize/startScan/...`。生产 [`crates/blehub/src/hub.rs`](../../../crates/blehub/src/hub.rs) 负责向外语 `BleBackend` 派发命令；Apple backend 中创建 `CBCentralManager` 并使用 CoreBluetooth。反向事件从 backend 的 ingress 经 Rust Hub/sink，再由 Swift handoff 投向 Nitro listener。依赖的 Rust 静态库来自 [`crates/blehub-uniffi`](../../../crates/blehub-uniffi/) 的单个生产包，不是 Nitro 自己实现了 BLE。
- **运行证据（iOS）：** [2026-09-28 iPhone RN → nRF RF 报告](../../testing/reports/2026-09-28-react-native-ios-nrf-rf.md) 对实际 JS 发起的扫描、连接、GATT、I/O、RSSI、通知、断开与恢复做了分场景检查；它与上述静态调用路径合起来，证明**该次私有 iOS candidate** 的 RN → Nitro → Swift → UniFFI/Rust `BleHub` → CoreBluetooth → RF 路径。单独的 Swift 单元测试、编译或画面截图不能替代这一端到端证据。
- **代码装配（Android）：** [`HybridBleHubProbe.kt`](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/HybridBleHubProbe.kt) 在 `createHubLocked()` 中把 `DeterministicBackend` 和 `CompletionSink` 交给生产 UniFFI `BleHub`；其 `initialize()` 事件经 Rust ingress 回到 Kotlin listener。RF 方法明确返回 `BACKEND_UNAVAILABLE`。因此当前只建立了 Android RN/Nitro → Kotlin → Rust `BleHub` → **测试 backend** 的可编译/待真机运行链路；没有走到 Android `BluetoothGatt`/`BluetoothLeScanner`，更没有 Android RF 通过证据。将来 Phase 4 的生产 Android Central backend 接入后，仍需对同一最终 artifact 单独执行 Android runtime/RF gate，不能由 iOS 结果或这段测试代码推断通过。

## 名词与文件各管什么

| 名词/文件 | 作用 | 这里的实例 |
|---|---|---|
| RN CLI autolinking | 发现 npm 依赖中的原生工程并接入宿主 | [`react-native.config.js`](../../../integrations/react-native-internal/react-native.config.js) |
| Nitro Modules | RN JS 与原生对象通信的桥层 | [`createBleHubProbe()`](../../../integrations/react-native-internal/src/index.ts) |
| HybridObject | JS 可创建和调用的、由原生代码实现的 Nitro 对象；`Hybrid` 不表示“iOS + Android”或“Rust + Swift” | [`BleHubProbe.nitro.ts`](../../../integrations/react-native-internal/specs/BleHubProbe.nitro.ts) |
| Nitrogen | 依据 TS spec 和 `nitro.json` 生成 C++/Swift/Kotlin 桥接代码 | [`nitro.json`](../../../integrations/react-native-internal/nitro.json)、忽略的 `nitrogen/generated/` |
| CocoaPods / `.podspec` | iOS 宿主集成原生模块和其依赖的入口 | [`BleHubReactNative.podspec`](../../../integrations/react-native-internal/BleHubReactNative.podspec) |
| Gradle / `build.gradle` | Android 宿主集成原生库的入口 | [`android/build.gradle`](../../../integrations/react-native-internal/android/build.gradle) |
| Stable UniFFI | 使用同一 Rust 生产镜像生成 Swift/Kotlin 调用接口 | [`crates/blehub-uniffi`](../../../crates/blehub-uniffi/) |

**有两层容易混淆的 “autolinking”：** RN CLI 根据 npm 依赖发现 Android 工程/iOS podspec；Nitrogen 根据 `nitro.json` 把 JS 的 `BleHubProbe` 注册到 Swift/Kotlin 的实现类。前者负责把包接进 App，后者负责对象创建时找到实现类；有一种不等于另一种已经完成。

## 为什么 Android 写 `sourceDir`，iOS 不写？

本包的 [`react-native.config.js`](../../../integrations/react-native-internal/react-native.config.js) 作为**依赖配置**声明了 `android: { sourceDir: 'android' }` 与 `ios: {}`。

- Android 依赖的工程入口在 [`android/build.gradle`](../../../integrations/react-native-internal/android/build.gradle)。`sourceDir` 指 **Gradle 工程目录**，不是 `src/main/java`。本项目安装的 RN CLI Android 配置在未指定该字段时也会探测包根目录下的 `android/`；因此这里是便于阅读的**显式默认值，不是必填条件**。
- iOS 依赖的入口是包**根目录**的 [`BleHubReactNative.podspec`](../../../integrations/react-native-internal/BleHubReactNative.podspec)。当前 RN CLI 的 Apple 依赖配置从包根目录查找 `*.podspec`；它的依赖侧配置没有对称的 `sourceDir` 字段。`ios/` 只放 Swift 源文件，由 podspec 的 `spec.source_files` 指向它。不能因为目录也叫 `ios/`，就照搬 `android.sourceDir`。
- 不要混淆宿主 App 的 `project.ios.sourceDir`：那是另一种配置，涉及寻找 App 的 Podfile/Xcode 工程，不是这个依赖的 Swift 源文件位置。当前默认发现已够用，`ios: {}` 不表示 iOS 没有原生实现。

以上默认行为已按当前宿主安装的 `@react-native-community/cli-config-android`（`config/index.js`、`config/findAndroidDir.js`）和 `@react-native-community/cli-config-apple`（`config/index.js`、`config/findPodspec.js`）核对；依赖路径在 `tests/consumers/react-native-internal/node_modules/` 下，升级 CLI 后须重查。此前将 Android `sourceDir` 说成“需要配置”过于绝对；将 iOS `sourceDir` 解释为对等的 Swift 源码路径也不正确。

## `nitro.json` 中的名字怎么对上

| 名称 | 类型及匹配位置 | 是否意味着公开 API？ |
|---|---|---|
| `@blehub/react-native-internal` | 内部 npm 包名；[`package.json`](../../../integrations/react-native-internal/package.json) 中 `private: true` | 否；`Internal` 在包名中足够，不必给每个类加后缀 |
| `BleHubReactNative` | iOS `iosModuleName` 对应 podspec 的 `spec.name`；Android `androidCxxLibName` 是 Nitro 原生库名 | 否，属于构建标识 |
| `blehubreactnative` | C++ namespace 段；Android 生成命名空间段，与生成的 `com.margelo.nitro.blehubreactnative` 包及 Gradle `namespace` 配套 | 否；改名须同步生成代码、源码和构建配置 |
| `BleHubProbe` | `autolinking` 的对象名；须与 TS spec、`createHybridObject<BleHubProbe>('BleHubProbe')` 一致 | 否；`Probe` 刻意提示这是验证接口 |
| `HybridBleHubProbe` | Nitro 注册的 Swift/Kotlin 实现类，分别实现 Nitrogen 生成的 `HybridBleHubProbeSpec` | 否；`Hybrid` 是 Nitro 常见的实现类前缀，不是本项目 BLE 业务概念 |

例如，JS 调用 [`createBleHubProbe()`](../../../integrations/react-native-internal/src/index.ts) 用字符串 `BleHubProbe` 创建对象；Nitro 查 [`nitro.json`](../../../integrations/react-native-internal/nitro.json) 中对应平台的 `implementationClassName`，构造 [`ios/HybridBleHubProbe.swift`](../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift) 或 [`android/.../HybridBleHubProbe.kt`](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/HybridBleHubProbe.kt)。TS `interface` 是 JS 侧可见的规格；`HybridBleHubProbeSpec` 是生成的原生规格；`HybridBleHubProbe` 是我们写的实现。不要手改生成代码来解决命名问题。

`ignorePaths: ["**/node_modules"]` 让 Nitrogen 跳过依赖目录；`gitAttributesGeneratedFlag: false` 是生成文件的 Git 属性标记开关，不决定能否生成。当前生成目录由 [`.gitignore`](../../../.gitignore) 忽略。[集成边界](react-native-integration-boundary.md)明确不把测试形状的 `BleHubProbe` 改名为产品对象：仓库保留独立的 `@blehub/react-native-internal` probe 包，将来只发布一个 `@blehub/react-native` 产品包（需先完成 BLeng/API 评审与正式包门禁）。两个包不承诺安装在同一个 App；内部现有 `BleHubReactNative` 等原生构建标识不必为了假想的共装场景提前更名。

## iOS 和 Android 怎么把包接进 App

- **iOS：** RN CLI 发现根目录的 podspec；CocoaPods 按其中的 `source_files` 编译 [`ios/`](../../../integrations/react-native-internal/ios/) 的 Nitro Swift 适配器，并加载 Nitrogen 生成的 `BleHubReactNative+autolinking.rb`。[`apple-pods/`](../../../integrations/react-native-internal/apple-pods/) 则是**供 CocoaPods 装配的两个独立依赖壳**：[`BleHubApple.podspec`](../../../integrations/react-native-internal/apple-pods/BleHubApple/BleHubApple.podspec) 编译从生产 Apple backend 复制来的 Swift 源；[`BleHubUniFfi.podspec`](../../../integrations/react-native-internal/apple-pods/BleHubUniFfi/BleHubUniFfi.podspec) 包含生成的 Swift 绑定和唯一的生产 UniFFI XCFramework。它们由 [`stage-apple-production`](../../../just/react-native/mod.just) 从已构建产物显式填充，再由宿主 [`Podfile`](../../../tests/consumers/react-native-internal/ios/Podfile) 以本地 Pod 引入；不是第二个 RN module，也不是第二份 Rust image。`ios/Package.swift` 仅独立测试纯 Swift handoff，**不会自动替代当前 RN iOS 宿主的 CocoaPods 接入**。
- **Android：** RN CLI 发现 `android/` Gradle library；[`BleHubReactNativePackage.kt`](../../../integrations/react-native-internal/android/src/main/java/com/margelo/nitro/blehubreactnative/BleHubReactNativePackage.kt) 是 RN 加载入口，`getModule()` 返回 `null`，不另外暴露一套 TurboModule API；Nitro 加载器注册 HybridObject，Gradle/CMake 集成其原生库和生产 UniFFI 依赖。
- RN 的宿主 App、签名、UI 和真机运行属于 [`tests/consumers/react-native-internal/`](../../../tests/consumers/react-native-internal/README.md)；集成包不是一整个 RN App。现役 RN 宿主面向 **iOS/Android**，已归档的 RN macOS 试验不代替独立的原生 macOS BLE 验证。将来产品包须有自己的独立下游 App/clean consumer，对实际发布产物验证单镜像、生命周期和 RF；不能用内部 probe 的通过代替。

## 锁文件与生成物

[`package.json`](../../../integrations/react-native-internal/package.json) 写直接依赖和 npm 脚本；同目录 `package-lock.json` 锁定可复现安装所需的完整依赖树。这个包有独立的 `npm ci` 开发流程（见 [`just/react-native/mod.just`](../../../just/react-native/mod.just)），**不应把 `package-lock.json` 加进 `.gitignore`**；`node_modules/`、`nitrogen/generated/`、Gradle/Xcode 产物应忽略。当前本地 `integrations/react-native-internal/package-lock.json` 尚未被 Git 跟踪；“建议保留/提交”不等于它已经提交。供隔离消费者测试的 npm tarball 有显式输入清单，不能把本地 `node_modules` 或构建垃圾带进去。

## 阅读和验证顺序

1. 先读 [`specs/BleHubProbe.nitro.ts`](../../../integrations/react-native-internal/specs/BleHubProbe.nitro.ts) 和 [`src/index.ts`](../../../integrations/react-native-internal/src/index.ts)，了解 JS 看见什么；`initialize()` 返回“是否接受派发”，不代表 BLE 成功，成功/失败还要看事件。
2. 读 [`nitro.json`](../../../integrations/react-native-internal/nitro.json) 和原生 `HybridBleHubProbe`，对照 Nitro 生成的 `HybridBleHubProbeSpec`（本地忽略目录，先运行生成命令）。
3. 读 podspec / Gradle，再看 [`tests/consumers/react-native-internal/README.md`](../../../tests/consumers/react-native-internal/README.md) 的宿主门禁和限制；不要由编译成功推断手机 RF 通过。
4. 从仓库根目录按 [包内 README](../../../integrations/react-native-internal/README.md) 通过 `pixi run just react-native install`、`pixi run just react-native check` 安装/检查；普通检查不需要把 probe 改成公开 API。

Nitro 概念与配置的上游资料：[Hybrid Objects](https://github.com/mrousavy/nitro/blob/main/docs/docs/concepts/hybrid-objects.md)、[Nitrogen / 配置](https://github.com/mrousavy/nitro/blob/main/docs/docs/getting-started/configuration-nitro-json.md)、[Swift/Kotlin autolinking 示例](https://github.com/mrousavy/nitro/blob/main/docs/docs/getting-started/how-to-build-a-nitro-module.md)。对本仓库实际行为，以上述本地锁定版本的源码和已验证产物为准。
