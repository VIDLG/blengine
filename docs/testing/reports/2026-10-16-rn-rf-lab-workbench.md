# RN RF Lab workbench：最终 Android 同 APK 三门禁

报告日期：2026-10-16；设备时间使用 UTC。依据生成的 [validation summary][summary] 和三份实际 manifest/ledger 核对，不重放原始 events，也不把 UI 显示当作 host verdict。

## 结论与候选绑定

**同一冻结 release APK 的 Android `active-loss`、`startup-off`、`full-suite` 三个独立 gate 均 passed，三份 bundle 的离线 integrity verification 均 passed。** 它们是同一 App 的三次独立启动、不同 session，不是三个场景在同一 JS runtime 内连续执行；full-suite 自身仍是同一次启动的基础十项 + RecoveryTen。

- 手机：OnePlus 11 / `3ac5cc09`。
- 三份 manifest 的 APK SHA-256 均为 **`3cff6dcc8da044b5e76eef12032ea86a44cb9798fc3d3d0ceed605f618769676`**，大小均为 `38,133,774` bytes；各 attempt 内冻结安装输入一致，不只比较 canonical 构建输出名。
- Git provenance 均为 `75edda34b20b043ef282d7d94614c9a1169c6179`、`dirty=true`、`source_to_build_attestation=false`。这不是 clean locked release、设备端 binary byte attestation 或签名信任链。
- 三场各自 live-attested DK-A image SHA-256 均为 `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a`；普通运行未刷机。

| Gate / manifest | Bundle UUID | Runtime/fixture session / App PID | UTC 起止 |
|---|---|---|---|
| [active-loss，`33c1dc…`][active] | `7f5a86a8-0836-4985-8620-c12f69863703` | `40cdcd39-30c7-438f-9881-2cad0d0c4a22` / `19035` | `01:34:39.915198–01:34:52.879885` |
| [startup-off，`ec419b…`][startup] | `869f1776-5720-4938-a8ae-6065106fc1cb` | `23b98a63-a799-48c9-8498-4b67580450a3` / `20744` | `01:35:04.478646–01:35:15.681828` |
| [full-suite，`85a866…`][full] | `ff6ca6e1-a87a-4063-a3e2-02913abdc22e` | `4a027f60-d7d1-4917-8d0d-e2e708da6dd5` / `21662` | `01:35:24.396924–01:39:37.544179` |

三份 manifest 均为 `status=passed/completed=true/primary_error=null`，cleanup/evidence errors 均为空；主执行日志均记录 `app=kept-open`。Summary 的场景、bundle、APK hash、起止和状态与 manifest 一致。本次文档核对另以现有 `verify_bundle` 对三份原件重新执行只读校验，均通过；离线校验不重新授权 RF。

## 三个门禁分别证明什么

| Gate | 实际 ledger/physical 边界 |
|---|---|
| Active read radio loss | `disconnect-first`：active/queued read `32008/32009` 各一次 `connection` terminal，随后无 commandId 的 passive disconnect；pre-close 旧 route retirement，两个 Probe close 各 17 项全零，ON 后 fresh-Probe I/O。不是 same-Hub recovery。 |
| Startup OFF → ON | 一个 App runtime、OFF/ON 两个 Probe/Hub；OFF 三条预期事件，两个 close 各 17 项全零，ON 后 fresh-Probe RF I/O；radio journal 初始/最终均 `ON`。不是活动 read fault 的替代证据。 |
| Full-suite | ledger `app_runtimes=1/base_flows=10/capture_children=1/recovery_cycles=10/recovery_terminals=100`；恢复段 50 resource snapshots、10 closed-zero snapshots，closed resource totals 全零。基础段含 cancellation/watchdog，不把两个旧 pass 拼接为组合结果。 |

[Active-read 专项报告](2026-10-16-rn-android-active-read-radio-loss.md) 保留首个失败与 `9f5ae4…` 的首次功能 pass（APK `2fd59e…`）。该早期候选不充当本表最终同 APK 结果；[startup 专项报告](2026-10-16-rn-android-adapter-transition.md) 也保留其早期候选记录。

## 页面职责与 UI 证据边界

此次是现有 RN 测试页面的信息层级调整，不是新 App、tool framework 或 BLE runtime：**Tests** 负责分类→场景→setup；**Current run** 保留一个 mounted scenario；**Evidence** 集中技术身份与 App/host 判定边界。Catalog 只描述准确 runtime/discriminator/recipe/fixture，App 仍是唯一执行路由，原 scenario runner 与 host 保留生命周期、RF/control/evidence 所有权。Bounded session summary 不接管 raw event/resource/native identity 或 canonical BLE state，failure 只有显式 manual begin 才能重开。详情见 [页面架构](../../research/react-native/react-native-rf-lab-test-architecture.md#rn-页面信息层级与职责)。

最终 full-suite 后执行了真实 UI 导航：**Current run → Tests → Evidence → Current run**，前后 PID 均为 `21662`，返回后仍显示 `RF Lab full suite passed`；Tests 的会话摘要为 `App checks · Passed`，Evidence 明确显示 `Host gate · Verify on the host`，未伪造 App 收到 host verdict。记录见 [navigation receipt][navigation]，设备截图见 [Tests][tests-ui]、[Current run][current-ui]、[Evidence][evidence-ui]。这是完成后的导航/结果保留检查，不升级为真实运行中切页或所有目录 setup 点击的设备证据；后两类边界仍由软件测试覆盖。iOS 本轮只有共享 TS/JS 软件范围及既有 Swift parse 记录，无新的 device runtime/RF 结果。

## 已完成的软件与构建记录

以下引用现有主执行线日志，不是本次文档工作代跑，测试数量不跨层相加：

| 检查 | 当前已完成记录 | 日志 |
|---|---|---|
| RN Jest | **301 tests / 12 suites passed，34.133 s** | `/tmp/blehub-rf-hierarchy-jest.log` |
| RN tooling + provider combined | **734 tests / 49.438 s / OK**，不推算分项 | `/tmp/blehub-rf-hierarchy-tools.log` |
| TypeScript | `tsc --noEmit`，主执行线报告 passed | `/tmp/blehub-rf-hierarchy-tsc.log` |
| Android release build | `BUILD SUCCESSFUL in 21s`，最终 APK 由上述三 manifest 绑定 | `tests/consumers/react-native-internal/android/build/host-hierarchy-release.log` |
| fmt / lint / Mermaid / diff | 全部通过；UI 导航完成后的复核已执行 | `/tmp/blehub-rf-hierarchy-final-fmt.log`、`/tmp/blehub-rf-hierarchy-final-lint.log`、`/tmp/blehub-rf-hierarchy-final-mermaid.log`、`/tmp/blehub-rf-hierarchy-final-diff.log` |

RF 主执行日志为 `/tmp/blehub-rf-hierarchy-final-active.log`、`/tmp/blehub-rf-hierarchy-final-startup.log`、`/tmp/blehub-rf-hierarchy-final-full.log`。当前 Python 文件/行数另见 [host tooling 实测统计](../../research/react-native/react-native-host-tooling-architecture.md#当前文件与测试清单)，不能沿用扩展前的旧行数或把软件测试数解释为 RF 覆盖率。

## 不外推

- 这是 Android 同 APK 的三个指定 gate，不是完整 Android+iOS/native/package/RN 全矩阵、ABI freeze 或 clean release；未新增 iOS 真机证据。
- 不证明 same-Hub recovery、in-flight write/CCCD radio loss 或 permission revoke/restore。恢复阶段正常 I/O 不等于这些 fault coverage。
- `active-gatt-canonical-v1` 是 bounded audit projection，不是 full native GATT replay；OFF deadline 不表示与 WAITING 原子同刻。App Passed 仍不能替代 host physical/cleanup/bundle verdict。

[navigation]: ../../../tests/consumers/react-native-internal/android/build/rf-hierarchy-navigation.json
[tests-ui]: ../../../tests/consumers/react-native-internal/android/build/rf-hierarchy-tests.png
[current-ui]: ../../../tests/consumers/react-native-internal/android/build/rf-hierarchy-current.png
[evidence-ui]: ../../../tests/consumers/react-native-internal/android/build/rf-hierarchy-evidence.png
[summary]: ../../../tests/consumers/react-native-internal/android/build/rf-hierarchy-validation.json
[active]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-33c1dcddd0a8478b95d22054ded373f6/evidence-bundle.json
[startup]: ../../../tests/consumers/react-native-internal/android/build/rf-adapter-transition-ec419b76474b4938b5d5ecdcd1f25eb7/evidence-bundle.json
[full]: ../../../tests/consumers/react-native-internal/android/build/rf-full-suite-85a866704fe7423694c9e6d3f2c12998/evidence-bundle.json
