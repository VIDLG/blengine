# RN RF evidence bundle：冻结安装快照与两端新运行

日期：2026-10-14

## 后续当前入口

后续新增原生 engine 生命周期 owner 和 `pending-read-reload` 场景，Android 真实两代 pending read/reload 门禁已通过（`10b3686e19e84146a4d9dc21ee1d9cdb`），同一更新 APK 的 continuous10 回归也通过（`73174345d0244b52893f9957ca626ad9`），两者均保留页面且 bundle 完整性复核通过。最新机制、候选与证据见 [Android pending-read reload 报告](2026-10-14-android-pending-read-reload.md)。下文保留首次统一 bundle 的两端实测：旧 iOS 产物不继承为本次 private Nitro 修改后的同候选 RF 通过；历史 manifest 不回写，旧 v1 缺少 `scenario` 的 bundle 仅以只读 continuous10 兼容核验。

## 本阶段结论与范围

**统一 bundle 已实现；Android 与 iOS 各自新的 continuous10 均为 REAL 10/10、host passed、`app=kept-open`，最终 `evidence-bundle.json` 均为 `status=passed`，CLI 离线完整性复核均通过。**不是为历史 RF backfill manifest，也不是拼接 normal9 与独立 watchdog。

本轮只更新 host 测试架构，未从 RF App 源码重 build、未 flash firmware、未改变 native ABI；runner 将已构建的 App 复制为本次 `attempt/candidate/` 快照，再安装该快照并实际执行新 RF。相同 artifact hash 不表示复用历史运行：本次有新的 run UUID、runtime session、fixture live receipt、物理 sequence 和十项 raw-log 索引。没有新 commit。

实现依据：

- 共享模块与离线 CLI：[`tools/react_native/rf_evidence_bundle.py`](../../../tools/react_native/rf_evidence_bundle.py)。
- 两端接入：[`run_android_rf_lab_suite.py`](../../../tools/react_native/run_android_rf_lab_suite.py)、[`run_ios_rf_lab_suite.py`](../../../tools/react_native/run_ios_rf_lab_suite.py)。
- 25 个 synthetic tests：[`test_rf_evidence_bundle.py`](../../../tools/react_native/test_rf_evidence_bundle.py)。它们不代表 BLE/RF 测试。
- 架构与剩余差距：[`react-native-rf-lab-test-architecture.md`](../../research/react-native/react-native-rf-lab-test-architecture.md)。

首轮十项及 optional-localName 修复保留在 [continuous10 历史报告](2026-10-14-rn-continuous10-rf.md)，固件身份与早期失败保留在 [multifunction 历史报告](2026-10-14-multifunction-nrf-rn-rf.md)；本报告不改写其候选或为其补造 bundle。

## 已落地的证据链

| 环节 | 当前行为 |
|---|---|
| 安装输入冻结 | stable regular-file/tree 读取；source 复制前后 fingerprint、协作式 stat signature 与 candidate fingerprint 必须一致；安装和后续检查只消费本次快照，不重新打开 mutable build source |
| Android install identity | `--apk` 现在必需，不接受未 fingerprint 的已安装 App；使用 pinned SDK `aapt2 dump badging` 精确验证 package `com.blehubnitrohost` 与 launchable activity `com.blehubnitrohost.MainActivity`，保存 badging 原文和 identity JSON |
| iOS install identity | 校验 `Info.plist` 的 `CFBundleIdentifier=org.reactjs.native.example.BleHubNitroHost`、Mach-O executable/execute mode 与 JS bundle；安装冻结 `.app` |
| Artifact inventory | Android 保存整个 APK SHA-256/size/mode、所有 ZIP item 的内容 hash/size、JS/DEX/native 路径，逐 ABI 要求 UniFFI/Nitro image；iOS 保存完整 app-tree bytes/modes 与 canonical inventory digest，包含 debug dylib、framework Mach-O 和 JS |
| Manifest provenance | run UUID、host/OS/device、git HEAD/dirty flag、copied artifact fingerprint、全部 attempt 内 raw files 的 hash inventory；runtime identity、fixture request/live receipt、两项 physical evidence 以 hashrefs 关联 |
| 十项索引 | 固定 mode 顺序，每 flow 的 status/source/start/end/terminal line、原始 byte SHA-256/size；保留 native/timestamp 前缀和原始换行，不规范化后再 hash |
| 收尾与错误 | owned cleanup 后原子发布 final manifest；primary/cleanup/evidence error 或缺必需证据均 fail closed。runner 捕获 finalization 的普通 `Exception`，在已有 primary error 上附 note，不能遮蔽原始 RF 错误；没有 primary error 时该异常令运行失败 |
| Partial failure | 已建立 bundle 后即使缺 candidate/log/flow，也保留 failed manifest、缺失项与已有 raw evidence，不填造 RF pass。未建立 attempt/bundle 的硬件/参数 preflight、并发锁拒绝不保证有 bundle；文件系统发布失败只能 best-effort |

指纹/快照保留 **bytes 与 modes**，不复制或 fingerprint xattrs/timestamps；source stat timestamps 仅用于协作式 drift detection。这不是完整文件系统等价证明。

APK 是按 ZIP **case-sensitive** 路径检查并整包复制，不解包；合法 `res/9N.9.png` 与 `res/9n.9.png` 可共存并分别 hash。exact duplicates、file/directory 同名冲突、traversal/不安全路径、symlink/special entry、加密和 size bounds 违规均拒绝；iOS tree 拒绝 symlink/special entry。入口数最多 20,000、单文件最多 1 GiB、累计最多 2 GiB−1 B，文档/日志读取另有上限。

## 本次真机结果

两端 manifest 均记录 host `Darwin 25.6.0 / arm64`、git HEAD `75edda34b20b043ef282d7d94614c9a1169c6179`、`dirty=true`、`source_to_build_attestation=false`。这是运行时的 host checkout 上下文，不证明已构建 App 来自该源码状态。

| 项目 | Android | iOS |
|---|---|---|
| Attempt | `e81103e7816a42c0829af6fdfab5e9ed` | `f0c851c8e19b41e49dd83df7a042d5f8` |
| Run UUID | `4db8a722-b0f1-47fd-a972-444984fce39b` | `37ec1884-f32f-42dc-babe-9dcf4114792c` |
| Central | `3ac5cc09` | `00008110-000C61160C39401E` |
| Runtime session | `971b56d3-e5d1-46c2-93a0-ff08ae5a82a4` | `22b6075a-3806-4a95-9e87-3586e2ac9477` |
| Runtime nonce | `mv80ovk3-8q4pk2hgovf` | `mv80scme-24at1z99zig` |
| 固定 App PID | `17576` | `42132` |
| iOS boot / ACK | 不适用 | boot `015ba0bd-ab25-4b59-95b4-39a4f5855ff1`；七组 native/JS ACK 全部 passed |
| Fixture PID/PGID | `46032/46032` | `47190/47190` |
| Physical resource | `jlink:1050252028` | `jlink:1050252028` |
| Cancellation / watchdog sequence | `18 / 19` | `20 / 21` |
| Target handle | `41` | `41` |
| Watchdog post-release old-route silence | `1009.317584 ms` | `1009.727167 ms` |
| Recovery read | exact `00` | exact `00` |
| RF / host / App | `10/10 / passed / kept-open` | `10/10 / passed / kept-open` |
| Final manifest / offline verify | `passed / passed` | `passed / passed` |
| Raw-file inventory | `63` | `71` |
| Primary / cleanup / evidence errors | `null / [] / []` | `null / [] / []` |

同一 multifunction DK-A image 的 installed identity 由每次租约内新 BTP readback 证明：`nrf-multifunction-rf-v1-a`，normalized image SHA-256 `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a`。每端各自一个 provider lease/BTP session/live receipt；十项期间无中途 App relaunch、BTP reopen、GATT reset 或 flash。上述机器身份只描述具名 attempt，不是通用命令默认值。

十项固定为 `rf`、`rf-connect`、`rf-gatt`、`rf-io`、`rf-rssi`、`rf-notification`、`rf-indication`、`rf-passive-disconnect`、`rf-cancellation`、`rf-watchdog`。每端 start/close session/nonce 一致、十项 ordered exactly once，最终 summary 为 10 passed / 0 failed。

### Android artifact 与入口

- Manifest：[android/build/rf-lab-suite-e81103e7816a42c0829af6fdfab5e9ed/evidence-bundle.json](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-e81103e7816a42c0829af6fdfab5e9ed/evidence-bundle.json)。
- Snapshot：该 attempt 的 `candidate/app-release.apk`。
- APK SHA-256：`e659814cde504fc2d4eb759c23cdb436967e752f62a3f11dce7d4d59040d7f58`；`37,691,882` bytes。
- JS `assets/index.android.bundle`：SHA-256 `b2304fd625a1363d92ec01dae65caa7abf150d82148367f4a19852b9c3973647`。
- `classes.dex`、`classes2.dex` 与全部 native items 均入 inventory；`arm64-v8a`、`x86_64` 各有 `libblehub_uniffi.so` 和 `libBleHubReactNative.so`，即 UniFFI/Nitro 各两 ABI slices。设备真机 RF 不证明 x86_64 执行。
- `artifact-badging.log` / `artifact-install-identity.json` 保存 package/activity 检查；`rf-lab-suite-continuous-runtime.log`、`.raw.log`、`.identity.json`、`.capture.json` 均由 manifest inventory 关联。PID 在本次 runtime capture 中固定，未把仅有 package name 当作 process identity。

### iOS artifact 与入口

- Manifest：[ios/build/rf-lab-suite-f0c851c8e19b41e49dd83df7a042d5f8/evidence-bundle.json](../../../tests/consumers/react-native-internal/ios/build/rf-lab-suite-f0c851c8e19b41e49dd83df7a042d5f8/evidence-bundle.json)。
- Snapshot：该 attempt 的 `candidate/BleHubNitroHost.app`。
- App-tree SHA-256：`4f7fafc68d06a1f986a15e37d9541016a51955c975518f1cd70b628277f728f7`。这是 canonical tree inventory digest，不是某个单独 executable 的 hash。
- `main.jsbundle` SHA-256：`0681b8b25412b2f4b90820b6feec9a005aad5894437f134813a91f609e50812e`，与此前已构建 JS 相同。
- Native inventory 包括 `BleHubNitroHost`、`BleHubNitroHost.debug.dylib`、`__preview.dylib`，以及 `React.framework/React`、`ReactNativeDependencies.framework/ReactNativeDependencies`、`hermesvm.framework/hermesvm`；不是只 fingerprint 主 executable。
- `device-rf-lab-suite-continuous.attempt.log` 为 manifest 的 flow-index source；安装 JSON、controller log、runtime identity/ACK 文件、continuous log 副本均在 raw inventory。七组 ACK 为 flow 8 `armed/held/released` 与 flow 9 `armed/held/released/recovery`。

两端共同的 relative references 包括 `coordination/fixture-session/fixture-request.json`、`fixture-receipt.json`，以及 `coordination/flow-8-rf-cancellation/physical-evidence.json`、`coordination/flow-9-rf-watchdog/physical-evidence.json`。具体 contract/profile 快照、image identity、tokens、native terminals 和 cleanup 以各自 manifest 引用的原始文件为准。

### 保留的失败，不计 RF

Android `9c6e9038f9154362a711de88c0f364cc` 的 [failed manifest](../../../tests/consumers/react-native-internal/android/build/rf-lab-suite-9c6e9038f9154362a711de88c0f364cc/evidence-bundle.json) 记录 `duplicate APK entry: res/9n.9.png`：这是 freeze 将合法大小写资源误拒绝，**未进入 RF**。其 candidate 未绑定、runtime log 缺失、flow 标为 missing，raw inventory 为 0；这些缺失如实保留，不是 RF fail，也不是 RF pass。修复 case-sensitive ZIP 检查后使用新 attempt `e811…`，不回写旧失败文件。

## 执行与复核命令

以下两条已对本次最终 manifest 执行并通过，无硬件访问；成功输出为 `evidence-bundle: integrity verified (not RF reauthorization)`：

```sh
pixi run python -m tools.react_native.rf_evidence_bundle tests/consumers/react-native-internal/android/build/rf-lab-suite-e81103e7816a42c0829af6fdfab5e9ed/evidence-bundle.json
pixi run python -m tools.react_native.rf_evidence_bundle tests/consumers/react-native-internal/ios/build/rf-lab-suite-f0c851c8e19b41e49dd83df7a042d5f8/evidence-bundle.json
```

Focused synthetic tests 本次复核为 **25 passed**：

```sh
pixi run python -m unittest tools.react_native.test_rf_evidence_bundle
```

Runner 相关聚焦测试为 **47 passed**，与上述 25 个 focused tests 分开记账。最终实际运行 `tools/react_native` 全量 unittest discovery 为 **255 tests passed**；`pixi run just peripheral nrf52840-test` 为 **109 tests passed**。`pixi run just fmt`、`pixi run just lint` 均通过；初次 lint 对 finalization 边界的广义异常捕获提出 `BLE001`，该边界必须保留任意普通收尾异常而不覆盖 primary RF error，已用局部带理由的 noqa 明确意图，并以 `zlib.error` 回归验证。没有新增生产依赖。最终 Mermaid 检查通过（129 个 Markdown 文件、24 个图）、`git diff --check` 通过、四份相关文档的 UTF-8/围栏/尾随空白/冲突标记检查通过。新增 `pixi run just react-native host-rf-evidence-check <manifest>` recipe 已对两端最终 bundle 实际执行并通过。

需要对同一已构建 App 做下一次真机运行时，可使用直接 runner（示例为本次具名设备和 DK-A contract；需要有效本机 fixture 配置与可用/解锁设备，会重新安装 App、取得独占租约并实际运行 RF）：

```sh
pixi run python -m tools.react_native.run_android_rf_lab_suite 3ac5cc09 --fixture-contract tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json --fixture-profile tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json --apk tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk
pixi run python -m tools.react_native.run_ios_rf_lab_suite 00008110-000C61160C39401E --fixture-contract tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json --fixture-profile tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json
```

Android 直接入口必须显式给 APK；iOS 直接入口从配置的 host build `.app` 冻结。两者都先验证 live fixture proof，再安装候选快照，默认 kept-open。`just react-native host-android-rf-lab-suite` / `host-ios-device-rf-lab-suite` 高层 recipe 还会触发 App build，不能把这类 recipe 描述为本轮“未重建 App”的操作；本轮结果指直接 runner 对现有 artifact 的 snapshot install/new RF。普通 run 不刷机。

## 限制与下一步

- **Offline verify 只验证 hash/inventory/引用和十项 marker 结构。**它不重复 BLE 语义断言、不 replay native BLE、不校验本次物理授权有效性，也不重新授权 RF；真实语义与 live receipt guard 仍由在线 runner/checker 判定。failed manifest 的离线完整性通过也不把失败变为 RF pass。
- **不是签名或安全证明。**manifest 本身未建立签名信任链，hash 可用于发现 evidence drift，不是恶意同 UID 篡改下的 attestation；不能据此声称 signing/security gate 通过。
- **不是 installed binary hash 或 source-to-build attestation。**证明的是 host 提交安装的冻结快照；device install/runtime identity 不等于设备端 App bytes 的 hash。HEAD/dirty flag 与源 stat signature 不证明 source-to-binary correspondence，已有 artifact 也不升级为 clean locked release。
- **统一 bundle 已完成，统一语义模型未完成。**domain/terminal events 仍在 raw log，watchdog 有专项 ledger；尚无覆盖每 flow 的统一 typed semantic ledger/native BLE replay。后续应在不替代 raw evidence、不扩大 native 语义的前提下补该层。
- Peripheral `RELEASED` 证明 hold 释放，不证明 Android/CoreBluetooth 晚 callback 已观察；隔离依据仍是旧路由 silence、exactly-once native terminals 与 recovery read。本次 10/10 是十项不同 flow，不是十次 recovery/soak。
- adapter/permission transitions、Android pending-operation reload parity、current-candidate 10-cycle recovery、clean locked candidate 与最终同 candidate native/package/RN gates、public ABI freeze 仍未完成。后续推进这些门禁时各建新 bundle，不 backfill 本轮或更早历史。
