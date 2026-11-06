# nRF52840 installed-image attestation transport smoke

日期：2026-10-13

## 结论

真实 nRF52840 DK 上的 BleHub-owned BTP attestation 控制面 smoke 已通过，随后 DK 已恢复为 locked normal Tester image。

这不是正式 fixture contract gate，也不是 RN RF 结果。smoke 使用了隔离的临时 Tester app copy 和 compile-check metadata，仅证明：

```text
J-Link/SWD flash
→ nRF52840 Tester
→ BTP VCOM
→ vendor 0xff / opcode 0x01 / index 0xff
→ host wire decoder
```

## 执行对象

- board：`nrf52840dk/nrf52840`
- SEGGER J-Link serial：`1050252028`
- source revision：`b5f309bc2da57b49012648c9432d0d4cb5ee1cc8`
- Zephyr SDK：`1.0.1`
- transport：J-Link SWD + DK BTP VCOM
- smoke HEX SHA-256：`a2076d761068b9683b682f88f3d508dbe4bc57aa9f93f0b33a6caa66d9ebf726`

smoke metadata：

- `image_id=nrf-attestation-transport-smoke`
- `image_version=2026-10-13`
- `board=nrf52840dk/nrf52840`
- `profile_id=blehub-nrf-attestation-transport-smoke`
- `service_uuid=fd100000-0000-4000-8000-000000000000`
- `local_name=BleHubAttestationSmoke`
- `capabilities=[diagnostic-btp]`
- compile-check `image_sha256=00...00`

该 hash 是刻意的 placeholder，不能用于正式 contract。

## 结果

### 编译

隔离 app copy 使用本地 Zephyr fork 的 attestation source overlay，ARM build 通过：

```text
[433/433] Linking C executable zephyr/zephyr.elf
FLASH: 425680 B / 1 MiB
RAM:   98496 B / 256 KiB
```

### normalized digest follow-up

transport smoke 之后，使用同一 locked Zephyr workspace、同一 `nrf52840dk/nrf52840` ARM 配置和隔离 app/build 目录进行了第二遍构建验证。firmware 的唯一 `BLEHUB_ATTESTATION_SHA256_SLOT_V1` slot 在 Kconfig 中写入第一遍计算出的 normalized digest；host 对完整 `zephyr.bin` 将该 slot 归零后重新计算，结果一致：

```text
configured_image_sha256=e0b61e424deab0542f7f7811fd7e7a3c7a4f8f78d8ebd9dff454df17dc81d761
normalized_image_sha256=e0b61e424deab0542f7f7811fd7e7a3c7a4f8f78d8ebd9dff454df17dc81d761
```

本步骤只验证 digest 算法、firmware slot 和 ARM artifact 的闭环；没有刷写 DK，也没有把该隔离 build 发布成正式 fixture contract。

### candidate attestation-only ARM image

在同一 locked Zephyr workspace 上，将当前本地 Zephyr attestation source prototype 复制到隔离 Tester app，使用仅包含 `diagnostic-btp` capability 的临时 profile 完成了两遍 `nrf52840dk/nrf52840` ARM build。该 profile 不包含 pending-read capability，因此不能用于 pending-read RF gate。

```text
pass1: [427/427] Linking C executable zephyr/zephyr.elf
pass2: [427/427] Linking C executable zephyr/zephyr.elf
FLASH: 346268 B / 1 MiB
RAM:   92480 B / 256 KiB
configured_image_sha256=b686878004614e4f78bc2465aebff581d68d655de2f2ff0360973ed5f1648cd1
normalized_image_sha256=b686878004614e4f78bc2465aebff581d68d655de2f2ff0360973ed5f1648cd1
```

这是当前 source prototype 的 candidate artifact evidence，不是已锁定 revision、已刷写 installed image 或正式 RF contract。

### combined pending-read + attestation ARM candidate

随后在一个隔离的 Zephyr clone 中合并了当前本地 attestation prototype 与
`0001-bttester-gatt-read-hold.patch` 的 pending-read 支持。pending-read 补丁的
`Kconfig` 片段因 prototype 已整理过缩进而手工等价合入；原始 Zephyr 工作树没有被修改。
使用 `nrf52840dk/nrf52840`、Zephyr SDK `1.0.1`、同一 DK config/overlay 和
`blehub-nrf52840-att-read-hold-v1.conf` 完成了两遍 ARM build：

```text
pass1: [427/427] Linking C executable zephyr/zephyr.elf
pass2: [427/427] Linking C executable zephyr/zephyr.elf
FLASH: 347676 B / 1 MiB
RAM:   92544 B / 256 KiB
configured_image_sha256=b864ace501523f51eda25cbb7b5538c73cc88ba777544c409c865d2544c872a1
normalized_image_sha256=b864ace501523f51eda25cbb7b5538c73cc88ba777544c409c865d2544c872a1
```

最终 `.config` 和 binary 检查确认：

- `CONFIG_BTTESTER_GATT_READ_HOLD=y`；
- `CONFIG_BTTESTER_GATT_READ_HOLD_TIMEOUT_MS=5000`；
- `CONFIG_BTTESTER_BLEHUB_ATTESTATION=y`；
- capabilities 同时包含 `att-read-hold`、`att-read-hold-timeout-5000`、`gatt-reset`、`diagnostic-btp`；
- binary 含唯一 `BLEHUB_ATTESTATION_SHA256_SLOT_V1` marker；
- digest slot 归零后的 normalized digest 在 pass 1/pass 2 完全一致。

这是 **combined candidate ARM build evidence**。由于 Zephyr source 仍是本地未提交 prototype，
该构建使用直接 west/CMake 验证而非 nrftest 的 clean-locked preflight；它不是正式 locked
revision、已刷写 installed image、pending-read RF gate 或 RN evidence。

### fork source integration and fail-closed artifact gates

pending-read 的同等变更随后合入本地 `contrib/zephyr` 工作树，与原有 attestation
prototype 共存；managed locked Zephyr checkout 仍未修改，lock revision 未更新。
Kconfig 按 Zephyr flat layout 校正，source 仍未提交。

从该工作树通过临时 west workspace 复用现有 modules/SDK，完成两遍 ARM build：

```text
pass1: [427/427] Linking C executable zephyr/zephyr.elf
pass2: [427/427] Linking C executable zephyr/zephyr.elf
FLASH: 347732 B / 1 MiB
RAM:   92544 B / 256 KiB
configured_image_sha256=7b2866df2152e024e4a1086a0aa7b3aa325e9bb7695d8e2d699309db8d3c4272
normalized_image_sha256=7b2866df2152e024e4a1086a0aa7b3aa325e9bb7695d8e2d699309db8d3c4272
```

该次 candidate 的构建路径/工作树与前一隔离 clone 不同，因此只记录该次产物身份，
不把两次 artifact 当作同一 candidate。nrftest 新门禁对真实输出通过：

- 每项请求 profile 设置与最终 `.config` 一致；
- 第一遍 digest slot 为 zero placeholder，第二遍为计算出的 normalized digest；
- flashable HEX 对应地址区间与 BIN 完全一致；
- 生成身份包含四项预期 capability。

同时完善 nrftest 软件门禁：新 build attempt 使旧成功 manifest 失效；最终 manifest
保存已验证配置与 installed identity；provision 在刷写前将 manifest/HEX/contract
绑定，刷写后与身份快照精确比对。回归测试覆盖 wrong resolved configuration、stale
embedded digest、HEX/BIN mismatch、失败重建、普通/旧 manifest 拒绝以及 wrong installed
identity；这些软件测试不等同于 physical provisioning。

同一本地 Zephyr 工作树还完成了不加载专用 profile 的普通 Tester ARM 编译：
`427/427`、FLASH `345588 B`、RAM `92480 B`，最终配置确认 read-hold 与 attestation
均未启用。此结果验证专用能力默认关闭时仍可构建普通固件，不证明普通固件 RF 行为。
nrftest 全量软件验证为 `188 passed, 1 skipped, 4 subtests passed`，fmt/lint 通过；
Zephyr Kconfig style 与 diff whitespace 检查通过。临时 workspace、两种 build 输出和
生成 profile 在验证后清理。

本步骤没有刷写 DK、发布正式 contract 或运行 RN RF。provider/provisioning 的统一
独占锁与 immutable artifact 消费仍需完成，检查与使用之间的文件替换风险尚未完全消除。

### 刷写和读取

通过显式 J-Link serial 刷入 smoke image 后，host capture 成功：

```text
fixture_attestation_capture=passed
fixture_image_id=nrf-attestation-transport-smoke
fixture_image_sha256=0000000000000000000000000000000000000000000000000000000000000000
```

原始证据文件：

- `.work/peripheral/nrf52840/installed-attestation-smoke.json`
- `.work/peripheral/nrf52840/installed-attestation-smoke.json.provenance.json`

capture 是只读的，不刷机、不 reset、不启动 RF provider；刷写动作单独使用显式 J-Link 路径完成。

### 恢复

之后使用 locked normal Tester 重新 build/flash：

- normal HEX SHA-256：`0dd62fc15ab44aed01325d7cfea055db82014df1f94da77844f10c34ec16cefe`
- flash report：`nrftest/.work/reports/firmware-flash-dk/20261013T003637.393743Z-1b43e2f956ac4628be87c93d97218fd5.json`

恢复后再次读取 attestation 得到预期的 fail-closed 结果：

```text
fixture firmware does not implement image attestation vendor command
(service=0xff opcode=0x01)
```

因此当前 DK 没有被错误地留在 smoke image 上。

## published revisions and clean-locked build follow-up

经用户明确授权，仅提交并推送两个 fork 的相关文件；未 force-push，未包含 AutoPTS
无关未跟踪目录。远端引用核实：

- Zephyr `main`：`d31eebc4557c7a0a8a64e77b9642ee9a341661f4`；
- AutoPTS `master`：`955f014070c21a3fe8a04a55acc4006917b1d509`。

随后更新 `nrftest/upstream.lock.toml`，在 `.work/upstream-attested` 建立独立 clean
checkout 与 west workspace。68 个 module 从现有本地 Git 对象独立克隆，执行正式
`setup-upstream-sources` 后，`verify-upstream` 验证 69 个 West projects 和 SDK `1.0.1`。
旧 `.work/upstream-v5` workspace 保持不动且 Zephyr checkout clean；新的 Zephyr/AutoPTS
checkout 也 clean。忽略的本机配置切换源码/build 路径，设备 selector 和 SDK 路径未改。

正式 `pixi run just firmware-build-dk-attested` 通过：

```text
pass1: [427/427] Linking C executable zephyr/zephyr.elf
pass2: [427/427] Linking C executable zephyr/zephyr.elf
FLASH: 347676 B / 1 MiB
RAM:   92544 B / 256 KiB
normalized_image_sha256=b864ace501523f51eda25cbb7b5538c73cc88ba777544c409c865d2544c872a1
hex_sha256=e8cf6e682033d3428869efad5e346e785ebebcec9fd44cb210b7d9ac27e5abbf
bin_sha256=50f506aaa66ea6ee2ff3821c0cbfc6a359088ab9cd416e3e6974626573c1f890
```

本地 manifest：`nrftest/.work/build-attested/nrf52840dk-tester/nrftest-build-manifest.json`，
记录正式 Zephyr revision、input/profile hashes、validated Kconfig、installed identity 和
artifact hashes。`diagnostic-pending-read-v1.json` 已更新为该 normalized digest。
这里的 manifest identity 是 **预期安装身份**，不是设备回读。nrftest host/profile 与
BleHub contract 的本轮修改尚未提交；本次 clean-locked 指 upstream checkout 和 firmware
构建门禁，不表示所有 host/RN 代码或整个 release candidate 已冻结发布。

验证：AutoPTS 3 个 codec/transport 单测通过，nrftest 全量 `188 passed, 1 skipped,
4 subtests passed`，fmt/lint 通过。编译仍有普通 DK board logging/RTT 配置在关闭
console/log 后的 Kconfig dependency warnings，但没有 read-hold/attestation undefined
symbol、digest mismatch 或产物验证错误。

本轮没有刷 DK、生成 installed-attestation 文件或运行 RN RF；新 clean workspace 和
正式 artifact 保留以供下一步显式 provision，临时 module seed script 已删除。

## shared physical lease / immutable consumption follow-up

后续实现了 nrftest 的 serial-based `DeviceLease` kernel lock authority，以及 BleHub 的
Pixi broker wrapper。AutoPTS、直接 BTP fixture、provider doctor、只读 diagnostic doctor、
installed capture 和 pending-read diagnostic 使用同一物理设备命名空间；VCOM 路径不是 key。
J-Link USB/debugger 前导零 aliases 冲突，两个不同 DK 不冲突。借用 lease identity 校验失败
后重试仍保留原借用约束，不能自动变成自行取得锁。

显式 DK provision 在一个 lease scope 中完成 snapshot/preflight/flash/physical readback/assert。
HEX/manifest 从稳定 regular file 复制到 attempt-owned 私有只读快照，刷机只消费该快照；
canonical build 文件在快照后被协作重建替换，不改变本次消费。contract 比对使用刷写前快照。

软件验证：

- nrftest `pixi run just fmt`、`lint`、`test`：`255 passed, 1 skipped, 4 subtests passed`；
- BleHub nRF subtree：`139 tests`，通过，ResourceWarning 作为错误；
- BleHub 改动 Python 文件的 Ruff lint/format：通过；
- BleHub `fmt`、`lint`（含 workspace Clippy/profile isolation）：通过；
- Mermaid：24 diagrams / 124 Markdown files，通过；
- 真实子进程 broker 冲突、不同设备并行、EOF 后重取锁、read/start/close 故障回收：通过；
- Pixi launcher readiness/EOF/reacquisition：通过，使用临时锁目录和 synthetic serial，未访问硬件；
- 保留的正式 HEX/manifest 经新 snapshot/preflight 校验：通过，normalized digest 仍为
  `b864ace501523f51eda25cbb7b5538c73cc88ba777544c409c865d2544c872a1`；私有快照离开 scope 后已删除。
  此检查只读取构建文件，没有打开硬件或生成 installed proof。

这些结果验证协作锁与消费快照，不防御恶意 same-UID 修改或外部非协作刷机。
macOS/POSIX 已验证；Windows launcher descendant cleanup 和 parent-death EOF 不作通过声明。
RF suite 仍需要把设备即时 attestation 与同一租约内的 RF 生命周期绑定；历史 JSON preflight
尚不能单独证明新 session 使用的 installed image。本轮未刷写 DK、未生成真实 installed proof，
也未重跑 Android/iOS RN RF。此前 clean-locked build 保持为 artifact evidence。

## continuous RN live-session follow-up（软件验证）

Android/iOS 连续 RF runner 已接入 `fixture_session.py` 的即时证明协议。每次 attempt 保存
UUID request、contract/profile 私有只读快照、hash 和预先枚举的物理 USB serial。fixture
在取得同一设备租约后、打开串口前验证目标身份，再通过同一 BTP session 读取实际 vendor
attestation；验证失败时不进行 GAP/GATT mutation、不广告、不打印 READY。新 receipt 绑定
session UUID、快照 hash、installed identity 与 child PID/PGID，不能覆盖或复用旧 receipt。

host 校验成功 startup stdout 中的唯一 PID/PGID 和当前 receipt 后才安装/启动 App，并在运行
期间与成功前检查 ownership。Android recipe 不再提前安装 APK；安装位于 runner 的即时证明
之后。fixture 的串口 I/O 与 suite 本地 marker 等待都会检查租约。失效时 fail closed，iOS
只终止自身 runtime controller process group。启动失败不能收养共享 PID marker；marker
被替换时不能停止别人的 fixture。cleanup 失败阻止最终 `:passed`，并保留原故障的 cleanup notes。

历史 attestation JSON 改为可选 expected baseline，不再单独授权新 RF。无证明的显式开发模式
打印 `compatibility-non-gate`，不等同 gate pass。成功/失败日志与 request/receipt 均保留在
UUID attempt 目录。标准 `pixi run just peripheral nrf52840-test` 现在覆盖这些软件边界。

验证：nRF subtree **174 tests**、RN host 工具 **150 tests**（包含 continuous runner 的 **30 tests**）
均通过，ResourceWarning 作为错误；nrftest 仍为 **255 passed, 1 skipped, 4 subtests passed**。
RN 工具目录是 namespace package，`unittest discover -s tools/react_native -t .` 直接发现失败；
改用枚举 `test_*.py` 模块并由 unittest loader 加载后，150 项通过，没有为 discovery 增加无关文件。
真实 BTP codec/session 用 fake
transport 验证 receipt 链路，USB/lease/native commands 在单测中受控模拟；这些不是 RF 结果。

本步骤未刷 DK、未生成真实 installed-session receipt，也未启动 Android/iOS RF。normal
attested image/contract、正式 hardware provision/readback 与 same-image continuous RF rerun
仍待完成。request/receipt 是协作证据，不是签名 attestation，不防御恶意 same-UID writer。

## 当前边界

已验证：

- vendor service 不会触碰标准 BTP service-handler 数组；
- Kconfig metadata 可以进入 ARM firmware；
- firmware TLV 与 host codec 真实互通；
- J-Link serial、VCOM 选择和 installed readback 路径互通；
- unknown command 仍然 fail closed；
- 旧 managed locked Zephyr checkout 在实验后保持 clean；
- Zephyr/AutoPTS 扩展已提交并发布，nrftest lock 已绑定真实远端 revision；
- pending-read clean-locked 双阶段 build、profile/embedded-digest/HEX-BIN 门禁通过；
- pending-read contract 已绑定本次已验证产物，尚不证明 DK 已安装它；
- provider/provision/capture 共享物理租约及 attempt-owned HEX/manifest 消费的软件门禁通过；
- continuous RN session 的即时证明/owner guard/cleanup failure 软件门禁通过，尚未 RF rerun。

尚未完成：

- 提交并发布 nrftest host/profile 集成与 BleHub contract/documentation 的修改；
- 为 normal、watchdog image 接入正式 metadata profile 和 contract hash；
- normal/watchdog fixture image 对即时 session 协议的正式真机验证；
- 新 locked pending-read image 的正式 physical provision/readback/assert gate；
- 正式 contract-attested RF run；
- RN Android/iOS continuous suite 的 attested image gate。

所以这次结果升级的是 **physical attestation transport readiness**，不是完整 RF Lab production readiness。
