# RN host tooling 测试架构：分层、约定与守卫

本文说明 `tools/` 的测试如何组织、每条约定由谁守、以及哪些是刻意未做的。它补充
[`react-native-host-tooling-architecture.md`](react-native-host-tooling-architecture.md)（那里讲职责归属），
不重复其请求/证据契约。报告在 [`docs/testing/reports/`](../../testing/reports/)，属于候选事实，不改本文。

## 结论

1. **一个 discovery 根**：`tools/tests`。所有切片都是同一个根上的 `-k` 过滤，因此「配方说跑什么」与
   「`-k` 跑什么」永远不可能各说各话。
2. **测试文件的位置就是它的层**：命令边界 → 设备协议 owner → 平台中立场景语义（scene semantics） → 信封 → producer/sequence
   → 证据，每层各有对应的测试目录或模块，不为对称而新建 runner。
3. **两个人群**：`tools/tests/react_native/` 是他造输入（synthetic），`tools/tests/observed/` 是真机
   真给出的输入（observed）。它们物理隔离，因为失败时的可信度完全相反。
4. **测试不是 RF 证据**。无设备测试证明的是「代码按契约读/写」，真机 pass 只能由真机重新挣得。

## 分层与测试的一一对应

| 层 | 生产模块 | 测试落点 | 测试证明什么 |
|---|---|---|---|
| 命令边界 | `tools/host_command.py`、`tools/android/adb.py`、`tools/react_native/ios_sdk.py` | `tools/tests/test_host_command.py`、`test_adb.py`、`react_native/test_android_sdk.py`、`test_ios_device.py` | 有界预算、argv 形状、stderr 合并、超时上报、错误词汇 |
| 设备协议 owner | `tools/react_native/run_android_scene_driver.py`、`run_ios_scene_driver.py` | `react_native/test_android_scene_driver.py`、`test_ios_scene_driver.py` | receipt 形状、attachment 导出、失败时保留什么 |
| 平台中立场景语义（scene semantics） | `tools/react_native/scene_native.py` | `react_native/test_scene_semantics.py`（构造校验 + 阶段/切片/发布 + 全部场景的派生） | 阶段顺序、host action、12 个 observation 派生、切片 |
| 平台 executor | `scene_android_native.py`、`scene_ios_native.py` | `react_native/test_scene_android_native.py`、`test_scene_ios_native.py` | 只钉平台专有事实：pinned artifact、平台拒绝、单进程合并运行、诊断文件、plan 命名与 attach 模式 |
| 信封 | `bleng_scan_evidence`、lease、authority | 各 executor 测试 + observed | identity/lease/deadline/rollback/cleanup 不被弱化 |
| producer / sequence | `run_bleng_android_scenes.py`、`run_bleng_ios_scenes.py`、`scene_sequence.py` | `react_native/test_scene_sequence.py`、`test_bleng_ios_scenes.py` | 场景顺序、journal 行、中断也写 `finished` |
| 证据 | `capture_io`、`evidence_io`、`file_digest` | 对应模块测试 | 只写一次、sealed 目录边界、摘要 |

## 约定

### C1 一个测试只替换一个接缝

`Fake`/`support` 只顶替被测模块与外界之间的那**一**层；不 mock 被测模块自己的逻辑。
理由：同时替换两层时，测试通过只证明两个假件互相同意。守：`test_layout.py` 的 support 依赖规则 +
review。

### C2 测试输出不是 RF 证据

任何 `tools/tests` 的 pass 都不能被引用为「真机可用」。它只覆盖「代码如何读/写设备回答」。
守：report 模板与 `record.json` 的值域从不包含 synthetic 路径；由 review 保持。

### C3 observed 与 synthetic 物理隔离

`regression_` 标记只允许出现在 `tools/tests/observed/`。observed 的测试名必须是
`test_regression_<where>_<symptom>`：`test_` 让 runner 收得到，`regression_` 让 `-k regression_` 切得开。
守：`observed/test_population.py`（四条：人群非空且全是回归、别处没有回归、三条事实齐全、模块自述）。

### C4 fake 必须通过 lane 自己的 validator

替身写出的 receipt 要由生产 parser 校验（`ios_sdk.official_result`、`require_receipt`），不得用
测试专用捷径解包。理由：捷径会与生产 parser 一起漂移而测试仍绿。守：support 模块只调用生产函数。

### C5 单一 discovery 根

测试文件要么在 `tools/tests` 下，要么被某个 recipe 具名。切片只能是同一 discovery 上的 `-k` 过滤，
且由测试断言其命令行逐字等于规范形式。守：`test_recipe_wiring.py`（可达性 + 切片形状）。

### C6 失败自解释

一次失败的 run 必须在自带的产物里回答「为什么」，而不是要求读被丢弃的原始目录：
`xcodebuild.log` 尾部、`primary_error`、导出过的附件清单都随结果发布。守：executor 的失败路径测试。

### C7 进度是观测，不是证据

进度日志（`progress.jsonl`）只用于人看「正在做什么」，不进入 sealed evidence，也不参与 verdict。
理由：它由被观测进程自己写，无法 attestation。守：sequence/journal 测试 + 目录布局（journal 与场景根平级）。

### C8 每个等待都声明它期待的窗口

一个守备性的等待不得花满一个「期待某件事发生」的超时：若计划不期望某个状态，它就声明一个短窗口
（`grace`），0 表示只看一眼。理由：`answerAlert` 曾每次等满 4 s，即使 32 次全部没有弹窗，占掉一场
场景四分之一的时间——「等一个不会发生的事」既慢又会掩盖真实问题。同一词汇、同一语义在两端驱动上
成立（iOS 与 Android 都读 `grace`）。守：
`test_ios_scene_driver.py::test_every_composed_prompt_step_states_its_window`。

同一条约定的另一半是**要守住的窗口也必须声明**：`background` 步骤带 `for`，声明 App 必须离开前台多久，
并且两端驱动都拒绝没有 `for` 的步骤——瞬时离开证明不了“订阅活过后台”，而驱动自己决定“多久算后台”
就是把场景策略搬进了执行器。窗口同时出现在计划的步骤里、场景自己的预算里（`min_seconds`）和 receipt 的
`observed["background"]` 里（例：`not-foreground for=30.8s`），派生侧（`_require_dwell`）再拿 receipt 的
实际时长与声明的窗口相比，所以一个“忘了守住窗口”的驱动会在 host 侧失败。守：
`test_scene_semantics.py`（`background` 派生五条）与
`test_ios_scene_driver.py::test_the_background_plan_leaves_the_app_and_returns_to_it`。

推论：**运行期的人工干扰不是测试失败，但必须被看见**。人在场景中拿起手机（例如停在多任务切换器）
会让 `activate`/`tap` 各自花掉几十秒，而判定仍然是通过——序号与耗时因此失去意义。真机 gate 期间
设备必须无人触碰；这也是把「逐步骤耗时」写进 receipt 的理由。

## observed 人群

- 命名 `test_regression_<where>_<symptom>`，`<where>` 是平台或组件，`<symptom>` 是看到的现象。
- 注释块（紧贴函数上方）必须含 `Trigger:`、`Symptom:`、`Provenance:`，`Provenance:` 里必须有日期与
  可追的路径（工作目录、报告或日志）。原始 prompt/凭据不得出现。
- 大于约 30 行的真实载荷放 `support/` 或独立 fixture 文件，测试用小输入时内联。
- 回归是契约：失败时先假定代码坏了；只有在原行为确实错时才替换它，并保留 provenance 线索。

## 守卫清单

| 守卫 | 位置 | 抓到什么 |
|---|---|---|
| 人群形状 | `tools/tests/observed/test_population.py` | 命名、注释事实、越界回归、模块自述；含模块级函数（不只看类内方法） |
| 目录与依赖 | `tools/tests/react_native/test_layout.py` | live 不导入 synthetic、support 不依赖其他测试、observed 同规 |
| 可达性与切片 | `tools/tests/react_native/test_recipe_wiring.py` | 未可达的测试文件、切片命令行漂移 |
| 切片配方 | `just/react-native/mod.just` | `tools-test`、`tools-test-android-safety`、`tools-test-lifecycle`、`tools-test-observed` 都是 `-k` |

## 刻意未做（staged）

- **`scene_native` 的 executor-free 套件（已迁完）**：`test_scene_semantics.py` 用两个只实现声明接缝的 test-only driver
  覆盖了全部场景语义：
  - `_InProcessDriver`：把整场 `scene` 当一次进程跑，证明阶段/宿主动作/切片/发布词汇；
  - `_ScriptedDriver`：逐阶段供给测试写的回执，证明 12 个派生。
  覆盖范围：构造期校验（无名 peer、缺 profile target、声明不驱动 target、payload 不可携、非法预算）、
  `scan` / `subscription` / `io` / `cancellation` / `write-reject` / `ccc-enable-reject` /
  `ccc-disable-reject` / `passive-disconnect` / `recovery` / `background` / `disconnect` /
  `connect-failure` 的派生，以及
  peer 一侧的完整命令序列（emit / arm / held / release / drop / resume，由 `_Peer` 记录）。
  派生类测试用 `_ScriptedDriver` **重写执行器接缝**（`_run_phase` / `_run_scene_plan`）来供给 App 的回答，
  不 patch 任何平台模块——每个测试仍然只替换一个东西（C1）。
  fake 的两条保真规则：一个阶段只发布**它的计划真正读到的**值；计划里每个时序/斜连声明（`for`、
  `grace`、`still`、`waitAny`）都必须在 `observed` 里有一个真实取值，不由 fake 凭空补上。
  iOS 模块现在只剩 8 条执行器事实（plan 命名、attach 模式、xcodebuild 退出码、附件导出、
  钉住的 xctestrun、非 iOS 拒绝），不再持有推导的第二份拷贝。

  术语：这套东西早期的名字是 “heart”；外部等价说法是 *platform-neutral domain core tested through
  an in-memory adapter*（六边形架构：`scene_native` 是 hexagon 内部，两个执行器是 driven adapter）。
  旧报告仍用旧名，不影响契约。
- **Popen 创建点统一（已完成）**：`host_command.stop()` 统一了 9 处 terminate→wait→kill；
  `ios_pod_refresh`、`run_quiet`、`ios_pod_refresh._retire` 与多链路的 `communicate()` 排空保留各自实现，
  因为需要拿回输出或自带平台知识。`bleng_ios_native_common`、`run_ios_rf_scan` 早已走 `stop()`，
  `bleng_ios_multi_link` 的 `idevicesyslog` 必须排空输出，不是遗漏。
- **iOS 失败时的截图**：managed scene 的 UI test 只附着文本 hierarchy 与 receipt，没有截图；
  若将来加入截图，`_export_attachments` 已经按清单发布，不需要新的通道。
- **真机复跑**：Android 已于 2026-11-24 重挣（11 个 managed 场景 + 4 条 native lane，见
  [`2026-11-24-android-lane-re-earned.md`](../../testing/reports/2026-11-24-android-lane-re-earned.md)）；
  该报告同时记录了 tap→ledger 竞态的根因、`_await_recorded` 修法与守卫。iOS 已于同日重挣 11 个场景，
  并完成两端的场景耗时收敛（`answerAlert` 的 `grace`、Android 的条件 `activate`），见
  [`2026-11-24-ios-lane-and-scene-speed.md`](../../testing/reports/2026-11-24-ios-lane-and-scene-speed.md)。
  同日晚些时候 `background` 的窗口被改为声明的 30 s 停留，两端 12 个场景在同一候选上重跑并通过，见
  [`2026-11-24-background-dwell-window.md`](../../testing/reports/2026-11-24-background-dwell-window.md)。
- **`setText` 再往下优化**：真机数据显示 2.5 s/次里 `type` 占 1.0–1.4 s、聚焦约 0.8 s。若要把「场景
  参数」提成 App 自己的机器面（plan 声明 + `read` 证明，而不是键盘输入），收益约 iOS 57 s / 763 s，
  但会改产品表面，属于独立决定。
