# ABI 冻结准备：清单与当前状态

ABI 未冻结。本文只回答三件事：冻结在机械上依赖什么、今天哪些已经通过、哪些只能由人决定。
它不是冻结公告，也不改变 Phase 退出条件。

## 1. 机械门禁（今天实跑）

| 门禁 | 命令 | 结果 |
|---|---|---|
| BleHub 已审公共 API 形状 | `pixi run just binding snapshot-check` | `verified 3 reviewed API snapshots` |
| BLeng + supervisor 已审形状 | `pixi run just binding bleng-snapshot-check` | `verified 6 reviewed API snapshots` |
| Android managed 场景（全部 20 条，一次运行） | `formal-host-android-managed-sequence` | **20/20 通过**（2026-11-26，11.9 min，20 份 bundle 离线 `integrity=passed`、`cleanup_errors=[]`） |
| iOS managed 场景（全部 20 条，一次运行） | `pixi run -e apple-ci python -m tools.react_native.run_bleng_ios_scenes` | **20/20 通过**（2026-11-26，33.4 min，App 树 `662451e7…`，20 份 bundle 离线 `integrity=passed`、`cleanup_errors=[]`）；`devices-kinds` 一次 553 s（自动化通道降级，见闭环报告） |
| 两端 artifact consumer（6 个） | `ffi android-backend-consumer(-build)`、`ffi android-bleng-build`、`apple consumer-ios`、`consumer-macos`、`consumer-bleng-macos`、`consumer-bleng-ios-unsigned` | 2026-11-26 全部通过；Android 消费者另跑设备生命周期 `android_instrumentation=passed` |
| Android native contract | `formal-host-android-native-test <device>` | 2026-11-26 通过 |
| Android native lane（7 次运行） | `formal-host-android-native-*` | **2026-11-26 全过**：`notification`（single/sequence/overflow）、`adapter-transition`（radio OFF→ON，`radio=restored`）、`permission-transition`（`death_reason=8`、`hold=held`）、`multi-link`（2 peers，1 轮与 2 轮）；同一 App/测试 APK（`a1fcb8ef…` / `0578d9b2…`），见 [`2026-11-26-android-native-lanes.md`](../../testing/reports/2026-11-26-android-native-lanes.md) |
| 包文档与示例契约 | `tools/tests/react_native/test_api_docs.py`、`pixi run just react-native quickstart-typecheck` | 2026-11-26：6 条契约通过；示例类型层通过 |

全部证据、候选哈希与未做项见
[`2026-11-24-freeze-closure.md`](../../testing/reports/2026-11-24-freeze-closure.md)，最新一轮序列见
[`2026-11-26-android-sequence-20.md`](../../testing/reports/2026-11-26-android-sequence-20.md)。

九份快照是冻结的**机械替身**：任何生成产物或 Rust 公共形状的漂移都会让 CI 失败，除非有人跑
`snapshot-update` 并因此留下一次显式评审。所以「冻结」在这套工具里等于「不再无评审地更新快照」。

快照文件：`api-snapshots/{rust,kotlin,swift}.txt`（BleHub）、`bleng-{rust,kotlin,swift}.txt`（BLeng）、
`supervisor-{rust,kotlin,swift}.txt`（supervisor）。

## 2. 只有人能关闭的项

这些都是**现在改便宜、冻结后改贵**的语义决定，必须由 review 拍板，代码无法自证：

1. **哪两个 component 是产品面。** 当前生产候选是 `blehub` + `bleng`；`bleng-supervisor` 与 candidate-only
   feature 仍在收敛。冻结前要确认没有任何 candidate 脚手架漏进默认产物。
2. **错误 taxonomy。** `BleErrorCategory` / `BlengError` 的分类与文案是否稳定到可以承诺跨语言一致；
   新增分类在冻结后是破坏性变更。
3. **deadline 表示。** 绝对 monotonic 时间戳进入 foreign 面之后不可回退，必须确认这是长期表示。
4. **取消语义。** `begin_*` 同步有界提交、独立 cancel、取消回执与「被动终止」的区分是文档承诺。
5. **单 reader 事件面。** `nextEvent` 只有一个逻辑 reader；这是 API 形状，不是实现细节。
6. **资源归零与 owner 生命周期。** close/fault 的终态、backend 资源计数、cleanup 错误如何上报。
7. **名称与展示。** `BLEngine`/`blehub`/`bleng` 的命名决定（展示名与 crate/component 标识不一致）需要一次
   明确确认，避免冻结后再改名。

以上每一项在 `.plan/PLAN.md` 与 `docs/research/bleng/` 下都有候选评审记录，冻结时以那些记录为准。

**签署进度（2026-11-24）**：九项已全部拍板，记录在
[`abi-freeze-review-packet.md`](../bleng/abi-freeze-review-packet.md#已签署的决定2026-11-24)：
其中 D7 追加“`begin*` 之外可加一层可移植 async/await 便捷接口（底层只调 `begin*`/`wait`/`cancel`）”，
D8 明确“开发阶段不承诺版本兼容性”。第 4/5/6 项的语义未变，只是多了 D7 这一条派生面；
冻结前该层必须已经存在并且纳入快照。

## 3. 明确不属于冻结条件的项

- **压力/长稳**：仍后置，冻结前只要求功能收敛的真机证据。
- **第二台 OEM 设备**：`expected_local_name` / service UUID 已经让 fixture 与机型解耦，但一台三星或
  一加之外的设备从未跑过；这属于覆盖缺口，不是冻结阻塞。
- **Windows / Tauri / 旧 Appium lane**：已归档，不因冻结复活。
- **并行执行**：两条 lane 可以并行（两台 DK），这是执行效率，不改变任何证据含义。

## 4. 冻结前建议的收尾顺序

1. 关掉 §2 的七项评审（人）—— 2026-11-24 已全部签署，见
   [`abi-freeze-review-packet.md`](../bleng/abi-freeze-review-packet.md#已签署的决定2026-11-24)。
2. 在**同一份候选**上跑一个完整闭环：两端 managed 场景各 20 条 + 4 条 Android native lane +
   两端 artifact consumer 编译 + 九份 `snapshot-check` —— **2026-11-26 已重建一次**，见
   [`2026-11-26-closed-loop.md`](../../testing/reports/2026-11-26-closed-loop.md)。冻结前需在同一份
   产物上重做一次，并保留该报告的形状。
3. 「更新快照需要什么评审」已写进 `AGENTS.md`（`Architecture guardrails` → 「API snapshots are the
   freeze's mechanical stand-in」）：允许的只有「被评审过的公共面变更 + 全消费者编译 + 报告」，
   不允许为了绿灯跑 `snapshot-update`。
4. 冻结之后仍允许新增场景（场景不是 ABI），但任何 DTO/handle/错误面的变化都要走解冻流程。

## 5. 当前未做的事（诚实清单）

- **同一份候选上的完整闭环已有一次**（2026-11-26，两端各 20/20 + 4 条 native lane 共 7 次运行
  + 六个 artifact consumer + 九份快照；产物指纹与逐场景耗时见
  [`2026-11-26-closed-loop.md`](../../testing/reports/2026-11-26-closed-loop.md)）。同一轮修掉了两处
  会造成假失败的缺陷：`background` 的 pull 等待被上一轮的值满足、`auto-connect` 的 fixture 把
  「立刻连上」当失败。iOS 的 `devices-kinds` 花了 553 s（同一计划别处为 13 s），原因是设备侧
  XCTest 自动化通道成片卡在 60 s 单位上（[报告](…/2026-11-26-closed-loop.md)），无代码层根因；
  计划已不再依赖该瞬态。冻结那天需要在同一份产物上重做一次。
- **iOS 已有两条 20 条序列**（均 2026-11-26，均全部离线 `integrity=passed`）：`47.4 min`（树
  `7e56a50d…`，其中 `link-facts` 1407 s）、`33.4 min`（树 `662451e7…`，其中 `devices-kinds` 553 s）。
  另有新树上的一次 19/20（`recovery-ten` 的 `announce:9`），它暴露了真正的环境根因：**iPhone
  屏幕熄灭**（设备截图为全黑，亮度 0）而 `lockState` 仍报未锁屏。`run_ios_scan` 现在有
  `phone.display` 门：lease 前读一次截图，黑屏直接拒绝并指向「自动锁定 → 永不」。三次慢（1407 s /
  1168 s / 553 s）都是同一形状：`press≈120.6 s`、`hittable≈0.1 s`、`setText` 取元素时多出约 61 s
  —— 设备侧 XCTest 自动化通道成片卡在 60 s 单位，无代码层根因；驱动已记录 `hittable=`/`press=`，
  慢阶段保留 XCTest 控制台尾部，计划不再依赖该瞬态。两轮详见
  [`2026-11-26-ios-sequence-20.md`](2026-11-26-ios-sequence-20.md)。
- **4 条 Android native lane 已在 2026-11-26 重跑**（`notification` single/sequence/overflow、
  `adapter-transition`、`permission-transition`、`multi-link`），全部通过，证据见
  [`2026-11-26-android-native-lanes.md`](../../testing/reports/2026-11-26-android-native-lanes.md)。
  其中 radio 与权限变更在这台机器上是安全的：HANS 解析证明 App 只在后台持有 `connectedDevice`
  前台服务且 `freezer_cutoff_adj` 不覆盖它（[`2026-11-26-hans-rescope.md`](../../testing/reports/2026-11-26-hans-rescope.md)）。
- **artifact consumer 已在 2026-11-26 重做**：两端六个消费者全部通过，其中 Android BleHub 消费者
  还跑了设备生命周期测试。本轮它们在编译时真暴露了两处漂移（10 个新 `BleBackend` 成员没人实现；
  消费者把资源清单数量写死成 11），见
  [`2026-11-26-artifact-consumers.md`](../../testing/reports/2026-11-26-artifact-consumers.md)。
- 没有做压力/长稳。
- 没有第二台非一加/非三星设备。
- adapter 状态流已按 2026-11-24 的决定实现到 JS 面（lane + 快照），Android 后端缺少被动
  adapter 事件发布，修复前该面在 Android 上收不到真实变化
  （[报告](../../testing/reports/2026-11-24-adapter-state-stream.md)）。这一条也解释了为什么
  `adapter-control` 场景只证明“平台答案被如实上报”，不声称 radio 状态变化。
- 另两个 API 面问题（`waitUntilEnabled` 联合的判别字段、广告不带设备身份）仍待人工确认，列在
  闭环报告 §7。
