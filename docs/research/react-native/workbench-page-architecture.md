# Workbench 页面与组件架构（审查 + 目标结构）

日期：2026-11-26
范围：`tests/consumers/react-native-bleng/src/` 的页面规划与组件分工。
本文只写职责与规则；候选事实与真机结果仍在 `docs/testing/reports/`。

## 1. 现状（已核对源码）

- 页面：`Tests` / `Current run` / `Evidence`（`App.tsx` 的 `pages`），初值 **`Tests`**。
- 渲染分两层：
  - **pinned**（不在任何 `ScrollView` 内）：标题、导航、page/state、error、retirement warning、
    `run-status`、`scan-latest`、`notify-latest`、**仅 Current run** 的 Start/Close、snapshot 卡、
    `CommandConsole`、`action-ledger`。
  - **content**（`workbench-content` ScrollView）：叙述、拓扑、样本、历史、以及 `Tests` 页的 Start 按钮。
- `CommandConsole`（197 行）承载 5 个能力族：scan、connect/link、link-facts、gatt、notify+CCCD+devices。
  布局是载荷（`flexBasis: 72` 的 wrapping row + 固定高度预算）。

## 2. 审查结论

### F1（真 bug）每条场景的第一条命令点在一个滚动容器内的元素上

`_prefix()` 的第一条动作是 `tap start-session`（`scene_plans.py`），此时 App 停在初值页 `Tests`。
而 `start-session` 在 `Tests` 页只存在于 **content ScrollView 里**（`App.tsx:287`）；pinned 的 Start
只在 `Current run` 渲染（`App.tsx:244-253`），而切到 Current run 恰恰发生在 `start()` 内部
（`setPage('Current run')`，`App.tsx:130`）。

也就是说：**17 条场景全部依赖“Tests 页 content 的第一个节点刚好在视口内”** 才成立。Android 会
省略视口外的 `ScrollView` 子节点，所以只要有人在它上面加一行文字，所有场景一起坏，且症状是
`timed out before … enabled` 而不是“布局改错”。这与本项目“场景可点的东西必须在 pinned 区”的
既定原则直接冲突。

### F2 Start/Close 是页面条件渲染，而 plan 假设它们永远在

`start-session` / `close-session` 只在 `Current run` 出现。`_close()` 直接 `tap close-session`，
成立的前提是“场景全程没有导航离开 Current run”。这是隐式耦合：任何一次导航都会让 close 变成
不可见元素。

### F3 同一个 testID 在两个页面各有一份

`start-session` 在 `Tests`(content) 与 `Current run`(pinned) 各渲染一次。二者不会同时出现，
所以 `getByTestId` 恰好不冲突；但这是靠“页面互斥”维持的巧合，不是契约。

### F4 `CommandConsole` 已经到尺寸上限，且按“族”混装

已因加控件回归两次（`link-facts` 挤掉 `disable-notifications`；`descriptor-io` 挤掉自身控件）。
notify 行里同时住着 CCCD 两个控件（描述符族），是为了省空间，不是语义归属。
再补 `auto-connect` / `adapter-control` / `bond` / `companion` / `adopt` 必然把它推爆。

### F5 `action-ledger` 的 32 条窗口是**场景长度的硬上限**

pinned 最后是账本，`EVIDENCE_LIMIT = 32`，超出后丢弃最旧条目。`descriptor-io` 已用到 25 条；
本轮还因此把 `_require_ledger` 改成“被截断即失败”。也就是说：**加长场景 = 撞窗口**。
`recovery` 早就用“只看后缀 + 计数”绕开了这个限制，但其他派生仍在比对整段。

### F6 死代码

`SnapshotCard` 的非 compact 分支（`statusCard` + 逐行）没有任何调用点；两个调用都传 `compact`。

### F7 机器事实与人类文案同名邻居

content 里有 `scan-state` / `link-state` / `gatt-state` / `notify-state`，pinned 里有
`run-status` 的同名字段。派生只读 `run-status`，但命名上容易让人误以为二者可互换。

## 3. 目标结构

### 3.1 每页四层，顺序固定

| 层 | 内容 | 是否 pinned | 是否可被场景访问 |
|---|---|---|---|
| L1 会话框架 | 标题、导航、page/state、error、retirement warning | 是（全页一致） | 是 |
| L2 机器面 | `run-status`、最新观测、`action-ledger` | 是（全页一致） | 是 |
| L3 该页命令面 | 一个能力族的控件 + 输入 + 结果标量 | 是，且有高度预算 | 是 |
| L4 人类内容 | 叙述、拓扑、样本、历史 | 否（ScrollView） | **否，永不** |

**L1/L2 在所有页面完全相同** —— 场景观察的事实不随页面变化。

### 3.2 页面划分

| 页 | 职责 | 命令面 |
|---|---|---|
| `Tests` | 会话生命周期 | 只有 pinned 的 Start/Close |
| `Current run` | 观测主线（**保持现状，不动**） | scan / connect / link-facts / gatt / notify+CCCD / devices |
| `Probe`（新） | 平台条件能力，不进主线 | adapter control、auto-connect、bond、companion、adopt |
| `Evidence` | 人读历史 | 无命令，只 pinned 快照 |

`Probe` 之所以单独一页而不是塞进 console：它的每个能力都可能触发系统 UI（配对、radio、companion
选择器），和“观测主线”是不同风险等级的测试面。

### 3.3 硬规则（可被测试钉住）

- **R1** 任何 plan 会 `tap`/`read` 的 id 必须渲染在 `workbench-content` 之外（按 id 集合静态检查）。
- **R2** 每条场景必须声明它所在的页面；driver 在第一步之前断言 `workbench-page` 等于该声明。
  这样“控件换页”会立刻变成具名失败，而不是超时。
- **R3** 一个 testID 在任一时刻只有一个节点（同一 id 不得跨页重复）。
- **R4** L3 的高度预算按“一屏能放下 L1+L2+L3+至少一条输入”计算；超预算时新增页面，不加高。
- **R5** 派生不得假设整段账本（见 F5）：只断言“由某个起点开始的连续后缀 + 单调计数”，
  使场景长度不撞 32 条窗口。
- **R6** 探针的归属是 **owner**，不是 `ScanRun`。凡“不是某条链路的事实”（系统清单、radio、
  配对），App 侧都用 owner 级记录 + `centralRef.current` 身份栅栏；否则场景不扫描时控件 enabled
  而处理器直接返回（`devices-kinds` 首版真 bug）。
- **R7** 文本输入不得把键盘留在屏上。场景 `setText` 会聚焦输入框，软键盘占住屏幕下半（iPhone 14
  Plus 实测 y 578–926），而 L3 控件就在那里：`pull-notification` 在 y 622 时“present、enabled、
  不可点”（`disconnect` 首跑）。所以标题 `formal-host-title` 的 `onPress` 调 `Keyboard.dismiss()`，
  驱动在每个 `setText` 成功后点它一次（`test_workbench_ids.py` 两侧钉住）。拿不到收键盘面时记
  `keyboard=present, no dismiss surface`，不静默。

## 4. 迁移顺序（考虑爆炸半径）

17 条现有 plan 钉死在 `Current run` 的现有 id 与布局上。**不要为了“更整齐”重排它们** ——
那会让 17 条场景全部需要重新在真机上取证。

1. **F1/F3 修复（无 plan 变更）**：`start-session` 移进 L1/L2 的 pinned 区并全页渲染；
   删除 `Tests` content 里的重复节点。行为等价，只是从“视口内”变成“真的 pinned”。
2. **F5 修复（无 plan 变更）**：派生改为后缀 + 计数断言；`_require_ledger` 不再要求整段。
3. **新增 `Probe` 页 + R2 页面断言**：先只有页面骨架与 Start/Close，不动主线。
   **已落地（2026-11-26）**：`Probe` 页 + `probe-console` + 第一个能力 `auto-connect`
   （App 控件、`auto` 标量、投影键、jest `AutoConnect.test.tsx`、S 档场景与真机通过）；主线
   17 条 plan 未改。**R2 首个实例**：`auto-connect` 的 arm 相先 `tap workbench-probe` 再
   `wait workbench-page=Probe`，所以控件换页会具名失败而不是超时。
4. 按 `auto-connect` → `devices-query(bonded/companion)` → `adapter-control` 顺序把能力搬进 Probe。
   **已落地（2026-11-26）**：`devices-kinds`（Probe 页 `probe-inventory-strip` 两个按钮 +
   `devicesKind` 标量 + jest + S 档真机通过）与 `adapter-control`（`probe-radio-strip` +
   `radio`/`radioTarget` 标量 + jest + 两端真机通过）。`devices-kinds` 落地的一条新规则：
   **探针的归属是 owner，不是 `ScanRun`** —— 第一次实现借用 `runRef.current`，而没有扫描就没有 run，
   于是按钮 enabled、处理器直接返回。凡“不属于某条链路”的能力（清单、radio、配对）都用 owner 级记录 +
   `centralRef.current` 身份栅栏，绝不能隐含要求场景先扫描（见 R6）。
5. `bond` / `companion` / `adopt-peripheral` 在矩阵里显式记为“不进 S 档 + 原因”（固件 SMP /
   系统确认 / 需要 Central 角色 peer），不做假场景。见
   [`api-coverage-matrix.md`](api-coverage-matrix.md#明确不列成-s-级的三条及原因)。

## 5. 明确不做

- 不把 `Evidence` 做成可执行的测试面。
- 不在 L4（滚动内容）里放任何机器事实，即使“现在看起来在视口内”。
- 不为对齐编号而重排 `Current run` 的控件顺序。
