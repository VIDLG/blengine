# Formal Android：device-local collector 平台边界与受控修正

本报告接续 [授权 Appium 诊断与软件 owner 接入](2026-11-07-formal-android-authorized-appium-diagnosis.md#14-设备本地采集-owner软件接入与回归)。
**原 Appium/ADB 主停顿尚未修复，也未在本轮重跑完整 lifecycle。** 本轮发现并修正的是新增
采集器的 root 字节传输和进程快照边界；不能把它们倒推为此前正常 Appium UI 停顿的起因。

## 1. 执行范围与候选

原先提出“无 App 验证完整 owner”不够准确：完整 owner 必须核验真实、正在运行的原
UiAutomator2 server，不能以假身份或跳过 `_server_check()` 来满足测试。因此改用既有
`formal-android-managed-idle-control`，只启动/绑定正式 App 的空闲 UI 与官方自动化服务，
不点击业务 Start/Close，不创建 BLeng owner，不进行 scan/RF。

- 手机 `3ac5cc09`；boot `35df0f9f-6c54-42b0-800c-3c410469b6e7`。
- 同一已记录 APK：`51711779e5e9efd60105d1f312b90e65df0d85f7f8996c1d70e200485e6d9fef`。
- SDK ADB `37.0.1 / 15733141`，普通 NATIVE daemon PID 16841 原样保留；未启用 daemon trace，
  未重启/切换 daemon、USB 或手机。QtScrcpy PID 35648 主进程仍在，不代表投屏连接已获证明。
- helper 三份安装哈希与前序候选相同；只读预检
  `f322c276-e9db-4af2-aef1-51736732aaa3` 确认无旧 App/instrumentation、无 forward、原 helper
  policy/AppOps baseline，正常释放自己的 lease。Inventory 仍为 `automation_admission=pending`，
  不作为后续 live owner binding 的替代。
- 正常 UI 沿用官方 Appium Python Client/server/UiAutomator2。现有 runner 管理同 APK 的明确
  root 安装与临时 helper 设置/恢复，没有修改 BLE 权限、radio、固件或生产 Rust/Native 代码。

预检证据 `.work/device-local-owner-live-preflight-f322c276-e9db-4af2-aef1-51736732aaa3/`。
两次执行都调用现有 `run_android_lifecycle`，只读 host observer 记录进度和 daemon 身份，
没有替换 driver、monkey-patch owner 或补造 UI/设备事件。包装进程 exit 0 不等于场景通过。

## 2. 两次 idle-control：均在 ready 前失败

| 项目 | 首次接入候选 | 仅增加 `su --no-pty` 后 |
| --- | --- | --- |
| run | `668d207f-f5c9-4c56-be93-464f39b5affb` | `b4590273-ee07-4587-a29e-25344f097f8c` |
| collector launch roots | 4857 / 5084 | 11795 / 11890 |
| primary | `invalid collector process identity` | 同一错误 |
| root ready receipt | 未发布 | 未发布 |
| observations / scenario | `[] / not_run` | `[] / not_run` |
| session cleanup | passed，DELETE 有确认 | passed，DELETE 有确认 |
| phone / overall | quarantined / failed | quarantined / failed |
| BLeng Start/Close / RF | not_run | not_run |

两次 App、helper、forward 和原日志标签均由原身份的正常收尾恢复。采集器的进程树未能
确认，`stop-device-local` 保留独立 cleanup error，phone 未误释放。`restored.json` 的标签值
已恢复，但整体 diagnostic `verified=false`，没有把单项恢复当作完整采集通过。

原始 bundle 位于 `.work/formal-android-managed-lifecycle/<run>/`；controller request、软件
文件哈希、原日志在 `.work/device-local-owner-idle{,-no-pty}-request.json`、对应 `*-once.log`
和 `.work/device-local-owner-idle-controller-<run>/`。业务场景未开始，因此这些失败不是原
“Started 后 UI 查询/Evidence click 停顿”的新复现。

## 3. 已证实的字节传输问题：APatch PTY + ONLCR

首轮独立恢复读取 PCAP 时，设备 stat/hash 与 `exec-out su -c cat` 返回字节不一致：

- 设备文件 34,003 bytes；SHA-256
  `e2f7e1466e1574243aba820bdae2c828ddcf60b5a03e97bff5d8995fc77427a8`。
- wire 输出 34,289 bytes，多 286 bytes，含 286 个 CRLF pair。
- **仅作为定位假设**，将 wire 的 CRLF 替换为 LF 后长度及 SHA-256 与设备精确一致。
  该转换结果没有作为 PCAP evidence 使用，也没有覆盖原 wire 文件。
- 实际 `su --help` 报 APatch，明确支持 `--no-pty`；默认 root 会话的 stdin/stdout 均为 TTY，
  `stty -a` 显示 `opost onlcr`。不能把 adb `exec-out` 当作 root 子进程无 PTY 的保证。

证据 `.work/device-local-owner-independent-recovery-45add352-51b7-4172-99ea-a7310c769c8b/`
中的 `pcap-exec-out.raw/json`、`crlf-hypothesis.json`、`su-help.json` 与 `su-tty.json`。
该次恢复在 binary hash guard 处拒绝，未删除 payload、未归档 lease；原失败完整保留。

修正使用 APatch 自带 `su --no-pty -c`，不自行处理 termios、不归一化 binary/proc frame、不
回退到默认 PTY。原始 PCAP 随后在无内容修补的情况下对上设备 size/hash。这是该传输修正的
真实设备证据，但第二次 idle-control 仍失败，证明它不是完整 owner 通过的充分条件。

## 4. 第二个边界：可选 proc 节点与 ADB 退出码

旧 `_process()` 无条件读取 `/proc/<pid>/task/<pid>/children`。恢复后只读原 framing 格式，
实际 PID 1 元数据包含该节点不存在的错误。两次 collector root 后来均已不存在，未获得原
collector 的完整同窗失败快照；不能把健康 PID 1 的信息称为原 collector 故障栈，也不猜测
节点缺失的 kernel/OEM 原因。

该证据确认代码依赖了此设备不保证提供的节点。修正改用官方 `ps -A -o PID,PPID`，完整表
在既有 64 KiB 回复预算内，严格校验 header、十进制两列、唯一 PID、最多 4096 行及目标 PID
存在，再由 PPID 推导 children。原 stat/status/cmdline 前后身份、UID/PGID、未知子进程和
owned-FD census 检查保留；读取失败不能变成空 children。

独立只读 probe `95a0897f-a28e-483d-b1be-17548a2d2aee` 确认：

| probe | 实际结果 |
| --- | --- |
| `/proc/1/task/1/children` 可读性 | exit 1，不提供该节点 |
| `ps -A -o PID,PPID` | exit 0，header `PID PPID`，910 行进程，10,932 bytes |
| `adb exec-out ... exit 37` | client exit 0 |
| `adb shell -T ... exit 37` | client exit 37 |
| `shell -T / su --no-pty` binary sentinel | 精确 `00 0a 0d 0a ff`，5 bytes |

这是有限的工具/格式观察，不是新 App/session 运行。证据
`.work/device-local-platform-format-probe-95a0897f-a28e-483d-b1be-17548a2d2aee/`；前后 boot
相同，lease 正常释放。Binary collector readback 现使用官方 ADB `shell -T` 加
`su --no-pty`，保留原字节及远端退出码，避免 legacy `exec-out` 把远端失败映射为 client 0。
没有新增 smart-socket、HTTP 或自定义退出码协议。

## 5. 软件回归补强：执行真实生成脚本，而非只模拟 frame

严格 SDK mock 原先直接构造协议 frame，没有执行实际 `printf/cat/ps` 组合。改成 PS 表后，
新增本地隔离测试执行组件真正生成的 POSIX shell 片段：所有 `/proc` 路径替换为本次临时
fixture，`ps` 是只返回固定 metadata 的 shell function；不调用 adb/root 或真实 `/proc`。

该测试先 red，捕获了新脚本的分隔换行缺失：cmdline 以 NUL 结尾而不是 LF，`ps` 结果本身
以 LF 结尾。随后只修正 emitter 在两者之间的 delimiter，不放宽 parser 或改原始字节；
green 后仍保留 CRLF frame、错误 PID/PPID table、binary 变形/远端非零退出的拒绝回归。

实现/测试分别位于 `tools/react_native/bleng_android_device_capture.py` 和邻近
`tools/tests/react_native/test_bleng_android_device_capture.py`。主 lifecycle 测试仅更新 SDK
边界以接受显式 `shell -T`，没有改变 UI 成功、身份或 lease release 断言。

## 6. 独立恢复与 PCAP 的真实含义

- 首轮独立恢复 `2d6a71c5-1534-4b67-a2e7-d3903d460533`：原 collector root 已不存在，官方
  `lsof` 未发现 exact payload 文件 holders；核对原 lease inode/owner、nonce、目录、文件
  metadata/hash、原 session/helper/App/tag 收尾。精确删除 payload、退休 sibling token 后
  归档 lock。未发送 collector/App/adbd 信号，未重启或改变 radio。
- 第二轮独立恢复 `9fcac699-79ac-4ad6-87a4-df7277fc0eaf`：同样仅恢复本次精确资源，使用
  `shell -T / su --no-pty` 取回并核验原字节，完成 exact cleanup 与 lock 归档。
- 取回的首轮 PCAP：183 records / 9 truncated packets / linktype 1；第二轮为
  182 / 8 / 1。tcpdump 各自报告 0 kernel drops，但 packet count、截断或无 drop 不能证明
  全窗口覆盖、UIA2 handler 完成、原主请求正常或 RF 通过。
- 两次 root ready 未通过；PCAP 包含失败后的 status/PNG/XML 等诊断流量，不能将其当成已
  开始的业务场景证据。原 failed/quarantined bundles 保持不变。

恢复证据为 `.work/device-local-owner-independent-recovery-<recovery>/`。当前物理状态已恢复，
不宣称 native resource-zero；“正常 DELETE 有确认”与“从未创建业务 BLeng owner”是不同事实。

## 7. 当前结论

本轮证实并修正新增诊断的三个软件边界：默认 root PTY 的字节变形、可选 proc children
节点依赖，以及 binary `exec-out` 的退出码缺口；另用实际生成脚本回归修正 delimiter。
未改 Rust Close、locator、UIA2 idle/read timeout 或 SDK 请求重试策略。

**完整 collector 的最终组合仍未获真机通过，原主停顿也未解决。** 最新的无 PTY + shell-v2
+ PS-table emitter 尚未重新执行 App 场景；未知真实进程树、自停、census 成本和最终 clean
terminal 仍需保留为 gap。不得把工具单项 probe、软件 green 或取回 PCAP 改写成 owner/
lifecycle/Phase/ABI exit。下一次应先收敛此最小采集 owner，而不是恢复盲跑完整 lifecycle。

## 8. 本轮验证

Red/green 日志包括 `.work/device-local-no-pty-{red,green}.log`、
`.work/device-local-process-framing-{red,green}.log`；最终相关分组共 **279 项通过**，不是完整
`tools-test` 执行或覆盖率声明：

- `pixi run python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_device_capture tools.tests.react_native.test_bleng_device_local_evidence tools.tests.react_native.test_bleng_lifecycle_evidence`：
  125 passed / 30.947s，`.work/device-local-platform-final-A.log`。
- `pixi run python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_android_appium tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_android_sdk`：
  150 passed / 5.371s，`.work/device-local-platform-final-B.log`。
- `AndroidLifecycleTests` 的 ready-before-Start、unready 拒绝、stop quarantine、source drift
  四项定向回归：4 passed / 71.822s，逐项名字在 `.work/device-local-platform-final-runner.log`。
- `pixi run just react-native formal-host-lifecycle-verify` 对 §2 两份原 bundle 分别通过
  integrity，`host_gate_upgraded=false`，仍保留 failed/quarantined/not_run；日志
  `.work/device-local-platform-verify-{668d,b459}.log`。
- `pixi run npm --prefix tests/consumers/react-native-bleng run typecheck` 通过；
  `pixi run just react-native formal-host-test` 为 2 suites / 52 tests 通过。日志
  `.work/device-local-platform-{typecheck,jest}.log`。

上述软件、文件完整性和工具 probe 均不能替代最新完整 collector 的物理准备/收尾证明。
