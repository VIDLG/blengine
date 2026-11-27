# Formal Android：USB 写完成边界的进一步离线核对

接续[原主停顿与双 backend trace](2026-11-07-formal-android-authorized-appium-diagnosis.md#7-授权-packet-trace主请求已获-remote-adb-ack)。
本轮仅读取既有 trace/sample 与官方源码，没有执行手机、ADB、Appium server 或 RF；没有升级 driver。
**原 BUG 尚未修复。本轮没有足以支持根因代码修正的证据。** 不把前序 Client 升级或 collector 修正算作主停顿修复。

## 1. 新补齐的源码边界

沿用既有 AOSP revision `1cf2f017d312f73b3dc53bda85ef2610e35a80e9`，仅补齐三个此前本地缺失的文件：

- [client/usb_libusb_device.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/client/usb_libusb_device.cpp)
- [services.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/services.cpp)
- [socket_spec.cpp](https://android.googlesource.com/platform/packages/modules/adb/+/1cf2f017d312f73b3dc53bda85ef2610e35a80e9/socket_spec.cpp)

原文保存在 ignored `.work/adb-write-boundary-official-*`，URL、revision、size、SHA-256 在
`.work/adb-write-boundary-source-manifest.json`。这不是已安装 SDK binary 或设备 adbd build 的源码 attestation。

`usb_libusb_device.cpp` L98–129 的 `Write` 日志在函数入口；随后对 header/payload 同步调用
`libusb_bulk_transfer`，检查返回码和完整 transferred length，失败返回 false。
既有 `client/usb_libusb.cpp` L117–139 的单 writer 只有在该函数返回 true 后才处理下一包。
因此在源码对应运行实现的前提下，**同一 writer 的下一次 Write 入口证明前一次 header/payload
已在 host libusb API 层成功完成**，比单纯 enqueue 更强。

例外：L121–126 的可选 ZLP 分支没有检查返回值，不能泛称所有 transfer 均被严格检查。
本段目标 endpoint size 为 512，相关非空 payload 均非其整数倍，空 payload 提前返回；此例外
不改变下表推导，也没有证据支持以它解释本案。

## 2. LIBUSB 故障窗口的完成上界

Trace 为 `.work/authorized-packet-trace-d1d8eb00-fb97-4fb0-bc09-aa501a3944ed/adb-packets.raw.log`。
同一 writer TID `936381` 从主 WRTE 到 capture 结束共有 16 个目标包的 Write 入口；前 15 个有后继入口。
下面是**成功返回的最迟上界**，不是直接记录的 transfer completion timestamp：

| 包，host/remote IDs | Write 入口（行号 / host 时间） | 由后继入口证明此前调用已返回 |
| --- | --- | --- |
| 主 WRTE `26d/4480`，407 bytes | L70732 / 10:20:04.071 | L70826 / 10:20:14.928；另有 L70734 matching remote OKAY |
| logcat ACK `32f/44c0` | L70904 / 10:20:16.389 | L70947 / 10:20:24.085 |
| fresh TCP OPEN `590/0`，9 bytes | L70947 / 10:20:24.085 | L71053 / 10:20:26.137 |
| fresh boot shell OPEN `595/0`，42 bytes | L71053 / 10:20:26.137 | L71066 / 10:20:29.158 |
| capture 最后 getprop OPEN `5b7/0`，66 bytes | L71757 / 10:21:16.888 | 无后继入口，完成未证明 |

这不支持“一个同步 host write 从主请求起一直 pending、挡住后续全部包”的解释。
但 **host API success 不等于设备 adbd endpoint read、ADB packet dispatch 或 TCP 6790 收到请求**；
不能把 fresh OPEN 与具有额外 remote ADB OKAY 的主 WRTE 混为一谈，也不把本推导扩大到 NATIVE backend。

同窗 sample `.work/authorized-appium-host-observer-d1d8eb00-fb97-4fb0-bc09-aa501a3944ed/adb.sample.txt`：
L12 开始时间 10:20:26.174；L116–122 的 writer `936381`，868 个样本在 condition-variable wait；
reader `936380` 在 L104–115 的 libusb read/event wait。这与 writer 返回外层队列等待一致，但直接
caller 未符号化，不单独拿它充当 completion 回执。更强依据是上表同 writer 后继 Write。

## 3. logcat 背压和 cleanup 不是已证实的主因

两次最后 logcat WRTE 都是 131 bytes，host socket 已写出全部并产生 ACK：

- NATIVE trace 的 L63500–63504：queue=131、rc=131、发送 ACK。
- LIBUSB trace 的 L70899–70904：同样完整写出，并有 writer 的 ACK Write 入口。

没有该包因 host logcat 客户端不消费而扣住 ACK 的证据。不能因此删掉日志采集来宣称修复。
两轮后续 logcat 关闭均先有 host local EOF；Appium 的 instrumentation SIGTERM 与 trace daemon
退出均属于既有 cleanup，不倒推为 primary crash。完整检查未发现主窗口的 transport disconnect
日志；缺日志也不是物理链路健康证明。主 ACK 后还有旧流 CLSE 和该 logcat WRTE，仍不能描述成
USB IN 当场完全停止。

## 4. 剩余缺口与有信息价值的下一步

补齐 `services.cpp` L77–89 与 `socket_spec.cpp` L186–205，确认 A_OPEN 服务建立还经过 socket
service/connect 路径。仅有 host API completion 无法知道设备停在读取、dispatch、connect、TCP
还是 UIA2。该源码路径只是调查边界，不是设备故障栈，不据此定因为 connect/backlog 阻塞。

最小新增事实是：**同一故障窗口，目标请求/响应是否出现在设备 loopback TCP 6790，以及原 server
PID 的完整请求入口日志是否出现。** 现有 tcpdump + device-local logcat 方案有相应判别价值：

| 同窗阳性观察 | 可以收窄到 |
| --- | --- |
| 目标请求的实际 TCP 字节 | 请求已跨到设备 TCP 边界；TCP ACK 不等于 Java 已读取 |
| matching `channel read` / command 日志 | 已进入 UIA2 FullHttpRequest / command 入口；不等于处理完成 |
| matching 响应实际 TCP 字节，host 无响应 | 调查响应之后的 adbd 读取、ADB 回传、USB IN 和 host 接收 |
| 未观察到请求/入口日志 | 单纯缺失不足以区分 USB 未交付、adbd 未读、未转发或采集缺口 |

否定判断必须先证明窗口覆盖、未提前触发包数/时限、相关字节未被 snaplen 截断、日志没有被轮转覆盖，
并按原 session、method/URI、body、TCP 四元组/sequence 与顺序关联。零 kernel drops 不等于这些条件全满足。

[此前两次 idle-control](2026-11-07-formal-android-device-local-platform-boundary.md#2-两次-idle-control均在-ready-前失败)
都在 collector ready 前失败，没有业务 Start/Close；取回 PCAP 含后置诊断，不能当原主故障现场。
在不重复真机的约束下，现有离线材料已不能补这个事实，继续同样 host trace 或换 backend 没有新增判别力。

若允许最小设备验证，应先仅验证最新 collector 的 ready→有界空闲→退出/原字节取回→exact cleanup，
不执行业务 Start/Close、scan/RF，不重启 daemon、不升级 driver。任何 ready/身份/收尾失败就停止并保留
primary/cleanup/quarantine，不能自动重试业务。该准备门通过后，才另行界定一次目标请求的同窗观测。
若最终仍只有“host 写成功、设备 TCP 无目标字节”，需补设备 USB OUT 与 adbd read/dispatch 的关联边界，
不能继续同配置重复复现或据此猜测修复。

本轮只更新调查报告，不修改 Rust Close、locator、timeout、driver 或 collector 生产代码。

## 5. 本轮验证

- 离线断言核对三份新增 source 的长度/hash、原 LIBUSB trace hash，以及主 WRTE 起 16 个目标
  outbound packet 与同 writer 的 16 次 Write 入口长度对应；通过。没有将最后一次入口断言为完成。
- 两份更新 Markdown 的 UTF-8、尾空格、围栏、冲突标记检查通过。
- `pixi run --locked just fmt`、`pixi run --locked just lint`、
  `pixi run --locked just docs-check-mermaid`、`git diff --check`：均通过。
- 日志 `.work/adb-write-boundary-{validation,fmt,lint,docs,diff-check}.log`。
  本轮未重跑 SDK/设备测试，不把前序测试数字宣称为新的主 BUG 验证。
