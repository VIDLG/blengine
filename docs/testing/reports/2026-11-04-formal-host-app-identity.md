# Formal RN host：候选/进程身份与已运行 WDA attach

日期：2026-11-04。主机：真实 Mac，本地 Pixi/Just、Xcode。
候选：基于 `7ba9ae40c9b3` 的未提交工作树，不是该 commit 的发布结论，ABI 未冻结。

**软件结论：两端 App identity pin/recheck、正式 Apple host 的 opt-in native boot responder、
已运行 WDA 的专用 attach helper 已实现。Python 定向组合 200 项通过；现有 macOS 无设备 XCTest
38 项通过；正式 iOS App/native-test 编译通过。未执行 iPhone/Android ART、真机 Appium 或 RF。**

规范来源：[Plan §1.5](../../../.plan/PLAN.md)、[scan contract](../../research/react-native/react-native-bleng-scan-contract.md)。
前段 [typed scan / ownership evidence](2026-11-04-formal-rn-scan.md) 保留其原构建指纹和失败范围；
本报告不把这些旧产物或 macOS 执行换算为移动 runtime/RF。

## Android：固定候选与 OS process incarnation

[`android_sdk.read_app_identity`](../../../tools/react_native/android_sdk.py) 使用只读 shell 命令：

- 明确 serial/package，唯一正 PID，唯一 installed `base.apk` 路径；split APK、歧义或读取拒绝失败。
- `/proc/PID/stat` 的 `start_ticks` 复用已有 parser，UID 四字段一致、cmdline 精确等于 main package。
- `/proc/sys/kernel/random/boot_id` 区分 OS boot；不拿 host 时间冒充 native 启动时间。
- 设备 `sha256sum` 严格匹配显式候选 APK SHA-256；不安装工具、不 root、不降级成只看 package。
- 前后复查原 PID/start/boot/path/UID/cmdline，不重新选择进程来修复失配。

[`pin_android_app` / `AndroidAppBinding`](../../../tools/react_native/bleng_scan_host.py) 固定实际
phone/run、driver 对象/session/capabilities 和候选；每次回调重新采样，配合必填 candidate/owner
检查。13 条成功读取命令共用绝对 deadline，每条前后检查 owner。该采样不是原子 OS snapshot
或设备安全证明；目标手机的 procfs/sha256sum 支持和高频读取耗时尚未验证。

### Android：managed candidate 与 App-only 收尾

新增 [`AndroidCandidateOwner`](../../../tools/react_native/bleng_android_candidate.py)，复用已有
SDK/process identity reader 和 scan host binding，不依赖 legacy RN host 的硬编码 package、
root installer 或 permission-grant helper。

```text
frozen APK SHA + 锁定 SDK aapt2 badging / manifest
→ 明确 serial、single user 0、非 shared UID、无既有 package/UID 进程
→ 一次 adb install --no-streaming -r [-t] --user 0
→ installed UID / APK hash + 再次检查进程不存在
→ 一次 am start -W --user 0 -n 明确 component
→ exact COLD/Status/Activity/Complete + OS process identity
→ bind existing driver；不得采用启动后重建的新进程
```

- 显式 `test_only` 控制 `-t`；无 `-g`、root 探测、installer force-stop、自动降级或安装重试。
  不主动 grant 不等于权限全未授予，更新安装可能保留既有权限。安装 deadline 只限制 host 等待，
  不证明远端安装回滚或临时 APK 已删除，失败不得继续 launch。
- metadata 在任何 adb 操作前核对 package/activity，并拒绝 manifest 的 sharedUserId；安装前后
  检查当前 user 0、仅存在 user 0 和该 App UID 不与其他 package 共享。不接管 work profile、多用户
  共装或未知 UID 拓扑，也不因此要求第二台手机或声称其他 Android 版本通过。
- `ps -A -o PID,UID,ARGS` 同时检查 package、`package:worker` 和已安装 UID，避免只看 main
  `pidof` 漏掉 worker／自定义进程名；错误 header、重复 PID、畸形 UID 不当作“无人运行”。
  procfs/ps 的可见性和当前目标设备的输出支持仍须真机确认，不能宣称 OS-wide 原子排他性。
- `Status: ok` 不够：复用 Activity 的 HOT/WARM/warning 被拒绝，要求 COLD 和 exact component；
  紧接 launch 固定 OS boot/PID/start ticks/UID/path/hash，随后 bind 必须仍是相同身份。
- 每次管理命令保存 argv、退出码、完整 stdout 和 host receipt。包括空 stdout log 与
  `bound-identity.json` 都固定字节摘要；运行中观察到 identity/provenance 失配永久撤销 authority。
  安装非零退出同时遇到发布/owner 失败时 SDK rejection 仍是主错误，附注保留次要错误。
  `android_sdk.read_app_identity` 也已修复 finally 中 owner failure 覆盖原 SDK failure 的问题。

新增 [`AndroidRadioController.finish_app_observation`](../../../tools/android/state_control.py)
复用同一 physical phone lock，但只接受从未执行 radio action 的 controller；不读取/修改 radio，
不借用旧 `finish_observation` 的 ON/readback/释放语义。必填 App finalizer 前后检查 owner，成功
发布 journal 后释放；清理、owner、证据发布或释放失败保持 quarantine，保留主错误和异常附注。
旧 radio/permission runner 的两个 finish 方法不变；新方法单次且禁止重入。

Android App finalizer 与 iOS 相同：独立 cleanup deadline 不延长场景；成功检查身份并保留 App，
失败只有在原 bound identity 新鲜核对后才发送一次 `am force-stop --user 0 package`，再检查
package/worker/UID 进程均不在。不重试、不重选、不从未 bind 的结果不明安装中认领进程。
`finalization.json` 不代表 native close、resource-zero 或 RF verdict。

**包级终止限制：** `force-stop` 没有 boot/PID/start-ticks 条件参数，也有比单进程终止更广的
package lifecycle 影响。代码避免在最后身份快照与 mutation 之间再 hash 整个 APK，但不能消除
外部重启/换包竞态；软件通过不等于该风险已被真实设备验证或接受。首次 live cleanup 仍需授权。

CLI 语义核对来源：本机 SDK `sources/android-36/android/app/{WaitResult,ActivityManager}.java`、
platform-tools 37.0.1 的本地帮助文本，以及固定 Android 16 tag 的官方
[adb installer](https://github.com/aosp-mirror-neo/platform_packages_modules_adb/blob/android-16.0.0_r1/client/adb_install.cpp)、
[ActivityManager shell](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/am/ActivityManagerShellCommand.java)、
[PackageManager shell](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-16.0.0_r1/services/core/java/com/android/server/pm/PackageManagerShellCommand.java) 和
[Toybox ps](https://github.com/aosp-mirror-neo/platform_external_toybox/blob/android-16.0.0_r1/toys/posix/ps.c)。
本轮无任何真实 adb 命令执行，以上不是目标手机回执。

## Apple：native boot 与新鲜 challenge

正式 consumer 的 [`AppDelegate`](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHost/AppDelegate.swift)
在创建 RN 前启用 host-only [`FormalNativeProcessIdentity`](../../../tests/consumers/react-native-bleng/ios/BleHubBlengHost/FormalNativeProcessIdentity.swift)，但仅当显式传入：

```text
--blehub-formal-identity-run-id <lowercase canonical UUID>
```

无参数时不创建身份文件/observer/nonce，不创建 BLE owner；同一个 native lifetime 的 nonce 不随
RN reload、视图或 operation owner 变化。正式 Nitro/JS surface 未增加 getter、raw ID 或控制面。
机制复用现有 Foundation/CoreFoundation [HIL support](../../../tests/hil/apple/support/RfFixtureConfiguration.swift)，
保留 Native HIL v2 的 namespace、receipt 和既有 gate。

```text
Documents/blehub-formal-identity/<runId>/
  native-identity.json       {run_id, boot_id, pid, bundle_id, executable}
  identity-request.json     {boot_id, challenge}
  native-inspection.json    {identity, challenge}

Darwin notification: dev.blehub.formal-rn.<runId>.<bootId>.inspect
```

请求上限 1024 字节。先由 Foundation 解码并验证 exact 两字段/canonical UUID，再要求字节表示为
`boot_id` → `challenge` 的规范键序与字面量（允许 ASCII JSON 空白）；重复 key、转义伪装、未知
字段、旧 boot、超长/不安全路径不得产生新响应。没有新增另一套 JSON parser。

### 异步 post 不等于 response 完成

独立 review 在本地 CoreFoundation header 和隔离 SDK mock 中确认：post 返回时，App callback
可能尚未执行。立即读一次会误拒绝仍然有效的进程。

修正后的 formal producer 首次发布先写 `{identity, challenge:null}` sentinel，再发布 identity；
null 不是 live proof。真正响应替换它，同一 responder 再次发布不重建 nonce或重置已有响应。
Native HIL namespace 不预发布该 sentinel。畸形/链接旧 inspection 不会被初始化静默掩盖。

[`ios_sdk.read_app_identity`](../../../tools/react_native/ios_sdk.py) 在固定 install/launch 原始
receipt、CoreDevice ID、唯一 PID/executable 和 native boot 下，每次发一个新 challenge。
request-copy 和 notification-post 各一次；只有本次 challenge 的 exact response 能通过。
同一固定 identity 的 null/旧 canonical challenge 可在原总 deadline 内只读等待，各次 pull 的
文件、SDK receipt 和日志独立保留。foreign/malformed response、owner/provenance 丢失或 SDK
错误立即失败，不重试命令、不重新 pin、不重启 App。成功路径为 6 条 SDK 命令加额外 response
重读次数；命令均有单次与总 deadline。

[`pin_ios_app` / `IosAppBinding`](../../../tools/react_native/bleng_scan_host.py) 同时固定 phone/run、
driver/session/capabilities、candidate SHA 与原始 install/launch receipts。**本地 candidate hash
和 CoreDevice inventory 都不是 installed binary hash**；必填 `require_candidate_provenance`
仍须由本轮 managed install/launch owner 建立，不能默认成功或接受旧 receipt 当作全部来源证明。
身份检查会写入 App container 并 post identity IPC，但不执行 BLE、radio、权限或 App 生命周期控制。

## Apple：本轮 managed candidate owner

[`IosCandidateOwner`](../../../tools/react_native/bleng_ios_candidate.py) 补上真实的 host 侧
install/launch provenance 实现，并通过 `bind()` 接入上述 `pin_ios_app`，不是默认成功的 callback。
调用方显式提供 frozen device `.app`、预期 SHA-256、bundle/executable、physical UDID、run UUID、
活动 observation phone lease、独占 attempt 目录和绝对 deadline。它不自动选择设备或创建租约。

流程为：

```text
验证 frozen App / Info.plist / iPhoneOS 平台
→ installed-app inventory + process inventory：拒绝既有目标进程
→ 一次受控 install
→ installed-app inventory + process inventory：再次拒绝既有目标进程
→ 一次受控 launch（本轮 run 参数，无 terminate-existing / console）
→ 校验并固定原始 receipts
→ bind：existing driver + fresh native challenge
```

- 使用 `result.apps[].bundleIdentifier/url` 找旧安装目录，再匹配 process executable；旧候选改名、
  URL 百分号编码、process 行没有 bundle ID 时，也不能仅凭新 executable 名称漏过旧进程。
- install/launch 及 inventory receipts 均固定本轮准确 argv、raw bytes digest 和同一 CoreDevice ID；
  launch 还校验 actual arguments、非 suspended、非 terminate、activation、PID 和安装目录内的
  exact executable。请求 UDID 不等于 CoreDevice ID，不混淆两者。
- `install_and_launch()` 即使失败也不可重试；launch timeout 可能已在远端启动，不能猜测未启动。
  `bind()` 失败不能重新 pin。候选内容、receipt 或租约校验失败后 owner 永久失效；把文件改回原样
  也不能恢复授权。重复调用已成功的一次性 API 本身不会撤销原有效 binding。
- binding 每个边界分别调用 artifact hash 与 receipt/lease provenance 检查，避免重复 hash；前者
  包含有界文件读取检查，后者不执行任何 SDK 命令。所有设备命令前后仍复查 owner/deadline。
- [`app_artifact.fingerprint`](../../../tools/apple/app_artifact.py) 从 Native HIL runner 提取为纯工具；
  保持历史排序相对路径 + NUL + 内容摘要，增加链接/特殊文件/读取漂移检查和分块 deadline guard。
  它不是原子 filesystem snapshot，不把 modes/signing/安装后二进制内容编码进历史 digest。
- 本地 `retire()` 仅撤销使用权，不终止 App、不释放 phone lease、不证明 native close/resource-zero。
  失败时仍需上层执行已确认身份的 cleanup 或 quarantine；成功页面默认保留。

这是软件 owner，不是已接好的 RF runner。测试 mock 真实 `ios_sdk.subprocess.run`，贯通真实 receipt
parser、App binding 和 challenge reader；未执行设备 install/launch。签名/preflight、WDA owner、
App finalizer 已补为软件实现（见下文），仍须与 WDA/fixture 及最终 bundle 接成完整 runner。
受控命令的来源链也不是 installed binary hash attestation。

### App finalizer：独立 cleanup budget，不重新授权场景

`IosCandidateOwner.finalize(primary_error, deadline=...)` 作为既有
`IosRadioLease.finish_observation(finalize_owned=...)` 的 App 收尾回调；不新增 lease framework。
它开始时退休原 candidate authority，且仅能执行一次。phone owner 继续负责主错误与 cleanup errors
分开记录、证据发布、最终 release 或 quarantine。fixture/WDA 的收尾顺序仍由未来的具名 runner
负责，candidate 不擅自释放其他资源。

- 成功：使用原 bind 时保存的 native identity 做新鲜 challenge；保留 App，不终止／重启或操作 UI。
  结果页的内容和导航仍由 Appium 场景验证，不把进程存活等同于 OS 屏幕持续可见。
- 失败：只有已成功 bind 的同一 native boot 才能进入检查。重新核对 lease、frozen candidate、准确
  receipts 和当前进程后，发送一次 PID-scoped terminate，再读取 inventory 验证 PID/App 不在。
  不 retry、升级 SIGKILL、追踪新 PID 或重新 bind。terminate 超时或仍有进程时保留 quarantine。
- 场景 deadline 与 cleanup deadline 分开：前者耗尽不允许继续测试，但不自动撤销本轮已证明的
  进程身份；仅本地预算耗尽使用专门的内部异常区分。真实 provenance 失配仍永久失效，不能把
  receipt 改回原样再借 cleanup 重新获得授权。新 cleanup deadline 不改写原场景 deadline。
- 未 bind、bind 失败、boot/PID/executable 不符、candidate/receipt/lease 漂移或 cleanup deadline
  耗尽时，不猜测终止未知 App；finalizer 抛错，由实际 observation lease 保留 lock 并 quarantine。
- `finalization/result.json` 记录本次 action、原身份、检查结果和错误；其成功只代表 App 收尾步骤，
  不是 BLeng close、resource-zero、radio restoration 或 host RF verdict。证据写入失败同样不能释放。
  SDK 主错误附带的 ownership/publication 异常现在通过标准库 exception formatter 保留进 phone
  journal，避免只保存在 `__notes__` 后被吞掉；不覆盖原始场景错误。

**PID-only 平台限制仍存在：** 本机 `devicectl device process terminate --help` 只提供 PID，未提供
native boot/audit-token 条件终止。代码已移除 fresh identity 最后检查与 terminate 之间的整包 hash，
并在终止后检查消失，但这些不是原子的“核验 boot 并终止”。phone lease 只约束合作 runner，不能
阻止外部进程在最后一次观察之后退出及 PID 复用。不能声称消除了全部 PID race；该限制和实际
设备行为必须在显式授权的 live cleanup gate 中评估，当前未开放 scan CLI 或执行设备终止。
本机只读 help 保存在 `.work/formal-finalization-terminate-help.log`。

## WDA：显式复用，不夹带 build/deploy fallback

[`create_ios_attached_driver`](../../../tools/apple/ios_appium.py) 要求明确 canonical physical UDID、
bundle ID、loopback WDA URL/port 和有限 HTTP timeout；通过锁定 Appium Python SDK 的
`web_driver_agent_url` 连接已运行 WDA，不设置 signing、derived-data 或 prebuilt/preinstalled build
选项。禁止 App autoLaunch/forceLaunch/reset/quit-termination；失败不回退到另一个部署模式。

SDK setter 的本地文档明确说明该选项连接 existing WDA 而非启动新实例。本次仅检查配置与 SDK
mock 行为，不证明真实 driver 版本不会有其他设备副作用；URL 也不是 WDA ownership receipt。
既有 `create_ios_driver` 仍服务于明确的 WDA build/deploy 场景；新的 helper 未接入默认 CLI。

## 实际执行与红绿证据

各段先新增失败回归再实现：Android SDK/binding、Apple producer、Python reader、native canonical
request、延迟 response/sentinel，以及 WDA attach 缺接口/控制字符拒绝。失败日志保留，不把编译
失败或异步误拒绝抹成通过。

| 命令 | 实际结果 |
|---|---|
| `pixi run just apple bleng-recovery-test` | **38/38 passed**，0 skipped；现有独立 macOS XCTest，无 App/BLE backend/simulator |
| `pixi run just react-native formal-host-ios-build` | 最终 producer/sentinel 的正式 iOS App/native-test graph 编译通过；未 boot/执行 simulator |
| `pixi run just react-native formal-host-test` | **48 passed**，Jest 的 package-boundary UI 回归，不是 native 运行 |
| `pixi run just react-native formal-host-check` | 正式 build prerequisite 中 typecheck/autolinking/no-internal 通过 |
| `pixi run just fmt` / `pixi run just lint` | 通过 |
| `pixi run just docs-check-mermaid` | 通过 |

Python 合并命令（本机 `TMPDIR` 使用已有忽略目录 `.work/ios-observation-lease-validation`）：

```sh
pixi run python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_ios_sdk \
  tools.tests.react_native.test_bleng_ios_identity \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_android_appium \
  tools.android.test_permission_control \
  tools.android.test_state_control -q
```

**200 项通过，最终重跑 8.235 秒**；120 秒上限未超时。mock 的是实际 SDK subprocess/WebDriver 边界，
不是全局无条件成功。Native `NativeProcessIdentityTests` 通过真实 macOS Darwin notification
验证请求限制、响应、sentinel、observer 退休及文件安全边界；不替代 iOS Darwin/runtime。

完整日志：

- `.work/logs/formal-app-identity-python-tests-final.log`
- `.work/logs/formal-app-identity-ios-build-final.log`（内部完整构建日志 `.work/logs/formal-host-ios-build.log`）
- `.work/logs/native-identity-sentinel-{red,final,ios-typecheck}.log`
- `.work/logs/android-app-identity-{red,strict-red,green,regression}.log`
- `.logs/ios-identity-poll-{red,green,final}.log`
- `.work/logs/ios-existing-wda-{red,controls-red,green}.log`
- `.work/logs/formal-app-identity-{jest,lint,final-targeted}.log`

最终静态构建产物：Simulator `BleHubBlengHost.app` 整棵文件树 fingerprint 为
`03449f208c9e2d71652919ef5bcdcc2d6d2715431f3fd811bf8fe30e268b18cd`，采用现有
[`fingerprint()`](../../../tools/apple/native_hil.py) 的排序相对路径 + 文件字节算法。只记录 App
目录树，不以 Debug stub executable 的未变 hash 冒充新实现身份。Native-test executable SHA-256 为
`599737fb83020ec40fcf7ba866515944ec2f3c7b3ecb270b8f1b572cbdb3d24a`；均未安装或运行在移动设备。

某次分段 `pixi run just react-native tools-test` 仍在 180 秒超时，没有全量 verdict，日志
`.work/logs/bleng-ios-identity-tools-test.log` 保留；已检查该 runner 无残留，未延长重试。定向 200 项
不能替代全量通过；先前 [慢测诊断](2026-11-04-formal-rn-scan.md) 的限制继续保留。

### Managed candidate 本次补充验证

先执行新增 candidate 测试，因 owner 模块尚不存在出现预期红灯，再实现安装/启动与 binding。
独立 review 补出旧 executable 改名及失配后重新授权的问题，修正后增加永久撤销回归。
组合回归初跑暴露 fingerprint 提取改变了缺失 App 的异常类别；已恢复原 `ValueError` 契约，
未放宽已有 Native HIL 的安装前拒绝断言。

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_ios_candidate \
  tools.tests.react_native.test_app_artifact \
  tools.tests.react_native.test_ios_sdk \
  tools.tests.react_native.test_bleng_ios_identity \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_ios_appium \
  tools.tests.react_native.test_ios_preflight \
  tools.tests.react_native.test_ios_observation_lease \
  tools.tests.react_native.test_apple_native_hil -q
```

**191 tests passed，37.539 秒**。这是本次相关组合，范围不同于前段 200 项，不累加成覆盖率。
SDK/driver 为隔离模拟；日志中的 `formal_rn_lifecycle_ui=failed` 是保留失败场景的预期测试输出，
最终 unittest 为 OK，不是本次设备执行失败或真机 Passed。

另执行 `pixi run just react-native formal-host-check`（typecheck/autolinking/no-internal 通过）、
`pixi run just react-native formal-host-test`（48 passed），以及 `pixi run just fmt`、
`pixi run just lint`、`pixi run just docs-check-mermaid`（均通过）。未重复盲跑已知超时的全量
`tools-test`，其未通过状态不变；没有重新执行 Apple native build/runtime。

本次日志为 `.work/formal-candidate-{regression-initial,regression,host-check,host-test,fmt,lint,mermaid}.log`。

### App finalizer 补充验证

新增 success-retain + 实际 observation lease 测试先因缺少 `finalize` 红灯，然后实现回调。
补测使用临时目录中的真实 phone lock/journal 与 SDK subprocess mock，覆盖原场景超时后独立
cleanup、fresh challenge 后单次 terminate、仍存活/替换进程、未知 boot、未 bind/失败 bind、
provenance 永久失效、cleanup 自身超时、证据发布失败、SDK failure + owner loss + publication
failure 三者同时出现。主错误不被覆盖，后两层附注均进入持久化 journal，不假装已清理。

- 重跑上节同一 Python 组合命令：**201 passed，31.176 秒**。
- `pixi run just --command python -W error::ResourceWarning -m unittest tools.tests.react_native.test_ios_radio_lease tools.tests.react_native.test_ios_radio_quarantine_restore tools.tests.react_native.test_ios_observation_lease -q`：**41 passed**；与前项有重叠，不累加为覆盖率。
- `pixi run just react-native formal-host-check`：typecheck/autolinking/no-internal 通过。
- `pixi run just react-native formal-host-test`：**48 passed**。
- `pixi run just fmt`：通过。
- `pixi run just lint`：**未通过**，Clippy 在当前工作树的
  `crates/blehub/src/tests/assembly_tests.rs:9` 报 `unused import: PlatformErrorDetails`，
  不属于本轮 Python 修改，未代为修改。不能沿用上一轮 lint pass。
- 单独 `pixi run just --command ruff check tools tests/support/rf_lab tests/peripherals/nrf52840/support`
  和 `pixi run just docs-check-mermaid`：通过。

日志：`.work/formal-finalization-{red,targeted,regression,lease-regression,host-check,host-test,fmt,lint,ruff,mermaid}.log`。
完整 `tools-test` 的既有超时未重新盲跑，仍无全量通过结论。本轮仅查询本机 `devicectl terminate --help`，
未运行设备查询、安装、终止、Appium/RF、radio/permission mutation 或 native build/runtime。

### Android managed candidate 补充验证

候选 owner 测试先因缺少模块红灯；App-only lease 的独立测试也先验证缺接口红灯。
实现及 review 修正后执行：

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_candidate \
  tools.tests.react_native.test_android_observation_lease \
  tools.tests.react_native.test_android_sdk \
  tools.tests.react_native.test_android_rf_host \
  tools.tests.react_native.test_android_appium \
  tools.tests.react_native.test_bleng_scan_host \
  tools.tests.react_native.test_bleng_scan_ui \
  tools.tests.react_native.test_bleng_ui \
  tools.tests.react_native.test_bleng_ios_candidate \
  tools.android.test_state_control \
  tools.android.test_permission_control \
  tools.tests.react_native.test_run_android_permission_transition \
  tools.tests.react_native.test_run_android_adapter_transition -q
```

**242 tests passed，25.475 秒**。包括真实临时 phone lock 与实际 SDK subprocess mock，验证无 root/
permission/radio 命令、单次安装/启动、shared UID/multi-user/worker 拒绝、安装 hash 失配前置拒绝、
非冷启动拒绝、绑定时 restart、运行中漂移后恢复仍失效、原预算耗尽后独立 cleanup、cleanup 超时、
worker 残留、未知 owner 不删除、非零 SDK + receipt 发布失败双重错误、空日志与 identity 证据完整性。
不是实际设备安装/执行，也不与前面的测试数累加为覆盖率。

`pixi run just react-native formal-host-check`、`pixi run just react-native formal-host-test`
（48 passed）、`pixi run just fmt` 与 `pixi run just lint` 均通过。前段 Rust unused-import lint
失败作为原记录保留；本轮在更新后的工作树重跑通过，未为此修改该 Rust 测试文件。
日志：`.work/formal-android-candidate-{red,targeted,regression,host-check,host-test,fmt,lint}.log`。
全量 `tools-test` 仍未重新盲跑，未执行 native build、移动 runtime、adb/device、Appium 或 RF。

## 后续衔接

[Prepared scan 候选](2026-11-04-formal-prepared-scan.md) 已在后续软件组合中接通已准备资源之后的
shared scene、严格 passive fixture close 和跨资源收尾，并修正 Android observation 准入互斥及
完整 identity 检查过密的问题。以下外层准备与真实运行缺口仍然成立；后续阶段 pass 不改写本报告
各段原测试范围、失败或移动 runtime/RF 限制。

## 尚未完成

- iOS/Android managed install/launch、pin/check、App finalizer 与实际 observation lease 的软件组合
  已实现；physical phone selection、签名/preflight、WDA/UiAutomator2 preparation/attach、
  fixture/automation/App 的完整收尾次序与最终 evidence bundle 仍未接成完整 runner。
  PID-only termination 的非原子边界及真实执行结果仍须显式评估，不能把软件测试当作已接受／通过。
- 真实手机 procfs、hash、Darwin IPC/WDA 行为，以及频繁 fresh checks 的耗时。不能缓存过去成功
  来冒充当前 incarnation。同步 callback 与在途 SDK/HTTP 调用的有界性仍由对应 host/SDK 共同保证。
- iOS/Android native runtime、Hermes/reload、RF 与其余 connect/GATT/I/O/notification 迁移。

仍不开放 scan CLI，不删除 legacy internal，不声明 resource-zero、RF、release、ABI/Phase exit。
本轮未执行设备安装/启动、权限/radio 变更、固件 provision 或 RF，未提交 Git。
