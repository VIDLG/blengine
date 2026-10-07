# Archived React Native macOS feasibility host

This independent `react-native-macos@0.81.9` / `react-native@0.81.6` host is retained for historical inspection only. It is not an active build, UI, RF, or lifecycle gate. The active RN internal host remains at [`../../react-native-internal/`](../../react-native-internal/), with iOS and Android as its mobile targets.

The 2026-09-25 signed macOS Release XCUITest passed a real RN UI accessibility assertion for the visible title, production BleHub completion, and explicit close; the Release app also passed a single-UniFFI-image symbol check. Earlier native runtime/lifecycle results apply only to their tested candidate. None of these results proves iPhone or Android RN behavior, BLE RF, or the public BLeng UI surface. See [`docs/research/react-native-internal-integration-review.md`](../../../../docs/research/react-native-internal-integration-review.md).

This archive keeps hand-authored host sources and its lockfile, not local `node_modules`, Pods, generated workspaces, signing state or build output. Active Nitro/CocoaPods metadata advertises iOS only; restoring RN macOS requires an explicit scope review and revalidation against a compatible module/platform candidate. The production macOS CoreBluetooth backend and native Apple tests remain maintained independently.
