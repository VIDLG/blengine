# LightBlue 手工测试外设

本页说明如何用 LightBlue 创建与 [`basic-gatt-peripheral.json`](../../tests/conformance/scenarios/basic-gatt-peripheral.json) 对应的手工测试外设。JSON 场景是测试 UUID 和属性的项目级来源；本页只描述 LightBlue 中的操作映射。

该外设用于开发期扫描和基础GATT smoke test，不替代平台backend conformance、受控managed peripheral或最终自动HIL。2026-08-29已用Windows Central与HONOR 90 GT上的LightBlue实际通过本页`ble-scan`和`ble-fixture-check`：广播、连接、GATT discovery、read、write with/without response和subscribe/unsubscribe均成功；notification数据送达仍需手工更新值。当前工具走Bleak，只证明OS/RF/fixture可用，不证明尚未实现的BleHub `WindowsBackend`。

## 广播配置

创建名为 `BleHubTest` 的 Virtual Peripheral，并添加一个 Primary Service：

```text
FFF00000-0000-4000-8000-000000000000
```

启用 `Advertise This Service`。LightBlue 的 Service UUID 输入框要求完整 UUID，不能只输入 `FFF0`。

128-bit Service UUID 会占用较多 legacy advertising 空间，平台可能省略、截断 local name，或像当前 Android LightBlue 一样广播系统设备名。Fixture 通过 `require_local_name: false` 明确把名称差异作为 warning；测试发现和通过条件以 Service UUID 为准。

## Characteristic 配置

在该 Service 下添加以下 Characteristic：

| 角色 | UUID | Properties | 初始值（hex） |
|---|---|---|---|
| 通用读写 | `FFF10000-0000-4000-8000-000000000000` | Read, Write, Write Without Response | `00` |
| 数据更新 | `FFF20000-0000-4000-8000-000000000000` | Read, Notify, Indicate | `00` |
| 只读信息 | `FFF30000-0000-4000-8000-000000000000` | Read | `426C65487562`（`BleHub`） |
| 只写控制 | `FFF40000-0000-4000-8000-000000000000` | Write, Write Without Response | 无 |

如果 LightBlue 自动为 Notify/Indicate Characteristic 创建 CCCD，则保留其默认设置，不要再手工复制 `2902` 描述符。

## 启动与扫描

保存配置后，先停止旧的 Advertising，再重新启动。使用另一台物理 Central 扫描：

```sh
pixi run just ble-scan --service-uuid FFF00000-0000-4000-8000-000000000000 --timeout 20 --require-match
```

完整验证默认读取项目 fixture，检查广播、连接、GATT 结构、properties、初始读取、两种写入及订阅/取消订阅：

```sh
pixi run just ble-fixture-check
```

只检查广播和 GATT 结构、不执行数据操作：

```sh
pixi run just ble-fixture-check --structure-only
```

自动验证可以确认订阅请求成功，但 LightBlue 仍需人工更新 `FFF2...` 的值才能证明通知数据实际送达。

如果扫描不到：

1. 确认 `Advertise This Service` 已启用；
2. 确认保存后重新启动过 Advertising；
3. 暂停广播并做一次基线扫描，再开启广播比较新增设备；
4. 不要以 advertised name 必须等于 `BleHubTest` 作为成功条件。

## 基础 smoke 场景

发现并连接后，依次验证：

1. 枚举出 `FFF0...` Service 和四个 Characteristic；
2. 从 `FFF1...` 读取 `00`，分别执行 write with response 和 write without response；
3. 订阅 `FFF2...`，在 LightBlue 中更新值并观察 Notify/Indicate，再取消订阅；
4. 从 `FFF3...` 读到字节 `42 6C 65 48 75 62`；
5. 尝试读取 `FFF4...`，确认 backend 把权限失败映射为约定的错误；
6. 主动断开并重新连接，确认旧 connection/GATT/subscription handle 不被复用为新 session。
