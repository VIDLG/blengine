# iOS 权限相关 tooling：分场景预检、失败归类与授权退出

日期：2026-10-25

## 范围与结论

按用户授权优化现有 Apple/RN host tooling，不改变 production 权限契约、不引入 Appium/WDA 或通用权限 driver。Hash 始终按实际 App 内容计算，不硬编码某次 candidate。外围 review 前置的既有 [native RF 证据](2026-10-25-native-matrix-validation.md) 保持原 candidate/session 范围；本报告只记录此次软件实现与无设备验证。

## 实现

### 1. 预检不再偷偷构建

- `ios-device-doctor` 移除 `prepare-ios-device` 依赖，避免其间接 production package 准备；旧 Python doctor 同样不再调用 `_environment` 或生成工程，而是委托唯一的 `tools/apple/ios_preflight.py`。
- `build-signing` 观察当前 Xcode/SDK/Swift、XcodeGen 版本、配置 Team 及可见匹配的 Apple Development identity。证书可见不证明当前进程能使用私钥，未试签名或解锁。
- `prebuilt` 只验证已有物理 iPhoneOS App 的实际 hash、签名/Info、development profile 的有效期与 bundle/team，并从 actual signature 读取 `application-identifier`、Team 与 `get-task-allow` 对照 profile，复验前后 hash；不要求 XcodeGen、配置 Team 或签名 Keychain 解锁。
- 按 [Apple TN2415](https://developer.apple.com/library/archive/technotes/tn2415/_index.html)，profile 可以使用合法 wildcard，但 signed App ID 必须完整精确匹配实际 profile prefix + Info bundle ID；prefix 不假定等于 Team。越界或非法 wildcard、signed wildcard、非 development entitlement 均拒绝。这里只验证上述三项 entitlement，不冒充 OS 完整安装策略或 TCC 检查。
- 只有显式 device 才检查 profile membership 和 available/unlocked；无 device 不自动选手机、不调用设备 SDK。查询通过原 owner，关闭 stdin，并有超时；XcodeGen verify 仍不安装工具。
- JSON 包含本次起止时间与 `verified / blocked / not-checked` observations。整体 verified 只覆盖该 mode 的 required 项；private-key access、TCC、UI automation、RF 明确不证明，不成为缓存授权。

### 2. 失败阶段不等于根因

- Native 已准入 attempt 增加 `failure.stage`，区分 artifact、phone ownership、device preflight、fixture admission、install/launch/runtime/result/retirement；保留原 `primary_error`，独立记录 `cleanup_failures`。未知根因不推断成 BLE 或 TCC 故障。
- 现有 `ios_adapter_handshake` 失败后可从同一 xcresult 有界导出 summary。只有同设备、唯一对应 runner 的明确 initialization failure 才标记 `ui-runner-initialization / test_body_started=false`。合法 live marker 为 true；缺 marker 且缺确定证据为 null，不仅凭 timeout 判定测试体未开始。
- handshake staging 检查 `EnvironmentVariables`、`TestingEnvironmentVariables` 和 `UITargetAppEnvironmentVariables` 三个入口，拒绝遗留 `BLEHUB` opt-in 与非字符串环境项，防止 non-RF 诊断夹带专项 gate。
- summary 导出、cleanup 和 evidence 写盘错误不掩盖原始 primary。没有 primary 时，结果发布失败仍正常导致失败，不能无有效结果文件却返回成功。

### 3. 显式允许场景间退出，不接管旧 App

`rf-native-smoke` 增加可选 `success_app=keep|close-owned`，CLI 对应 `--success-app`。默认 keep 保留成功页；iOS 显式 close-owned 只在本次 run 的 PID、完整 executable、fresh boot challenge 均匹配后终止并确认退出。失败退出不能升级为 host Passed，未知旧 App 仍在准入时拒绝。

操作者可一次授权指定手机/候选的 IO→notification 及中间 App 退出；每场仍保留独立 session，最后一场使用默认 keep。不顺带授权 TCC reset/revoke、radio OFF/ON、固件 provision、Keychain 操作或未知 owner 恢复。没有新建 universal batch runner。

持久职责与使用示例见 [Apple 指南](../apple.md#scoped-read-only-preflight) 和 [host ownership](../../research/react-native/react-native-host-tooling-architecture.md#预检系统授权与执行同意分离)。

## 实际验证与保留边界

| 实际命令/检查 | 结果 |
|---|---|
| `pixi run python -m unittest tools.tests.react_native.test_ios_preflight tools.tests.react_native.test_ios_adapter_handshake tools.tests.react_native.test_layout -q` | **17 tests / OK**；`.work/validation/2026-10-25-permission-refinement-targeted-verified.log` |
| `pixi run just react-native tools-fixture-test` | **1262 tests / OK，235.249 秒**；`.work/validation/2026-10-25-permission-refinement-tools-fixture-final.log` |
| `pixi run just apple test-rf-host` | **15 tests / OK**；含旧 doctor CLI 委托与 flags admission；`.work/validation/2026-10-25-permission-refinement-apple-host-final.log` |
| `pixi run just fmt` / `pixi run just lint` | 通过；`.work/validation/2026-10-25-permission-refinement-{fmt,lint}-final.log`；首次 lint 暴露的测试闭包绑定/UTC 构造问题已修正，失败日志保留 |
| Mermaid / Markdown / diff | `pixi run just docs-check-mermaid`：27 diagrams / 171 Markdown files；相关 authored Markdown 的 UTF-8、围栏、空白、冲突与本地链接、`git --no-pager diff --check` 通过；Plan 四处失效的迁移路径已修正，不改历史结论 |
| 核心未改 | `crates/blehub`、`crates/bleng` 共 67 文件与原基线逐文件 SHA-256 一致 |

测试 mock 实际 subprocess/SDK 边界：覆盖过期/不匹配 profile、合法与越界 wildcard、actual signed entitlement mismatch、三种 handshake 环境入口的遗留 opt-in/非字符串项、签名拒绝、artifact 漂移、wrong Team、required 未检查项、设备锁定、无隐式选择、doctor 的完整 Just dependency dry-run、close-owned 的身份拒绝、失败归类及 publication failure。未使用全局永成功 mock，也未将 synthetic device ID 送到真实命令。

本轮还对已有 signed notification App 执行无 device 的真实本机 `prebuilt` doctor：实际 signature/profile/三个 entitlement 与前后 hash 均 verified，`app_id_match=wildcard-profile-exact-signed-app`；未查询手机，private-key/TCC/UI/RF 仍为 not-checked。本机日志为 `.work/validation/2026-10-25-permission-refinement-prebuilt-local-verified.log`。首次检查误拒合法 wildcard profile 的失败日志保留，修复依据与回归同时补齐。

没有安装/启动 App、UI 自动化、RF 或 OS 权限/radio/Keychain mutation；新 close-owned 选项的此轮证明是隔离软件回归，不借用先前手工授权退出的 live 证据。未更换测试框架、重跑未变的 Jest/native package 矩阵、修改核心 crate、commit、push 或创建 branch。无需将此次 tooling 优化变成阻止核心人工 review 的新门槛。
