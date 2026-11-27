# 冻结闭环：同一候选上的两端场景、Android native lane 与机械门禁

这份报告只做一件事：把**同一份候选**上的全部证据串成一个闭环，并明确哪些项目仍然是分轮取得、
哪些根本还没做。它不是冻结公告；冻结仍取决于
[`abi-freeze-readiness.md`](../../research/cross-cutting/abi-freeze-readiness.md) §2 的人工签署。

## 1. 候选固定（本轮全部证据的输入）

| 项 | 值 |
|---|---|
| Android App APK | `c1f813f968707fd9015757194b70e572ea60b20bdad06e78bfb83072c4511785`（初轮）
| Android App APK（adapter 流） | `8d77e5870123211a3a2f913b5d38f284a321263ad61096c0fb59ab46d8d8674b` |
| Android test APK | `a615af544cbc5bae883e72f92eb82ae6a45fb713e25443df30668890d538bb3c`（初轮） |
| Android test APK（adapter 流） | `0ab5572c401a9a5f0d1ea2f8b795f7b3a9e09bd3a33b56129a0dc746f03fe289` |
| iOS App 树 | `d2cc5a69f7c2bd9bdfc0b882a3808434fcafb9e76c799f860abb1068a89fa9d7` |
| iOS xctestrun | `tests/consumers/react-native-bleng/ios/build/device-derived/Build/Products/BleHubBlengHost_iphoneos27.2-arm64.xctestrun` |
| fixture 固件/契约 | `multifunction-rf-v2-a`（DK-A, `jlink:1050252028`）、`multifunction-rf-v2-b`（DK-B, `jlink:1050251479`） |
| 手机 | Android OnePlus PHB110 `3ac5cc09`（rooted）、iPhone 14 Plus `00008110-000C61160C39401E`（iOS 27.2.1） |

两侧 fixture 在整轮里报同一个 `boot_nonce`（Android `feaad191006157648610d5c3c0307d40`、
iOS `fc3da81101ef3b07471a216f57d62241`），即没有换固件、没有重启板子；每个场景结束时
`fixture_close` 都是 `advertising_inactive: true` + `resources_closed: true`
（BTP GAP controller info 的 advertising 位为 0），`cleanup_errors` 全空。

## 2. 两端 managed 场景（12 + 12）

| lane | 命令 | 结果 | 证据根 |
|---|---|---|---|
| iOS | `formal-host-ios-managed-sequence` | `scene_sequence=passed`，12/12 场景 `passed`，离线复验 12/12 `integrity=passed` | `.work/ios-native-sequence/2026-11-24-freeze2` |
| Android | `formal-host-android-managed-sequence` | 12/12 场景 `passed`，离线复验 12/12 `integrity=passed` | `.work/android-native-sequence/2026-11-24-freeze` |

场景集合两端同构：`scan`、`notification`、`indication`、`background`、`io`、`cancellation`、
`write-reject`、`ccc-enable-reject`、`ccc-disable-reject`、`passive-disconnect`、`recovery`、
`recovery-ten`。每个 attempt 的 `result.json` 三态（`status` / `scenario_status` /
`session_status`）都是 `passed`，`host_gate_status=pending` 是**设计如此**：host gate 是人工
判定，不是自动通过项。

iOS 每个场景的 `background` 步骤声明的 30 s dwell 是场景自己持有的事实（`scene_plans.py`
`BACKGROUND_DWELL_SECONDS`），本轮 `background` 一次通过。

离线复验命令（两端同一套）：

```sh
pixi run python -m tools.react_native.bleng_scan_evidence <attempt-dir>
```

## 2b. adapter 流落地后的 Android 复跑（同一新候选）

新增 Android 后端的被动 adapter 发布（详见
[`2026-11-24-adapter-state-stream.md`](2026-11-24-adapter-state-stream.md)）之后，Android 侧
**整套在本日重跑**，用新的 APK / test APK：

| lane | 结果 | 证据根 |
|---|---|---|
| managed 12 场景 | `scene_sequence=passed`，12/12，离线复验 12/12 `integrity=passed` | `.work/android-native-sequence/2026-11-24-backend-adapter` |
| adapter-transition（含新 `adapter-stream-on`） | `passed`，`radio=restored`，lane 交付 `poweredOn` | `.work/android-native-lanes/adapter-stream4` |
| notification single / sequence / overflow | 全部 `passed` | `.work/android-native-lanes/b-notification-*` |
| permission-transition | `passed` | `.work/android-native-lanes/b-permission-transition` |
| multi-link 1 轮 / 2 轮 | 全部 `passed` | `.work/android-native-lanes/b-multi-link-rounds*` |

`adapterReceivers` 现在是 `required_zero_resource_names` 的一员，12 个场景的退休证明覆盖了它。

**iOS 证据因此过期**：本轮改了 `crates/bleng`、`crates/bleng-supervisor` 与 Nitro spec，
iOS App 树（`d2cc5a69…`）不再对应当前源码。冻结前必须重建 iOS 设备测试包并在同一候选上重跑
12 场景；这是当前唯一未完成的冻结前置。

## 3. Android native lane（与 UI 场景无关，7 次运行）

| lane | 参数 | 结果 |
|---|---|---|
| notification | `--scene single` | `passed`，stages 到 `disconnected,done` |
| notification | `--scene sequence` | `passed`，`values-requested,values-matched` |
| notification | `--scene overflow` | `passed`，`overflow-observed`（fail closed 而非静默丢弃） |
| adapter-transition | 收音机 OFF → ON | `passed`，`radio=restored`，refusal code `bleng.operation.adapter_unavailable` |
| permission-transition | denied / 读中死亡 / restored | `passed`，`death_reason=8`，`hold=held` |
| multi-link | `--rounds 1` | `passed`，`peers=2`，drop 首个 peer 后第二个仍交付值 |
| multi-link | `--rounds 2` | `passed`，同一拓扑跑两轮 |

证据根：`.work/android-native-lanes`、`.work/android-native-notification`。全部使用上面的
APK 哈希，`host_gate=pending`，`cleanup_errors=[]`。

adapter/permission 两条 lane 在本轮末尾又重跑了一次（`*-r2` attempt）：这两条 runner 原来把
fixture 身份拼成 `--fixture-identity`，与 notification/multi-link 的 `--fixture-resource-identity`
不一致，一条真实运行就是因为旗标名字而失败。现在四种单 fixture lane 只有一个拼写，并由
`tools/tests/react_native/test_fixture_identity_vocabulary.py` 从源码里的选项字符串断言（重跑仍全过）。

本轮运行时两个 lane 与 iOS 序列并行：DK-A/iPhone 与 DK-B/Android 各自持有独立 lease，
互不共享 fixture，唯一共享的是主机 CPU（这是执行效率，不改变证据含义）。

## 4. 机械门禁（本轮实跑）

| 门禁 | 命令 | 结果 |
|---|---|---|
| BleHub 已审公共 API 形状 | `pixi run just binding snapshot-check` | `verified 3 reviewed API snapshots` |
| BLeng + supervisor 已审形状 | `pixi run just binding bleng-snapshot-check` | `verified 6 reviewed API snapshots` |
| RN 正式 host 类型与自动链接 | `pixi run just react-native formal-host-check` | 通过（含 D7 await 层） |
| RN 正式 host jest | `pixi run just react-native formal-host-test` | 10 suites / **136 tests passed** |
| RN 工具链回归 | `pixi run python -m pytest tools/tests/react_native -q -n auto --dist loadfile` | **905 passed, 21 skipped, 1698 subtests** |
| 示例类型层 | `pixi run just react-native quickstart-typecheck` | `passed` |
| 文档形态 | `pixi run just docs-check-mermaid` | 279 个 Markdown / 27 张图通过 |
| Rust 与 Python lint | `pixi run just fmt`、`pixi run just lint` | 通过（clippy 无问题，ruff 全绿） |

artifact consumer 编译（`ffi android-backend-consumer-build`、`ffi android-bleng-build`、
`apple consumer-macos`、`apple consumer-bleng-macos`、`apple consumer-bleng-ios-unsigned`）
在**同一候选**上于本日早些时候通过；本轮之后的改动只有文档、`tools/` 与 `examples/`，
不进入这些产物。

## 5. 本轮同时交付的消费面（包文档与示例）

冻结前一个包必须能回答"用户怎么用"，所以本轮补的是消费面，不是新能力：

- `integrations/react-native-bleng/API.zh.md`：两种形状（lane / operation）、能力表、await 层、
  按 `code` 决策的失败面、bounds/lifecycle、平台注意事项，以及"这个 API 不做什么"。
- `examples/react-native-bleng-quickstart/`：扫描 → 连接 → 发现 → 读 → 订阅 → 断开 → 关闭的
  参考屏（只有 JS；原生模板不 vendored，所以它明确是"复制进你的 App"而不是"独立可跑"）。
- 两道新的文档契约（`tools/tests/react_native/test_api_docs.py`，4 条）：
  每个可调用能力必须出现在 `API.zh.md` / `API.md`；公共导出必须出现在任一份或冻结清单；await 层必须被写成
  派生的；**每个 `examples/*` 必须能从包 README 到达**（没有入口的示例等于死代码）。
- `quickstart-typecheck` recipe（`tools/react_native/quickstart_typecheck.py`）：复用正式 host
  已装的 toolchain 编译示例，不写进示例目录、不装第二份依赖。它确实会红：把
  `isBlengFailure(...)` 改回 `value.failure` 时以 `TS2339` 失败。

写这两份文档时发现并修掉的真实问题：`waitUntilEnabled` 的成功臂与失败臂没有共同判别字段，
直接读 `.failure` 在类型层不成立——示例原本就是这么写的，现在文档给出了统一写法
（`isBlengFailure`），并在 `API.zh.md` 里解释了为什么。

## 6. 仍然没做的事（诚实清单）

- **压力/长稳**：仍后置。`multi-link --rounds 2` 是拓扑重复，不是 soak。
- **第二台 OEM**：fixture 已与机型解耦（`expected_local_name` / service UUID），但没有第二台
  非一加设备跑过。
- **artifact consumer 不在本轮同一时刻**：见 §4 说明，代码未变，证据取自同日早前运行。
- **ABI 冻结本身**：未冻结。冻结等于"不再无评审地 `snapshot-update`"，以及 §2 的人工决定，
  本报告不改变任何一项。

## 7. 交给人审的 API 问题（冻结前决定，冻结后是破坏性变更）

1. **adapter 状态流（已落地到 JS，Android 后端缺口未修）。** 现状：`start()` 只报初始化那一刻，
   `beginObserveAdapter()` 是之后唯一的观察面（lane，有界 64 条，溢出 fail closed，端点可重读）。
   四层已打通：Rust lane + UniFFI 视图 + Nitro spec + Swift/Kotlin 适配 + JS 类型 + 双语 API 文档，
   快照按评审流程更新过。真机运行暴露出一个**既有平台缺口**：iOS 后端每次
   `centralManagerDidUpdateState` 都发布被动快照，Android 后端只在 initialize 时发布
   （`AndroidBleBackend.kt:1582`），所以 Android 上这条 lane 收不到真实的收音机变化。
   详见 [`2026-11-24-adapter-state-stream.md`](2026-11-24-adapter-state-stream.md)。

2. **`waitUntilEnabled` 的联合没有判别字段。** 文档已给出统一写法，但若希望调用方不用 guard，
   就得在 DTO 上引入判别字段——同样是冻结前才便宜的改动。
3. **扫描广告不带设备身份。** 这是刻意的（`peripheral` 是 owner-bound 能力，不是 device id），
   代价是 App 无法按设备去重，只能按"最近看到"列表呈现。示例已按这个事实编写。
