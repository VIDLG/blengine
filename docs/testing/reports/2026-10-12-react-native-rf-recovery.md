# React Native 真机 RF recovery gate（2026-10-12）

## 结论

当前 RN production candidate 在真实 nRF52840 RF fixture 上通过了 Android 和 iOS 的三周期 recovery gate。

每个周期都验证了：

1. scan、connect、GATT discovery；
2. Notification enable 和精确 `ef` value；
3. fixture 触发真实 passive disconnect；
4. 旧 connection/GATT/subscription route 在观察窗口内无 late event；
5. recovery connection 成功；
6. recovery connection disconnect 后无 late event；
7. 下一周期可以重新开始。

这不是 fake Jest evidence。Jest 仍然只负责 RN UI/state-machine；本报告的结果来自 RN/Nitro、production BleHub、平台 backend、真实手机 BLE 和 nRF52840 fixture。

## Android

- Device: `3ac5cc09`
- Service UUID: `fd000000-0000-4000-8000-000000000000`
- Gate: `rf-recovery`, 3 cycles
- Result: passed
- Log: `tests/consumers/react-native-internal/android/build/rf-recovery-runtime.log`

Android host runner 现在支持：

- `rf-recovery`：3 cycles；
- `rf-recovery-ten`：10 cycles 的 host/fixture protocol（本次未执行 10-cycle RF）；
- 每个 cycle 的 fixture readiness、subscription/value/fault/recovery ACK；
- runtime marker 累积，避免 Android `logcat` ring buffer 中的系统噪声覆盖早期 marker；
- recovery-specific timeout、runtime-exit 和 structured checker 调用。

本次第一次 Android 尝试暴露了 host capture 问题：RF 实际完成，但直接保存最后一次 `logcat -d` 快照时，cycle 0 前段 marker 已被系统日志挤出 ring buffer。修复为只累积新的 `BLEHUB_NITRO_RUNTIME` 行后，重新执行通过。没有放宽 checker。

## iOS

- Device: `00008110-000C61160C39401E`
- Service UUID: `fd000000-0000-4000-8000-000000000000`
- Gate: `rf-recovery`, 3 cycles
- Result: passed
- Log: `tests/consumers/react-native-internal/ios/build/device-rf-recovery.attempt.log`

iOS 使用已有 host-controlled recovery runner 和相同的 fixture ACK / structured checker contract；设备在测试前已解锁。

## Diagnostic pending-read gate

本次将一块 DK 切换为专用 maintained diagnostic DK，另一块保持 normal RF：

```text
Diagnostic DK: 001050252028
VCOM: /dev/cu.usbmodem0010502520281
Normal RF DK: 001050251479
Image SHA-256: e7a66e9aebd81495fa77b88ccb15702df3dd0424cf2c192b02a4d68d5a906304
```

J-Link 对 diagnostic DK 报告 `Program & Verify` 成功，目标型号为 nRF52840，VTref 为 3.3V。未刷写另一块 DK；对照读取仍返回 normal GATT command mask `feffbffd0f`。

在 iPhone 解锁并修复 diagnostic subprocess 的 Python module 启动方式后，maintained pending-read gate 通过：

```text
old_hold=EXPIRED:sequence=2:handle=41:gap_disconnected=true
pending_read_reload=passed:old=2:fresh=3:read_characteristics=13,13
```

Evidence log:

```text
.work/nrf-att-hold-maintained-dk/rn-pending-read-reload-8acc72771d3c4a2ca9eb9bbf54c378a6.attempt.log
```

这个 gate 验证真实 iOS/CoreBluetooth pending read、native reload 边界、旧 hold expiry、GAP disconnect 和 fresh generation read。它不等同于独立的 watchdog timeout soak；纯 watchdog timeout 及 physical RF recovery 仍未宣称通过。

本次修复了 `tests/peripherals/nrf52840/diagnostic/device.py` 中使用绝对脚本路径启动 RN runtime 导致 `ModuleNotFoundError: No module named 'tools'` 的问题，改为从项目根目录使用 `python -m tools.react_native.run_ios_device_runtime`。

## 验证

通过：

```text
pixi run python -m unittest discover -s tools/react_native -p 'test_*.py'
Ran 120 tests
OK

pixi run ruff check tools/react_native/run_android_rf.py tools/react_native/test_run_android_rf.py
All checks passed

pixi run just lint
passed
```

Android recovery runner 和测试文件没有 diagnostics warning/error。

## 未覆盖边界

本 gate 不宣称以下能力已经通过：

- 10-cycle recovery soak（Android 本次未执行；iOS 10-cycle 需单独引用对应 evidence）；
- watchdog timeout 及物理 RF recovery；
- adapter off/on transition；
- permission transition；
- engine reload / pending-operation reload；
- 多 DK strict Indication 当前 candidate rerun；
- RN public ABI freeze。

下一阶段优先级仍是 watchdog/recovery 和 adapter transition，而不是重复已经通过的 9-flow suite。
