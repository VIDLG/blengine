# Android RN：真实蓝牙 OFF → ON 与 RF 恢复

报告日期：2026-10-16；设备执行记录使用 UTC。初始功能 passing attempt 为 2026-10-15 23:11:26.660840–23:11:38.584658；下文另列 RF Lab UI 集成后的最终同 APK 新候选，真实设备导航/截图检查仍待补充，不跨 APK 继承 RF 结果。

## 结论

OnePlus 11 与 DK-A 的真实 adapter-transition gate **passed**，host runtime/physical/state journal/bundle 与离线完整性复核通过，`app=kept-open`。一个 App PID、一个 JS runtime，OFF/ON 两段各自 fresh Probe/Hub；这是独立 opt-in gate，不是新 App，也不把 radio mutation 自动加入非破坏性 full-suite。

链路仍为 RN → Nitro → production BleHub/Android backend → 手机 radio → live-attested multifunction DK。没有 flash、GATT image 切换或 fake RF。初始功能 passing attempt 仅修复 host 收尾读取状态日志的用途错误，没有更改该场安装的 App、native backend 或固件。

## 已核验身份

证据目录：[`rf-adapter-transition-dc2b86db4afa4e0e99b202d5237c68d4/`][attempt]。

| 项目 | 实际值 |
|---|---|
| Bundle run UUID | `bd80287f-7e22-4a3f-b631-b2a7f2011a9d` |
| Runtime/fixture/control session | `8733e019-51b7-4b61-966f-f0ada403b5c4` |
| 手机 | OnePlus 11 / `3ac5cc09` |
| App PID / native boot | `25314` / `7c2e7d38-f226-456b-a187-df41810e6b57` |
| JS nonce | `mva5ksgd-3jw7wjeuwwk-xuw9azcmlr` |
| Fixture resource / PID/PGID | `jlink:1050252028` / `89357` / `89357` |
| Profile / image | `blehub-nrf52840-multifunction-v1-a` / `nrf-multifunction-rf-v1-a` |
| Image SHA-256 | `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a` |
| APK SHA-256 / size | `6975ce535d640bac178a295bd3292edec8d8ac2fd2b72b94e8ac7ea5b2ae65ac` / `38,090,962` bytes |
| Root service | `fd500000-0000-4000-8000-000000000000` |

Bundle UUID 与 runtime/fixture session 是不同身份。APK 从本轮输入冻结后安装；旧 full-suite APK 不含 adapter-transition mode，未拿它代跑。本场 APK 指纹不是设备端 binary byte 回读、签名信任链或 source-to-build attestation，也不继承此前不同 APK 的完整 RF 矩阵结果。

## 真实验证边界

1. Host 持有 phone-specific owner，先读回初始 `ON`；不会自动修正一台原本 OFF 的手机。
2. 相同 fixture lease 下完成 live image receipt，安装冻结 APK，确认 Bluetooth runtime permissions。
3. 只发一次关闭命令，轮询读回真正 `OFF`；此时启动本场 App。
4. OFF 段有精确三条事件：`adapterSnapshotChanged(PoweredOff, Authorized, commandId=31000)`、initialize terminal、`commandId=31002` 的 `adapter_unavailable` terminal。未伪造 scan/RF 成功。
5. OFF Probe close 后 17 项逻辑资源全零。Provider 确认 hold inactive、无 peer 后发布 ON-window ready。
6. Host 只发一次开启命令，确认 `ON` 后发一次同 PID control URL；native/JS ACK 和进程/executable identity 持续校验。
7. 新 Probe 通过真实 scan/connect/GATT/I/O：initial read `00`；write-with-response 后 readback `10`；write-without-response local terminal 后另行 readback `AB`。无响应写 terminal 本身不升级为远端应用确认。
8. 物理证据有一对对应 GAP connected/disconnected；ON Probe close 后 17 项逻辑资源也全零。
9. 收尾独立读回手机 `ON`，hold inactive、peer count `0`、advertising stopped；cleanup/evidence errors 均 `[]`，`primary_error=null`。
10. Logcat detach 后的 `after-logcat-detach` process receipt 仍确认原 PID `25314` 和 exact executable，支持结果采样时 App kept-open，不保证无限期存活。

Radio journal 的四个 action 为 `initial ON / off OFF / on ON / final ON`，receipt 数分别 `1 / 7 / 5 / 1`；off/on 的额外 receipt 是状态轮询，不是重复 mutation 命令。Ledger 计数为一个 App runtime、两个 Hub/Probe lifecycle、两个 closed-zero snapshot。逻辑计数不证明 OS heap/CoreBluetooth/Android framework 内部对象全部销毁。

主要原件：`adapter-state-control.json`、`adapter-transition-runtime.identity.json`、`adapter-transition-runtime.log` / `.raw.log`、`adapter-transition-ledger.json`、`coordination/adapter-transition-physical.json` 与 fixture-session request/receipt，均由 [bundle][bundle] hash 绑定。它们是合作式 host/provider 记录，不是独立空口抓包。

## 先失败、修复后新跑

首个 attempt `4218a2e112cd4d68a5482567d3e95748` 的 OFF/ON、恢复 I/O 与 native close markers 已完成，但收尾错误地用 fixture-session 的 1 MiB document reader 读取 `adapter-state-control.json`。真机 BluetoothManager dumpsys journal 为 `2,111,277` 字节，超过其限额，因此整场保留 **failed**，不因 App 上显示 passed 就改判。

根因是文档用途不匹配，不是链接文件或 radio 恢复失败。修复 [`run_android_adapter_transition.py`](../../../tools/react_native/run_android_adapter_transition.py) 的单一读取点：用现有 immutable bounded evidence reader（8 MiB 场景预算）及 strict JSON parser 校验 state journal，保留 regular/symlink/大小/内容稳定性与 duplicate-key 检查。**未提高 fixture/attestation 的 1 MiB 上限，未截断状态原文，未重写首个 attempt。**

新增 [`test_large_radio_journal_is_not_limited_as_a_fixture_contract`](../../../tools/tests/react_native/test_run_android_adapter_transition.py) 用超过 1 MiB、各次 dump 仍小于 command 限额的合成 journal，执行真实读取与独立 schema/state checker，保护此真机回归。修复后才重新建立 passing attempt；其 journal 为 `2,061,189` 字节。

## 运行与软件检查

本次实际 RF 用已有 APK，未重新 build：

```text
pixi run python -m tools.react_native.run_android_adapter_transition --device <serial> --apk <APK> --fixture-contract <contract> --fixture-profile <profile> --timeout 180
```

VCOM 通过本机环境 `BLEHUB_NRF52840_PORT` 提供，不写入共享脚本。普通 run 不刷机。正式 build-and-run recipe 为：

```text
pixi run just react-native host-android-rf-adapter-transition <serial> <fixture-contract> <fixture-profile>
```

该新 recipe 本轮只作 dry-run；真实 RF 来自上述 frozen-artifact runner，不补记未执行的构建。最初完整 Android device-select 预检曾 45 秒超时，原因未确定；后续独立有界 ADB version/devices 检查确认官方 Platform-Tools `37.0.1` 与手机 `device` 状态，未为此修改工具链选择或安装任何工具。

已运行：parent 专项 12 tests、RN/provider 同进程 **753 tests passed**（含本回归，不相加重复计数），passing bundle 的 `host-rf-evidence-check` 成功。离线校验不重新授权 RF。

## RF Lab UI 集成后的最终同 APK 候选

RF Lab 页面已统一为 Tests → Current run → Evidence；普通启动不创建 Probe，场景由既有 App 路由与 runner 执行。最终冻结 APK **`3cff6dcc8da044b5e76eef12032ea86a44cb9798fc3d3d0ceed605f618769676`** 的 startup-off、active-loss、full-suite 三门禁均重新 passed/离线校验通过，绑定与 UI 职责见 [workbench 最终报告](2026-10-16-rn-rf-lab-workbench.md)，不沿用前文旧 APK 的 RF 结果。

其中最终 [startup-off manifest](../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-ec419b76474b4938b5d5ecdcd1f25eb7/evidence-bundle.json) 的 bundle 为 `869f1776-5720-4938-a8ae-6065106fc1cb`，session `23b98a63-a799-48c9-8498-4b67580450a3`，PID `20744`；UTC `01:35:04.478646–01:35:15.681828`。Ledger 仍是一个 App runtime、两个 Probe/Hub lifecycle、OFF 三条预期事件与两个 closed 17zero snapshots；radio journal 初始/最终均 `ON`。Manifest `status=passed/completed=true/primary_error=null`，cleanup/evidence errors 为空。

**RF gate pass 不等于真实设备 UI 导航检查通过。** 主执行线的分类/setup/三视图切换与截图检查尚待补充；本节只记录集成候选的 RF 结果和最终 UI 报告入口，不虚构已完成的导航操作，也不新增 iOS device runtime 证据。

## 不外推的边界

- 这是**启动于 OFF、随后 ON 并新建 Probe 恢复 RF**，不是活动扫描/连接/GATT 期间的 radio-loss/恢复。
- 已确认授权，不证明 permission deny/revoke/restore；不证明 iOS 的 adapter/permission 行为。
- 这是一次 gate，不是完整 same-candidate Android/iOS native/package/RN/RF 矩阵，也不是 ABI freeze 或 clean release。

[attempt]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-dc2b86db4afa4e0e99b202d5237c68d4/
[bundle]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-dc2b86db4afa4e0e99b202d5237c68d4/evidence-bundle.json
