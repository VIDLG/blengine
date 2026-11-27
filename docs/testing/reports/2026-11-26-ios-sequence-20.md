# iOS 受管场景 20/20：一次运行、全部离线校验通过

日期：2026-11-26
Lane：`pixi run -e apple-ci python -m tools.react_native.run_bleng_ios_scenes`
设备：iPhone 14 Plus（`00008110-000C61160C39401E`，iOS 27.2.1）
Peripheral：nRF52840 DK-A（`jlink:1050252028`，`multifunction-rf-v2-a`）
产物：App 树 `7e56a50df324999e0cc64989ec69682a55dd7dd9b5dec1e223259f3f7630df8a`，
xctestrun `4bca358da3b431f54a388398eefe4902496789e225447530355321c7234ff3c9`，
App id `FWVYQ287W5.bleng-rn-host`
证据根：`.work/ios-native-sequence/2026-11-26-a`

本文记录候选事实，不宣布 ABI 冻结，也不把 `host_gate=pending` 当成通过。它补上的是
`abi-freeze-readiness.md` 里长期写着的「iOS 没有 20 条序列」。

## 结果

`scene_sequence=passed`，20 条场景全部 `passed`，顺序就是共享的 `scene_sequence.ORDER`：

`scan → notification → indication → background → io → link-facts → disconnect → descriptor-io →
devices-query → devices-kinds → adapter-control → connect-failure → auto-connect → cancellation →
write-reject → ccc-enable-reject → ccc-disable-reject → passive-disconnect → recovery → recovery-ten`

整个序列 2 843.9 s（47.4 min，18:35:41 → 19:23:05）。20 份 bundle 逐个用
`pixi run python -m tools.react_native.bleng_scan_evidence <attempt>` 离线重放，全部
`status=passed`、`integrity_status=passed`、`verification=offline_integrity`、
`cleanup_errors=[]`、`phone_cleanup_errors=[]`。

fixture 每次运行都留下同一套收尾证据：`advertising_inactive=true`、`resources_closed=true`，
证明方式 `btp-gap-controller-info-advertising-bit-clear`，DK-A 的 boot nonce 全程未变
（`91a0f16bffc1d12fe30e5fee2e3ab9a4`），没有烧写、没有复位、没有换镜像。

| 场景 | 步数 | 场景耗时 (s) | 结果 |
|---|---|---|---|
| `scan` | 24 | 17.6 | passed |
| `notification` | 43 | 24.4 | passed |
| `indication` | 43 | 24.4 | passed |
| `background` | 51 | 27.0 | passed |
| `io` | 45 | 33.4 | passed |
| `link-facts` | 42 | **1407.4** | passed |
| `disconnect` | 57 | 25.6 | passed |
| `descriptor-io` | 50 | 34.3 | passed |
| `devices-query` | 30 | 20.6 | passed |
| `devices-kinds` | 24 | 13.4 | passed |
| `adapter-control` | 24 | 13.4 | passed |
| `connect-failure` | 39 | 20.1 | passed |
| `auto-connect` | 36 | 20.2 | passed |
| `cancellation` | 59 | 27.1 | passed |
| `write-reject` | 59 | 29.1 | passed |
| `ccc-enable-reject` | 55 | 27.2 | passed |
| `ccc-disable-reject` | 59 | 27.1 | passed |
| `passive-disconnect` | 50 | 24.8 | passed |
| `recovery` | 94 | 24.7 | passed |
| `recovery-ten` | 276 | 25.0 | passed |

## 一个未解释的慢：`link-facts` 的每次按压约 121 s

这是本轮唯一的异常，且**没有根因**，所以按候选事实记录，不当作已经解释过的现象。

同一条 `link-facts` 计划、同一份 App 树，当天单独运行是 42 步 **25.7 s**
（`.work/ios-native-link-facts/2026-11-26-a/97d073e8-…`）；在序列里同样的 42 步花了
**1407.4 s**。逐步骤时间（驱动 receipt 自带 `seconds`）指向 10 次按压：

```text
242.7s  tapUntil next-scan-event
121.0s  tap start-scan
121.0s  tap stop-scan
121.0s  tap connect-peripheral
121.0s  tap mtu-query
121.0s  tap mtu-request
120.9s  tap mtu-query
120.9s  tap priority-high
120.9s  tap refresh-gatt
120.8s  tap close-session
 63.5s  setText scan-service-filter   (其中 focus=0.96 s)
```

每一次都**成功**，元素在 `observed` 里是 `exists label=…`，所以不是找不到控件，也不是不
hittable 超时（那种情况会抛 `present but never became hittable`）。同一次运行里
`setText` 的聚焦按压只要 0.96 s，`tapUntil` 又恰好是 2 × 121 s。

121 s 这个数字很可疑：XCTest 在派发事件前会等 App 报 idle，超时预算按 60 s 计，一次事件
付两轮就是 ~120 s。也就是说最可能的解释是「这段时间 App 从未报 idle」，但为什么只发生在
这一次、这一段，没有证据能确认。已经排除的是：不是计划差异（同计划单独跑 25.7 s）、不是
控件不可点（步骤成功）、不是设备断开（后续 8 条场景继续 13–34 s 完成）。

驱动为此加了一个**只测量、不改变行为**的钩子：`tap` 现在把
`hittable=X.XX press=Y.YY` 写进该步的 `observed`（`setText` 记为 `focusTap=`）。下一次
出现同样现象时，receipt 会直接说明秒数花在「等 hittable」还是「等 idle 后派发」，而不是
只能从总时长反推。`tap` 的判定、超时与失败文案都没变。

这个钩子已上机验证：重新 `formal-host-ios-device-test-build`（新 App 树
`634f057536538995e86fed668d1bb969f745be69da76947c037b5bd95b3c8455`）后单独跑 `scan`
（`.work/ios-native-scan/2026-11-26-c/6a42feb8-b1e2-4748-ac37-e54c167911df`），6 次按压全部
`hittable≈0.1s press≈0.5s`，场景 17.4 s 通过。也就是说正常情况两半都很快，120 s 只会在某种
「App 不报 idle」的状态里出现——这正是下一次要抓的东西。

## 其他观察

- `recovery-ten` 的 276 步里绝大多数是读取，不是按压，所以它只有 25.0 s——这和
  `link-facts` 的 121 s/按压形成对照，进一步说明慢的不是「场景大」。
- `background` 在这条序列里一次通过（27.0 s），与 Android 侧 20/20 序列里的表现一致。
- 每条场景都 `fresh_install=true`，前一条保留下来的进程由 `--release-retained-app` +
  `--release-before-install` 在**下一条**安装前释放；`cleanup_errors=[]` 是这一步生效的证据。
- `host_gate_status=pending`：iOS 场景 lane 不跑 host 侧门禁，这一列不因为 20/20 而升级。

## 第二轮：新 App 树上的 19/20，与真正的根因（屏幕休眠）

驱动加了 `hittable=`/`press=` 之后重新构建（`test-without-building` 用同一份 xctestrun，App 树
`634f0575…`），用新树重跑整条序列（`.work/ios-native-sequence/2026-11-26-b`）：
**19/20 通过**，`link-facts` 这次只要 **25.3 s**（上一轮 1407 s），证明那个慢不是计划或 App 的问题。

失败的只有 `recovery-ten` 的最后一条 host 步：`announce:9` 的 `activate` 报
`state=4, switcher=card-present`。随后连 `scan` 也开始失败，症状变成
`setText` 在 30 s 内一直读到 `value=`（键盘报 `present`，但字符一个也没落到输入框）。

排查过程与结论：

- 同一份应用的 `main.jsbundle` 在三轮构建里完全相同（`89d3e861…`，3 348 188 字节），所以不是
  JS/App 代码回归；
- 设备 syslog 显示每个进程都记 `hasHardwareKeyboard=1`，但后来一次 *通过* 的运行里同样有，
  所以它不是判据；
- 决定性证据是设备截图：`xcrun devicectl device capture screenshot` 得到的整幅图
  **平均亮度 0、最大值 0**，也就是说 **iPhone 屏幕是熄灭的**。`devicectl ... lockState` 同时
  仍然报 `passcodeRequired=false`、`unlockedSinceBoot=true`，这正是这个坑难认的原因：
  “未锁屏”不等于“屏幕亮着”。屏幕熄灭时 XCTest 会照常下发事件、照常报元素存在，什么都不会
  发生，于是变成 `activate` 永不可点、按键无处可去、以及 XCTest 等 idle 的长时间停顿。

### 因此加了一道真正的门：`phone.display`

`tools/react_native/ios_display.py` 把「屏幕是否亮着」变成一个可测事实：截一张图，`sips` 缩到
8×8 BMP，只读平均亮度。黑屏（亮度 0）与任何亮着的内容（默认阈值 1.0）分得很开，不需要调阈值，
也不做 OCR 或内容比较。

`run_ios_scan` 在 **lease 之前**就有 `phone.display` 阶段，结果写进 `result.json` 的
`phone_display`；屏幕熄灭时直接拒绝并说出唯一有效的修法：

```text
the iPhone display is off (device screenshot is entirely black: luminance 0.0);
wake the phone and set Settings > Display & Brightness > Auto-Lock to Never —
a UI scene cannot interact with a sleeping screen
```

这条门在当下的真机上当场验证过：它拒绝的正是这台熄屏的 iPhone（亮度 0.0）。单元测试
`tools/tests/react_native/test_ios_display.py` 用合成 BMP 覆盖黑屏/亮屏/不可读样本/命令失败/非法
预算五种情况。

**这 19/20 不是 App 的失败**，也不再是一个未解释的异常：屏幕亮着时同一份 App 树在同一晚跑过
25 s 级别的场景。完整闭环需要等手机唤醒并把自动锁定设为「永不」后重跑整条序列。

## 离线验证

| 命令 | 结果 |
|---|---|
| `pixi run python -m tools.react_native.bleng_scan_evidence <attempt>`（20 次） | 全部 `integrity_status=passed` |
| `pixi run python -m pytest tools/tests -q` | `1083 passed, 21 skipped, 3002 subtests passed`（47.5 s） |
| `pixi run just --fmt --check` | 通过 |
| `pixi run just docs-check-mermaid` | `Checked 28 Mermaid diagrams in 302 Markdown files` |
| `pixi run just binding snapshot-check` / `bleng-snapshot-check` | `verified 3` / `verified 6 reviewed API snapshots` |

本轮只改了 iOS **UITest 驱动**与文档，产品 crate/ABI 未动，所以九份快照不变。注意：驱动改动后
重新构建得到的是另一份 App 树（`634f0575…`），上面这 20 条序列绑定的是构建时的 `7e56a50d…`；
下一次全量重跑应使用重新构建的树与新的 xctestrun。
