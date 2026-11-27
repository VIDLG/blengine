# 2026-10-18 nRF runtime 验证：Linux host 前置条件

## 结论

**维护目标仍是一套多功能固件，不新增按场景切换的固件。** A/B 是独立身份/profile，普通RF与READ/response-write/CCC故障机制共用同一代码。当前已提交生产候选保持Zephyr `433a6f37340990f96f597ed2a542224ff1460c9c`，其[机制/策略分离和真实构建证据](2026-10-18-nrf-fault-mechanism-policy.md)不变。

本轮继续检查运行条件，但**没有运行真实ATT/BabbleSim或C encoder assertions**。本机未检测到Docker、Podman、Colima、Lima、OrbStack、Multipass或QEMU可执行入口；没有可从当前配置确认的Linux测试执行环境。没有自动安装工具、下载VM、重新启用历史Linux BLE fixture、连接猜测的SSH主机，或将Mac编译当Linux运行通过。

## 本地官方证据与实测阻碍

Zephyr本地 `doc/develop/test/ztest.rst` 的unit-testing说明明确仅支持Linux；Windows/macOS需Linux guest。`unit_testing`不运行Zephyr内核，不能代替 `native_sim` 或BabbleSim调度与ATT生命周期测试。

在canonical `zephyr/tests/bluetooth/tester/attestation` 尝试复用标准unittest目标，直接编译 `ZEPHYR_BASE` 所属checkout的真实 `btp_blehub_attestation.c` 和已有 `src/main.c`。CMake配置通过，两次有界compile尝试均被Mach-O section语义阻止：

```text
error: argument to 'section' attribute is not valid for this target:
mach-o section specifier requires a segment and section separated by a comma
```

真实encoder的 `.rodata.blehub_attestation` 与Ztest iterable sections均涉及该限制。没有修改生产section、linker、Ztest framework或加内核shim绕过；没有执行 `test_capabilities()`、encoder init或handler。确认共享host阻碍后，不对另外五个variant重复无意义尝试。

Twister仍缺 `natsort`，CMakeStyle工具缺 `tree_sitter` / `tree_sitter_cmake`；没有安装。官方schema/parser检查通过，但不是完整Twister CLI或CMakeStyle通过。

## 保留的测试入口草稿

仅工作区Zephyr的以下四个test文件发生变化，**未stage/commit**，等待受支持Linux环境实际验证：

- `tests/bluetooth/tester/attestation/CMakeLists.txt`：复用标准 `unittest/testbinary` 目标，原integration仍用app目标。
- `tests/bluetooth/tester/attestation/tests.yaml`：共享六个variant overrides，增加unit scenarios；未永久改为build-only。
- `tests/bluetooth/tester/attestation/unit.conf`：encoder-only配置，fault两项关闭，不链接Bluetooth stack或kernel。
- `tests/bluetooth/tester/attestation/README.rst`：界定encoder-only、fault-enabled integration、ARM编译及host支持范围。

没有复制encoder或实现平行状态模型。Unit profile即使未来通过，也只证明编码行为，不证明fault-enabled integration、ATT、radio或RF。

Generic/single/32-byte boundary/empty/duplicate/33-byte oversize六个原fault-enabled ARM配置，本轮均使用现有SDK 1.0.1逐配置West构建，**compile/link通过、未运行**。原integration metadata与已提交HEAD一致，generic capability仍是两个任意名称。Checkpatch为0 errors/0 warnings，diff whitespace和定向文件检查通过。

本轮构建的test root位于canonical工作区，`ZEPHYR_BASE`明确指向nrftest配置的clean source-owner checkout；两处生产encoder字节与当前commit相同。完整命令、flags、错误与逐variant结果保存在ignored `zephyr/tests/bluetooth/tester/attestation/build-host-investigation/`。

## 不变的生产候选与下一步

配置使用的Zephyr build clone保持clean，生产源码、lock、镜像和A/B contract未修改；normalized digest仍为：

- A：`8b5299d3cad30ae50b5caf813bd90fc2a5e6355430ac1eec604c8078851dd23b`
- B：`ba23244261a08b91a5ea5e531cb2e31232112da80b6bd629eb689f0632e9ae16`

未push、刷机/reset、访问串口、执行installed-image attestation或新RF，也没有重试前轮超时的iOS完整构建。`IOS-RADIO-GATE`继续deferred。

下一步需要明确可用Linux runner，或显式授权配置Linux测试环境；随后验证unit草稿和真实ATT/BabbleSim，再决定提交测试改动。不能把这道环境前置条件改写为通过或默默豁免。
