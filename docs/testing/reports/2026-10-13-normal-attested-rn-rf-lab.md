# 普通 attested DK 镜像与 RN 连续真机 RF — 2026-10-13

## 结论与证据边界

本轮完成普通 RF 的 A/B 声明式镜像、独立产物、正式契约导出，以及两块物理 DK 的显式 provision + BTP installed identity readback。Android 和 iOS 分别在同一块 DK-A、同一个普通镜像上连续运行九个真实 RF flow，均出现 `flows=9:passedCount=9:failureCount=0`，host cleanup 成功，`app=kept-open`。

随后增加真实 wrong-image 负向验证，发现并修复 provider 启动等待将未回收的退出子进程误判为存活的问题。修复后的 Android 正向重跑再次通过；修复后的 iOS 重跑因手机锁屏而被系统拒绝启动，未进入 RF。随后用户解锁，最终 controller 的 iOS 补跑也完成 **9/9 passed、app=kept-open**，证据目录为 `tests/consumers/react-native-internal/ios/build/rf-lab-suite-ea3df60d219e44219b1388550dc8b8b6/`；该补跑与此前使用同一手机产物和 DK-A 镜像。锁屏 attempt 仍保留为未进入 RF 的独立记录。

这些 normal 结果是私有 RN/Nitro → production BleHub → production Apple/Android backend → 手机无线电 → 实体 nRF52840 的真实 HIL，不是 Jest/fake 结果。不证明 ABI freeze、BLE 认证、恶意同用户篡改防护、normal B RF、watchdog RF 或所有测试特性完成。后续独立 Android watchdog 本轮完整通过，iOS watchdog signed build 完成但 preflight `Locked`、未进入 RF，另见 [native watchdog 报告](2026-10-13-rn-native-watchdog-rf.md)，不与本报告 normal 9/9 合并成同一 gate。

## 镜像与契约

BleHub 拥有 [A declaration](../../../tests/peripherals/nrf52840/firmware/images/normal-rf-v1-a.json)、[B declaration](../../../tests/peripherals/nrf52840/firmware/images/normal-rf-v1-b.json) 与 canonical profiles；nrftest 通过显式路径接收，不导入 BleHub 源码。local name 和 service UUID 从 profile 提取，declaration 不维护第二份副本。

本报告 normal A/B provision 时两块板的配置均为 `CONFIG_BTTESTER_GATT_READ_HOLD=n`、`CONFIG_BTTESTER_BLEHUB_ATTESTATION=y`。fixture capabilities 为 `normal-rf`、`gatt-reset`、`notification`、`indication`、`passive-disconnect`；Central cancellation 不是 DK capability。

| 产物 | DK-A | DK-B |
|---|---|---|
| Image ID | `nrf-normal-rf-v1-a` | `nrf-normal-rf-v1-b` |
| Profile ID | `blehub-nrf52840-indication-v2` | `blehub-nrf52840-indication-v2-b` |
| Local name | `BleHubNrf52840` | `BleHubNrf52840B` |
| Service UUID | `fd000000-0000-4000-8000-000000000000` | `fd200000-0000-4000-8000-000000000000` |
| Normalized image SHA-256 | `83b42f5ef1d28f410ea48785bcb5a8eaa3b6dc2152deee3d58eb3c026d526003` | `6418fd767310ad495645d85ebf2842e1e7e0913adf716c523bd38871ee412385` |
| Raw HEX SHA-256 | `3d13cb4eef8091af3c72735ca8afbabd08bff3102b1276673d279b8e507ad7ec` | `64cb7aacb6c11e204168d3e097d08d3b7ecfa27776b4109641d5fb298ac38cbc` |
| USB probe serial | `001050252028` | `001050251479` |
| Canonical resource | `jlink:1050252028` | `jlink:1050251479` |

机器身份只在本机配置和具名证据中选择，不写入通用构建、fixture 或 runner 逻辑。上表 DK-B normal B 为历史安装记录；后续显式切换为 `nrf-att-hold-watchdog-v1`（30s hold），DK-A 保持 normal A。普通 run 不刷机；当前 DK-B 状态与 Android watchdog RF 见 [独立报告](2026-10-13-rn-native-watchdog-rf.md)。

锁定 Zephyr revision 为 `d31eebc4557c7a0a8a64e77b9642ee9a341661f4`，AutoPTS revision 为 `955f014070c21a3fe8a04a55acc4006917b1d509`，ARM SDK 为 `1.0.1`。两遍 west 构建验证 digest slot、resolved config 和 HEX/BIN 地址一致性；输出分别位于 nrftest：

```text
.work/build-attested/images/nrf-normal-rf-v1-a/nrftest-build-manifest.json
.work/build-attested/images/nrf-normal-rf-v1-b/nrftest-build-manifest.json
```

未覆盖旧 pending-read 产物。正式预期契约分别为 [normal-rf-v1-a.json](../../../tests/conformance/scenarios/fixture-contracts/normal-rf-v1-a.json) 与 [normal-rf-v1-b.json](../../../tests/conformance/scenarios/fixture-contracts/normal-rf-v1-b.json)。exporter 重新检查输入快照、配置、产物 hash 和 normalized digest；再次 `firmware-check-contract` 均通过。导出只是 **expected build identity**，不是 installed proof。

## 显式 provision 与物理读回

flash/provision CLI 现要求显式 `--manifest`，Just recipe 的第一个参数也为 manifest；不搜索“最新”镜像，不回退默认目录。只消费 `manifest.parent/zephyr/zephyr.hex`，用 raw HEX SHA-256 确认，私有 immutable snapshot 隔离协作重建。每块板的同一设备租约持续覆盖 snapshot/preflight → flash → BTP readback → exact identity assertion。

nrftest 本机保留：

```text
.work/reports/normal-rf-v1-a-installed-2026-10-13.json
.work/reports/normal-rf-v1-b-installed-2026-10-13.json
.work/reports/firmware-provision-dk/20261013T103903.703424Z-814321cdc8cd495f94b71e19ab82c604.json
.work/reports/firmware-provision-dk/20261013T103934.863716Z-489e27ea4f774d1388bd46674256ac69.json
```

两次 provision 均通过；installed identity 的 image/profile/digest/capabilities 与各自 manifest 和 contract 一致。J-Link wrapper 报告的 `flashed_bytes=0` 不作为刷写字节数或 installed proof，成功依据是后续物理 BTP readback。普通 RF run 不调用 flash/provision。

## 连续 RN 真机结果

| Flow | Android OnePlus 11 | iOS iPhone 14 Plus |
|---|---|---|
| `rf` | passed | passed |
| `rf-connect` | passed | passed |
| `rf-gatt` | passed | passed |
| `rf-io` | passed | passed |
| `rf-rssi` | passed | passed |
| `rf-notification` | passed | passed |
| `rf-indication` | passed | passed |
| `rf-passive-disconnect` | passed | passed |
| `rf-cancellation` | passed | passed |

Android ADB serial 为 `3ac5cc09`；iPhone UDID 为 `00008110-000C61160C39401E`，development team 为 `FWVYQ287W5`。两端串行独占 DK-A，一个 App/JS runtime 连续完成各自九项，没有每 flow 刷机或重启 App。最终保留结果页面，无需人手点击开始下一项。

每个 session 在 advertising 前，通过同一 BTP 连接、同一物理租约读取当前镜像，receipt 绑定 session UUID、contract/profile hash、physical resource 和 fixture PID/PGID；不是复用历史 installed JSON。Android 的 APK 安装也在 live proof 之后。

初次 Android 与 iOS 的 receipt 均确认：

```text
image_id=nrf-normal-rf-v1-a
image_sha256=83b42f5ef1d28f410ea48785bcb5a8eaa3b6dc2152deee3d58eb3c026d526003
resource_identity=jlink:1050252028
contract_sha256=e12cc5dd79fdd26b0592541f597308e9985f49b4fde8955d71f68c46697d7a0b
profile_sha256=ef37a09bd575f8c7872901204c65dc6ccac0831445b4d78fb66b6bec85239dca
proof_method=btp-vendor-live-under-device-lease
```

### Native cancellation 实际覆盖

两端真实日志均包含：

- active read cancellation 返回 `operation_not_cancellable`，原 read 随后正常完成；不声称 native ATT 已被取消；
- queued read cancellation 被接受，目标 `9028` terminal 为 `cancelled`；
- active read `9027` 完成；
- canceled read 后 1,000 ms 观察窗口无晚到事件；
- unknown target、completed target 返回 `operation_not_found`；
- recovery read `9029` 完成并读回 `AB`，随后 disconnect/cleanup；
- `rf-cancellation:passed` 与 suite `9/9` summary。

这是 production backend queued cancellation/active rejection 的 RF 验证，不是只在 JS 提前结束 Promise，也不等同 active native operation abort。

### 本机 RF 证据目录（BleHub-relative）

```text
# 初次 Android pass
 tests/consumers/react-native-internal/android/build/rf-lab-suite-beece781e45c4a0ebe2d3b4b306e1235/
# 初次 iOS pass
 tests/consumers/react-native-internal/ios/build/rf-lab-suite-68701d7551914d279285f00316fc087b/
# provider exit/reap 修复后的 Android pass
 tests/consumers/react-native-internal/android/build/rf-lab-suite-dbcb80ccdbca47f2a21d8fd2ba1c25c4/
# 修复后的 iOS historical attempt：locked、未进入 RF
 tests/consumers/react-native-internal/ios/build/rf-lab-suite-51dc3abb029746b2b2aecc99fca0e21c/
# 解锁后的最终 controller iOS pass：REAL 9/9、app=kept-open
 tests/consumers/react-native-internal/ios/build/rf-lab-suite-ea3df60d219e44219b1388550dc8b8b6/
```

成功目录保留 runtime logs 与 `coordination/fixture-session/{fixture-request,fixture-receipt,profile,contract}.json`。本机 generated evidence 不作为 tracked 通用设备配置。

两端 single-image check 通过：iOS canonical UniFFI image；Android 两个 ABI slice 各含 UniFFI/Nitro image。运行后的产物 SHA-256 补充记录如下（不是 runner 内置的强制 provenance binding）：

```text
Android app-release.apk:
2488daa63ac7ba332a8e1d875da3a4e59d8d116f222516acd15fd4ba1b6ab07d

iOS BleHubNitroHost.debug.dylib:
fc7d4a3c1fa0f9a2cb8990eb5f531a46653dc9e5c762b759402a541e5c5fbcc9
```

## 负向验证与本轮发现

1. 首次 A/B 实际编译被过宽的 recursive symlink 检查拦截：Zephyr 正常生成 syscall links。检查收窄为消费/发布固定产物及其祖先路径，并补回归；重新双阶段构建通过。保留 native `__ASSERT()` enabled CMake 提示，不把它伪装成零 warning 构建。
2. 首次 Android prepare 命令外层 240 秒超时，停在 production artifact/toolchain 准备阶段，未进入 RF。独立工具链 verify 通过，后续 production AAR publish 与 host release build 通过。
3. 并行手机构建共享 npm install 目录，造成一次 TypeScript 缺依赖；串行重跑通过。共享安装/生成路径的构建不得并行。
4. 将 B contract/profile 指向 DK-A，实际 vendor read 返回 `installed fixture mismatch for image_id: expected 'nrf-normal-rf-v1-b', got 'nrf-normal-rf-v1-a'`。初次外层 45 秒超时暴露 `_wait_ready` 只检查 PID 的 zombie 问题；改为先 poll/reap owned child，再判断 PID/READY。负向复测及时退出 2，保留在 Android `rf-lab-suite-36185958dd0d448c9bc11d6543b98fd2/`，没有 receipt 或 App launch。provider 清理了自己的 spawned fixture；host 没有成功 startup ownership，保守拒绝接管清理并打印提示。这不是 cleanup success 的附加证明，也不是 RF pass。
5. iOS historical attempt `51dc3abb029746b2b2aecc99fca0e21c` 在 devicectl launch 处明确报 `Locked`，与 RF/backend 失败分开记录；不能自动输入手机密码或把之前的 pass 冒充该次执行。其后用户解锁，最终 controller 的 `ea3df60d219e44219b1388550dc8b8b6` 独立补跑已通过 REAL 9/9、`app=kept-open`，保留两轮记录。

## 软件验证与剩余工作

通过：

- nrftest：`471 passed, 1 skipped, 4 subtests passed`；fmt/lint；A/B contract check。
- BleHub nRF subtree：`178 tests`；live-suite preflight/session：`30 tests`。
- BleHub fmt/lint（workspace Clippy、profile isolation）；两端 RN typecheck/构建与 single-image check。
- Mermaid、UTF-8、代码围栏、尾随空白与冲突标记检查。

后续状态：独立 watchdog image 已安装到 DK-B，Android 最终稳定 runner（格式化后）attempt `cbf82ed5-bff9-4cd8-a9f8-a251042ebfd8` 完整通过 native timeout/queued abort/old-event isolation/recovery/cleanup RF。它新增 host `post-release-old-route-silence`：RELEASED + physical disconnect 后、recovery advertise 前，两次 snapshot 间隔至少 1,000 ms，本轮记录 `1316.8694591149688 ms`；`release_gate` 强校验 `21010/21011` 各一个 native terminal 且无 domain，测试拒绝 post-release old event。`f068aac8` 含 post-release 边界的 pass 和此前仅持续 ledger 的 Android pass 均保留为 historical，细节见 [独立 watchdog 报告](2026-10-13-rn-native-watchdog-rf.md)。iOS 最新 watchdog signed build 完成但 Locked preflight 未进入 RF；后续再次只读 ready check 仍 Locked，不能称通过。

最终软件验证由主执行线报告：BleHub `nrf52840-test`、`pixi run just fmt`、`pixi run just lint`、Mermaid 全通过；nrftest lint/test 为 542 passed、1 skipped、4 subtests passed；全 RN Jest 43/43（34.9s，在 90s limit 内）、typecheck 通过。此前 focused watchdog 26/26、RN UI 30/30、transport + watchdog 31/31 仍保留。旧全 RN Jest 120s 超时因纯 mock suite 累计真实 timer 窗口；测试 fixture 改用 fake timers，不改 production、不改 timeout。本次文档更新不声称重新运行了这些软件或 RF 门禁，也不由此推断所有 native/package 验证已通过。

剩余：iOS watchdog 实际 RF；adapter/permission transitions；Android pending-operation reload；attested current-candidate repeated recovery/multi-DK gates；完整统一 production artifact + per-flow run manifest；ABI freeze 同候选门禁。normal iOS controller 修复后的解锁补跑已完成，不再是 blocker。pending-read 的正式镜像已构建，但本报告 normal 轮没有刷入或验证该诊断镜像，不能继承后续 watchdog 的结果。

设计和可复现命令见 [架构与差距评估](../../research/react-native/react-native-rf-lab-test-architecture.md) 与 [fixture README](../../../tests/peripherals/nrf52840/README.md)。本轮没有提交 BleHub/nrftest，也没有再次修改或提交已发布 Zephyr/AutoPTS forks。
