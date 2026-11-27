# Formal Android managed scan：共用准备、同进程 fixture 与 sealed evidence

本轮接续[具体 prepared-scan 组合与迁移审计](2026-11-07-formal-android-scan-handoff.md)，实现正式 Android scan 的 named live producer，不再只依赖调用方手工拼接已准备 tuple。方向仍依据 [Plan §1.5](../../../.plan/PLAN.md) 与 [consumer 职责](../../../tests/consumers/README.md)。

**当前结论：managed producer、具体 owner 与离线 evidence 已接通，但完整 scan→stop→close→Evidence 仍无 pass。最新固定控制区 APK `5fd07609…` 的 RF attempt `832206de…` 停在 idle 的第一次 Evidence 导航校验，未提交 Start/scan，正常收尾完成。随后同 APK 的独立 idle-only diagnostic `f3892c13…` 七次导航与收尾 passed，`rf=not_run`；两者不能合并为完整 gate，也不能据此认定首次导航失败的原因已修复。** 较早 APK 的真实 DK-A 广播观察只属于 `c9059a16…`，其 failed/quarantine 与后续恢复、其他失败候选均保留。已补 wrapped-timeout cleanup 分类、固定控制区及有界页面失败上下文；各软件/设备边界见 §5—§9。先前 HANS lifecycle 或 idle-control pass 不继承为这份 producer 的 scan/RF pass。`status=passed` 仅表示对应 scope；成功 scan scope 的 `host_gate_status=pending`、`rf=scan-ui-observation` 不变，未满足 internal 删除、集中 core review、ABI freeze 或 Phase exit。

## 1. 最小维护入口与责任

| Owner | 本轮责任 |
| --- | --- |
| [`run_bleng_android_scan`](../../../tools/react_native/run_bleng_android_scan.py) / `formal-host-android-managed-scan` | 薄 CLI，只转交完整显式输入和授权，运行后复验 bundle；无 build/setup prerequisite |
| [`run_android_scan`](../../../tools/react_native/bleng_android_scan.py) | 本地准入、冻结 candidate、phone/fixture leases、live fixture proof、共用 Android 准备、prepared handoff、外层 lease 与最终 evidence |
| [`AndroidManagedHost`](../../../tools/react_native/android_managed_host.py) | lifecycle/scan 共用的具体 helper/platform/candidate/Appium 准备；不拥有 phone、fixture、UI 或结果判定 |
| [`run_prepared_scan`](../../../tools/react_native/bleng_scan_runner.py) | 原 typed scan UI 场景及其已交接资源收尾；不复制第二套按钮流程 |
| [`DirectFixture`](../../../tests/peripherals/nrf52840/direct_fixture.py) / [`DeviceLease`](../../../tests/peripherals/nrf52840/resource_lease.py) | 同进程真实 installed-image proof、bounded startup、被动关闭、资源和 broker 退休 |
| [`bleng_scan_evidence`](../../../tools/react_native/bleng_scan_evidence.py) / `formal-host-scan-verify` | 正式 scan scope 的 movable offline consistency/integrity replay，不重新访问硬件或授权 RF |

不新增 App、generic runner/driver registry、remote fixture proxy、raw Probe API、依赖包或第二 native image。正常 UI 继续使用官方 Appium Client/server/stock UiAutomator2。

CLI 要求 physical phone、prebuilt APK/hash、三个 reviewed helper hashes、已有 Appium 路径/独立端口，以及明确的 fixture contract/profile/port/**canonical resource identity**。`--authorize-install`、`--authorize-helper-settings`、`--authorize-fixture-start`、`--authorize-bluetooth-permissions` 四项均必填。fixture 授权只允许 live proof 后的精确 GATT profile setup/advertising，不是 flash/reset。permission 授权独立允许 root proof、两项蓝牙权限的 baseline/readback/原值恢复；不是 radio 授权。

`--use-root` 和 `--authorize-server-hans-exemption` 分别显式 opt-in；权限 mutation 不再隐含于 scan 入口，缺少独立许可在目录/owner/设备动作之前拒绝。没有 radio action、helper/tool 安装或 destructive retry。完整参数及离线入口见 [formal consumer](../../../tests/consumers/react-native-bleng/README.md#managed-android-scan)。裸 `formal-host-ui android` 的 owning-handoff 拒绝不改变。

## 2. 实际执行顺序和准备复用

本地校验 APK/helper/path/输入与授权，创建独占 attempt 并冻结 APK；取得 phone scope 后，按明确 port 取得物理 lease，重新核对 USB resource identity。provider 冻结本 run 的 contract/profile/request，创建**同进程** `DirectFixture`：live attestation、idle/no-peer、profile setup/advertising 成功且 receipt 核验后，才构造共用 Android host、准备 helper、安装/冷启动 App、启动 managed Appium。

lifecycle 原有本地验证和三个准备阶段移入共用模块，但保留诊断先于 helper mutation 的顺序、partial owner references、cleanup labels 与 result schema。共享准备不做 App bind 或释放 phone；任何中途失败仍可清理已实际创建的 owner，不靠一个“准备成功”布尔值代替证据。

fixture startup 新增可选 absolute deadline，复用已有 leased transport 的 read/write/碎片读取机制；超时/late hook 后不能提交后续 mutation。原无 deadline 调用的行为不因新入口被整体替换。explicit-deadline 启动失败只退休资源，不走 legacy disconnect/reset。`close_resources()` 也是纯资源退休，不构成 advertising-inactive 证明。

broker acquire/release 仍各有独立固定上限；local hooks/OS close 不是可抢占硬 wall-clock。inner/outer cleanup completion 使用同一个实际 absolute deadline，迟到不能发布成功 sealed bundle；失败证据另用有限 seal budget 保留，不能靠再给一段 cleanup 时间取得 pass。

## 3. 审查发现的边界，均已修正并回归

### 所有权、异常与资源

- **不可从目录存在猜 handoff。** 新 `PreparedScanHandoff` 是同进程 bookkeeping，不是 lease/token authority。prepared runner 独占创建结果目录并进入 cleanup 保护后才确认交接；预存在的空目录或 admission refusal 不会使外层跳过 cleanup。交接后不能重做 DELETE、App stop、fixture stop 或 phone finish。
- **已完成事实不能因 lost return 丢失。** inner 在中断后先发布终态再传播；outer 会读取其真实 result，保留已执行 scenario、session/close receipt 和所有 cleanup errors，同时保留逃逸异常本身。不会把已完成扫描写成 `not_run`。
- **记录失败不能替换启动中断。** fixture start error、start-record publication error 独立保存。`KeyboardInterrupt` 不被 `finally` 中的写盘异常遮掉。start 成功但 required record 丢失，仍保留实际 passive close receipt，但 fixture verdict failed，不继续安装 App。
- **broker acquisition 中断也要 unwind。** `DeviceLease.acquire` 在 readiness 前收到 `BaseException` 时仍退休已启动 broker；acquisition/release failures primary-first 聚合，不能留下无人持有的 broker。
- **构造器的 cleanup uncertainty 不是普通拒绝。** `DirectFixture` 构造失败后独立收集 session/serial/lease unwind errors。clean rejection 保留原异常对象；有 cleanup failure 时用 typed `FixtureConstructionError` 明确通知 producer，阻止不确定情况下释放 phone。没有通过吞错或放宽 reset scope 解决。
- **外层 lease 失败不改写已释放的 phone。** 正常 prepared runner 先完成 phone finalization，outer 再退出物理 lease。如果 broker release 失败，outer failed，保留 phone 已完成事实，不重新认领或伪造 quarantine。

### 离线 evidence

独占 publication、bounded tree/hash/ZIP/strict JSON 和 link/drift 检查复用原机械实现。lifecycle snapshot 只增加内部可选 layout 参数，原默认 layout/facts 没有放宽。正式 scan 自己拥有状态/fixture/trace 约束，不套 legacy probe 的 RF ledger。

回归覆盖了可重封 manifest 的 semantic attacks：

- prepared/inner 的非空 primary 与 cleanup prefixes 不能从最终 result 擦除后改成 pass；
- HANS 授权为 true 的成功/已运行 session 必须有对应 receipts；false 不能保留 HANS mutation evidence；
- passive close 要求 canonical 16-byte lowercase hex nonce、ADVERTISING supported/current bit 一致，以及 exact session/resource identity，不接受“bit 仍开、另写 inactive=true”；
- inner driver session、contract/profile/resource tuple、两份 copied fixture input 及其 hashes 必须与实际 owner/snapshot 一致；
- acquired/released lease 的 PID/PGID 必须为 request 的 exact positive identity；requested 与 observed physical identity 分开保存，成功必须相同，失配失败不能把实际取得的 lease 记成请求的那一块 DK；
- failed attestation 合法地没有 installed receipt；snapshot 写入中断也可只有部分 immutable inputs。failed bundle 保留每份实际存在的 canonical input 和独立 lease 观察，不虚构完整 plan、安装证明或成功 start；
- helper hashes 来自实际 inventory receipts，不把 AppOp 文本误作 inventory；
- 搬迁副本可改变 filesystem mtime。provider 新的 pure `verify_receipt_contents` 被 offline 复用；原 live `verify_receipt` 仍额外检查真实请求 mtime 和当前时间，未放宽现场 freshness。

manifest 是 host-submitted consistency evidence，不是抗恶意篡改签名或远端设备证明。runtime passive close 仍实际核对启动时 pinned boot nonce；当前 start record 未单独保存该 nonce，offline 不能仅凭两条 JSON 重新证明 startup-to-close boot continuity。保留此观察边界，不从合法 nonce 格式升级为 RF/resource-zero。

## 4. 软件回归

所有设备面对面边界均被明确隔离：真实 phone lock/host/fixture/session/receipt/publisher 组合保留，USB enumeration、broker 子进程、SDK/installer/Appium、serial/BTP/doctor 与 UI responses 使用 owner-local synthetic support；未知端口/命令拒绝，占位身份不会进入真实设备命令。

### 初始软件交付 parent 实际执行

| 命令/范围 | 结果 | 完整日志 |
| --- | --- | --- |
| `pixi run --locked python -W error::ResourceWarning -m unittest -v tools.tests.react_native.test_bleng_android_scan` | **16 passed / 133.517s** | `.work/managed-scan-delivery-producer.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest -v tools.tests.react_native.test_run_bleng_android_scan` | **6 passed / 0.005s** | `.work/managed-scan-cli.log` |
| scan CLI、scan/lifecycle/HANS/device-local evidence + canonical fixture-session | **155 passed / 7.093s** | `.work/managed-scan-final-evidence-cli.log` |
| shared Android prep、原 lifecycle CLI、Android/iOS candidate、automation owner | **122 passed / 109.842s** | `.work/managed-scan-final-compatibility.log` |
| `pixi run --locked just react-native formal-host-test` | **2 suites / 52 passed** | `.work/managed-scan-final-jest.log` |
| formal `npm run typecheck`、`pixi run --locked just react-native host-check`、new module `--help` | 通过；不启动设备 | `.work/managed-scan-final-{typecheck,host-check}.log`、`.work/managed-scan-cli-help.log` |

两个组合的确切 unittest 模块：

```sh
pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_run_bleng_android_scan \
  tools.tests.react_native.test_bleng_scan_evidence \
  tools.tests.react_native.test_bleng_lifecycle_evidence \
  tools.tests.react_native.test_bleng_hans_evidence \
  tools.tests.react_native.test_bleng_device_local_evidence \
  tests.peripherals.nrf52840.test_fixture_session -q

pixi run --locked python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_android_managed_host \
  tools.tests.react_native.test_run_bleng_android_lifecycle \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_bleng_ios_candidate \
  tools.tests.react_native.test_bleng_automation -q
```

后者经现有 `run_quiet.py --no-tty --progress-seconds 20`、180 秒外层预算执行，非无限等待。focused provider 的 resource lease/direct fixture/passive close **99 项 / 6.484s 通过**（`.work/managed-scan-final-focused-provider.log`），包括真实 synthetic broker 进程和 framed/serial adapter 边界；其余 transport/session 具名回归与 red→green 日志保留在 `.work/logs/`。

**未通过项不洗白：**实际 `pixi run --locked just peripheral nrf52840-test` 运行 **513 tests / 30.577s，1 error**：`test_adapter_transition.AdapterTransitionTests.test_direct_start_hook_is_after_services_before_profile_mutation` 用 `DirectFixture.__new__` 构造不完整 fixture，缺少已有 guard 要求的 `_start_attempted`。原 start guard 在本轮前即要求该字段；未为了全绿改动范围外测试或弱化生产 guard。完整日志 `.work/managed-scan-final-provider.log`。不能说 provider discovery 全绿。

最初的实际组合还暴露 helper receipt shape 与 failed-attestation partial bundle 校验不兼容，已按真实 receipts 修正；原失败、审查攻击 red 和 publication/interrupt 尝试保留，不把它们算作真机失败或真机 pass。共享准备的额外 full lifecycle 模块回归曾在 120 秒超时，未计为完成；最终上述具名兼容组合通过，不替代 canonical 全 tools-test。没有再盲跑此前多次超时的完整 RN tools discovery。

交付检查为 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check`，日志 `.work/managed-scan-delivery-{fmt,lint,docs,diff-check}.log`。未改 native/package ABI，因此没有重建 native artifacts；没有 commit/push。

## 5. 后续真机失败与离屏边界修正

### 保留的真实失败，不是旧 freezer 问题的复现

- Attempt：`6f41b988-8aff-40b7-97ee-bc3400bb4a6a`；原目录 `.work/formal-android-managed-scan/6f41b988-8aff-40b7-97ee-bc3400bb4a6a/`，原 failed result/manifest 不改写、不重新 seal 成 pass。
- Phone：`3ac5cc09`；App：`com.blehubblenghost`；原 APK SHA256：`51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef`。
- DK：`jlink:1050252028`，BTP port `/dev/cu.usbmodem0010502520281`；contract/profile 是 canonical `multifunction-rf-v2-a.json`。installed-image receipt、实际 fixture setup/close 与 lease 结果留在该 attempt，不由 APK 或构建结果代替。
- `scan/result.json` 只观察到 `idle`、`started`。primary 保留原文 `TimeoutError: Android helper preparation deadline exceeded`；这条 deadline 标签本身不能证明实际阻塞发生在 helper preparation。
- `automation/appium.log` 有 **262 条**针对 `scan-observation-timeout` 的查找请求，正常返回 `404 no such element`，而不是相应请求的 ADB `service_accept` 或 UiA2 代理无响应。App 已进入 Current run，但 runner 只反复查找，没有滚动；APK 包含该 testID。结合 [`App.tsx`](../../../tests/consumers/react-native-bleng/src/App.tsx) / [`ManualScan.tsx`](../../../tests/consumers/react-native-bleng/src/ManualScan.tsx) 的原 ScrollView 布局，离屏 accessibility 节点未暴露是本次定位到的 UI 边界；不是新 RF verdict。
- `platform/permission-scope.json` 实际记录两项权限 `false → true → false`，`cleanup_errors=[]`。scope 的 `status` 仍为 **failed**，并保留同一 primary；准备/恢复事实不等于整场或 permission gate pass。
- HANS apply/restore 各自 `passed`；Appium DELETE 返回 `200 / 553 ms`，helper restoration passed；fixture close 记录 advertising bit clear、资源退休与 lease release。outer `phone_cleanup_errors=[]`，但 `phone_status=failed`，不能改称 phone gate passed。

### 最小软件修正与 red → green

[`run_bleng_ui.py`](../../../tools/react_native/run_bleng_ui.py) 仅 formal scan 的 Android 路径启用 stock UiAutomator2 `mobile: scroll`。target 必须在具名内容集合内，每次滚动重新获取通过 `-android uiautomator` 定位的 `workbench-content` origin，显式传 `elementId`、selector 和 `maxSwipes=4`。缺失 origin 或 scroll 异常直接失败；调用前后仍检查 authority/总 deadline，迟到或失权后不做后续 remote work。click 异常不重放 BLE 动作。iOS 和 lifecycle 不隐式启用该滚动。

本地安装的 UiA2/server 实现核对表明：这是官方 extension，origin 要求 UiObject；`maxSwipes` 是 UiScrollable 的搜索上限，可能先回起点再向前搜索，不是“整个调用最多四个 gesture”或可抢占 wall-clock 保证。driver HTTP timeout 仍需有限。源码核验不替代真机滚动证据。

审查还复现了初版 host 修正中的往返问题：按钮 disabled/stale 时，每轮页检查滚回标题，随后又滚向按钮。新增 Python 用例先 **red：同一等待实际 5 次 scroll**。随后把 `workbench-page` 移出外层 ScrollView，保留逐轮 foreground/state/page 检查，green 时只需一次 scroll。广播样本本身保持完整，只把 `scan-samples` 放入 `maxHeight=160` 的嵌套 viewport，避免最多八条长摘要不断拉长外层操作区；Current run/Evidence 共用同一摘要组件。Jest 先 red，再验证完整内容、两页保留及 controls 不在样本 viewport 内，不声称它执行了 Android 原生布局。

另外补齐独立 `--authorize-bluetooth-permissions`：CLI 必填、producer 默认拒绝，不能只在 request 中硬编码授权。缺省、false 或非 bool true 均在目录、lease 和 SDK 动作之前拒绝；现有 exact grant/readback/restore owner 不变。该入口变更要求旧命令显式加上新 flag。

离屏边界修正的 release APK 通过 `pixi run --locked just react-native formal-host-android-build release` 构建（含 typecheck/autolinking），SHA256 **`38cb89d8fb6c3c65e63324e8fd9892a6eb631ba3a1db9bafd5989370ad35d264`**、43,374,769 bytes；日志 `.work/scan-scroll-release-build.log` / `.work/scan-scroll-device-preflight/prior-release-build.log`（后者为下一次构建前保存的原完整 build log）。这次构建本身没有访问设备；后续只执行了 §6 的一次显式安装/scan，未修改 radio/firmware 或宣称原失败已恢复。

### 本次修正的实际软件验证

| 命令/范围 | 结果 | 日志 |
| --- | --- | --- |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_android_prepared_scan -q` | **107 passed / 98.131s** | `.work/scan-scroll-regression-final.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_scan tools.tests.react_native.test_run_bleng_android_scan tools.tests.react_native.test_bleng_scan_evidence tools.tests.react_native.test_android_bluetooth_permissions tests.android.test_permission_control -q` | **94 passed / 139.360s** | `.work/scan-scroll-producer-evidence-verified.log` |
| `pixi run --locked just react-native formal-host-test` | **2 suites / 53 passed** | `.work/scan-scroll-jest-final.log` |
| `pixi run --locked just react-native formal-host-check` | typecheck / 正式包 autolinking 通过，无 internal 泄漏 | `.work/scan-scroll-typecheck-final.log` |
| `pixi run --locked just react-native host-test` / `host-check` | 原 internal discovery **17 suites / 621 passed**；typecheck 通过，不作为迁移完成证明 | `.work/scan-scroll-host-{test,check}.log` |
| `pixi run --locked just fmt` / `lint` / `docs-check-mermaid` / `git diff --check` | 通过；不代表完整 tools/provider discovery | `.work/scan-scroll-{fmt,lint,docs}-delivery.log`、`.work/scan-scroll-diff-check.log` |

未隐藏的软件失败：初版新 UI 测试先 red（缺容器/viewport、5 次往返）；第一次 fmt 因新增回调未绑定 loop variables 报 B023，已修正；另一次组合误用旧模块名 `tools.android.test_permission_control`，整体 `49 tests / 1 import error`，随后以上正确模块组合通过。日志分别为 `.work/scan-scroll-{container-red,layout-red-jest,fmt,producer-evidence-final}.log`。未重新盲跑历史多次超时的完整 RN tools discovery。

编辑器 diagnostics 选择 Xcode Python 3.9，仍报 Appium/Selenium 缺失，以及测试替身既有 override/故意缺参用例的类型诊断；不能称 editor diagnostics 全绿。实际测试使用 `.pixi/envs/default/bin/python`，核验 `Appium-Python-Client=6.1.5`、`selenium=4.51.0`，上述测试/构建通过。未向系统 Python 安装依赖或更改用户解释器配置。

## 6. 新 APK 真机：已匹配目标广播，完整 gate 仍失败

### 一次受控执行与原始结果

预检串行执行 `pixi run --locked just peripheral nrf52840-doctor` 和 `nrf52840-attestation 001050252028 .work/scan-scroll-device-preflight/attestation.json`，均通过；attestation 为预期 `nrf-multifunction-rf-v2-a` / image SHA256 `9b97e9e0b332930de940e2c7c4ff2331e11d402e7f7f75215da870eef00ce7e1`。普通 SDK daemon PID **16841** 的 executable 经 `lsof` 核对为实际 SDK 路径，本轮不重启、不操作外部客户端；phone lease、旧正式 App/server PID 与 forwarding 在 admission 前为空。独立预检不代替 producer 同一物理 lease 内的新 live receipt。

实际通过 maintained `pixi run --locked just react-native formal-host-android-managed-scan` 执行，输入是 §5 的 **38cb89d8…** APK、原 exact helper hashes、phone `3ac5cc09`、DK `jlink:1050252028`、v2-A contract/profile 和原 BTP port，端口 `4737/8237`，`timeout=240` / `cleanup_seconds=90`，四个必需授权与 `--use-root --authorize-server-hans-exemption` 均显式传入。外层复用 `run_quiet.py --no-tty --progress-seconds 20` 并保留日志 `.work/scan-scroll-device-gate.log`；命令正常结束 **exit 2**，不是外层进程被 timeout 杀掉。没有第二次 RF 尝试。

新 attempt：**`c9059a16-ceae-4fc8-ba3a-c125f4329274`**，原 bundle：`.work/formal-android-managed-scan/c9059a16-ceae-4fc8-ba3a-c125f4329274/`。具体观察：

- `scan/result.json`：`idle → started → scan:running → scan:advertisement-matched`。目标谓词来自该 run 的 live-proved fixture contract；不是 Jest/fake 产生的记录。没有 `scan:stopped`、`closed` 或最终 Evidence 成功，不升级为完整 RF/native gate。
- 官方 scroll 正常返回，离屏 `scan-observation-timeout` 不再停在旧的 262 次 missing polling；标题在固定区，样本内容可通过 accessibility 文本读取。此 run 不证明任意视口、任意八条最长样本或 iOS 布局均通过。
- 匹配后的 populated-run 浏览准备中，在 `workbench-tests` click **之前**，fresh App identity 的 `shell sha256sum <原 installed base.apk>` 用尽 scenario 总预算。primary 是 `AndroidSdkError: command failed to start or finish…`，附有 `Android identity post-command boundary: Android App binding total deadline exceeded`。不能仅凭 wrapper 的字面描述认定 transport 再次故障。
- Session `a82b3e6b-14aa-4a5d-abfd-bae72b02b2d8` 创建成功；DELETE **200 / 581 ms**，`automation/close.json` 为 passed、server/session closed、helper restoration passed。HANS restore passed，临时 debug property 也恢复原值。权限仍是两项 **false → true → false**，cleanup errors 为空，但 permission scope 随 primary 保持 failed。
- fixture close 记录 advertising-inactive、资源退休和 lease release。candidate finalizer 因 sticky `Android candidate authority invalidated` 未执行其 fresh identity/stop，phone 正确保留 quarantine；原 result/phone/manifest 的 failed/quarantined 事实保持不变。

离线运行 `pixi run --locked just react-native formal-host-scan-verify .work/formal-android-managed-scan/c9059a16-ceae-4fc8-ba3a-c125f4329274` 成功重放 **failed** bundle（`.work/scan-scroll-device-preflight/failed-bundle-verify.log`），不把 verifier exit 0 当作场景 pass。

### 时间证据与仍未证明的归因

从原 `automation/appium.log` 按 HTTP response 的服务端 duration 字段统计：**570 条响应累计 97.885 秒**，其中 **15 次 stock scroll 累计 61.229 秒**，单条 response 最大 6.911 秒。这是日志字段的串行累计，不是完整 end-to-end timing trace；没有记录每次 fresh SDK snapshot 的独立耗时，不能把全部剩余时间归给 APK hashing、ADB 或 Python。

源码中 [`AndroidAppBinding`](../../../tools/react_native/bleng_scan_host.py) 仍对每个动作/完成 checkpoint 做完整 13 条 SDK 读命令的正反向 identity sweep；其 SDK guard 又核验本地 candidate/receipts。fresh device identity、remote installed hash、lease/fixture/provenance checks 都未缓存或移除。纯离线对本 run 的 43,374,769-byte frozen APK 连续 20 次本地 hash，median **0.01645 秒**、累计 **0.33597 秒**（`.work/scan-c905-offline-hash-cost.log`）。这只能说明重复校验有可测成本，不能证明它独自导致本轮 240 秒耗尽，更不能以此取消安全检查。下一步应明确区分已知滚动代价、identity/host validation 和场景浏览成本，不盲目扩大预算。

### 已知 quarantine 的独立核对

先只读观察 App/server 已缺席；没有 force-stop、重新 launch 或接管未知实例。原 host PID 已退出、boot 与原 bound identity 一致，独立核对 exact DUT/helper package path/hash/唯一 UID、两项权限 baseline、hidden API keys、mock-location scope/store、instrumentation/Settings services 与 forwarding，并两次核对相关 UID/process 缺席。原 HANS classification restore 使用 sealed run 中已 passed 的实际回执；新鲜读取仅核对 debug property 已恢复，不为了重读分类再次打开 HANS dump 或修改 classification。

恢复中的只读草稿曾因 `pm list packages` 的子串匹配同时包含 `.test` 而被严格断言拒绝；另一份直接 dump 在 debug property 已恢复后只返回 OEM time envelope，因此 `_maps` 拒绝。保留这些失败日志，不把它们伪造为设备/SDK 故障；未发出任何恢复 mutation，也未因此提前归档 lease。最终恢复记录 `.work/scan-scroll-c905-reconciled/result.json` / `.work/scan-scroll-known-reconciled-final.log`：**restored / lease_archived=true / device_mutations=[]**。原 exact owner lock 移入该恢复目录；旧 result、manifest、phone journal 未改写，App 退出原因及 native resource-zero 均不由 PID 缺席推断。恢复不是重试 scan。

### Test-first 修正：wrapped deadline 不等于永久 authority 失效

[`AndroidCandidateOwner.bind`](../../../tools/react_native/bleng_android_candidate.py) 原只对裸 `TimeoutError` 且已达总 deadline 的情况保留独立 cleanup authority；SDK 将真正 `subprocess.TimeoutExpired` 作为 `AndroidSdkError.__cause__` 保存时，原代码错误地把它 sticky invalidated。因此不确定的 scenario 被保留为 failed 是正确的，但本来可以在独立 cleanup budget 下做的 fresh original-identity 检查被过早拒绝。

先加 4 个边界回归，**red：4 tests / 2 failures**，准确复现 `Android candidate authority invalidated`。最小修正只识别 typed SDK timeout cause，且仍要求已到原 scenario deadline；保持原异常/cause/notes 和场景失败，不重试命令、不延长预算。cleanup 仍重新核对原 boot/PID/start_ticks/UID/path/hash 及所有 lease/provenance：early timeout、非 timeout SDK failure 仍 sticky 拒绝；deadline 后换进程仍 quarantine、不得 force-stop 新实例。没有修改 Rust/BLeng、driver 或 firmware。

实际回归 `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_candidate tools.tests.react_native.test_android_sdk tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_android_prepared_scan -q`：**97 passed / 173.908s**，日志 `.work/scan-deadline-cleanup-green.log`；原 red 保留在 `.work/scan-deadline-cleanup-red.log`。这是 host 软件验证，不继承为新一轮设备收尾 pass；没有为此再次运行真机。

进一步实际执行 `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_scan tools.tests.react_native.test_run_bleng_android_scan tools.tests.react_native.test_bleng_scan_evidence -q`：**42 passed / 136.911s**（`.work/scan-deadline-producer-regression.log`）。`pixi run --locked just fmt`、`lint`、`docs-check-mermaid` 与 `git diff --check` 均通过，日志 `.work/scan-deadline-{fmt,lint,docs,diff-check}.log`；editor 仍有 §5 所述解释器及既有 dict 类型收窄诊断，不称 diagnostics 全绿。维护 Python 变更未改 RN/native artifact，因此没有再次 build/install。交付时 phone lease 目录为空，4737/8237 无 listener，共享 SDK daemon 仍为 PID 16841。

## 7. 离线时间成本收敛：紧凑操作区，不削减身份核验

### 分开可测事实与优化假设

进一步按原 run 的每条 stock scroll selector 对应 response duration 拆分，原 **61.229 秒 / 15 次** 中：`next-scan-event` **5 次 / 30.185 秒**，`scan-state` **4 次 / 13.925 秒**，`scan-count` **4 次 / 12.602 秒**；其余是第一次 timeout 控件与 `evidence-run` 揭示。证据 `.work/scan-budget-scroll-baseline.log`。这些是旧 APK 上的真实服务端 duration，不是新布局的性能结果。

另做严格隔离的离线量测：复用现有 candidate tests 的 SDK subprocess fake 和临时 phone lease，所有未知命令拒绝；本地 APK 内容来自原 sealed attempt 的 43,374,769-byte candidate，复制到临时 test candidate，**不向真实 serial 发命令**。对已 bind owner 的一次 fresh identity callback 重复 3 次，得到每次 **13 条 SDK 读、28 次大 APK 内容 hash、896 次全部内容 hash**，host elapsed 分别约 **0.980 / 1.000 / 0.998 秒**（`.work/scan-budget-identity-offline.log`）。这不是手机命令耗时，也不能代表 fixture/driver/live bundle 的完整成本；它证明 guard 嵌套有具体开销，但不授权按 metadata 缓存内容、缓存 device identity 或删除每条 SDK 前后的 owner 检查。本轮不改这些检查、checkpoint 数量或总预算，也不以 fake 速度声称真机能在 240 秒完成。

### 针对已观察往返的最小布局调整

[`ManualScan.tsx`](../../../tests/consumers/react-native-bleng/src/ManualScan.tsx) 原把完整摘要和长说明放在 timeout/主要操作之前，运行状态、计数和 pull 按钮相距较远；样本虽已限高，连续 pull 仍需要反复揭示不同区域。

本次仅在现有 App 中集中 `scan-live-panel`：phase/accepted/error/end/count、timeout、Start/Pull/Stop 与原 160 高度 bounded sample viewport 放在同一紧凑区；主按钮可换行横排、wrapper 可伸缩，按钮位于样本前。caption、资源保留警示、长说明和两个独立 cancel 操作移到 panel 之后，文本和全部行为不删除。panel 明确 `accessible=false`，不聚合吞掉 testID 子节点；没有绝对坐标、固定全屏、裁切错误或改变字体语义。状态/样本/警示只在本文件按职责复用，Current run/Evidence 仍使用同一个 controller 和内容；不创建第二套摘要事实、状态 owner、App 或 JS diagnostics API。仍使用原官方 Appium `mobile: scroll`，host 场景、安全 guard、浏览一致性与错误处理未改。

这是对已证明往返成本的结构性修正，不是原生布局可达性证明：窄屏/字体放大可能换行，完整错误可能增高，后续仍需实际候选验证，不能把新的 React 树内聚直接换算成省下 61 秒。

### 本次实际 red → green / build

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| `pixi run --locked just react-native formal-host-test`，先补长 8 样本 / running 与 failed-end 两类紧凑区、跨页一致性及无额外命令回归 | red：**52 passed / 2 failed**（缺 panel）；实现后 parent delivery **2 suites / 54 passed** | `.work/logs/manual-scan-layout-red.log`、`.work/scan-budget-layout-jest-delivery.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_ui -q` | **90 passed / 2.811s**；原共享按钮场景、guard、错误和 click 不重试保持 | `.work/scan-budget-layout-ui-regression.log` |
| `pixi run --locked just react-native formal-host-android-build release`（含 formal typecheck / Android autolinking） | **通过 / Gradle 1m**，正式 host 无 internal dependency 泄漏 | `.work/scan-budget-layout-release-build.log`、`.work/compact-scan-device-preflight/compact-release-build.log`（下一次构建前保存的原完整日志） |

紧凑区 release APK：**SHA256 `3735ea11160a71ffb64233931d771c3a92438ac0cb01e425f8015ef5ffab9286` / 43,375,593 bytes**。该软件交付阶段没有安装/启动它，也未访问手机/DK；后续仅运行 §8 的一次独立 gate，不修改 radio/firmware 或共享 ADB。未改 Rust、native ABI、useManualScan 状态机或 SDK guard，因此没有为此重建其他 native/package lane。当前两个修改的 RN TSX 文件 diagnostics 无错误/警告；不继承为整个 Python editor diagnostics 全绿。

本轮交付实际运行 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 均通过；日志 `.work/scan-budget-layout-{fmt,lint,docs,diff-check}.log`。修改的两个 TSX、consumer README 和本报告另做 UTF-8、尾随空白、冲突标记/Markdown fences 检查通过；原 sealed `c9059a16…/candidate.apk` hash 仍为 `38cb89d8…`。没有改写旧 bundle，也没有把 targeted UI 回归计成完整 tools/provider discovery。

## 8. 紧凑区真机仍失败；控制区与正文需要分离

### 一次原预算执行，无 quarantine

独立预检 `.work/compact-scan-device-preflight/`：SDK daemon 仍为 PID **16841**、原 SDK executable；phone online、原 App/server PID/forward/lease 为空。doctor 与 probe `001050252028` attestation 串行通过，v2-A identity/hash 未变化。之后使用 §7 的 **3735ea11…** APK、同 phone/DK/contract/profile/helper hashes/ports，四项授权和 root/HANS opt-in 显式传入；原 `timeout=240` / `cleanup_seconds=90`、无过滤、duplicates disabled、最多 8 次显式 pull 均未调整。维护入口仍是 `pixi run --locked just react-native formal-host-android-managed-scan`，外层复用原 `run_quiet.py --no-tty --progress-seconds 20`，不是临时拼另一套 UI flow。日志 `.work/compact-scan-device-gate.log`；正常 **exit 2**，没有外层进程强杀，也没有第二次设备尝试。

原 attempt：**`092fce88-61a0-447c-beba-77c397e3d8ba`**；bundle `.work/formal-android-managed-scan/092fce88-61a0-447c-beba-77c397e3d8ba/`。记录仅到 `idle → started → scan:running`，**没有本 run 的 target-match、stop、closed 或最终 Evidence**。不把 `c9059a16…` 的广播观察继承给这个候选。

最终 primary：`urllib3.exceptions.ReadTimeoutError`，HTTP timeout 为 **0.308570791996317 秒**，来自剩余总预算钳制。最后是 stock `mobile: scroll` 揭示 `scan-samples`；Appium 该 response 只有 `- - ms`，不能算成已正常完成。DELETE **200 / 571 ms**，App/session/server 收尾、helper restoration 与 HANS restore 均 passed；candidate fresh identity/原 App force-stop finalization passed。两项权限仍为 **false → true → false**，`cleanup_errors=[]`、`phone_cleanup_errors=[]`，phone 是随 primary 保留的 **failed**，不是 passed，也不是 quarantine。fixture close/advertising bit clear、物理 lease release 有回执。最终只读核对 App/server 缺席、无活动 phone lease，4737/8237 无 listener，共享 daemon 未重启；不从进程缺席推导 native resource-zero。

`pixi run --locked just react-native formal-host-scan-verify .work/formal-android-managed-scan/092fce88-61a0-447c-beba-77c397e3d8ba`：离线 integrity **passed**，原 status/host gate **failed**、`host_gate_upgraded=false`，日志 `.work/compact-scan-device-preflight/failed-bundle-verify.log`。原 bundle 不改写、不 reseal。

### 这次证据否定了“紧凑就能同屏”的假设

原 Appium 日志有 **593 条带 duration 的 response / 累计 92.587 秒 / 单条最大 3.687 秒**。共 **17 次** stock scroll request，其中 **16 次**有完成 status/duration，累计 **48.689 秒**；最后一次没有可计时完成回执，未混入总和。统计 `.work/compact-scan-device-preflight/http-summary.json`。

反复 selector 明确从旧的 `next-scan-event ↔ scan-state/count` 转为 **`scan-state ↔ scan-samples`**。这证明将所有内容塞进一个紧凑 panel 并不保证真实 accessibility viewport 同屏；不能用 Jest containment 或两个不同 session 的累计时长声称性能修复已通过，也不能把最后 0.309 秒 HTTP budget 当作旧 freezer/service_accept 卡死的复现。完整 SDK/host 时间仍未逐阶段计时，身份检查不据此削减。

### Test-first：小型固定控制区，完整详情继续滚动

在 [`App.tsx`](../../../tests/consumers/react-native-bleng/src/App.tsx) 的 Current run，把纯 `ManualScanControls` 放到 `workbench-content` **外面**，只保留 phase/count、timeout 和三个主按钮；同文件的 `ManualScanDetails` 在正文内显示完整 accepted/error/end、bounded samples、警示、说明及取消按钮。无限长错误和样本不挤占固定区，也不被截短。两者共享原 `useManualScan` controller，owner/action 状态继续在导航之上；Evidence 复用同样的状态/详情组件，不复制另一份事实或 testID。保留 safe-area、可换行主按钮和 accessibility children，没有新框架、坐标、JS API、radio 操作或 BLE 自动动作。

先改结构回归：**54 tests / 5 failed / 49 passed**，具体暴露原 panel 仍位于 ScrollView 内；实现后 **2 suites / 54 passed**。保留长 8 样本、running/failed-end、完整错误、跨页唯一内容、无隐式 scan/stop/cancel/owner 命令断言，并检查控制区位于各 safe-area 情形的正文外；这些仍是 React 结构证明，不是原生尺寸/字号适配证明。

host 只在既有 scan 内容集合中增加 `scan-error`，让固定 failed phase 能通过同一 stock scroll 搜索取得正文里的完整错误；不重放失败 BLE 动作。新增裁剪模型回归覆盖连续 **8 次**显式 pull：正文首次揭示后，固定 status/count/actions 不再引入每次 pull 的滚动；Evidence 仍按真实正文规则查找。另一用例先 **red：真实错误被报为 unavailable**，加上具名 error target 后 green，保留原 error detail、不再发 pull/stop/close。模型的固定位置与 Jest 的实际组件层级对应，但不将模型次数当作真机次数。

### 本次实际验证与新产物

| 命令/范围 | 结果 | 日志 |
| --- | --- | --- |
| `pixi run --locked just react-native formal-host-test` | red 后 **54 passed / 2 suites** | `.work/anchored-scan-layout-red.log`、`.work/anchored-scan-jest-delivery.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_android_prepared_scan -q` | **109 passed / 121.717s** | `.work/anchored-scan-ui-regression.log` |
| 两个新增 host 边界用例先独立执行 | red：**2 tests / 1 failure**，不是设备失败 | `.work/anchored-scan-error-red.log` |
| `pixi run --locked just react-native formal-host-android-build release` | **通过**，含 typecheck / 正式 Android autolinking | `.work/anchored-scan-release-build.log`、`.work/logs/formal-host-android-release-build.log` |

固定控制区 APK：**SHA256 `5fd07609d076b75aa6994445aa3362fe457abaf60f2b23a369bfa32fac3daeca` / 43,375,957 bytes**。该软件交付阶段仅构建；后续设备验证是 §9 的一次独立 RF gate 和一次不同 scope 的 non-RF idle diagnostic，不是第二次 RF。Rust/native ABI、candidate identity/SDK guard、浏览一致性、pull 上限与运行预算均未修改。当前修改的 App/ManualScan TSX diagnostics 无错误/警告；Python editor 仍使用 Xcode interpreter，Appium/Selenium 缺失诊断保持，不以实际 Pixi 测试通过冒充整个 editor 全绿。

交付另实际运行 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid` 和 `git diff --check`，均通过，日志 `.work/anchored-scan-{fmt,lint,docs,diff-check}.log`。格式化后再跑 `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_ui -q`：**92 passed / 2.945s**（`.work/anchored-scan-ui-final.log`）。修改源码/测试/文档的 UTF-8、尾随空白、冲突标记与 Markdown fences 检查通过；旧 sealed compact APK 仍为 `3735ea11…`。未运行完整 tools/provider discovery，不把 targeted 结果计成整体覆盖。

## 9. 固定控制区候选：初始导航校验失败，独立 idle diagnostic 通过

### RF attempt 的新失败发生在 Start 之前

预检 `.work/anchored-scan-device-preflight/`：原 SDK daemon **PID 16841** / executable 未改变，phone online、App/server/forward/lease 为空；doctor 与 probe `001050252028` attestation 串行通过，仍是原 v2-A installed image。随后用 **5fd07609…** APK 与原 exact phone/DK/contract/profile/helper hashes/ports、`timeout=240` / `cleanup_seconds=90`、四项必需授权及 root/HANS opt-in 执行一次 `formal-host-android-managed-scan`。没有改过滤、pull 上限、identity 检查或预算。命令正常 **exit 2**，不是外层 timeout 强杀；日志 `.work/anchored-scan-device-gate.log`。

新 attempt：**`832206de-f3e2-4571-b53f-e9474c6c35ca`**，原 bundle `.work/formal-android-managed-scan/832206de-f3e2-4571-b53f-e9474c6c35ca/`。`observations=[idle]`，唯一 click 是 `workbench-evidence`，返回 **200 / 140 ms**；之后 Evidence 页面校验未通过，直到总预算只剩 **0.05984366699703969 秒**，一次 page 查找报 `ReadTimeoutError`。**没有 Start、scan、目标观察、Stop、Close，也没有任何 scroll request**；因此本 run 不能验证固定控制区在 active scan 中的效果，不能继续称它为前一轮“状态↔样本”滚动问题的复现。

原日志有 **1,541 条带 duration 的 HTTP response / 累计 88.497 秒 / 单条最大 1.778 秒**，元素查找无 404。state/title/page 分别查找约 154/153/153 次；已完成的 page text response 长度始终 **17 bytes**，与 click 前同长。因为 info 日志没有保存 response body、当时没有 PNG/XML，**只能记录长度一致，不能据此声称实际文本一定是 Tests，或认定 onPress 未触发、accessibility cache、覆盖层或渲染故障中的某一个**。HTTP 200 也不能升级为 RN 导航已生效。

Appium session `55d6e5de-fbfa-44fb-a5d4-3cca2e5e0911` 的 DELETE **200 / 520 ms**；candidate fresh identity/原 App force-stop、helper restoration、HANS restore、permission exact restore、fixture close/lease retirement 都完成。outer `cleanup_errors=[]`、`phone_cleanup_errors=[]`，phone 随 primary 保持 **failed**，无 quarantine。SDK daemon 未重启，后续无活动 lease、4737/8237 无 listener。

`formal-host-scan-verify` 对原 bundle 重放成功，保留 **failed** verdict（`.work/anchored-scan-device-preflight/failed-bundle-verify.log`），没有修改/reseal 原始记录。另只读收集原 PID `28671` 的 bounded logcat，仅有可见的启动/系统记录，不能由没有日志断言 JS 没有执行。post-run power/policy 观察为 Awake、interactive、screen on、keyguard showing=false；它不是失败瞬间的 UI/锁屏证明，也没有因此执行 wake/unlock 或全局电源变更。

### Test-first 补齐页面失败上下文，不修改点击语义

原等待只在 timeout 时保留最后一个 HTTP 错误，无法从 sealed result 知道期望页与实际 UI 字段。对 [`_WorkbenchUi.verify_page`](../../../tools/react_native/run_bleng_ui.py) 增加 host-only exception note `formal_ui_page_context`：expected page/state、require_evidence 以及最近**已完成**的 state/title/page/evidence 读值；每值仅保留 160 字符前缀并记录完整 length，未读到为 null。这不是原子 snapshot，不能回填旧 failed bundle 的未知字段，也不是 JS/public diagnostics API。

原 exception 对象、cause、message 和 failure verdict 保留；只附加 bounded note，经原 host exception formatter 进入已有 primary_error。**没有 extra remote I/O、截图、click retry、timeout extension、identity cache 或成功判定放宽**。记录发生在 failure 后的本地路径，authority 丢失时也不为了“补现场”再访问设备。

先加 4 个回归，**red：4 failures**，再最小实现；覆盖 mismatch 的最后实际文本、late HTTP 原对象/cause、authority loss 后无 I/O、超长/Unicode 值有界前缀及真实长度。实际执行：

- `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_android_prepared_scan -q`：**113 passed / 81.922s**（`.work/scan-page-context-green.log`）。
- `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_bleng_ui.PageContextTests -q`：**20 passed / 0.054s**（`.work/scan-page-context-cli-regression.log`）。
- 原 red：`.work/scan-page-context-red.log`。`pixi run --locked just fmt` / `lint` 已通过（`.work/scan-page-context-{fmt,lint}.log`）。Python host-only 变更不改 APK/native artifact，没有为此重建 App。

### 一次独立 non-RF idle control，不拿 RF 等待窗口猜点击原因

复用已维护的 `formal-host-android-managed-lifecycle --automation-idle-control --close-diagnostics`，不是新建 runner 或另一套按钮流程。明确只允许 idle 浏览；**不调用 Start/Close/scan，不打开 DK，不 grant/revoke BLE 权限，不改 radio/firmware**。沿用 same APK/hash、phone、helper hashes、ports、managed install/helper/HANS scopes；`timeout=240`、原默认 `phase_timeout=30`、`cleanup_seconds=90`。开启已有官方 Appium debug/timestamps、bounded live logs/失败现场机制及其恢复；可能包含本地敏感 UI/系统信息，只写 ignored evidence。它是不同假设和责任的诊断，不是把同一 failed RF 自动重跑一次。

实际 attempt：**`f3892c13-f4cc-45e5-ae0f-f7d213d4ca8b`**，目录 `.work/anchored-navigation-control/f3892c13-f4cc-45e5-ae0f-f7d213d4ca8b/`，命令日志 `.work/anchored-navigation-control.log`：**passed / scope=formal-android-managed-idle-control / host_gate=pending / rf=not_run**。完成 `Evidence → Current run → Tests` 两轮，再到 Evidence，共七次导航；debug 日志保存了实际 `Tests` / `Current run` / `Evidence` text response，最后为 Evidence，不是仅从响应长度推测。

session `2c6828bc-2323-4681-9dcc-88950056d906` 的创建/DELETE/server reaping、helper/HANS/debug/log-tag 恢复、candidate retain finalization 和 phone finalization 均通过，cleanup errors 为空。`formal-host-lifecycle-verify` 独立离线重放通过（`.work/anchored-scan-device-preflight/idle-control-verify.log`）。按默认保留 App 在 **idle / Evidence**，原 bound App PID **8130** 的完整身份见其 `candidate/bound-identity.json`；没有创建 BLE owner，也没有停止成功页面。无活动 phone lease、无 Appium/system-port listener，SDK daemon 仍 PID 16841。

这个成功只排除了“同 APK 的 idle 导航必然失败”的判断；不同 session、诊断配置和权限准备 scope 不能合并成因果对照，也不能归为某个已修复根因。**原 RF failed 保持 failed，固定控制区的 active scan 效果与完整 scan chain 仍待验证**。没有第二次 RF attempt，没有修改 App/Rust/driver 来迁就尚未证明的原因。

交付检查实际运行 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 均通过，日志 `.work/scan-page-context-{fmt-delivery,lint-delivery,docs,diff-check}.log`。格式化后 targeted UI discovery **96 passed / 2.980s**（`.work/scan-page-context-ui-final.log`），不与 113/20 项重复合算覆盖数。修改的源码/测试/README/本报告/AGENTS 另做 UTF-8、尾随空白、冲突标记与 Markdown fences 检查通过。editor 仍选择 Xcode Python，报依赖缺失及既有 test fake 类型/导入诊断；实际 Pixi 运行通过，不称整个 editor diagnostics 全绿。没有重新执行完整 tools/provider discovery，也没有 commit/push。

### 导航边界离线审查与点击失败上下文

继续只读审查正式 App 的导航、shared UI 与已有 `AndroidCloseDiagnostics` owner。当前 `App.tsx` 导航 handler 只做 `setPage(title)`，未找到能具体解释 §9 原 session 的确定性逻辑错误；导航与页名也在正文 ScrollView 外，不能继续套用离屏 scan 控件的归因。当前工作树审查不是历史 APK 的源码同一性证明，也不能由 Jest 的 `fireEvent.press` 推出原生触摸命中。暂不加 JS navigation marker、不改布局、不换 APK、不运行设备/RF。

现有 failure PNG/XML 仍由 [`AndroidCloseDiagnostics.capture_failure_ui`](../../../tools/react_native/bleng_android_close_diagnostics.py) 与 lifecycle owner 控制：同一绑定 session、一次捕获、PNG 后 XML、有限预算、authority 检查及 endpoint 不可用时拒绝追加读取。它们不是原子快照。没有把这套机制复制到 shared UI，也没有在 scenario deadline 耗尽后越界补抓；managed scan 尚未因此取得同现场 PNG/XML。已有 `before_close` 机制保持原样。

对 [`_WorkbenchUi.click`](../../../tools/react_native/run_bleng_ui.py) 增加一个可替换的 bounded host-only 最近点击记录。失败 note `formal_ui_click_context` 区分 `waiting` / `prechecks` / `before-close-capture` / `dispatching` / `returned` / `postchecks-passed`；保留 bounded identifier、期望 state/page，以及 SDK click 前后 host monotonic timestamps。`verify_page` 的既有 `formal_ui_page_context` 附带该记录和最近已完成的实际读值；若尚无 click 则为 null。新 click 即使被拒绝也替换旧记录，不会把上一成功动作当成本次 dispatch。

**这些 phase/timestamp 只证明 host 走到哪一步，不能证明设备端请求到达、RN onPress/render、导航完成、Close 结果或 ADB service 健康。** SDK 抛错时仍可能已经投递，`returned` 不代表导航，`postchecks-passed` 仅指既有 post-click guard/可用的 identity callback 已返回；独立页面检查仍必须通过。保留原异常对象/cause/message，只追加本地 note；不新增 remote reads、点击重放、超时放宽、身份缓存、ADB 协议或 UI 框架。

先加 5 个边界回归，在正确的平台 helper 下 **red：9 tests / 4 failures + 2 subtest errors**，再最小实现，**green：9 passed / 0.013s**（`.work/navigation-click-context-{red,green}.log`）。覆盖 Android/iOS click 正常返回但页仍为 Tests、SDK timeout 原 cause/无重试/失败后无 I/O、返回后 identity 丢失、新动作失权不复用旧 context，以及超长标签截断。之后补既有 before-Close capture 抛错的边界，确认未误记为 click dispatch、无后续读取或点击。正式 App 原有 idle browsing Jest 同时加强为 fresh Tests → Evidence → Current run → Tests，每步核对准确页面、内容、唯一 selected 且可用的导航、owner/start/close/beginScan 零调用与 idle scan controls 禁用。

实际验证（各次不重复合计为覆盖率）：

| 命令 | 结果 | ignored 日志 |
| --- | --- | --- |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui tools.tests.react_native.test_bleng_scan_host tools.tests.react_native.test_bleng_android_prepared_scan -q` | 全部新增用例及格式化后 **119 passed / 80.734s** | `.work/navigation-boundary-regression-final.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui -q` | 补齐用例并格式化后 **102 passed / 3.097s** | `.work/navigation-boundary-ui-final.log` |
| `pixi run --locked just react-native formal-host-test` | **54 passed / 2 suites** | `.work/nav-contract-jest.log` |
| `pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck` | **通过** | `.work/nav-contract-typecheck.log` |
| `pixi run --locked just fmt` / `pixi run --locked just lint` | **通过**；首次 fmt 报新增测试 loop closure 未绑定，修正后再通过 | `.work/navigation-boundary-{fmt,lint}.log` |

完整 discovery 实际命令 `pixi run --locked python tools/react_native/run_quiet.py --no-tty --progress-seconds 20 .work/navigation-boundary-tools-test.log -- pixi run --locked just react-native tools-test` 使用已有 wrapper 执行，在 **360 秒外层上限**未结束；日志 `.work/navigation-boundary-tools-test.log` 没有 unittest 最终 summary，**不计为通过**。超时后核对本次残留 `pixi → just → unittest` PID **359 → 434 → 445**、PGID **359**，只对该 attempt process group 发 TERM，随后三 PID 均不存在；没有自动延长预算或重跑完整 suite，也没有触碰共享 ADB。日志中的 PCAP truncated warning 是 synthetic discovery 输出，不是本次新 RF 证据。

交付时再执行 `pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 均通过（`.work/navigation-boundary-{fmt-final,lint-final,docs,diff-check}.log`）；五个修改文件的 UTF-8、尾随空白、冲突标记及 Markdown fences 检查通过。Python editor 仍从 Xcode Python 3.9 查找依赖，报 Appium/Selenium missing；没有向系统解释器安装包，也不以 Pixi passes 声称 editor 全绿。本轮只有 host Python 与 Jest 回归/文档变更，没有 native/API/固件变更、设备 session、成功页面接管或 failed bundle 改写；§9 两次设备 attempt 的原 verdict 保持独立。

### 投递前复验：已证明的软件缺口与受控修正

继续对照 §9 两份原 Appium 日志和两个 runner。两次 `POST /session` 的完整 requested capabilities 相同，cold launch 均由 candidate 的 `am start -W` 执行；idle diagnostic 的 Appium debug/timestamp 日志配置与 scan 的 info 不同，fixture、BLE permission prep、初始读取顺序和等待预算也不同。实际 UiA2 settings 虽有读取，但失败 session 未保存正文、bound-session receipt 也未保存 settings 内容，不能声明有效 settings 完全相同。scan 在首次页面 mismatch 后耗尽剩余总预算、idle 按既有 phase budget 等待，只解释等待形状，不证明最初点击为何未产生期望页。未调整这些配置或预算。

发现一个可独立复现的 shared UI 安全缺口：普通点击先核对 state/page、定位和检查按钮，随后进行 fresh App identity 读取，成功后直接点击先前元素，没有重新核对 UI。identity 证明的是 App 进程/安装身份，不证明这段时间页面或按钮未变化。纯 UI fake 让 identity 正常返回但改变 page/state/foreground、隐藏/禁用按钮或使旧 handle stale，旧实现仍尝试 dispatch。**这证明 host 有复验缺口，不证明这些变化曾在 `832206de…` 发生，也不把它命名为原 RF 根因。**

修正 [`_WorkbenchUi.click`](../../../tools/react_native/run_bleng_ui.py)：在 identity 与可选 `before_close` capture 完成后增加 common `final-ui-checks`。一次核对 foreground、期望 state/page、**原 handle** 的 displayed/enabled；保留 before-Close 原 chosen-id 检查。漂移、missing、stale、transport error、失权或 deadline 耗尽均离开 click，不 refind/retarget、不滚动、不回到 polling、不重放 click。最后一次 enabled 返回后仍有 guard。最终 state 用一次普通文本读，避免 `failed`/`closedWithError` 转入错误详情 polling。全部检查共享原 deadline，新增 UI reads 有成本但不延长预算、不减少原 identity 检查；顺序复验仍不是原子 snapshot，不能保证最后一次检查后绝无 UI 变化。

先新增 3 个组合回归：**red / 15 个 subcase failures**（`.work/predispatch-red.log`）；实现后通过。审查再补 `failed`/`closedWithError`，先复现 **4 个 subcase failures**，再修复其错误详情 polling；最后覆盖 Android/iOS、displayed/enabled 返回时 deadline/authority 丢失、原 SDK timeout/cause 与失败后零 I/O（`.work/predispatch-terminal-state-red.log`、`.work/predispatch-final-green.log`）。既有 scan 测试不再要求 identity 与 click 绝对相邻，改为精确断言两者之间只存在上述最终复验序列，仍要求一次 pre/post identity、总 identity 数不随 polling 增长、每条远程边界有 guard、单次 click。保留原 disabled/stale 预备 polling 和一次 stock scroll 回归，增加最终 enabled 读的精确断言。

扩大验证还暴露了上一轮 click-context 的测试遗漏：3 条 managed lifecycle 回归把 `primary_error` 全文与原单行错误比较，未接受新增 exception note。隔离得到确切 assertion failure 后，仅调整测试为 **原错误首行精确相等 + 完整 before-close context JSON 精确相等**，保留其 ADB/fresh-server 错误、独立 cleanup、quarantine、session DELETE 和 original-App 清理断言；没有移除 note 或改成模糊包含判断。新 helper 不被 live runner 导入。

实际验证：

| 范围／命令 | 结果 | ignored 日志 |
| --- | --- | --- |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui.PredispatchTests -q` | **3 passed / 0.029s**，含组合 subcases | `.work/predispatch-final-green.log` |
| `test_bleng_ui` + `test_bleng_scan_ui` + `test_bleng_scan_host` + `test_bleng_android_prepared_scan`，以 Pixi/Python `unittest.main` 执行 `-v`、stdlib faulthandler 120 秒总上限 | **122 passed / 101.037s** | `.work/predispatch-main-regression-final.log` |
| `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics -v`，独立 bounded wrapper | **62 passed / 4.136s** | `.work/predispatch-close-diagnostics-final.log` |
| managed lifecycle 的 `test_adb_probe_failure_preserves_ui_primary_and_independent_cleanup`、`test_failed_ui_probes_fresh_server_before_delete_without_promoting_verdict`、`test_fresh_server_timeout_preserves_ui_primary_and_cleanup_time`，Pixi unittest `-v` | **3 passed / 41.139s** | `.work/predispatch-lifecycle-primary-final.log` |
| managed lifecycle 的 idle-only、start-hold/preclose、opt-in capture/restore、failed-UI-before-DELETE 四条路径，Pixi unittest `-v` | **4 passed / 43.106s** | `.work/predispatch-lifecycle-paths-final.log` |
| `pixi run --locked just react-native formal-host-test` / `pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck` | **54 Jest passed / 2 suites**；typecheck 通过 | `.work/predispatch-{jest,typecheck}.log` |
| `pixi run --locked just fmt` / `pixi run --locked just lint` | **通过** | `.work/predispatch-{fmt-final,lint-final}.log` |

未将未完成组合计为通过：最初六模块组合在 240 秒外层上限结束，无 unittest summary；核对并 TERM 仅本次 PGID **11740**（PID **11740/11777**），复查无残留。随后拆分 lifecycle/diagnostics，在 175 秒子预算停止：lifecycle 收集 60 项，仅 11 ok、1 已记录 assertion FAIL、1 未完成；diagnostics 尚未开始（`.work/predispatch-lifecycle-diagnostics.log`）。该 FAIL 后来被上述隔离用例定位并修正，不能据此把其余未完成 lifecycle 项补算 passed。第二次 wrapper 只清理 PGID **15095** 并回收。stack 显示前后测试仍在推进，未证明死锁；没有继续延长整组预算。完整 `tools-test` 本轮未重跑，其上一轮超时保持未通过状态。最初 UI/scan 回归还暴露过预期序列及 finite fake readiness 与新增复验不符，修正相关测试后才取得 122 项 pass，原失败日志保留。

交付再跑 `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui -q`：**105 passed / 3.986s**（`.work/predispatch-ui-delivery.log`）；`pixi run --locked just docs-check-mermaid`、`git diff --check` 通过（`.work/predispatch-{docs,diff-check}.log`），六个修改文件的 UTF-8、尾随空白、冲突标记及 Markdown fences 检查通过。

本轮未访问设备、创建新 Appium session、重建/安装 APK、退休 retained App、修改权限/radio/HANS/固件或改写 sealed bundle。只修 host 点击安全边界及相关软件回归，不等于原导航故障或整条 scan gate 已修复确认。Python editor 的 Xcode interpreter 依赖缺失仍存在，实际 Pixi passes 不替代 editor 全绿。

### Release 导航标记与同 session non-RF 正向控制

#### 为什么标准 click 成功仍不足以定位 JS 边界

只读复核本地 UiA2 **10.7.6 / revision `31de0505539af35602cbe62519d95702702b8195`** 的两个下载 manifest/source snapshots（`.work/authorized-device-observer-uia2-sources/`、`.work/managed-idle-uia2-wakelock-source/`），13 个 source hash 均匹配。两处并无完整 click Java 源码，因而补用已安装 SDK `dexdump` 对与原 session receipt 同 SHA256 **`b0dea678…`** 的 server APK 做离线反汇编，不能将 debug positions 冒充本地原文源文件。

原 `WebElement.click()` 路径是 driver `lib/commands/element.ts` → servlet `handler.Click` → `AndroidElement.click` / `BaseElement` → `AxNodeInfoHelper.click` → gesture controller 根据元素 bounds 中心注入输入，随后 `Device.waitForIdle()` 返回再回应成功。`utils-Device.java:68–85` 的既有原文显示 idle timeout 可为零且异常被记录后容忍；没有 RN handler、Hermes 或 React commit ACK。APK 中这条注入路径也没有把 `injectInputEvent(..., false)` 的 boolean 转成目标 handler 成功判据。**标准 click 并非简单的请求已接收，但其 200 仍不能证明 RN 导航成功；`200 / 140 ms` 中 200 是状态码。** 未改用坐标、accessibility action 或另一种 click API 来假装增加保证。

已有页面读值/PNG/XML 只能观察结果，不能单独区分 handler 未进入与请求后未提交。因此在正式 consumer [`App.tsx`](../../../tests/consumers/react-native-bleng/src/App.tsx) 加极小的私有 `BLEHUB_FORMAL_NAV`：handler 首条诊断为 `phase=request`，既有 page state 的 `[page]` React effect 发 `phase=commit`。只含固定 page/phase 和 JS monotonic time，不新增 owner、命令、导航 state/ledger、JS/public API、重试或结果判据。try/catch 隔离诊断 sink；不在 render 函数体中写日志。React effect 语义用官方 [useEffect 文档](https://react.dev/reference/react/useEffect) 核对；它不是原生画面或 accessibility ACK。同页 press 可只有 request，batch 可合并提交，Strict Mode 可重复 effect；缺日志或初始 Tests commit 未被捕获不能证明 handler 未进入。日志有调度成本，不把新旧 APK/session 当作无扰动因果对照。

先加 3 个 Jest 组合回归，**red：3 failed / 54 passed**，实现后 **57 passed / 2 suites**（`.work/navigation-js-marker-{red,green}.log`）。覆盖 request/commit 顺序、同页 press 不捏造 commit、idle 零 owner/BLE，以及 request 或 commit sink 抛错后导航、pending Start、原 owner 和 settlement 不受影响。原 Close phase 顺序断言只筛选原 Close prefix，仍精确核对全部原 phase；没有因新增日志丢弃其断言。typecheck、正式 Android autolinking 通过，实际执行：

- `pixi run --locked just react-native formal-host-test`
- `pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck`
- `pixi run --locked just --no-deps react-native formal-host-check`
- `pixi run --locked timeout --kill-after=10s 180s just --no-deps react-native formal-host-android-build release`

构建通过（`.work/navigation-js-marker-build.log`、`.work/logs/formal-host-android-release-build.log`）。新 APK **SHA256 `cf10697246fd4a7d141182a295dd378865637ac16c9c06d25d540cd3b1814a9c` / 43,376,305 bytes**；不是原 `5fd07609…` 候选，不替换原 failed evidence。

#### 精确退休已知旧 idle App，而不是接管

预检原 SDK daemon **PID 16841** 仍监听 5037，4737/8237 和 phone lease 为空。先离线验证旧 `f3892c13…` passed idle-control bundle，再在新 App-only phone lease 下用既有 `android_sdk.read_app_identity(expected_identity=…)` 反复核对原 **PID 8130 / UID 10378 / start ticks 242619909 / 原 boot/APK/path/cmdline**、单 user 0 与非 shared UID。只有 exact match 才通过既有 bounded SDK argv 边界执行一次 `am force-stop --user 0`，随后严格核对整个 UID/package process absence 与同 boot，未改 radio。身份若不符本路径会拒绝，不收养新实例。证据 `.work/navigation-marker-known-retirement-8eef9bbf-fcc3-4e3f-8a28-24ce616f5293/`，phone **passed**、cleanup errors 为空，日志 `.work/navigation-marker-retirement.log`。这是本次明确退休，不把旧成功页误称仍保留。

#### 一次现有 idle-control，证明 release 通道可用

复用维护入口 `formal-host-android-managed-lifecycle --automation-idle-control --close-diagnostics`，新 candidate、原 exact phone/helper hashes/Appium 端口，`timeout=240 / phase_timeout=30 / cleanup_seconds=90`，显式 install/helper/root/HANS 授权。没有新 runner/collector，也没有 Start、Close、scan、DK、BLE permission grant/revoke、radio 或固件动作。完整命令由原 Just/Python CLI 执行，日志 `.work/navigation-marker-idle-control.log`。

新 attempt **`718b8c00-864e-412d-8317-ca3fc4a49c9e`**：`.work/navigation-marker-idle-control/718b8c00-864e-412d-8317-ca3fc4a49c9e/`，结果 **passed / scope=formal-android-managed-idle-control / host_gate=pending / rf=not_run / observations=[idle]**。同一 App PID **25327** 的 live logcat 收到 **14 条导航 marker / 七组 request→commit**，顺序为 `Evidence → Current run → Tests` 两轮，再到 Evidence；Appium debug 同 session 保存了对应实际 page text，不是只凭响应长度或日志 marker 判为通过。首次 request/commit 的 JS 同时钟差约 **7.447 ms**；这不是 click-to-frame 延迟、host/device 跨时钟测量或性能门槛。live capture 在 App 启动/identity 绑定后才开始，未收集到初始 Tests commit；没有靠缺少该行推断未执行。

Appium session **`4f8c5528-5d57-4de1-a925-503ccb7ae228`** DELETE **200 / 596 ms**。candidate retain、helper/HANS/debug/native-log-tag restoration、live capture reap、phone finalization 都通过，primary/cleanup errors 为空。实际执行 `pixi run --locked just react-native formal-host-lifecycle-verify .work/navigation-marker-idle-control/718b8c00-864e-412d-8317-ca3fc4a49c9e`，离线 integrity **passed**，`host_gate_upgraded=false`（`.work/navigation-marker-idle-offline-verify.log`）。postflight 无 active phone lease、4737/8237 无 listener，原 ADB daemon PID 未变。

按默认保留新 App **idle / Evidence**，bound identity：**PID 25327 / UID 10378 / start ticks 243249699 / boot `35df0f9f-6c54-42b0-800c-3c410469b6e7` / APK `cf106972…`**，完整 base.apk path 在本 attempt `candidate/bound-identity.json`；后续不得普通 run 接管。这个 pass 只证明该候选下的新诊断信号和七次 idle 导航可用；没有复现原 `832206de…`，不证明其根因、不证明 active scan/Stop/Close，也不把 §9 不同候选/session 的记录拼成完整通过。

交付检查：`pixi run --locked just fmt`、`pixi run --locked just lint`、`pixi run --locked just docs-check-mermaid`、`git diff --check` 通过（`.work/navigation-marker-{fmt,lint,docs,diff-check}.log`）。再次运行 `pixi run --locked just react-native formal-host-test`：**57 passed / 2 suites**，typecheck 通过；`pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_ui tools.tests.react_native.test_bleng_scan_ui -q`：**105 passed / 3.918s**（`.work/navigation-marker-{jest-final,typecheck-final,ui-regression}.log`）。修改的五个 authored 文件通过 UTF-8、尾随空白、冲突标记和 fences 检查，`App.tsx` editor diagnostics 无错误/警告。没有重跑此前未完成的完整 tools/lifecycle discovery，也没有将这些 targeted 软件结果计成全套覆盖。

## 10. 下一道实际边界

完整 `scan → stop → close → final Evidence` 仍无 pass。最新一次失败停在初始导航，独立 idle-only 控制已通过；已补齐实际读值/host 点击 context、修正可离线证明的投递前复验缺口，并取得新 release 候选导航 request/commit 的同 session 正向控制；首次导航故障本身仍未复现定位，下一步要在保留这些观测的现场区分 JS request、post-commit 与实际页面，而非继续用 click 200 推断，而不是继续改布局、换点击机制、放宽条件或盲跑 full RF。后续任何 fresh RF 仍须 exact identity/lease/readiness、原预算和完整 cleanup；当前 retained idle App 不可被普通 run 接管，需按其原 bound identity 显式处理。所有 failed/pending 与不同 scope 的 pass 保持独立。

正式 connect/GATT/I/O/notification、Hermes reload、原 full-suite 及各独立 fault/permission/radio gate/ledger 的迁移仍在后面。保留 internal 的独有回归和失败证据，不因本轮入口或测试计数删除它，也不把阶段性软件收敛当作最终功能/人工 review/发布验收。
