# 2026-10-18 DK-B v2 provision 尝试：J-Link target-init 阻塞

## 结论

用户在明确询问“仅将DK-B从v1刷为已构建v2-B并readback，DK-A不动”后同意继续。本轮按该目标执行了一次显式provision：**在J-Link连接nRF52840目标阶段失败，未进入项目driver的镜像写入调用，也未取得v2 installed-image proof。没有自动重刷、recover、unsecure或全片擦除。**

失败后通过独立physical lease的只读BTP采集，DK-B仍返回原v1-B身份。该结果说明当时应用仍可回读原身份，不是完整Flash字节未改变或连接绝对无halt/reset的证明。DK-A未操作。

当前Android Release APK已成功构建并通过single-image检查，但没有安装/启动、没有新RF结果。真实手机Central+DK主线继续，不扩展Mac Central辅线、不配置Linux，不恢复 `IOS-RADIO-GATE`。

## 1. 明确目标和构建输入

本轮通过进程级环境覆盖设备选择，没有改nrftest默认A设备配置：

```text
NRFTEST_DEVICE_SERIAL=001050251479
NRFTEST_DEBUGGER_SERIAL=1050251479
NRFTEST_DEVICE_PORT=/dev/cu.usbmodem0010502514791
NRFTEST_REPORTS_DIR=/Users/vision/projects/nrftest/.work/provision-v2-b-2026-10-18
```

Resolver preflight确认USB/J-Link serial规范化后同属 `jlink:1050251479`，SEGGER library取自现有本机配置，解析为V9.80文件。执行前provider status为stopped；一次新的read-only BTP capture确认旧v1-B仍存在。

固件候选和build proof沿用[提交树重放报告](2026-10-18-nrf-fault-mechanism-policy.md)：

- Zephyr：`433a6f37340990f96f597ed2a542224ff1460c9c`。
- Manifest：nrftest `.work/build-v2-433a6f37/images/nrf-multifunction-rf-v2-b/nrftest-build-manifest.json`。
- Expected contract：[v2-B](../../../tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json)。
- Normalized image digest：`ba23244261a08b91a5ea5e531cb2e31232112da80b6bd629eb689f0632e9ae16`。
- 显式确认的raw HEX SHA-256：`c72aafb0988f03e500043255ec02f946ad6ddb815aa9228d0bfdd8b332b0827a`。

`pixi run --as-is --offline just firmware-check-contract` 用真实manifest/declaration/profile/contract参数执行并通过；输出仍明确 `installed-proof=none`。随后调用现有 `firmware-provision-dk` recipe，由同一physical lease覆盖私有manifest/HEX快照、programming和计划中的post-flash readback。

## 2. 实际失败边界

唯一一次provision调用退出2。关键原始输出：

```text
J-Link script file function InitTarget() returned with error code -1
unable to connect J-Link 1050251479 to NRF52840_XXAA:
JLinkException: Unspecified error.
```

原flash report记录 `outcome=environment-failure`、`flashed_bytes=null`、目标serial正确。异常发生于 `PyLinkTargetResetDriver.open()` 内的target connect，因此项目没有进入后续 `programmer.flash_hex()` / `link.flash_file()`。

日志中两行 `InitTarget` 不等于主机执行了两次provision。已安装PyLink 2.0.1的 `connect()` 会先发 `Device = NRF52840_XXAA`（源码注释说明可能auto-connect），随后设置speed，再在未连接时调用native Connect；两行与这一实现相容，但现有日志不能精确归属每行的API来源。

**不能根据当前错误断定供电、SWD速度、debug保护或SDK不支持是根因。**

静态核验所得边界：

- target名称和当前nRF52840 DK board没有明显错配；V9.80包含该器件支持。
- West runner配置中的4000 kHz并不自动作用于直接PyLink路径；这只是配置差异，不是速度错误的证明。
- 当前已安装PyLink默认unsecure hook返回NO，且V9.80存在相应hook symbol；没有把它改为YES。该事实不证明本次确实遇到了APPROTECT，也不把target-init标成无副作用。
- `Device`选择/native connect本身可能改变debug状态、halt或reset；不能把“没有调用flash”笼统称为完全只读。
- 待刷v2镜像的 `CONFIG_NRF_APPROTECT_USE_UICR=y` 不等于强制关闭保护，也不能倒推当前已安装v1的UICR状态；没有基于猜测改固件或解锁芯片。

## 3. 失败后状态

失败后仅执行独立read-only attestation和provider status，没有新的J-Link target连接尝试。DK-B仍返回：

```text
image_id: nrf-multifunction-rf-v1-b
normalized digest: 9937f35d591fcb9bff86492b4b82841c4050fdb5901b460974635ba64a367f2b
```

Provider仍为stopped。没有v2 installed-attestation成功文件，未据本地expected contract放行App执行。DK-A未reset/flash/probe，未切换目标绕过失败。

完整原始证据保存在ignored目录，未回写旧失败JSON：

- nrftest `.work/provision-v2-b-2026-10-18/provision.log`、`contract-check.log`。
- flash report：`firmware-flash-dk/20261018T101635.498940Z-8e80d638a3d74fb9b90cd572060c0659.json`。
- provision report：`firmware-provision-dk/20261018T101635.515040Z-dd77c3a889f94835a7cf02796716aa3e.json`。
- BleHub `.work/rf-v2-b-2026-10-18/dk-b-before*`、`dk-b-after-failed-provision*`。

## 4. 修复确定的证据报告错误

原flash失败JSON中的 `proof_boundary` 无条件写着“J-Link accepted and reset the validated image”，与target-connect失败相矛盾。上层provision没有因此通过，但该文字会误导证据解释。

已在nrftest提交：

```text
65f056b4148dd7f53d7b6fd4fd9bc2d5639172cb
Bound DK flash reports to observed outcomes
```

仅两个文件：`tools/flash_dk_firmware.py`、`tests/unit/test_dk_firmware_snapshot.py`。

- success只声明编程API与reset返回，明确不是installed-image/BTP/RF proof。
- failure不再声称编程或reset成功，也不因null字节数声称芯片未变化。
- 轻量phase区分open、snapshot_verify、flash_and_reset、close、complete。
- 主错误保留原detail/因果链，close错误独立为 `cleanup_detail`；单独close失败也不能输出成功。
- 回归覆盖连接失败不调用flash、flash/reset失败、并存的primary/cleanup错误、成功proof边界，同时保留原snapshot/lease/hash断言。

验证：定向50项通过；完整当前工作区713 passed、1 skipped、4 subtests passed。提交前从精选index tree导出源码，执行其自身 `just test lint`：**711 passed、1 skipped**，lint/format通过；受测tree与最终commit tree同为 `02d3e84d02c62894adf02e3d1fd22d55865d3e8f`。没有把713冒认为精选提交树结果。

本地提交未push，9项原有无关dirty状态和原字节保留，index为空；没有改J-Link driver参数、速度、unsecure hook、固件pins/contracts，也没有用新报告代码自动重试硬件。

## 5. Android候选准备，不执行RF

复用现有入口，串行执行：

```text
pixi run just react-native host-android-config-check
pixi run just react-native host-check
pixi run just --command python tools/react_native/run_quiet.py <ignored-log> -- pixi run -e android tests/consumers/react-native-internal/android/gradlew -p tests/consumers/react-native-internal/android :app:assembleRelease --no-daemon
pixi run just --command python tools/react_native/check_single_image.py android tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk
```

上述配置检查、typecheck、Release构建和single-image gate均通过。Gradle为BUILD SUCCESSFUL，single-image检查为2个UniFFI ABI slices、2个Nitro slices；不把它理解为两份Rust native image。

APK：`tests/consumers/react-native-internal/android/app/build/outputs/apk/release/app-release.apk`，38,162,882 bytes，SHA-256：

```text
361ee8b9511cc84401d9b071956858922abe5bca983d79ffbfd93e10d28c2e23
```

Android `3ac5cc09` 的ADB状态为device，但本轮未安装/启动该APK，未grant/revoke权限、切换radio或跑full-suite。相关日志和 `android-artifact.json` 位于BleHub ignored `.work/rf-v2-b-2026-10-18/`。iOS前轮完整构建超时仍未解决，本轮没有重试。

## 6. 下一步与安全限制

阻塞在target初始化，不在RN页面或Linux环境。下一步应先取得能分辨VTref、SWD/AP/CTRL-AP及native日志的连接证据，或核对实际PCB revision、供电与SWD路由；不能靠反复provision或把unsecure默认改为YES试错。

若要新增一次带完整日志的target连接诊断，需明确它不调用flash/unsecure/recover但仍可能halt/reset；不得标成无扰动只读。板卡开关/外部target接线需要核对时先目视记录，不自动拨动、断电或改接。硬件问题解决后，重新取得明确provision授权/执行窗口，再完成installed proof与Android→iPhone独立RF。

所有失败attempt保留；没有新RF/安装通过，也没有放弃firmware生命周期/资源安全、native/package或Phase退出要求。
