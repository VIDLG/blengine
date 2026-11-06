# 平台 loss / same-Hub / iOS continuity：关闭条件与执行准备度

日期：2026-10-26

## 结论与本轮进展

本报告落实 [Plan F15 / §16.1.9](../../../.plan/PLAN.md) 和 [平台 loss 审计](2026-10-26-platform-loss-functional-convergence.md) 的未关闭项，不另立架构计划，不将方案或工具存在视为通过。

**后续软件进展：**已新增两个 write/CCC 系统-loss 用例的 provider-only 协议，并补 Apple backend 只读 native FIFO 观察端口；动态 command/subscription ID 在 ARM 后从实际接受记录一次绑定，不预测 ID。相关测试、构建、组合套件超时及尚未接线边界见 [provider 与 native 观察前置项](2026-10-26-system-loss-provider-native-observation.md)。BLeng iOS HIL witness emitter 现已接线，generic device/Simulator unsigned build、37 项确定性测试和两种 case 的 Swift → Python provider 软件协议回放通过；HIL 另已加入显式 `--rf-system-loss-active` phase：host relay authorization 后才接受旧 operation terminals，并支持导出 terminals、校验一次 restore-on relay、在同一 owner 上执行新 discovery/subscription/read/EF value recovery。另新增具名 `tools/apple/system_loss_host.py` host 状态机，并接入现有 `IosRadioLease` 的同锁 native scope；10 项 host contract + 12 项既有 lease 测试通过，固定一次 OFF、一次 ON 恢复、primary/cleanup 分离、旧操作逐一 terminal 和 same-owner 新 ID 校验。native active-loss 的真实 XCUITest OFF/ON adapter、当前 App/host relay 的真机执行与最终 RF 证据尚未接通，仍不能作为就绪 RF 命令或关闭下列真实平台项。

本轮完成一项可离线关闭的软件缺项：Apple capacity action → watchdog schedule/cancel → expiry retirement 的生产接线测试。未操作手机、radio/权限、Peripheral、固件、签名或 RF，未运行长稳、commit/push/branch。

其余项应分别处理：

| 项目 | 不能误认成什么 | 下一步 / 关闭条件 |
|---|---|---|
| write/CCC 真实系统 loss | 正常 I/O、ATT REJECT、held-read loss 或软件模型 | 在实际 accepted/pending 边界实施受控 loss，记录精确结算、native retirement、旧代隔离；无可靠 pending 观察时不得宣称跑到目标边界 |
| 同 Hub/BLeng 恢复 | 同 App/PID、fresh Probe、fresh process | 同一存活 owner、一次初始化、不 dispose/reassemble，loss 后显式新 connection/GATT/subscription 完成真实 I/O；全程证明未重建 owner |
| Android 撤权杀 UID | 同 Hub cleanup 或同 Hub 恢复失败 | 保留 OS cause-of-death 与 old PID/boot，旧 owner cleanup 不可验证；恢复 grants 后用新进程验证恢复，不能伪装旧 owner 存活 |
| iOS CCC disable REJECT continuity | RN bug、未发送、已通过，或缺一次重跑 | 用户于 2026-10-26 明确接受已记录候选的平台限制；该项不再单独阻塞集中核心 review，原 gate 仍 failed/pending，不能改成 passed；不关闭其他 loss/recovery 缺口 |
| Apple 永久 capacity stall | 正常流控恢复、人工 expiry 或 fake readiness | 有真实、可控的 native stall 才能取得该项 RF 证据；当前仍未验证，不引入固件/系统黑魔法制造通过 |

## 1. 本轮关闭的软件接线缺项

新增内部 [AppleGattWatchdogOrchestration](../../../platforms/apple/Sources/BleHubApple/GattWatchdogOrchestration.swift)，由真实 [AppleBleBackend](../../../platforms/apple/Sources/BleHubApple/AppleBleBackend.swift) 和测试共用：

- `nextAction` 接收既有 controller 的动作，在 capacity wait 时进入原 arm 路径。
- `arm` 保留 registry/token、replaced item cancellation 和原 delay；production schedule port 仍是原 serial queue 的 `asyncAfter`。
- 无响应写在 native characteristic 校验后，经同一 seam 取消匹配 capacity timer，再 native submit/记录本地完成。
- `expire` 使用同一 current-token、cancelled、capacity target、connection/native gate；保持退役 → native cleanup 请求 → 事件发布顺序。
- 不新增 timer owner、registry、driver 或公开 API，不改 timeout、generation、barrier 或 watchdog 分类。

[GattWatchdogOrchestrationTests](../../../platforms/apple/Tests/BleHubAppleTests/GattWatchdogOrchestrationTests.swift) 4 项测试验证：首次 schedule、重复 drive 不续期；匹配 timer 在 native submit port 前取消；当前 scheduled item 的 head Timeout / queued sibling OperationAborted 一次结算；其他连接隔离；旧 token 显式进入生产 expiry gate 时不能领取后继工作；native guard、IO/discovery 分类及 cleanup 请求顺序。

提取前相关 baseline 52 passed；新测试在 seam 尚不存在时为结构编译红，不称为已发现行为 bug。提取后新测试 4 passed、相关组合 56 passed。实际维护命令：

```sh
pixi run just apple test
pixi run just apple build-ios
```

完整 Apple backend **95 passed**，附带 **17 项 RF scenario host tests passed**，generic iOS Simulator/device unsigned builds passed。不是设备或 RF 执行。

日志：`.work/validation/2026-10-26-apple-watchdog-orchestration-{targeted,regression,build}.log`。

**仍未证明：**GCD 在真实负载下的投递时延、CoreBluetooth 实际 unsubscribe/detach/disconnect 效果、真实永久 capacity stall、radio/permission loss 和 same-Hub RF。expiry 只使旧连接不可用并退役 IO/GATT；connection route 最终移除仍等既有 disconnect callback，不能把 cleanup 请求算成该 callback 已到达。

## 2. 哪些现有入口可复用，哪些尚不能直接跑

| 现有入口 / 证据 | 已有内容 | 对本次缺口的限制 |
|---|---|---|
| [Android adapter runner](../../../tools/react_native/run_android_adapter_transition.py) 的 active-loss | attested held/queued READ → radio OFF → retirement → ON；[既有 RF 报告](2026-10-16-rn-android-active-read-radio-loss.md) | 当前 [App 场景](../../../tests/consumers/react-native-internal/src/rf_adapter_transition.tsx) 与 [ledger](../../../tools/react_native/adapter_transition_ledger.py) 之后 close 并执行 fresh-Probe I/O。不是 write/CCC 或 same-Hub；不能直接换 mode 名字算补齐 |
| [Android permission runner](../../../tools/react_native/run_android_permission_transition.py) / [v2-B 结果](2026-10-23-rn-android-permission-transition-v2-b.md) | denied / active held-read / restored fresh process、OS/process 证据和恢复 grants | 不是存活 owner 的 write/CCC 恢复；不得以 PID 消失单独判原因，也不得伪造 dead owner close/resource-zero |
| [iOS startup runner](../../../tools/react_native/run_ios_adapter_startup.py) / [fixed3 结果](2026-10-22-rn-ios-radio-gate-fixed3.md) | 显式授权的 Settings startup-off → ON、fresh-Probe RF | 当前 App 对 iOS active-loss 明确拒绝，不能把已通过的 startup gate 扩大解释 |
| [既有 BLeng iOS RecoveryScenario](../../../tests/hil/apple/bleng-ios/Sources/RecoveryScenario.swift) | 一个 owner、一次初始化、Peripheral 被动断连后显式重连、新 IDs、订阅和 stale-handle 检查 | 可复用现有 App、owner/证据结构，不必新建 App；现有逻辑不是系统 radio/permission loss，也没有活动 write/CCC loss |
| [历史 BLeng iOS RF](2026-09-30-bleng-ios-single-dk-rf.md) | 当时 production Apple backend 与同一 BLeng owner 的三轮 Peripheral passive-loss/reconnect | 不是“same-Hub 从没测试过”；但旧固件/包/会话不证明当前 v2 candidate 的 radio/permission/write/CCC 场景 |

因此，目前没有一个已经就绪的命令可以一次关闭所有这些缺项。应在既有 workbench/native HIL、provider control、物理租约、ledger 和 evidence bundle 上补专项，不增加独立 App 或通用 runner 框架。

## 3. 下一轮应接入的最小专项

先实现并通过无设备回归，再申请具体设备动作授权；本节不是 RF 运行记录或架构范围变更。当前 provider 协议、Apple pending/queued 观察端口、既有 BLeng HIL 的累计 witness emitter、active phase relay、phone lease、窄 XCUITest Settings target 与 host/UI ledger adapter 已有实现、构建和软件回归，下一段接线重点是在真实 session 启动该 xctestrun，取得 provider disconnect receipt 与 same-owner recovery ledger；不是继续把这些前置机制记为“未实现”，也不是把 deterministic active model 当成已具备 RF 证据。

### 3.1 同一存活 owner 的 radio-loss / 显式恢复

选择能区分错误实现的代表性组合，不展开所有操作 × 系统 × 参数的笛卡尔积：

- 活动 response write + 已接受的 queued CCC enable。
- 已有 subscription 上的活动 CCC disable + queued response write。

复用 v2 fault engine 的既有 hold/receipt 机制，在 host 实际确认该 head 进入目标 pending 边界后才允许 radio 动作。不新刷固件，不硬编码设备地址或 GATT handle；本场使用的 contract/profile、installed-image receipt 和发现的 IDs 逐项绑定。

无响应写的 local-completion/native-slot 分离继续保留确定性回归。若实际平台上没有可控且可观测的对应 pending 窗口，明确保留该 RF gap，不把已经 drain 的操作或正常写入充当故障覆盖。

通过条件：

1. 同一 App/PID/boot、backend 和 Bleng/Hub owner；initialize 一次，期间不 close/reassemble，不只是声明 owner_count=1。
2. 目标和 sibling 按真实证据各结算一次；没有伪造写入交付、CCC 成功或远端回滚。
3. 旧 connection/GATT/subscription 退役，所属 pending/watchdog/routes 安全释放；无关 owner 不受影响。
4. host 确认 radio 恢复，应用**显式**重新 scan/connect/discover/subscribe，使用新 IDs，在同 owner 上完成 read/write/实际通知。
5. 旧 handle 被拒，迟到结果不污染新代；最终才 shutdown/cleanup，primary failure 与 restore/cleanup failure 分开保存。

low-level RN 证明 backend/RN 边界；若要关闭 BLeng 的恢复承诺，必须在现有 native BLeng HIL 上取得该层证据，不能用低层 Probe 代替。

### 3.2 权限 loss：先分清进程是否还活着

Android 与 iOS 各按实际系统观察分类，不要求第二台 Android 或多 API tier。

- 进程与 owner 存活：可以验证 live authorization loss 的结算/退役与同 owner 显式恢复。
- 系统终止进程：该场只能验证准确 OS 原因、物理断链、旧进程身份及 fresh-process 恢复；不能要求旧 Hub 跨进程存活，更不能把这个条件下“不适用”计成 same-Hub pass。
- `SecurityException` 的单次 submission 拒绝不直接等于完整全局撤权；ATT authentication/authorization status 也不等于 OS app permission。

当前 read-based fresh-process gate 不冒充 write/CCC loss。任何 live revoke 专项都要先确认受控动作、观察边界和恢复条件；缺乏可重复入口时，保留未验证或提交明确限制裁决，不制造伪场景。

## 4. iOS continuity 的裁决：不重跑已知失败

[Native HIL](2026-10-24-ios-native-hil-ccc-disable-reject.md) 已排除 RN/Nitro；[先前 RN sniffer](2026-10-24-rn-ios-ccc-disable-v2-a-sniffer.md) 与 Native 是独立 session，不拼成同-run PCAP。现有事实是 rejected disable 后 continued value 没有进入 production native callback。

用户于 2026-10-26 明确接受以下范围受限的平台限制，现已回写 [Plan §1.4](../../../.plan/PLAN.md)；这是一项明确裁决，不是新增 RF 结果：

> 在已记录的 iOS/CoreBluetooth 版本与候选上，CCC disable 失败后原 subscription 的 continued-value delivery 不作保证。保留原失败和 typed error，不合成 value、不自动 re-enable/reconnect、不宣称远端已关闭。后续显式断连/重连/重订阅是待专项验证的恢复路径，不能仅凭建议标成已验证 workaround。

**该限制已被用户接受，但原 continuity gate 仍保持 failed/pending，不改成 passed。** 该项不再单独阻塞集中核心 review；发布说明与平台验收仍须披露它，不能扩大到未测 iOS/macOS 版本。write/CCC 系统 loss、same-Hub 恢复与 capacity stall 等未验证边界没有被一并豁免。更换 Rust actor/测试框架不能补出 CoreBluetooth 没交付的 callback。

不为了 reconfirm 已知缺口再跑一次 RF；Apple feedback 材料可以整理，但本轮未向外部服务上传日志或设备信息。

## 5. 执行授权与当前状态

- 普通构建、定向软件回归、unsigned build 可继续；本轮实际做了 §1。
- 后续 radio/revoke 专项须给出具体手机、冻结 App、DK/live receipt、pending 操作、动作次数、deadline 和恢复/失败处置，再取得 fresh 授权。未知最终状态不释放安全责任；不并跑其他 radio/firmware/CCC RF gate。
- 不自动 unlock Keychain、收密码或绕过系统认证；iOS UI runner 初始化失败单独归为环境阻断，不记作 BLE 用例失败。
- 不新增长稳、多 Android 版本、额外 App 或 blanket retry 门槛。

本轮整合检查：`pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git --no-pager diff --check` 通过，日志 `.work/validation/2026-10-26-platform-loss-readiness-{fmt,lint,mermaid,diff}.log`。未重复无关 RN/Jest/Rust 全矩阵，也没有用本轮软件检查关闭真实平台项。
