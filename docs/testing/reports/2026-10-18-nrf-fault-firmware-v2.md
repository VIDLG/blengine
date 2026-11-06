# 2026-10-18 nRF52840 Multifunction Fault v2：实现、构建与迁移

## 结论与证据边界

**已完成前向源码实现、静态安全审查修复、启用/关闭 ARM 编译、A/B 真实两遍构建与 expected contract 导出/校验，以及现有 read-fault host/RN 调用链迁移。没有 provision、安装证明或新 RF pass。Linux/BabbleSim 尚未运行，完整 RN iOS Simulator 构建本轮超时。**

用户明确批准不维护 v1、可以修改实际构建 clone 并本地 commit；本轮未 push、未创建新分支、未刷机/reset、未访问设备串口、未恢复 `IOS-RADIO-GATE`，未请求密码或安装 SDK/toolchain。仍使用同一个 RN App/workbench。

职责与协议见[固件架构](../../research/peripheral/nrf52840-fault-firmware-v2.md)，执行入口见[provider README](../../../tests/peripherals/nrf52840/README.md)。本报告不改变 Plan 发布顺序、ABI freeze 或退出条件。

## 1. 源码候选与提交范围

实际实现位于 nrftest 本机配置选择的独立 Zephyr Git clone，不是编辑器 `contrib/zephyr`。本机相对位置为 `nrftest/.work/upstream-attested/zephyrproject/zephyr`；运行时必须重新解析配置，不把该路径当通用默认。

```text
base:   d31eebc4557c7a0a8a64e77b9642ee9a341661f4
commit: 82f7b2b0e63960f18fed3fff3ab376b0ef443cb9
subject: bluetooth: tester: unify nonblocking ATT fault controls
```

该提交包含23个精确选择的文件：ATT hook/engine及配置、Tester/vendor/有界UART交接、文档与真实peer回归源码。提交后该clone clean，保持原配置的 detached checkout，没有修改独立编辑器clone或推送remote。遵守agent规则未代加人类 `Signed-off-by`；这是本地待运行验证候选，不宣称已完成上游DCO提交流程。

`nrftest/upstream.lock.toml` 已指向新commit。69个West projects重新验证clean且pins一致，`west.yml`未变；仅更新ignored ready marker，未运行 `west update` 或下载。BleHub/nrftest原工作树已有大量交叉未提交改动，本轮保留它们，**没有批量stage/commit这两个仓库**。因此Zephyr提交并不意味着跨仓库host/builder工作已全部提交。

## 2. 非阻塞实现与审查修复

- READ/response-write/managed CCC共用slot，真实ATT ingress在目标callback前复制请求。
- ARM预留 `K_NO_WAIT` response，正常执行复用原ATT路径；拒绝/expiry/validation error也使用reservation。
- connection ref、ATT context pin、channel pin分开；detach逻辑失效后等待metadata/TX/evidence排空。
- response outstanding期间阻止第二Request产生冒充response；L2CAP FIFO answer-ordered后允许正常下一Request，不等待旧observer。
- 所有Tester reply/event，包括异步GATT client completion，复制进有界UART TX handoff；RX owner不等UART。
- vendor version2 `0x10–0x16`，attestation仍为opcode1/schema1；旧semaphore及 `0x24–0x27` handlers删除。
- 旧untracked read-hold shim确认无活跃调用后删除，不加入dirty allowlist。

独立静态review发现并修复了六个具体问题：

1. sticky evidence error重复退役使cooperative RX自排队饥饿，阻止TX cleanup。
2. 异步BTP回复仍可绕过event queue等待UART，以及inline completion的command ownership。
3. unused ARM取消/过期后reservation排空期间误拦目标Write Command。
4. answer-ordered后的普通allocation failure覆盖原COMMIT winner/outcome。
5. owner跨retirement deadline后才处理drain，却错误发布REARMABLE。
6. 首Request mismatch未CAPTURED且HCI allocation失败、连接仍CONNECTED时，后续Request/Command穿透unused-arm gate。

对应真实peer回归已加入 `tests/bsim/bluetooth/host/att/fault/src/review.inc`。最终复审未保留已知静态阻塞项；这不是并发运行验证。ARM compile-only工程保留测试role但不执行、无可工作的bsim backchannel/verdict，不能记作测试运行通过。

## 3. A/B真实构建与expected contracts

输入位置均相对BleHub：

- `tests/peripherals/nrf52840/firmware/images/multifunction-rf-v2-{a,b}.json`
- `tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-{a,b}.json`
- 导出：`tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-{a,b}.json`

A/B均精确11 capabilities、16 renderer slots、五项settings；attestation payload实际370 bytes，BTP payload上限1019。`CONFIG_BT_TESTING_ATT_FAULT_REGRESSION=n`，无旧read-hold配置。

| Artifact | A SHA-256 | B SHA-256 |
|---|---|---|
| Normalized image | `fa0d485d3e60d6738d8472516ecb19f36dd9d7d35d33937f0c91f8ea1696754a` | `97ffaf30e28f87ff2b2266d68999454b2b266f4e7a8519492d2d236e83a3b52e` |
| ELF | `cfb24d10c457b29b6e1c2b56cf16f98a6ecc41f48da3b2a8bef160b6ccb2e8f2` | `5a11a51c10ec9c0a1467c98abaaf7af2342e27fb7ce1929c974490fb8d6c8a1a` |
| Raw HEX | `3b69f7299ddfb03102e860b9577a813676ad79329194f2f163463bf81b3db31b` | `5ce3ef873127fddff5e4d65d8820868a3bcb9445d62755c334222ab1005cbde8` |
| Raw BIN | `4c0f71535cf07e03ad111bc3d9bc8cd4cccd739fbaab8d87015d607effaebe64` | `4de8b3a4c155d6c1fe1cab544b075b3ca7e24f0f7d9f788f4726f931dcc04841` |

两个manifest分别位于nrftest本机ignored `build_dir/images/nrf-multifunction-rf-v2-{a,b}/nrftest-build-manifest.json`。Expected contract记录normalized image digest；将来显式provision确认的是manifest中的raw HEX hash，不混用。

Source audit schema2/clean，共357个files，覆盖Tester、Bluetooth host、公共Bluetooth headers。共同inventory SHA-256为 `d8e3d91ab0463b53ab1339e68ae8beeef3925f43529d49512e255c9cef68ca85`。实际compile command指向锁定clone的 `att.c`，Ninja dependency记录消费 `att_fault.h/.inc`；commit、工作字节、inventory和archive一致。最终 `att.c` SHA-256为 `f5d7cffb53da328e97b621d91d9994896c5899e75f81f7e4a0ff1103aaa9f628`。

Inventory是scoped source证据，不是完整SDK/compiler dependency closure或installed binary密码学证明。

## 4. Host/RN前向迁移

[`fault_control.py`](../../../tests/peripherals/nrf52840/fault_control.py) 提供typed wire、严格length/enum/reserved-bit校验、完整ticket、limits/peer/epoch绑定。Attestation codec payload上限由1024修为1019，并增加1019接受/1020拒绝及UTF-8预算回归。

现有full-suite的cancellation、continuous watchdog、handoff、RecoveryTen、pending engine reload、独立cancellation/watchdog及适用adapter/permission路径迁到v2。App在connected+discovered后通过既有barrier等待host ARM，确认后再read，不再pre-launch ARM。iOS single-control复用现有Darwin notification→Linking→ACK路径，没有新增App、页面或通用桥接框架。

断连后只查询exact retained STATUS，不再对旧ticket RELEASE/ABORT。Cleanup同时约束firmware resource-zero、native旧owner归零、peer/boot/lease与绝对deadline；GAP cleanup后重采STATUS，不能复用动作前样本。Host hold预算从STATUS请求开始计时，回复延迟不续期。Backend默认10s watchdog未改。

当前Just默认选择v2 contract/profile，live session拒绝旧normal/multifunction family。v1实现及独有安全回归归档；历史JSON保留原路径和原字节作为唯一evidence-only副本，以维持原报告链接，profile discovery明确标记evidence-only，不构成运行兼容层。`nrf52840-start`旧桥接入口fail closed。

**尚未新增write/CCC RF matrix。** C hook支持这些操作、codec测试通过、已有read full-suite迁移均不能代替真实write/CCC故障证据；也没有新candidate的普通RF、multi-DK或native/package pass。

## 5. 实际验证命令与结果

构建完整输出保存在ignored日志；下列是实际执行的recipe，绝对JSON参数按上述真实文件解析，而不是占位artifact/hash：

| 命令/检查 | 结果 |
|---|---|
| nrftest `pixi run --frozen just verify-upstream` | 锁定source、69 West projects与SDK通过 |
| nrftest `pixi run just firmware-build-dk-image`，A后B，各两遍 | 通过；第二遍normalized digest稳定 |
| nrftest `firmware-export-contract`、`firmware-check-contract`，A/B | 新文件独占导出与check均通过 |
| enabled/disabled nRF52840 DK Tester West build | 通过；disabled无fault symbols，普通enabled无regression injection symbols |
| 真实peer回归源码ARM compile/link | 通过，未执行role |
| Zephyr checkpatch、`.inc`按C补查、diff whitespace、script语法/执行位 | 0 errors / 0 warnings；通过 |
| Linux/BabbleSim build attempt | Mac配置失败：POSIX architecture要求Linux；未运行 |
| nrftest `pixi run --frozen just test`（最终重跑） | 664 passed、1 skipped（Windows-only）、4 subtests passed |
| nrftest `pixi run --frozen just lint`（最终重跑） | 通过 |
| BleHub `pixi run just react-native fault-inputs-check`，A/B | 均通过，无硬件访问 |
| BleHub `pixi run just peripheral nrf52840-test` | 294 + 95项通过 |
| BleHub `pixi run just react-native tools-test` | 469项通过 |
| BleHub `pixi run just react-native tools-fixture-test` | 820项通过 |
| archived diagnostic unittest discovery | 44项通过，独立于live discovery |
| RN Jest `--runInBand` | 13 suites / 356项通过 |
| `react-native host-check`、`host-ios-handoff-test` | 通过；后者2项，不等于App构建 |
| BleHub `pixi run just react-native host-ios-build` | **600,000ms超时终止**；此前typecheck/generate/Pods通过，unsigned Simulator xcodebuild未取得完成结果 |
| BleHub `pixi run just fmt`、`lint`、`docs-check-mermaid` | 通过；文档报告完成后再次验证 |

Simulator使用 `CODE_SIGNING_ALLOWED=NO`，未请求密码。外层日志停止于xcodebuild调用，quiet build日志未提供可定位错误；不推断为签名失败，也不自动加时重跑。Source parse/handoff单测不能替代该完整App build。

最后定向文档检查通过6份文档的UTF-8、fences、空白、冲突标记与122个本地链接；Mermaid检查通过145份Markdown中的27个图。编辑器diagnostics不是全绿：Zephyr孤立header报告 `zephyr/bluetooth/conn.h` 不可见，Apple source报告 `BleHubUniFfi` module不可见，未通过改源码掩盖这些编译上下文问题。三个本轮nrftest unit文件的3/14/15个类型errors已用typed helpers修复并归零，剩余warnings未作无关清理；随后664项test及lint再次通过。

本机ignored完整证据位置：

- Zephyr clone `build/att-fault-v2/`：build commands、审查patch、enabled/disabled/regression编译和bsim配置失败日志。
- nrftest `.work/firmware-v2-build-82f7b2b0/`：two-pass/export/check完整日志、`artifact-verification.json`、最终test/lint输出。
- BleHub `.work/fault-v2-finish/`、`.work/fault-v2-review/`：分轮增量文件列表、patch、软件验证与Simulator超时外层日志。

## 6. 下一道真实门禁

1. 在Linux/BabbleSim执行真实peer、调度、UART饱和、deadline与故障注入回归；补齐尚未覆盖的late TX/channel reuse/精确deadline交错，不以ARM编译替代。
2. 完成本轮超时的完整RN iOS App构建；超时不是pass，也不是已定位源码错误。
3. 显式授权并选择DK identity后才provision，随后在physical lease内读取新installed-image receipt。普通run不刷机。
4. Android/iOS分别重跑正常RF/full-suite与适用独立门禁，再接入write/CCC REJECT/watchdog/disconnect专项。不能借旧v1或另一平台pass。

`IOS-RADIO-GATE`仍deferred；10,000 lifecycle、长稳、Android API档位、最终同candidate native/runtime/ABI/package gates继续按Plan检查，不因本轮固件重构或软件测试计数而完成。
