# Formal Android：真机准备阻塞与 release 构建候选

日期：2026-11-04。真实 Mac、本地 Pixi/Just、未提交工作树。依据：[单一 Plan](../../../.plan/PLAN.md)、[concrete owner 软件候选](2026-11-04-formal-android-session-owner.md)。用户授权 Android 安装；不将安装授权扩展为 AppOps、DUT BLE 权限、radio 或 firmware 变更。

## 结论与边界

- 唯一 Android 手机 `3ac5cc09` / PHB110 / API 36 已进行实际只读 preparation。三个 helper APK 已安装且 hash 与审核候选一致，**未重装**。
- OEM envelope、输出上限与 CLI 空清单问题经过 test-first 修复；最新 preparation 正确停在受支持 profile 的前置条件，不再误报 CLI 格式错误。
- 当前 Settings helper 的 mock-location 是 **default-scope deny**，不是 package-scope allow；`/data/local/tmp/mock_apps.json` 读取返回文件不存在。未自动补配置。
- 正式 RN release-shaped APK 已构建并静态检查；**未安装或启动该 App，未创建 Appium session，未执行 scan/RF**。release 使用 consumer 的 debug signing key，不是商店发布包。
- 本轮 preparation 的主结果为 blocked/failed，cleanup errors 为空，App-only phone lease 已释放；未将未开始的 session 标记为成功，也不声称资源归零。

## 实际 preparation 与修复

证据根目录（ignored）：`.work/android-install-9fe81951-544c-4f98-a5ac-d54b967beb91/`。

| Attempt | 实际结果 |
|---|---|
| `8cc8fa37-ecbc-402c-8015-53877eb31cc5` | 前序只读检查发现 OnePlus `dumpsys activity processes` 带单个 `---- TIME … ----` 前缀，完整输出约 1.4 MiB；未安装。 |
| `742e3d21-81fd-4b30-97ff-ebaff09dc2cf` | 前序安装准备确认三个 helper 均已是 exact-hash 候选，因此未执行安装或 `su`；owner 被空 forward 输出 `"\n"` 阻塞。 |
| `780d1fdd-d5c5-488a-b05c-6881628c657b` | 修复 forward parser 后越过该处，实际 AppOps 输出 `No operations.\nDefault mode: deny\n` 暴露默认值解析缺口。 |
| `b806b43f-7d75-4a85-a620-13f87bc07443` | 修复默认值解析后，inventory 完成，但 owner 明确拒绝缺少 package-scope allow 的 profile；独立只读 store 检查返回不存在。 |

最新 attempt 中 `owner/prepared/result.json` 是 inventory observation，不是 session authority。顶层 `result.json` 保留 preparation 阻塞与独立 store observation；`phone.json` 保留 failed 主结果和空 cleanup errors。这里只执行读取，phone finalizer 没有 session/App 资源需要回收；这不是已建立 session 的 finalizer 通过证据。

### CLI 与 scope 的窄范围修复

实现位于 [`bleng_android_automation.py`](../../../tools/react_native/bleng_android_automation.py) / [`bleng_android_session.py`](../../../tools/react_native/bleng_android_session.py)：

- 共用 `parse_adb_forward_list()`：只忽略末尾空行，全空输出为空清单；前导/中间空行、重复行、非法 TCP endpoint 继续拒绝。idle inventory、active bind 和 final inventory 不再各自解析不同格式。
- `No operations.` 后的显式 default mode 按 `allow/deny/ignore/default` 原样记录为 **default scope**，不冒充 package-scope grant。额外/重复/冲突输出仍拒绝。
- 默认 `allow` 也不足以通过当前 owner：明确 package-scope allow 的要求没有被 parser 放宽。新测试同时保护 default deny/default allow 均不触发 Remote 或自动 repair。
- 前序 OEM 修复只允许单个合法 TIME wrapper；仅 exact global process dump 使用独立的 `< 4 MiB` 上限，普通命令仍保留原上限。不能据此声称所有 OEM active-session 输出已验证。

## 已安装的 helper 候选

| Package | SHA-256 |
|---|---|
| `io.appium.settings`（8.4.5） | `0f8de773243d45d2ecd05613e4fcce526d674738b1979212cd2cf5e5fe83f871` |
| `io.appium.uiautomator2.server`（10.7.6） | `b0dea678506e4a1d98bd003a3c8842ae2ff119e3d17f63c9d657c2d61d9f198d` |
| `io.appium.uiautomator2.server.test` | `f18ce4e6fc9ca7606875bca4f9739b2e1d3f0bf3544c432e071ff2d24f43ed31` |

安装路径与 UID、注册、原始命令回执保存在各 attempt 中；不把这些历史路径当作以后 run 的 authority。root 已可用不是阻塞，本轮没有缺失 APK 需要通过 root-aware installer 安装。

## Formal App 构建

[`formal-host-android-build`](../../../just/react-native/mod.just) 复用一个入口：默认 debug 仍构建 App 与 native contract test APK；显式 `release` 只构建内置 JS 的 release variant，非法 variant 非零拒绝。说明位于 [formal consumer README](../../../tests/consumers/react-native-bleng/README.md)。普通 recipe 保留正常 Gradle dependency resolution，不强制依赖本机缓存。

本轮已有依赖时实际执行：

```sh
pixi run just --no-deps react-native formal-host-check
timeout --kill-after=10s 180s pixi run just --no-deps react-native formal-host-android-build release
```

构建时该 recipe 尚带 `--offline`；该参数随后从 maintained recipe 移除以避免 fresh checkout 缓存回归，移除后只做 dry-run/格式检查，未再次构建。原构建 **BUILD SUCCESSFUL in 1m 2s**，没有 timeout/retry。

产物：`tests/consumers/react-native-bleng/android/app/build/outputs/apk/release/app-release.apk`。

- SHA-256：`e759808968673fe070af463337d98ae162c9b580c5b0b2eb4abd0478feec547f`。
- 大小：42,825,730 字节；package：`com.blehubblenghost`；activity：`com.blehubblenghost.MainActivity`。
- manifest 未标记 debuggable；包含 `assets/index.android.bundle`，1,027,848 字节。
- `arm64-v8a` / `x86_64` 分别只有一个 `libbleng_uniffi.so` 定义三个 component contract markers；这只是静态 image 检查，不是 ART/Nitro 运行通过。
- 日志：`.work/logs/formal-host-android-release-build.log`、`.work/logs/formal-android-release-apk-metadata.log`、`.work/logs/formal-android-release-apk-audit.log`。

## 本轮验证

```sh
pixi run just --command python -W error::ResourceWarning -m unittest \
  tools.tests.react_native.test_bleng_android_automation \
  tools.tests.react_native.test_bleng_android_session \
  tools.tests.react_native.test_android_automation_observation -q
```

- forward 两项 focused regressions 先失败；修复后 automation/session 组合 **53 passed**。
- 新 default-mode 回归先失败（三个不支持的默认值 subcases）；修复后以上三模块组合 **70 passed，46.847 秒**。日志：`.work/formal-android-cli-readbacks-green.log`；default-mode red：`.work/formal-android-appops-default-red.log`。
- 四个当前修改的 Python production/test 文件通过 targeted Ruff lint 和 format check；Just 格式与 default/release/invalid dry-run 通过。invalid 的 `--no-deps` 实际调用也以退出码 1 拒绝，未启动 Gradle。
- `formal-host-check --no-deps` 的 typecheck/autolinking/no-internal 检查通过。
- `pixi run just react-native formal-host-test`：**48 passed**，保留已有 SafeAreaView deprecation warning。日志：`.work/formal-android-cli-readbacks-host-test.log`。
- `pixi run just docs-check-mermaid`：201 份 Markdown 中 27 个图通过；本轮四份 authored/updated Markdown 的 UTF-8、围栏、尾随空白、冲突标记和本地链接检查通过。日志：`.work/formal-android-cli-readbacks-mermaid.log`。
- 全量 `pixi run just lint` 被未修改的 `crates/bleng/src/operation_observation.rs:186` 上 `clippy::module_inception` 阻塞。未越界修复；不声称全仓 lint 通过。未执行会重写其他 dirty 文件的全仓 `fmt`，使用针对性格式检查。

## 下一边界

需要单独授权、限定范围并保存/恢复原值，才能准备 helper 的 mock-location AppOps 与 mock-app store，再验证真实 managed session 的建立/DELETE/finalization。自动化 preparation 不能默许改变 DUT BLE 权限、radio、IME 或固件。正式 App 安装/启动、其后的 UI runtime、完整 host bundle、iOS concrete owner 与 RF 仍需各自证据；本报告不完成 phase/ABI exit，也不满足 internal 删除条件。
