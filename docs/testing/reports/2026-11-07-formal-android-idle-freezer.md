# Formal Android：真实 idle 复现、外部冻结证据与采集器修正

> 后续进展：[单 UID HANS 豁免与原 lifecycle 链通过](2026-11-07-formal-android-hans-lifecycle.md)。
> 本报告保留修正前阶段的原始失败和当时结论；后续通过不改写以下历史 verdict。

用户本轮明确授权执行设备检查、相关工具升级评估和必要软件重构。本报告接续
[USB 写边界](2026-11-07-formal-android-usb-write-boundary.md)与
[生态复用](2026-11-07-formal-appium-ecosystem-reuse.md)。

**已证实原身份 UiAutomator2 server 进程被冻结；主 BUG 尚未修复。**
未点击业务 Start/Close、未创建业务 BLeng owner、未做 BLE scan/RF，也未改 Rust Close、locator、
BLE 权限、radio、固件或全局 freezer/防火墙。五份原场景均保留 failed；独立恢复不改写原 verdict。

## 1. 工具候选：最新不等于稳定兼容

核对 registry 后，Appium 最新稳定 server 为 3.9.0，但 UiAutomator2 9.0.1 要求
`Appium ^4.0.0-beta.6`。没有强制 peer dependency，也没有为追 latest 引入 beta server。
选用稳定兼容组合 Appium 3.9.0 / UiA2 8.7.3 / server APK 10.7.6 / Settings 8.4.5；
Python Client 仍是前轮已锁定的 6.1.5。

新增 `tools/appium/android/{package.json,package-lock.json,.npmrc,selection.json}` 和
`tools/appium_toolchain.py`，使用官方 npm 安装、官方 driver discovery，不 fork driver。

> 2026-11-13 布局更新：输入随 iOS 候选一同归入 `tools/appium/<platform>/`，字节未变，
> 已装 home 与 receipt 继续有效。见[iOS 设备 lane 报告](2026-11-13-formal-ios-device-lane.md)。
显式安装入口为 `pixi run just react-native appium-android-install`；普通测试不安装。
工具 root 遵循 `BLEHUB_APPIUM_ROOT` / 本地配置 / managed-cache 约定。本机显式选定：

```text
BLEHUB_APPIUM_ROOT=/Users/vision/projects/blehub/.work/toolchains/appium
home=android-3.9.0-uia2-8.7.3-06f2fb41c72a
entry=<home>/node_modules/appium/index.js
```

这是隔离重装和来源锁定，不是 server/driver 版本提升。旧 `/Users/vision/.appium` 和旧 npx entry
保留不动；没有升级 iOS driver。三份 helper SHA-256 与旧候选完全相同，精确版本、来源和哈希见
`tools/appium/selection.json`，安装收据位于候选 `installed.json`。

官方 doctor 为 0 个必需修复，独立短时 `/status` ready，prepared caps 的软件验证通过；均不是
设备行为证明。npm 锁解析超时和首次 optional-dependency 锁不完整导致的 `npm ci` 失败保留在
`.work/logs/appium-android-*`，补齐锁后安装成功，没有强制忽略 peer/engine 约束。

## 2. 实际运行矩阵

全部使用手机 `3ac5cc09`、同 boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`、同 APK
`51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef`，以及上述 managed
工具候选。Appium UI 全程仍由官方 Python Client/server/stock UiA2 执行。
共享 SDK ADB daemon PID 16841 保留，没有重启 daemon、切换 USB backend 或关闭其他 host client。

| run | 唯一需要区分的条件 | 观察 / 原结果 |
| --- | --- | --- |
| `89724fa1-b20a-415a-bed5-e77379e1f05c` | 首次用 managed 候选验证修正后的 collector | collector ready；`[idle]` 后 click timeout；原 failed/quarantined |
| `ae4d11a0-390c-46a9-b246-90cd4fd1ba49` | 显式 server deviceidle 豁免候选 | ready 前超时，`[] / not_run`；DELETE/helper/power 恢复通过，但 census exit 124 导致原 quarantine |
| `c45b0100-c48c-437a-951f-ca7f4dfc8719` | 采集器批量优化后、不加省电豁免的对照 | ready；`[idle]` 后 UI timeout；原 failed/quarantined |
| `39821471-fc01-4ab9-babd-c014602ee7a7` | 相同批量 collector，加标准 server deviceidle 豁免 | `[idle]` 后 text timeout；ADB probe 通过、collector 完整 finished；原 session/host failed/quarantined |
| `a4c8193f-919c-4812-8d01-324190bae017` | 不加 deviceidle 豁免，单次官方 `unfreeze --sticky` 原 PID | 命令 exit 0，事后原身份核验通过；仍 UI timeout，恢复前原 server 再次处于 freezer trap；原 failed/quarantined |

Bundle 均位于 `.work/formal-android-managed-lifecycle/<run>/`。没有自动重复同配置的业务场景，
也没有在准备失败后自动启动 Start/Close。`ae4d…` 不计为省电策略的有效业务对照。

## 3. 第一份真正覆盖 idle 故障的设备 PCAP

`89724fa1…` 的设备目录、nonce、metadata/hash、原 collector roots 和 owned FD 已核对后，
无信号取回本地原字节：

```text
.work/device-local-managed-capture-retrieval-576dee9c-ec9d-4bc9-823c-fdbe263d95e3/artifacts/
capture.pcap: 113017 bytes / 610 records / 0 snapshot truncated
SHA256=d6a8a01f3d382e0a67eced87eb3026d98dbae14e2a4088771a99efb03b8bc5b6
```

`pcap.stderr` 报 0 kernel drops。以下均为设备时钟 UTC+8，packet index 从 1 开始；不与 host
Appium UTC 或 host monotonic 直接相减：

- 实际首末包 17:09:00.850754 → 17:10:15.506798。
- 主连接 `127.0.0.1:42869 ↔ 127.0.0.1:6790`；`workbench-current` 查找、displayed、enabled 均有 200。
- #564，17:09:40.253058：完整 374-byte click 请求出现在设备 loopback，原 device session
  `fbb7714c-20af-42b1-86f4-7ed640754c9b`，element `…0000000d`。
- 请求 `seq=594227191`，完整确认应为 `ack=594227565`；最后观察到的 ACK 仅为请求前的
  `594227191`，没有确认新增字节。随后相同序号的重传不是多次业务 click。
- 所有 6790→client 的最后包是 #562，17:09:39.795066；其后三条旧连接也不再获应答，新流只有
  SYN 重传，没有 SYN-ACK。没有服务端 HTTP response。

这排除了“主 click 字节完全没到设备抓包点”，但抓包点出现报文不等于接收端 TCP 已接受，更不等于
Java/Netty handler 已读取。后置 shell 不在 TCP 6790 capture 范围内，仍是独立的 service_accept 超时。

## 4. 冻结证据：从相关线索推进到原进程栈

### 第一份同窗关联

恢复 `e78e15c4-2581-4083-8225-a94af8cd37cc` 的原 server 为 PID 28356 / UID 10255。
恢复目录中的 `013-kernel-history-before-retirement.json`，下述 L 是其 `stdout.splitlines()` 行号：

- L1230 与 L1542 的 UTC anchors 覆盖本次窗口；两锚换算偏移一致到亚微秒级。
- L1369：`hans_handler: --> FROZEN_TRANS, uid = 10255, frozen_check_type = 0`，对应设备约
  17:09:41.026597，紧邻上述报文停顿。
- `008-power-before-retirement.json` stdout L11687–11688：本次 ScreenKeeper 从
  17:08:49.497 ACQ 到 17:09:40.024 REL。REL 落在最后应答与 click 之间，但日志不能独立解释释放原因。
- `012-network-readonly-before-retirement.json` 有 OEM HANS BPF ingress/egress DROP 链；没有
  命中计数或 UID map，不能仅凭常驻规则证明本包被丢弃。

### 标准 deviceidle 豁免后的直接进程证据

`39821471…` 的网络与 CPU 边界不相同：

- 原 server PID 25839 / UID 10255；device session `228fcaed-eaad-46bf-aa90-3df0711ec737`。
- 本 run 的 `device-local/capture.pcap` 为 132611 bytes / 858 records / 0 截断、0 kernel drops；
  SHA-256 `d4f91270a7d2866ebaddbbd3baa951e84616dad37d3d362f3d74b7f43165d5db`。
- 主流 `37157 ↔ 6790`，#608，19:20:07.182966：302-byte text 请求；#609 的
  `ack=2869720366` 完整确认。随后 30.724 秒窗口内无 HTTP response，TCP keepalive 仍有回复。
- 新流 `43849 ↔ 6790` 完成握手，#765 的完整 `GET /status` 获 #766 ACK，但没有 HTTP response。
- 原 PID 的后置 logcat 成功取得 407 行，最后是更早的 displayed response，没有故障 text 或
  `/status` 的 `channel read`；不把缺日志单独当作未进入 handler 的证明。
- **退休前的原身份进程快照**在独立恢复目录
  `.work/formal-android-managed-recovery-fad70c15-b099-4bb8-8f69-39d87abd5ae0/`：
  `013-server-proc-before-retirement.json` 的主线程 wchan/stack 显示 `do_freezer_trap`；
  `014-server-threads-before-retirement.json` 中全部 **29 个线程**均在 `do_freezer_trap`。

因此，当前候选有“TCP 可接受数据，但原 UiA2 用户态被冻结，不能继续处理”的直接解释；不是
GetText 内部死锁的证据。它不自动证明每一个历史 session 都具有相同冻结状态。

### 官方 sticky 命令也未解决 OEM 路径

手机 `cmd activity help` 明确支持 `unfreeze --sticky <PROCESS>`；AOSP 对应实现保证所选进程
记录的普通 cached-app refreeze 行为，不保证 OEM/强制 freezer 路径服从。
`a4c8193f…` 的 `server-freezer-result.json` 保留命令 exit 0、原 PID 3170 的前后身份匹配，以及
`effect=unverified / cleanup=not-claimed`。恢复目录
`.work/formal-android-managed-recovery-74dd1f55-8977-4f50-afc7-ebf5baea4f66/` 的
`014-server-freezer-before-retirement.json` 再次记录原 PID 全部线程在 `do_freezer_trap`。

不能把命令成功称为 OEM 豁免成功；本轮不再继续试不同 timeout 或循环解冻。

## 5. 本机 root 模块背景及尚未进行的环境修正

只读模块清单发现已启用的 `/data/adb/modules/murongruyan`，名称为
“ColorOS移除温控并开启Color墓碑完全体”，自报 4.2。实际脚本副本在
`.work/freezer-module-source-5b72a95d-029b-43a9-8f5c-89d8155b1694/`：

- `service.sh` L23–28 初始化 freezer v2；
- `post-fs-data.sh` 将模块内配置 bind 到 `/data/oplus/os/bpm/sys_elsa_config_list.xml`；
- 模块还改变温控及 CPU 限制，不能把整个模块当作仅有一个 freezer 开关。

实际 Elsa XML 的只读副本保存在上述 `74dd…` 恢复目录的
`015-oem-elsa-config-readonly.json`，含启用的 HANS、cgroup v2、fast-freeze，以及 per-package
配置结构。**这些证明本机存在非标准冻结配置，不证明该模块是唯一责任来源，也没有验证
某个 XML 节点的完整豁免语义和热加载/恢复协议。**

按模块自身 updateJson 定点核对的[更新记录](https://raw.githubusercontent.com/murongruyan/murongruyan/main/changelog.txt)
仍为 4.2；[作者 README](https://github.com/murongruyan/murongruyan/blob/main/README.md)
明确说明墓碑与温控 Hook 的捆绑及高风险，所查材料没有给出已验证的 per-helper 豁免/热加载协议。
未下载整个仓库或模块包，也没有以“升级”名义替换该模块。

本轮没有编辑该模块、厂商 XML、thermal 配置或全局 freezer。后续环境修正应只针对自动化 helper
建立受支持、可读回的 OEM/module 定向豁免；不能凭字段名猜测配置、通过停整个温控模块试错，
或在 BleHub/BLeng 内补心跳来规避外部冻结。当前标准 deviceidle 和官方 sticky 两种候选均已证实
不足以消除本机现象，不作为默认修复启用。

## 6. 已修正的项目软件边界

### 可复现工具链

`tools/appium_toolchain.py`、精确 npm lock 和一个显式 Just install 入口；普通运行不安装或升级。

### 采集器准备/收尾性能

`tools/react_native/bleng_android_device_capture.py`：

- owner guard 合为一次 SDK 往返，保留 boot、目录、nonce、文件类型/权限/链接数/大小及前后
  metadata/hash 校验，避免独立 4 秒 ready 窗口被多次启动 SDK 客户端耗尽。
- 原逐进程 cat/ls/readlink census 在真实 905 PID 上 4.140 秒 timeout；不用延长预算掩盖。
- 改为批量 `ps`、shell builtin 的独立 proc 检查、原生 `find` 与有界 awk reducers，强制 pipefail；
  不新增自制原生 helper、通用框架或设备安装。
- 保留 PGID/owned-FD 检测、4096 PID、每 PID 8192 FD、总 131072 FD 上限、显式权限/读取错误
  拒绝，以及删除前两次 quiet 确认。4 秒 ready、5 秒单命令预算未放宽。
- 真实手机只读性能 probe：906 PID，exit 0、`complete`、**1.028 秒**，证据
  `.work/collector-native-census-probe-37cfa7ee-7e9e-41d1-82ac-ebfe3509e659/`。
- `39821471…` 的真实完整 collector finished：文件取回、三个 quiet checks、payload/token 删除均有回执。

枚举是非原子的：FD 在 find stat 前消失的 ENOENT 可能省略，Toybox 未报告的中途 readdir 错误不能
证明不存在；旧 ls 同样使用该 traversal，不具有更强保证。`complete` 只表示本次可观察检查完成，
不是原子或绝对完整内核快照。这一 gap 明确保留，不以软件测试或零 drop 声称不存在。

### 显式、默认关闭的诊断候选

- `--authorize-server-power-exemption`：只为 server 临时添加标准 deviceidle user 项，保存完整
  user baseline 并仅撤销本次添加。未知提交或恢复/漂移失败阻止放租，不覆盖其他包。
- `--authorize-server-freezer-exemption`：绑定原 PID/UID/start/APK/boot 和 lease，单次官方
  `cmd activity unfreeze --sticky`，无全局设置、无后台循环、无重放；正常 finalize 的原进程
  absent 检查和未知 session quarantine 不变。

它们是保留证据与安全回归的诊断入口，不是已通过的本机 HANS workaround，均不默认启用。
相关责任分别在 preparation/session owner，runner/CLI 只显式转交授权。

## 7. 恢复、安全状态与验证

全部原 run 的 quarantine 均已独立恢复并归档 exact phone lease：

| source run | 成功恢复 evidence ID |
| --- | --- |
| `89724fa1…` | `e78e15c4-2581-4083-8225-a94af8cd37cc` |
| `ae4d11a0…` | `662bf47a-ca09-4f68-bdde-d812ebc570c4`（device-local-power-idle-final-recovery） |
| `c45b0100…` | `edab9d54-0442-47bf-b018-9ec5ef688e59` |
| `39821471…` | `fad70c15-b099-4bb8-8f69-39d87abd5ae0` |
| `a4c8193f…` | `74dd1f55-8977-4f50-afc7-ebf5baea4f66` |

取回前核对原 boot、lease inode/owner、目录/nonce、collector absent/FD 及原文件 metadata/hash。
未知 DELETE 不改成成功；只退休核验过的原 instrumentation。`398…` 的 App 已由原 candidate
正常收尾，独立恢复验证 finalization、包/hash 和进程缺席，没有对未知 App 重发 force-stop。
helper、临时 user 白名单、日志标签和 forward 回到原 baseline；当前无活动 phone lease。
不宣称 graceful BLeng Close 或 native resource-zero，原五份 bundle 仍 failed/quarantined。

失败的恢复适配尝试同样保留：一次 legacy helper 的参数标签不匹配导致 `adb: unknown command ps`；
一次 source 已正常退休 App，旧 recovery 的“App 必须仍在”guard 拒绝；随后一次遗漏冻结的本地
finalization source 导致 KeyError。这些失败均未放宽身份、未释放旧 lease；按实际 source facts
修正独立恢复后完成。`c45…` 首次只读 boot 仍超时，保留失败；未重跑场景或重启 daemon。

本轮实际软件验证：

- preparation/CLI/device-capture/evidence/toolchain/Android/mobile/iOS Appium 八模块合并：
  `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_preparation tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_bleng_android_device_capture tools.tests.react_native.test_bleng_device_local_evidence tools.tests.react_native.test_appium_toolchain tools.tests.react_native.test_android_appium tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_ios_appium`：
  **217 passed / 55.803s**，`.work/managed-appium-fix-final-targeted.log`。
- session 的七个 `test_server_freezer_*`：**7 passed / 5.716s**；测试实际 SDK 命令边界的 one-shot、
  原身份、post-guard/publication 失败及 cleanup，不能替代真实 OEM 效果。
- 另有 runner、toolchain、collector 的定向 red→green，以及正式 host typecheck / Jest
  **2 suites / 52 tests**、host-check 通过；日志 `.work/logs/server-power-*`、
  `.work/managed-appium-fix-{host-check,formal-typecheck,formal-jest}.log`。
- 五份原 bundle 经 `verify_lifecycle_bundle` 离线验证无异常返回，原 failed/quarantined 保留；
  `.work/managed-appium-final-integrity.log`，没有提升 verdict。
- `pixi run --locked just fmt`、`pixi run --locked just lint` 通过；最终日志
  `.work/managed-appium-fix-final-{fmt,lint}.log`。

广域工具 discovery、完整 lifecycle/session 检查仍有超时，不能计为通过：工具链阶段 tools-test
180s；power 阶段 tools-test 900s / lifecycle 180s；collector 组合 120s；session 整模块 60s。
停止并清理各次自有测试链，没有用延长预算掩盖。它们与真实冻结证据分开，不无证据归因于 SDK 升级。

交付前 `pixi run --locked just fmt`、`pixi run --locked just lint`、
`pixi run --locked just docs-check-mermaid`、`git diff --check` 均通过，日志
`.work/managed-appium-delivery-{fmt,lint,docs,diff-check}.log`。最终本地检查确认无活动 phone lease、
4737/8237 无 listener、原 5037 daemon PID 16841 保持。

当前交付是更可靠的取证基础和明确的原进程冻结证据，**不是已修复的 lifecycle、RF、ABI 或 Phase exit**。
