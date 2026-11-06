# nRF 通知 TX 观测：候选构建、DK-B 安装与独立手机 RF

日期：2026-10-19。**初始快照为源码、host 接入与 A/B build-only 候选；随后用户明确确认精确 B HEX，已仅安装 DK-B 并执行独立 Android/iPhone RF。** 下文保留初始构建结果；后续安装、Android pass、iPhone CCC continuity gap 和独立 RecoveryTen stop-advertising failure 见末节。A 未安装；[getter-candidate 的旧报告](2026-10-19-rn-ccc-disable-reject.md)及两次 iPhone failed 不回写、不转移 pass。

## 为什么增加观测

原 iPhone 诊断已看到 CCC disable 的 ATT14、remote CCC 仍启用、logical route 恢复 active，但没有后续 FFF2 delegate callback。Host `SET_VALUE accepted` 只证明本地处理受理，可能只是 storage update，不能据此认定一个通知已经发给 iPhone。

本轮增加底层通用机制，区分 storage/CCC gate、subscriber selection、GATT admission、ATT queue、L2CAP submit、buffer release 和独立 connection TX callback。**TX callback 也不是独立 air/link-ACK、CoreBluetooth delegate 或 JS delivery 证明。** 不修改 CCC 场景通过条件，不重新 enable，不换 subscription，不把 `e1` 变为可选。

## 所有权与边界

- Zephyr 的 `tests/bluetooth/tester/src/notify_observation.inc` 与私有 host hook 提供有界 receipt；没有 RN 场景名、平台 verdict、重试策略、新 BLE owner、thread、timer 或 workqueue。
- 编译开关和 runtime capture 均默认关闭。保留 `BT_GATT_NOTIFY_MULTIPLE=y`、flush=1ms 的原 batching 路径，不以关闭 batching 改变待诊断行为。
- 八份 receipt、每 PDU 八个 observed cookies、八组 TX sideband；overflow/eviction/unmatched/late completion 显式记录。boot 内 numeric cookie/token 不重用，owner 仅在 OFF 状态轮转，旧 callback 不能更新新 owner 的 receipt。
- Wire authoritative source 为 Zephyr `tests/bluetooth/tester/src/btp/btp_notify_observation.h` 和 `notification_observation.rst`：service/index `ff/ff`；QUERY `0x20` 请求 1 / 成功 156 bytes；CONFIGURE `0x21` 请求 54 / 成功 156；READ `0x22` 请求 37 / 成功 259（2-byte header + 257-byte record）。
- 唯一 Python codec 为 [`notification_observation.py`](../../../tests/peripherals/nrf52840/notification_observation.py)。[`notification_observation_capture.py`](../../../tests/peripherals/nrf52840/notification_observation_capture.py) 复用已有 lease/BTP session，FINAL 在 physical cleanup 前尝试；诊断错误不覆盖原 primary error。
- CLI `run_rf_ccc_disable_reject --observe-notification-tx` 显式 opt-in；只包围已有的 `ef/e1/f0` SET_VALUE，不新增 BLE stimulus。poll 位于非 HELD 区间，最多 2Hz。无 sidecar 时旧 bundle 路径不变；观测不能替代 App `e1`。
- family admission 仅接受两种 reviewed exact variants：原有 ordered 11 项；或原 11 项后仅追加 `notification-tx-observation-v1`。未知 superset/reordering 仍拒绝；profile 仍为相同五个角色。修正 live session loader 原先只允许 base11 的遗漏。

## 本地提交

| 仓库 | 精确 commit | 范围 |
|---|---|---|
| Zephyr | `4189bfcaa0dfa9d0c94f7809a331254a7879564b` | notification observer、typed vendor wire、回归与文档 |
| nrftest | `1bf6c08bde93b07dbf6446183f640107bf40eed1` | pin、exact capability/settings policy、两遍 compiled truth 校验与测试 |

沿用用户对实际固件仓库的本地提交授权。**均未 push。** Zephyr 已有四份 `attestation/` dirty 文件未纳入；nrftest 的 README/Just/J-Link/lifecycle 等其他改动未纳入。未 reset/stash/clean、未改 Git 身份、未代用户添加 Signed-off-by。BleHub 本轮改动未提交。

## 软件验证与实际限制

| 实际执行 | 结果 |
|---|---|
| observer `run.py --build-dir build-notify-host --sanitize` | **2970 checks / 0 failures**，完整日志无 ASan/UBSan 报错 |
| C `--wire-fixture` 输出，再次生成与 tracked sample `cmp` | 13 组 actual-C payload，逐字节一致；Python strict decode/request re-encode/replay/坏数据拒绝已覆盖 |
| session + codec + capture + host 定向组合 | **77 tests，OK** |
| `pixi run --frozen --no-install just react-native tools-fixture-test` | **1012 tests，OK / 212.418s** |
| clean nrftest selected-commit `just test` / `just lint` | **817 passed / 1 skipped**；Ruff 通过；不将 dirty checkout 的 819 与 4 subtests 贴给 selected commit |
| `npm --prefix tests/consumers/react-native-internal run test:ccc-disable-reject`（Pixi） | **6 suites / 157 tests passed** |
| BleHub `just fmt`、`just lint`、`just react-native host-check`（Pixi） | 全部 exit 0；Cargo lint 使用 offline |
| observer ON、observer OFF Tester、fault regression 三个 existing ARM compile/link probes | 全部 exit 0；不是目标上执行 |
| observer commit checkpatch / cached diff whitespace | **0 errors / 0 warnings**；不是全 Zephyr compliance pass |

C harness 包含实际 production Tester/ATT/GATT 实现或原样抽取函数，但 kernel、transport、部分 handoff/destruction/scheduler/lock 行为使用 substitutes；不是实际并发、controller、UART 或 RF 证明。编译保留一项既有 `%u`/`size_t` warning 与三项单 translation-unit inclusion 的 shadow warnings。

复核发现增量 unit build 缺失生产抽取输入依赖，已在 owning CMake 添加 `run.py`、`att.c`、`gatt.c`、`att_fault.inc` 的 configure dependencies，并确认四者进入 Ninja 再生成图。实际执行普通 `cmake --build build-notify-host` 时，仍因 macOS Mach-O 不接受 Ztest ELF section attributes 失败；随后 standalone runner 重新抽取、编译、sanitized 执行通过。没有把标准 unit/Twister/Linux 运行记为通过。

早期 crossing 验证中的 canonical suite 曾在 120s 超时，无 pass；上表是之后主线程在 300s 上限内完成的一次运行。完整记录保留在 ignored logs。

## Clean-source A/B two-pass build

复用 nrftest 已有 `firmware-build-dk-image`、`firmware-export-contract`、`firmware-check-contract`，只适配 ignored 的单次 execution receipt，没有新增 tracked builder/framework。本节构建阶段的候选输入为 `.work/notification-observation-candidate/inputs/`，当时没有覆盖 tracked base11 image declarations、contracts 或 profiles；后续已获确认的 B 安装与 promotion 单独记于下文。

使用 clean detached worktrees：

- nrftest：`nrftest/.work/notification-observation-candidate/source`
- Zephyr：`nrftest/.work/upstream-attested/zephyrproject/notification-observation-candidate/zephyr`

既有 west workspace、SDK 1.0.1、AutoPTS 和本机 Pixi 环境复用；frozen/no-install/offline。`west.yml` bytes/blob 不变，69 个 pinned clean projects 前后验证一致，再创建 candidate-only completion marker。原 configured checkout、原 marker、`.west/config`、本地 TOML 和现有 dirty 工作均保持不变。nrftest 默认 TOML 仍指旧 source，直接默认 verify/build 会因 pin mismatch 拒绝；本候选通过显式 `NRFTEST_ZEPHYR_ROOT` / `ZEPHYR_BASE` / `NRFTEST_BUILD_DIR` 选择，不运行 setup/update-west。

本次实际完成 A/B 各两遍 pristine west build（共四次），export/check 与前后 verify 全部 exit 0，无构建重试。实际 config 验证 observer/private hook=`y`、multiple=`y`、flush=`1`、EATT=`n`、maxconn=`1`；capabilities exactly 12、顺序一致、剩余槽为空。保留 logging/RTT/EATT dependency warnings。

| SHA-256 | A | B |
|---|---|---|
| normalized image | `ef9b71209fbd4c3cf286c8e68c8dbb996182cfd9b69525a8cc74c634cf9d3179` | `d57953806f0195dff0b175d80b09279d64e5b2d82b15198de77bd9ebd54e75f7` |
| raw HEX | `2d8851e9f8ce9bd79ed3016aea61d67e00dda0273db7ea57d87c6244e23a8d79` | `ab275c20bb107f9235cac45b026f5dcf32746b9702d60e4b7cc9cf84ec263dcd` |
| manifest | `200cf44b9fdfb8f9c54f3277d20ba348d3a823eb629f0a07072f79bedbff2b6d` | `9fc043770595f2b778b4398a11c387518a8217bc16b3d77edb935b2ca8a8322b` |
| expected contract | `4e5527e65866dcfa942dfa3e716db49f26dfe200bafd6e0737dfd798d6f81dd2` | `bc109c22bb4b3c27effffa016b3c318792d9b6632c7c31895d5710ac23a689bf` |

FLASH/RAM：各为 **359,712 / 103,136 bytes**。Before/after preservation fingerprint 文件 SHA-256 同为 `20a67b90cf6c03d827e6d88fea24ba31e7896e814b0b47e9ad50010c1fdb4f57`。

产物和完整日志位于 `.work/notification-observation-candidate/`：`images/<image-id>/`、`expected-contracts/`、`evidence/{summary,build-result}.json`、各 build/export/check logs；C logs 在 Zephyr `build-notify-host/`。这是 build-extracted expected identity，不是 physical installed receipt，也不是完整工具链 bit-for-bit reproducibility 证明。

## 用户确认后的 DK-B 安装

用户确认上表精确 B HEX 后，仅选择 USB `001050251479` / J-Link `1050251479` / BTP `/dev/cu.usbmodem0010502514791`。同一 DK 暴露两个 CDC interface；首次只读预检错误地要求该 serial 只有一个 interface，断言失败时尚未刷写。随后按显式 BTP interface 核验 serial/VID/PID，不猜测另一个接口。

复用 clean nrftest `1bf6c08…` 的 `firmware-provision-dk`，显式覆盖 device/debugger/port/reports 路径。在同一 `purpose=provision` lease 内，一次完成 manifest/HEX/contract preflight、programming、读回/正常 reset-release、BTP installed-image assertion，`recovery=null`。另外逐字段比较实际 installed JSON 与 expected contract（除 contract-only role），完整12项能力及顺序一致，才更新 tracked B contract。最终软件回归另发现 B declaration 尚未 promotion，已同步为 reviewed build input，byte-for-byte核对 declaration 与 candidate input、contract 与 exported expected identity 均相同；不重建/改写旧产物。A contract/profile/declaration与两份profiles未改。

Receipt 位于 `.work/notification-observation-candidate/provision/`：

- `dk-b-installed-attestation.json`
- `firmware-flash-dk/20261019T083645.293478Z-be93d7fd194c4aab8e08882cc6e52fed.json`
- `firmware-provision-dk/20261019T083647.575332Z-53a1a2ba4f3642568bdcb9a9c5a5d5a3.json`
- `provision.log`

installed normalized digest 为上表 `d5795380…54e75f7`。没有 recovery/unsecure/mass erase、重刷或 DK-A 操作；每个后续 RF attempt 仍有各自 lease 内 fresh live receipt。

## 新 installed candidate 的真实通知观测

| Gate | Attempt（平台 build 下） | bundle run ID | 结果 |
|---|---|---|---|
| iPhone CCC disable + observer | `rf-ccc-disable-reject-5d11208f45574c179ab3be2895b41d4d` | `abf88c86-cd21-4b6f-ba72-36b6b27006b5` | failed，原 subscription 无 `e1`，停在5/8 controls |
| Android CCC disable + observer | `rf-ccc-disable-reject-a2faaeb6e8ea46ac9be69e1fb96598d7` | `644b3807-1c60-46b3-a683-992b396ab876` | strict passed，8/8 controls与完整observer replay |

未重新构建手机 App：iPhone frozen App tree `93322afb8357cf64a47349e8d965fc34e72ea75655dd7197e575ca2d12444c3d` 与上次诊断相同；Android 使用原 Release APK。iPhone 显式加 `DEVICECTL_CHILD_BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS=1`；普通 full-suite 不继承此 env。两端同用 `run_rf_ccc_disable_reject --observe-notification-tx`，显式新 B contract/profile、上述 DK-B port。没有 re-enable、新 subscription、额外 stimulus 或修改通过条件。

### iPhone：已经证明本地 TX callback，但没有 delegate delivery

physical/App session 为 `29194470-f365-4b20-9bb3-7a4d0b5eecb6`，observer owner 同 session bytes、generation1；boot `77775178dee981bf990cd8ed59eca805`。baseline `ef` 和 continued `e1` 分别为 update/PDU sequence `1/1`、`2/2`，value handle41，peer `01b5fa4374306c`，connection/bearer epoch `1/1`；entry database epoch15/16，SET_VALUE 后16/17。不能把 entry 与 after 的不同 epoch 误判为漂移。

| 通知层 | `ef` | `e1` |
|---|---|---|
| storage / CCC / selection | UPDATED；CCC flags `0x3f`，cached/aggregate/per-peer均1，subscriber1，NOTIFY_SELECTED/errno0 | 相同 |
| GATT / ATT | API RETURNED/errno0；BOUND、PDU_SEEN、ATT_QUEUED | 相同 |
| 实际 PDU | `0x1b`，entries/tracked/ordinal=`1/1/1` | 独立PDU2，其他相同 |
| L2CAP | attempts/admitted=`1/1`，SUBMIT_RETURNED/errno0 | 相同 |
| buffer release | errno0，uptime63150ms | errno0，66392ms |
| 独立 TX callback | 1次、errno0，63188ms | 1次、errno0，66428ms |

`att_errno=None` 因没有 ATT_REJECTED；`queue_errno=None` 因retry=0，不能把未观测当成有效errno0。自动 batching 编译配置为y/1，但 peer live flags7不含PEER_BATCHING，两条实际都不是 BATCH_APPEND。无错误、retry、overflow、eviction、unmatched或trace loss。35个raw exchanges（17 QUERY、2 CONFIGURE、16 READ）由真实parser重放；11次poll间隔0.502–1.878s。FINAL后同owner/gen精确关闭capture；`f0`/retry-disable/silence未执行，sidecar complete只表示诊断收尾，不是场景完整。

原 fault receipt 为CCC handle42，flags `0x0299`，observed `0000`、before/after `0100/0100`，六refs全零。Apple诊断21条（backend1–17、probe1–4），无overflow：`ef` 到达原subscription34009；ATT14后 `isNotifying=true`、phase active、acceptsValue=true、同对象/代际、无barrier，FFF1 read继续到达；没有后续FFF2 `value.entry` 或App `e1`。缺口因此推进到**精确Peripheral本地connection/HCI TX callback成功，但未观察到Central delegate**，仍不证明独立air/link ACK或iPhone收到。

App在缺少`e1`后记录 `JS wait expired; no native outcome inferred` 并native失败收尾。Host先消费到closed资源行，在未完成场景disconnect step时触发 `closed resources order`，这是本次保留的primary，不改写为其他错误；raw另保留其后的native/App failure。最终native17项全零、peer detached、physical cleanup errors为空；bundle保持failed。

### Android：完整对照与 OFF owner 轮转

同boot下，上一iPhone owner保持OFF；本次按 handoff-off → rotate-off → enable，owner/session `696fae83-35a5-4a71-abbe-71f926a88b44`、generation2。`ef/e1/f0` sequence为3/4/5，connection/bearer epoch2/2，peer `01ab0bd860a45e`，value handle41。

- `ef/e1` 完整选择、GATT/ATT/L2CAP admission、本地TX callback errno0；实际为各自单条`0x1b`，不是batch。App另行证明**原subscription34009收到ef/e1**，不由TX推断。
- failed-disable34011为ATT14，无Disabled；只有初次enable34010，收到e1后才正常retry-disable34013，无re-enable。
- `f0`确实执行storage update，但cached/aggregate CCC为0、decision CCC_ZERO，selection/API/PDU/TX均未执行，对应无效字段为None。App双read-fenced silence1001ms，host窗口1.3676258329942357s。
- 原rejected receipt不因当前CCC=0而变化；13个command terminals各一次，最终native17项/known peers/firmware六refs归零。
- 43个raw exchanges（17 QUERY、4 CONFIGURE、22 READ）重放通过；9次poll间隔0.502–1.072s。FINAL后capture OFF/gen2。计数是boot-sticky，Android相对initial增量为3个updates、2个calls/TX_OK和1个UPDATE_ONLY，无loss/error，不能把iPhone历史累计值算成本轮。

两份 observer bundles 的 `verify_bundle` 离线完整性重放均通过且manifest bytes未变；只保留各自原passed/failed verdict，不由offline replay重新授权RF。

## 同候选 full-suite 回归：Android通过，iPhone有独立失败

串行复用 `run_rf_lab_full --timeout 900`，同新B contract/profile/live receipt，**不请求notification observer、无sidecar**：

| 平台 | Attempt | 结果 |
|---|---|---|
| Android | `rf-full-suite-6909045fc06a4a1592c20e3df1e48f73` | 10 flows + RecoveryTen10 cycles passed，bundle run `dbfdf121-9fc6-4dfd-a97e-a52dd745326b` |
| iPhone | `rf-full-suite-2756a30909194347a334fb48aa4cea4f` | base10 flows passed；RecoveryTen cycles0–2 passed，cycle3后failed；bundle run `2430c1ad-6a63-45a6-b554-43b58296cf74` |

iPhone physical session `ea767108-661c-4d21-812f-caceacbe0cd8`。cycle3 App的recovery-read已通过；`recovery_ten.py`在等待cycle-closed前调用GAP stop advertising，BTP返回`0x01`。`recovery-ten-physical.json` primary/errors保存 `GAP stop advertising failed with BTP status 0x01`；失败收尾发布progress `closed/failed/physical_closed=false`，host据此抛出 `physical recovery progress failed: closed`。`closed`在这里不是native Closed，也不是resource-zero。

raw保留10312的Disconnected与commandCompleted，但host physical失败先于后续1s App silence/closed证据收齐；不能说disconnect未发生，也不能假定旧Probe已close。不是900s whole budget或ACK预算耗尽，identity/retained-status replay未见漂移，128个fault samples无quarantine/infrastructure error。原始具体stop errno没有保存，不能仅凭通用BTP FAILED归因为新固件。

physical cleanup独立stop确认成功、peer0、hold inactive、最后fresh limits flags0；原primary不抹除。cycles0–2各有17-zero，**cycle3没有native closed snapshot，因此最终native zero未证**。bundle cleanup_errors为空不填补该缺口。两份full-suite bundles的offline integrity重放均通过，iPhone仍failed。Android保留成功结果页；iPhone失败证据不清除、不以重跑覆盖。

普通iPhone full-suite的Apple diagnostic prefix为0条。另在全部RF收尾后，独立新physical lease做仅只读的attestation/GET_LIMITS/observer QUERY：exact installed digest、engine idle、无peer、同boot、capture OFF；保存 `evidence/post-run-observer-query.json`。该新观察不回填此前full-suite缺失的绝对OFF或nativezero证据；没有CONFIGURE、reset、profile mutation或BLE stimulus。

## Provision exact-variant 安全回归

复核发现nrftest旧`_assert_identity_matches()`只逐项验证两端各自属于合法family，却漏比对capabilities；引入两个合法variant后，可能接受expected11/installed12或反向不一致。此次实际安装已额外逐字段比较，未受此漏检影响。

修复只在host provision比较中加入capabilities；新增两variant四组合和preflash/post-attestation失败边界。旧实现**4 failed / 2 passed**，修复后owning test file **82 passed**；nrftest dirty checkout全套 **825 passed / 1 skipped / 4 subtests passed**、lint通过。没有用dirty总数冒充clean-build commit结果，也未重复刷写。

本地提交 `629316d7cc10d84713078c192455f4049d365d57`，仅含provision实现及其测试，未push；Zephyr pin与firmware bytes未改。该host修复晚于构建/刷写，原manifest中的nrftest构建来源仍为`1bf6c08…`，不追溯改写receipt。

## 安装后最终软件与文档验证

B contract切换后首轮组合回归发现两项失败：canonical declaration/contract不一致，以及测试仍假设tracked B为base11。promotion精确B declaration后，定向测试又暴露“移除可选observer也应失败”的旧假设；已改为逐项检查base11必选能力，并在独立测试中显式验证两个exact variants。没有放宽未知capability/reordering或改写历史bundle。

最终实际结果：contract/session定向 **41 tests OK**；`pixi run --frozen --no-install just react-native tools-fixture-test` **1012 tests OK / 210.693s**；`just fmt`、offline Cargo的`just lint`、`host-check`通过；两份改动测试的Ruff check/format通过。Mermaid检查 **155 Markdown / 27 diagrams** 通过，四份authored Markdown的UTF-8/fence/whitespace/conflict检查通过。首次失败日志与最终通过日志均保留，不把失败轮次计通过。

新provision比较对已保存的DK-B installed receipt只读重验通过，无设备动作。四份本轮RF bundles各自offline integrity重放通过，passed/failed保持原verdict；最终provider marker不存在，不以该marker替代各自resource/peer证据。

## 当前明确缺口与 radio 顺序

1. CCC disable REJECT：Android在新候选严格通过；iPhone停在本地TX之后、Central callback之前，后续若要归因需独立HCI/air或等价可验证证据，不能重新enable/放宽e1或无依据修改生产语义。
2. 新候选iPhone RecoveryTen stop-advertising失败独立保持；未定位具体errno，nativecycle3 close-zero仍缺，不能从旧getter候选full-suite pass外推。
3. `IOS-RADIO-GATE`已重新开放，当前firmware/CCC有明确阻塞边界；本轮尚未运行Settings/XCUITest/radio mutation。只读Mac进程仍看到iPhone Mirroring PID484，这不是正在镜像或失败根因证明；按既有对照要求，由操作者退出镜像、实体手机解锁后再运行只读Settings诊断。没有擅自关闭用户镜像或新建全局process guard。
4. 完整日志保存在本候选`evidence/`与各平台attempt目录；未修改原getter-candidate、旧failed bundles、原configured toolchain或DK-A。
