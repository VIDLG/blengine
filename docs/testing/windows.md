# Windows 测试执行指南

本文档是 [测试执行指南](README.md) 的 Windows 平台补充，整理 [`.plan/PLAN.md`](../../.plan/PLAN.md) 的 Windows-first 测试要求。它定义 fixture、native seam、覆盖矩阵、HIL 执行和报告方式，不是第二份架构 Plan，也不改变 Plan 的规范性 acceptance 或阶段退出条件。

Windows-first 的目标是尽早验证 BLE 事件和状态机，不是把公共 API 设计成 Windows API 的翻版。

## 适用阶段与 lane

| 阶段/条件 | 执行内容 | Lane |
|---|---|---|
| Phase 0-1 | 运行平台无关 contract/bridge、`FakeBackend` 和共享 conformance；Windows 不拥有这些语义的私有副本 | `core-primary`；可移植性由 `core-portability` 复核 |
| Phase 2，硬件到货前 | private Windows mapping fixtures、按需窄 native adapter、无硬件 backend 功能、scripted conformance、package | `windows` |
| Phase 2，USB adapter 与 managed peripheral 环境可用后 | RF smoke、资源清理和至少 100 次 scan/connect soak | `windows-hil` |
| Release candidate | 执行并保存 Windows HIL 报告 | `windows-hil`，独占 runner |

`windows-hil` 的计划环境是 Windows Central + QEMU Linux Peripheral VM + 2 BLE controllers，使用 BLESS managed peripheral。HIL 不阻塞普通 PR，也不承担全部状态组合；确定性组合应在较低测试层覆盖。

## Windows backend 测试边界

`WindowsBackend` 直接实现公开 `BleBackend`。不得为了测试再定义逐方法镜像的 `WindowsPlatform` trait、Windows command DTO 或 `WindowsDispatchError`。这种同构接口会复制 accepted/rejected、ID 和 error mapping 规则，并形成可能漂移的第二套 backend contract。

共享行为由 `FakeBackend` 和 backend conformance scenarios 固定。Windows 专属测试位于更低的 native mapping 边界：

```text
BleBackend command
-> WindowsBackend acceptance transaction
-> windows-rs call

WinRT result/callback
-> private plain Windows observation DTO
-> pure mapping function
-> unified BleEvent
-> push_backend_event(token, event)
```

### Acceptance transaction

- accepted 之前的 dispatcher 或调用失败，直接映射为公开 `CommandDispatchError`，不得 emit 该 command 的 event。
- accepted 之后的 HRESULT、WinRT status 或 callback failure通过`CommandCompleted`中的`BleOperationError { operation, failure }`表达，不得重新解释为trait method的同步`Err`；被动watcher stop或connection state failure只映射`BleFailure`，不伪造`CommandKind`。同一command-associated failure还先产生state event时，两处必须使用值相等的`BleFailure`。
- command method 返回前已经同步 emit event 时，返回值必须是 `Ok(())`。
- pending connect上的disconnect按[Plan 8.4](../../.plan/PLAN.md#84-建立连接)可在logical lifecycle与owned resources结束后成功；Windows没有证据证明该disconnect导致原connect被取消时，不得为原connect伪造`Cancelled`，只保留关联晚到结果所需的最小correlation。独立connect failure仍terminal `Failed`；原connect继续受原completion evidence与caller deadline约束，晚到callback保留原connect IDs且不得恢复为当前连接。
- Windows mapping 不引入第二套公共 event、error、ID 或 command lifecycle 形状。

## Private mapping fixtures

Mapping tests 直接构造 crate-private plain Windows observation DTO，而不是 mock 复杂 COM object。Fixture 应围绕原生事实和期望的统一输出组织；输出断言引用Plan的[扫描配置 6.6](../../.plan/PLAN.md#66-扫描配置)、[统一 event payload 6.15](../../.plan/PLAN.md#615-unified-event-payload)、[public event 不变量 6.16](../../.plan/PLAN.md#616-public-event)和[backend 可观察契约 7.1](../../.plan/PLAN.md#71-backend-可观察契约规范性)，不在本页建立第二套矩阵。

每个 fixture 至少记录适用字段：

| Fixture 输入 | 期望断言 |
|---|---|
| operation/callback kind 与 WinRT status | 统一 event variant、domain event 与 terminal attempt 的顺序 |
| HRESULT、status、错误消息 | 先映射`BleFailure { category, message, platform }`；passive state event直接携带它，accepted command terminal再包装为`BleOperationError { operation, failure }`，并按`error.failure.category`检查Plan 6.16不变量；同一command的failure state与terminal复用值相等的failure |
| `ScanOptions`、WinRT filter positive evidence 与 callback observation | `service_uuids`中的空字符串item和其他malformed UUID在accepted前映射`InvalidRequest`且零event，空`service_uuids` vector则单独验证为无service约束；service filtering没有support capability，WinRT native pushdown可选，pushdown evidence与callback补过滤必须共同满足Plan 6.6 predicate |
| caller-owned command/session/connection/GATT/subscription ID | event 原样回显对应 typed ID；晚到 callback 不改挂到当前 session |
| native object identity/key | 同一 native peripheral 复用 `PeripheralId`；不同对象即使 payload 相似也使用不同 ID |
| private native GATT handle/key 与枚举结果 | 映射为session-scoped opaque typed ID；`GattDiscovered.database`嵌套connection/GATT session ID和services，不在variant顶层重复，并保留duplicate UUID instance |
| callback source、到达顺序与重复cleanup request | 同一command的domain/terminal程序顺序和错序callback结果；重复stop与重复disconnect/disable分别遵循Plan 8.3、8.4、8.8的不同outcome，不重复native side effect或state event |
| payload、properties 与 notification mode | 统一 DTO 值、capability 和实际 mode 的准确映射 |

共享 `EventScenario` 不嵌入 HRESULT 或 WinRT status。Windows suite 在共享 scenario 上关联 mapping fixture 和平台能力断言，既复用 contract，又保持平台事实私有。

## 窄 native adapter seam

Production 路径由 `WindowsBackend` 直接调用 `windows-rs`。只有真实调用点无法被确定性驱动时，才增加最小 crate-private adapter 或函数注入。

窄 seam 必须满足：

- 只包住需要替换的 native acquisition、WinRT invocation 或 callback source。
- 形状从已经存在的真实调用点产生，不预先抽象完整平台 API。
- 不镜像整个 `BleBackend`，不定义第二套 command DTO/error，也不拥有独立 command lifecycle。
- 不改变 acceptance transaction、correlation ownership、opaque ID 或 event ingress 路径。
- Scripted Windows integration 驱动这个 seam 或 callback fixture，并最终经过统一 mapping 与 production `push_backend_event`；它不是另一个 fake backend。

如果实际调用点可以通过 plain observation fixture 和真实 API integration 充分测试，则不创建 adapter。

## Windows event matrix

下表是 Windows backend、mapping fixture、scripted integration 和 HIL 的联合覆盖清单。除明确需要原生无线环境的项目外，优先放入确定性 mapping/scripted tests。

| 领域 | 必须覆盖的场景 |
|---|---|
| Adapter | `Ready`、`PoweredOff`、`Unavailable`、`Unauthorized`、恢复、初始化失败 |
| Scan lifecycle | start成功/失败；watcher stopped/aborted；stop后晚到advertisement；错误scan ID；重复stop terminal success但不产生第二个`ScanStopped` |
| Advertisement | Plan 6.6 predicate的pushdown+callback补过滤；重复结果、数据更新；同一native device多次observation复用`PeripheralId`；不同device即使payload相似也使用不同ID |
| Connection | 成功、失败、主动/被动断开、重复callback；timeout后`Connected`；旧connection callback；已移除/unknown connection的重复disconnect同步`InvalidState`且零event；pending connect disconnect无因果证据时完成logical disconnect但不伪造原connect `Cancelled`，独立connect failure仍为`Failed`，晚到结果保留原IDs |
| GATT discovery | 成功、失败、空database、重复UUID、rediscover；`GattDiscovered.database`嵌套完整database且不重复顶层session IDs |
| Read | 成功、平台错误、timeout、错误 command ID |
| Write | with response、without response、平台错误、错序 callback |
| Notification | enable/disable、enabled前到达data、disabled后到达data、disconnect cleanup；已移除/unknown subscription的重复disable同步`InvalidState`且零event |
| Lifecycle/threading | backend 在 command 返回前同步 callback、跨线程 callback、downstream 非重入、重复 close、close 后 callback |
| Downstream dispatch | 慢 consumer、调用方 queue overflow、advertisement coalescing、notification burst policy；downstream sink 只做快速 dispatch，不让这些调用方策略阻塞 backend callback |

执行时应为每行标明覆盖层：

- `mapping`：plain observation DTO 到统一 event/error/ID。
- `scripted`：Windows backend acceptance、native call/callback 装配与 ingress 路径。
- `shared-conformance`：跨平台 accepted/rejected、ID、terminal、sink 与 close 语义。
- `HIL`：只有真实 WinRT adapter、RF、driver 或 peripheral 交互才能证明的行为。

同一场景可以跨层验证，但报告应区分每层证明了什么，避免用 HIL 成功代替 deterministic contract coverage。

## Windows HIL

### 必测能力

真实硬件至少验证：

- adapter 初始化与扫描。
- 真实连接和被动断连。
- GATT discover、read、write、notify。
- 重复 scan/connect 至少 100 次 soak。
- watcher、GATT object 和 event token 释放。

HIL 主要证明 WinRT、蓝牙芯片/驱动、RF 和真实外设组成的闭环。重复、乱序、stale/orphan 和错误组合仍应由 mapping、scripted 和共享 conformance 确定性覆盖。

### 执行与判定

- 普通 PR 不因 Windows HIL 未运行而阻塞；`core-primary` 和 `windows` lane 仍须承担各自的非硬件检查。
- Release candidate 必须运行 Windows HIL 并保存可追踪报告。
- `windows-hil` 使用独占 runner，避免 BLE controller、VM passthrough 或 RF 资源被并行 job 争用。
- Smoke 与 soak 分开记录；soak 报告必须给出计划迭代数、实际完成数、失败迭代和首次失败证据。
- 资源释放检查必须覆盖正常完成、失败、主动断开、被动断开和 close 后晚到 callback 中环境支持的路径。
- 未执行、跳过或环境故障不得记为通过；报告应区分 product failure、test fixture failure 和 infrastructure failure。

## 报告要求

每次 release-candidate HIL 至少记录 Plan 要求的环境身份：

- Windows 版本。
- 蓝牙芯片与驱动版本。
- 外设实现与版本。

为使结果可追踪，执行报告还应包含：

| 字段 | 内容 |
|---|---|
| Build identity | commit/revision、构建配置、package/artifact 标识 |
| Runner identity | runner 名称或资产标识；是否独占 |
| Central environment | Windows 版本、蓝牙芯片、驱动版本 |
| Peripheral environment | BLESS managed peripheral 或参考外设、实现版本、固件/fixture revision |
| Topology | controller 分配、QEMU Linux Peripheral VM/USB passthrough 状态；如使用不同 host，明确记录 |
| Test selection | smoke case、soak 场景、计划迭代数、超时与 fixture 参数 |
| Result | pass/fail/skip/infra-error 分项，不只给总状态 |
| Failures | 首次失败迭代、相关 command/session IDs、WinRT status/HRESULT、关键事件顺序 |
| Cleanup | watcher、GATT object、event token 与 runner/VM 恢复结果 |
| Artifacts | 结构化测试结果、日志、telemetry 和必要的诊断附件路径 |

报告字段是执行细节，可以随 runner 和 artifact 系统演进；Windows 版本、蓝牙芯片/驱动、外设实现/版本以及 release-candidate 必须执行并记录的要求来自 Plan，不得在此弱化。
