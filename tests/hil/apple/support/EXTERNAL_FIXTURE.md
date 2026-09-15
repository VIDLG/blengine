# Apple HIL：外部 Peripheral

外部入口只运行 BleHub Central，不启动、停止、reset 或控制 Peripheral，不调用 Android recipes，也不负责 BTP/串口编排。生产 backend 不变。各运行必须由外部 host 先准备 GATT、reset 初值、启动可连接广播；Mac 与 iPhone 按顺序运行，不争用同一个 Peripheral。

## Just 入口

在 BleHub 根目录运行，`profile` 是调用者提供的 JSON 路径，可在仓库外，也可含空格。仓库不依赖任何兄弟项目路径。

```sh
pixi run just apple rf-macos-external /absolute/path/to/profile.json io 10 ab
pixi run just apple rf-ios-external /absolute/path/to/profile.json 10 ab
```

- macOS 参数顺序：`profile scenario="io" write_with_response="102030" write_without_response="abcd"`。支持 `scan`、`connection`、`gatt`、`io`、`rssi`；没有隐式 `all` 或 provider 恢复动作。
- iPhone 参数顺序：`profile write_with_response="102030" write_without_response="abcd"`；仅支持完整 `io`，含 scan、connect、GATT discovery、五次 IO command、disconnect、完整 ledger 和 dispose 后静默。
- `10 ab` 用于只能保持单字节写 buffer 的 fixture。默认仍是 Android 原有 `102030 abcd`；不是根据厂商或 profile 名猜测 payload。Android/BLESS profile 已归档，不再作为 active external RF 输入。
- IO 每轮前 reset read-write 特征到 profile 初值。运行顺序：精确初值读取 → with-response 写/精确读回 → without-response 本地提交 → 等待 0.5 秒 → 精确读回。no-response terminal 不等于远端确认。
- 不提供 Notification/Indication/被动断连/Recovery 的外部 host 编排；选择这些场景会拒绝，不会回退到 Android。

底层入口分别为 `python -m tools.apple.test_macos_runtime rf-external <scenario>` 和 `python -m tools.apple.test_ios_runtime runtime-rf-external io`，接受 `--profile`、`--write-with-response`、`--write-without-response`。通过 `pixi run -e apple-ci` 调用；直接 Python 入口要求 production package 已 staged，Just 自动准备。

## 支持的 profile 子集

使用 active [`blehub-nrf-indication-v2.json`](../../../conformance/scenarios/blehub-nrf-indication-v2.json) 的结构，但不要求 Android provider、名称或服务 UUID：

- `schema_version=1`；`profile_id` 为 1–128 个字母、数字、`_`、`-`。
- `advertising.service_uuids` 恰好一个合法 UUID；`services` 中恰好一个同 UUID 的 `primary: true` 服务。
- 目标服务恰好包含下表前四个角色，可选第五个；不允许重复 UUID、角色、额外特征或属性。其它服务不影响目标服务校验。
- 所有特征使用完整 UUID `fffN0000-0000-4000-8000-000000000000`，不是 SIG 16-bit UUID 的展开。

| role | N | properties（精确集合） |
|---|---:|---|
| `read-write` | 1 | `read`, `write`, `write_without_response` |
| `updates` | 2 | `read`, `notify`, `indicate` |
| `read-only` | 3 | `read` |
| `write-only` | 4 | `write`, `write_without_response` |
| `indication-updates`（可选） | 5 | `read`, `indicate` |

`read-write.initial_value_hex` 必填。初值和两个写入值均为 1–512 字节、无分隔符 hex，三者必须不同，以防静态/旧值读回冒充写入成功。其它初值字段仍由 Peripheral host 实现，此 IO scenario 不读取它们。

GATT discovery 要求 `updates` 的 CCCD；profile 存在 `indication-updates` 时，也严格要求其特征属性和 CCCD。CCCD UUID 是 `00002902-0000-1000-8000-00805f9b34fb`。profile 缺少第五特征只改变该次显式配置，不放松默认 Android 的五特征断言。

`local_name` 不用于筛选。必须实际广播所选服务 UUID，并确保没有另一 fixture 使用同一服务 UUID，避免混淆来源。iPhone 外部入口拒绝 `BLEHUB_APPLE_RF_UNFILTERED=1` 和 `BLEHUB_APPLE_RF_TARGET_IDENTIFIER` 诊断覆盖。

## Apple 前置条件与结果

- macOS：解锁的图形会话，Bluetooth PoweredOn；可见 probe App 获得 TCC Bluetooth 授权。重新 ad-hoc 签名后可能需要再次授权。
- iPhone：已配对、CoreDevice tunnel 连接、Developer Mode 开启、有效开发签名与 provisioning；App 保持前台、手机解锁且允许 Bluetooth。多个设备时设置 `BLEHUB_APPLE_DEVICE`。
- 更新 `project.yml` 后，旧签名工程不会自动覆盖。按工具提示显式运行 `pixi run just apple ios-project-regenerate`；必要时 `ios-signing` / `ios-project-confirm`。keychain human gate 仍只由人手执行 `pixi run just apple unlock-login-keychain`，普通运行不自动解锁。
- macOS 结果：`.work/apple-macos-runtime/<scan|connect|gatt|io|rssi>-external-<run-id>-result.txt`，启动前打印完整路径。
- iPhone 结果：设备 `Documents/blehub-rf-<run-id>/result.txt`；host 拉回并保存 `.work/apple-ios-runtime/<run-id>-result.txt`，保留兼容 `.work/apple-ios-runtime/blehub-result.txt`。
- 外部成功结果必须包含匹配的 `apple_rf_profile_id`、`apple_rf_service_uuid`、`apple_rf_indication_only_required` 和三个 IO payload 配置行，以及目标场景 pass。iPhone 另要求同 run ID 的 `io`、ledger、post-dispose silence、overall RF pass；初始化成功不算 RF 通过。

## 不运行硬件的验证

```sh
pixi run just apple test-rf-host
pixi run -e apple-ci python tests/hil/apple/support/test_rf_scenario.py
pixi run just apple build-macos-hil
pixi run just apple build-ios-hil
```

Swift 测试消费已有 production package。`build-macos-hil` 仅构建/ad-hoc 签名，不启动 App；`build-ios-hil` 在 `.work/apple-ios-runtime/unsigned-project` 生成独立工程并做 generic iOS unsigned build，不安装、不启动、不访问 keychain，也不覆盖手工签名工程。这些结果都不是 RF 或真机签名证据。
