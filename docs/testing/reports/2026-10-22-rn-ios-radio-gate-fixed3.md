# RN iOS `IOS-RADIO-GATE` fixed3 candidate report

日期：2026-10-22

## 结论

fixed3 signed RN candidate 完成了一次真实 iOS startup-off → ON RF gate。该结果是本次 candidate、iPhone、DK-B、firmware contract/profile 和 host session 的限定证据，不是最终 public ABI、Phase exit、active-loss/same-Hub recovery 或长期压力通过。

## Candidate / device

- App：`tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed3-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app`
- XCTest run：`tests/consumers/react-native-internal/ios/build/device-ui-startup-fixed3-derived/Build/Products/BleHubNitroHost_iphoneos27.1-arm64.xctestrun`
- iPhone UDID：`00008110-000C61160C39401E`
- iOS：`26.6.1`
- DK-B BTP：`/dev/cu.usbmodem0010502514791`
- physical identity：`jlink:1050251479`
- service：`fd700000-0000-4000-8000-000000000000`
- contract：`tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-b.json`
- profile：`tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-b.json`
- run ID：`fff45a36-b076-4c67-addf-737e4c2653b9`
- evidence bundle：`tests/consumers/react-native-internal/ios/build/ios-adapter-startup-f9f25c8469214f14b7a3a22786cbbda9/evidence-bundle.json`

## Passed boundaries

- XCTest handshake and native/App identity。
- Settings `ON → OFF → ON`，同一 native App PID/boot。
- OFF 状态下 BLE command 被拒绝为 `adapter_unavailable`。
- OFF journal 的 17 项 native resource 全部为零。
- ON 后 fresh Probe 进入 RN workbench。
- Service UUID advertisement 正确，scan stop 后静默观察 `2000 ms`。
- Connect / Connected / GATT discovery / Disconnect。
- GATT inventory：2 services、5 characteristics、2 descriptors。
- Initial read：`00`。
- Response write/read：`10`。
- Write-without-response/read：`ab`。
- Disconnect 后 connection silence `1000 ms`。
- Probe close、listener removal、第二次 17 项 resource-zero。
- DK-B physical：1 connection、1 disconnection、advertising stopped、hold inactive、peer count 0。
- physical cleanup errors：0。
- final ON evidence 已收集。

XCTest summary 为 `passedTests=1`、`failedTests=0`、`skippedTests=0`。Evidence bundle 离线 verifier 重新执行通过。

## 事件流修复

上一轮 fixed2 失败的根因是同一个 scan session 中的重复 advertisement 留在事件队列，connect 阶段被错误消费为第一条 connection event。fixed3 在连接前使用 `consumeAll()` 消费剩余合法 advertisement，并有回归测试覆盖该情况。

本次首次 fixed3 RF 尝试还暴露了证据校验器对 iOS App container UUID 的错误严格相等判断：安装 receipt 与 runtime executable 的 `/Bundle/Application/<UUID>/` 不同，但 App/executable 相同。证据层现在统一复用 `ios_executables_match()`，只允许合法 container UUID 变化，仍拒绝不同 App/executable 和 PID 替换。

## 失败尝试边界

- fixed2 run `bcec9f9d-2acb-43a4-ab05-b5aff5f0ff3a`：事件队列 duplicate advertisement bug，保留原 failed verdict。
- fixed3 首次 run：证据校验器 container UUID drift，RF 已到达运行阶段但最终 bundle 被正确拒绝；保留原 failed bundle。
- fixed3 后续 run：XCTest `Timed out while enabling automation mode`，重启 iPhone 后 automation session 清除；该次 `fixture_started=false`、`probe_started=false`、`radio_mutated=false`。

这些失败没有被改写为通过，也没有用 synthetic/Jest 结果替代真实 RF 证据。

## 仍未证明

- iOS active-loss / held-read radio loss。
- 同一个已存在 Hub/Probe 的 same-Hub recovery。
- 最终 public ABI/private RN contract freeze。
- 10,000 lifecycle、long-running stress 和 Android API-tier coverage。
- 持续结果页可见性。
- Linux/BabbleSim scheduling、resource-failure 和非法协议交错覆盖。

下一步应按计划继续独立的 Android candidate/remaining ABI exit，不把本次 candidate pass 泛化为全阶段完成。
