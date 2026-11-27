# Android Appium/UiAutomator2 上游复核：active-session stall 尚无可直接采用的根治补丁

日期：2026-11-10。本文接续 [HANS OFF 根因诊断](2026-11-10-formal-android-appium-root-cause-diagnosis.md)，只记录软件版本、上游源码/发布记录和公开 issue 复核；没有执行手机命令、没有修改本地第三方 cache、没有修改 Appium 版本，也没有改变 RN/Rust/Close 实现。

## 1. 结论

社区确实遇到过 `socket hang up`、UiAutomator2 instrumentation crash 和停止延迟，但截至本次复核，没有发现能直接覆盖 BleHub 故障链的通用上游修复。

BleHub 当前故障链是：

```text
active Appium/UiA2 session
  → Started 后 UI request timeout
  → bound UiA2 /status timeout
  → ADB protocol version/cache/select 成功
  → ADB service_accept timeout
  → DELETE 无响应
```

公开报告大多属于下面几类，不能相互合并：

1. **启动期旧 instrumentation/session race**：可以通过启动前停止确认或版本/安装一致性处理；
2. **UiA2 instrumentation 自身 crash**：表现为 `Process crashed` 后 `socket hang up`，需要故障窗口 logcat 才能确认；
3. **host/device connectivity 或 ADB transport stall**：需要把 UiA2 HTTP、ADB daemon、adbd service、USB/ROM policy 分层取证；
4. **active session 中 UiA2 server 与 ADB service 同时失去响应**：本次 BleHub 证据属于这一类，尚未找到对应的 upstream fix 或单一因果证明。

因此当前不能声称“Appium bug 已根治”，也不能把 HANS、增大 HTTP timeout、自动 retry 或 Maestro pass 当作根治。

## 2. 当前锁定版本和上游版本变化

BleHub 锁定在：

| 组件 | 当前锁定 | 上游观察 |
| --- | --- | --- |
| Appium server | `3.9.0` | 当前正式 host 选择保持不变 |
| UiAutomator2 driver | `8.7.3` | 2026-10-28 发布；唯一 bug fix 是升级 server 到 `10.7.6` 以修复 UiA2 issue `#988`（Compose elements not displayed） |
| UiAutomator2 server | `10.7.6` | 与当前 driver lock 一致 |
| Appium ADB dependency | `16.1.2` | 未发现针对本次 `service_accept` stall 的 release fix |

UiAutomator2 driver `v9.0.0`/`v9.0.1`（2026-11-01）包含两项与 session 生命周期有关的变化：

- IPC session claim 时丢弃 obsolete sessions；
- 同一设备的新 session 等待旧 session teardown 完成。

但该版本要求 **Appium `>=4.0.0-beta.0`**、Node 22.22.2+，并将 server dependency 提升到 `^10.7.10`；它不是当前 Appium 3.9.0 host 的可直接替换版本，也没有证据表明它修复 active-session 后段的 ADB `service_accept` stall。不能只升级 driver 来改变正式 candidate。

## 3. 官方文档和上游源码结论

官方 UiAutomator2 troubleshooting 对 `socket hang up` 的定义是：Appium 无法连接设备上的 UiAutomator2 REST server。官方列出四类原因：

- session 已被 DELETE，server 已随之停止；
- Android OS 杀掉 UiA2 server；
- UiAutomator framework 或 UiA2 server crash；
- host/device connectivity 不稳定。

官方建议收集 `io.appium.uiautomator2.server` 的 logcat；并明确 `uiautomator2ServerReadTimeout` 只是“等待 UiA2 HTTP 响应的最大时间”，不负责修复 ADB service 建立或设备端进程状态。

对当前锁定 driver 的源码复核显示：

- UiA2 startup 对 instrumentation startup 有有限 retry；
- active session 的普通 proxy command 没有能够安全恢复远端 server 的通用 retry；
- DELETE 失败只记录 warning，不能证明远端 session、instrumentation 或 native resource-zero 已关闭。

这与 BleHub 的 owner/cleanup 语义一致：不能对未知身份盲目重试命令、重建 session 或把 host child 退出解释成远端清理成功。

## 4. 社区同类 issue

### 4.1 UiAutomator2 driver #655：启动期停止延迟

[PR #655](https://github.com/appium/appium-uiautomator2-driver/pull/655) 描述 Android 13 Samsung 上一次新 session 的启动过程：旧 UiA2 instrumentation 停止延迟，期间出现 `socket hang up`，随后新 instrumentation 启动。该 PR 增加了启动前停止状态确认，并发布在 driver `2.29.9`。

它证明：

- Android/OEM 的 instrumentation stop delay 确实会产生 `socket hang up`；
- “旧 session 未完全退出”是已知启动期类别；
- 这不是本次 active-session 后段 stall 的充分解释，因为本次故障发生在新 session 已成功建立、已完成 idle→started 之后。

### 4.2 UiAutomator2 driver #842：多台 Samsung/Android 上 instrumentation crash

[Issue #842](https://github.com/appium/appium-uiautomator2-driver/issues/842) 报告 `POST /element cannot be proxied ... instrumentation process is not running`。后续评论（2025-08-06）在 Galaxy S25、Galaxy Tab、Pixel 等设备上记录了：

```text
Process crashed
socket hang up
The instrumentation process has exited
```

该 issue 仍是公开的 instrumentation crash 类问题，没有一个适用于所有 ROM 的解决方案。它与本次的 UiA2 侧症状相似，但本次 HANS OFF candidate 的故障窗口受 ADB stall 影响，尚未取得原子 crash/kill logcat 证据，因此只能作为“社区已有同类类别”的旁证，不能直接判定本机就是同一 crash。

### 4.3 UiAutomator2 server #708 / driver #903：版本混装导致 instrumentation 启动失败

[server #708](https://github.com/appium/appium-uiautomator2-server/issues/708)（由 [driver #903](https://github.com/appium/appium-uiautomator2-driver/issues/903) 转发）记录 `NoSuchMethodError`、`socket hang up` 和 instrumentation 退出。最终维护者判断更像 client 侧 Appium 本地安装混乱/版本混装，而不是 server 的通用 runtime bug。

这类问题是当前 BleHub 已通过 lock-specific Appium home、helper APK SHA256、installed identity 和 single-image/receipt 检查排除的方向之一；不能用来解释当前已锁定、已核对 hash 的 candidate。

## 5. 对 BleHub 当前故障的判断

### 已排除或不足以解释

- RN/Rust 的 `Close`：失败发生在 `Close` 提交前；
- 只改 click API：历史 click/clickGesture 对照未改善；
- 只增大 `uiautomator2ServerReadTimeout`：不能修复 ADB `service_accept` timeout；
- 旧 QtScrcpy/ADB daemon provenance：已做隔离对照，不能作为充分根因；
- helper APK/hash 混装：本轮 helper receipt 与锁定版本一致；
- HANS OFF 单纯启动 race：本轮 active session 已建立并运行到 Started 后；
- Maestro 通过：Maestro 是独立 UI 对照，不经过 Appium/UiA2 proxy，不能证明 Appium 根因已消失。

### 尚未证明的候选

1. Oplus/OnePlus ROM 的 HANS/freezer policy 在 active UiA2/应用组合下冻结或阻塞进程；
2. UiA2 instrumentation 在 Started 后 crash/被 ROM 杀掉；
3. ADB daemon 到设备 `service_accept` 的 host USB/transport 或 adbd service 阶段停顿；
4. UiAutomator framework 在特定 RN/Fabric UI 查询后阻塞，随后触发设备侧连锁失活。

当前证据只允许说这些层在同一窗口失效，不允许选择其中一个作为唯一根因。

## 6. 是否应立即升级版本

**不立即升级正式 lock。** 理由：

- `8.7.3` 已包含当前 server `10.7.6`，其发布说明没有 active transport 修复；
- `9.0.1` 需要 Appium 4 beta/新 Node，并带有 session-claim/teardown 变化，属于架构和版本对照，不是无风险 patch；
- 在没有 fresh, single-variable candidate 前升级会同时改变 Appium server、driver、ADB dependency、UiA2 server 和 session lifecycle，不能用于归因；
- 项目规则禁止用未授权版本变更替代 release-shaped candidate 证据。

若后续用户单独授权版本对照，应将它作为新的独立 non-RF candidate：新的 tool home、APK/helper receipt、phone lease、同一 APK、同一 HANS policy、同一 start-hold/lifecycle scope，并在失败后做独立 recovery；不得重封本次 failed bundle。

## 7. 建议的 upstream issue 最小证据包

可以向 `appium/appium-uiautomator2-driver` 或维护者提交一个**诊断请求**，而不是宣称已有确定 bug。正文应包含：

- Appium `3.9.0`、UiAutomator2 driver `8.7.3`、UiA2 server `10.7.6`、Appium ADB `16.1.2`；
- OnePlus/Oplus、Android API 36、HANS OFF；敏感 serial/UID/PID 用脱敏标识；
- active session 已完成 idle→started；
- UI request timeout；
- 同一 bound UiA2 `/status` fresh connection timeout；
- ADB smart socket：`host:version`、cached state、transport select 成功，shell `service_accept` timeout；
- Appium DELETE 无响应；
- HANS ON 同 APK lifecycle 对照通过，但仅作为 ROM policy comparison；
- recovery evidence：exact identity retirement、forward/helper/store/AppOps restore、共享 ADB daemon 未重启；
- 明确说明未取得 atomic freezer snapshot，未声称 instrumentation crash 已证实；
- 请求维护者判断应向 driver、UiA2 server、appium-adb、adbd/USB 或 OEM ROM 哪一层继续取证。

不应上传原始 UI text、完整 capability/payload、serial、用户数据、APK 或包含敏感页面的截图；只提供脱敏时间线、版本、阶段状态和可复核的最小日志片段。

## 8. 下一步

在用户另行授权前保持当前软件和手机状态：

1. 不再重复 HANS OFF 完整 lifecycle；
2. 不修改 RN/Rust/Close/watchdog；
3. 不把 HANS ON 提升为 Android 默认策略；
4. 优先由维护者反馈或单独批准 Appium 4/UiA2 9 version comparison；
5. 在 root cause 未明确前不进入 RF，不升级 host gate/ABI/release verdict。

本报告是 upstream/source review，不是 Appium pass、host gate pass、native resource-zero、RF 或 release evidence。

## 9. 公开来源

- [UiAutomator2 troubleshooting: Socket Hangup](https://github.com/appium/appium-uiautomator2-driver/blob/master/docs/troubleshooting/index.md)
- [UiAutomator2 capabilities](https://github.com/appium/appium-uiautomator2-driver/blob/master/docs/reference/capabilities.md)
- [driver v8.7.3 release](https://github.com/appium/appium-uiautomator2-driver/releases/tag/v8.7.3)
- [driver v9.0.1 release](https://github.com/appium/appium-uiautomator2-driver/releases/tag/v9.0.1)
- [driver #655](https://github.com/appium/appium-uiautomator2-driver/pull/655)
- [driver #842](https://github.com/appium/appium-uiautomator2-driver/issues/842)
- [server #708](https://github.com/appium/appium-uiautomator2-server/issues/708)
- [driver #903](https://github.com/appium/appium-uiautomator2-driver/issues/903)
