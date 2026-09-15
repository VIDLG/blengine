# Apple 与 nRF52840：Host bring-up 与 RF 阻塞记录

## 结论

2026-09-10 至 09-11 本地运行完成 nRF 的 macOS Host-only 配置、BTP doctor 和广播控制，并完成 Apple 外部 profile HIL 适配。**macOS 和 iPhone 对 nRF 的完整 RF gate 均未通过。**

| 边界 | 本次结果 |
|---|---|
| nRF USB CDC ACM | 可枚举，串口 `/dev/cu.usbmodem31201`，serial `DBDBE94A2CED8C63`，VID:PID `2fe3:0004` |
| Mac nrftest Host | 固定 AutoPTS 加受管 Darwin socat 补丁；BTP CORE/GAP/GATT doctor 通过 |
| nRF 动态 GATT | 首轮 built、后续 attached/reset 初值；10 attributes、service handle 33、CCCD handle 38 |
| nRF 广播 | 四轮 BTP fixture readiness，独立 FD00… 服务；ready 不是 RF pass |
| macOS Central | HIL 编译与 ad-hoc 签名通过；TCC 提示未回应，初始化超时，未进入 RF |
| iPhone Central | 两轮 development signing、install、launch 通过；PoweredOn + Authorized；扫到 nRF，连接超时 |
| GATT RF / characteristic IO | 两平台均未到达；不宣称通过或已验证 profile 的实际 RF 映射 |
| Notification / Indication / RSSI read / recovery | 未执行，不属于新增外部入口已验证范围 |
| 收尾 | `host-stop → host-doctor → host-stop` 全部通过；最终 BTP Advertising=false，Host clean close |

没有重新烧录、复位 nRF 或安装 Zephyr SDK。锁文件中的 Zephyr revision 不证明板上二进制身份；本次没有读取 firmware image SHA-256。

## Candidate 与拓扑

- BleHub base：`ce0c5d54dcd7a337c0c688bfcff7b08b44d89590`，加原有未提交工作及本次 Apple HIL/host 适配。不是该 commit 的干净 candidate；原有用户改动未覆盖，不把旧 RF 结果归入本次。
- nrftest base：`d64b80ca5bed820243fdb663ef91e1fd86a78ef1`，加本次 Host-only setup、Darwin 补丁校验和 stop 入口。
- AutoPTS base：`54e81c7f3495bce72e5f688e9c996b85b8272799`，加 nrftest tracked/hashed Darwin socat 参数补丁。普通 gate 严格核验 base 与完整预期 diff，不自动修改上游。
- Mac：macOS 26.2 Apple Silicon，内置 `BCM_4388C2` controller On；图形会话解锁。
- iPhone：`iPhone1`，iPhone 14，iOS 26.6.1，Developer Mode enabled；CoreDevice `9BA077D6-EE9A-5BB2-8BBC-4D263CFE328E`。
- nRF 是 USB/BTP 控制的独立 Peripheral，不是 Mac 的 Bluetooth controller。Mac/iPhone 用各自 CoreBluetooth 与 nRF 进行 RF 通信。
- 两项目的运行代码无相互 import、仓库路径或 provider 进程依赖。本次 ignored 外部协调器只通过各自 Just 命令启动两方，并在 nRF readiness 后启动 Central；它不是发布组件或新的 production backend。

## 本次可复用入口

用法与受支持 profile 子集见 [`EXTERNAL_FIXTURE.md`](../../../tests/hil/apple/support/EXTERNAL_FIXTURE.md)。Apple 入口只运行 Central，不自动启动/停止外部 Peripheral。

在 `nrftest` 根目录（首次配置 ignored `nrftest.local.toml` 后）：

```sh
pixi run --locked just setup-host
pixi run --locked just host-doctor
pixi run --locked just btp-gatt-rf-fixture "" profiles/blehub-nrf-basic-v1.json 600
```

等待 `NRFTEST_GATT_FIXTURE_READY` 后，在 BleHub 根目录、另一个终端运行其中一个 Central：

```sh
pixi run just apple rf-macos-external /Users/vision/projects/nrftest/profiles/blehub-nrf-basic-v1.json io 10 ab
pixi run just apple rf-ios-external /Users/vision/projects/nrftest/profiles/blehub-nrf-basic-v1.json 10 ab
```

两个 Central 必须顺序运行。每轮需重新启动 fixture：其 attach 路径恢复初值 `00`。本次预定 IO 值是 `00 → 10 → ab`，以适配 resident Tester 的单字节写 buffer；没有修改 Android 默认五特征及 `102030/abcd` 断言，也没有让 nRF 冒用 Android 的服务 UUID。

基础 fixture 等待 connected/disconnected，不适用于把单独 scan-only 的 Central 退出视为 fixture RF pass。失败后先中断 fixture 并确认进程释放串口，再执行：

```sh
# 在 nrftest 根目录
pixi run --locked just host-stop
```

该命令只确认 BTP 停止广播与 Host close，不请求断开既有连接，不证明空口永远静默。

## 实机证据

原始报告均为本机 ignored 文件，不随 Git 自动分发。日期包含 UTC 与本地跨日，不代表不同 candidate。

### nRF Host

在 `/Users/vision/projects/nrftest/` 下：

- 初次 BTP doctor：`.work/reports/host-doctor/20260910T155917.261162Z-7d3dc16b7b6d47699498ed6b198f5421.json`，`pass/built/clean`。
- 收尾重新接入：`.work/reports/host-doctor/20260910T162052.977996Z-7a59d20f18c74b74ad9ed533fc8d0476.json`，`pass/attached/clean`。
- 最终停止广播：`.work/reports/host-stop/20260910T162055.275651Z-7bcb9fbe0ba449f9980ff514ad464680.json`，`advertising_false_confirmed=true/clean`。
- profile 原始 LF 文件 SHA-256：`2c315af0092cb67ebcdd257913ec96a877edae648a0993f2be707dd725e26924`。

Host-only 配置以及 socat 根因和固定补丁的详细来源在 nrftest 的 `docs/research/2026-09-11-macos-host-bringup.md`。

### macOS

结果文件（相对 BleHub）：

- `.work/apple-macos-runtime/io-external-6454383ef1404fad930fa5d841757199-result.txt`
- `.work/apple-macos-runtime/authorize-48db6943ca804c678541aebe36aa9b3f-result.txt`
- `.work/apple-macos-runtime/io-external-aea84c9d6cec4dea8e34ad2c853b2e69-result.txt`

均为 `timed out waiting for CoreBluetooth initialization events`。系统日志明确显示：

```text
Failed to match existing code requirement for subject dev.blehub.macos-initialization-probe
AUTHREQ_PROMPTING ... service=kTCCServiceBluetoothAlways
```

日志证据指向重新 ad-hoc 签名后的 TCC 授权等待；未伪造授权、修改 TCC 数据库或将初始化超时算作 nRF RF 故障。用户需在可见提示中允许 Bluetooth。源码仅在 hash 变化或签名无效时重签；本机多次运行实际发生了 rebuild/re-sign，仍有授权身份不稳定风险。

一次前置验证曾并行运行两个会 stage 相同 package 的 Just recipe，造成 lipo 临时文件冲突。随后改为串行并成功完成 package gate；该次错误是本次执行编排问题，不是 RF 结果。后续 Apple prepare/build 不应并发运行。

### iPhone

- `.work/apple-ios-runtime/922545fe05be4c7fbe4bc67df96c4dbf-result.txt`
- `.work/apple-ios-runtime/d08a65f3a4a94f2b924dfc1085ec47ed-result.txt`

两轮均完成：

1. 真实开发签名构建、安装、launch。
2. 初始化 `PoweredOn + Authorized`。
3. provider 服务过滤扫描，观察到 `fd000000-0000-4000-8000-000000000000`，`connectable=true`；第二轮也得到 local name `BleHubNrf52840`。
4. 扫描 stop 与终端事件。
5. command 4 仅到 `Connecting`；未得到 `Connected`/成功 terminal，最终测试层连接截止时间到达。

第一轮广告 RSSI `-98 dBm`；第二轮 `-99/-98 dBm`。信号弱是已观察事实，但**未证明它就是连接失败的根因**；还需改善距离/遮挡并重试、观察 nRF 连接事件。不能通过延长 timeout 或削弱成功条件将该结果转为 pass。

两轮结果都明确标记 `apple_ios_rf_io=failed`、`apple_ios_corebluetooth_rf=failed`；nRF 日志未见 connected 事实。由于在连接前停止，未验证 GATT discovery、读写或后续 ledger/静默成功。

## 静态与构建验证

- BleHub `pixi run just lint`：通过，含 workspace Clippy、Rust 格式检查、Ruff 和 fixture isolation。
- Apple host profile/dispatch 单测：4 tests 通过。
- Apple Swift RF 确定性测试：16 tests 通过。
- macOS HIL build、iPhone generic unsigned build：通过。
- Apple production 单 native-image symbol/XCFramework slices gate：实测 Just prepare 中通过。
- iPhone 真机 development signing/build/install/launch：两轮通过，未执行人工 keychain 解锁命令。
- nrftest fmt/lint、setup/patch 与 Host-stop 定向测试：通过。
- nrftest 完整测试：179 passed、1 skipped、1 failed。唯一失败为既有 `test_profile.py:97` 原始 SHA 的 LF/CRLF 敏感断言；未改测试以掩盖问题，也未宣称完整测试套件通过。

## 剩余工作

1. Mac 允许当前 HIL App 的 Bluetooth 请求，再运行同一外部 profile gate；必要时另行处理稳定开发签名，不能绕过 TCC。
2. 将 iPhone 靠近 nRF，避免天线被金属遮挡，重新观测 RSSI 与连接；若仍失败，再分别调查固件/radio/controller 的实际链路事实。
3. 成功连接后才验证四特征 GATT、精确单字节读写、disconnect 和 ledger；Notification/Indication/故障恢复需要额外外部编排，不在本次通过范围。
4. 已知 AutoPTS pending-command/stop 无硬截止风险未解决；本地协调器超时终止不等于底层 teardown 已有强保证。
