# 2026-10-18 真实 Central RF 主线：设备预检与 native Apple 安全边界

## 执行决定

用户确认使用**Mac控制机 + 真实Android/iPhone Central + 真实nRF52840 DK Peripheral**，继续一套多功能固件，不再把配置Linux/BabbleSim作为启动真机RF的前置条件。macOS原生Swift/CoreBluetooth Central保留为独立Apple backend/HIL开发与回归辅线；不复活归档的RN macOS或Tauri App，不借Mac结果证明手机RN/iOS通过。

该调整改变执行顺序，不删除未证明的调度、非法协议、资源失败和生命周期断言，也不豁免Plan退出条件。`IOS-RADIO-GATE`仍暂缓。镜像需要更换时须单独授权provision；普通run只在同physical lease的新live receipt通过后执行，不自动flash/reset或修改权限/radio。

本轮实际进行了设备枚举、两块DK的只读身份采集、现有native Apple RF入口审查和Mac入口安全修复；native iOS扩围修复已从活跃工作树撤回并保留为待审阅candidate。**没有provision、App安装/启动、BLE RF场景或native签名执行，也没有新RF pass。**

## 1. 只读物理预检

### 手机

- Android：通过配置SDK的ADB执行 `devices -l`，设备 `3ac5cc09` 为 `device`，model `PHB110`。
- iOS：`pixi run -e apple-ci xcrun devicectl list devices --timeout 15` 显示物理iPhone 14 Plus `00008110-000C61160C39401E` 为available/paired。其他simulated或unavailable条目未选为DUT。

这些只证明主机可见/ADB状态或CoreDevice连接状态，不证明iPhone已解锁、App签名/安装就绪、BLE授权或RF通过。首次Android枚举命令的模块import路径错误已改为现有 `tools.android_toolchain`；该工具查询错误不记作设备或BLE失败。

### DK

通过BleHub现有read-only attestation入口，在各自physical lease内尝试：

```text
pixi run just peripheral nrf52840-attestation 001050252028 .work/real-central-preflight-2026-10-18/dk-a-installed.json
pixi run just peripheral nrf52840-attestation 001050251479 .work/real-central-preflight-2026-10-18/dk-b-installed.json
```

| DK | 实际结果 | 可作出的结论 |
|---|---|---|
| A，USB serial `001050252028` | capture失败；USB身份下有两个VCOM，BTP detection在两者均未验证到有效响应 | installed identity未知；不能假定是v1、v2、固件损坏或未上电。没有reset或重刷来掩盖失败 |
| B，USB serial `001050251479` | capture成功，保存attestation及provenance sidecar | 当前回读为v1，明确不匹配当前v2 contract，不能进入v2 gate |

两块板均枚举为SEGGER J-Link，VID/PID为 `0x1366/0x1061`，每板有 `…1` 与 `…3` 两个CDC端点；端口存在不等于BTP ready。Provider status为stopped，没有停止或接管其他fixture。

DK-B读回：

```text
image_id: nrf-multifunction-rf-v1-b
image_version: maintained
normalized hash: 9937f35d591fcb9bff86492b4b82841c4050fdb5901b460974635ba64a367f2b
profile_id: blehub-nrf52840-multifunction-v1-b
service: fd400000-0000-4000-8000-000000000000
```

当前expected v2-B是 `nrf-multifunction-rf-v2-b`，normalized digest `ba23244261a08b91a5ea5e531cb2e31232112da80b6bd629eb689f0632e9ae16`，service `fd700000-0000-4000-8000-000000000000`。真实构建及raw HEX摘要见[候选报告](2026-10-18-nrf-fault-mechanism-policy.md)。本轮没有把旧安装身份改写成新contract，也没有借历史收据授权测试。

Ignored `blehub.local.toml` 的A/B测试profile选择仍为v1，现已只将两处profile路径更新到canonical v2输入；更新前原字节保存到本轮ignored evidence目录。**配置选择变更不是设备provision或安装证明**，物理serial/port未改。

## 2. Mac当Central的合理范围

当前已有未归档native链路：

```text
现有 Mac HIL probe
→ production BleHub / AppleBleBackend
→ CoreBluetooth
→ 真实 DK
```

它复用共享Swift Apple backend，可用于快速定位Apple backend问题，未来独立验证macOS RF。它不经过RN/Nitro/BLeng，不替代iPhone系统行为、Android Framework、RN生命周期或artifact-only backend consumer。

现有Swift probe源码包含scan/connect/GATT/IO/Notification/strict Indication/RSSI/passive-disconnect场景；这些只是代码能力，不是本轮pass。Mac与手机不能同时占用单连接DK或同一fixture owner；共享Apple package staging和provider状态也应串行隔离，不能因有两块DK就假定可无冲突并行。

## 3. Mac入口安全修复与native iOS待审阅风险

只读审查发现Mac及native iOS旧快捷入口仍：

1. 不带v2 immutable request启动provider；
2. 默认历史profile，而不是当前Multifunction Fault v2；
3. 使用全局 `nrf52840-stop` EXIT trap并吞掉cleanup错误，start失败也可能影响其他owner；
4. 部分路径删除共享coordination目录；
5. native marker与当前provider的subscription/passive-disconnect协议不闭合。

没有用假marker、借用已有广播或打开“传了request就算获准”的旁路。当前采取最小fail-closed修复：

- `just/apple/mod.just` 中受影响的Mac RF快捷入口去掉RF前置build/sign依赖、provider start/stop trap和共享目录清理，在外部动作前明确拒绝；Mac使用旧Android Peripheral的native旁路同样不能绕过。
- `tools/apple/test_macos_runtime.py` 在CLI和直接lower-half入口前检查未完成的v2 session桥接，保留场景实现及非RFbuild/runtime/authorize等入口。
- `tools/apple/test_rf_fixture.py` 保留原routing/readback/ledger断言，仅在纯软件测试内部mock Mac admission。
- 新增 `test_macos_rf_safety.py`，通过canonical `apple test-rf-host`发现，验证拒绝发生在构建、签名、设备和fixture动作前，且不清理他人owner/evidence。

Native iOS同源修复不直接并入本轮范围：已将其增量从工作树精确撤回，保存于ignored `.work/ios-native-rf-hardening/native-ios-candidate.patch`，原字节与候选测试同时保留。`test_ios_runtime.py`恢复原字节，新iOS安全测试未留在活跃discovery。**旧native iOS快捷入口仍有missing-request/全局cleanup风险，不是ready，不能执行**；后续单独审阅，未通过删除用户工作或恢复整个旧文件来回退。

**Native Mac RF当前明确blocked；native iOS旧入口尚未接好v2且仍不安全，二者均不是可执行的新gate。** RN手机主线使用另一套已有live-attested runner/provider，不因Mac入口加固而变成blocked。这个native v2会话桥缺口与暂缓的 `IOS-RADIO-GATE` 是不同事项；非RF入口本轮也未实际构建/签名/运行。

软件验证：Mac安全测试对旧实现的red run为6 tests/50个失败子项；最终Mac-only `pixi run just apple test-rf-host` 为11 tests/OK，Ruff、format、Just recipe解析/dry-run及diff whitespace通过。Primary admission error没有被无权执行的cleanup覆盖。Native iOS candidate曾通过包含它的18项组合软件检查，其red run为7 tests/42个失败子项，但这不是当前活跃工作树的计数或通过声明。完整日志与精确撤回核验位于ignored `.work/mac-rf-hardening/` 和 `.work/ios-native-rf-hardening/`。这些是mock/host软件证据，不是RF证据。

主lane另重跑 `pixi run just react-native tools-fixture-test`：821项通过；`pixi run just fmt`、`lint`、`docs-check-mermaid`、`git diff --check`通过，Mermaid检查148份Markdown中的27个图。五份本轮维护文档的UTF-8、fences、空白、冲突标记与本地链接检查通过。以上不包含设备端RF或前轮超时App build。

## 4. 接下来的顺序

1. **先选择一块明确授权更换镜像的DK。** B已能读回身份，适合先行v2-B provision与新installed-image readback；A的BTP检测失败保留，另行诊断，不自动复位。
2. 在新live receipt及物理lease成立后，先Android正常RF/full-suite，再iPhone独立candidate/session。前轮完整unsigned iOS App build超时仍未解决，不拿host-check代替。
3. 在同一workbench接入并执行write/CCC REJECT、watchdog、disconnect与恢复专项；当前新增write/CCC RF矩阵仍未完成，不能把已有read故障或恢复后写入当active-write覆盖。
4. Mac/native iOS辅线在原host owner内接通request/contract/profile、启动前/最终receipt/PID/PGID/lease校验、真实marker桥及identity-bound cleanup，再取得独立native RF结果；不新增App或通用runner。
5. 无法由手机/CoreBluetooth稳定制造的第二ATT Request、内部pool耗尽、缺失/晚到completion、UART饱和及精确deadline交错继续单列；未来受控目标回归或仿真需真实执行，不能由常规RF自动补齐。

本轮源码/文档改动未stage/commit，原工作区其他改动保留。没有Linux安装、push或设备破坏性操作；等待明确的DK provision授权后才改变installed image。
