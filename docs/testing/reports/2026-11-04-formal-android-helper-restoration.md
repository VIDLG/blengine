# Formal Android：按权限语义恢复 helper 与解除已知隔离

日期：2026-11-04。真实 Mac、Android 真机 `3ac5cc09`、本地 Pixi/Just、未提交工作树。前序证据：[授权 helper 准备与 exact-shape 恢复失败](2026-11-04-formal-android-helper-scope.md)。用户随后明确批准：保留测试功能与安全要求，但非功能细节可按实际平台行为调整，不要求兼容旧的记录形态断言。

## 决策与代码

[`require_mock_location_restored`](../../../tools/react_native/bleng_android_automation.py) 比较原始和新鲜读回的 mode/scope，而非 Android operation entry 是否消失：

- scope/mode 一致时允许历史时间等显示细节不同，原始观测仍由调用方保存。
- 额外允许已实测的单向表示变化：原 `No operations / Default mode: deny`，恢复后只有 package-scope `deny`。
- 不将 UID deny 与 package deny 合并，不允许新增 UID scope，不将 `MODE_DEFAULT` 当作 deny，也不接受 deny 变 allow/ignore、冲突、重复或未知输出。
- 不推广成 default allow 与 package allow 的任意等价规则；正常 session admission 和 prepared-baseline 检查没有被放宽。

这不是旧输出兼容层，也不要求为了抹除系统记录执行全包/global reset。测试保持权限撤销、临时文件清理和 owner/lease 的功能目标；额外系统记录作为可见 evidence 保留，不伪装成完整数据记录回滚。

测试位于 [`test_bleng_android_automation.py`](../../../tools/tests/react_native/test_bleng_android_automation.py)，覆盖已知 deny 表示差异、相同 mode 的历史显示变化以及拒绝 UID scope/grant/default/冲突/重复等情况。生产 parser 不 import 测试；没有添加普通 run 自动 repair、宽泛 quarantine takeover 或新 provisioning CLI。

## 已知 quarantine 的显式只读恢复

Recovery run：`00e7f8e3-622c-44f0-b9d1-6268a23987a7`。

证据：`.work/android-helper-recovery-00e7f8e3-622c-44f0-b9d1-6268a23987a7/`。

这个 one-shot recovery 只处理本次对话产生、明确获批的 `c1228f5d-a4a0-4731-888c-bf8337aeadf8` no-session helper attempt，不从任意 readiness JSON 构造 session authority：

1. 核对 exact 原 owner bytes、原 lease directory identity、原 host PID 已不存在，以及原 `phone.json` / `result.json` 的固定 SHA256。拒绝未知/替换 lease、仍存在的原 host PID 或改写的失败记录。
2. 验证旧 attempt 明确未创建 Appium session、owner prepare/finalize 和 store 删除均通过，旧失败仅为外层记录形态判据。
3. 新鲜读取同 boot/user、三个 helper 的安装路径/hash/唯一 UID、进程、forward、instrumentation、Settings services、hidden keys 与 IME。要求没有未知 helper/uiautomator 进程、forward 漂移或残留 instrumentation。
4. 确认 mock store 仍 absent、AppOps 仍是 package deny，使用新的纯语义 validator 通过。完整 global settings receipt 仅留相关 policy rows 和 stdout SHA256，不保存无关设置值。
5. 先发布 `verified.json`，再复查原 lease 并将该 exact directory 归档为本 recovery 的 `released-lease/`，最后发布 `released.json`。

实际结果：`status=released`、`device_mutation=false`、`record_shape_changed=true`。**原 attempt 的 blocked/quarantined 文件与 SHA256 均未改写**；独立 recovery 只证明按新获批判据完成读回和归档释放，不把原失败重标为 passed。租约和 evidence 并非 crash-atomic 事务，合作锁也不是对系统中其他控制者的强制排他 attestation。

## 新租约下的完整准备/恢复验证

Fresh attempt：`98d7f019-381b-4ba6-9a2d-0cf59be88b21`。

证据：`.work/android-helper-preparation-98d7f019-381b-4ba6-9a2d-0cf59be88b21/`。

使用真正的全新 `AndroidRadioController` lease，再执行已授权的 helper-only temporary preparation：

| 步骤 | 实际结果 |
|---|---|
| 审核 helper hashes/原安装身份、原 package deny、store absent | 通过 |
| root-scoped helper 单项 AppOps → allow，读回 scope | 通过 |
| noclobber 创建本次 store，pin identity/bytes | 通过 |
| 实际 `AndroidAutomationOwner.prepare()` | 通过 |
| 明确没有 session 的 `finalize()` / `require_closed()` | 通过 |
| 核对文件 identity 后删除本次 store，读回 absent | 通过 |
| helper 单项 AppOps → deny，语义恢复核对 | 通过 |
| phone finalization、lease release | 通过，cleanup errors 为空 |

这一轮原始与恢复后的 AppOps 都是 `MOCK_LOCATION: deny`，`record_shape_changed=false`。顶层 `status=passed` 和 `phone_status=passed` 仅指本轮 helper 准备/恢复；`app_install/app_launch/appium_session/rf=not_run`，`host_gate_status=pending`。这没有验证真实 WebDriver 的创建、DELETE 或 DUT runtime，更不是 scan/RF verdict。未操作 DUT BLE 权限、radio、IME、定位坐标或固件，也没有全包/global AppOps reset。

## 本轮验证

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_automation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_android_automation_observation -q
```

- 新 validator 回归先 red（实现不存在），加入实现后组合 **71 passed，44.971 秒**。
- 日志：`.work/formal-android-restoration-semantics-{red,green}.log`。
- 两个本轮修改 Python 文件的 targeted Ruff lint / format check 通过；`pixi run just fmt`、`pixi run just lint` 全量通过，包含 workspace/all-targets Clippy，Ruff format 报告 299 文件未变。
- `pixi run just --no-deps react-native formal-host-check`：typecheck/autolinking/no-internal 通过；`pixi run just react-native formal-host-test`：**48 passed**，保留已有 SafeAreaView deprecation warning。
- `pixi run just docs-check-mermaid`：203 份 Markdown 中 27 个图通过；本轮四份 authored/updated Markdown 的 UTF-8、围栏、尾随空白、冲突标记和本地链接检查通过。
- 以上日志为 `.work/formal-android-restoration-semantics-{fmt,lint,host-check,host-test,mermaid}.log`。
- 真机 recovery 与 fresh attempt 分别由 `pixi run just --command python -c 'import runpy; runpy.run_path(..., run_name="__main__")'` 执行，完整 ignored one-shot source、日志、逐命令回执和原/新状态均保存。
- 新 attempt 总操作预算 100 秒、独立 cleanup 60 秒，recovery 90 秒；单条命令不超过 5 秒。没有 timeout，也没有将未知结果当作已知状态重试。

## 剩余边界

实际 managed Appium session、目标 App 的安装/启动/UI runtime、完整 host bundle 和 RF 尚未执行。后续应把已验证的 helper preparation/semantic rollback 接入明确拥有生命周期的 host scope，再连接正式 UI 场景；ordinary run 不隐式 provision，不因本报告释放另一条未知 quarantine，也不把 helper 的一次通过推广为整个 Android 自动化 gate 通过。
