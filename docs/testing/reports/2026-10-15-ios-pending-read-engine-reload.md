# iOS pending read 跨 RN engine reload 核验报告

日期：2026-10-15。

## 结论与范围

本次独立 RF 证据记录了：同一个已安装的 iOS App、同一进程内的两个不同 RN/JS runtime；旧 runtime 发出的 read 在 DK 端进入 `WAITING` 后触发真实 RN engine reload，旧模块的 `invalidate` 进入 native 关闭流程；旧 owner 完成 dispose、旧 handoff 关闭验证及 native 资源门禁后，新 owner/runtime 才获得初始化许可。新 runtime 重新扫描、连接、发现 GATT，并在第二次 DK read hold/release 后读回 `00`，随后完成 native close 和 fixture cleanup。

这不是另装一个新 App 来替代旧 runtime，也不是 full-suite 中在同一个 runtime 内顺序运行多个 case。它是独立的 pending-read engine-reload 场景。场景开始时安装一次 frozen App 并启动进程；reload 阶段通过 payload URL 请求 RN reload，不杀进程重启。

本次 RF、host/physical/ledger/bundle passed，App kept-open，offline `host-rf-evidence-check` 也已实际通过。离线只读复核没有重复运行设备；后续另用这份冻结 App 新跑了一次完整 full-suite，结果见下文，期间没有重建或刷机。离线校验通过表示证据完整性及绑定检查通过，不等于重新执行或重新授权 RF。

## 1. Bundle、App 与运行身份

证据目录：[`rf-pending-read-reload-01580b03446347e98c27ff909899dc62/`][evidence-dir]。

| 项目 | 实际值 |
|---|---|
| Bundle attempt | `rf-pending-read-reload-01580b03446347e98c27ff909899dc62` |
| Evidence bundle `run_id` | `bbf937b6-935d-43be-b755-9cda1a3abd91` |
| RF/native/JS run ID | `0fa8213c-efae-4e51-98b3-651c04bbe6cd` |
| Fixture session ID | `0fa8213c-efae-4e51-98b3-651c04bbe6cd` |
| App bundle ID | `org.reactjs.native.example.BleHubNitroHost` |
| iPhone UDID | `00008110-000C61160C39401E` |
| 官方 device identifier | `130256F7-7150-5A81-A906-6806AAFBFADA` |
| App PID，两代相同 | `50658` |
| Native host `bootId`，两代相同 | `45fd2121-0b0f-42dd-93c3-8516282621ee` |
| 旧 read native probe ID | `95637fe8-c2dd-479b-b628-6b9e0e1dbe4f` |

**Bundle `run_id` 与 RF/session run ID 是不同层级的身份，不得混写。** Native `bootId` 是本次 host 身份字段，不是经官方设备接口验证的 iOS 系统启动 ID。

[`evidence-bundle.json`][bundle] 记录 `status=passed`、`completed=true`、`primary_error=null`，`cleanup_errors=[]`、`evidence_errors=[]`，inventory 含 35 个证据文件。起止时间为 `2026-10-15T09:21:45.529594+00:00` 至 `2026-10-15T09:22:02.348422+00:00`。

[`device-install.json`][install] 的官方安装回执为成功，安装目录为：

```text
file:///private/var/containers/Bundle/Application/4519EEE5-1E95-4A66-AFC6-A9EBDB79BE73/BleHubNitroHost.app/
```

两代 native host 记录、官方 process 回执和三个控制 payload 回执均绑定 PID `50658` 及同一已安装 executable URI。三个 payload 均成功，且 `terminateExistingInstances=false`。代表性原件为 [`reload-payload.json`][reload-payload]、[`reload-before-processes.json`][reload-before]、[`reload-after-processes.json`][reload-after] 和 [`completion-processes.json`][completion]；本文不逐一列出全部 process 回执。

### 两个 owner 与两个 JS nonce

| Generation | Native owner ID | JS runtime nonce |
|---|---|---|
| 1 | `d29cc2f2-818d-49e6-a728-2367faabed23` | `mv9bxtjj-68pdr9nxku5-b98h6lwwyss` |
| 2 | `360c3f36-78df-4ac1-a63a-ce5ed5bfa8c6` | `mv9bxx8s-c4oxr4ariol-v6sdmtn8bfr` |

身份原件：[`pending-read-reload-runtime.identity.json`][identity]、[`pending-read-reload-runtime.native-identity.log`][native-log]、[`pending-read-reload-runtime.log`][runtime]。不同 owner、不同 nonce、同 PID/boot 和真实 invalidation 顺序共同支持“两 runtime、同 App 进程”，不能仅凭 JS generation 标签作此判断。

## 2. RN 真实 invalidate 与 native 门禁

当前 host 的 [`BleHubPendingReloadLifecycle.mm`][lifecycle-source] 实现 `RCTInvalidating`，在安装 JSI bindings 时生成并绑定 runtime owner；绑定对象必须是实际 Swift Nitro probe 的 NativeState，而不是任意 JS 对象。模块的 `invalidate` 由旧 RN TurboModuleManager 的销毁路径调用，再转入 `invalidateOwner`；控制 URL/reload 请求本身不直接冒充该 invalidation，也不在此回调中访问旧 JS/JSI。

[`HybridBleHubProbe.swift`][probe-source] 将旧 owner 的关闭来源、已接受的 read、已发出的 reload 请求和失效状态作为门禁条件。旧 runtime 的 JS `cleanup-started` marker 不是 native dispose 已完成的证明；新 runtime 的许可也不依赖旧 JS Promise 成功返回。

本次 [`pending-read-reload-runtime.log`][runtime] 的可见顺序如下，行号为该 normalized log 的 1-based 行号：

| 行号 | 事实 |
|---|---|
| 14–15 | 旧 `native:read-accepted`，随后旧 JS `read-dispatched` |
| 16 | 旧 owner `reload-url-accepted` |
| 17–19 | JS `cleanup-started` → `module-invalidation-entered` → `native:engine-invalidation-close-started` |
| 20 | `native:dispose-returned` |
| 21 | `native:late-submit-rejected:source=synthetic:result=closed` |
| 22–23 | `native:engine-close-complete` → 旧 owner `native-close-complete` |
| 24–25 | 新 owner `runtime-installed` → 新 JS `runtime-start`，nonce 已改变 |
| 28–30 | 新 owner `native-ready` → JS `native-ready:passed` → 新 `initialize:passed` |
| 38–42 | 新 read `value=00` → 新 owner `native-close-complete` → JS `close:passed` → `javascript-complete` → 场景 `passed` |

旧 native read/close 五个 boundary 均绑定同一 RF run、probe ID 和旧 read route：

```text
commandId=12011:connectionId=12004:gattSessionId=12008:characteristicId=13
```

### 资源零的准确口径

当前 native 实现先关闭 listener routes 和旧 sink，在 JS 调用栈及 probe state queue 之外执行同步 `BleHub.dispose()`；仍持有 backend 时采样 disposed backend 的逻辑资源，再等待已接受的 handoff handler 返回。发布 close-complete 前的门禁要求：共 17 项计数，`lifecycleOwners=1`，其余 16 项为零；随后注销 owner membership，并保存注销后的 closed snapshot。

16 项为 `activeScans`、`connections`、`gattAttributes`、`subscriptions`、`pendingIo`、`watchdogs`、`callbackRoutes`、`ingressRoots`、`adapterDelegates`、`peerCacheEntries`、`backendLeases`、`hubRoots`、`handoffRoots`、`handoffPending`、`listeners`、`workerThreads`。

本 bundle 保存的是门禁通过后的 native close markers，**没有逐项数值快照**。因此本次可表述为“native 逻辑资源零门禁通过，旧 membership 随后退休”，不能写成“离线独立复算了 17 项全零”。当前源码解释门禁含义，但 dirty-source 状态不构成源码与该二进制的构建证明。

该边界也不承诺系统蓝牙对象已物理销毁、内存全量回收，或所有已经交给 Nitro/JS 的任务均已排空。

## 3. DK 身份、WAITING 与两代 read

[`fixture-receipt.json`][receipt]、[`contract.json`][contract]、[`profile.json`][profile] 记录的 fixture 身份为：

| 项目 | 实际值 |
|---|---|
| Board | `nrf52840dk/nrf52840` |
| Resource identity | `jlink:1050252028` |
| 串口 | `/dev/cu.usbmodem0010502520281` |
| Fixture PID / PGID | `91380` / `91380` |
| Profile ID | `blehub-nrf52840-multifunction-v1-a` |
| Image ID / version | `nrf-multifunction-rf-v1-a` / `maintained` |
| Local name | `BleHubMultiRfDKA` |
| Service UUID | `fd500000-0000-4000-8000-000000000000` |
| Read characteristic UUID | `fff10000-0000-4000-8000-000000000000` |
| ATT handle | `41` |
| 目标权限 | `native=expected_native=67`，`profile=3` |
| Receipt proof method | `btp-vendor-live-under-device-lease` |
| Receipt UTC | `2026-10-15T09:21:47.820173+00:00` |

[`coordination/pending-reload-physical.json`][physical] 与 [`pending-read-reload-runtime.capture.json`][capture] 保存以下 monotonic 边界：

1. **旧代 sequence 37**：DK 首次 `WAITING` 为 `1293470.314392583`；host reload 请求为 `1293470.736835375`。Provider 观察到 native dispose 后，于 `1293471.003446166` 再次确认目标仍为 `WAITING`，然后请求 release。随后记录 GAP disconnect、`RELEASED` 及 advertising recovery。这使“reload 时存在真实未完成 read”不只依赖 JS dispatch marker。
2. **新代 sequence 38**：新 runtime 重新 scan/connect/discover，重新 arm；DK 再次进入 `WAITING`，随后 release。新 route 为 `commandId=13011:connectionId=13004:gattSessionId=13008:characteristicId=13`，JS 记录精确读回 `00`，再完成 close。
3. **最终 cleanup**：`advertising_stopped=true`、`hold_inactive=true`、`hold_state=RELEASED`、`no_known_peers=true`，关闭边界 `peer_count=0`，`cleanup_errors=[]`。Capture 最终为 `phase=closed`、`status=passed`。

两代 characteristic ID 都是 `13`，peripheral ID 都是 `0`，这些局部 ID 的复用不是隔离失败，也不是隔离证据。隔离依据是 owner/nonce、完整 command/connection/GATT-session route 及 native 关闭/就绪顺序。Ledger 汇总见 [`pending-reload-ledger.json`][ledger]。

## 4. Frozen candidate 与 hashes

安装快照为 [`candidate/BleHubNitroHost.app/`][candidate]。[`evidence-bundle.json`][bundle] 中 source fingerprint 与 frozen candidate fingerprint 完全相同。源产物路径记录为 `tests/consumers/react-native-internal/ios/build/device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app`。

| 对象 | SHA-256 |
|---|---|
| Frozen App tree | `376d204319fdda71ce1fff5c0bb54bb444cc94bf1415730ac2027b13b2b5925c` |
| `BleHubNitroHost` | `00050beaf1f214f93a5fa4dbe164604f77283b7de1572c288bfd97b590a61792` |
| `BleHubNitroHost.debug.dylib` | `6dc6dc2aacf6b5053778a1d37b7afc201300d008265b0126340b297fb8a13af5` |
| `main.jsbundle` | `1bf0a7343ebe6e2ee5a23c9e3b878aa00c46ac3127cba63fde61b6d8b5dd5af7` |
| Fixture image，contract/receipt 声明 | `dd7dea99a325db32fa1fd66648d0a0814c8c7c442a77cf08998f2b91a1cc5b7a` |
| Fixture contract | `3fe22ba286d36b6aa086c7ae92ecc7db27f07189536a539b1d5e03252a876750` |
| Fixture profile | `aa735baac72d19d9f1fd2dcd6f4e1868e4ac61080aa9ee7767faf074a2dd2ed8` |

这些 App hashes 标识主机提交安装的 frozen 内容，不是从 iPhone 回读的已安装二进制哈希。App tree 指纹保留内容及 mode，但排除 xattrs 和 timestamps，不代表完整文件系统等价。

## 5. 入口、无刷机与软件检查状态

正式新入口已添加，并通过 dry-run 检查：

```text
pixi run just react-native host-ios-device-pending-read-reload <udid> <team> <fixture-contract> <fixture-profile>
```

底层 [`tools/react_native/run_ios_pending_read_reload_gate.py`][runner] 接受显式 `--app`、`--fixture-contract`、`--fixture-profile` 和 device 参数，先冻结安装产物，再安装一次并通过持续读取的同一 console 与官方 payload URLs 驱动两个 runtime；底层不 build、flash 或 reset DK。Just 入口先构建签名 App，再调用该 runner。实际 RF 执行使用底层 runner，正式 recipe 本轮仅 dry-run，不把后续入口验证追溯为本 bundle 的证据。

本场景使用现有 multifunction firmware 及 live fixture receipt，不是 diagnostic HEX 刷写路径；hold/release、重新 advertising 和 cleanup 是运行期控制，不应描述为刷机。

### 本轮软件复核与同产物 RF 回归

| 检查 | 本轮实际结果 |
|---|---|
| `pixi run python -m unittest discover -s tools/react_native -p 'test_*.py' -q` | 391 passed |
| `pixi run python -m unittest discover -s tools -p 'test_*.py' -q` | 43 passed；此 discovery 不递归计入 RN 子目录，不冒充统一全量总数 |
| `pixi run python -m unittest discover -s tests/peripherals/nrf52840 -t . -q` | 278 passed |
| RN Jest `--runInBand` | 8 suites / 235 tests passed |
| `pixi run just react-native host-check` | TypeScript passed |
| 新 Just recipe | dry-run passed，不等于重新执行 build/RF |
| `pixi run just fmt` / `pixi run just lint` | passed，lint 含 strict workspace Clippy 与 provider profile isolation |
| `pixi run just docs-check-mermaid` | passed |
| Pending 与后续 full-suite bundle offline verify | 两份均 passed |

前一实现轮次已完成 selected iOS native 12 项、Swift handoff 2 项、Simulator build-for-testing、unsigned generic iOS build；本轮未重跑这些 native/build 门禁，不能与上述本轮检查混写。一次错误的 tools discovery 加了 `-t .`，因 tools 不是可导入 package 而在加载前失败；改用表中的正确命令后通过，未改代码绕过测试。

随后从本 pending attempt 的 `candidate/BleHubNitroHost.app` 再冻结/安装并真实执行 [full-suite 回归 `b108c187335e4d72b6854bff8a42cebf`](2026-10-15-rn-full-suite.md#新版-ios-reload-同一冻结产物的完整回归)：App tree、JS、主 Mach-O 和 debug dylib 指纹完全一致，未重建。基础十项、七组控制 ACK、physical-clean/canonical readback、恢复十轮、50 个快照及十个 closed snapshot 的 17 项全零均 passed，host/physical/bundle 与 offline verify passed、App kept-open。该回归是新的独立 App 启动/PID，不把两场拼成一个 runtime；它补齐该 iOS frozen artifact 的两类 RF 场景，但不等于所有 native/package/Android 同候选门禁完成。

## 6. 语义限制与证据边界

- **Synthetic 不等于真实 late callback**：`late-submit-rejected:source=synthetic:result=closed` 验证保存的旧 handoff 拒绝 synthetic old-route 提交，不声称实际捕获到 dispose 后的 late CoreBluetooth callback，也不证明 radio 侧 ATT 事务被 native dispose 同步取消。
- **Native close 不等于 JS 全量排空**：门禁覆盖本 probe 的 native lifecycle、dispose 和已接受 handoff handler 的关闭边界；不能把 Promise、close marker 或逻辑资源计数扩大为所有 Nitro/JS 工作完成。
- **新代事件审计有限**：旧 route/error 防护和 post-read silence 由 JS guard 检查；本目录未逐条保存 native event body，不能独立重放审计所有新代事件。
- **物理证据是合作式记录**：DK 结论来自 provider 边界和设备 lease 下的 live receipt，不是独立 BLE/BTP replay、无线抓包或签名 attestation。Monotonic 记录支持已记录边界的先后；未记录的中间采样及阶段起点仍由 provider 负责。
- **Same PID/boot 的范围**：官方 process/payload 回执支持本次 reload 没有替换 App 进程；native boot/owner 记录加强跨前缀绑定，但不是系统启动证明或安装二进制哈希证明。
- **Dirty-source 边界**：bundle 记录 HEAD 为 `75edda34b20b043ef282d7d94614c9a1169c6179`、`dirty=true`、`source_to_build_attestation=false`。不能声称该 HEAD 单独重建出此 App，不能将当前源码解释视作已签名的 source-to-binary 对应关系。
- **不外推到 full-suite 或其他平台**：本报告只覆盖此独立 iOS two-runtime pending-read 场景；不替代 full-suite 同 runtime 运行、Android RF、全部生命周期故障或生产 ABI 冻结门禁。

[evidence-dir]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/
[bundle]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/evidence-bundle.json
[install]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/device-install.json
[identity]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.identity.json
[native-log]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.native-identity.log
[runtime]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.log
[reload-payload]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.reload-payload.json
[reload-before]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.reload-before-processes.json
[reload-after]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.reload-after-processes.json
[completion]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.completion-processes.json
[receipt]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/coordination/fixture-session/fixture-receipt.json
[contract]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/coordination/fixture-session/contract.json
[profile]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/coordination/fixture-session/profile.json
[physical]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/coordination/pending-reload-physical.json
[capture]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-read-reload-runtime.capture.json
[ledger]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/pending-reload-ledger.json
[candidate]: ../../../tests/consumers/react-native-internal/ios/build/rf-pending-read-reload-01580b03446347e98c27ff909899dc62/candidate/BleHubNitroHost.app/
[lifecycle-source]: ../../../tests/consumers/react-native-internal/ios/BleHubNitroHost/BleHubPendingReloadLifecycle.mm
[probe-source]: ../../../integrations/react-native-internal/ios/HybridBleHubProbe.swift
[runner]: ../../../tools/react_native/run_ios_pending_read_reload_gate.py
