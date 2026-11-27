# Android 多机型 preflight：共用 owner，隔离 OEM 策略

日期：2026-11-10。用户要求通用 preflight 考虑一加以外的 Samsung/Galaxy 等机型。
本次为现有 Android automation owner 增加只读 build/device 观测与 OEM 策略拒绝边界，
不增加 runner、registry、工具依赖或强制第二台设备门槛，不修改 BLE/Rust/RN API。

## 测试是否通过

必须区分上一轮设备 candidate 和本次软件改动：

| 范围 | 已验证结果 | 不能外推的结论 |
| --- | --- | --- |
| 既有 Appium 4、一加、临时 HANS、完整 UI lifecycle | passed；分类和清理恢复通过 | 无 HANS 稳定、三星真机、RF、native resource-zero、release |
| 既有 Appium 4、一加、无 HANS、完整 lifecycle | failed；随后独立恢复通过 | 升级已根治或 failed verdict 可以改写 |
| 本次通用 profile、Samsung/Google/未知厂商软件样本 | 软件回归通过 | 对应品牌真机或 OEM 电源策略已验证 |
| 本次一加新字段的实际只读采集 | passed | 新 preflight 的完整 session/lifecycle 已重新验证 |

上一轮 UI 对照的具体 run、版本、失败和恢复仍以 [Appium 4 真机报告](2026-11-10-formal-android-appium4-device-comparison.md)
为准。本轮没有重跑真实 WebDriver/UI/RF，不把前一轮 runtime pass 继承为改动后的新 candidate。

## 实现与职责

继续使用 `tools/react_native/bleng_android_session.py::AndroidAutomationOwner` 的原 SDK/lease/
deadline/receipt boundary。`_read_device_profile` 只读取固定五项属性：

| 证据字段 | Android property |
| --- | --- |
| `manufacturer` | `ro.product.manufacturer` |
| `brand` | `ro.product.brand` |
| `model` | `ro.product.model` |
| `api_level` | `ro.build.version.sdk` |
| `fingerprint` | `ro.build.fingerprint` |

每项为有界、非空、可打印单行；API 必须为规范正整数，不把本机 API 36 写成共同最低版本。
fingerprint 仅保留为 opaque 身份观测，不按其格式解析 capability。输出缺失、异常或漂移明确
拒绝，不安装工具、重试、fallback 到另一设备或静默采用默认值。

`_extra_observations` 将 profile 放入 `prepared-owner.json` 的 `extra.device_profile`。
既有 prepare → admission → bind → finalizer 的基线比较自然包含这五项，不在每次 cheap
`require_owner`/UI 字段读取时额外执行 ADB。额外观测仍是顺序且非原子的，build properties
可被定制 ROM/root 改写，不是 ROM authenticity attestation；原 boot、helper/APK/UID、session、
原进程和资源所有权检查保持独立。

`prepared-owner.json.automation_policy` 记录请求的策略：

- 默认 `generic`：Samsung、Google、其他/未知厂商，以及未明确授权 HANS 的 Oplus 都走
  同一个通用路径。并非“unknown device 自动失败”，而是实际命令/输出/ownership 不满足契约才拒绝。
- 显式 `oplus-hans-temporary`：只在 HANS flag、Oplus manufacturer/brand family、原 reviewed
  ROM hash/debug eligibility 全部匹配时记录。family 是拒绝不适用策略的 routing hint，
  不能单凭品牌授权加白。Samsung/未知 manufacturer 即使 brand 写成 OnePlus 也不会准入。
- `android_hans_policy.py::ServerHansPolicy.preflight` 在任何 Oplus-specific ROM/debug 命令
  之前拒绝非 Oplus profile；实际 addwl 仍依赖绑定原 PID/start ticks/UID、host/device session，
  原 WHITE 保留、原 THIRD 临时变化并恢复的边界没有放宽。
- 不自动选择三星电源管理命令、后台白名单、root、HANS 或全局 power/freezer bypass。
  Samsung 的通用观测路径已经有软件验证，但真实 helper/session/parser/cleanup 和具体 ROM
  行为仍须真机证据。以后若有独立 OEM 补偿，仍须具名授权、前置 eligibility、原身份/回读及恢复，
  不为目录或品牌对称创建空 adapter/第二个 runner。

本次只扩展既有 prepared receipt 内容，不更改 offline bundle schema 或要求旧 sealed receipts
回填新字段；原 receipt hashes/immutable authority 规则仍有效。

## 软件验证

先改严格 SDK sample 和测试，再运行：

```text
pixi run just react-native tools-test multi_model_preflight
```

首次 6 tests red（19 subtest failures、4 errors）：旧实现未记录 profile、未检查 drift/非法字段，
也未在 OEM-specific 读之前拒绝三星。实现后 6 tests green；随后补 finalizer drift 回归，
再补 HANS 独立补偿与 profile 漂移的组合边界后，最终 **8 tests / OK**。覆盖：

- 一加、三星、Google、未知 manufacturer 的 prepare/admission/binding/正常 finalize；默认
  policy 保持 generic，无 HANS/freezer/property mutation，重复 cheap owner guard 不新增 I/O。
- 三星/Google/未知 manufacturer 在显式请求 HANS 时，不读取 Oplus ROM/debug，不执行
  scene/addwl/remove；没有 session 提交。
- 五项任一字段在 admission 前漂移被拒绝；binding 或 finalizer 的 build 漂移不接管、不假闭合。
- HANS 已 apply 后仅 metadata 漂移，原 lease/boot/受审 ROM/helper 身份仍匹配时，仍精确恢复
  本轮唯一 UID 的 WHITE → THIRD 及 debug，其他分类不变；之后普通 finalizer 拒绝发布
  finalized/release。重复 finalize 不重发补偿，不把恢复成功当 session 成功。
- 空值、多行、NUL、超长、非法 API、SDK exit failure、timeout、lease drift 均失败且不发布
  prepared authority。

相关整组检查：

| 命令 | 实际结果 |
| --- | --- |
| `pixi run just react-native tools-test test_bleng_android_session` | 最终 74 tests / OK，138.942s |
| `pixi run just react-native tools-test test_bleng_android_preparation` | 58 tests / OK，40.766s |
| `pixi run just react-native tools-test test_bleng_android_prepared_scan` | 4 tests / OK，88.738s |
| `pixi run just react-native tools-test android_hans_policy` | 最终 28 tests / OK，37.819s |
| `pixi run just react-native tools-test test_bleng_lifecycle_evidence` | 69 tests / OK，3.106s |

这些命令均未超时；测试样本只替换现有 SDK/HTTP/child 边界，不连接真实设备或证明 OEM runtime。
另以 `pixi run just react-native formal-host-lifecycle-verify <attempt>` 重放旧的 Appium 4 HANS pass
和无 HANS failed bundles，两者 integrity 均 passed；旧 receipt 无需回填新字段，无 HANS 原
failed/quarantined verdict 保留，独立恢复仍是另一份证据。

只读审查后保留既有独立 HANS compensation 顺序，并补上述组合回归，而非将普通 owner 的
失效状态或描述字段纳入已发生变更的撤销权限。新 session/apply 的 OEM eligibility 与原
资源的独立补偿不同；只有普通 finalizer 通过原基线核对才可释放，补偿本身不豁免失败。

最终 `pixi run just fmt`、`pixi run just lint`、`pixi run just docs-check-mermaid` 均通过；
Mermaid 检查 232 个 Markdown 中 27 个图。新增/更新的三份文档 UTF-8、fence、空白、冲突标记
及本地链接检查通过；`git --no-pager diff --check` 通过，既有三个文件的 CRLF→LF warning
未顺手改动。最终 active phone lease 清单为空。

## 一加的实际只读采集

本地证据：`.work/android-multi-model-profile-readonly-2026-11-10/`，run
`3c17bcaf-e367-4179-ab64-e126fa4d6414`。

fresh phone lease 下通过实际 `AndroidAutomationOwner._read_device_profile` 连续两次读取，
前后 boot 一致、两份 profile 相同；实际 manufacturer/brand 为 OnePlus、model 为 PHB110、
API 36、family 为 oplus。原 fingerprint 只留本地，摘要 SHA256 为：

```text
32201327c8c35dd366fd8e78274b2567dbece31c329eee6e2c96e2ca42e21a80
```

scope 仅为 `read-only-device-profile-parser-smoke`，没有 helper preparation、安装、
WebDriver/session 创建或 App 操作。未启用 HANS、修改属性/权限/radio/firmware。
`status=passed`、`phone_status=passed`、`phone_cleanup_errors=[]`，phone lease 释放；
共享 ADB 仍为原 PID，没有 kill/restart。不是完整 platform prepare 或 session admission 的设备 pass。

## 后续独立设备接线验证

用户随后确认没有 Galaxy，只需程序不绑定机型。新的独立 run
`be441256-e25f-4389-82ff-747960d71da2` 已在现有一加上完成新 preflight + Appium 4 + 临时
HANS 的完整 lifecycle、分类恢复及 cleanup pass，详见[后续设备报告](2026-11-10-android-preflight-hans-runtime.md)。
这是后续实际 session 证据，不将上面的只读 smoke 重标为完整 runtime，也不升级无 HANS
failure、其他机型或 RF。没有新增 Galaxy gate 或 OEM 空 adapter。

## 来源与维护边界

属性映射以 [官方 AOSP Build.java](https://github.com/aosp-mirror/platform_frameworks_base/blob/master/core/java/android/os/Build.java)
核对。Context7 对 Android SDK 的本次检索仅返回无关 Firebase 项目，没有把其文档当作 Android
平台依据；转而读取上述官方源代码。fingerprint 不作语义解析，与官方 Build 的 opaque 使用边界一致。

长期职责已更新到 [host tooling architecture](../../research/react-native/react-native-host-tooling-architecture.md)
及 [`tools/react_native/README.md`](../../../tools/react_native/README.md)。AGENTS 不追加机型名单、
run timeline 或未经验证的三星策略；本轮没有发布 upstream issue 或上传设备证据。
