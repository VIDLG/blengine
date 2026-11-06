# RN iOS REJECT gates：fixed3 / v2-B candidate

日期：2026-10-24。范围：同一个 RN Workbench、同一个 fixed3 iOS App candidate、真实 iPhone Central、DK-B v2-B Peripheral。三类 REJECT 保持独立，不并入 `full-suite`；本报告不表示 Phase 5、生产 ABI 或 iOS continuity 已闭合。

## 固定输入

- iPhone：`00008110-000C61160C39401E`，iOS `26.6.1`，CoreDevice 状态 available/paired。
- App：`tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed3-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app`。
- xctestrun candidate：`tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed3-derived/Build/Products/BleHubNitroHost_iphoneos27.1-arm64.xctestrun`。
- Peripheral：DK-B，BTP `/dev/cu.usbmodem0010502514791`，J-Link `1050251479`。
- Image/profile/service：`nrf-multifunction-rf-v2-b` / `blehub-nrf52840-multifunction-v2-b` / `fd700000-0000-4000-8000-000000000000`。
- 执行均显式传递 `--port /dev/cu.usbmodem0010502514791`；没有使用本机默认 DK-A 端口，也没有刷写、reset 或隐式 provision。

## 结果

| Gate | Attempt | 结果 | 关键事实 |
|---|---|---|---|
| response-write REJECT | `ios/build/rf-write-reject-cc317eafb62e45efafc6cc0953afc4d6` | **passed** | iOS native/App、ATT rejection、unchanged read、recovery、resource-zero 和 physical cleanup 完成 |
| CCC enable REJECT | `ios/build/rf-ccc-enable-reject-8d3d4f3fe6ea4b5a8a32504a45169baf` | **passed** | 7 个控制边界、recovery/silence、resource-zero 和 physical cleanup 完成 |
| CCC disable REJECT | `ios/build/rf-ccc-disable-reject-94a6de60497346c39fa9db8e9a2c039b` | **failed** | 失败只在 rejected disable 后原订阅继续收到 notification 的 continuity 断言 |

前两个 bundle 的 evidence verifier 通过；第三个失败 bundle 的 verifier 也通过完整性校验，并保留原始 `status=failed`，不是 RF 重新授权，也不是成功判定。

## CCC disable：Peripheral TX observation 与 Apple delegate 诊断

为区分 Peripheral 未发、空中丢失和 Apple backend 丢路由，使用同一 fixed3/v2-B 输入再次运行了 opt-in `--observe-notification-tx`，attempt：`ios/build/rf-ccc-disable-reject-228d6920258b4a92b2a8968e26491792`。该 attempt 的 observation capture 完整、cleanup errors 为空，仍按严格 App continuity 判定为 failed。

- `e1` 对应 sequence `4`，value handle `41`；peer、connection epoch、bearer epoch、database epoch 和 CCC selection 全部匹配。
- `submit_attempts=1`、`submit_admitted=1`、`submit_errno=0`、`tx_callbacks=1`、`tx_errno=0`；最终 observation counters 为 `tx_ok=4`、`tx_errors=0`。
- Apple diagnostics 记录了 ATT14 的 `didUpdateNotificationState`，并保持 `isNotifying=true`、原 `subscriptionId=34009`、route phase `active`。
- ATT14 后 FFF1 queued read 的 `value.entry`/`value.disposition=read` 正常出现；在对应时间窗口没有任何 FFF2 `value.entry`，也没有 `value.disposition=notification`。

因此当前证据排除了“Peripheral 没提交/提交失败”和“BleHubApple 收到后错误 route/drop”两类解释；它证明的是 Peripheral 本地 TX callback 成功，但不等同于空中链路 ACK。剩余边界是 iOS/CoreBluetooth 未向 delegate 交付 FFF2 `e1`，或更低层链路/控制器未完成实际空中传输。Apple backend 不应合成缺失的 notification，否则会伪造 BLE 事实并破坏 strict continuity gate。要继续向下归因，需要 HCI/空中 sniffer 或同等链路级证据，而不是放宽 App 判据。

## CCC disable failure boundary

失败 attempt 的 runtime 已证明：

1. scan/connect/GATT discovery 通过；
2. baseline subscription 建立，原 subscription 收到 `ef`；
3. disable 请求返回精确 `CBATTErrorDomain` code `14`；
4. queued FFF1 read 返回 `00`；
5. reject 后 native 仍报告原 subscription/connection 保持；
6. host 发出 `continued-value-sent`，但 App 没有观察到后续 notification；
7. 最终 native resources 全零，fixture cleanup errors 为空。

因此本次不是权限、安装、连接、GATT discovery、DK-B 选择、provider readiness 或资源泄漏失败。失败主错误为 `ccc-disable-reject: native/App failure`，随后 JS wait expired；不能把 firmware 的 local `SET_VALUE` admission 或 CCC reject receipt 当成 iPhone 已收到后续 `e1` 的证明。

该结果与历史 iPhone CCC disable continuity gap 一致。当前结论是：iOS response-write 和 CCC enable 已通过当前 candidate；CCC disable 的 strict continuity 仍未通过，不能用重订阅或合并到 full-suite 绕过。

## 工具架构修正

为避免独立 gate 误选默认 DK，公共链路现在支持显式端口：

- `tools/react_native/run_rf_write_reject.py`
- `tools/react_native/run_rf_ccc_enable_reject.py`
- `tools/react_native/run_rf_ccc_disable_reject.py`
- `tools/react_native/rf_scenario_runner.py`
- `tests/peripherals/nrf52840/provider.py`
- `just/react-native/mod.just`

端口由 CLI/Just 入口传入 `rf_scenario_runner`，再传递给 `provider direct-start` 和其 `direct-serve` 子进程。三个 REJECT Just 入口现在要求显式 `port`，避免回退到本机 local config 中的 DK-A。场景仍共享 runner/capture/ledger/evidence 机制，但保持独立 scenario 和独立 verdict。

## 验证

- `pixi run python -m unittest tests.peripherals.nrf52840.test_provider -v`：30 tests OK。
- 目标 Python 文件 `compileall`：通过。
- `pixi run just --list`：通过，Just 文件解析正常。
- 本次相关文件 `git diff --check`：通过。
- 三个 iOS RF attempt 的 evidence integrity verifier：通过；仅前两个是 passed verdict，CCC disable 保留 failed verdict。

## 下一步

不把 CCC disable failure 合并或降级。下一步应在明确授权后补充 Peripheral notification TX/submission/completion/error 证据，或继续做候选汇总与其他非 destructive gates；在获得能证明 `e1` 到达 iPhone 的新证据前，不宣称 iOS CCC disable continuity 通过，也不进入最终 ABI/Phase 5 freeze。
