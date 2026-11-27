# Appium 4 beta：设备 helper 升级被 Settings 签名变更阻塞

日期：2026-11-10。接续 [host-only 升级](2026-11-10-formal-android-appium4-beta-host.md)。
用户已授权在既有 OnePlus 测试机升级三份 helper，并以同 APK 做无 HANS 豁免的非 RF 对照。
本次实际尝试覆盖安装，但 Android 拒绝 Settings helper；没有创建 WebDriver session，
不能据此判断新版是否修复 [active-session stall](2026-11-10-formal-android-appium-root-cause-diagnosis.md)。

## 结论

- **已验证：** `io.appium.settings` 新旧 APK 签名证书不同，设备返回
  `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。普通覆盖升级不可行。
- **已验证：** UiA2 server 与 test APK 的新旧证书相同；本次在第一份 Settings 失败后停止，
  没有继续安装后两份，也没有留下新旧 helper 混装。
- **已验证：** 失败后的新租约只读复核确认三份 installed APK hash 仍为旧基线，
  无目标 App/helper 进程、无 active instrumentation、无 forwarding；phone cleanup 通过。
- **未执行：** helper 卸载、Appium 4 device session、HANS 白名单变更、BLE permission/radio、
  firmware 或 RF。原 active-session bug 仍 unresolved，本次不是该 bug 的新版复现。
- **下一步需额外授权：** 仅卸载重装 `io.appium.settings`。卸载会移除 helper 数据和其权限/
  AppOps 基线，不能作为普通安装失败后的隐式 fallback。不卸载被测 App，不使用签名绕过、
  重签名或第三方 cache 修改。

## 实际执行与失败保留

设备 serial/boot、完整命令回执和安装路径只留本地证据。共享 SDK ADB daemon 在前后核对中
均为 PID `16841`；没有 kill/restart、关闭未知 App、修改 radio 或固件。

### 1. 安装前的取证脚本失败

本地目录：`.work/appium4-helper-provision-2026-11-10/`。
run：`aea631d9-fd8c-4c98-be7a-101bb097cdca`。

一次性编排通过 `pixi run python` 调用现有 `AndroidRadioController`、SDK command boundary
和 parser，没有新增 maintained runner。最初 import 写错，在 acquire 之前即失败，无设备命令。
修正 import 后，脚本误将 AMS 全局进程清单限制为 512 KiB；实际约 1.37 MiB，因此安装前拒绝，
finalizer 同样拒绝并 quarantine。此为取证脚本错误，不是 Appium/设备安装故障。

随后独立只读恢复使用现有 `AndroidAutomationOwner` 的 `MAX_PROCESSES_OUTPUT_BYTES`
专用上限（4 MiB，仅完整 AMS processes dump），确认原 boot、空目标进程/instrumentation/
forward、HANS debug property 为空、原 host 已退出。核对原 owner 内容和锁 identity 后将已知
lease 归档为 `archived-lease`，原 failed result/phone journal 未改写；
`independent-recovery.json` 记录恢复通过。没有安装或其他设备 mutation。
正式 owner 的输出上限和 parser 均未修改。

### 2. 新租约下的单次安装

本地目录：`.work/appium4-helper-provision-2026-11-10-attempt2/`。
run：`eb698ffa-0fe2-47f8-aaf5-dedd12b602f2`。

先核对完整本地三件套及新版 reviewed SHA256，再 acquire fresh phone lease。检查共享 ADB、
原 boot、App/helper absence、active instrumentation、forward、reviewed ROM hash 和 debug
property。通过现有 `android_sdk.install_apk` → push/root package-manager/install/temporary-file
cleanup 边界，对第一份 Settings 使用显式 `use_root=True`、`grant=False`、绝对 deadline 和
逐命令 owner guard。

实际安装返回：

```text
Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE:
Existing package io.appium.settings signatures do not match newer version; ignoring!]
```

停止后续安装；不卸载、不自动重签、不 grant。安装器 temporary-file cleanup 和 phone
App-only finalizer 完成，`phone_cleanup_errors=[]`、`phone_status=passed`，租约释放。
此次 provisioning 的 `status=failed` 保留，不重标为成功。

### 3. 本地 APK 签名核对

使用已安装 SDK `build-tools/37.0.0/apksigner verify --print-certs <apk>` 检查两套 managed
home 的全部六份 APK。最初默认 Pixi 环境缺 Java runtime，六次检查均 exit 1，不算签名验证。
随后使用既有 `pixi run -e android python` 环境和其 `JAVA_HOME` 执行同一命令，六份全部 exit 0；
没有安装 Java/SDK、接受 license 或请求密码。

本地结果：`.work/appium4-helper-signature-audit-2026-11-10/result.json`。
下表是 signer certificate SHA256，不是 APK SHA256：

| Helper | 旧版证书 SHA256 | 新版证书 SHA256 |
| --- | --- | --- |
| Settings 8.4.5 → 8.5.2 | `6d1150ac03787964b71ff149f36ba730b578d90909dd26629d21f3d9a3772edb` | `a2e497953a77dc8f7ae37869d0d9962deafd1e958ca37ccc0a4b9452769f9fd1` |
| UiA2 server 10.7.6 → 10.7.10 | `a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc` | `a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc` |
| UiA2 test APK | `a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc` | `a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc` |

设备安装错误与本地新旧签名差异相互吻合；不将其推断为整个 Appium 4 无法使用，也不声称
所有版本升级都会发生此问题。

### 4. 安装失败后的独立只读复核

本地目录：`.work/appium4-helper-blocked-readback-2026-11-10/`。
run：`0bd29394-d2dd-4e3b-914d-ca12c0c43e7d`。

fresh phone lease 下，三份 installed path/hash 均与升级前旧基线相同：

| Package | Installed APK SHA256 |
| --- | --- |
| `io.appium.settings` | `0f8de773243d45d2ecd05613e4fcce526d674738b1979212cd2cf5e5fe83f871` |
| `io.appium.uiautomator2.server` | `b0dea678506e4a1d98bd003a3c8842ae2ff119e3d17f63c9d657c2d61d9f198d` |
| `io.appium.uiautomator2.server.test` | `f18ce4e6fc9ca7606875bca4f9739b2e1d3f0bf3544c432e071ff2d24f43ed31` |

原 boot、无 App/helper 进程、无 active instrumentation/forward 均通过；HANS debug property
和 Close log tag 为空，Settings mock-location AppOp 为 `deny`。只读复核 `status=passed`、
`phone_cleanup_errors=[]`、`phone_status=passed`，租约释放；这不是 helper 升级或 UI pass。

## 后续受限操作

取得额外授权后：

1. fresh lease 下重新核对三件套、原 installed identity/hash/UID、无活动 process/session/forward，
   记录 Settings 原有权限、AppOps 和 helper scope；不借旧报告直接准入。
2. 仅卸载旧 `io.appium.settings` 并安装 reviewed 新版，明确接受该 helper 数据和权限基线重建；
   不卸载被测 App，不删除其他 package，不绕过签名校验。
3. 覆盖升级同签名的 server/test APK，逐份 installed hash/UID/instrumentation readback。
   任何中途失败保留 partial provisioning 证据，不能运行混装 candidate。
4. 新完整基线下，复用正式 managed runner 和同 APK
   `c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584`，先做无 HANS 豁免
   start-hold 非 RF 对照；仅在独立恢复闭合后考虑新 run 的临时 HANS 对照。
5. helper 临时 settings 仍由原 owner 显式准备和恢复；不把卸载前旧 UID 或旧 AppOps 记录
   当作新安装的 authority。不改 BLE 权限、radio、firmware 或共享 ADB daemon。

## 后续授权与实际结果

用户随后明确授权仅卸载重装 Settings。新版三件套已在 fresh lease 下安装并核验；
同 APK 的无 HANS start-hold passed、无 HANS 完整 lifecycle 仍 stall 并已独立恢复，
临时 HANS 完整 lifecycle passed 且恢复 THIRD。详见[后续真机对照](2026-11-10-formal-android-appium4-device-comparison.md)。
本报告上述安装失败和旧三件套 readback 均保留原执行范围，不改写为成功。

## 文档验证

- `pixi run just fmt`：通过，352 个 Python 文件未变化。
- `pixi run just lint`：通过，包括 fmt check、workspace/all-targets Clippy、Ruff 和 profiles。
- `pixi run just docs-check-mermaid`：通过，230 个 Markdown 中 27 个图。
- `git --no-pager diff --check`：通过；现有三个文件另有 Git CRLF→LF 提示，不是本次新报告错误。
- 新报告 UTF-8、fence 配对、无冲突标记和尾随空白检查通过；最终 active phone lease 清单为空。

本次只增加候选报告，未修改 production、安装器、runner、AGENTS 或架构计划；未提交 upstream
issue，未上传设备日志、APK 或身份。
