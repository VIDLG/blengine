# Android 新 preflight：现有一加的完整临时 HANS lifecycle 验证

日期：2026-11-10。用户确认目前没有 Galaxy，不再推进该设备验证；程序保持通用，不增加
机型硬编码、第二台手机门槛或 OEM 空 adapter。本轮补齐[通用 preflight 软件候选](2026-11-10-android-multi-model-preflight.md)
之后的实际 session/lifecycle 接线证据，并说明 Maestro 的限定职责。

## 实际结果

**新 preflight + Appium 4 + 单 run 临时 HANS 的完整 UI lifecycle passed。**
这不是继承旧 pass，也不是无 HANS bug 已解决；此前无 HANS failed verdict 和
[独立恢复](2026-11-10-formal-android-appium4-device-comparison.md)全部保留。本轮没有重复无 HANS
失败条件、执行 scan/RF、改变 DUT BLE permission/radio/firmware，或重启共享 ADB。

| 项目 | 本次证据 |
| --- | --- |
| run | `be441256-e25f-4389-82ff-747960d71da2` |
| scope | `formal-android-managed-lifecycle` |
| local directory | `.work/formal-android-preflight-hans-runtime/be441256-e25f-4389-82ff-747960d71da2/` |
| App APK SHA256 | `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`，同一预构建 APK，无 rebuild |
| host/driver/server | Appium `4.0.0-beta.7` / UiA2 `9.0.1` / server `10.7.10`，沿用 reviewed helper 三件套 |
| observed profile | OnePlus / OnePlus / PHB110 / API 36 / family oplus；不是固化程序输入 |
| recorded policy | `prepared-owner.json.automation_policy=oplus-hans-temporary` |
| UI | `idle → started → closed` 与完整页面浏览 passed |
| startup snapshot | poweredOn，scan denied，connection denied；没有 grant 或 scan |
| HANS apply | THIRD → WHITE；debug 恢复，其他分类未变 |
| HANS restore | WHITE → THIRD；debug 恢复，其他分类未变 |
| session/helper/phone | passed，`cleanup_errors=[]`、`phone_cleanup_errors=[]` |
| host/RF | `host_gate_status=pending`、`rf=not_run` |
| App retention | 成功 App 保留结果页；phone lease 已释放 |

本轮 `extra.device_profile` 中 fingerprint 的 SHA256 为：

```text
32201327c8c35dd366fd8e78274b2567dbece31c329eee6e2c96e2ca42e21a80
```

原 fingerprint、serial、完整 OS identity 及日志只留本地。本次实际运行覆盖新 profile 的
prepare、admission、binding 和 finalizer 基线比较；不证明所有机型支持、长期稳定、native
resource-zero、RF 或 ABI/release gate。

## 准入与执行

1. 离线重放前一 HANS candidate `88974e20-f8ee-4737-834d-7e9edcc5ec68`：integrity passed。
2. 在 fresh phone lease 下，按其 immutable bound identity 核对原 APK、boot、PID、UID、start
   ticks 和 cmdline，确认没有 helper/Maestro process、active instrumentation 或 forward。
   仅 force-stop 该已知 retained App，并核对 UID/process 缺席。
   独立退休 run `2d32cba7-eaec-42e4-b383-6cee2c28829b`，证据
   `.work/preflight-hans-retained-app-retirement-2026-11-10/`；status/phone passed。
3. 通过原入口运行独立新 candidate：

```text
pixi run just react-native formal-host-android-managed-lifecycle
```

使用原显式 local device、package/activity、上述 APK 与三份新版 helper hashes；host home
仍为 `android-4.0.0-beta.7-uia2-9.0.1-4d4153d22890`。参数保持
`--appium-port 4727 --system-port 8247 --timeout 240 --phase-timeout 30 --cleanup-seconds 60`
和 `--authorize-install --authorize-helper-settings --use-root --close-diagnostics
--authorize-server-hans-exemption`。没有 power/freezer bypass、额外重试或 timeout 扩大。
CLI exit 0，scenario/session/phone 均 passed，host gate 仍 pending。

4. 新 sealed bundle 离线重放：

```sh
pixi run just react-native formal-host-lifecycle-verify .work/formal-android-preflight-hans-runtime/be441256-e25f-4389-82ff-747960d71da2
```

integrity passed、verdict passed，未升级 host gate。另核对
`platform/server-hans-result.json` 和 `platform/server-hans-restore-result.json`：实际
THIRD → WHITE → THIRD、两份 `debug_restored=true`、`other_classifications_changed=false`。

## Maestro 的作用

Maestro 是另一套 UI 自动化工具，官方以 YAML flows 描述点击、可见性断言和导航。
在本项目中它是**显式 opt-in 的 UI transport 对照**，不是默认 Appium 的替换批准：

- **作用：**以另一条 UI 驱动路径执行同一正式 App 的 idle 与 Start/Close/页面浏览，帮助区分
  “产品 UI 流程本身不能完成”和“某条自动化链路出问题”。不同框架仍可能共享 OS、ADB 或
  instrumentation 依赖，不能凭 A pass/B fail 唯一定位根因。
- **已有证据：**Maestro `2.12.1` 在同 APK 上的限定完整 lifecycle pass 见
  [既有报告](2026-11-09-formal-android-maestro-lifecycle.md#5-修正候选的完整-lifecycle-comparison-pass)。
  本轮只执行现有 bundle 的 offline verify，exit 0；没有运行新的 Maestro device candidate：

```sh
pixi run just react-native formal-host-maestro-lifecycle-verify .work/formal-android-maestro-lifecycle/9ff9215d-665d-44c5-9d73-fddfcb51dd79
```

- **不负责：**BLE 命令/协议、真实 RF、native resource-zero、fixture/radio/permission control，
  或最终 host/release verdict；这些继续由所属 owners 和独立测试判定。
- **不扩建：**Appium 仍为默认，Maestro 只保留小型 named comparison；不为相同按钮流程
  维护第二套完整 BLE/RF 测试栈，不把 Maestro pass 升级为 Appium pass。
- **重试边界：**官方工具有自动等待/重试能力，不等于可以重跑业务操作。当前 reviewed flow
  对 tap 显式关闭 no-change retry，Start/Close 和 BLE 操作不因 UI 等待重新提交。

本次通过 Context7 官方 Maestro docs 核对 YAML/`tapOn`/`assertVisible` 的用途和 retry 边界；
项目版本行为仍以锁定 2.12.1、本地 flow 和旧候选证据为准，没有升级或安装 Maestro。
官方参考：[flows](https://docs.maestro.dev/)、[tapOn](https://github.com/mobile-dev-inc/maestro-docs/blob/main/api-reference/commands-available/tapon.md)、
[assertVisible](https://github.com/mobile-dev-inc/maestro-docs/blob/main/api-reference/commands-available/assertvisible.md)。

## 收尾检查

- `pixi run just fmt`、`pixi run just lint`：通过；352 个 Python 文件未变化。
- `pixi run just docs-check-mermaid`：通过，233 个 Markdown 中 27 个图。
- `git --no-pager diff --check`、两份新/更新报告的 UTF-8、fence、空白、冲突标记和本地链接：
  通过；既有三个文件的 CRLF→LF warning 未顺手修改。
- 最终 active phone lease 清单为空，5037 listener 仍为原 ADB PID `16841`。
- 本轮未更改可执行源码，未重跑整组 Python 单测；本轮新增的是上述真实 lifecycle 和独立
  offline replay 证据，不能拿先前单测数量或旧 Maestro pass 冒充新的设备 run。

## 保留边界

- 通用入口不限制固定型号、serial 或 API 36；实际型号写入 candidate receipt。HANS 的
  reviewed ROM hash 是有意保留的安全约束，不为“通用”移除。未知机型默认 generic observation，
  不自动执行 OEM mutation。
- 当前只用已有一加，不新增 Galaxy 工作、强制设备矩阵或推测性的 OEM 策略。
- 下一项产品证据应针对尚缺的正式 scan/reload/native/RF 边界选择，不重复已知无 HANS failure，
  也不把本次 UI pass 当作可以跳过它们。本轮不自动进入 RF 或 stress。
- 没有修改 production、preflight、runner、Maestro flow、AGENTS 或架构计划；只记录新设备
  candidate 与旧 Maestro evidence replay。没有提交 upstream issue 或上传设备证据。
