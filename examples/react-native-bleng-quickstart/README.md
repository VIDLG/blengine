# BLeng quickstart（React Native）

`@blengine/react-native-bleng` 的最小可用示例：扫描 → 连接 → 发现 GATT → 读取 → 订阅 → 断开 → 关闭，
每一步都是 API 文档里的形状，不含重试/缓存/自动重连之类的策略。

- API 参考：[`../../integrations/react-native-bleng/API.zh.md`](../../integrations/react-native-bleng/API.zh.md)
- 设计边界（哪些事这层不做）：[`../../docs/research/react-native/pending-operation-layer.md`](../../docs/research/react-native/pending-operation-layer.md)

## 这个示例刻意展示的五件事

1. **能力不是状态**：`central` / scan lane / `connection` / subscription 都活在 `screen.ts` 的
   闭包里，渲染状态只放"引擎报了什么"；组件重渲染不会丢掉或复制任何一个 handle。
2. **两种形状**：lane 用 `nextEvent(ms)`/`awaitEnd(ms)`（同时只有一个挂起 pull），
   一次性操作走 `run(...)`（就是 `pendingOperation(sub, ms).result`）；需要 cancel handle 或
   `AbortSignal` 时才改用 `pendingOperation(...)`。
3. **失败按 `code` 决策**：`bleng.timeout` 表示"这段时间没有事件"（安静不是故障），
   其它 code 才是真失败；`message` 只用于显示。
4. **取消/关闭是显式的**：这个屏上的操作都用 `run(...)`，没有一个需要中途 cancel；需要取消时换成
   `pendingOperation`（或传入 `AbortSignal`），它的 receipt 是请求而不是回滚。`central.close()`
   结束观察，不等于资源归零，也不能代替 `disconnect`。
5. **组件保持 React Compiler 干净**：命令逻辑是普通函数，`App` 只渲染与接线，所以编译器
   真的会优化它（见下面的 React Compiler 一节）。

## 这是一个"参考屏"，不是一个完整 App

这里**只有 JavaScript/TypeScript**，没有 `ios/` 和 `android/` 原生工程。刻意如此：
原生模板会随 RN 版本腐烂，把它复制进来只会让示例和真实工程分叉。

用它的方式是把它放进你自己的 App（`npx @react-native-community/cli init` 生成的那个）：

| 这个目录 | 放到你的 App 里 |
|---|---|
| `src/App.tsx` | 你的入口组件（或整屏嵌进导航） |
| `src/screen.ts` | 屏幕的命令层：可以和 `App.tsx` 合并，但见下面"组件要编译器干净" |
| `metro.config.js` | 替换默认 Metro 配置（见下面"为什么 Metro 也要改"） |
| `babel.config.js` | 同上；这里已经开了 React Compiler，见下一节 |
| `index.js` | 只在你的入口长得不一样时才需要改 |

`app.json` 的 `name` 用你自己 App 的注册名。

## 先决条件

- **原生产物**：这个包是"源码 + 预编译原生镜像"。Android 需要已发布的
  `dev.blehub:bleng`（内含 `blehub + bleng + bleng_supervisor` 单镜像）AAR；Apple 需要已 staged
  的 `apple-bleng` UniFFI 包。仓库内由
  [`tools/react_native/stage_bleng_apple.py`](../../tools/react_native/stage_bleng_apple.py) 与
  `pixi run just ffi android-bleng-build` 产出。
- **平台声明**：
  - iOS：`Info.plist` 需要 `NSBluetoothAlwaysUsageDescription`；后台订阅需要
    `UIBackgroundModes: bluetooth-central`。
  - Android：API 31+ 需要 `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT`；后台订阅需要
    `connectedDevice` 前台服务。
- **Metro**：包是源码交付，它的裸 import 由宿主解析（包自己的 `node_modules` 被 blocklist），
  所以 `extraNodeModules` 里必须包含 `@babel/runtime`（包内的 Error 子类要用 babel helper）。
  这就是 `metro.config.js` 不能省的原因。照抄即可，路径指向你自己 App 的 `node_modules`。

## 验证这个示例本身

```sh
cd examples/react-native-bleng-quickstart
npm install
npm run typecheck    # 类型层就是契约：能力、等待、失败的形状都在类型里
npm run compile      # Babel + React Compiler：配置真的跑一遍，组件必须被编译
```

在仓库里不需要安装第二份依赖，同样两条检查复用正式 host 已装好的 toolchain：

```sh
pixi run just react-native quickstart-typecheck   # 类型层
pixi run just react-native quickstart-compile     # Babel + React Compiler
```

这两条就是示例的自动化门槛：示例一旦用错 API（比如在没有 narrowing 的情况下读 `.failure`），
类型检查会红；Babel 配置失效或组件被编译器跳过，编译门禁会红。真机行为本身由
[`tests/consumers/react-native-bleng`](../../tests/consumers/react-native-bleng/README.md) 的
jest 套件与真机场景覆盖，示例与那些断言使用同一套 API 形状。

## React Compiler

`babel.config.js` 已经启用 React Compiler（`babel-plugin-react-compiler`；你自己的 App 里用
`npm install -D babel-plugin-react-compiler` 装上它即可）。三条实践约束：

1. **必须排在最前面**：编译器分析的是原始源码，而 Babel 先跑 `plugins`、后跑 `presets`，
   所以写进 `plugins` 就满足官方要求的"compiler 必须第一个跑"。
2. **React 19 不需要任何选项**：`target` 只在运行时是 React 17/18 时才需要。
3. **组件要"编译器干净"**：编译器只改写组件与 hook。它无法降低 `throw`、`try/finally`，以及
   `try/catch` 内部的条件表达式/可选链，遇到这些会**静默跳过整个组件**——配置看起来生效了，
   实际没有任何 memoization。所以这个示例把命令逻辑放在 `src/screen.ts` 的普通函数里，
   `App` 只负责渲染与接线。

验证方式不是"看配置有没有写"，而是让配置真的跑一遍：

```sh
pixi run just react-native quickstart-compile
```

它拿示例自己的 `babel.config.js` 编译 `index.js` 与 `src/**`，并断言每个 `.tsx` 都产出了
`react/compiler-runtime`（编译器自己的标记）。组件被跳过时门禁直接红，并打印 bail-out 原因。

确实无法编译的个别组件，用 `"use no memo"` 指令显式退出，而不是让编译器默默跳过：

```tsx
function Problematic() {
  'use no memo'
  // ...
}
```

## 用一个真实 Peripheral 试

仓库用的测试外设是 nRF52840 Multifunction Fault v2（`BleHubMultiRfV2DKA/B`，service
`fd600000-…`），它提供一个可读可写特征与一条通知流，正好覆盖上面所有步骤。任意 BLE 外设也可以：
示例只按 `properties()` 选择"第一个可读/可通知的特征"。
