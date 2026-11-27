# `adapter-control` 场景：App 自己改 radio，两端都只拿到平台自己的回答

日期：2026-11-26
设备：OnePlus PHB110 `3ac5cc09`（rooted，Android 15 / API 36）；iPhone 14 Plus
`00008110-000C61160C39401E`（iOS 27.2.1）；DK-A `jlink:1050252028`（v2-a）
范围：第 20 个 managed 场景 `adapter-control`（`beginAdapterControl(enabled)`，Probe 页最后一条能力）。
状态：两端真机通过、`integrity=passed`；`host_gate` 仍为 `pending`。

## 先查平台事实，再决定场景该证明什么

仓库自带的官方 API 证据（`docs/platform-api-reference/android/BluetoothAdapter.java`）写得很死：

> Starting with **TIRAMISU**, applications are not allowed to enable/disable Bluetooth. For
> applications targeting TIRAMISU or above, this API will always fail and return `false`.
> Deprecation exemptions: Device Owner (DO), Profile Owner (PO) and system apps.

我们 targetSdk 36、不是 DO/PO/系统 App；CoreBluetooth 更是根本没有这个 API。所以这条场景**不能**
证明"radio 被关掉"，它能且只能证明：**App 把平台自己的回答如实报出来，并且不声称任何它没被告知的
状态变化**。这也顺带消掉了原先记在案上的破坏性风险（HANS OFF 期间 UiA2/ADB 卡顿）：平台保证
`disable()` 对我们这类 App 恒为 false，radio 不会被关。

## App 面：owner 级 radio 探针（R6 的第三个实例）

radio 是**适配器事实**，不是链路事实：没有扫描、没有连接时也必须可以问。所以它沿用
`devices-kinds` 立的规则——owner 级记录 + `centralRef.current` 身份栅栏，而不是借 `ScanRun`：

```ts
const radioRef = useRef<{ central: BlengCentral; observation: object; pending: boolean }>()
```

三条不变量与清单探针一致：一次一条（`pending` 覆盖整个 wait）、只由被问的 owner 发布、
`ownerClosing()` 即退休。新增投影键两个：`radio`（终态）和 `radioTarget`（请求的方向 `off|on`）——
后者是必需的，否则"上一次被拒的 off"会被下一次 on 的 `waitAny` 误当成答案。

页面归属不变：两个按钮在 `Probe` 页的 `probe-radio-strip`，plan 用 `workbench-probe` +
`workbench-page=Probe` 显式声明。

## 两个真缺陷（都是这一条逼出来的）

### 1. `failureIdentity` 把平台的"没有代码"当成了代码

Android 的 `notSupportedError()` 带的是**平台细节**（`platform=Android, code=null,
name=AndroidBleBackend`），渲染成 `[Android/not reported AndroidBleBackend]`。原来的读取正则抓
`[^\s:]+` 于是得到 `android/not`，投影里 `failure=android/not` —— 真正可断言的 `not_supported`
被丢掉了。修法两条一起：

- 渲染：平台没报代码时不再打印 `not reported` 占位，写成 `[Android AndroidBleBackend]`；只有真
  代码才写成 `[Android/0x85 BluetoothGatt]`。于是 `[platform/code]` 这个形状**只在有代码时**出现。
- 读取：`failureIdentity` 因此只把带斜杠的括号当作平台代码，否则回落到 `domain/code`。

这条不只是为了本场景：任何"平台报了组件但没报代码"的失败，之前都会得到一个无法断言的
`<platform>/not`。

### 2. `not_supported` 有两种拼法，App 只认一种

`crates/bleng-supervisor/src/ffi/failure.rs` 按边界拼 code：

| 边界 | code |
|---|---|
| 引擎在提交前拒绝 | `bleng.dispatch_rejected.not_supported` |
| 后端把拒绝作为**操作终态**报回（Android 的 adapter 电源请求就走这条） | `bleng.operation.not_supported` |

App 的 `NOT_SUPPORTED_CODE` 只等于前者，于是 Android 这条被报成 `failed`。同一个答案、两种形状，
所以判定改成看**代码的末段**：

```ts
const isNotSupported = (failure: BlengFailureInfo | undefined): boolean =>
  failure !== undefined
  && (failure.code === 'not_supported' || failure.code.endsWith('.not_supported'))
```

7 个 lane（`mtu`/`mtuRequest`/`priority`/`refresh`/`devices`/`auto`/`descriptorWrite`/`radio`）共用
这一个判定，不再各自比对字符串。派生侧同理：`scene_native.is_not_supported()` 取失败句 `":"` 之前
的 `domain/code` 再看末段，供所有"平台拒绝"的断言共用。

## plan 与账本

```text
初始 → start-session → tap workbench-probe → wait page=Probe
     → tap radio-off → waitAny(radio=done|unsupported, radioTarget=off) → read
     → tap radio-on  → waitAny(radio=done|unsupported, radioTarget=on)  → read
     → close-session
```

两个方向都必须问：一次被拒不能留下"App 以为 radio 关了"的印象，而"on"那一次正是真的关了 radio
的设备（DO/PO）恢复所需。账本形状由答案派生（与 `devices-kinds` 共用同一个
`_require_paired_submission_ledger`）：两次提交都在、"答完"才有 `-wait` 行、"被拒"必须点名
`not_supported`。

派生还钉住两件不能"顺手"发生的事：每次回答的 `radioTarget` 必须是问的方向，且 `scan`/`link`
全程不动（radio 请求不是扫描、不是链路）。

## 真机结果

| 平台 | `radio-off` | `radio-on` | 说明 |
|---|---|---|---|
| Android | `unsupported` + `operation/bleng.operation.not_supported` | **`done`**（`failure=none`） | `disable()` 按平台规则恒 false；`enable()` 在 radio 已开时被接受 —— 两个答案都是平台自己的，App 没改过 radio |
| iOS | `unsupported` + `operation/bleng.dispatch_rejected.not_supported` | 同左 | CoreBluetooth 只有 dispatch 级拒绝，两个方向都一样 |

- Android：`.work/android-native-adapter-control/2026-11-26-b`，`status=passed`，5/5 marker，场景片 11 s，
  APK `a1fcb8ef…`，`omitted=0`。
- iOS：`.work/ios-native-adapter-control/2026-11-26-a`，`status=passed`，5/5 marker，场景片 18 s，
  App 树 `7e56a50d…`。
- 两端离线复验均 `integrity=passed`、`phone/fixture=passed`、`cleanup_errors=[]`。

**`done` 不等于"radio 状态变了"**：它只表示平台接受了请求。真正的状态面是 adapter observation
lane（`beginAdapterObservation`），那一条由 L 档 `adapter-transition` 证明；本场景刻意不装作
receipt 就是新状态。

## 守卫（不需要设备）

- `test_scene_semantics.py`（+6）：两个方向都被拒可通过；真能改 radio（`done`）也可通过；答案必须
  自洽方向；`pending` 超时不能通过；非 `not_supported` 的拒绝不能通过；一次 radio 请求不得扰动
  `scan`/`link`；以及"拒绝落在操作终态（`bleng.operation.not_supported`）"这一形状。
- `AdapterControl.test.tsx`（+5）：拒绝报为平台答案且不动 `scan`/`link`；两个方向保持区分；真接受时
  报 `done` 并记账 `adapter-control(-wait)`；操作终态的 `bleng.operation.not_supported` 同样算
  `unsupported`；无 owner 时控件禁用。
- `test_scene_sequence.py`：顺序钉住 `devices-kinds < adapter-control < connect-failure`。
- `test_workbench_ids.py`：`probe-radio-strip` / `radio-off` / `radio-on` 进入词表。

## 尚未完成

- 序列现为 20 条，仍**不声称连续通过**（`background` 的老问题未定位，本轮没有重查）。
- `Probe` 页其余成员按此前的决定不做场景：`bond`（固件无 SMP）、`companion` 配对（需人工确认）、
  `adopt-peripheral`（需 Central 角色 peer）。见
  [`api-coverage-matrix.md`](../../research/react-native/api-coverage-matrix.md#明确不列成-s-级的三条及原因)。
- 若将来有 DO/PO 设备，这条场景不需要改：`done` 分支已经在那，只是那时 `radio-on` 才真的有意义。
