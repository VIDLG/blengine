# Appium 4 beta 真机对照：无 HANS 仍 stall，临时 HANS lifecycle 通过

日期：2026-11-10。用户明确授权继续，包含仅卸载重装 `io.appium.settings`、升级三份 helper、
同 APK 非 RF 对照及此前约定的单 run 临时 HANS 豁免。接续 [host-only 升级](2026-11-10-formal-android-appium4-beta-host.md)
和 [Settings 签名阻塞](2026-11-10-formal-android-appium4-helper-signature-block.md)。
本报告不改写旧 candidate 或本轮 failed bundle，不批准永久白名单或跨 ROM 通用补偿。

## 1. 结论与范围

| 本轮 candidate | 场景 | 结果 | 清理 |
| --- | --- | --- | --- |
| `0e1ccc8f-c169-4cde-924d-0834a70d2667` | 无 HANS：Start、同页静置、Close | passed | session/helper/phone passed |
| `8af6e307-f466-4db4-9bbb-02d6424e46c6` | 无 HANS：完整 lifecycle | failed；初始 title 查找即 stall，尚未 Start | 原 bundle quarantined；后续独立恢复通过 |
| `88974e20-f8ee-4737-834d-7e9edcc5ec68` | 临时 HANS：完整 lifecycle 与页面浏览 | passed；idle → started → closed | session/helper/HANS/debug/tag/phone passed |

- **已验证：** Appium `4.0.0-beta.7` / UiA2 driver `9.0.1` / server `10.7.10` 没有消除本机
  活动 session 内的请求停滞；失败窗口仍有 downstream `/status` 与新 ADB `service_accept`
  timeout。不能宣布根治，也不能因 start-hold passed 宣称完整无 HANS 可用。
- **已验证：** 同 APK、同新版三件套的独立临时 HANS lifecycle 通过；分类实际为
  `THIRD → WHITE → THIRD`，debug property 恢复，其他 package 分类未变。
- **限定推论：** 临时 HANS 豁免仍是这台受审 OnePlus/Oplus ROM 上有设备证据的 workaround；
  不是 HANS 冻结的唯一根因证明，也不是长期稳定性或 Samsung/Galaxy 支持证明。
- 三轮 `rf=not_run`，没有 scan、BLE grant、radio/firmware mutation。两次 UI pass 的
  `host_gate_status=pending`；native resource-zero、RF、ABI/release gate 均未由此关闭。
- 最终无 active phone lease，共享 ADB daemon PID `16841` 保留；成功 App 按既定策略保留在
  结果页。后续 candidate 必须重新核对其 retained identity，不能隐式接管。

## 2. 固定输入和安装基线

三轮消费同一预构建 App APK：

```text
SHA256: c96fc9909ed056e33e1ce47927639da742614294a60114d8ae546ee49255a584
package: com.blehubblenghost
activity: com.blehubblenghost.MainActivity
```

原始 APK 来自旧 `763dfcd3-f847-4a70-a25f-78c278a9e881` sealed bundle；本地 aapt2 badging
与 hash 核验通过，没有 rebuild 或代码变更。三轮各自 freeze 到新 attempt，不修改旧文件。
新版 host home 为：

```text
~/.cache/blehub/toolchains/appium/android-4.0.0-beta.7-uia2-9.0.1-4d4153d22890/
```

安装后的新版 helper SHA256：

| Package | APK SHA256 |
| --- | --- |
| `io.appium.settings` | `73be659a4dbb3a7d8a50faa5b63ee9719eafa6ed4e4d589e281e49806683ac6a` |
| `io.appium.uiautomator2.server` | `fe3aad50752ca459c0e91412f1361c1f54e41dff871d7414701748a6069aa397` |
| `io.appium.uiautomator2.server.test` | `6e9878f1ffbc5f496413a49dc96898b9a1aed418b8bbe91f028e76f2213602a0` |

### 显式 provisioning

- fresh lease 下核对原 boot、单 user 0、旧 installed path/hash/独立 UID、无 App/helper process、
  无 active instrumentation、无 forwarding，以及 reviewed ROM/debug property。
- 新授权只允许卸载旧 Settings；旧 UID `10254`，新版重新安装 UID `10394`。server/test 保持
  原独立 UID，通过同签名覆盖升级。没有卸载被测 App、绕过签名校验或重签 APK。
- 使用原 `android_sdk.install_apk` 与 root-aware installer，显式 `use_root=True`、
  `grant=False`、test APK 的 `test_only=True`；逐命令 owner/deadline 守护、临时上传文件清理。
- 每份 installed path/hash/UID 与最终 instrumentation component 均核验；全三件套就绪后
  才允许 managed session。新 Settings 安装后 mock-location 为 default deny，不沿用旧 UID。
- 第一段一次性编排在卸载成功后，误将 `pm path` 的 absent exit 1 当异常，停止而未安装。
  `.work/appium4-helper-reprovision-2026-11-10/` 的 run `9dfe6880-4823-4c34-97d8-950b18247cef`
  保留 failed，phone cleanup passed。没有重复卸载；下一 fresh lease 验证 Settings absent 和
  后两份仍为原身份后继续安装。最终 provisioning run `17ac0eb9-2b4d-4e38-8969-6fcc85eddbab`
  passed，证据在 `.work/appium4-helper-reprovision-2026-11-10-resume/`。

### 旧恢复清单遗漏的 hidden-API 残留

provisioning **之前**观察到三项 `hidden_api_policy*` 均为 `1`，本次 installer 未写入。
旧 failed bundle 的 `platform/prepared/result.json` 记录初始均为 absent/null，旧
`automation/appium.log` 明确记录该 session 的三项 `settings put ... 1`；此前独立恢复摘要
只检查 App/helper/store/AppOp/tag/forward，漏查这三项，不能当作完整恢复证明。

本轮离线重放旧 bundle integrity passed，旧 verdict 仍 failed。随后在同 boot、新租约、
无 App/helper/instrumentation/forward、共享 ADB 未变条件下，只删除这三项已归属 Appium 的
残留，并核对 `get=null` 与 global key inventory 不存在；没有 global reset 或改其他 key。
独立证据 `.work/appium4-known-policy-recovery-2026-11-10/`，run
`1c0c615f-6cf0-4e54-b763-d87edc5d5a5f`，recovery/phone passed。正式 owner 的 absent-key
准入要求没有放宽。

## 3. 执行入口与三轮证据

三轮均通过现有入口，不新增 runner：

```text
pixi run just react-native formal-host-android-managed-lifecycle
```

共享参数为显式 local device、上述 package/activity/APK/helper hashes、新版 appium home/entry、
`--appium-port 4727 --system-port 8247 --timeout 240 --phase-timeout 30 --cleanup-seconds 60`
以及 `--authorize-install --authorize-helper-settings --use-root --close-diagnostics`。
没有 power/freezer exemption、device-local tcpdump 或 retry/timeout 修补。

| 轮次 | 增量参数 | Output root | CLI exit |
| --- | --- | --- | --- |
| 无 HANS start-hold | `--automation-start-hold-control` | `.work/formal-android-appium4-start-hold-off` | 0 |
| 无 HANS lifecycle | 无控制场景或豁免开关 | `.work/formal-android-appium4-lifecycle-off` | 2 |
| 临时 HANS lifecycle | `--authorize-server-hans-exemption` | `.work/formal-android-appium4-lifecycle-hans` | 0 |

### Start-hold passed 不等于修复

`0e1ccc8f…` 为独立 `formal-android-managed-start-hold-control` scope，只在 Current run
静置五秒，不做完整导航；idle/started/closed、Close 设备日志、DELETE/helper/tag/phone 均通过。
其独立 status/ADB failure probes 没有执行，不把缺文件当通过。

进入下一 candidate 前，依据已重放 bundle 的 `candidate/bound-identity.json`，在新租约下
fresh 核对原 APK/boot/PID/UID/start ticks/cmdline，精确 force-stop 本轮 retained App，
确认 App/helper absent、无 instrumentation/forward 后释放。证据在
`.work/appium4-start-hold-retirement-2026-11-10/`，run
`d4f5db12-4a60-4372-814c-99d458d18b65` passed。进程退休不声称 native resource-zero。

### 无 HANS 完整 lifecycle：首个 title 查找 stall

`8af6e307…/automation/appium.log` 确认运行的确为 beta.7/driver 9.0.1/server 10.7.10。
其成功边界及首个失败为：

```text
session 建立，downstream /status ready=true，forward 已建立
→ downstream GET /appium/settings 200
→ mobile: getCurrentPackage 返回 com.blehubblenghost
→ POST /element，selector resourceId("formal-host-title")
→ client 30s ReadTimeout
→ fresh/direct downstream /status 约2s timeout
→ ADB version、cached device、transport select OKAY
→ 新 shell service_accept 约3s timeout
→ session DELETE / 后续 shell/logcat/helper restore 无法完成
```

本轮 `observations=[]`、无 business Start/Close click。故障早于旧版 `763dfcd3…` 的
Started → Evidence 导航后 `session-state` 查询 stall；不能将本轮描述成 Close 未返回，
也不能从相同后续 timeout 模式断定唯一相同根因。此结果说明重现该类故障不必先提交业务
Start/Close，不支持先改 Rust/Close/watchdog。

本轮 live-logcat raw 只有 156 bytes；没有相应设备 handler 日志不证明请求未到设备。
host `Proxying` 也不证明远端收包。`cached_state=device` 和 daemon 协议响应不证明新 service
健康；没有证据把故障唯一定位到 HANS、UiA2、adbd 或 USB。

原 bundle 的 scenario/session/host_gate failed、phone quarantined 和缺失 DELETE 回执全部
保留。offline integrity passed 不是 device pass。

### 临时 HANS 完整 lifecycle passed

`88974e20…` 在相同 APK/helper/host 下完成 idle → started → closed 与 Tests/Current run/
Evidence 浏览。初始化快照为 poweredOn、scan denied、connection denied；没有 grant 或 scan。

- HANS 前置 ROM/debug eligibility 在 prepare/admission 检查；分类与 addwl 仍在绑定原 UiA2
  identity/session 后执行，不凭 preflight dump 直接授权 process mutation。
- `platform/server-hans-result.json`：原 THIRD，临时 WHITE，`status=passed`、
  `debug_restored=true`、`other_classifications_changed=false`。
- `platform/server-hans-restore-result.json`：WHITE 恢复 THIRD，`status=passed`、
  debug property 恢复，其他分类未变。恢复声明只覆盖 classification，不声称重建 transient
  timestamps/state。
- session DELETE、platform finalizer、helper AppOp/store、Close log tag 和 phone 均通过，
  `cleanup_errors=[]`、`phone_cleanup_errors=[]`；host gate 仍 pending。
- 成功页由 App 保留，host lease 已释放；没有永久白名单、全局关闭 HANS 或应用到其他 OEM。

## 4. 无 HANS 失败的独立恢复

证据目录：`.work/appium4-lifecycle-off-recovery-2026-11-10/`，不写入 sealed bundle。

第一条有界只读 boot admission timeout，保持 quarantine；离线日志分析和软件回归后，第二次
独立只读 admission 取得相同 boot，期间没有 daemon restart、USB 重插、radio toggle 或
unknown process stop。

恢复绑定原 owner bytes/lock identity、原 host 已退出、原 manifest/receipts hashes、原 App 和
UiA2 OS identity。检查 helper installed path/hash/UID、唯一匹配 active instrumentation、仅
owned forward、原 store stat/content、package-scope mock allow、known tag/hidden-API 值后：

1. fresh 校验各自完整原身份后，精确退休原 server 与 App；不 force-stop 未知 test/helper。
2. 只移除 `tcp:8247 → tcp:6790` owned forward。
3. 在删除同一命令内再次核对 store 非链接、stat pin 与精确内容，只删除本轮创建的 store。
4. 恢复 Settings package-scope mock deny、三项 hidden-API key absent、Close log tag 原空值。
5. 复验 boot、App/helper UID/process absence、instrumentation、Settings services、forward、
   helper/App installed identity、AppOp/store、IME、hidden key existence、debug/tag 后才释放。

第一次恢复在最后的空 tag 写入处遇到一次性编排错误：ADB separate empty argv 被 device
shell 丢弃，`setprop` 返回 usage/exit 1。因此保留 recovery failed/quarantined，不冒充完成。
后续仅针对剩余 tag 在独立 `finalize/` 证据中使用带明确空值引号的设备命令，先后完整读回：
`recovery_status=recovered_failed`、`phone_status=passed`、`cleanup_errors=[]`。未延长原 scenario
预算，未把独立恢复等同成功 DELETE 或 native resource-zero。后续 HANS candidate 在该恢复
完成后才获得新租约。

## 5. 软件与离线验证

- `pixi run just react-native tools-test-appium`：86 tests / OK，73.389s；硬件无关回归不替代
  本次设备三轮结果。
- 分别执行 `pixi run just react-native formal-host-lifecycle-verify <attempt>`：旧稳定版
  failed bundle、本轮 start-hold、本轮无 HANS failed、本轮 HANS passed 的 integrity 均通过。
  scope/verdict 不升级；无 HANS bundle 仍显示原 quarantined，而独立 recovery 另记 passed。
- `pixi run just react-native tools-test android_hans_policy`：26 tests / OK，27.221s。
- `pixi run just fmt`：通过，352 个 Python 文件未变化；`pixi run just lint`：通过，含
  workspace/all-targets Clippy、Ruff、Just/Cargo fmt check 与 profiles。
- 新增/更新报告 UTF-8、fence、尾随空白、冲突标记和本地链接检查通过；
  `git --no-pager diff --check` 通过，既有三个文件的 CRLF→LF warning 保留且未顺手修复。
- 最终本地检查 active phone lease 清单为空，5037 listener 仍为原 PID `16841`。

## 6. 后续边界

- Appium 不应宣告不可用，也不应宣告已通用修复：本机有临时 HANS 条件下的新版完整 UI pass，
  无 HANS 条件下仍有明确 failure。它是整套依赖升级的比较，不是主版本单变量因果实验。
- HANS profile 只适用于 reviewed Oplus ROM；Samsung/Galaxy 需要自己的设备/电源策略证据，
  不复制 HANS 命令，不把当前 workaround 设置为 Android 全局默认。
- 可以用新旧版本、无 Start 的早期失败及 HANS 对照整理脱敏 upstream issue；本轮没有实际
  发布、上传日志/APK/截图或设备身份。公开前只提供必要版本、阶段和脱敏最小日志。
- 不继续同条件盲跑，不扩大 timeout、不改业务关闭语义，不跳到 RF/stress/ABI freeze。
  后续设备操作仍需 fresh retained-App identity、lease、helper/ROM preflight 和明确场景范围。
- 本轮未修改 production、runner、installer 或 AGENTS；一次性 provisioning/recovery 编排的
  失败均保留，不能把其脚本错误归入 Appium 原 bug。
