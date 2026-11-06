# Formal Android automation：只读准备清单

日期：2026-11-04。真实 Mac、本地 Pixi/Just、未提交工作树。**本轮未操作手机、安装 helper、启动 Appium/WDA 或执行 RF。**

前段[源码审计及 root 边界更正](2026-11-04-formal-android-automation-admission.md)保留；root 已可用，不是安装器人工确认或架构阻塞。本轮完成的是具体只读准备检查，不是完整 Android platform owner。

**同日后续：** [concrete session owner 软件候选](2026-11-04-formal-android-session-owner.md) 已继续实现 prepare/bind/已知 DELETE/finalizer，并接入 managed owner；下文本候选的计数、未实现边界与失败仍按当时范围保留，不改成设备通过。

## 已实现的职责

[`inspect_prepared_android_automation`](../../../tools/react_native/bleng_android_automation.py) 消费实际 App-only `AndroidRadioController` lease、显式 Appium home、三项已审核 helper SHA256、拟使用的 system port、全新输出目录和绝对 deadline。它不创建/释放 phone lease，也不开放新的 scan CLI。

1. 在首条 SDK 命令前，复用既有 resolver 检查唯一完整 helper triplet，并对照显式 SHA256；不从文件名或本机最新版推导批准。
2. 固定同一 device/run，检查 single user 0、helper package UID 唯一性、installed base.apk 路径与字节哈希；缺包、split、foreign shared UID 或 path/hash 漂移不自动修复。
3. 检查 exact instrumentation component/target 的**注册关系**，不把注册等同 active instrumentation 就绪或已退出。
4. 拒绝现有 server/test/相关 UID 的未知进程、Settings 任意 suffix/foreign UID，以及独立 `uiautomator`/launcher。只记录 exact Settings process，不据此认领该进程或其服务。
5. 只读检查 adb forwards：本设备已有 device 6790/7810 映射，或任意设备占用拟使用的 host TCP port，均拒绝；不删除或接管旧 forward。非规范 TCP 端点（如 `tcp:06790`）拒绝，避免文本比较漏掉同一十进制端口。forward 清单不是本地 socket availability 或原子端口 reservation。
6. 记录三个 hidden-API policy 和 helper mock-location AppOps。合法非默认值可作为 observation，不擅自清零；保留 UID/package scope，同 mode 才能提供一致值摘要，不猜冲突时的 effective precedence。
7. 反向核对 user、注册、policy/scope、UID、原安装路径/哈希、进程、转发与原 boot。路径漂移先拒绝，不能读取新路径后再当作原 candidate。

每条 SDK 命令共享绝对 deadline，单次至多五秒；前后检查同一实际 lease、本地 triplet 与新鲜文件哈希。SDK 失败、超时、lease/hash 变化立即停止，不 reconnect/root/install/grant/force-stop/radio/settings repair。stdout 超过 1 MiB 拒绝，receipt 仅保留受限字节；底层 subprocess 先 capture 再检查大小，**不是流式内存限额**。本地 APK 每文件最多 128 MiB。

检查在初始准备阶段执行，不接入逐字段 UI polling，避免重现每次 UI read 都跑完整设备快照的命令放大问题。

## 结果不是 authority

成功仅产生：

```json
{
  "status": "inventory-verified",
  "automation_admission": "pending"
}
```

report 包含同 run/device、installed helper path/hash/UID、Settings process、forward、policy 和明确 remaining requirements。它是 bracketed observation，不是原子 OS snapshot、签名 attestation、安装 provenance 或后续 session 创建许可；不能把 JSON 交给普通 scan 绕过 ownership。

**尚未接入 `ScanAutomationOwner` 的正式 Android 启动许可**。现有 guard 保留，直到同一个 concrete platform owner 能组合准备检查、driver 解析/来源、known-session binding、instrumentation/forward 归属、Settings services、必要设置的恢复读回及 finalizer。不能将本轮 inspector 函数直接用作廉价 `require_owner` callback，也不能为其配 no-op finalizer。

本轮不证明 physical phone selection、helper 包签名/完整本机 driver resolution、active instrumentation、Settings 服务归属或完整 host bundle。调用者继续拥有所有已准备资源及 phone release/quarantine 决策；本轮没有删除 legacy internal。

## 失败和发布边界

- 全新目录内，每次命令与最终结果复用 [`publish_new_json`](../../../tools/react_native/capture_io.py) 独占发布；不会覆盖既有或竞争创建的 evidence。
- SDK 主错误与 post-command lease/hash failure、receipt/result publication failure 通过 notes 分开保留。
- review 发现最终 publish 之后仍可能耗尽 deadline 或丢失 lease/artifact，已增加返回前复查。仅能撤回本轮已知 inode 且字节未改的 result，再发布 failed；外部 replacement 不改不删。失败证据发布是本地 best effort，不延长设备执行或复活 authority。
- 共用 publisher 新增可选 `on_commit` inode 交接：在保留 temporary link 时交给 caller，callback 失败回滚本轮发布。原 caller 的默认行为不变；普通错误、中断及外部 replacement 均有独立真实文件系统回归。
- inode/字节检查与 unlink 不是恶意并发 writer 下的原子 compare-and-delete；仍是合作 host evidence 的 ownership 检查，不作安全认证声明。

## review 依据与修正

初跑的 reverse-path 测试揭示新路径被重新选中后才比较，已改为查询新路径前先匹配原路径。复查另补三类可复现 red：UID-scope AppOps 被误拒绝、前导零 TCP 端点漏掉冲突、最终发布越过 deadline/失去 authority 后仍返回成功，均已修正。进一步补同 mode 的 UID/package scope 漂移，避免将 scope 切换当作不变。

AppOps 合法输出的本地证据不足，使用固定 AOSP `android-16.0.0_r1` 的窄范围源码核对：

- [`AppOpsService.java` 5873–5912](https://android.googlesource.com/platform/frameworks/base/+/android-16.0.0_r1/services/core/java/com/android/server/appop/AppOpsService.java#5873)：`get` 同时查询 UID/package ops，UID 行带 `Uid mode: ` 前缀。
- [`socket_spec.cpp` 89 附近](https://android.googlesource.com/platform/packages/modules/adb/+/android-16.0.0_r1/socket_spec.cpp#89) 与 [`adb_listeners.cpp` 128 附近](https://android.googlesource.com/platform/packages/modules/adb/+/android-16.0.0_r1/adb_listeners.cpp#128)：TCP 十进制端口解释与列表输出，不能只靠原始字符串判定冲突。

这只是源级格式依据，不冒充当前手机实际 readback。

## 实际软件验证

测试位于 [`test_bleng_android_automation.py`](../../../tools/tests/react_native/test_bleng_android_automation.py) 与共用 publisher 原测试目录；canonical `tools-test` discovery 自动纳入。设备只在实际 `android_sdk.subprocess.run` / SDK executable lookup 边界隔离；使用真实临时 APK、phone lease、文件漂移和独占发布冲突，没有真实设备命令。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_automation \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_mobile_appium \
  tools.tests.react_native.test_capture_io \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_android_instrumentation_capture -q
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -q
```

- 组合工具/UI/SDK 回归：**209 passed，19.703 秒**。
- 两端 candidate/prepared runner 回归：**59 passed，102.876 秒**。
- 格式化后预检/publisher 定向重跑：**26 passed**；计数不叠加为覆盖率或 RF verdict。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**48 passed**，保留既有 SafeAreaView deprecation warning。
- `pixi run just fmt`、`pixi run just lint`：通过；lint 包括 workspace/all-targets Clippy。
- `pixi run just docs-check-mermaid`：通过，199 份 Markdown 中 27 个图；本轮 authored Markdown 另查 UTF-8、fences、尾随空白与 conflict markers。
- 完整输出：`.work/formal-android-automation-inventory-{targeted,review,green,regression,candidates,final,fmt,lint,host-check,host-test,mermaid}.log`。

既有广域 tools-test timeout 和 legacy Jest timeout 保留，未盲跑重置其结论。没有正式 Android live session/cleanup/RF、ABI freeze 或 Phase exit 结论。
