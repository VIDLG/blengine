# nRF Tester local getter 类型修复与 A/B 候选构建

日期：2026-10-19。范围：修复 CCC enable REJECT 过程中发现的本地 attribute getter 类型混淆，为后续 CCC disable REJECT 的 enabled-state readback 做准备。**本报告初始快照为代码与双遍构建完成，尚未安装或执行新候选 RF。** 随后用户另行明确授权安装 DK-B，安装、独立回读、已有 RF 回归及 CCC disable REJECT 的 Android pass / iPhone gap 见[后续报告](2026-10-19-rn-ccc-disable-reject.md)；下文 build-only 结果不追溯升级，DK-A 未更新。

## 根因与修改

Zephyr `tests/bluetooth/tester/src/btp_gatt.c::get_attr_val_rp()` 在成功调用 attribute reader 后，只凭属性位于 Tester `server_db` 数组，就将其 userdata 当作 `struct gatt_value` 读取 `enc_key_size`。

但 service、characteristic declaration、CEP 和 managed CCC 的 userdata 使用其他结构，可能因此伪造 encryption-key-size 错误。前次 [CCC enable REJECT 报告](2026-10-19-rn-ccc-enable-reject.md) 中的拓扑查询失败保留原事实；该次没有记录具体 ATT 错误码，不由本修复补写为某个实测错误。

最小修复：同时要求 **Tester ownership + `attr->read == read_value`**，才进入普通 value 的 key-size 检查。`alloc_characteristic()` 和普通 `alloc_descriptor()` 使用这个 reader 与 `gatt_value`；声明、CCC、CEP 等使用专属 reader。没有用 permissions 猜测 userdata 类型，保留普通 characteristic/descriptor 的原 minimum-key-size 行为、已有 ATT 错误优先级和无连接时的诊断语义。

生产改变仅两行 guard/comment。回归位于 Zephyr `tests/bluetooth/tester/gatt_value/`，包括 canonical `tests.yaml` 与 standalone Mac runner。没有修改 fault protocol、公共 BLE API、native watchdog 或普通 run 的刷机权限。

## 本地提交

沿用用户先前对 nRF 固件前向实现与本地 commit 的授权，仅提交本项文件：

| 仓库 | 提交 | 范围 |
|---|---|---|
| Zephyr | `b59a7aa1191cc7b110e0bde74387b70d71c24659` | getter guard + owning regression |
| nrftest | `7b441459cf50e261fe9ef8d33061a917a8508589` | `upstream.lock.toml` 精确锁定上述 Zephyr 修复 |

**未 push。** Zephyr 原有四份 attestation 工作区改动和 nrftest 的其他 staged/unstaged/untracked 工作均未纳入提交、未 reset/stash/clean。没有修改 Git 全局身份配置，也没有代用户添加 Signed-off-by。

## 实际 getter 回归

回归编译完整 production `btp_gatt.c` 并调用实际 getter；所需 GATT read helpers 从 production `subsys/bluetooth/host/gatt.c` 原样提取，使用真实 public GATT/CCC types、buffer/address helpers，不复制 getter/type predicate 或用源码正则代替行为断言。连接 key-size/peer identity、value-handle lookup 是明确的窄 stub；不启动 kernel/controller/radio。

覆盖 service/characteristic declaration、CEP、指定 peer 的 CCC `0000/0100/0200`、无匹配 peer/无连接、继承权限、普通 value 的 key sizes 0/7/16 与 connection sizes 0–16、foreign/static 属性、数组两端、错误映射/数据长度及 response canary。

| 执行 | 实际结果 |
|---|---|
| 同 checkout 保存的旧 getter | **453 response checks，9 failures，exit 1** |
| 修复后的 getter | **453 response checks，0 failures，exit 0** |
| 修复后的 ASan + UBSan | **453 response checks，0 failures，exit 0**，完整日志无 sanitizer diagnostics |

这些是 **453 次响应检查，不是 453 个独立注册测试**。主要命令从 nrftest 的已有 Pixi 环境执行：

```sh
pixi run python /Users/vision/projects/contrib/zephyr/tests/bluetooth/tester/gatt_value/run.py --build-dir /Users/vision/projects/blehub/.work/tester-getter-fix/gatt-unit
```

旧码重现加 `--tester-source /Users/vision/projects/blehub/.work/tester-getter-fix/btp_gatt_before.c`；sanitized 运行加 `--sanitize`。构建目录先通过 owning CMake/unit_testing 配置取得实际 headers/config/compile flags，详见该测试 README 和 ignored logs。

其他检查：

- scoped checkpatch（含新文件）：0 errors / 0 warnings；不是全 Zephyr compliance pass。
- scoped Ruff format/check、`git diff --check`、UTF-8/whitespace/conflict 检查：通过。
- nrftest 定向 pin/build/contract 测试：**299 passed / 1 skipped**。
- `pixi run just test`：**747 passed / 1 skipped / 4 subtests passed**；`pixi run just lint` 通过。
- 独立只读源码复核未发现 type guard 丢失普通 value key-size 路径或回归复制生产逻辑的阻塞问题。

**限制：** 标准 Ztest 在 macOS 的 Mach-O compiler 上因 ELF section attributes 失败，未声称 Linux Ztest/Twister/native_sim 通过，也未安装 Linux。CMake-style checker 缺 `tree_sitter_cmake`，未隐式安装；host 编译仍有既有 `btp_gatt.c:928` `%u`/`size_t` warning，未顺手改无关代码。included-service reader 只作构造路径审阅，未独立动态覆盖。UBSan runner 未显式禁用 recover，因此检查了完整诊断而非只依赖 exit 0。

## Clean-source 双遍候选构建

使用 nrftest 原有 build/export/check recipes，未新增 builder 或 dirty allowlist。保留原 configured clean Zephyr `d9096bd...` 与 `.nrftest-sources-ready`，在已安装 west workspace 之内创建新 **detached clean worktrees**：

- nrftest：`/Users/vision/projects/nrftest/.work/gatt-getter-candidate/source`
- Zephyr：`/Users/vision/projects/nrftest/.work/upstream-attested/zephyrproject/gatt-getter-candidate/zephyr`

未创建 branch、复制 `.git`、联网 clone/update 或安装工具。通过祖先 `.west` 发现既有 workspace，显式 ZEPHYR_BASE 选择新 source，west.yml 与原 manifest byte/blob 一致；原 owner 验证 **69 个 pinned clean west projects**、AutoPTS 与 SDK 1.0.1 后，才创建 candidate-only completion marker。

通过 parent Pixi offline/frozen/no-install 环境，指定 candidate Justfile/working directory，执行：

1. `verify-upstream`、`verify-host-tools`：exit 0。
2. A：`firmware-build-dk-image` 两次 pristine west pass，随后 export/check：全部 exit 0。
3. B：同上两次 pass、export/check：全部 exit 0。
4. 构建后再次 `verify-upstream`、module cleanliness 和 before/after preservation 比较：通过。

每个 image recipe 自带双遍，故本次是 **四次实际 west builds**。单 recipe 300s、verify 180s、export/check 120s、outer 1,200,000ms；未超时或重试。构建保留 logging/RTT 与 EATT 相关 Kconfig dependency warnings，artifact/config/digest 验证通过，不把 warnings 当作未发生。

| 产物 | A | B |
|---|---|---|
| image ID | `nrf-multifunction-rf-v2-a` | `nrf-multifunction-rf-v2-b` |
| normalized image SHA-256 | `41323a56117fd2136f432908204ce475e3095f8e0faee5ae18d4a09bcfb509ca` | `834d5a0a62c8d0c4b4fb5b06fe86937c9240eeaf933a0dcdb40f04edbb5e1561` |
| raw HEX SHA-256 | `fce099f4d197198912c7e96987fc40c7fa9e4c69002748b65184bb663539d981` | `5143964d1db6acafe80dab888c2fa68d9eb2ba19f53cfaeb5e6e58657c806b35` |
| manifest SHA-256 | `7b8f780caf785022433a513a68b1b7d1cf98d64582b2a261b2c6c35d1c2b6439` | `0ddd4020cdcfd49eace0aa991872912bde9ab37c678ea27f9e2e8e39683cddc3` |
| ELF / HEX / BIN size | `5,374,532 / 994,196 / 353,424 bytes` | 相同 |
| reported FLASH / RAM | `353,424 / 100,512 bytes` | 相同 |

两个 manifest 的 schema-2 clean input audits 一致：369 files、空 local patch，inventory digest `5da07259f28d1ebd58d973cfbccb8907432035faa59947f3e31555e13a72ff61`；source archive digest `71caca6d463d44c230fe6f604f2be564f8d977261e3dc9fa3f2f287c3521a617`。compile-command 记录指向新 worktree 的 tester/ATT 源，而非旧 configured checkout。

所有新产物仅在 ignored `.work/gatt-getter-candidate/`：`images/<image-id>/nrftest-build-manifest.json`、`expected-contracts/multifunction-rf-v2-{a,b}.json`、`evidence/{summary,handoff}.json` 与完整 build/export/check logs。tracked canonical contracts、profiles、image declarations 均未覆盖；这是 **expected build identity，不是 installed-image proof**。

Preservation summary digest `fad57807ec6963c49e4d19819937f8b446b5fcbb8d87350b90409a78c08784b6` 前后相同，覆盖两个 dirty workspaces 的 HEAD/index/diff/local files、branch refs、原四份 attestation 文件、canonical inputs/local config、原 clean Zephyr 与 marker；新提交是在此前完成的明确变化，不混入此 before/after 构建比较。

### 本地配置边界

nrftest tracked pin 已指向新 commit，但 ignored TOML 按要求未修改，仍选择旧 clean checkout。**直接用旧默认路径执行 verify/build 会因 revision mismatch 被拒绝**，不是重试 setup 或联网更新的理由。本候选构建显式覆盖 `NRFTEST_ZEPHYR_ROOT` / `ZEPHYR_BASE` 为上述新 worktree，`NRFTEST_BUILD_DIR` 为新产物目录；SDK/AutoPTS 沿用已验证本地路径。完整 literal env/argv 在 execution receipt 中。

BleHub 原 installed-candidate contract/profile 的无设备 preflight 仍成功，service `fd700000-0000-4000-8000-000000000000`；未把新 pin 当作设备已升级。

## BleHub 最终软件/文档复核

当前 pin、报告与排队状态更新后：`pixi run just react-native tools-fixture-test` **927 passed / 147.553s**；`pixi run just fmt`、`pixi run just lint` 通过；`docs-check-mermaid` 检查 **153 Markdown files / 27 diagrams** 通过。六份修改 Markdown 的 UTF-8、balanced fences、trailing whitespace 与 conflict-marker 检查通过。未修改 RN/native App 源码，未重跑手机或 simulator tests，也不由软件回归推断新 image 已安装。

## 下一步与 radio 排队

1. **待明确授权后**仅对 DK-B（debugger `1050251479`，USB serial `001050251479`）安装新 B 候选；精确 raw HEX digest 是上表 `5143964d...`。这是普通 provision，不授权 unsecure/mass erase/DK-A 操作或 destructive retry。
2. 在同一物理 lease 内 programming/readback/installed-image assertion 成功后，才能按明确新 contract 执行 RF；getter 的真实 per-peer enabled readback、基础/已有拒绝专项回归与 CCC disable REJECT 仍待验证。不能复用旧 image 的 RF pass。
3. `IOS-RADIO-GATE` 已由用户明确重开，排在当前 firmware/CCC 工作后，执行顺序已同步 Plan、AGENTS 与当前导航。此前 opt-in code/失败证据、no-password/no-terminal-input 和独立 RF/cleanup gate 保留；当前未恢复 Settings/XCUITest/Continuity 诊断或运行 iOS radio mutation。

本报告不更新原报告 verdict，不宣告 native/runtime/package/RF 同候选退出，也不豁免尚未运行的 Linux/firmware 调度资源断言。
