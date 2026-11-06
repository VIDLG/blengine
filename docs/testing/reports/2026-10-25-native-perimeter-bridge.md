# Native 外围收口：Apple v2 桥接与 Android 权限前置

日期：2026-10-25

## 结论与本轮边界

继 [第一轮外围接线修复](./2026-10-25-perimeter-review-readiness.md)，本轮接入 Apple 常规 native v2 smoke 的实际 host owner，准备对应预构建产物，并检查 Android direct Kotlin native smoke 的真实设备前置。

**工程接线、构建和无硬件回归已通过；新 native 路径的真实 install/launch/SDK marker/RF 行为尚未通过验收。** Android 的明确阻塞是新 consumer 尚未取得附近设备权限。没有用编译/synthetic 通过冒充 RF，没有运行长时间压力或重复已定界的 iOS CCC disable REJECT。

按用户选择：先把外围及适用最小平台验证收口，再开始 `crates/blehub`、`crates/bleng` 人工 review；不再催用户并行 review。最终 ABI freeze、同候选终验和长期压力仍按 [Plan §14](../../../.plan/PLAN.md) 后置。本轮前后两个核心 crate 的 67 个文件逐文件 SHA-256 相同。

## 1. Apple 常规 native v2 已有明确入口

新入口与 owner：

- `pixi run just apple rf-native-smoke`，详见 [Apple 执行指南](../apple.md#explicit-prebuilt-native-smoke)。
- [tools/apple/native_hil.py](../../../tools/apple/native_hil.py) 是两平台共享的具体 native smoke owner，不是通用注册/驱动框架。
- 复用现有 macOS/iOS production HIL App。`io` 与 `notification` 分为两次独立 session，不复制 RN 场景库，也不恢复全部历史 native matrix。

每场明确要求平台、设备、BTP port、contract、profile、预构建 App 与场景；无隐式 build、provision、固件 reset/flash、系统 radio 或权限修复。iOS 不使用 `--terminate-existing`。

### 准入与清理

- 使用同一个 leased `DirectFixture` / BTP session，冻结 canonical v2 输入，live attest 匹配后才允许安装/启动。
- 在启动前固定 firmware boot nonce；install/launch、notification trigger 与 disable 放行等 mutation 前再次检查当前 boot，漂移后拒绝对新 boot 做操作或清理性 mutation。等待结果的循环不持续高频查询固件。
- App 以 opt-in helper 发布 run、boot、PID、bundle 和 executable。结果必须匹配本次身份。iOS cleanup 还需该 live boot 对新 challenge 的响应及当前 SDK process row；未知启动结果或 PID/boot 漂移不触发猜测式 terminate。
- Mac 只拥有本次 `Popen` 子进程；fixture cleanup 仅通过自己的 lease/session 退休与停止广播，无 provider-wide stop。
- iOS 使用与 RN scan runner 相同的原子目录锁，持有至 cleanup；另外只读拒绝已有 radio owner/restoration-pending。radio 检查不是预留，其并发获取仍存在窗口；报告如实记录合作排他边界，不宣称全局 phone/radio 锁。物理执行仍必须独占安排。
- 成功 iPhone 结果页保留。再次运行另一 scene 前须显式关闭前一 probe，不默默接管；两场有独立身份和证据。

### 实际判据保持在原 probe

两平台把 immutable profile 的 local name 作为 `--scan-name-prefix`，与 service UUID 组合过滤；结果必须回显准确前缀。没有硬编码地址/ATT handle，也没有改 production ABI。

`io` 保留原生 scan/connect/discovery、initial read、两种写入及独立 readback、logical disconnect 与 dispose 后静默。no-response completion 仍只是本地提交，readback 是另一份证据。

`notification` 在 enable 后才发送 baseline；确认 disabled 后，再通过同一 fixture 写入 distinct stimulus，然后才放行原 Swift 的 2 秒静默窗口。stimulus 翻转 profile baseline 首字节最低位，保持长度；当前 profile 为 `ef → ee`。证据保存两次 phase/handle/value/ACK，**BTP ACK 不等于空口 TX 或应用 delivery**。原 Swift 判据未放宽，不合成事件。缺失/提前/重复放行和 ACK 失败均 fail closed。

这修复了新 runner 初稿只放行 marker、没有 post-disable stimulus 的缺口；未扩大修改历史 provider，也不据此推导旧报告的覆盖范围。

### 旧入口不能绕过准入

旧 macOS RF dispatch 继续 fail closed。旧 iOS normal RF Just 快捷入口移除 build/provider/global cleanup 前置并直接拒绝；`test_ios_runtime` 的 `runtime-rf`、`runtime-rf-android`、`runtime-rf-external` CLI 也在环境/SDK/build 前拒绝，指向新入口。非 RF doctor/runtime/build 保留；CCC 专用 adapter 的 private helpers 不受影响，未重跑 CCC。

## 2. 构建产物已准备，不等于 RF 已通过

- Mac native App：`.work/apple-macos-runtime/BleHubAppleMacOSProbe.app`，签名离线校验通过。
- iPhone development-signed App：`.work/apple-native-hil-signed/a1f96dac-0bc6-4587-a1ca-a469243d671b/DerivedData/Build/Products/Debug-iphoneos/BleHubAppleIOSProbe.app`。
- 两端 `BleHubNativeHilProtocolVersion = 2` 是私有 HIL ownership/组合过滤协议版本，不是新增 firmware v1/v2 compatibility lane。
- iPhone App 的 Team 为 `FWVYQ287W5`、bundle 为 `dev.blehub.fwvyq287w5.ios-probe`；embedded profile 包含已选 iPhone `00008110-000C61160C39401E`，development entitlement 与 `codesign --verify --deep --strict` 校验通过。
- 仅使用既有 Xcode、证书、已解锁 keychain；未请求密码、unlock、授权、注册设备或下载工具。生成独立忽略工程，未改用户 tracked project。
- 首轮签名参数将 Automatic signing 与显式 certificate hash 混用，exit 65；恢复既有 helper 的 `Apple Development` identity 后一次构建成功。原失败日志保留，没有无限重试。

源码、staged package 和完整 App hash 及精确 argv 保存于该目录的 `prebuild.json`、`build-result.json`、`artifact-receipt.json`。iPhone App SHA-256 为 `90826a06a3ec391a822057161b064e6c911316de55f1b0003e10a47d76536f98`，Mac App 为 `5139d9e32c2df08bba6b78f7b5893b71ede4ac5a1ed4eb3cbd103be9dbe27906`；不能用 hash 或签名校验替代运行证据。

## 3. Android 真实前置：已更新包，等待系统权限

只读查询确认唯一手机 `3ac5cc09` / PHB110；consumer 原本未运行，旧包未声明新 BLE 权限。本轮通过现有安装 owner 更新了 **app APK**，未启动 consumer、未安装/运行 instrumentation test，也未启动 Peripheral。

- 包名：`dev.blehub.ffi.runtime.consumer`
- 系统显示名：**BleHub UniFFI AAR Consumer**
- 安装输入 APK SHA-256：`34a040edfb26d1efa9356fc2782ba053f8a4545faf974722d03a38190acafc13`
- 安装回执成功，随后 `dumpsys package` 明确显示 `BLUETOOTH_SCAN`、`BLUETOOTH_CONNECT` 均已声明但 **`granted=false`**；consumer PID 不存在。
- 没有使用 `pm grant/revoke`、切换蓝牙或打开 Settings 覆盖现有结果页。

需要用户在此 App 的系统权限中允许“附近设备”，然后再做新鲜权限读取和 leased RF 准入。此人工设置不是要求 review 核心代码。不能用 RN App 的已有授权代替 native consumer 自己的权限，也不因当前不足而偷偷 grant 或盲跑一次注定失败的 RF。

安装与读取证据：

- `.work/validation/2026-10-25-android-native-permission-setup-install.log`
- `.work/validation/2026-10-25-android-native-permission-setup-install-receipt.log`
- `.work/validation/2026-10-25-android-native-permission-preflight.txt`

## 4. 实际验证与失败保留

| 检查 | 结果 |
|---|---|
| Apple normal smoke host 回归 | 24 passed，由 combined canonical discovery 收录；SDK/serial/installation 边界全部隔离 |
| `pixi run just apple test-rf-host` | **13 passed**，含旧 CLI RF 拒绝与非 RF 分发两项回归 |
| `pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py` | **16 Swift tests，0 failures**；无 RF |
| Mac production native probe build | 通过；`.work/apple-native-hil-review-macos-build.log` |
| generic iOS native unsigned build | 通过；`.work/apple-native-hil-review-ios-build.log` |
| iPhone development-signed build | 通过；独立 artifact receipt 如上；未 install/launch |
| `pixi run just react-native tools-fixture-test` | 修正测试归属后 **1238 tests，229.174 秒，OK**；`.work/validation/2026-10-25-native-perimeter-combined-fixed.log` |
| `pixi run just fmt` / `pixi run just lint` | 通过；`.work/validation/2026-10-25-native-perimeter-final-{fmt,lint}.log` |
| Mermaid / Markdown / `git diff --check` | 27 个 diagrams；6 份主导航/架构/报告的 UTF-8、围栏、空白与 114 个相对链接目标通过；diff 无空白错误 |

首次 combined 为 1240 tests / 1 failure：新 RN test 导入历史命名 `tools.apple.test_ios_runtime`，触发既有 test-module 目录边界。将两项 CLI 回归移至已经依赖该 utility 的 Apple owner-local `test_rf_fixture.py`，由维护的 `apple test-rf-host` 执行；没有删除断言、加 exemption、动态 import 绕过或修改 layout 规则。首次失败 `.work/validation/2026-10-25-native-perimeter-combined.log` 保留；最终 combined 通过后未再跑整个 tooling subset。

新增 Python lint 问题已定向修复并保留首轮失败日志。没有重跑无关 Jest/RN device suite，没有把测试数量当作覆盖率。

## 5. 下一步仅做外围最小验收

1. 用户为 Android native consumer 允许附近设备权限；host 只读确认，不替用户授予。
2. 明确当次 Central、DK、contract/profile、预计占用与期限，在 lease 下验证 live installed image，再执行现有 Android 最小 native smoke。
3. Apple 已有 prebuilt 和新 host owner；按独占安排执行正常 IO/notification 的具名 session，分别保留 Mac/iPhone 的实际证据，不互相替代。未通过 install/launch/marker/RF 前，不把新路径标记为通过。
4. 对真实失败只做根因所需的定向修复与回归。外围完成后再由用户 review 核心；不扩第二档 Android、重复已知 CCC、强行恢复完整旧 native matrix 或启动多小时压力。

本轮没有真实 BLE RF 结果，没有宣告外围设备验收完成、Phase exit 或 ABI freeze，也没有 commit/push/branch。
