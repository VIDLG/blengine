# Formal RN prepared scan：执行、检查频率与跨资源收尾

日期：2026-11-04。真实 Mac 本地 Pixi/Just，未提交工作树。仅软件候选，不冻结 ABI，不代表移动 runtime/RF。

**结论：已准备资源之后的共同执行阶段已接通两端实际 candidate owner、共享 Appium UI 场景、
严格被动 fixture close 和 phone finalization。阶段可以通过，外层 host gate 仍 pending。**

架构依据：[唯一 Plan §1.5](../../../.plan/PLAN.md)、[host tooling 职责](../../research/react-native/react-native-host-tooling-architecture.md#正式-typed-scan准备交接与执行阶段)。
前段身份与安装来源、平台终止竞态及原失败见 [candidate identity 报告](2026-11-04-formal-host-app-identity.md)。

## 后续收紧

后续 [automation ownership 候选](2026-11-04-formal-automation-ownership.md) 已进一步要求 managed
Appium/session owner 及必填平台 finalizer；关闭裸 driver 不再是 phone release 的充分依据。
本报告的原 55/205 项软件结果保留为当时 handoff 接口的证据，不替代新接口的验证或真实平台 owner。

## 这次接通什么

[`run_prepared_scan`](../../../tools/react_native/bleng_scan_runner.py) 消费调用方准备好的同 run：

- 已安装/启动、尚未 bind 的实际 iOS/Android candidate owner；
- 明确 attach-only 且有有限 HTTP timeout 的 WebDriver session；
- active in-process `DirectFixture`、immutable request/contract/profile 与 live receipt；
- 实际 phone lease。

准入失败不接管、不退出未知 driver、不清理调用方资源。独占结果目录创建后，执行一次 bind 和
正式 scan 场景，再按以下顺序收尾：

```text
driver.quit
→ candidate.finalize（成功保留 App；失败按原 identity 检查收尾）
→ fixture.stop_advertising_and_close
→ prepared-result.json（仍为 pending）
→ phone.finish
→ result.json（绑定 phone journal hash）
```

独立清理失败不跳过后面的独立清理。primary、各资源 cleanup、phone journal 错误分开保留；成功
App 页面不因其他清理失败而被自动关闭。cleanup 中断保留对象，完成其他步骤和证据发布后传播。
候选 bind 的 deadline 只允许收窄到 runner deadline，不能继续使用较长的原预算；cleanup 独立总
预算不恢复场景权限。WebDriver 每个读写边界使用锁定 SDK `client_config.timeout` 设置剩余预算。

最终 JSON 区分 `scenario_status`、`session_status`、`host_gate_status`。即使本段全部通过，host gate
仍为 pending：设备选择/平台准备、外部 WDA/UiAutomator2/server、借用的外层 fixture lease 和完整
bundle 不由此函数证明。没有新增 scan CLI、成功 ready JSON 捷径或通用 runner/cleanup registry。

结果通过同目录临时文件写入并关闭后，以独占发布方式成为正式路径；不将半写 JSON 暴露为终态。
phone release 前的 prepared evidence 失败会 quarantine。最终提交若在 release 后失败，不能重新
认领已释放的 lease；保留 pending evidence，调用失败，不伪造完整 bundle。

## 接线中发现并修正的实际问题

### Android observation 互斥

旧 `exercise_owned_scan` 要求 `initial ON`，而新的 candidate 与 `finish_app_observation` 要求
未执行 radio action，二者无法组合。已统一正式 scan 为 App-only observation；不执行隐式 radio
检查/恢复，拒绝混入旧 initial/off/on controller。旧 permission/radio gates 不变。

### 完整 identity 不能嵌在每个 UI 字段 guard 里

首次真实 owner 组合在测试中超时。边界追踪显示：尚在 idle 页浏览时就已经执行超过 1,000 条
Android identity SDK 命令，并非需要更长扫描等待。曾核对测试 clock/sleep 并保存诊断日志；最终
修的是检查职责，而不是增加超时、缓存 native identity 或跳过安全断言：

- 每个远端读写前后仍检查 lease、fixture proof、candidate 本地 authority、driver 和 deadline。
- 场景入口/出口、每次点击前后、初始化/scan start/pull/stop/close 完成点才做完整 fresh native
  identity/installed-candidate 检查。失败立即中止；不重试不确定点击。
- 场景、准备与 SDK budget 仍有界，测试也限制完整成功场景的 SDK command 增长，防止逐字段膨胀。

没有保留本地 artifact digest 的试验性 metadata cache；完整 identity checkpoint 仍重读候选。
真实设备检查耗时、SDK 支持和端到端总预算仍待测，合成执行时间不能外推为真机性能。

### fixture cleanup 必须有可审查的回执

旧 `DirectFixture.stop()` 对部分 BTP 错误采用 best-effort，并可能 disconnect/reset，不能据此给
正式 scan cleanup 写 passed。新增通用 provider primitive
[`stop_advertising_and_close`](../../../tests/peripherals/nrf52840/direct_fixture.py)：

- 同一 live-attested lease/session/boot，前后 idle/no-peer，拒绝不明或排队 peer event。
- 单次 STOP_ADVERTISING；依据本地 Zephyr BTP GAP ADVERTISING bit 10 和 controller-info readback，
  要求该 setting 被支持且已清零。缺支持明确 `AdvertisingStateUnverified`，不算通过。
- 不 disconnect/reset/flash；共享 wire deadline 覆盖碎片读取和实际串口写入，超时仍独立关闭资源。
- 单个失败原样传播，多重 wire/release 失败分组保留；所有结果退休该 fixture，旧 stop 不再补发命令。
- 回执是 BTP inactive + 本次 scope closed，不是空中 RF 静默、firmware 六类 zero 或借用外层 lease
  已释放。broker 释放沿用其独立有界预算，逾期不返回成功回执，但不是硬 OS wall-clock 保证。

## 实际验证

两端 candidate/runner 组合：

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -v
```

**55 passed，最终重跑 87.252 秒**（与其他验证并行）；先前同范围为 68.368 秒。覆盖实际临时 phone
lock、candidate SDK subprocess mock、真实 scene/attestation/fixture finalizer 组合，包括成功保留、
UI 主失败、driver/fixture cleanup 失败、短 deadline、错误 driver 准入、publication 与中断。

互补回归：

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tests.peripherals.nrf52840.test_direct_fixture_passive_close \
  tests.peripherals.nrf52840.test_direct_fixture \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_bleng_ios_identity \
  tools.tests.react_native.test_android_observation_lease \
  tools.tests.react_native.test_ios_observation_lease -q
```

**205 passed，26.128 秒**。provider 专项还使用真实 session/framing/serial adapter、仅 mock 串口和
lease 边界；覆盖 no-peer、boot drift、设置位读回、deadline、单次 mutation、资源关闭多重失败。

- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：48 passed。
- `pixi run just fmt` / `pixi run just lint`：通过。
- `pixi run just docs-check-mermaid` 与本轮 authored Markdown UTF-8/fence/whitespace/conflict 检查：通过。
- 检查频率新增测试先红灯，runner 组合先因缺模块红灯；超时与定位日志未删除。

**未通过的广域检查保留：**子任务运行的 `pixi run just react-native tools-test` 在 180 秒超时，
没有全量 verdict，未再重试；legacy `pixi run just react-native host-test` 为 620 passed、1 failed，
失败位于 `rf_ccc_disable_reject.test.tsx` 的 `spawnSync pixi ETIMEDOUT`，未修改或删除该范围外测试。
正式 App 的 48 项不能替代 legacy suite 或全量 Python 通过。

日志：`.work/formal-prepared-scan-{candidates,regression,host-check,host-test,fmt,lint,mermaid}.log`；红灯和
诊断为 `.work/formal-scan-runner-{red,targeted,single,stack,progress,location,candidates}.log`；
广域失败为 `.work/bleng-scan-admission-*.log`。

本轮未执行设备安装/启动、Appium 真机、radio/permission mutation、固件 provisioning 或 RF，未提交 Git。
仍不满足 internal 删除条件，也不代表 native/runtime/package/release/ABI/Phase exit。
