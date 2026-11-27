# Multifunction v2：DK-B 恢复、栈溢出修复与双手机 full-suite

## 结论与范围

真实 Mac 控制机、同一 DK-B Peripheral、Android/iPhone Central 的主线已跑通。修复后的 v2-B 完成显式 provision 和 BTP installed-image contract 校验；两端分别在原有 RN workbench 中通过 **continuous10 + RecoveryTen** 的完整 host 门禁。没有新增 App、场景 CLI、第二套固件 builder 或 AutoPTS codec。

这只是下述固件、手机 App artifact、host 实现与 session 的证据，不是 native HIL、production ABI freeze、Phase 退出或所有故障专项通过。`IOS-RADIO-GATE` 继续 deferred；DK-A 未操作；没有 push。

## 1. 提交与构建身份

本地提交：

| 仓库 | 提交 | 内容 |
|---|---|---|
| Zephyr | `d9096bd963e24bf157641f537443134676dc0a1d` | 有界静态 TX 帧池，避免生产者/发送线程整帧栈分配；补实际 Tester 初始化及资源回归 |
| nrftest | `36fe4a122c6b005abc8129c5f4331e4750d8a2bf` | 仅显式 provision 可授权一次解锁，绑定精确 debugger serial；保存解锁/UICR/错误证据 |
| nrftest | `a9cc60e62cee5145f4fcb8b3889cd6c5e50bf515` | 修正 PyLink 下载参数与 reset 后运行释放，锁定 stack-safe Zephyr |

Zephyr 工作区和实际 configured build clone 均到 `d9096bd`。四项原有 attestation host-unit 草稿仍留在工作区，未纳入提交。nrftest 原有九项无关 dirty/untracked 工作保留，index 为空。BleHub 的相关源码、contracts 和文档保留在现有脏工作区，没有批量提交。

使用已验证的 nrftest 提交树导出源码构建，tree 为 `46df20b05a8ee973cc3a8687daa9c9137ecbe0de`；139 个导出文件与提交 blob 核对一致。A/B 各执行两次真实 pristine West build，export/check 通过。69 个已安装 West projects 在构建前后均核对 pin/clean；`west.yml` 未变，没有 `west update`、下载或工具安装。

| 镜像 | raw HEX SHA-256 | normalized digest |
|---|---|---|
| A | `a6fb2530c61b0ffddb1e955425cd72c43ffd6e065a9f82ace3795ede92a5be21` | `432871f04bb6c9010d72a139927d0185068f4e20d800280ddf8b040e63b64e1f` |
| B | `da258b5d3237d46f00e16900d8eb8d74b749416f5da7e90e5715f35353149f2b` | `2066c7545e8ce2533288adbe980ce1c36ebc18dc9dc425f97c7bab868baa4799` |

当前 expected contracts 为 [A](../../../tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json)、[B](../../../tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json)。**A 只构建，未安装或 RF。** 完整构建命令、旧 contract 快照及摘要在 nrftest ignored `.work/build-v2-d9096bd/evidence/summary.json`。

## 2. DK-B：先诊断保护，再显式恢复

目标始终为 USB serial `001050251479` / `jlink:1050251479`，BTP port `/dev/cu.usbmodem0010502514791`。没有覆盖默认 DK-A 本机配置。

前次 [provision 失败报告](2026-10-18-dk-b-v2-provision-attempt.md) 没有建立根因；随后获授权的连接诊断明确收到 **CTRL-AP secured**。所有解锁请求均回答 NO；BTP before/after 仍为相同 v1-B identity/hash。该诊断证据在 nrftest `.work/dk-b-connect-diagnostic-2026-10-18/`。

向用户说明解除保护必然全片擦除后，用户明确要求继续。本轮仅对 DK-B 开放一次 affirmative unsecure response；实际只出现一次 YES。默认 reset/flash/RF 路径仍回答 NO，实际 debugger serial 在 target connect 前回读核对，整个 provision 持有物理 lease。没有自动恢复重试。

源代码核验表明：nRF52840 的 UICR.APPROTECT 位于 `0x10001208`；当前 `NRF_APPROTECT_USE_UICR` 启动路径把该值写入软件 disable register。因此单靠解除保护或 `LOCK=n` 不能证明重启后保持可调试。本轮显式写入 `0xFFFFFF5A`。首次下载 session 的回读可能来自 J-Link cache；之后独立、禁止 unsecure 的连接再次读到该值，且无 unsecure prompt。**没有执行断开全部电源后的 cold-boot 验证，不能外推冷启动持久性通过。**

### 两项工具修复

- 已安装 PyLink 的 byte `flash()` wrapper 将 `flags=0` 作为 ctypes keyword 传给无参数元数据的 `BeginDownload`；native log 实际显示非零不定参数。改用已安装公开 `flash_write32()` 的 positional `BeginDownload(0)` 路径，并以真实 wrapper + fake native boundary 回归，而非修改 vendor。该缺陷不能据此认定是首次 BTP 超时的原因。
- PyLink `reset()` 默认 halt；正常编程也必须 reset、确认 halt、再 run，不能把 target 留在 halted 状态后等待 BTP。历史 `flashed_bytes` 字段保留但注明它是 API 返回值；不再把返回 0 打印为“写了 0 字节”。

## 3. 真机定位的固件栈溢出

首次恢复后，旧 `433a6f37` v2-B 编程 API 成功，但两路 VCOM 均没有 BTP responder。没有再次擦除或盲目重刷。独立 non-programming attachment 得到：

- FICR PART 为 `0x52840`，UICR 为 `0xFFFFFF5A`，没有触发解锁。
- 四个 application Flash segments 均逐字节匹配该 HEX。
- PC sample 为 `0x50122`，落在 `arch_system_halt`；保存的 MMFAR 为 `0x20010c1c`。
- 构建产物中 `z_main_stack=0x20010e00`、`CONFIG_MAIN_STACK_SIZE=1024`；`tester_send_with_index` 本身用栈 1064 B，启动期间 `tester_init()` 会调用它发送 IUT_READY。

这定位到明确的启动栈溢出，不是镜像未写入或串口选择问题。完整 native log、非原子 RAM snapshot 和 Flash 比对在 nrftest `.work/dk-b-v2-recovery-2026-10-18/`。

修复使用 **9 个静态 slab blocks + 8 项 pointer queue**，保留一帧 UART in-flight 的容量。生产者仅做 `K_NO_WAIT` 分配/入队，失败先回收再标记 infrastructure failure；worker 发送后回收 block，再通知 evidence drained。没有扩大 main/RX/ISR 栈或削弱 quarantine。

同 ARM 编译参数下，生产者栈帧 **1064 → 32 B**、worker **1056 → 32 B**；这些是单函数栈帧，不是整条调用链上界。standard Tester、fault Tester、ATT fault regression 均编译链接通过；新增 1 KiB 启动栈、ISR 最大 payload、队列/池耗尽与回收断言。**Linux/BabbleSim 未执行，这些编译过的内部断言不记 runtime pass。**

随后在默认禁止 unsecure 的普通 provision 路径刷入新候选 `d9096bd` v2-B；没有再次恢复擦除。BTP 回读的 image/profile/service/capabilities 和 normalized digest 与 contract 精确匹配。证据：nrftest `.work/dk-b-v2-d9096bd-provision/installed-attestation.json`、`provision.log` 及对应 reports。

## 4. 连续场景的 host 根因与修复

真实 RF 揭示了合成样本此前没有闭合的组合边界，原失败 session 全部保留：

| attempt | 实际失败与修复 |
|---|---|
| Android `6401a8ad00894d53bf1d514df0215876` | 前八项通过；rf-io 写入的 `ab` 留在同一 ATT database，取消场景要求 `00`。在场景 owner 内新增无 peer、idle、精确 handle/UUID/permission 的准备与回读，不放宽取消 before-value 断言 |
| Android `f7f710f1cb104545b53f927d943bbdcc` | 严格 no-peer 检查发现前一被动断连场景的 recovery connect/disconnect 未消费。现在由 flow 7 显式观察原连接/断连及恢复连接/断连，精确绑定同一 peer，关闭广播后交接；不靠 permissive drain 丢弃事件 |
| Android `5d189171f2794809a001cd891918c55c` | 准备和 ARM 成功，但生产 writer 把进行中报告初始化为 failed，relay 正确拒绝。改为 running；异常入口先写 failed，最终 passed 仍要求 close/resource-zero。新增真实 writer → 真实 relay 的交叉回归 |
| iOS `9be0233636914a93b94990b3cfd566a1` | 十项基础通过，RecoveryTen 第 0 轮被 aggregate BUSY 拒绝。该标志也包含普通 BTP reply/GAP event 的 TX pending；原记录没有保留失败的 GET_LIMITS raw reply，因此不声称已识别当时的具体 pending frame |

取消 preparation 记录前值、回读、control/session、process/resource、boot 和前一精确 GAP lifecycle，并嵌入现有 physical evidence；最终 bundle 冻结并重新校验这些事实，不新增独立 verdict 系统。

对 full-suite 恢复阶段，直接传递同一 session 已验证退休的 watchdog ticket，作为**只读归属证明**。精确 STATUS 必须仍为该 READ owner 的 RETIRED / DISCONNECT / DISCONNECTED_UNCOMMITTED、rearmable、无 infrastructure error，前五类 fault refs 全零；仅第六类 evidence pending 可在原绝对预算内排空。成功边界仍要求原 `cleanup_ready()` 六项全零，再加 fresh GET_LIMITS flags=0、同 boot。stale/new ticket、活动 fault、quarantine 或任何 fault ref 不归零立即失败；不 ARM、ABORT、RESOLVE 或清除隔离。

Standalone 无 ticket 的 RecoveryTen 仍 fail closed。该修复不宣称所有其他 `require_idle()` 路径均已消除瞬时 BUSY false-failure。采样是顺序观察，依赖既有 exclusive lease/单 BTP owner；诊断有界保留 128 项及失败，不是完整十轮 TX 历史。

## 5. 最终双平台 RF

两端使用同一 v2-B installed candidate、相同最终 host 源码，各有独立 App、PID、session、live receipt 和 evidence bundle：

| 平台 | App artifact | 最终 attempt | UTC 开始—完成 |
|---|---|---|---|
| Android / `3ac5cc09` | Release APK SHA-256 `361ee8b9511cc84401d9b071956858922abe5bca983d79ffbfd93e10d28c2e23` | `07e28c8ab36b44368a246a87261c167c` | 14:11:20—14:15:36 |
| iPhone / `00008110-000C61160C39401E` | Debug device App；fingerprint `b82d06ad337a3ec3ddebb98839bbbd5b5a81ec99ff91946e2ca0bdeb58102fee` | `b643cd26fe464e52abc70f19afa58698` | 14:07:27—14:10:24 |

每个平台均为：

- continuous10 十项通过，含真实 cancellation 和 native watchdog。
- RecoveryTen 十轮、20 次连接、20 次断连、10 份 closed native snapshots。
- physical status 与 bundle status 均 passed，completed=true；无 primary/cleanup/evidence errors。
- 最终 advertising stopped、hold inactive、peer_count=0；provider PID marker 已释放。
- App 结果页保持可见，没有在成功后 force-stop 或切走页面。

完整 bundles：

- Android：`tests/consumers/react-native-internal/android/build/rf-full-suite-07e28c8ab36b44368a246a87261c167c/evidence-bundle.json`。
- iOS：`tests/consumers/react-native-internal/ios/build/rf-full-suite-b643cd26fe464e52abc70f19afa58698/evidence-bundle.json`。

使用既有 `run_rf_lab_full` CLI，显式传入 platform/device/artifact、v2-B contract/profile、`--timeout 1200`；进程级 `BLEHUB_NRF52840_PORT=/dev/cu.usbmodem0010502514791` 在启动前核对为 `jlink:1050251479`。没有只覆盖 port 却沿用 DK-A resource identity，也没有用历史安装回执放行新 App session。

此前 Android `48fc6d9ec0de41a3ad62566239951fe5` 也通过，但发生在 retained-witness 修复前；最终重新完整执行 Android，不把该早期通过当成最终 host 版本结果。

## 6. 软件、构建与离线验证

实际执行结果：

- nrftest 精选最终提交树：`pixi run just --justfile .work/btp-slab-candidate/source/justfile --working-directory .work/btp-slab-candidate/source test lint`：**745 passed、1 skipped**，Ruff 通过。完整工作区计数与提交树计数不混用。
- BleHub `pixi run just react-native tools-fixture-test`：最终 **847 tests passed**。
- `pixi run just react-native host-check`、`pixi run just fmt`、`pixi run just lint` 通过，lint 包含 Clippy。
- 既有 host `test:ui` npm script：**71 tests passed**；没有伪称存在新的 Jest Just recipe。
- iOS `pixi run just react-native host-ios-device-build 00008110-000C61160C39401E FWVYQ287W5` 通过，使用真实 Mac 的选定 Xcode 及既有 no-tty wrapper。device debug dylib 的 single-image check 通过；SHA-256 `ff422c29f49cc84afc560cb8a88a34e8090e7bab385401de980ba2b6bc0d041f`。没有执行 UI/radio XCTest 或收集密码。
- iOS 常规 npm/CocoaPods prerequisites 使 `Podfile.lock` 增加 React-Core-prebuilt 0.87.1/facade 依赖记录，并重排 `PrivacyInfo.xcprivacy`。没有主动升级公共已解析 pod 版本；前后 hash/patch 保存在 ignored `ios-recipe-drift.patch`，改动保留供审阅。
- 对两份最终 bundle 分别运行 `pixi run just react-native host-rf-evidence-check <对应 manifest>`，均返回 `integrity verified (not RF reauthorization)`。这是离线完整性/ledger 重放，不是新增 RF run。
- `pixi run just docs-check-mermaid` 通过（150 份 Markdown、27 个图）。额外全文链接扫描发现 `.plan/PLAN.md` 四个既有失效引用：三个历史 BLeng candidate 路径及旧 `docs/testing/windows.md`；未顺带改写历史计划。新增报告与相关维护文档的链接另行核对，UTF-8、fences、空白及冲突标记检查独立执行。

主机日志与最终摘要位于 BleHub ignored `.work/rf-v2-d9096bd-2026-10-18/`；失败 bundle 和原始日志不被成功结果覆盖。

## 7. 未覆盖项与后续

- Response-write REJECT、CCC enable/disable REJECT 等具名 RN 新专项尚未实现或 RF；firmware capabilities 不等于它们已通过。下一步在同一 workbench 中增加 opt-in 场景，不新建 App，不先混入 full-suite。
- DK-A 未 provision；新候选 multi-DK、独立 native Mac/iOS HIL、artifact-only consumer/package、最终 ABI 及 API-tier gates 不继承本报告。
- Linux/BabbleSim 内部调度、第二 outstanding ATT Request、observer 延迟、池耗尽、UART 饱和和 deadline equality 的运行断言仍有明确缺口。
- 未完成 cold-boot debug persistence、10,000 lifecycle 或长稳验证；十轮恢复不是替代。
- `IOS-RADIO-GATE` 未恢复、未通过、未豁免。普通 RF run 仍禁止隐式 flash/reset/unsecure。
