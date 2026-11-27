# Formal scan：Appium/session ownership 与平台收尾交接

日期：2026-11-04。真实 Mac，本地 Pixi/Just，未提交工作树。

**仍未进入新链路的真机 RF。** 本报告记录软件 ownership、隔离 SDK/HTTP/进程测试和正式 App Jest。
历史 mobile RF 结果不继承到此候选；`host_gate_status` 继续为 `pending`，不冻结 ABI/Phase。

依据：[单一 Plan §1.5](../../../.plan/PLAN.md)、[host tooling 职责](../../research/react-native/react-native-host-tooling-architecture.md)。
前段 [prepared scan](2026-11-04-formal-prepared-scan.md) 与 [candidate identity](2026-11-04-formal-host-app-identity.md)
的原结果、失败和范围不改写。本轮进一步收紧 prepared runner 的准入与 phone release 条件。

**后续核实（同日）：** 下文 Android 两个 skip 仅表示 host 的请求参数，不是 driver 无隐式变更的证明。
[本地源码审计与准入修正](2026-11-04-formal-android-automation-admission.md) 发现仍有 settings/AppOps
写入、旧 session 清理与 retry/root 路径；正式 Android scan/lifecycle 现已在启动前拒绝。
本报告原合成测试结果保留，但不能据其推导当前安装的 Android driver 满足 prepared contract。

## 本次实际接入

### 本轮 local Appium child，不是旧端口上的任意服务

[`AppiumServer`](../../../tools/mobile_appium.py) 仍是共享基础设施，不另建 server registry：

- one-shot start，独占日志；每实例生成 nonce `--base-path`，只读 `base_url` 与该路径一致。
- spawn 前拒绝已占用 loopback port；readiness 前后检查 caller owner 与本次 Popen 存活。
- readiness 只接受明确布尔 `value.ready`，缺字段、字符串、畸形/重复 JSON key、NaN、重定向、
  截断或超限响应不能成功。用标准库 HTTP parser，socket I/O 与轮询共用绝对 deadline，整个
  response（含 headers）不超过 64 KiB；不通过 DNS/proxy/redirect 改接其他 endpoint。
- `require_running()` 只读 local child 状态，不冒充设备健康。start 可被更短 caller deadline 收窄。
- stop 只处理记录的 owned process group，TERM、必要的 KILL 与 reap 有独立总预算；升级/超时/
  reap 失败保持失败，重复 stop 不向可能复用的 PID 发信号，也不能改写已知失败。
- startup 失败仍由 caller 的 finally stop 收尾，没有隐式设备清理或自动重启。

本机 Appium 3.9.0 的 `server --help` 确认 `--base-path`。bind probe 不是原子端口 reservation，
nonce/child 检查也不是对恶意本地服务的认证；这些是合作 host 的 ownership 检查，不是安全 attestation。

### 一个实际 session owner，平台 preparation 仍显式

新增 [`ScanAutomationOwner`](../../../tools/react_native/bleng_automation.py)：

1. 在同 run 的实际 phone lease 下检查必填 platform owner。
2. 创建并检查自己的 local Appium child。
3. 通过现有 SDK helper 创建一个 attach-only session：iOS 使用已运行 WDA URL；Android 使用
   skip-device-initialization / skip-server-installation 的已准备 helper 路径。
4. 固定实际 driver/session/executor 和 App/device/capabilities，输出 start evidence。
5. 收尾依次执行 session quit、独立 local server reap、平台 owner finalizer，再发布 close evidence。

不安装 helper、不 root/grant、不 sign/build WDA、不重新启动或 reset 目标 App。iOS attach 参数和
Android package/activity 在启动 child 前验证，避免把本地参数错误误记为“远端 session 可能已创建”。
HTTP request 的 deadline 与 parent budget 对齐；server startup 的 platform guard 使用同一个更短
startup deadline，而不是较长的整个 scenario deadline。

`require_platform_owner(deadline)` 和 `finalize_platform_owner(deadline)` **均必填，无默认成功实现**。
前者验证原 WDA/forwarder 或 UiAutomator2 scope；后者验证并关闭同一个 scope。具体平台 producer
的真实接入与来源证据仍未完成，本轮测试用明确可失效、可关闭的合成 platform scope，并 mock 实际
WebDriver/process/socket 边界；不把 callback 测试当作真实平台 owner 已通过。

创建请求失败且没有可信 session ID 时，保留 unknown；不枚举/接管其他 session、不盲目 DELETE。
已观察到的 owner/session/receipt 漂移永久撤销 authority，后来恢复字段不能恢复权限。未知平台
owner 不被清理，但本轮已知 local server 仍独立回收。普通 scenario 超时与身份失效区分，cleanup
可以有独立预算；预算耗尽后最多额外两秒只用于 owned local child reap，且仍记录失败。

### phone release 必须等平台 owner 收尾

接线审查发现：仅关闭 driver 或 local server 就释放 phone lease，会把 WDA/UiAutomator2 或转发
资源留到 lease 外清理。已修正：

- [`run_prepared_scan`](../../../tools/react_native/bleng_scan_runner.py) **必须接实际 managed automation
  owner**，不再接受裸 driver 作为 phone release 的充分依据。
- automation.close 包含必填平台 finalizer；失败时仍完成独立 App/fixture 收尾，但 phone quarantine。
- start/close JSON 字节摘要被重验；`require_closed()` 不仅看一个布尔字段。platform finalizer 缺失、
  失败、身份失效或证据漂移都不能允许 phone release。
- 场景成功但平台清理失败，仍保留成功 App，分别记录 `scenario_status=passed` 与
  `session_status=failed`；不将 App Passed 代替 host gate。

新的顺序是 `automation(session → local server → platform finalizer) → App → passive fixture →
prepared evidence → phone → final result`。具体 producer/真实 callback、设备预检、借用 lease
来源和完整 bundle 未验证，因此阶段全通过时仍只得到 `host_gate_status=pending`。

## review 驱动的补测与修正

- 完整 JSON 关闭后才独占发布；链接成功后若中断，也撤回本次刚发布的 inode，不删除外部替换文件。
- 抽出 [`capture_io.publish_new_json`](../../../tools/react_native/capture_io.py)，由 runner 与 automation
  receipts 复用；commit、rollback、temporary cleanup 的错误不能互相覆盖。
- 场景/cleanup 的中断与最终 result 发布失败叠加时，传播原中断，并附加 publication error。
- WebDriver quit 失败不会跳过 local server reap；server/平台收尾与 publication 错误分别保留。
- 新测试验证 unknown session、owner 丢失、session replacement、平台 cleanup 未验证、记录漂移、
  更短 startup/cleanup budget、晚返回的已知 session，以及不可恢复的单次失败。

## 实际验证

先新增 owner/边界失败测试，再实现；review 的 publication、预算与本地参数问题也有红灯记录。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_automation \
  tools.tests.react_native.test_mobile_appium \
  tools.tests.react_native.test_capture_io \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui -q
```

**158 passed，最终重跑 10.237 秒**。Appium server 核心还独立覆盖有限本地 child/HTTP 测试；没有启动
实际 Appium/WDA 服务或向设备发送命令。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate -v
```

**59 passed，121.575 秒**。两端 prepared scan 已改为交接真实 automation owner，实现仍使用真实
candidate/scene/provider/临时 lease，mock 实际 SDK/HTTP/process 边界。覆盖 server 在场景中退出、
平台 finalizer 失败时不能释放 phone，以及主错误/cleanup/证据失败组合。此耗时不是设备性能结果。

- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：48 passed。
- `pixi run just fmt`、`pixi run just lint`：通过。
- `pixi run just docs-check-mermaid` 与本轮 authored Markdown 的 UTF-8、fence、whitespace、conflict 检查：通过。

日志：`.work/formal-automation-{red,publication-red,review-red,targeted,combined,handoff,regression,candidates,host-check,host-test,fmt,lint,mermaid}.log`；
server 专项为 `.work/mobile-appium-*.log`。广域 Python 180 秒超时及 legacy Jest `spawnSync pixi ETIMEDOUT`
沿用前段未通过状态；本轮未盲跑全量 tools-test/host-test，也未删除失败测试。

## 仍未证明

- 具体 WDA/forwarder 与 UiAutomator2 producer 的真实 preparation、ownership 与 finalizer 来源。
- 当前手机支持、physical selection、签名/预检、真实 native identity 检查耗时和 deadline。
- 完整 final host bundle、移动 runtime、Hermes/reload、其他 typed operation/RF 场景迁移。
- iOS PID-only termination 与 Android package force-stop 的非原子边界仍在，不能升级为已接受。

没有 RF、安装/启动设备 App、radio/permission mutation 或 firmware provisioning；未提交 Git。
不开放 scan CLI，不删除 legacy internal，不声明 resource-zero、release、ABI 或 Phase exit。
