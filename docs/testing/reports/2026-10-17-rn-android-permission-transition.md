# Android RN permission transition：root-controlled 真机通过与失败证据

日期：2026-10-17。

## 结论与边界

权限变化场景已集成到原有 **Tests → Current run → Evidence** 工作台，没有新 App 或独立导航框架。Android host recipe、三阶段 RN runtime、OS controller、provider coordination、ledger 与统一 evidence bundle 已接通，软件回归和 Release APK 构建通过。

**第五次显式授权的新 attempt 已通过完整 Android permission-transition 真机门禁及离线证据复核。** 真实完成 denied startup、正常 RF baseline、held + queued read 下的 CONNECT 撤权、OS reason 8 旧进程死亡证明，以及恢复权限后的 fresh-App RF I/O；host、physical、permission/radio cleanup、bundle 均 passed，结果 App 保持打开。该证据来自 root-controlled Android API 36 手机与具名 APK/session，不外推 stock/non-root API-tier、iOS、same-Hub 或最终同候选全门禁。

前四次失败分别暴露 fixture 冷启动顺序、UID 列表子串匹配、普通 shell 系统权限不足和阶段静默预算问题。它们的原始 failed manifest、错误与清理边界原样保留；第五次是独立新测试，不是重写旧 verdict，也未隐式刷机或自动重试。

## 第五次 attempt：完整通过

执行命令：

```text
pixi run just react-native host-android-rf-permission-transition 3ac5cc09 tests/conformance/scenarios/fixture-contracts/multifunction-rf-v1-a.json tests/conformance/scenarios/blehub-nrf-multifunction-v1-a.json
```

| 项目 | 记录 |
|---|---|
| Attempt | `rf-permission-transition-107ccbbf90c8413c924ba7fe209998b6` |
| APK SHA-256 | `18ba7a1c899a050f64f4a295a07956b2823639031212a91dc93b9d17d00caa2b` |
| Fixture session | `58840b12-f10b-4230-b52f-1f20411225e1` |
| Bundle run ID | `5de6ba16-b517-4c62-b18b-917fd2a26594` |
| Platform / control | Android API 36、User 0、root permission command mode |
| 三个 fresh processes | denied `21368` → active `21674` → restored `22382`，UID 均 `10372` |
| OS death proof | active PID `21674`，reason `8 (PERMISSION CHANGE)`，`2026-10-17 22:36:57.935 +0800` |
| Host authorization→revoke 完成 | `225ms`，在独立 4s budget 内，不宣称与 DK WAITING 原子同刻 |
| Physical | 三对 GAP connect/disconnect；baseline、held-loss、restored |
| 逻辑生命周期 | 四个 Probe/Hub；活着完成 close 的 denied/baseline/restored 各一份 17 项零，共三份 |
| 死亡 active Probe | `dead=true`、`closed_resources=null`，不伪造 close/terminal/resource-zero |
| 最终 verdict | `passed/completed=true`；primary、cleanup、evidence errors 均为空 |
| Final state | SCAN/CONNECT granted、radio ON；DK hold inactive、peer 0、advertising stopped |
| 成功 App | PID `22382` 后续只读复核仍在，runner 返回 `app=kept-open` |

证据路径：

```text
tests/consumers/react-native-internal/android/build/
  rf-permission-transition-107ccbbf90c8413c924ba7fe209998b6/
    evidence-bundle.json
    permission-transition-runtime.raw.log
    permission-transition-runtime.log
    permission-transition-runtime.result.json
    permission-transition-os-death-proof.json
    permission-transition-exit-info.raw.log
    permission-transition-ledger.json
    permission-state-control.json
    permission-radio-control.json
    coordination/
```

Host boundary 顺序为 `authorize_active → authorize_arm → mark_waiting → confirm_loss → authorize_restored`。Controller journal 的 initial、deny-startup、grant:active、revoke-connected、await-permission-death、grant:restored、restore-initial-grants 全部 confirmed；权限先于 phone lease release 恢复。

同一 bundle 通过：

```text
pixi run just react-native host-rf-evidence-check tests/consumers/react-native-internal/android/build/rf-permission-transition-107ccbbf90c8413c924ba7fe209998b6/evidence-bundle.json
```

结果为 `integrity verified (not RF reauthorization)`；这是原始 runtime/OS/physical/control 证据的离线复核，不是第二次 RF。后续只读确认主包 UID/grants、radio ON、provider PID 标记不存在和 restored PID 存活；未切换页面、停止 App 或重复执行门禁。

## 实现边界

- [`rf_permission_transition.tsx`](../../../tests/consumers/react-native-internal/src/rf_permission_transition.tsx)：denied startup 验证 Denied snapshot、initialize 成功及 scan Permission terminal；active 阶段先完成正常 I/O baseline，再以另一 Probe 建立 held + queued read；restored 阶段在 fresh process/Probe 上运行正常 I/O。
- [`App.tsx`](../../../tests/consumers/react-native-internal/src/App.tsx)、[`rf_lab_catalog.ts`](../../../tests/consumers/react-native-internal/src/rf_lab_catalog.ts) 与 [`rf_lab_session.tsx`](../../../tests/consumers/react-native-internal/src/rf_lab_session.tsx)：沿用统一工作台，投影有界阶段摘要，过滤 raw event/resource/native identity；App 阶段结果不等于 host physical/cleanup/bundle verdict。
- [`permission_control.py`](../../../tools/android/permission_control.py)：在 phone lease 下控制 SCAN/CONNECT grants，并以旧 PID/UID/user/process/start-time 和事件时间关联 `ApplicationExitInfo.REASON_PERMISSION_CHANGE = 8`。PID 消失或 reason 10 均不构成成功证明。恢复 grants 必须先于释放 phone lease；不主动切换 radio。
- [`run_android_permission_transition.py`](../../../tools/react_native/run_android_permission_transition.py)、[`permission_transition_capture.py`](../../../tools/react_native/permission_transition_capture.py) 与 [`permission_transition_ledger.py`](../../../tools/react_native/permission_transition_ledger.py)：冻结 artifact、持续 logcat owner、三个 fresh processes；revoke→death-proof 窗口禁止 host kill/reinstall/relaunch。死进程 Probe 不伪造 terminal、close 或 17 项零。
- [`permission_transition.py`](../../../tests/peripherals/nrf52840/permission_transition.py)：live-attested multifunction DK，同步真实 WAITING、撤权授权、OS death proof、hold release、GAP loss、canonical restore 与 fresh-App I/O。
- [`rf_evidence_bundle.py`](../../../tools/react_native/rf_evidence_bundle.py)：固定证据引用，离线重放共享 runtime/OS/physical/control ledger，不只信任 `passed` 字段。离线完整性验证不是 RF 重验。

本场景不覆盖 same-Hub recovery、active write/CCCD revoke、iOS permission transitions、死进程 cleanup 成功或 ABI freeze，也不隐式加入非破坏性 full-suite。

## 只读 preflight 与解析器修正

设备 `3ac5cc09` 为 Android API 36、User 0；目标包 UID 为 `10372`，SCAN/CONNECT 均 granted，Bluetooth ON。当前 App 的 `/proc/PID/stat/status/cmdline` 可读，UID 与包匹配。provider PID 标记不存在，配置串口存在；串口存在本身不是 live receipt 或 RF readiness。

实际 exit-info 在标准标题前包含 `---- TIME YYYY/MM/DD HH:MM:SS ----`，分组标题使用 `Historical Process Exit for uid=…`。解析器已支持这两种实际格式，并保留旧 userId 格式；可选 banner 仅允许一次且只能出现在首行，日期时间须合法。重复、未知、截断或错误位置的内容仍拒绝；reason/UID/time 证明边界未放宽。未经裁剪的设备原始 dump 可解析 16 条历史记录，但没有本次 reason 8 撤权证据。

## 第一次硬件 attempt

| 项目 | 记录 |
|---|---|
| Attempt | `rf-permission-transition-3ffbfc739b6e41f7a4e8419d35b0487f` |
| APK SHA-256 | `18ba7a1c899a050f64f4a295a07956b2823639031212a91dc93b9d17d00caa2b` |
| Fixture session | `dd8cc1da-fa4f-4072-91b5-cf85a8f32967` |
| Bundle run ID | `2f5c59d1-b13b-448c-883d-2350811ecd42` |
| 结果 | `status=failed`、`completed=false` |
| 主错误 | `diagnostic firmware rejected ATT hold command` |
| App/权限阶段 | 未到达安装或启动；未执行权限修改 |
| Radio | initial ON、final ON；只读检查，无 cleanup error |
| Fixture cleanup | 资源退出，但 hold inactive / advertising stopped **未确认** |
| Offline bundle | integrity verified，仍保留 failed，不重新授权 RF |

证据位于忽略目录：

```text
tests/consumers/react-native-internal/android/build/
  rf-permission-transition-3ffbfc739b6e41f7a4e8419d35b0487f/
    evidence-bundle.json
    permission-radio-control.json
    coordination/permission-transition-physical.json
    coordination/fixture-session/
```

原 physical evidence 中 `hold_inactive=false`、`advertising_stopped=false`，外层 cleanup 因启动失败已释放 lease 而不能发命令。`peer_count=0` 是本地 tracker，不能升级为现场清理证明；`nrf52840_fixture=stopped` 也不是 hold 清理成功。

## 第二次硬件 attempt：UID 列表包含 instrumentation 包

用户授权继续后显式新跑一次，attempt 为 `rf-permission-transition-0b93941fc523472b94ae07e2b4ad4a6c`。Release 构建通过；fixture 已完成 live attestation、服务初始化、profile 与广告准备，随后安装冻结 APK。在 `permission.initial()` 的初始 UID 核对处报 `package/user0 UID listing mismatch or ambiguity`，没有执行 deny/revoke/grant。

系统实际返回：

```text
package:com.blehubnitrohost.test uid:10373
package:com.blehubnitrohost uid:10372
```

包名 filter 返回子串匹配，现有 instrumentation 包并不使主包身份含糊。原实现要求整份结果只有一行，误拒绝合法输出。修复为严格解析每行，要求匹配 filter 的唯一包名及 user0 UID，并精确选择主包；只有 `.test`、缺主包、重复、错误 UID、畸形或无关行仍拒绝。没有接受“第一项”或放弃 dumpsys UID 交叉核对。

第二次 APK SHA-256 与第一次相同（`18ba7a1c899a050f64f4a295a07956b2823639031212a91dc93b9d17d00caa2b`），但 fixture/session/attempt 独立。第二次 manifest 仍为 `failed/completed=false`，primary 为上述 UID 核对错误，host cleanup errors 为空。Physical cleanup 明确 `advertising_stopped=true`、`hold_inactive=true`、`peer_count=0`；这是第二次独立结果，**不能回填第一次未确认的 cleanup**。权限 controller 尚未建立 initial grants，未执行权限变化。后续只读确认 UID `10372`，SCAN/CONNECT 均 granted、radio ON、fixture PID 标记不存在；第二次 bundle 离线完整性 verified，但 failed verdict 不变。

## 第三次 attempt：普通 shell 缺少系统权限

用户授权按理想顺序继续后，先只读核对 User 0、主包 UID/grants、包名筛选、exit-history 格式、radio ON、无 provider PID 标记与 declared fixture profile，均通过；随后显式运行一次新门禁。

| 项目 | 记录 |
|---|---|
| Attempt | `rf-permission-transition-f2d2f84947cd46ff8b45861a626cc41e` |
| APK SHA-256 | `18ba7a1c899a050f64f4a295a07956b2823639031212a91dc93b9d17d00caa2b` |
| Fixture session | `bc6834ba-fb32-4c8f-a568-915df852bab3` |
| Bundle run ID | `4e14a861-6f40-4fa0-bf12-d4c67d4ecfe3` |
| 结果 | `failed/completed=false`；尚未启动任何 phase App |
| 首个失败 | `pm revoke --user 0 … BLUETOOTH_SCAN`，exit 255，shell UID 2000 缺 `REVOKE_RUNTIME_PERMISSIONS` |
| Cleanup 次级错误 | 两条 grant 均 exit 255，缺 `GRANT_RUNTIME_PERMISSIONS`，保留为 restoration failure |
| Physical cleanup | advertising stopped、hold inactive、peer count 0 均已确认 |
| 最终独立只读检查 | UID 10372，SCAN/CONNECT 均 granted，radio ON，provider PID 标记不存在 |

原 controller journal 中 `initial_grants` 已建立，`deny-startup` 与 `restore-initial-grants` 均 failed，`final_grants=null`。最终独立 readback 不能回写 journal 或清除 restoration failure。没有 reason 8、旧 Probe close/zero 或 fresh-App recovery 证据。第三次 bundle 离线完整性检查仍不改变 failed verdict。

现有 root-aware APK 安装日志显示同一设备已使用 `su -c pm install` 成功；普通 RF grant helper 也已有 `has_root()` 分支。Permission controller 此前遗漏了该执行身份差异。这不是 BLE backend 的 permission mapping 失败，也不需要修改公开 BLE API。

### Root-aware 软件修复

- runner 复用现有 `has_root()`，在 phase 执行前选定一次 command transport；controller initial journal 另保留原始 root `id` 回读并要求 UID 0。
- 只有固定的 User 0、目标 package、SCAN/CONNECT、grant/revoke 可以编码到 root command；带空格的命令正确引用。其余 OS 读取仍用普通 shell。
- mutation 失败不自动切换身份或重试。非 root 路径仍是 shell；既有普通 RF 自动 grant helper 不参与本门禁，不能破坏 denied 阶段。
- offline ledger 核对 initial root identity 的原始 stdout/hash、同一 transport 下的全部 permission mutations 和 exact CONNECT revoke；拒绝缺失/伪造身份、混用 shell/root 和追加脚本。保留原 4 秒撤权、6 秒死亡、旧实例及 OS reason 8 校验。
- root-aware 软件回归不代表真实 root 撤权已执行或通过；未来该路径的 RF 证据也不能外推为 stock/non-root Android API-tier 覆盖。
- 修复后只读执行了与 initial journal 相同的 `adb shell su -c id`：exit 0、UID/GID 0。没有撤权、授予、启动 App 或重跑 RF。这验证现有执行身份可用，不是 permission gate pass。

Root-aware 最终联合回归：`pixi run just react-native tools-fixture-test` **837 tests / 54.074s / OK**，包含 shell/root controller、capture、原始 OS death proof、root identity/script tamper、root bundle 离线重放及 provider 回归。`fmt`、`lint`、Markdown/Mermaid 与 diff 检查另外完成。原第三次 failed manifest 和 restoration errors 不作修改。

## 第四次 attempt：root 操作和 denied checks 完成，host silence 等待失败

| 项目 | 记录 |
|---|---|
| Attempt | `rf-permission-transition-d131bbd60c1a4a40a9ee2293eaaccf45` |
| APK SHA-256 | `18ba7a1c899a050f64f4a295a07956b2823639031212a91dc93b9d17d00caa2b` |
| Fixture session | `46dfa456-b16a-44aa-850c-a4c8fefa51fe` |
| Bundle run ID | `968e294f-d8dc-461c-90f0-fa02305c6a14` |
| Denied process | PID `19101`、UID `10372` |
| 结果 | `failed/completed=false`，`permission phase silence deadline exceeded` |

这次 initial journal 确认 root UID 0。`deny-startup` 和 `grant:active` 均 confirmed：root SCAN/CONNECT revoke、OS denied readback 和恢复 grant 命令真实执行成功。Denied App 原始日志依次包含：

- command `51000` 的 `PoweredOn + Denied` snapshot 与成功 initialize terminal；
- command `51002` 恰一次 `permission` terminal，平台细节为缺少 BLUETOOTH_SCAN 的 SecurityException；
- denied Probe close 后 17 项逻辑资源为零，`denied:passed`、runtime close 与 phase passed。

这些是本次具体阶段的原始检查结果，**不是完整 permission-transition 或 RF pass**。Host 尚未发布 `authorize_active`，没有 active process、held + queued read、活动 CONNECT revoke、reason 8 或 restored phase。

失败原因：`_silence()` 将至少 300ms 的观察期误用成 `300ms + 2×poll` 的完成时限；同时 `_wait()` 允许七条 ADB 身份复核跨越该时限，下一次检查先报 deadline，再检查观察期已完成。实际 `phase silence` pin 的记录包含七次每次约 55–60ms 的 ADB 往返，与该短时限冲突。

修复仅区分 **minimum observation interval** 与 **maximum capture deadline**：保持 300ms 完整观察、持续 log drain、PID/UID/sourceDir/procstart 复核和原 absolute capture deadline，不再给正常身份复核附加两次 poll 的完成预算。未修改 4s revoke、6s death、2s ARM ACK、native watchdog 或 provider hold 预算，也不把握手类最大时限套用该观察逻辑。合成回归覆盖 500ms 身份复核不误超时，以及剩余总预算不足时不能缩短观察或逃逸 absolute deadline。

失败后 `restore-initial-grants` confirmed，journal final SCAN/CONNECT 均 true，cleanup errors 为空；radio final ON。Physical cleanup 确认 advertising stopped、hold inactive、peer count 0；后续独立只读确认权限仍 granted、radio ON、provider PID 标记不存在。失败 bundle 的离线完整性校验不升级 verdict；本次未自动重跑。

静默修复后实跑：定向 capture/ledger/parent/bundle **96 tests / 14.240s / OK**；`tools-fixture-test` **839 tests / 54.415s / OK**；`fmt`、`lint`、Mermaid（26 diagrams / 139 Markdown files）、UTF-8/fences/whitespace/conflict 检查与 `git diff --check` 均通过。这些是软件回归，不是新一轮硬件 attempt。

## 冷启动根因与软件修复

原顺序为 `attest → before_mutations/hold STATUS → doctor 注册 GAP/GATT`。本地 Zephyr 源码表明：Core/vendor 初始化不安装 GATT handlers；Core 注册 GATT 后才安装 STATUS handler。STATUS 本身接受 index 0，且不因 IDLE 拒绝。故当前启动顺序依赖上次运行留下的暖状态。

原日志未保存拒绝响应 payload，因此不声称已经在线捕获具体 `UNKNOWN_CMD` 状态字节。源码验证的是初始化缺陷及其与失败位置的一致性。

[`doctor.py`](../../../tests/peripherals/nrf52840/doctor.py) 提取现有 probe/register/reprobe 为 `ensure_services()`；[`direct_fixture.py`](../../../tests/peripherals/nrf52840/direct_fixture.py) 改为：

```text
live attestation
→ ensure GAP/GATT command services
→ before_mutations：hold / peer 安全检查
→ profile provisioning
→ advertising
```

没有提前执行 profile mutation，没有把拒绝当作 IDLE，没有绕过 foreign hold 检查。共享修复也消除了 adapter hook 的同类冷启动顺序隐患，但未重跑 adapter 或 permission 真机 gate。合成回归通过真实 DirectFixture.start/BtpSession/ReadHold 验证冷/暖启动、attestation 失败、foreign hold、unknown STATUS 和 hook 失败的 fail-closed 边界。

## 初轮集成与冷启动修复验证

| 检查 | 本轮结果 |
|---|---|
| RN 全量 Jest | 13 suites / 346 tests passed |
| RN `host-check` | passed |
| 冷启动修复后 `tools-fixture-test` | 779 tests passed |
| Android permission/state controller regressions | 51 tests passed |
| Android Release APK | assembleRelease passed；未在本 attempt 安装 |
| 失败 bundle 离线完整性 | verified，failed 保留 |
| `pixi run just fmt` / `lint` | passed |
| `pixi run just docs-check-mermaid` | passed |
| `git diff --check` | passed；既有 CRLF 转 LF 提示不构成错误 |

## UID 修复与工具收敛后的软件验证

- `pixi run just react-native tools-fixture-test`：**832 tests / 54.018s / OK**。本入口现同时包含 RN tooling、Peripheral 与 Android permission/radio controller tests；不与先前分项相加，也不据此推断 RF。
- 定向 controller、permission parent/bundle、fixture 与 transport regressions 通过。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 与 `git diff --check` 通过。
- 只读 phone-lease 收尾迁到 `AndroidRadioController.finish_observation()`；permission runner 不再操作 controller 的私有收尾状态。原 radio-transition `finish()` 的恢复策略不变；保留 permission-before-release、final ON 失败先停止 owned App、只读路径不切换 radio 的回归。
- 删除无调用 fixture wrapper 和无独立不变量的 direct-delegation test；没有删除 maintained CLI、独特安全回归、归档历史或失败证据。`AGENTS.md` 和 [host tooling 架构](../../research/react-native/react-native-host-tooling-architecture.md) 改为当前规则与职责导航，不再积累过期日志/文件统计。

本次通过仅属于上述第五次 artifact/host/device/session。任何新门禁仍须独立确认 fixture 并建立新证据；不能重用前四次失败或单阶段检查替代完整结果。最终 native/ABI/package 同候选矩阵、10,000 lifecycle、长稳、Android 两档 API、iOS transition 与 active write/CCCD loss 仍是独立边界。
