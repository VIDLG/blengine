# Appium 4 beta：用户授权的 host-only 版本组合升级

日期：2026-11-10。用户授权升级 Appium 4 beta；本次只更新并安装 host 工具，不操作手机、
安装设备 helper、创建 WebDriver session 或运行 RF。它接续 [上游复核](2026-11-10-formal-android-appium-upstream-review.md)
之后的新决策，不改写此前稳定版失败结果，也不声称已解决 [HANS OFF stall](2026-11-10-formal-android-appium-root-cause-diagnosis.md)。

## 版本和隔离

| 组件 | 原 candidate | 本次 host candidate |
| --- | --- | --- |
| Appium | 3.9.0 | 4.0.0-beta.7 |
| UiAutomator2 driver | 8.7.3 | 9.0.1 |
| UiAutomator2 server | 10.7.6 | 10.7.10 |
| Settings helper | 8.4.5 | 8.5.2 |
| driver 内 appium-adb | 16.1.2 | 16.3.1 |

npm registry 本次返回 `appium@beta=4.0.0-beta.7`、`latest=3.9.0`；driver peer 为
`^4.0.0-beta.6`。Pixi Node `26.8.1` / npm `11.19.0` 满足双方 engines。
这是整套依赖组合升级，不是 Appium 主版本单变量实验。

工具由 `tools/appium/android/{package.json,package-lock.json,.npmrc,selection.json}` 和原
`tools/appium_toolchain.py` 管理，没有新增 installer、runner 或更改业务 API。
当前输入选择 beta 工具目录；这不授予设备自动准入或正式稳定版采用结论。

> 2026-11-13 布局更新：输入随 iOS 候选一同归入 `tools/appium/<platform>/`；上述四个文件
> 的**字节未变**，因此 `inputs_sha256`、home 名与 receipt 全部照旧，旧路径引用仅指当时的
> 位置。见[iOS 设备 lane 报告](2026-11-13-formal-ios-device-lane.md)。

- 新 home：`~/.cache/blehub/toolchains/appium/android-4.0.0-beta.7-uia2-9.0.1-4d4153d22890/`。
- 原 home：`~/.cache/blehub/toolchains/appium/android-3.9.0-uia2-8.7.3-06f2fb41c72a/` 保留；
  原输入副本、依赖和安装回执未覆盖。恢复 stable 选择需要恢复其输入并复验，不能将旧 home
  当作符合当前 beta lock 的 candidate。
- 新 lock SHA256：`35019db60a0854ebec1120dedabf54d1954c42366396b816e03833a7f9daf8de`。
- 三份新版 helper SHA256 已记录在 `selection.json` 和新 home 的 `installed.json`，
  只是本地文件证据，不能冒充手机已安装镜像。

## 安装和验证

1. 先修改版本锁定回归，执行 `pixi run just react-native tools-test-appium`：86 tests，
   其中只有旧版 lock 与新版期望不符这一项失败，作为 red。
2. 使用 `npm install --package-lock-only --ignore-scripts --install-strategy=nested
   --engine-strict --strict-peer-deps --no-audit --no-fund` 生成新 lock。
3. 首次 `pixi run just react-native appium-android-install` 被 `npm ci` 拒绝：
   bundled sharp `0.35.5` 的 optional 平台依赖记录不完整。没有成功回执，失败目录
   `android-4.0.0-beta.7-uia2-9.0.1-115891ae0c66/` 的 `install.log` 保留。
4. 再次显式 lock-only resolution，增加 `--include=optional`，补全平台包记录；不使用
   `--force`、legacy peers 或启用 install scripts。新 lock 导出不同 hash/home。
5. `pixi run just react-native appium-android-install` 成功，严格 `npm ci` 和 Android-only
   driver discovery 通过，生成新 home 的 `installed.json`；仅发现 UiA2 `9.0.1`。
6. 再次 `pixi run just react-native tools-test-appium`：**86 tests / OK**。
   回归同时检查新版 pins、peer、helper layout、selection channel 和 sharp optional lock 树。
7. 通过原 `AppiumServer` 执行本机 loopback/nonce 路径启动、fresh `/status` 和 bounded stop：
   **ready=true、build.version=4.0.0-beta.7、local_cleanup=passed**。没有创建 session。
   本地证据：`.work/appium4-beta-host-2026-11-10/{server.log,result.json}`。
8. `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 均通过；
   Mermaid 检查 27 个图 / 229 个 Markdown。另核对新版 selection/helper receipts、旧版输入
   和 helper SHA256 未变，以及 authored Markdown 编码、fence、空白和本地链接，均通过。

安装后源码只读核对发现 driver 保留 attach-only boolean constraints、nested helper layout，
并有 IPC claim/teardown 跟踪变更；这不是继承 capabilities 的全面行为验证，更不是移动端
runtime 兼容性证明。原 phone/session 身份、protected caps、deadline 和 cleanup 规则均不放宽。

## 剩余边界

- `device_validation=not_run`、`rf=not_run`；原 HANS OFF stall 仍未解决。
- 手机 helper 没有升级；下一次 session 不能把新版 host 与旧 helper 混用。必须先完成新版
  helper 的显式 provisioning、fresh installed identity/hash/readback、phone lease 和 forward
  冲突检查，再运行独立 non-RF candidate。
- 保持同一 App APK 和明确 HANS 条件；一次成功也不能声称长期稳定、native resource-zero、
  host gate、ABI 或 release pass。
- 不自动重启共享 ADB、不重跑 sealed failure、不引入业务 retry 或延长原故障预算。
- 本次没有发布 upstream issue，也没有上传日志、APK、截图或设备身份。

## 名词

- **Appium**：自动化入口；Android 这条路线通过 UiAutomator2 driver/server 操作手机 UI。
- **Maestro**：另一套 UI 自动化工具，以 YAML flows 描述点击、导航和断言；项目当前只将其
  作为 opt-in Android 对照。它不是 BLE 实现，也不能用 UI pass 代替 RF/cleanup verdict。
  参见 [官方文档](https://docs.maestro.dev/)。
- **HANS**：本机 Oplus/OnePlus ROM 的进程管理/冻结机制，不是测试框架，也不是 Appium
  capability。项目的 `ServerHansPolicy` 只对审核过 ROM、原 UiA2 UID/identity 做有界白名单
  补偿并恢复；HANS ON/OFF 在既有报告中指启用/不启用该临时豁免，不是全局关闭/开启系统 HANS。
  此前豁免下 lifecycle pass 是线索和 workaround，不是唯一根因证明。
