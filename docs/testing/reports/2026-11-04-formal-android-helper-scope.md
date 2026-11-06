# Formal Android：授权的 helper 设置准备与恢复边界

日期：2026-11-04。真实 Mac、Android 真机 `3ac5cc09`、本地 Pixi/Just、未提交工作树。依据：[Plan](../../../.plan/PLAN.md)、[前序设备准备与 release 候选](2026-11-04-formal-android-device-preparation.md)。用户明确允许仅准备 Appium helper 的 mock-location AppOps 与 mock-app store，并在测试后恢复；不包含定位坐标注入、DUT BLE 权限、radio、firmware 或全局设置 reset。

## 操作的是哪个 Settings

不是 mock 系统，也不是 macOS 系统设置：

- `io.appium.settings` 是安装在 Android 真机上的 Appium helper package，不是 Android 的 Settings App 本体。
- `cmd appops set --user 0 io.appium.settings android:mock_location …` 调用这台手机的系统 AppOps 服务，只改变该 helper 的一个 operation mode。
- `/data/local/tmp/mock_apps.json` 是手机上由 Appium Android driver 使用的包名列表文件，不是 BLE 模拟器或设备事件来源。
- 本轮没有调用位置注入接口，没有发送坐标，没有创建 WebDriver session，没有安装/启动目标 App，也没有 BLE 命令或 RF 流程。

本机实际 Appium Android driver 源码的 `lib/commands/geolocation.ts` 中，`setMockLocationApp` 先设置 helper AppOps，再维护 store；已有目标包名则不写 store。`resetMockLocation` 另有遍历列表并 deny 的分支，本轮没有执行它。`lib/commands/device/common.ts` 会在真机初始化时调用前者。这里核对的是本机已解析源码，不是完整 dependency/config/plugin provenance 或 stock driver runtime 通过；未修改第三方文件。

## 两次真实 attempt

两次都使用实际 `AndroidRadioController` App-only phone lease、明确设备/run ID、同一审核 helper hashes、独立证据目录与有界命令。开始时通过原 read-only inventory 核对 helper 安装、boot、UID、注册、旧进程/forward 和 AppOps；变更前再次 pin boot 与 helper image。没有创建未知 session 的机会，所有 owner 行为仅 prepare/finalize。

### 普通 shell：准备被拒绝，未留下配置变更

证据：`.work/android-helper-preparation-64f2230e-fa3c-4976-b1cc-c720250b3531/`。

- 原 AppOps：`No operations.\nDefault mode: deny\n`；store 不存在。
- 普通 shell 调用 `cmd appops set` 被设备以 `uid 2000 does not have android.permission.MANAGE_APP_OPS_MODES` 拒绝。
- 读回仍为原 default deny；没有创建 store。
- 主结果 failed/blocked，cleanup errors 为空，phone lease 已释放。
- 没有把 shell 无权限解释为 rooted phone 不可用，也没有重试同一未知结果。下一次使用全新 run 和显式 root scope。

### Root-scoped preparation：owner 检查通过，但 exact-shape 恢复失败

证据：`.work/android-helper-preparation-c1228f5d-a4a0-4731-888c-bf8337aeadf8/`。

实际顺序：

1. 同样的原始 default deny 与 absent store，在首条变更之前保存到 `original-state.json`。
2. `su -c cmd appops set --user 0 io.appium.settings android:mock_location allow`，读回 exact package-scope allow。
3. shell 以 noclobber 创建只含 `["io.appium.settings"]` 的 store，pin 文件 identity/metadata/bytes；不覆盖已有文件。
4. 真正的 `AndroidAutomationOwner.prepare()` 通过，包括其额外的 instrumentation、Settings services、mock store、global hidden-key 存在性和 IME baseline 检查。
5. 在明确未创建任何 session 的 prepared 状态执行 `finalize()` 与 `require_closed()`，通过。`owner/finalized.json` 明确 `session_deleted=false`、`resource_zero=not-claimed`、`host_gate_status=pending`，不是 DELETE 或完整 session 证据。
6. 复查原文件 identity/bytes 后只删除本次 store，读回恢复 absent。
7. 只对同一 helper operation 执行 root-scoped `deny`，权限读回为 `MOCK_LOCATION: deny\n`。
8. 因该输出不等于原来的 absent/default-scope deny，外层 exact-shape restoration 报错，phone 保持 **quarantined**，没有释放租约。

### 当前结果必须分开表述

| 项目 | 实际状态 |
|---|---|
| helper temporary preparation | 通过 |
| 无 session 的 owner prepare/finalize | 通过 |
| 本次 store 删除并恢复 absent | 通过 |
| helper mock-location mode 恢复 deny | 已确认 |
| AppOps 输出/scope 形态完全等于原值 | **失败：default/no-record → package deny record** |
| phone finalization | **quarantined，lease 保留** |
| Appium session / DUT runtime / RF | 未执行 |

不能将两个 deny 的安全效果直接等同于“原 AppOps 数据记录形态已完全恢复”，也不能把 scoped owner prepare/finalize 的通过升级为整条自动化链路通过。原主失败及 cleanup failure 均保留。

## 恢复差异的源码核对与限制

本次缺少对应本地官方快照，窄范围核对固定 AOSP `android-16.0.0_r1`：

- [AppOpsService.setMode](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/appop/AppOpsService.java#L2442) 使用 `getOpLocked(... edit=true)` 获取/创建 operation；`get` 分别输出 UID/package entries，缺 entries 时才输出 `No operations` 与该 op 的 default mode。
- [AppOpsCheckingServiceImpl.setPackageMode](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/appop/AppOpsCheckingServiceImpl.java) 在回到 operation 默认 mode 时可移除非默认 mode 的存储项。这是 AOSP legacy storage 实现，不证明本机 OEM 使用同一实现，也不保证 `get` 中 materialized entry 消失。
- `appops reset` 的 shell 分支调用 `resetAllModes(userId, packageName)`，没有本次目标 op 的单项参数。不能为了消除这一条记录而隐式 reset helper 的全部其他 AppOps，更不能清理全系统 AppOps、修改持久化系统数据库或重启手机。

因此，“deny 已恢复但记录保留”有本机观察和源码解释；**对 OEM 内部存储的完整等价性仍不作保证**。本轮不悄悄放宽 restoration 判据，不重写历史失败为 passed。是否接受权限 mode/scope 的有界语义恢复并保留系统记录，或另行授权更广的恢复动作，需要明确评审。当前不得接管保留的 quarantine lease 来继续普通测试。

## 验证与证据

本轮仅运行 ignored one-shot host attempt 和文档更新，没有变更 Rust、native、正式 helper owner 或测试代码；没有新 production provisioning API/CLI。实际命令在每个 attempt 的 `attempt.py`、`attempt.log` 和逐命令 JSON 中，全部经 `pixi run just --command python` 执行。

- 每次 attempt 总操作预算 100 秒，独立 cleanup 预算 60 秒，单条命令不超过 5 秒；没有 timeout 或盲重试。
- root attempt 保留 `prepared-settings.json`、`owner/prepared-owner.json`、`owner/finalized.json`、`result.json` 和 `phone.json`。
- 本次未重跑此前的 Python 70/Jest 48 项，不把旧软件通过继承为新 runtime 证据。
- 当前工作树 `pixi run just lint` 全量通过，包含 Rust format check、workspace/all-targets Clippy、Ruff、Just 与 profiles；这次不再受前序 Clippy 问题阻塞。`pixi run just --command ruff format --check tools tests/support/rf_lab tests/peripherals/nrf52840/support` 通过。为保留其他 dirty work，未执行会自动改写全仓文件的 `fmt`。
- `pixi run just docs-check-mermaid` 通过：202 份 Markdown、27 个图；三份本轮 authored/updated Markdown 的 UTF-8、围栏、尾随空白、冲突标记及本地链接检查通过。日志在本次 root attempt 的 `lint.log`、`format-check.log`、`mermaid.log`。
- 当前 lease：`.work/android-radio-leases/86a48912ccabaa1ee36ad5513ebb6dfa46e51e33c7c28f6d92ce5e36709b2f41/`，owner run 为 `c1228f5d-a4a0-4731-888c-bf8337aeadf8`。不手工删锁、不假冒新的准备 authority。

下一步先处理这个恢复契约边界，再实现可测试、显式授权的 helper preparation scope，并接真正的 managed Appium session；不是跳过 quarantine 直接跑 UI/RF。
