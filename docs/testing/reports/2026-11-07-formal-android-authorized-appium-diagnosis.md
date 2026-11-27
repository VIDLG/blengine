# Formal Android：授权单次 Appium 诊断与原身份恢复

用户明确允许继续真机，并询问是否使用正常 Appium API/库、是否存在不必要的手写实现。
最初 SDK 审计执行一次同候选非 RF managed lifecycle（§2–6）。用户随后明确授权共享 ADB
daemon 重启、packet trace、官方 host USB backend 对照与环境恢复，追加 NATIVE/LIBUSB
各一次同候选诊断（§7–10）。三次均失败，未改生产代码、点击机制、locator、UiAutomator2
idle/readTimeout 或 Rust owner/watchdog；最后一次隔离现场已独立恢复，不再追加 lifecycle。
前序软件和协议边界见 [离线分析](2026-11-07-formal-android-offline-proxy-boundary.md)。
后续[USB 写完成语义核对](2026-11-07-formal-android-usb-write-boundary.md)进一步收窄 host writer 边界，
不改变本报告的原始失败与恢复 verdict；主 BUG 仍未修复。

## 1. SDK 与自定义代码的真实边界

不能把整个 host controller 都称作“Appium 官方代码”：

| 路径 | 实际使用方式 |
| --- | --- |
| 正常 UI 场景 | Appium Python Client `webdriver.Remote`、`UiAutomator2Options`、标准 locator、element click/text/displayed/enabled；由官方 Appium server + UiAutomator2 driver/server 执行。 |
| 正常/pre-Close PNG/XML | Appium Python Client screenshot/page source API；本轮尚未走到 pre-Close。 |
| failure UI 直连 | 项目自定义 bounded HTTP reader，调用原 UiAutomator2 device session 的 screenshot/source REST endpoint，绕开 host Appium queue；不是官方 Python Client 的正常会话调用。本轮 status 失败而拒绝读取，未执行这条路径。 |
| ADB 固定失败探针 | 项目自定义最小 smart-socket framing，三条 fresh socket、四条固定请求；不是 Appium API 或完整 ADB 客户端。目的包括避免 SDK adb CLI 在失效窗口隐式启动/替换共享 daemon。 |
| 安装、租约、identity、回执与清理 | 项目 host controller 策略；调用既有 Android SDK/系统工具，不是重新实现 Appium server 或 BLE。 |

已安装 Python Client 5.3.1、Selenium 4.51.0、urllib3 2.7.0。
审计实际 `AppiumConnection` / Selenium `RemoteConnection.execute/_request`：标准 transport 可以发送
WebDriver commands，但直接替换当前诊断 reader 并不自动保留 wire size ceiling、绝对总时限和
禁止 redirect 的附加限制。不能为了“都用 SDK”默默删除这些安全边界，或仅换连接就宣称远端
queue 被取消。host/device session UUID 不同，端点切换也不能混用 session 身份。
本轮没有新增协议，也没有宣称自定义诊断已由官方 SDK 实现。

本机 API 源码与版本核对在 `.work/appium-sdk-diagnostic-api-audit.log`；Context7 官方索引查询
为 `.work/appium-sdk-docs-{library,direct}.log`。这只是软件 API 核对，不是新设备通过证据。

## 2. 预检与单次运行范围

只读 inventory `f3d77e39-e39b-485d-bb20-1f742eeb5ad3` 通过，未取得 session authority：
三份 installed helper path/hash 匹配、无 forwarding、hidden policies 为 null、mock-location deny，
无活动 phone lease。该只读 lease 独立正常释放，没有用 inventory JSON 替代 live owner binding。

Host listener、`lsof -d txt` 和 process/start-time 核对确认 SDK daemon PID **16217** 原样运行；
QtScrcpy PID **35648** 保留。Appium/system ports `4737/8237` 空闲。
没有切换或重启共享 daemon、关闭投屏或重插 USB。相关日志为：

- `.work/authorized-appium-preflight.log`
- `.work/formal-authorized-preflight-f3d77e39-e39b-485d-bb20-1f742eeb5ad3/`
- `.work/authorized-appium-{daemon-image,daemon-process,mirroring-process,local-inputs,toolchains}.log`

单次运行请求保存在 `.work/authorized-appium-request.json`，使用既有
`run_android_lifecycle`，没有另写 UI driver 或替换场景：

```text
run_id=57b183c0-84cc-44de-97f3-72c4f718ac88
serial=3ac5cc09
app=com.blehubblenghost
APK SHA256=51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef
app_pid=27295; app_start_ticks=230758052; app_uid=10378
server_pid=28142; server_start_ticks=230758760; server_uid=10255
host_session=5d839ee6-f906-4f11-949d-11ffab39bd37
device_session=774e0eff-cd0b-42f2-b9cb-ff159527250c
timeout=180; phase_timeout=20; cleanup_seconds=90
Appium=4737; systemPort=8237; close_diagnostics=true
observations=[idle, started]
status=failed; scenario_status=failed; session_status=failed
phone_status=quarantined; host_gate_status=failed; rf=not_run
```

授权使用既有 root-aware installer 重新安装同一 APK，`candidate/008-install.json` 明确
`grant=false`；只允许 runner 已有的临时 helper 设置及恢复，没有执行 BLE 权限授权/撤权、radio
或固件变更，没有 scan/RF。进程可见不代表 BLE/native resource-zero。

外层 `.work/authorized-appium-once.py` 是一次性启动/host observer，不是新维护入口：调用现有
runner，单次读取原 daemon 身份，失败后才用 macOS `/usr/bin/sample` 采样一次，子进程有独立
期限；不向设备发额外命令，不动态选择其它 daemon，不改变 scenario verdict。
`run_quiet.py` 的 `command passed` 仅表示此包装脚本返回，**不等于 lifecycle passed**。
原始输出 `.work/authorized-appium-once.log`，verdict 必须读取 sealed result。

## 3. 本轮失败位置：Close 尚未提交

本轮不是“Close 已返回后最终 Evidence click 卡住”的重复证据。

相对 `automation/appium.log`：

- L1318：`workbench-current` click 被 proxy；后续正常返回。
- L1333–1350：原 session 的 state find、displayed、text 依次完成，`01:39:46.647` 实际返回
  `started`。
- L1351–1357：`01:39:46.680–681`，正常 SDK 提交 `formal-host-title` find，host proxy 到原
  device session；未见对应 device response。
- L1358 前的客户端完成行在 `01:40:06.683`，约 20 秒超时，不是 no-such-element 404。

设备 `platform/close-diagnostics/live-logcat.raw` L503–507 记录了回 Current 的 Click
handler/response；L508–518 记录最后成功的 state find / displayed / text，设备文本为 `started`。
没有最后 title find 的 `channel read`，但缺日志本身不证明请求未到设备。
没有 Close 操作，也没有本轮 pre-Close PNG/XML；不能把原运行的较早画面当作本轮现场。

Appium 日志的 `01:xx` 与设备 logcat `09:xx` 保留各自时钟域，不能直接相减得单向传输耗时。
后面的同窗诊断只使用共同 host monotonic 时钟。

## 4. 新 marker 的真实设备证据与同窗 host 栈

Fresh `/status` 由 host monotonic `56917.670842958` 到 `56919.683074125` 超时。
原四条请求的 ADB probe 随后记录：

| 阶段 | 观察 |
| --- | --- |
| server_version | `OKAY00040029`。 |
| cached_state | `OKAY0006device`，仅缓存状态。 |
| transport_select | `OKAY`。 |
| service_accept start | `56919.718617416`。 |
| 本地 request sendall 完成 | `56919.727129125`。 |
| 进入响应读取 helper | `56919.727129958`。 |
| service_accept finished | `56922.693213416`，timeout，output 为空，未进入 boot output。 |

这次不是只能根据总阶段时间推测发送是否完成：**本地 write completion 确实已观察到**，其后
约 2.966 秒没有取得接受回执。它仍不证明 daemon 解析完成、USB 物理发送或设备收包。
Fresh status 和 ADB probe 都发生在 primary SDK timeout 之后，不把它们倒推成故障起点。

失败触发后对原 PID 16217 的 process/start-time/executable 做前后 guard，采样命令窗口为
`56919.720437500 → 56920.973772875`，与本轮 service 响应等待窗口重叠。
observer 已本地退休，未使用旧 run 的栈冒充本轮：

- `.work/authorized-appium-host-observer-57b183c0-84cc-44de-97f3-72c4f718ac88/observer.json`
- 同目录 `adb.sample.txt` / `sample-command.log`

栈中主 loop 为 `poll`，USB read thread 为 `UsbConnection::Read → usb_read → IOUSBInterfaceClass::ReadPipe`，
write-side thread 为 condition wait。它不是 ADB packet trace，也不提供 adbd service 执行位置；
这些等待栈不能单独定性为死锁、USB 故障或设备未收到 `A_OPEN`。

随后只读查询同一次 `09:39:30–09:40:40` 的 macOS unified log，按 kernel/adb USB 消息过滤，
得到 `.work/authorized-appium-macos-usb-window.log`（70 lines / 8945 bytes）。该过滤窗口持续
约每秒出现 `adb@(null): AppleUSBHostUserClient::start: unlock exception granted`，
在仍正常查询和进入超时前后均有，没有本过滤结果中的明确目标 detach/attach 或拒绝记录。
日志没有 serial/interface 身份关联，不能把 `unlock exception granted` 当作目标 Android 链路
全部权限/健康已获证明，也不能据此排除 USB。没有提权或修改 USB/TCC。

Fresh status 失败后，failure UI 保存明确 refusal，`reads=[] / rawfiles={}`，本轮没有执行新增
直连 PNG/XML，也没有把诊断 SDK/HTTP 超时当成截图 handler 超时。因而此轮不能验证直连 UI
读取的真实设备成功，只有此前离线软件回归。

## 5. 失败收尾与独立恢复

原 DELETE 没有成功回执，host Appium TERM 超时后只对本轮 child group 升级 KILL；
App/helper/tag 的设备操作仍不可用，因此 run 正确保留 quarantine。
Appium 的 instrumentation `SIGTERM` / “probably crashed” 信息出现在 host 收尾阶段；
没有据此声称此前设备 server 已崩溃。实际恢复仍查到了原 server identity。

在原 quarantine lock 下，一次 4 秒 boot-id 只读检查恢复可用：
`57045.401601416 → 57045.460073291`，boot ID 未变。
`.work/authorized-appium-quarantine-reachability.json`。
没有 daemon 重启、USB reset 或手机重启。

复用已核对的原身份恢复脚本，仅替换原 run selector，不放宽身份/lease/assertion：
`.work/recover-authorized-appium-57b183c0.py`。先验证 sealed bundle、原 lease inode/owner、boot、
原 App/server 的 PID/UID/start-time/APK、helper baseline 和 active instrumentation，再退休这次
instrumentation/App，核对 helper path/hash、policy/AppOps/store/IME、空 forward 与日志标签恢复，
最后归档 exact quarantine lock。没有强停未知 App，也没有把 force-stop 当作 graceful Close。

独立恢复 `a99810af-ae30-4397-8b78-eec2099d422b`：

```text
status=restored
app_stopped=true; helper_restored=true; diagnostic_tag_restored=true
lease_archived=true; radio_actions=[]; rf=not_run
device_delete=false; graceful_blen_close=not-claimed; native_resource_zero=not-claimed
```

恢复日志 `.work/authorized-appium-recovery.log`；证据目录
`.work/formal-android-managed-recovery-a99810af-ae30-4397-8b78-eec2099d422b/`。
恢复后 daemon PID 16217、QtScrcpy PID 35648 均保持，4737/8237 无 listener，当前无活动 phone lease。
原 sealed run **仍是 failed/quarantined**，与当前已恢复的物理状态是不同事实，不改写原 verdict。

## 6. 验证、结论与后续

- `pixi run python -m unittest tools.tests.react_native.test_android_appium tools.tests.react_native.test_mobile_appium`：
  49 passed / 1.358s，包括真实安装 SDK 的 session/HTTP/retry 边界回归；
  `.work/authorized-appium-sdk-regression.log`。
- `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/57b183c0-84cc-44de-97f3-72c4f718ac88`：
  integrity passed，`host_gate_upgraded=false`，保留 failed/quarantined/not_run；
  `.work/authorized-appium-bundle-verify.log`。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid`、`git diff --check`
  通过；日志 `.work/authorized-appium-{fmt,lint,mermaid,diff-check}.log`。没有把文档/软件检查
  当作本轮设备通过，也没有重试全工具 discovery 的既有 timeout。

本轮没有证据支持换 locator、改 Rust Close 或重写正常 UI driver。主 SDK find 在自定义失败探针
之前已经超时，不能把诊断实现当作本轮 primary failure 的起因；也不能因此宣称所有自定义 host
代码没有风险。对于自定义传输，继续保持小范围、固定请求、明确期限与身份/字节上限，不扩展
成通用客户端，不以换 SDK 的名义删除 safety guards。

这次 SDK 诊断结束时，原故障尚未修复；它将 service timeout 明确为本地 send 完成后的回执
等待，但没有 packet 证据。当时没有重启共享 daemon。后续用户另行明确授权 trace/backend
对照和环境恢复，执行结果如下；不得将该授权倒写成最初 SDK run 已做 packet capture。

## 7. 授权 packet trace：主请求已获 remote ADB ACK

仍使用 §2 的同一 APK、手机及官方 Appium UI 路径；每次开始前确认无活动 instrumentation、
原 App 已退出、helper baseline 与 phone lease。仅显式切换官方 SDK ADB 37.0.1/build 15733141
的 host daemon/backend，启用 `ADB_TRACE=packets,sockets,transport,usb,services`；没有开启
设备端 `persist.adb.trace_mask`，没有改 BLE 权限、radio 或固件。共享 daemon 重启会使其他
ADB client/投屏连接短暂中断；保留 QtScrcpy 主进程不等于证明投屏连接恢复。

| 项目 | NATIVE | LIBUSB 对照 |
| --- | --- | --- |
| run | `6107a1d9-b574-48c1-a32e-9edbeae5d44b` | `d1d8eb00-fb97-4fb0-bc09-aa501a3944ed` |
| trace daemon PID | `8029` | `15587` |
| `server-status` | `usb_backend: NATIVE` | `usb_backend: LIBUSB`、`usb_backend_forced: true` |
| observations / Close | `[idle, started]`；尚未提交 Close | `[idle, started]`；尚未提交 Close |
| 主 find | `session-state` | `session-state` |
| 主 stream host / remote ID | `0x26f / 0x4236` | `0x26d / 0x4480` |
| host WRTE → remote OKAY | `09:56:57.147 → 09:56:57.150` | `10:20:04.071 → 10:20:04.074` |
| matching remote HTTP WRTE | 未记录 | 未记录 |
| host CLSE | `09:57:39.806` | `10:20:46.727` |
| fresh `tcp:6790` OPEN | `09:57:17.162`、local `0x592` | `10:20:24.085`、local `0x590` |
| fresh boot shell OPEN | `09:57:19.212`、local `0x595` | `10:20:26.137`、local `0x595` |
| fresh OPEN 的 remote OKAY/CLSE | 截至各自 capture 结束均未记录 | 截至各自 capture 结束均未记录 |

时间来自同一 host daemon 的 packet 日志；不能与 Appium UTC 日志或 device logcat 直接相减。
主 stream 是既有连接：host `WRTE` 长度均为 407，随后收到 matching remote `OKAY`，不是主
请求在 OPEN 阶段失败。约 42.66 秒后 host local socket EOF/CLSE 包含客户端超时后的收尾时间，
不是主 SDK 的 20 秒期限被扩大。Fresh status/boot 是 primary timeout **之后**的诊断，不能
倒推成故障起点。Remote ADB ACK 也不等于 UiAutomator2 handler 已收到/完成 HTTP 请求。

原始证据目录为 `.work/authorized-packet-trace-<run>/`，各自包含 `trace-server-status.json`、
`trace-daemon-image.json`、`trace-capture.json` 和 `adb-packets.raw.log`。Raw capture 均未截断：

- NATIVE：6,326,869 bytes / 64,346 lines；SHA-256
  `5ac1c3473ac1bdc09e12ad3d455b26a38f16db45579e05e4b927987c18928a43`。
  主 WRTE/ACK 为 L63339/L63342，CLSE 为 L63942；fresh OPEN 为 L63546/L63610。
- LIBUSB：7,125,222 bytes / 71,795 lines；SHA-256
  `a13ec8338091d3651bd6b636f39c467fd6d061da97c74218f5b2b8c96d843817`。
  主 WRTE/ACK 为 L70729/L70734，CLSE 为 L71388；fresh OPEN 为 L70946/L71049。

两次 bundle 位于 `.work/formal-android-managed-lifecycle/<run>/`：
`automation/appium.log` 记录正常 SDK proxy；`platform/close-diagnostics/live-logcat.raw`
各自最后成功 Click 为 L499–502 / L498–501，没有后续主 find 的 `channel read` 证据。
缺日志不能单独证明未到设备。两次 device server 的 “probably crashed”/SIGTERM 仍只出现在
host cleanup，不作为 primary crash 证据。

### ACK 后不是整个入站 USB 流立即停止

对目标 serial 的所有 `from remote` packet 做离线核对，而非只看主 stream：

- NATIVE 主 ACK 后还有三个旧 `tcp:6790` stream 的 CLSE（L63422/L63434/L63446），以及
  `09:57:15.188` 的 logcat stream WRTE（L63497，remote/local `0x4276/0x331`）。
- LIBUSB 同样还有三个旧 `tcp:6790` stream 的 CLSE（L70815/L70829/L70843），以及
  `10:20:16.389` 的 logcat stream WRTE（L70896，remote/local `0x44c0/0x32f`）。

两份 trace 的主 ACK 后均恰有以上四个目标 remote packet；摘要在各自
`post-ack-remote-summary.json`。这排除了“主 ACK 之后再无任何设备入站数据”的错误描述，
但不证明此后 host→device 写入、fresh service admission 或 UIA2 HTTP 健康。
`to remote OPEN` / LIBUSB `Write A_WRTE` 日志本身仍不是 USB transfer completion。

LIBUSB 启动时的 `LIBUSB_ERROR_INVALID_PARAM` / missing endpoints 关联其他 USB 地址；目标
`0-1 / 3ac5cc09` 有 ADB interface、bulk IN/OUT、claim/attach 和 AUTH/CNXN 成功记录，不能把
其他设备初始化错误归为目标故障。官方 LIBUSB 对照没有消除同类失败，不支持“仅 NATIVE
backend 有问题”，也不能据此完全排除 USB/host/设备端 transport。

### 未进入场景的失败尝试

首次 trace controller 在 Python quoting 语法检查阶段失败，未执行设备命令。随后一次 daemon
使用 `-L tcp:127.0.0.1:5037`，SDK 报 `listening on specified hostname currently unsupported`，
PID 7548 退出；没有启动 App 场景，普通 daemon 已恢复 PID 7572。之后使用已验证的
`-L tcp:5037` 并核对 listener。保留这些失败，不将 controller 的正常退出计为 lifecycle passed。

## 8. 两次隔离现场恢复与当前环境

NATIVE trace controller 恢复普通 daemon PID 9231；独立恢复
`fa017e2d-4645-49a0-907b-2ba2423ef054` 完成原身份 App/server 退休、helper/tag/forward
恢复和 exact quarantine lock 归档。日志 `.work/authorized-packet-trace-recovery.log`。

LIBUSB trace controller 恢复普通 **NATIVE daemon PID 16841** 后目标曾为 offline。稍后一次
有界 boot-id 读取成功（host monotonic `59969.479544458 → 59969.537573708`），boot ID 与原
session 相同：`35df0f9f-6c54-42b0-800c-3c410469b6e7`；没有执行 `adb reconnect`、USB reset
或手机重启。回执 `.work/authorized-libusb-recovery-reachability.json`。

复用已审计恢复脚本，仅替换 source run selector 为 `d1d8eb00…`，先核对该轮诊断回执形态，
不放宽 PID/UID/start ticks/APK/boot、helper baseline、source hash 或 lease inode/owner guards。
首次脚本启动因缺项目 `PYTHONPATH` 在 import 阶段失败，未执行设备命令；加 `PYTHONPATH=.`
后完成独立恢复 `6a54bfbb-257b-4b1e-883e-110ebdfcb10e`：

```text
status=restored; source_verdict=failed; host_gate_status=failed
app_stopped=true; helper_restored=true; diagnostic_tag_restored=true
lease_archived=true; device_delete=false; radio_actions=[]; rf=not_run
graceful_blen_close=not-claimed; native_resource_zero=not-claimed
```

证据 `.work/formal-android-managed-recovery-6a54bfbb-257b-4b1e-883e-110ebdfcb10e/`，日志
`.work/authorized-libusb-recovery.log`。恢复后 `lsof` 核对 5037 listener 的 executable 仍为 SDK
ADB，4737/8237 无 listener，当前无活动 phone lease；QtScrcpy PID 35648 主进程仍在。
随后 `server-status` 确认 NATIVE、`trace_level: ""`，没有留下强制 LIBUSB/trace daemon。
三次原 sealed run 均保留 failed/quarantined；独立物理恢复不能改写历史 verdict。

## 9. 恢复后设备只读历史日志：窗口缺失

不再启动 Appium/lifecycle。在独立 App-only phone lease 下，对同一 serial 单次读取 boot、
设备时钟、server-status、过滤的 `logcat -b all -d -t 4000`、只读 root `dmesg`、adbd PID，最后
再次核对 boot。不清日志、不修改 trace property、不安装 helper、不重启 adbd，不操作 radio。
每条命令独立 7 秒期限，lease 正常释放，无 cleanup errors。

证据 `.work/authorized-adbd-history-50a63e3d-0aca-474d-a9d0-d6c17edeb1db/`：

- 设备时钟 `2026-11-07T10:43:16+0800`，boot 前后一致；adbd PID 当时为 14887。
  这不是故障窗口的线程/进程状态，不能声称其在故障中未重启或没有阻塞。
- 按 adbd/UsbDeviceManager/UsbPortManager/UsbHostManager/UsbGadget/kernel/appium 等标签
  过滤的 logcat 输出为空；不等于相关错误未发生，且 `-t 4000` 限定历史覆盖。
- `kernel-dmesg.raw` 为 1,072,230 bytes / 8,505 lines，SHA-256
  `6cd64b4b53648791002fedbb28bb4fd036e451a26ac8efc90324dde8167f57f4`。
  环形日志从 kernel 时间 2269057.982993 开始；首个 UTC anchor 在 L461：
  `2026-11-07 02:22:46.998875371 UTC`。保留段已晚于 LIBUSB 主失败 `10:20:04 +0800`，
  更不覆盖 NATIVE 主失败。该段没有支持目标 adbd/USB 根因的现场证据，不能据此排除它们。

## 10. 当前结论与验证

**主停顿尚未修复。** 已完成的软件诊断边界修正及回归不等于本轮真机通过。新增证据明确：

1. 正常 UI 仍走官方 Appium Python Client/server/UiAutomator2，不是自定义 UI 驱动。
2. 两种官方 host backend 均出现主 stream remote ACK 后无 HTTP response；后续 fresh
   `tcp:6790` 和 shell OPEN 未获 remote 确认，且不能误称主 ACK 后整个入站流立即断掉。
3. 证据不能区分 adbd/service admission、设备 TCP forwarding/UiAutomator2 与底层 USB 的
   具体失败机制；不能仅据此修改 Rust Close、locator、idle timeout 或扩大客户端超时。
4. 现场已恢复，不再做第三次 trace lifecycle。下一项有信息价值的工作是设计设备端 adbd →
   TCP 6790 → UiAutomator2 的同窗、只读、有界观测；必须先明确官方工具能记录哪一边界、
   如何关联原身份和停止/恢复，不能再增加手写协议或用事后健康栈替代故障栈。

本次执行的离线验证：

- `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/6107a1d9-b574-48c1-a32e-9edbeae5d44b`
- `pixi run just react-native formal-host-lifecycle-verify .work/formal-android-managed-lifecycle/d1d8eb00-fb97-4fb0-bc09-aa501a3944ed`

两项 `integrity_status=passed / host_gate_upgraded=false`，保留 failed/quarantined/not_run。
日志 `.work/authorized-packet-trace-bundle-verify.log` 与 `.work/authorized-libusb-bundle-verify.log`。
本次只补证据与文档，没有新增生产/runner 实现，也未重新执行前述历史测试来宣称新通过。
`pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 均通过，日志
`.work/authorized-dual-backend-{fmt,lint,mermaid}.log`。作者文档 UTF-8、围栏、尾空格与冲突标记
检查通过。`git diff --check` exit 0；工作区另外三份文件有 Git CRLF→LF warning，未为此修改
无关文件，回执 `.work/authorized-dual-backend-diff-check.json`。最初包装检查误将非空 warning
输出视为失败，随后以 Git 实际退出码核实，不把该包装断言当作 whitespace 错误。

## 11. 继续调查：设备本地观测的只读能力核对

本节没有重跑 lifecycle、启动 Appium 或改 helper/BLE/radio/daemon。目标是避免继续依赖同一
USB 链路实时输送故障日志：预先把短时 TCP 6790 capture 和原 server 日志留在设备本地，
链路恢复后核对身份再取回。它不是新的 HTTP/ADB 实现，也不是用抓包取代官方 Appium UI。

三次独立 App-only lease 的能力检查均正常释放，单条命令最多 6 秒：

- `.work/authorized-device-observer-capabilities-a2ebf93b-39e8-47f1-9e5b-bd30b8684197/`
- `.work/authorized-device-observer-tools-7be9c35b-7881-4deb-b6cb-2f676341dcf1/`
- `.work/authorized-device-observer-root-scope-4f2d40d6-40d7-4e8d-9cf1-0f19cbb84ce9/`

观察结果：

- root 环境可见 `/system/bin/tcpdump`，报告 `4.99.4 / libpcap 1.10.5 (TPACKET_V3)`；
  binary SHA-256 为 `4aab8ec95b2421e7915a4a5c1dcaf3d373f7130fb941fc223deb8d90ac562281`。
  这是设备现有 binary 的身份/自报版本，不宣称已证明它与上游原版逐字节相同。
- `tcpdump -D` 能列出 `lo`，`timeout` 为 Toybox `0.8.12-android`，支持独立时限、首次信号及
  `-k` 升级期限。`logcat` 支持 `-f`、`--pid`、`threadtime`、`-T` 和文件大小/轮转选项。
- adbd 仍为 PID 14887；root 下 `/proc/14887/{stat,wchan,stack}` 可只读访问。这次健康状态
  的主线程在 `epoll_pwait`，不是故障栈，不能解释之前的停顿，也不提供 Java handler 栈。
- 初次复合 `su -c` probe 没有把整个命令体统一 shell-quote，部分后续命令实际在普通 shell
  执行，出现 Permission denied。随后用现有项目同类方式 `su -c <shlex.quote(body)>` 核对
  `uid=0/context=u:r:magisk:s0` 后重做只读检查；不将前次拒绝解释成设备不支持该能力。
  没有修改 SELinux、ptrace policy、服务或授权。

设备端 API/线程边界也已核对。已安装 server package 10.7.6 的 `gitHead` 为
`31de0505539af35602cbe62519d95702702b8195`；缺失于 npm 包的少量官方源码按该 revision
定点取得，原文与来源/hash manifest 保存在 `.work/authorized-device-observer-uia2-sources/`。
源码对照不等于已对安装 APK 做完整 build provenance 证明：

- `HttpServer.java` L46–47 为 `NioEventLoopGroup(1)` boss 和默认大小的 NIO worker group。
- `ServerHandler.java` L50、L64、L72–75：完整 HTTP request 进入 `channelRead` 后记录
  `channel read`，并同步 dispatch servlet；`SafeRequestHandler.java` L43–57 再记录 command。
- `FindElement.java` L43–59 为 selector/UI lookup 入口；`Status.java` L36–46 不做 UI lookup。
- 未发现官方 UiAutomator2 device thread-dump endpoint。不能把 `mobile: shell`、`kill -3`、
  `debuggerd` 或 `/proc` 读取称为 UiAutomator2 官方诊断 API；本次没有发送这类信号。

因此有 device `channel read` 可定位到 Netty 完整请求入口，但单纯缺日志仍不能证明未到达；
新的 TCP 连接也不保证分配到不同 worker。继续增加经 USB 的 logcat 流不能补齐这条缺口。

## 12. 两秒无 App 采集检查：轮转拒绝与单文件边界

只在精确 phone lease 下创建本次 UUID 的 `/data/local/tmp/blehub-observer-<run>/`，保存
owner nonce、目录 device/inode/UID/mode 和工具 hash；没有联网安装工具、启动 App 或发送
UI/BLE 命令。首次采集使用 `timeout -s INT -k 1 2`，仅对本次 collector 限时退出，不对
App/adbd/系统服务发信号。

### 首次检查失败，按原身份恢复

`66f8ac22-1832-4ae9-85c9-e786540db133`：

- `tcpdump -i lo -p -s 0 -U -C 1 -W 2 -c 2000 ... tcp port 6790` 正常启动，2.114 秒后返回
  timeout exit 124，报告零 packet/零 drop；本地 `logcat -f` 同样 2.133 秒后 exit 124。
- 实际 PCAP 文件名为 `capture.pcap_1107_110358_0.pcap`，不是预期的 `capture.pcap0`。
  文件 allowlist 和清理 guard 正确拒绝；原 smoke 保留 failed/quarantined，未通配删除。
- 独立恢复 `2c13286b-698a-48fa-a110-ac1fd8922fe9` 核对 frozen 原回执、exact lease
  owner/inode、boot、设备目录 `65099:17721:0:41c0`、nonce 和三项实际文件名，并确认没有
  仍含本次路径的 collector process。随后取回该精确 PCAP/logcat、删除三项原文件和空目录，
  归档 exact quarantine lock。没有扩大清理范围或把原 smoke 改成 passed。
- 取回 PCAP 24 bytes，可由 `/usr/sbin/tcpdump -nn -r` 解析；server log 0 bytes。

证据 `.work/authorized-local-capture-smoke-66f8ac22-1832-4ae9-85c9-e786540db133/` 和
`.work/authorized-local-capture-recovery-2c13286b-698a-48fa-a110-ac1fd8922fe9/`。
不根据 binary 自报版本猜测设备实际文件格式，也不为通过检查放宽成任意轮转文件通配符。

### 改用无轮转、包数/snaplen 有界的单文件检查

`9ee3858c-96d6-4039-8589-305015ee14ed` 使用：

```text
timeout -s INT -k 1 2 /system/bin/tcpdump -i lo -p -s 2048 -U -c 4000 -w <owned>/capture.pcap tcp port 6790
```

- 不使用 `-C/-W`，实际只有 `capture.pcap` 与 owner marker，符合固定文件边界。
- 2.134 秒 exit 124，零 packet/零 drop；24-byte PCAP 可由系统 tcpdump 解析。
- 核对目录 `65099:89378:0:41c0`、owner、精确文件集合、collector process absent、前后相同
  boot，再取回、删除本次文件与目录；lease 正常释放，`cleanup_errors=[]`。
- `-c 4000` 是包数、`-s 2048` 是每包 snapshot 上限；对于此次确认的传统 PCAP 格式，
  `24 + 4000 × (16 + 2048) = 8,256,024 bytes`，不是“4000 bytes”或恒定文件大小。
  若采用其他格式，必须重审预算。2048 snaplen 可能截断大 body，不能宣称完整 HTTP 重组。

证据 `.work/authorized-fixed-local-capture-smoke-9ee3858c-96d6-4039-8589-305015ee14ed/`。
**这只验证空闲 collector 自停、固定文件、取回和清理，不验证实际 packet、故障窗口或场景。**
三份 capability lease 与两次 smoke/独立恢复结束后，当前无活动 phone lease，没有遗留本次
collector、设备目录、Appium session 或 daemon 改动。

## 13. 下一次同窗观测的最小接入约束

本节记录前序预检结束时的接入约束；后续软件实现见 §14，真机组合仍未验证。
当时没有直接开后台 watcher 再重跑场景，待接入的边界是：把 device-local capture
接到现有 `AndroidCloseDiagnostics.start_live/stop_live` 和 runner 的 phone finalize 中，确保
采集器有同一 owner、在 Start 前就绪、失败后能阻止不确定的租约释放。不能仅观察 JSON 文件
出现就启动脱离 runner 的后台进程，也不能先释放手机租约再让 collector 继续运行。

后续若实现，应保持以下最小范围，不创建通用 driver/cleanup 框架或新 UI 协议：

1. 沿用既有 Appium UI 与 diagnostic owner；仅显式启用本轮 device-local 观测。
2. 在原 bound server identity 确认后、Start 前，记录同一 boot、server PID/UID/start ticks、
   collector PID/start ticks、工具 hash、目录 inode/owner 与原 host/device session；没有本地
   capture-ready 回执则拒绝执行 App 场景。
3. PCAP 仅 `lo / tcp port 6790`、非混杂模式、固定单文件、包数/snaplen 与独立时长上限；
   server log 固定原 PID/tag、文件上限和独立停止期限。文件可能含 UI 文本/session/payload，
   仍为忽略目录中的 host-only 敏感证据，不上传。
4. 自停只作用于本轮 collector；不要发送 App/adbd 信号，不开启 persist trace property、
   ptrace/SELinux 改动或动态注入。首次可判别信息优先来自 PCAP + 本地 server log，暂不加
   泛化进程 sampler/Perfetto/线程 dump。
5. 未确认 collector 已退出、文件身份/大小/hash 或 exact cleanup 时保留 quarantine；与原
   failed verdict 分离。恢复后先检查原文件再取回，不用重建 session 或清空日志。
6. 用同一 device session 的目标 method/URI、TCP sequence 与时间窗关联请求；有完整目标
   请求不等于 Java 已读取，TCP ACK 也不等于 handler 完成。只有匹配的响应数据才能证明
   响应跨过对应 TCP 边界。截断、drop、覆盖或 capture 未就绪时，缺包不能作为否定证明。

实现前仍需软件回归覆盖准备失败、Start 前 barrier、早期异常、自停/清理不确定性、租约
归属、原身份漂移及文件格式/容量边界。本次没有为了得到下一条真机证据跳过这些约束；
主故障和设备侧同窗断点依然未验证，不能声称修复完成。

## 14. 设备本地采集 owner：软件接入与回归

本节只做受控软件实现与无设备回归，**没有调用真实 ADB、启动新 Appium/真机场景、安装工具、
重启共享 daemon 或变更 BLE 权限/radio/固件**。正常 UI 仍由官方 Appium Python Client、server
和 UiAutomator2 执行。新增的是现有 SDK/设备工具的项目级生命周期管理，不是重写 Appium、
HTTP、ADB 协议或 TCP 重组。

### 接入、默认关闭与 Start barrier

- 专用 `tools/react_native/bleng_android_device_capture.py` 由现有
  `AndroidCloseDiagnostics` 持有；不新增通用 registry、driver、cleanup framework 或 Just 入口。
- 既有 managed CLI 新增 `--device-local-tcpdump-sha256`；仅在显式 lowercase SHA256、
  `--use-root`、`--close-diagnostics` 同时存在时接受。输入校验先于 phone acquire；普通
  diagnostics 或 root installer 不会自动开启本地 collector。
- `start_device_local()` 在原 App 绑定和 host live logcat 启动后、现有 UI 场景前执行，冻结
  candidate/bound-session 的文件 hash、原 server PID/UID/start ticks/boot、host/device session
  身份，写入 request/binding 回执。采集器 PID/进程树、输出 FD、固定文件和原身份检查全部
  完成后才发布 root ready receipt；任何失败都不得点击 Start。
- 收尾在失败探针之后、DELETE 之前调用 `stop_device_local()`。沿用诊断半预算，不侵占全部
  session/helper/tag 收尾期限；独立 cleanup 仍逐项执行。`require_restored()` 必须同时满足
  component `require_finished()`，不以文件存在、`finish()` 返回或远端部分清理代替关闭证明。

### 资源、取回和失败恢复

PCAP 固定 `lo/tcp port 6790`、非混杂、`-s 2048 -c 4000 -U`，不使用 `-C/-W`。每个 collector
由设备现有 timeout 约束最多 89 秒加 1 秒 grace，并在场景剩余期限内启动；server log 固定
原 PID、`appium:I`、文件轮转与容量。原 SDK 命令/二进制取回各自有字节与绝对时限；复合
root 命令统一 shell-quote。启动不依赖 host queue，不另发 HTTP probe。

目录/nonce、工具 hash、PID/start/UID/PGID/cmdline、文件 stat/hash 及前后检查保持分离。
只向已验证的本轮 collector 发停止信号；未知 child、reparented owned FD、非 quiet 或无法
完整读取的 bounded procfs census 均保留 quarantine，不杀未知进程，也不改 App/adbd。
Census 是有界快照，不是内核级 fork tracking。

独立审查与回归驱动了以下修正，原 red 和中间失败日志保留：

1. 删除 payload 前创建独占 sibling `.recovery` token，绑定 nonce、原目录 identity、server
   与工具；partial-rm、rmdir 或未知文件出现时不会随 payload 一起丢掉恢复依据。
2. 单独记录 `remote_removed` 与 token retirement。远端删除已确认后，即使 boot recheck、
   本地发布或 final guard 失败，重复 finish 也不重发远端清理；原失败仍 sticky。
3. 最后一次树发现后的 late child/owned FD 通过 bounded census 再检查；无远端 attempt 时
   `finish()` 一直返回 None，不返回不存在的 `finished.json`。
4. 集成测试复现持续轮询使失败 JSON 达 10,120,571 bytes，超过原 2 MiB 限制、无法封存。
   现命令 trace 仅保留最近 32 条及总数/省略数/连续 sequence；argv 预览最多 2048 UTF-8
   bytes/16 参数，stderr/error 各 1024 bytes、label 128 bytes，截断附原长度/hash/省略数。
   执行参数和 SDK I/O budget 不变；独立 intent、完整 launch script、身份和恢复 token 回执
   不依赖 tail，也没有提高 bundle JSON 上限。

### 离线证据一致性

`bleng_lifecycle_evidence` 校验 request/binding/ready/stop、原 session/candidate/server/source
hash、时序、component receipt hash、artifact 内容/大小/metadata 和 bounded legacy PCAP。
旧 Close tag mutation intent 只统计其直接子文件，不把嵌套 collector intent 错算为 tag 变更。

Clean terminal 必须包含 owner、两项 PID、PCAP、server log、两项 stderr（仅额外允许
`server.log.1`），并保留实际 pcap/logcat collector identities、bounded trace summary 和完整
recovery-token identity；缺失不能支持 root stop/phone pass。失败清理可以保存 partial facts
及 sticky error，无 stop 时仍可封存 failed/quarantined，而不是丢掉真实失败。

空 PCAP 只表示没有可用 packet 证明；确认 collector/文件清理后不因此强制 quarantine，也
不能因有包或 offline integrity 而升级 runtime/host gate。公共 result 字段和历史无此 opt-in
bundle 的语义不变。

### 本轮软件验证与未验证边界

Test-first 日志分别位于 `.work/device-local-observer/` 和
`.work/device-local-integration-{red,targeted,source-drift}.log`。集成使用真实 collector 和 root
owner，只在实际 SDK/Popen 边界模拟设备，synthetic serial 不会发到真实命令。

- component/PCAP、离线 evidence、CLI/SDK/Appium/Close 诊断采用模块级无设备回归；关键
  lifecycle 用例覆盖 ready-before-Start、unready 拒绝 Start、stop 不确定仍执行 DELETE/host
  cleanup 并 quarantine、source drift 拒绝采用、显式参数前置拒绝和默认关闭。
- 两个新测试模块由现有 canonical `tools-test` discovery 自动收集，不增加第二套发现入口。
- Formal host TypeScript typecheck 通过；`pixi run just react-native formal-host-test` 为
  2 suites / 52 tests 通过。日志 `.work/device-local-owner-host-{typecheck,jest}.log`。
- 最初 integration 组合 45 秒、较大九模块组合 240 秒到期，没有计为通过。已检查各自日志，
  仅结束本次已核验的 synthetic test process group，再按可解释的相关模块/关键用例分组；
  没有重跑全 `tools-test` 或盲目放大同一静默等待。
- 并行分组时旧 `test_mobile_appium.ServerTests.test_explicit_debug_logging_keeps_owned_endpoint_and_timestamped_output`
  曾触发 `Appium deadline exceeded`，该组保留失败；单用例及随后隔离的同组通过，未改动该
  测试/其超时，未把失败归因为已证明的系统原因。日志
  `.work/device-local-validation/{B,mobile-timeout-isolated,B-isolated}.log`。

最终按现有 canonical 模块命名执行的相关回归共 **274 项通过**（不是全 tools-test）：

- A：`pixi run python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_device_capture tools.tests.react_native.test_bleng_device_local_evidence tools.tests.react_native.test_bleng_lifecycle_evidence`：
  117 passed / 29.597s，`.work/device-local-validation/A-final.log`。
- B：`pixi run python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_android_appium tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_android_sdk`：
  150 passed / 5.247s，`.work/device-local-validation/B-isolated.log`。
- 关键 lifecycle：对 `AndroidLifecycleTests` 定向运行六项 `test_device_local_*` 和既有
  `test_complete_fake_sdk_flow_preserves_evidence_app_and_owner_receipts`，7 passed / 102.652s；
  逐项名称和结果在 `.work/device-local-owner-key-lifecycle-final.log`。
- `TestLoader.discover` 只收集、不执行全发现，确认两个新模块的 34/14 项属于原 `tools-test`
  canonical discovery；没有将 collection 当作执行通过。
- `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 均通过；日志
  `.work/device-local-owner-{fmt,lint,mermaid}.log`。作者 Markdown UTF-8/围栏/尾空格/冲突
  检查通过；`git diff --check` exit 0，保留无关三文件的 CRLF warning，未为此改其内容。
  回执 `.work/device-local-owner-diff-check.json`。本地检查当前无活动 phone lease。

**原停顿未修复。** 软件接入不证明设备上的组合 timeout/ulimit 继承、background process tree、
procfs 可读性/census 成本、真实 linktype 或 local logcat 轮转/退出。§12 的空闲两秒检查也不
替代这些组合验证。下一步先验证新 owner 在无 App 场景下的准备/自停/取回/清理，再考虑带
本地 PCAP + server log 的单次同窗诊断；不得直接把新代码当作已获真机通过。

后续已执行两次只到准备阶段的 idle-control，并发现 root PTY、可选 proc children 节点及
legacy exec 退出码边界。两次仍为 failed/quarantined，随后均按原身份独立恢复；软件修正与
真实证据见 [device-local 平台边界报告](2026-11-07-formal-android-device-local-platform-boundary.md)。
