# Formal Android scan：具体 owner 组合与共用恢复边界

接续[HANS lifecycle 真机候选](2026-11-07-formal-android-hans-lifecycle.md)，按 [Plan §1.5](../../../.plan/PLAN.md) 和 [consumer 迁移职责](../../../tests/consumers/README.md) 推进正式 scan，而不是重新诊断同一冻结或搬入 private Probe API。

**本轮是软件组合、安全修正与职责审计：没有操作手机/DK、扫描、修改 BLE 权限/radio/固件，也没有新增可绕过准备条件的 scan CLI。上一轮真实 lifecycle pass 不继承为本次修改后的 scan/RF pass。**

## 1. 现有实现与真实缺口

已有代码不是空壳：

- 正式 [`BlengSessionOwner`](../../../integrations/react-native-bleng/specs/BlengSessionOwner.nitro.ts) 提供 start、beginScan、close；[`BlengScan`](../../../integrations/react-native-bleng/specs/BlengScan.nitro.ts) 提供原 start wait/cancel、单 pending pull 和独立 stop operation。opaque peripheral 目前没有 connect 方法。
- 共享 [`exercise_scan`](../../../tools/react_native/run_bleng_ui.py) 已实现 bounded scan/UI 场景；[`exercise_owned_scan`](../../../tools/react_native/bleng_scan_host.py) 核对实际 phone/fixture、冻结 request/receipt 与新鲜 App identity。
- [`run_prepared_scan`](../../../tools/react_native/bleng_scan_runner.py) 已接实际 candidate、managed automation、同进程 attested `DirectFixture`、被动 fixture close 与 phone finalization，阶段成功仍保持 `host_gate_status=pending`。
- Android helper、platform、managed APK/cold-launch candidate 和 Appium owner 都已有具体实现，不应以默认成功 callback 替代。

缺口分开处理：

| 类型 | 本轮核对结果 |
| --- | --- |
| 已实现但缺具体组合覆盖 | 原 candidate 层 scan 测试显式模拟 Android driver admission/platform callbacks，适合隔离下游逻辑，但不能证明实际 Android helper/platform/HANS 路径可组合；本轮新增具体组合测试补上这层软件覆盖。 |
| 实际恢复接线缺陷 | 正常 platform finalizer 已会恢复 HANS，但 authority 失效后该 finalizer 被跳过时，独立补偿只有 lifecycle 外层兜底。prepared scan 使用共用 automation owner，会漏掉这份补偿；本轮修正。 |
| 未完成的 live 外层接线 | managed scan 的物理选择、fresh fixture producer、安装前 live proof、准备失败与 handoff 拒绝时的 owned cleanup、外层 lease release 和完整 sealed scan bundle，尚无统一维护入口。 |
| 未取得的新平台证据 | 具体 scan 组合尚无手机/Hermes/Nitro/RF pass；本轮不借 lifecycle 或 synthetic UI 结果推断。 |
| 缺失的正式功能 | 当前正式 specs/exports 只有 lifecycle 和 scan，connect/GATT/I/O/notification 不能仅靠增加 Appium 测试就迁移完成。其正式 operation contract 仍须按批准边界收敛，不能复制 raw Probe。 |

本轮不把这些状态压成一个“测试架构已经全部 OK”，也不据此删除 internal。

## 2. Test-first 修正：HANS 补偿归共用 automation owner

正常 `AndroidAutomationOwner.finalize` 已在检查 session 清理前恢复 HANS。缺口是 [`ScanAutomationOwner.close`](../../../tools/react_native/bleng_automation.py) 原流程在 `_authority_failure` 已记录时拒绝 platform finalizer，正确地避免接管未知 session，却也跳过了这份补偿。此失权路径只有 `run_android_lifecycle` 后面的额外调用能兜住，`run_prepared_scan` 没有这一层。

新增七个 [`test_android_hans_policy`](../../../tools/tests/react_native/test_android_hans_policy.py) 回归，经实际 `AndroidAutomationOwner` 和 `ScanAutomationOwner`，只在 SDK/Appium 子进程/HTTP 边界模拟设备：

- 正常 finalizer 与共享 close 的恢复至多一次；重复 close 不重放。
- authority 失效仍补偿，但不 DELETE、不写成功 finalizer、不允许 `require_closed`。
- bind proxy 失败或 debug property 恢复失败后，独立恢复 membership/debug。
- unknown Remote 不认领 session，也没有虚构的 HANS mutation。
- boot/APK/shared UID 漂移、phone lease 丢失仍阻止越权命令。
- 恢复异常、中断和 close receipt publication 异常分别保留，失败 sticky。

Red：**23 tests，8 assertion failures / 1 error**；中心失败为恢复命令次数 `0 != 1`，中断用例因未执行恢复而未抛出预期中断。日志 `.work/hans-shared-close-red.log`。最小实现是在共用 `close` 中，platform finalization 之后、helper restoration 之前，独立调用具体 Android owner 的 `restore_server_hans_policy(deadline)`；不通过失效的 session guard 授权，而由原补偿 owner 核对实际 phone lease、boot、ROM、installed UID/APK。

Green：**23 tests / 26.805s**。未知 DELETE/清理失败仍是失败；恢复成功不释放未知 session 的 quarantine。`run_android_lifecycle` 的重复外层恢复调用已删除，避免不同 runner 各保留一套兜底。默认关闭 HANS 时不新增设备命令，不改变 iOS 路径或正常 UI API。

## 3. 新的具体 Android prepared-scan 组合测试

[`test_bleng_android_prepared_scan.py`](../../../tools/tests/react_native/test_bleng_android_prepared_scan.py) 使用：

- 实际 temporary phone lease、`AndroidHelperPreparation`、`AndroidAutomationOwner`；
- frozen candidate APK、实际 `AndroidCandidateOwner` install/launch/bind/finalize；
- 实际 `ScanAutomationOwner`，不绕过 `require_prepared_driver_contract`；
- 实际 `FixtureSession`、生产 receipt writer（输入为 synthetic attestation）、`DirectFixture` 和 `run_prepared_scan`；
- 复用 owner-local `LifecycleHarness`、`HansSample` 与 `prepared_fixture`，SDK/installer/Appium、串口/BTP 和 UI 响应全部隔离，未知命令拒绝，不会把占位身份送到真实设备。fixture 的 USB identity、lease/broker 和 BTP attestation 均为 synthetic；不宣称测试实际 broker acquisition、串口 I/O 或安装镜像证明。

四个组合边界：

1. initialize→scan→匹配同条目标广告→stop→close→Evidence；原有限 HTTP cap 保留；HANS/helper/automation 恢复后 phone 释放，成功 App 保留。
2. scan 点击后 driver identity 漂移，失权立即停止正常命令，不 DELETE；HANS 仍恢复，原平台/session 保持失败、phone quarantine。
3. UI 成功但 fixture passive STOP 失败：不能由 Android cleanup 成功洗白；仍保留成功 App、清理 wire resources、quarantine phone，不 fallback disconnect/reset。
4. 被替换的旧 fixture receipt 在 candidate bind、结果目录创建/UI 动作前拒绝；在此之前仍会执行 candidate/automation 的 handoff validation，但尚未转移资源所有权。所有权仍留给准备者，不接管未知 handoff。

初次组合检查在 45 秒预算内未完成，已检查日志和残留进程；单用例的 12 秒 stack 记录显示执行仍在受控 identity/file guards，随后 28.971 秒完成，并暴露测试误把 stock driver 配置后的 cap 当作 5 秒。修正的是测试按真实初始 finite cap 断言，**生产 HTTP budget 无缺陷、不改实现或增大场景 timeout**。另一初次用例误直接写只读 receipt，测试明确 chmod 后再注入攻击；没有放宽 receipt 保护。失败/超时日志保留 `.work/formal-scan-concrete-{first,single,failures}.log`。

## 4. live 外层下一步的约束，不新增通用框架

实际 fixture API 已核对：

- `prepare_session` 只冻结输入/USB identity，不打开串口、不 flash/reset。
- `DirectFixture.start` 在 live installed-image attestation、idle/no-peer 之后准备精确 GATT profile 并开始广告；它不是纯 read-only attach。正常 profile setup 与 firmware provisioning 必须分清，不声称 start 没有 fixture mutation。
- 当前 `run_prepared_scan` 要求同进程实际 `DirectFixture`，并以当前 PID/PGID 验证 receipt。`provider direct-start` 的子进程 receipt/日志 PID 不能当作这个 handoff；不新建 remote proxy 来掩盖不同所有权。
- `stop_advertising_and_close(deadline=...)` 已支持有界被动关闭；借用的物理 lease 仍须由外层 owner 释放，其结果不能由 phone release 代替。
- fixture startup、attestation、物理 broker acquisition 目前使用内部/fixed budgets，并非全都接受 host 绝对 deadline。后续 live producer 必须明确 setup budget 和 admission/failure cleanup，不能只给外层 UI timeout 就声称整个准备阶段有统一截止时间。
- 不调用 monolithic `run_android_lifecycle` 然后回收其已消费的 owners；也不为了 scan 再复制整段 lifecycle 和自己的恢复逻辑。先用具体 owner 组合和回归固定边界，再接 named producer；不把 Appium 换成另一套 UI 框架。

这些是已有实现的审计与下一段接线约束，不是新的 production API/ABI 决策。未选择默认 DK/port、未读取旧 receipt 来授权新 run、未隐式 grant 或 provision。

## 5. 验证

以下均实际执行，计数仅描述运行结果，不代表需求覆盖率或真机执行。

### 具体组合及恢复

```sh
pixi run --locked python -W error::ResourceWarning -m unittest -v \
  tools.tests.react_native.test_bleng_android_prepared_scan \
  tools.tests.react_native.test_android_hans_policy \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_server_hans_authorization_is_strict_before_phone_lease \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_server_hans_restores_after_delete_before_release \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_server_hans_unknown_submission_compensates_without_cleanup_adoption \
  tools.tests.react_native.test_bleng_android_lifecycle.AndroidLifecycleTests.test_server_hans_unknown_remote_never_applies
```

经现有 `run_quiet.py --no-tty --progress-seconds 15` 保存输出，外层 150 秒预算：**31 passed / 107.343s**；`.work/formal-scan-concrete-final.log`。

共用 owner 的互补回归：

```sh
pixi run --no-install python -m unittest \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_bleng_android_preparation -q
```

**150 passed / 148.188s**；`.work/hans-shared-close-regression.log`。不是 canonical 全量 discovery，也没有因单个用例或超时省略失败断言。

### 证据、UI 和 consumer topology

```sh
pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_hans_evidence \
  tools.tests.react_native.test_bleng_lifecycle_evidence \
  tools.tests.react_native.test_bleng_device_local_evidence \
  tools.tests.react_native.test_consumer_topology \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_scan_host -q
```

**158 passed / 15.248s**；`.work/formal-scan-boundaries-regression.log`。

- `pixi run --locked just react-native formal-host-test`：2 suites / **52 passed**。
- `pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck`、`pixi run --locked just react-native host-check`：通过。
- 交付检查使用 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check`，完整日志 `.work/formal-scan-handoff-{fmt,lint,docs,diff-check}.log`。

没有再次盲跑此前已多次超时的整个 tools-test；本轮只做上述具名/定向组合，**不声称 canonical 全量工具回归通过**。没有 native 或正式 JS API 改动，所以未重建 native artifacts，也没有把旧设备结果转移到新软件组合。没有提交或推送。

## 6. consumers 的收敛状态

已在 `tests/consumers/README.md` 的原迁移表区分目的地与当前缺口，而不是另建第二份架构计划。保留 native artifact、正式 native bridge、正式 JS/UI/runtime/RF 的互补边界；一个正式 App 不等于所有 tests 都要塞进 App。

本轮补齐的是 **具体 Android scan 软件组合覆盖和共用补偿边界**。后续仍须完成 live outer scan/bundle、正式 connect/GATT/I/O/notification、Hermes reload 及原独立 RF gate/ledger 迁移；未满足 internal 整体删除、功能收敛后集中 review、ABI freeze 或 Phase exit 条件。
