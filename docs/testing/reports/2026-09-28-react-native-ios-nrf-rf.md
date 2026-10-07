# React Native iOS Nitro → nRF52840 RF 验证

日期：2026-09-28

## 范围

本报告记录 repository-owned private React Native New Architecture + Nitro module 经由生产 Apple backend、Stable UniFFI image 和 standalone nRF52840 DK 的 iPhone RF 证据。

真实设备：

- iPhone 14 Plus：`00008110-000C61160C39401E`
- Apple Development team：`FWVYQ287W5`
- Peripheral：standalone nRF52840 DK，当前 active serial 为 `/dev/cu.usbmodem0010502520281`
- RN service filter：`fd000000-0000-4000-8000-000000000000`

这不是 native Swift/CoreBluetooth HIL 的重标记，也不是 BLeng/public RN API 证据。它验证的是：RN JavaScript → Nitro HybridObject → Swift production adapter → Stable UniFFI production image → `BleHub` → CoreBluetooth → nRF52840 DK。

## Runner 修复

本轮首先发现 RN RF runner 通过脚本绝对路径启动 `run_ios_device_runtime.py`，在真实调用中导致 Python 无法解析仓库 `tools` package。启动方式已改为：

```text
python -m tools.react_native.run_ios_device_runtime
```

同时，长等待控制统一使用低噪声阶段输出：

- 阶段开始/通过各输出一行并包含 phase elapsed；
- 超过 15 秒仅输出低频 heartbeat，并记录最后观察到的 marker；
- `BLEHUB_NITRO_RUNTIME` 的 `failed`/`failure` marker 立即终止；
- coordination marker 等待同时检查 runtime failure 和非零退出；
- runtime 正常 exit 与 coordination 文件落盘之间保留 5 秒有界 flush grace；
- fixture、runtime 和已发布日志仍按原有 ownership/rollback 规则清理。

首次 reload 运行暴露了 exit-0 与第二代 coordination 文件落盘的竞态；修复后 reload 重新通过。该竞态不被记录为 RF 失败。

## 通过矩阵

以下场景均在同一构建候选、同一 iPhone、同一 nRF DK 上执行并通过：

| 场景 | 关键证据 |
| --- | --- |
| scan/connect baseline | `PoweredOn + Authorized`、过滤后 advertisement、scan stop 后 2 秒 silence、`Connecting`/`Connected`/disconnect/connection silence |
| GATT | aggregate discovery，`gattSessionId=9009`，2 services、5 characteristics、2 descriptors |
| I/O | initial `00`、write-with-response `10`、readback `10`、write-without-response `ab`、本地 submission terminal 与后续 `ab` readback |
| connected RSSI | 本次实测 `-63 dBm` |
| Notification | actual mode `Notification`、精确 `ef` value、disable 后 2 秒 silence |
| strict Indication | 双属性 characteristic rejection `not_supported`、纯 Indication characteristic actual mode `Indication`、精确 `ef` value、disable 后 2 秒 silence |
| passive disconnect | active Notification route、`commandId=none` passive disconnect、断开后 silence、recovery connection/disconnect |
| recovery | 同一 RN probe/listener 连续 3 cycles，cycle 0/1/2 全部通过 |
| reload | 两代 active subscription、native reload trigger、fixture-host disconnect、两代 value/active ordering、最终日志检查通过 |

每个 scenario 都由 `tools/react_native/check_runtime_log.py` 做最终结构化 marker/order 校验；不是根据 iPhone Mirror 的画面判断。

## 结论与边界

- RN/Nitro iOS low-level internal RF gate：通过。
- iOS RN clean artifact、signed runtime、XCUITest contract-lab 和 RF 现在各自都有独立证据。
- 该结果不替代 2026-09-28 native Swift/CoreBluetooth iPhone → nRF RF 报告，也不覆盖 macOS RF 或 Android RF。
- private internal module 仍不是 public RN package；BLeng 的 actor、event projection、operation completion 和 close 语义完成并评审前，不建立正式 UI-facing RN API。
- Android RN RF 仍待 Android 设备/Peripheral 条件满足后独立验证；RN macOS 宿主已归档，不是当前 gate。
