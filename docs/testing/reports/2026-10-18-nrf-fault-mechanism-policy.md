# 2026-10-18 nRF fault：机制/策略分离与所属仓库提交

## 结论

用户要求可复用机制留在底层、策略上移，并在所属 Zephyr/nrftest/AutoPTS 仓库验证后提交。本轮完成**确定的策略解耦、Zephyr工作区对齐、nrftest三个自洽提交，以及从已提交builder源码重放A/B两遍构建**。AutoPTS没有实现缺口，未为凑提交而新增第二套codec。

**没有push、创建新分支、安装工具、刷机/reset、串口或RF执行。Linux/BabbleSim仍未运行；本报告不继承旧RF pass，不恢复 `IOS-RADIO-GATE`。** 这是新的expected-build candidate，不是installed proof。首轮 `82f7b2b0…` 的[报告和产物](2026-10-18-nrf-fault-firmware-v2.md)保持原始范围，不被新hash回写。

## 1. 分层修正

职责见[固件架构](../../research/peripheral/nrf52840-fault-firmware-v2.md)。这次不是预建任意fault graph/VM，而是移除已经发现的两项策略耦合：

| 原耦合 | 当前行为 |
|---|---|
| ARM_CCC只允许0→1/2或1/2→0 | 固件和Python codec仅要求精确u16/66-byte布局、真实managed target和before-state；允许no-op、模式切换或任意exact请求字节，COMMIT仍走正常GATT路径。enable/disable专项前提由host负责 |
| fault-enabled固件必须固定11个capability且Kconfig自动填本产品family | 通用encoder只验证count/name/duplicate/UTF-8/payload预算；family由显式配置声明，上层严格准入。11项样例移到build-probe config |
| 通用builder要求BleHub A/B及五角色 | `firmware_image.py`只负责受限声明/配置和构建安全；BleHub专用identity/profile/admission集中在 `tools/blehub_fixture_policy.py`，export/provision显式调用 |

仍保留单slot、20-byte payload、fixed bearer、MAX_CONN=1、EATT关闭、reservation、最大deadline、旧ticket隔离及quarantine。这些是机制安全，不是可被host取消的测试策略。正式fault候选强制regression-off，不开放任意Kconfig透传。REJECT仍固定 `0x0e`；不宣称已支持EATT、多slot、任意opcode或任意错误注入。

Python机制测试覆盖 `{0,1,2,3,0xffff}` 两两组合的CCC request/receipt roundtrip，保留错误长度、READ携带requested、未知operation/opcode及ownership/deadline拒绝断言。具体场景没有放宽为“任意转换都算enable/disable测试通过”。

## 2. 所属仓库与提交

### Zephyr

当前工作区 `zephyr/main` 与配置使用的build clone均为clean：

```text
433a6f37340990f96f597ed2a542224ff1460c9c
bluetooth: tester: separate fault mechanisms from fixture policy
```

其父提交为首轮实现 `82f7b2b0e63960f18fed3fff3ab376b0ef443cb9`。新提交包含12个精确文件；没有重写旧提交、强推或添加人类DCO sign-off。

工作区原先只有两份v1修改及untracked read-hold harness。经范围核对，先保存原字节与patch到ignored `zephyr/build/blehub-v2-forward-backup/`，再将已被v2替代的v1修改移出活跃路径；通过本地Git object fetch和fast-forward接入同一提交，不复制 `.git`。原harness备份保留，工作区没有残留v1 live compatibility。

启用/关闭Tester、真实peer回归与六种encoder配置均通过ARM编译链接；321-byte文本负向构建按预期触发320-byte上限。Checkpatch为0 errors/0 warnings。**这些不是运行断言通过**：Twister发现前缺少 `natsort`，未安装；逐配置使用West编译。BabbleSim仍被macOS的Linux-only POSIX限制阻止。

### nrftest

三个有序本地commit如下，锁定Zephyr `433a6f37…` 与AutoPTS `955f0140…`：

| Commit | 责任 | 精选提交树验证 |
|---|---|---|
| `5e1170e506e98fdaafbc55c774117fbea3fadbec` | DK build、通用声明/renderer、source audit、具名BleHub admission、contract export | 546 passed、1 skipped；lint/format通过 |
| `a98c283a52504d34b042bfd155c030e50f653909` | immutable HEX snapshot、共享lease、显式provision及installed-image readback | 705 passed、1 skipped；lint/format通过 |
| `b5be860af50075ff017a647d01e83ba3df5c28fb` | 文档与Just入口 | 705 passed、1 skipped；lint/format/Markdown/recipe解析通过 |

每步先stage精确路径/hunks，`git write-tree`固定候选，导出到ignored目录，再用导出树自身Just recipes和现有Pixi工具运行测试；提交后的tree与受测tree相同。C3 tree为 `491deafc17e2228504a055f00d1c80bd0df15437`，受测source archive SHA-256为 `97359cdef0378d2975041c0ef11368fbaa0a7a0520c17896336e12da826672a8`。

完整工作区另运行 `pixi run --as-is --offline just test`：707 passed、1 skipped、4 subtests passed；lint通过。**707不冒充精选提交树结果**，其中含两个未提交的可选J-Link setup tests。原来的9项dirty状态完整保留，包括README/Just的J-Link相关hunks、local example、diagnostic probes、PDF及可选工具；没有stash/reset/clean或批量提交。暂存区为空。

### AutoPTS

工作区与实际build clone均使用既有commit `955f014070c21a3fe8a04a55acc4006917b1d509`。当前已有BTP framing/transport与read-only attestation，没有v2调用方要求新增fault wrapper。故**没有新AutoPTS提交**，也不复制第二套Python字段codec。未来有真实共享typed API需求时，应迁移唯一纯codec，而不是把包含场景/证据解释的整个host controller搬下去。

本轮执行：

```sh
PYTHONPATH=/Users/vision/projects/contrib/auto-pts PYTHONDONTWRITEBYTECODE=1 pixi run --as-is --offline just --command python -m pytest -q -p no:cacheprovider /Users/vision/projects/contrib/auto-pts/test/test_blehub_attestation.py
```

结果3 passed。上述是本机执行记录，不是通用路径默认。未运行会改写模拟报告目录的全部AutoPTS unittest。工作区原有untracked mock report保持原样，不作为新代码或硬件证据。

## 3. 从已提交builder重放A/B

实际builder从C3 source export运行，路径为nrftest ignored `.work/v2-commit-review-433a6f37/c3`；没有import包含无关dirty修改的工作区builder。用导出树自身Just recipes串行完成A/B两遍构建、export及check，每步退出0。新输出单独位于 `.work/build-v2-433a6f37/`，不覆盖旧82候选产物。

| 身份 | A | B |
|---|---|---|
| Normalized image SHA-256 | `8b5299d3cad30ae50b5caf813bd90fc2a5e6355430ac1eec604c8078851dd23b` | `ba23244261a08b91a5ea5e531cb2e31232112da80b6bd629eb689f0632e9ae16` |
| Raw HEX SHA-256 | `863edf57df2b6973a6429ddff6fbda648a6343b90b4e677892f0604d5e91e799` | `c72aafb0988f03e500043255ec02f946ad6ddb815aa9228d0bfdd8b332b0827a` |

Current expected contracts已更新：

- [A](../../../tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json)
- [B](../../../tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json)

与旧contract差异仅为 `image_sha256`；原字节保存于nrftest ignored `.work/build-v2-433a6f37/history/expected-82/`。导出前没有伪造hash、借用旧digest或把export当安装证明。

构建前后Zephyr/AutoPTS source、69个West projects均clean且匹配pin，`west.yml`未变。验证后只刷新ignored ready marker，无West update/网络。两份compile commands与Ninja deps证明消费锁定commit的 `att.c`、`att_fault.h/.inc`，与Git blob、archive和inventory一致。最终fault/attestation开启、MAX_CONN=1、EATT/regression关闭；16 slots、11 capabilities、370-byte payload。

独立receipt位于nrftest `.work/build-v2-433a6f37/evidence/receipt.json`，记录nrftest commit/tree/source export、外部pins、声明/profile/config/overlay hash、命令/时间/退出状态、manifest/ELF/HEX/BIN/contracts和实际编译依赖证明。每个镜像只执行一次两遍构建，未做额外独立重建，不宣称bit-for-bit reproducibility；source audit不是完整工具链依赖闭包。

## 4. BleHub验证与剩余门禁

本轮 `test_fault_control` 21项通过；`pixi run just react-native tools-fixture-test` 最终821项通过，`host-check`及A/B `fault-inputs-check`通过。普通run仍不flash/reset，未知family不得因为“通用构建成功”取得RF授权。BleHub原有大量交叉未提交改动未被批量提交，本轮机制codec、current contracts和文档改动仍在该工作区。

下一步仍需Linux真实ATT/BabbleSim与encoder runtime断言、前轮超时的完整iOS App构建、显式provision后的live receipt、两端正常RF与write/CCC专项。没有因通用化跳过native/package/ABI/lifecycle/长稳或API档位要求。`IOS-RADIO-GATE`保持deferred。
