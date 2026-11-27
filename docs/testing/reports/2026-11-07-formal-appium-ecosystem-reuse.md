# Formal Appium：官方 Client 升级与离线生态复用

本轮按用户授权复用成熟生态，不重跑真机、不升级外部 server/driver、不改变共享 ADB daemon。
接续 [Appium/ADB 原停顿诊断](2026-11-07-formal-android-authorized-appium-diagnosis.md) 与
[device-local 平台边界](2026-11-07-formal-android-device-local-platform-boundary.md)。
**原主停顿未修复；完整 collector 仍缺最新组合的真机通过证据。**

## 1. 版本不是一整个“最新 Appium”

本次前序 registry/PyPI 核对结果保存在本地 `.work/ecosystem-{appium-server,uia2,xcuitest,appium-python}-latest.json`。
它们是该次查询的记录，不是普通运行时动态选择 latest 的依据。

| 组件 | 本轮开始时 | 该次查询 latest | 本轮操作 |
| --- | --- | --- | --- |
| Appium server | 3.9.0 | 3.9.0 | 不变 |
| Appium Python Client | 5.3.1 | 6.1.5 | 精确锁定并更新到 6.1.5 |
| UiAutomator2 driver | 8.7.3 | 9.0.1 | 不变 |
| XCUITest driver | 12.22.4 | 13.0.7 | 不变 |

Client 经 `pixi update appium-python-client` 定向更新；`pixi.toml` 使用 `==6.1.5`，
`pixi.lock` 对本轮开始时的 dirty baseline 仅增量更新该包，保留已有 pytest/scapy/cmake 等改动。
实际 Python 3.13.15、Selenium 4.51.0、typing-extensions 4.16.0、urllib3 2.7.0 均不变。
6.1.5 的安装包元数据要求 Python >=3.10、Selenium >=4.51.0,<5.0、typing-extensions ~=4.16，
当前环境满足。参考 [官方 Client 文档](https://github.com/appium/python-client#compatibility-matrix)，
精确依赖要求以本次安装包为准。

server/driver 仍来自显式外部 entry/Appium home，没有在本轮新增受管安装链，也没有把它们
宣称为 repo 已锁定的依赖。未来 driver 升级须独立核对 breaking changes、helper APK/hash、
准备基线与真实设备行为；不能混入普通检查或从 client 软件通过推断。

## 2. 使用官方 API，不重写 UI 自动化

生产正常 UI 仍经官方 `webdriver.Remote`、UiAutomator2/XCUITest Options、Appium locator 与
WebElement API；不 fork driver，不自行实现 UI WebDriver 协议。6.1.5 不需要修改生产调用代码。

新增 SDK 回归在实际 HTTP transport 处隔离，保留安装 SDK 的真实 session 创建、capabilities
序列化、element 解码、点击、错误映射与 quit；创建失败不重试、不回退。邻近回归继续验证显式
命令 timeout 和禁用隐式 transport retries。文件：

- `tools/tests/react_native/test_mobile_appium.py`
- `tools/tests/react_native/test_android_appium.py`

项目保留的 child/session owner 是租约、身份、deadline 和失败收尾责任，不是另一套 UI 框架。
未直接用通用 `AppiumService` 替掉该 owner，也未因升级改变 prepared profile 的
`skipLogcatCapture`。正常日志能力与故障后独立观察不同：不能让全部诊断再排队到已阻塞 session。

## 3. PCAP 复用已有 Scapy，保留严格 evidence policy

`tools/react_native/bleng_android_device_capture.py::validate_pcap` 改用已有依赖 Scapy 的
`RawPcapReader` 解码 endian、timestamp precision 与 record metadata/body，不再自行解码
每个 packet header。只读 `BytesIO`，不打开设备、不修改原文件、不重组 TCP/HTTP。

安装的 Scapy 2.8.0 源码与 [上游实现](https://github.com/secdev/scapy/blob/master/scapy/utils.py)
确认：它会把短 record header 当作 EOF，并容忍部分缺失 body；因此不能直接以 iterator 正常结束
证明文件完整。项目的小型 policy wrapper 继续拒绝：

- 非四种 classic PCAP magic；gzip/pcapng 在进入 reader 前拒绝，不隐式解压/转换；
- 非 2.4、非零 reserved fields、snaplen 非 2048、未知 linktype 或上位 FCS 扩展；
- 文件超过 8,256,024 bytes、record 超过 4000、部分 record header 或 body；
- 超范围 timestamp fraction、caplen > 2048、caplen > wirelen。

Scapy 不保留 global version/reserved fields，项目仅为该 policy 读取这四个字段。合法的
snapshot 截断仍被计数，不与损坏 record 混淆。没有为了使用库而放松证据门槛。

先补齐四种 endian/precision 的完整结尾、长度/数量/格式边界回归，在旧实现通过后替换 parser，
再确认新实现通过。这是行为保持重构，不宣称发现了旧 parser bug 或完成了故障 red→green。
测试位于 `tools/tests/react_native/test_bleng_android_device_capture.py::PcapTests`。

历史恢复 PCAP 离线重放也与原记录一致，原字节未改：

| 恢复 run | bytes | records / snapshot truncated / linktype |
| --- | --- | --- |
| `2d6a71c5-1534-4b67-a2e7-d3903d460533` | 34003 | 183 / 9 / 1 |
| `9fcac699-79ac-4ad6-87a4-df7277fc0eaf` | 31939 | 182 / 8 / 1 |

路径与 SHA-256 见 `.work/ecosystem-pcap-replay.log`。这只是解析兼容，不提升原 failed/quarantined
场景，也不证明 HTTP delivery、UiAutomator2 handler completion 或 RF。

## 4. 验证与未完成项

本轮实际执行：

- 升级前 Android/mobile SDK 49 项通过；升级后
  `pixi run --locked python -m unittest tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_android_appium tools.tests.react_native.test_ios_appium`：60 项通过。
- `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_device_capture tools.tests.react_native.test_bleng_device_local_evidence tools.tests.react_native.test_bleng_lifecycle_evidence`：127 项通过 / 28.231s，`.work/ecosystem-regression-A.log`。
- `pixi run --locked python -W error::ResourceWarning -m unittest tools.tests.react_native.test_bleng_android_close_diagnostics tools.tests.react_native.test_run_bleng_android_lifecycle tools.tests.react_native.test_android_appium tools.tests.react_native.test_mobile_appium tools.tests.react_native.test_ios_appium tools.tests.react_native.test_android_sdk`：161 项通过 / 6.578s，`.work/ecosystem-regression-B.log`。包含上述 60 项，不重复相加。
- `pixi run --locked just react-native host-check`、`pixi run --locked just react-native host-test`：通过，后者 17 suites / 621 tests。
- `pixi run --locked npm --prefix tests/consumers/react-native-bleng run typecheck`、`pixi run --locked just react-native formal-host-test`：通过，后者 2 suites / 52 tests。
- `pixi run --locked just fmt`、`pixi run --locked just lint`：通过，日志 `.work/ecosystem-{fmt,lint}.log`。

更广检查存在超时，不能计为通过：`just react-native tools-test` 为 120s；单独
`tools.tests.react_native.test_bleng_android_candidate` 为 60s；包含 automation/session 的较广
unittest 分组为 60s。曾停留的一个 candidate 用例单独执行通过，但尚无稳定根因，不把超时
归因为 Client 6.1.5，也没有通过修改生产工具或延长 timeout 掩盖它。未遗留本轮测试进程。
Pixi 对现有 urllib3 2.7.0 的 `socks` extra 有非致命求解警告，未扩大本轮依赖修正。

本轮未操作手机、radio、BLE 权限、固件、ADB server 或外部 driver。没有新 RF/runtime verdict。
下一步仍须分别解决工具链来源锁定与受控 collector 准备；原停顿的因果证据缺口不能由生态迁移替代。
