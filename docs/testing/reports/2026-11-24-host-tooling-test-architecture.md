# 宿主工具链测试架构收敛（离线部分）

日期：2026-11-24
范围：`tools/`、`tools/tests/`、`just/react-native/mod.just`、docs
设备：未触碰（Android OnePlus PHB110 `3ac5cc09`、iPhone 14 Plus `00008110-000C61160C39401E`、nRF52840 DK-A）
结论：离线架构工作完成；**两端 lane 的旧真机 pass 不能继承**，须在设备上重新挣得。

## 1. 做了什么

本轮只做离线部分：把测试的分层、约定与守卫写下来并落到代码里，去重与去冗余，不碰设备。

### 1.1 observed 人群（新建）

`tools/tests/observed/` 存放输入来自真机回答的回归，与 `tools/tests/react_native/`（合成输入）物理隔离。
两个人群失败时的可信度相反，混在一起会埋掉高价值信号。

| 文件 | 回归 | 真机出处 |
|---|---|---|
| `test_android_scene_driver.py` | 2 | `.work/android-native-sequence/2026-11-24-a/write-reject/8178a98a-…/driver/scene-write-reject.json`（rc −15，3.197 s）；手工读取的 `adb exec-out cat` exit 0 |
| `test_ios_app_cleanup.py` | 2 | `.work/ios-native-sequence/2026-11-24-a/scan/release/01-apps.json`（`outcome: success`，`apps: []`，当时 App 在运行 PID 4647） |
| `test_ios_scene_driver.py` | 1 | `.work/ios-scene-driver/e42a99ac-f793-4851-bc18-d5e854187f19/xcodebuild.log` |
| `test_population.py` | 守卫 | — |

支撑 fake 移到 `tools/tests/react_native/support/`（`android_scene_device.py`、`coredevice_fake.py`），
因为合成契约测试用同一批手机替身；fake 仍走生产 validator（`ios_sdk.official_result`、`require_receipt`）。

### 1.2 一个真缺陷：五条回归从未执行

`unittest` 与 `pytest` 都只收集 `test*`，而五条回归的方法名是 `regression_*`。它们通过了 AST 级的
命名检查，却从未被任何 runner 执行——上一轮报告的「5 regressions passed」不成立。

修法：

- 统一改名为 `test_regression_<where>_<symptom>`。`test_` 让 runner 收得到，`regression_` 让
  `-k regression_` 与 grep 仍能切分。
- `test_population.py` 的必需前缀改为 `test_regression_`，并**扩展到模块级函数**：原先只遍历
  `ClassDef.body`，模块级回归可以绕过全部三条规则。已用植入文件验证：错名、缺 `Trigger/Symptom/
  Provenance`、模块级缺事实、模块级合规——四种都被正确判分。

修复后真跑又抓到第二条：`test_regression_iphone_coredevice_first_query_is_asked_again` 的 fixture
只脚本了 apps 回答，没脚本设备当时也给过的两次 process 清单（fake 因此报 `unexpected … processes
query`）。断言与代码行为都是对的，补全 fixture（`processes=[[], []]`）。

### 1.3 去重与一个真缺陷

- `tools/host_command.py::stop()`：收拢 9 处手写的 terminate→wait→kill。保留 3 处有真实平台知识的
  专门实现（`capture_io.stop_local`、`run_quiet.py`、`ios_pod_refresh._retire`、多链路 `communicate()`
  排空）。**顺带修掉一个真缺陷**：docstring 说最后一次等待会报告超时，实现却把异常吞了；现在最后
  一次 `process.wait(timeout=grace)` 会抛出。`tools/tests/test_host_command.py` 9 条覆盖。
- `tools/file_digest.py::sha256_file`：收拢 12 份 `_sha256(path)`（三种实现）；`scene_native._sha256`
  是死代码，删除。
- 共享 Android instrumentation 驱动：两处 `subprocess.Popen` 改为 `adb.start_command`。

### 1.4 进度日志

`ProgressReporter(journal=...)`、`rf_runner.reporter()`、`rf_runner.add_journal_argument`、常量
`JOURNAL_FLAG="--progress-journal"` / `JOURNAL_NAME="progress.jsonl"`。7 个 producer CLI 全部改用
`reporter(...)`（默认 `<output>/progress.jsonl`），sequence 给每个场景传同一个 journal 并写
`scene` / `finished` 行（中断也写）。journal 永远不放进 sealed evidence，也不参与 verdict。

### 1.5 executor 契约补全

- 新增 `tools/tests/react_native/test_scene_android_native.py`（13 条）：pinned test APK（缺失/符号
  链接/运行后变更）、平台与 owner/lease 类型拒绝、`_artifact()` 形状、一个阶段跑合并 plan 且
  `launch=False`/`require_owner=None`、in-band `on_handoff` 透传、`instrumentation_returncode`、
  hierarchy 诊断 64 KiB 截断、无 hierarchy 时不产生空文件。
- `scene_native`（1932 行 heart）目前经 iOS executor 覆盖（约 1900 行测试）。抽成 executor-free
  套件记为 **staged**：抽之前不复制第二份阶段/派生逻辑。

### 1.6 iOS 失败时保留证据

`run_ios_scene_driver` 原 `_export_receipt` 是一次调用：导出附件并在同一步要求恰好一个 receipt。
一旦 receipt 缺失/歧义/外来 scope，它先抛错，已导出的 hierarchy 随临时目录一起被丢弃。

现在拆成两步：

- `_export_attachments(bundle, destination, …) -> ExportedAttachments`（`diagnostics` / `receipts` /
  `declared`）——导出后立即构建清单。
- `_read_receipt(destination, export)`——单独校验，拒绝时用 `declared` 说明它看到了什么。

`run_scene` 在要求 receipt **之前**把 `diagnostics` 写进 `result["attachments"]`，于是失败路径上
`_publish_diagnostics` 仍能把 hierarchy 拷进 bundle。测试：`test_an_unusable_receipt_still_leaves_what_
was_exported_listed`（lane 层）+ 失败场景发布诊断文件的断言（executor 层）。

## 2. 约定与守卫

新文档 [`docs/research/react-native/host-tooling-test-architecture.md`](../../research/react-native/host-tooling-test-architecture.md)
记录分层与 C1–C7：

| | 约定 | 守卫 |
|---|---|---|
| C1 | 一个测试只替换一个接缝 | `test_layout.py` support 依赖规则 + review |
| C2 | 测试输出不是 RF 证据 | 报告/`record.json` 值域 |
| C3 | observed 与 synthetic 物理隔离 | `observed/test_population.py` |
| C4 | fake 必须通过 lane 自己的 validator | support 只调生产 parser |
| C5 | 单一 discovery 根，切片只是 `-k` | `test_recipe_wiring.py` |
| C6 | 失败自解释 | executor 失败路径测试 |
| C7 | 进度是观测不是证据 | journal/sequence 测试 + 目录布局 |

切片现在四条，全部是同一 discovery 上的 `-k`，且由测试断言命令行逐字相等：

```sh
pixi run just react-native tools-test
pixi run just react-native tools-test-android-safety
pixi run just react-native tools-test-lifecycle
pixi run just react-native tools-test-observed
```

## 3. 验证（本轮实际运行）

```text
pixi run ruff check tools                          All checks passed!
pixi run ruff format --check tools/tests           clean
pixi run python -m pytest tools/tests -q -n auto --dist loadfile
                                                   976 passed, 21 skipped, 2737 subtests
pixi run just react-native tools-test-observed      7 passed, 993 subtests
pixi run python -m unittest tools.android.test_run_instrumentation tools.android.test_state_control
                                                   29 tests OK
npx jest (tests/consumers/react-native-bleng)      122 passed, 9 suites
pixi run just lint / docs-check-mermaid            passed, 272 Markdown files
```

observed 守卫用植入文件做了正反两向验证（错名/缺事实/模块级/合规），验证后删除植入文件。

## 4. 限制与下一步

- **Android 已重挣（2026-11-24）**：11 个 managed 场景 `scene_sequence=passed`（run root
  `.work/android-native-sequence/2026-11-24-d`），逐 bundle `integrity=passed`/`host_gate=pending`/无
  cleanup 错误；四条 native lane（notification ×3、adapter-transition、permission-transition、
  multi-link 2 peer × 2 轮）同样通过。过程中发现并根治了一个 **plan 竞态**：tap 只注入触摸，
  UiAutomator 不等待 App 处理器，紧接的 ledger 读取可能读到 tap 之前的内容；新增
  `_await_recorded()` 作为声明式 settle，并加守卫要求每个 ledger 读取前一步必须是 wait/waitAny/still。
  详见[报告](2026-11-24-android-lane-re-earned.md)。
- **iOS 仍未跑**：iPhone「设置 › 开发者 › UI 自动化」未生效时 XCTest 以 `Timed out while enabling
  automation mode`（exit 65）失败。`run_ios_scene_driver` 的导出拆分与 iOS 11 场景都不算已验证。
  **同日后续**：该开关在手机重启后会复位，重新开关后 iOS 11 场景已首次完整通过，并修掉三个真缺陷
  （诊断名冒号、释放后被恢复的竞态、答过的弹窗被记成 `absent`），见
  [_iOS 车道重挣 + 场景耗时收敛_](2026-11-24-ios-lane-and-scene-speed.md)。
- **staged**：`scene_native` 的 executor-free 套件；其余 Popen 创建点统一（`bleng_ios_native_common`、
  `bleng_ios_multi_link`、`run_ios_rf_scan`），前提是先真机验证 iOS lane。
- ABI 未冻结；本报告不改变 Phase 退出条件，也不构成释放就绪。
