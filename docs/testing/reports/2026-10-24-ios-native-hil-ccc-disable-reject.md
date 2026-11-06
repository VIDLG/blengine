# Native iOS HIL Flow 12：CCC disable REJECT 与 CoreBluetooth callback 边界

**日期：** 2026-10-24
**范围：** 独立 Native iOS HIL → production `AppleBleBackend` / CoreBluetooth → nRF52840 Multifunction Fault v2-A，执行 Flow 12 `ccc-disable-reject`。本次不经过 RN/Nitro。

## 目的与判定

本次 HIL 复用 RN Flow 12 的 control、provider physical evidence、runtime transcript 和资源账本，但将 Central 替换为现有 Native iOS HIL App，直接调用 production Apple backend：

- Native HIL 收到 `E1`：问题继续定位到 RN host、Nitro bridge、runtime 或生命周期；
- Peripheral 已通过物理证据发送 `E1`，但 Native HIL 也没有收到 `E1`：归类为 iOS/CoreBluetooth callback delivery gap；
- 不合成 `E1`，不把 provider submission 当作 App delivery，不修改固件，也不放宽 continuity assertion。

Flow 12 的控制阶段仍保持：`baseline-value-sent`、`armed`、`held`、`rejected`、`continued-value-sent`、`retry-allowed`、`silence-started`、`close-allowed`。本次在 `rejected` 后等待 Native CoreBluetooth 的 `E1` callback 超时，因此后续 continuity/close 阶段不能被标记为通过。

## Candidate 与设备

- image：`nrf-multifunction-rf-v2-a`
- normalized image SHA-256：`9b97e9e0b332930de940e2c7c4ff2331e11d402e7f7f75215da870eef00ce7e1`
- DK-A J-Link：`1050252028`
- Native Central：iPhone 14 Plus，iOS `26.6.1`
- Native HIL bundle：`dev.blehub.fwvyq287w5.ios-probe`
- 独立 sniffer：本次 Native HIL 未启动同步抓包；同一 v2-A candidate 的先前 RN 空口证据见 [RN iOS v2-A sniffer 报告](./2026-10-24-rn-ios-ccc-disable-v2-a-sniffer.md)

## Native HIL 实现

本次实现没有建立第二套 BLE 业务协议或测试判据：

- `tests/hil/apple/support/CccDisableRejectScenario.swift` 负责 Darwin control relay、Flow 12 阶段 ACK、production backend event transcript、native resource snapshot 和错误投影；
- `tests/hil/apple/support/AppleCoreBluetoothRfScenario.swift` 复用现有 Native HIL 的 scan/connect/GATT/notification 路径及显式 command IDs；
- `tests/hil/apple/ios/BleHubAppleIOSProbe/AppDelegate.swift` 仅增加 `ccc-disable-reject` 场景分支并保留失败 transcript 的 boot identity；
- `tools/apple/run_ios_native_hil_ccc_disable.py` 负责 immutable fixture session、provider、设备安装/启动、Darwin relay、进程 identity、结果收集和 cleanup；
- `just/apple/mod.just` 提供显式入口 `rf-ios-native-hil-ccc-disable-reject`。

Native route/command identity 与 Flow 12 contract 对齐：scan `34001`、connection `34004`、GATT `34007`、subscription `34009`；connect command `34005`、GATT discovery command `34008`。GATT discovery transcript 包含目标 service、五个 characteristic、CCCD descriptors、properties 和 opaque IDs，未使用硬编码 Peripheral address。

## 运行身份与证据

最新成功完成到失败边界的运行目录：

```text
.work/apple-ios-native-hil/71a31094-708d-4882-b3f1-94a8c416fa6f/
```

关键 identity：

```text
native boot id: 6799fa11-5c2e-4477-a059-9a13cde32ef9
native pid: 1952
fixture pid/pgid: 31293 / 31293
control session: bb9fb6dd-c679-43c1-8430-8eef949311a1
action token: 3e27811c-27d8-45e7-a1a0-6df44064f018
```

原始证据保存在：

- Native result：`.work/apple-ios-native-hil/71a31094-708d-4882-b3f1-94a8c416fa6f/native-hil-result.json`
- Native identity：`.work/apple-ios-native-hil/71a31094-708d-4882-b3f1-94a8c416fa6f/native-hil-identity.json`
- runtime transcript：`.work/apple-ios-native-hil/71a31094-708d-4882-b3f1-94a8c416fa6f/coordination/runtime.log`
- provider physical evidence：`.work/apple-ios-native-hil/71a31094-708d-4882-b3f1-94a8c416fa6f/coordination/ccc-disable-reject-physical.json`

Provider 已清理，运行结束后没有保留 owner。上述 `.work` 文件是本机运行证据，不是可由报告文本替代的 RF 证明。

## 观察到的顺序

Native runtime transcript 证明了以下前置边界：

1. iOS adapter 为 `PoweredOn`，scan/connection authorization 为 `Authorized`；
2. Native HIL scan、connect、GATT discovery 成功；
3. 目标 `FFF2` notification enable 成功；
4. Native HIL 收到 baseline `EF`，并发出 `baseline-value-sent` / `baseline-notified` 对应 ACK；
5. Native HIL 发出 disable request；
6. provider control 观察到 `rejected`；
7. production Apple backend 将失败投影为：

   ```text
   category=gatt
   platform=platform_apple
   name=CBATTErrorDomain
   code=14
   message=Unlikely error.
   ```

8. 同一连接上 queued read 仍能完成，说明本次结果不是简单的连接断开或 App 进程消失；
9. provider physical evidence 记录 Peripheral 已提交/发送继续值 `E1`；
10. Native event transcript 没有对应的 `notificationReceived` `E1`，等待超时，结果分类为：

    ```text
    ios-corebluetooth-callback-missing
    ```

本次 Native HIL 没有产生同一运行的 sniffer PCAP。因此，`E1` 的空口发送结论只能引用同一 v2-A candidate 的既有独立 sniffer 运行，不能声称该 PCAP 与本次 Native HIL 是同一 session。既有 PCAP 已在 [RN iOS v2-A sniffer 报告](./2026-10-24-rn-ios-ccc-disable-v2-a-sniffer.md) 中确认：`CCC disable 00 00` 后 Peripheral 发送 `E1`。

## Verdict 与边界

- Candidate identity / contract：本次运行使用最新 v2-A candidate；provider 物理身份和控制 session 已关联；
- Native iOS HIL 前置 RF 路径：scan、connect、GATT discovery、enable、baseline `EF` 成功；
- CCC disable REJECT：失败，CoreBluetooth 返回 `CBATTErrorDomain` code 14；
- Peripheral `E1`：本次 physical evidence 记录已发送，但 Native HIL 未收到对应 value callback；
- RN/Nitro：被本次 production Native HIL 隔离，当前证据不支持继续把问题归因于 RN/Nitro；
- iOS CCC disable continuity gate：保持 `pending`/`failed`，不能标记为 passed；
- 同步 PCAP：缺失，不能用既有 RN PCAP 伪装为本次 Native HIL 的同-run 空口证据。

因此，当前最准确的归类是：**iOS/CoreBluetooth 在 CCC disable 请求失败后，对 Peripheral 继续发送的 `E1` 没有向 production native delegate/backend 交付 callback 的实现行为缺口**。Apple 官方 CoreBluetooth 文档描述 notification state failure 与 value callback 的普通 API 责任，但没有承诺“disable 失败后仍发送的 value 必须继续触发 `didUpdateValueFor`”。这份报告记录的是实测边界，不把它升级为 Apple 文档保证。

不采取以下规避方式：

- 不合成或补发缺失的 Native event；
- 不把 provider `SET_VALUE`/submission 当作 Central delivery；
- 不修改 v2 固件以绕过异常；
- 不放宽 `E1` continuity assertion；
- 不把 radio gate 或 RN 页面结果重标为本 Native HIL gate 的通过。

## Validation

本次 adapter 相关验证已完成：

- unsigned iOS HIL build：`apple_ios_hil_unsigned_build=passed`；
- Flow 12 provider tests：`6 passed, 32 subtests passed`；
- Python `ruff` / `py_compile`；
- candidate contract/profile strict validation；
- `git diff --check`；
- provider cleanup：`provider_identity=None`。

后续只需对最新 Swift/Python 工作树做轻量回归；不应为了重复确认同一 callback gap 而自动重跑该破坏性 RF 场景。若将来需要同-run PCAP，应作为新的、明确授权的 sniffer-coupled RF 实验单独登记，不能改写本次结论。

## 相关文档

- [RN iOS v2-A 空口与 CoreBluetooth delegate 边界](./2026-10-24-rn-ios-ccc-disable-v2-a-sniffer.md)
- [Flow 12 / CCC disable REJECT 架构与 provider 证据边界](../../research/react-native/react-native-rf-lab-test-architecture.md)
- [Apple 生产 backend 与 iOS HIL 责任边界](../../testing/apple.md)
