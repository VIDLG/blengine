# Formal Android：离线拆解 Close、Appium 排队与 ADB service 接受边界

本次只读取既有证据、本机已安装依赖源码并运行软件测试；没有执行新的真机、ADB、Appium server、
USB、radio、权限或固件操作。没有重放 lifecycle。原始运行及独立恢复见
[前序报告 §20](2026-11-05-formal-android-preclose-evidence.md#20-2026-11-07保留-qtscrcpyclose-已返回最终-evidence-点击仍超时)。
原 sealed failed/quarantined/not_run 不变；本次不产生新的设备清理或平台通过结论。

## 1. 原始时间线：不是同一层的一个 Close timeout

证据根目录为 ignored 本地
`.work/formal-android-managed-lifecycle/e9c6c064-0019-4cf7-a5f1-75418b8885f2/`。
以下行号分别相对于 `automation/appium.log`、`platform/close-diagnostics/live-logcat.raw`。

| 时钟域 / 时间 | 原始观察 |
| --- | --- |
| 设备 logcat `11-07 00:33:29.927` | raw L668–669：`hybrid.close.promise.resolve.enter/return`；此前 Rust closeResult 也返回。 |
| Appium 日志 `11-06 16:33:29.644` | L1669–1671：Close click 收到下游 200 并向客户端返回 200。 |
| Appium `16:33:32.201`、`34.724`、`35.559` | L1698、1727、1802：原 session 的文本查询实际返回 `closed`。 |
| Appium `16:33:36.014` | L1848–1850：Evidence 元素 enabled 查询 200。 |
| Appium `16:33:36.113` | L1851–1856：`mobile: getCurrentPackage` 的 ADB `dumpsys window displays` 已返回原 App package。 |
| Appium `16:33:38.246` | L1857–1860：最终 Evidence click 转发到原 device session；未见对应下游 response。 |
| Appium `16:33:58.248` | L1861：客户端 20 秒窗口结束，日志为 `- - ms - -`，不是 HTTP 200 或远端取消回执。 |
| Appium `16:34:03.349` → `08.350` | L1862–1864：失败截图进入 host 命令入口后，客户端 5 秒超时；此窗口未见截图 proxy dispatch。 |
| Appium `16:34:18.435` | L1865–1871：DELETE 到达并转发；也未取得完成回执。 |
| Appium `16:34:23.439` | L1873–1881：本地 SIGTERM / session shutdown 中，才出现此前截图的 `Matched` / `Proxying`。 |

设备墙钟与 Appium 日志相差约八小时，并有亚秒级偏移；不能直接相减得出单向传输耗时。
上表分别保留原始时钟域；只用各域内顺序、同请求/元素/session 关联。
Close click 的 200、native promise 返回和 JS/UI `closed` 也是三个不同边界，不混成一个原子完成。

raw L749–751 最后看到 Evidence enabled 的入口、handler 和 response；没有最后 Evidence click 的
`channel read` / `Click command`。L752 还有 ActivityManager warning，但没有充电 overlay 的证据。
`live-stop.json` 最后一次 host 字节到达为 monotonic `24130.814445208`，raw 共 94041 bytes。
这不能证明最后 click 未到设备，也不能证明整条 transport 立即完全停传。

## 2. Fresh status 与 ADB 探针：只确定观测窗口

这部分直接使用同一 host monotonic 时钟，不从 Appium/设备墙钟换算：

| 回执 / 阶段 | started → finished monotonic | 观察 |
| --- | --- | --- |
| `platform/server-status.json` | `24149.388298291 → 24151.399045208` | fresh socket 直连 `8237/status`，绕开 Appium command queue，超时。 |
| `adb-transport.json` / server_version | `24151.407076375 → 24151.413613291` | `OKAY00040029`。 |
| cached_state | `24151.413618625 → 24151.419963750` | `OKAY0006device`，仅 daemon 缓存。 |
| transport_select | `24151.419970875 → 24151.425983250` | `OKAY`，选择原 serial。 |
| service_accept | `24151.425986125 → 24154.409362708` | 新 `shell:cat .../boot_id` 等待接受，未读到任何响应 bytes，超时；boot output 阶段未进入。 |
| `failure-ui-failed.json` / screenshot SDK read | `24154.459646708 → 24159.502200375` | 原 Appium SDK 路径超时，无 raw PNG/XML。 |

因此 `service_accept` 的失效**在 click 客户端超时之后才被测量到**；不能倒推出它何时开始，
或声称 click 导致 ADB service 不接受。cached state / transport OKAY 也不等于设备新服务能应答。
既有证据不足以区分 host USB/backend、链路、adbd、设备调度/服务创建中的哪一项。

## 3. 已确认的软件边界：诊断截图也在排队

本机 driver 8.7.3、server 10.7.6 的核对基础见
[前序源码核对](2026-11-05-formal-android-preclose-evidence.md#16-2026-11-06按实际-uiautomator2-版本核对使用方式)。
本次另外读取实际安装的 driver 所带 `@appium/base-driver` 10.8.2
（`gitHead=2c2820242ffdbc450025d4963cff7b17a7e5a7d3`）：

- `lib/basedriver/driver.ts::executeCommand` 用 `commandsQueueGuard.acquire` 串行执行命令；
  SDK 客户端 read timeout 不会自动删除该队列里的命令。
- `lib/jsonwp-proxy/proxy.ts::proxy` 有独立 downstream timeout；`proxyReqRes` 等待该 promise。
  Python SDK timeout 与下游 proxy timeout 不是同一预算，更不是设备操作取消。
- `lib/uiautomator2-server/core.ts` 的 proxy 使用 keep-alive；只有显式 readTimeout 才覆盖默认值。
  当前 prepared capabilities 未设置 `uiautomator2ServerReadTimeout`；安装版本 README 的默认值是
  240000ms，不能把客户端 20 秒超时解读成这层已完成。
- 设备 server 的 fresh `/status` 不是 Appium 的串行命令队列；它失败仍包含 server/forward/ADB
  共享依赖。不能用 host queue 解释全部 ADB 现象，也不能把设备 Netty worker 说成全局单线程。

用已安装 `BaseDriver` 的一个离线脚本控制 unresolved command 与随后截图，得到：
`proxy-start → diagnostic-client-deadline → proxy-complete → screenshot-dispatch`。
该脚本只模拟客户端 deadline 标记，不启动 HTTP/server/设备；它验证排队机制，不模拟 USB 故障。
结果在 `.work/formal-offline-appium-queue.log`，实际包版本在
`.work/formal-offline-appium-source-versions.log`。

这与原日志中的晚发截图一致。原失败截图只能证明 **SDK/host 路径超时**，不能当作设备端
`CaptureScreenshot` 处理耗时的证据；原 `failure-ui-failed.json` 和失败事实保留，不改写 sealed bundle。

## 4. 首轮受控修正与当时边界

只修正本轮已证实的失联取证分支：

- [runner](../../../tools/react_native/bleng_android_lifecycle.py) 记录 fresh `/status` 读取是否失败。
  若失败，后续 failure-UI capture 不再向原 Appium queue 追加 PNG/XML；ADB 固定探针仍独立执行。
- [diagnostics](../../../tools/react_native/bleng_android_close_diagnostics.py) 在原 candidate、driver、
  session/config 和 host authority guard 后，保存带明确 refusal error、`reads=[]`、`rawfiles={}`
  的 `failure-ui-failed.json`；该拒绝也消耗 one-shot，不冒充已读取，更不冒充健康。
- 保留 primary error、failed/quarantine、live capture 退休、DELETE、独立 finalization/restoration
  的原顺序和预算。端点可读取时原有 PNG→XML 路径及 partial failure 取证保留。

**这不是通用 queue cancellation 修复。** fresh status 成功也不证明 Appium queue 空闲；这条剩余
边界仍然存在。若需要所有失败窗口均有独立 UI 取证，应另行在现有 Android session owner 内设计
固定原 device session 的 bounded direct read，并测试身份、原始响应、预算和证据兼容性；
不能仅换一个 WebDriver TCP connection 就宣称绕过 server command queue。
首轮没有引入该机制、改变点击/locator/idle/readTimeout 或修改第三方源码、Rust owner/watchdog。
随后经用户要求继续，在下面 §6 补齐了固定原 session 的直连诊断；不改变这里记录的首轮验证范围。

## 5. 实际验证

新增 [runner 回归](../../../tools/tests/react_native/test_bleng_android_lifecycle.py) 先 red：
fresh status timeout 后仍调用截图一次；最小实现后 green，并经现有 bundle verifier 校验 refusal。

- `pixi run python -m unittest` 的四个定向用例：新增 unreachable-server refusal、既有成功
  failure-UI capture、截图自身失败、fresh server timeout cleanup，4 passed / 19.373s。
  完整命令/结果见 `.work/formal-offline-failure-ui-{red,green}.log`。
- `pixi run python -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_run_bleng_android_lifecycle`：
  133 passed / 6.132s，`.work/formal-offline-failure-ui-neighbors.log`。
- `pixi run just react-native tools-test`：180 秒 timeout，无最终汇总，不计全套通过；检查日志及进程后
  未见遗留 unittest discovery 进程，未扩大预算。`.work/formal-offline-tools-test.log`。
- `pixi run just --no-deps react-native formal-host-check`：typecheck/autolinking passed；
  `pixi run just --no-deps react-native formal-host-test`：2 suites / 52 tests passed。
  对应 `.work/formal-offline-host-{check,test}.log`。

- 补充四个既有定向回归（普通成功流、显式 diagnostics 成功流、fresh status/ADB 接线、ADB
  probe failure 独立清理）通过；完整命令/结果为 `.work/formal-offline-failure-ui-regression.log`。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check` 通过；新增 Markdown 的 UTF-8、
  balanced fences、trailing whitespace/conflict marker 离线检查通过，无新增 Mermaid 图。
- `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/e9c6c064-0019-4cf7-a5f1-75418b8885f2`：
  原 bundle `integrity_status=passed`，同时保留 `status=failed / phone_status=quarantined / rf=not_run`
  及 `host_gate_upgraded=false`。`.work/formal-offline-original-bundle-verify.log`。

新软件回归不升级原真机失败、资源零、完整 lifecycle/reload/RF/ABI 或任何 Phase gate。

## 6. 继续离线修正：绕开 host queue 的绑定 session 诊断

用户询问是否修好并要求继续。结论拆开：**失联后的排队防护已通过软件回归，但 Evidence click
阻塞和 ADB service 接受超时没有被证明修复。** 本轮继续仅离线实现诊断路径，不重放真机。

### 机制与责任

- [Android session owner](../../../tools/react_native/bleng_android_session.py) 新增
  `read_failure_ui(kind, deadline)`：PNG→XML，各一次，失败后不得继续/重试。仅用原 bound selector
  的 device session UUID、system port、server identity，以及原 driver/executor/config、phone lease
  和 evidence guards；不通过 SDK 找新 session，不刷新或收养 identity。
- 只对原 `http://127.0.0.1:<system_port>/session/<device_session>/screenshot` 与 `/source`
  发 fresh/direct GET。绕开的是 **host Appium command queue**，不是设备 Netty、USB 或 ADB forwarding。
  fresh `/status` 失败时仍保留 §4 的拒绝读取分支；status 成功也不再把它当 host queue 空闲证明。
- [共享 bounded HTTP reader](../../../tools/mobile_appium.py) 的默认 64 KiB ceiling 不变；
  只有 UI 读取显式采用 PNG 12 MiB、XML 7 MiB 的 wire ceiling（包含 headers/JSON 编码开销），
  再严格验证 session envelope、base64/PNG signature、decoded PNG ≤8 MiB、XML UTF-8 ≤1 MiB。
  无 DNS/proxy/redirect/retry/fallback。每次读取仍是 `min(shared deadline, now + 5s)`，没有扩大预算。
- owner 发布 `platform/failure-ui-{png,xml}.json`，保留原 endpoint/selector、起止时间、预算、
  decoded bytes hash/size 和错误，不放 UI raw 内容。publication 后再检查身份和期限；迟完成
  或漂移不返回成功 raw，不允许下一读，不改写已发布的不可变回执。
- [diagnostics](../../../tools/react_native/bleng_android_close_diagnostics.py) 只经注入的 bound reader
  取得失败 PNG/XML；原 driver 仅用于固定身份/authority 检查。新 capture 明确记录
  `read_transport=bound-device-http` 与 `device.screenshot → device.source`，不冒称 SDK 调用。
  原 pre-Close 截图路径、场景点击、DELETE、主错误与独立清理顺序保持原职责。
- [verifier](../../../tools/react_native/bleng_lifecycle_evidence.py) 兼容历史 SDK capture，严格区分
  新 transport。新 owner receipt 的 sessions/server 绑定 `bound-session.json`，端口绑定
  `prepared-owner.json`；核对固定 URL、严格数值类型、顺序/嵌套时间与 raw hash/size。
  已发布 raw 必须有成功 owner receipt；失败/零读/partial evidence 保留真实缺口。

### Test-first 与集成验证

先增加 mobile wire-ceiling 和 runner 测试：status 可读、原场景 click timeout、Appium SDK 截图
方法明确禁止调用。旧实现确实调用了该方法，测试 red；直连接线后成功保存原 session 的 PNG/XML，
且 primary failure、host failed/not_run 不被升级。SDK 的禁止不是 global success mock；新请求在
现有 strict socket harness 内按 exact port/path/envelope 应答，未知设备/网络命令仍拒绝。

- red：`.work/direct-ui-integration-red.log`；初次集成还暴露 verifier 错把 port 放在
  `bound-session.json` 而非真实 `prepared-owner.json` 的 schema 不一致，已修正实际接线并回归，
  没有改生产 bound record 去迎合 fixture。
- `pixi run python -m unittest tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_bleng_android_session tools.tests.react_native.test_bleng_android_preparation tools.tests.react_native.test_run_bleng_android_lifecycle`：
  281 passed / 161.951s，`.work/direct-ui-neighbors.log`。
- 8 个 runner 定向回归：直连不经过 unresolved proxy、成功 failure capture、direct timeout
  主因/cleanup、status unavailable refusal、普通成功、显式 diagnostics 成功、fresh status/ADB 接线、
  ADB probe failure 独立清理，8 passed / 72.490s；完整命令见 `.work/direct-ui-runner-regression.log`。
- 随后补充 failed bundle 中 owner 已报错却保留 raw 的 re-sealing 反例：先 red，verifier 必须拒绝
  该矛盾；保留成功 read 后 publication/guard 失败而没有 raw 的合法路径。
  `.work/direct-ui-error-verdict-red.log`。最小修正后完整 evidence 模块为 68 passed / 2.652s，
  `pixi run python -m unittest tools.tests.react_native.test_bleng_lifecycle_evidence`，
  `.work/direct-ui-evidence-final.log`。
- `pixi run just --no-deps react-native formal-host-check` 与
  `pixi run just --no-deps react-native formal-host-test` 再次通过（2 suites / 52 tests）；
  `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check` 通过，
  日志为 `.work/direct-ui-{host-check,host-test,fmt,lint,docs,diff-check}.log`。
- 原真实 bundle 再次 offline integrity passed，同时保留 failed/quarantined/not_run，
  `host_gate_upgraded=false`；`.work/direct-ui-historical-bundle.log`。
- 本轮没有重试上一轮 180 秒超时的全 `tools-test`，没有把定向回归当作完整 discovery 汇总。

### 仍未修复或验证的部分

| 边界 | 本轮结论 |
| --- | --- |
| failure UI 在原 host proxy queue 上晚发 | 维护 runner 已不再经该 queue 提交失败诊断；离线回归通过。 |
| 原 Evidence click 是否到设备、为何未返回 | 未定位，不修改场景点击，也不推定 RN/Rust/UiAutomator2 根因。 |
| `service_accept` 为何无响应 | 未定位；直连 UI 仍依赖同一 forwarding/ADB/设备服务，不能保证故障现场一定拿到图像。 |
| 真机 lifecycle、graceful DELETE、native resource-zero、RF/ABI | 无本轮新证据，原失败和 pending 保留。 |

再次核对前序 §18 的 daemon trace：它来自 `2e18a370…` 初始节点不可见的独立运行，
当时 service probe 完全通过；不是本次 `e9c6c064…` 的 service_accept 故障窗口。
因此不能靠重读该 trace 就将本故障定因于 USB/adbd，也不能混合两轮时序。
当前缺少的是同一次失效窗口中的 ADB stream/service 建立与设备侧接受的关联证据。
如需继续收集，应另行提出最小只读观测、身份/租约/停止条件及设备授权；
本轮不执行，不无证据重复完整 lifecycle，也不扩大超时。

## 7. ADB 握手边界：client OKAY 不等于 transport 缓存状态

再次收到“继续”后，仍保留不重复真机约束。本轮没有运行设备/ADB/Appium、采样当前 daemon、
切换 USB 或重启共享服务；只核对旧日志、补读少量本地缺失的官方源码并验证软件。

### 官方实现与已有健康 trace 的交叉核对

官方源固定到 AOSP adb revision `1cf2f017d312f73b3dc53bda85ef2610e35a80e9`，
不是对已安装 SDK binary 或设备 adbd build 的精确源码 attestation。
只下载五个文件（sockets、adb、listeners、trace initialization、CLI），没有 clone 仓库。
原始许可证和内容保存在 ignored `.work/adb-handshake-official-*`，URL、revision、size、SHA256
在 `.work/adb-handshake-source-manifest.json`，已核对五份 hash。

- [sockets.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/sockets.cpp)：
  `smart_socket_enqueue` 对非 host service 设 `local_socket_ready_notify`，然后 `connect_to_remote`
  发送 `A_OPEN`；`local_socket_ready_notify` 才向 client `SendOkay`。
- [adb.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/adb.cpp)：
  接收 `A_OPEN` 后先 `create_local_service_socket`，再发送 stream ready；首个 `A_OKAY`
  在另一侧建立 peer，再经 `local_socket_ack` 触发 ready callback。后续 `A_OKAY` 也可用于同一
  stream 的流控，不能不看 local/remote IDs 就当作某个新 service 的接受证明。
- [adb_listeners.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/adb_listeners.cpp)：
  forwarding listener accept host TCP 后，`connect_to_remote` 打开 remote destination；在首个
  remote ready 前没有普通 local socket 的 readable forwarding。因此 fresh TCP connect 成功，
  不等于 HTTP 已到设备 UiAutomator2。

旧健康 control 的实际 SDK 37.0.1 trace 支持该顺序，但不升级为本次故障证据：
`.work/adb-daemon-trace.log` L359–364，18:52:42.719，原 boot-id shell service → outbound
`OPEN arg0=4`；L371，18:52:42.730，remote `OKAY arg0=0x3a8a arg1=4`；L375–377，
main-loop ACK → client `fd=11` 的四字节 `OKAY`。之后才有 shell output `WRTE`。

所以原探针 `service_accept` 是 client 在 host smart socket 上等待服务连接结果，不只是缓存 state，
也不是等待 boot-id 内容。**没有这四字节回执仍不能证明卡在设备创建服务**：host 解析/调度、
packet 发送/接收、设备处理和 host ready callback 都在观察边界内。日志 `to remote [OPEN]`
同样只是 host 相应软件边界，不独立证明 USB 物理发送或设备接收。

这使 fresh `/status` 与 shell service 的失效有一个值得核对的共同下层——新 remote stream 建立；
不是已经证明二者同因。最终 click 可能复用已有 keep-alive stream，现有日志无法确认，不能机械
套用 fresh connection 的 `A_OPEN` 路径。当前 sample 文件也不能补这个事实：
`.work/formal-fresh-status-adb-sample.txt` 是 **11-06 07:59、PID 93206**，不是本次 e9 run 的
PID 16217；USB read/poll/condition wait 的旧栈不证明本次存在死锁。

### 修正现有固定探针的 I/O 可观测性

源码核对还暴露了回执的真实缺口：原 `service_accept` 将 `sendall` 和响应读取放在一个阶段，
只有总时间及 error；历史 timeout 不能单凭它判断本地发送是否完成。

[session owner](../../../tools/react_native/bleng_android_session.py) 现在仅给该固定阶段补两个字段：

- `request_sent_monotonic`：`sendall` 返回后立即记。只证明本地 socket send completion，
  不证明 daemon 解析、USB 完成或设备收到；若 write 返回已过预算，仍记录迟返回事实并失败。
- `response_wait_started_monotonic`：调用状态读取前记。若在更新剩余 socket timeout 前已到期，
  仍可在读取 helper 内拒绝 I/O，不能把该标记当作已进入 `recv` 的证明。

发送失败时两者保留 null；发送完成、尚未进入状态读取时后者可为 null。没有新增命令、retry、
daemon 管理或预算。boot-id shell 原样保留，测试仍断言仅原四条白名单请求。
[verifier](../../../tools/react_native/bleng_lifecycle_evidence.py) 接受历史无字段记录，
新字段只可成对出现在 `service_accept`，校验有限数值、阶段内次序及输出一致性；
失败迟返回仍如实保存，不能升级成功。旧 e9 bundle 不补填字段、不重封。

### 实际验证与下一步授权边界

- 新 owner/verifier 用例先 red（缺 marker / 旧 verifier 拒绝新结构），后 green：
  `.work/adb-handshake-markers-{red,green}.log`；三个定向用例 3 passed / 1.541s。
- `pixi run python -m unittest tools.tests.react_native.test_bleng_android_session tools.tests.react_native.test_bleng_lifecycle_evidence tools.tests.react_native.test_run_bleng_android_lifecycle`：
  142 passed / 102.610s，`.work/adb-handshake-neighbors.log`。
- 三个现有 runner 回归（status/ADB 接线、ADB failure 保留主错误/独立清理、unresolved proxy
  直连取证）3 passed / 31.127s；完整命令见 `.work/adb-handshake-runner.log`。
- Formal host check 再次通过，Jest 2 suites / 52 tests；原 e9 bundle offline integrity passed，
  failed/quarantined/not_run 及 `host_gate_upgraded=false` 保留。
  `.work/adb-handshake-{host-check,host-test,historical-bundle}.log`。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check`
  通过；首次 fmt 发现新增测试闭包的 loop binding 问题，显式绑定后修正并通过，未改产品逻辑。
  日志为 `.work/adb-handshake-{fmt,lint,mermaid,diff-check}.log`。

检查的 [trace initialization](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/adb_trace.cpp)
从 host `ADB_TRACE` 环境在 init 时读 mask；`server-status` 仅返回状态。本次未在所查公开
CLI/host service 中找到给既有 daemon 无扰动开启完整 packet trace 的入口，不把给后续 client
设置环境变量当成 daemon tracing，也不修改设备 `persist.adb.trace_mask`。

如需继续到原故障根因，下一份证据至少应关联同 serial、同 daemon、同一次失效窗口中的
service 请求、`A_OPEN` local ID、首个对应 remote `A_OKAY/A_CLSE` 和 client 回执；对 fresh
forward 还需区分 remote stream 建立与 HTTP handler 入口。只有 host trace 仍不能独立证明
设备收包/服务执行位置。没有现成 trace 时，启用它可能需要共享 daemon 的受控重启，
会中断已有 ADB/投屏连接；必须另获明确授权，不能藏进普通 run。
本轮不执行这一步；后续真机复现本身也需明确解除“不重复真机”的约束。
