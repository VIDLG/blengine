# LightBlue 手工测试外设

本页说明如何用 LightBlue 创建与 [`android-peripheral.json`](../../tests/conformance/scenarios/archive/android/android-peripheral.json) 对应的手工测试外设。该 JSON 是 Android/手工 fallback 的完整 UUID 和属性来源；QEMU/BLESS 使用独立的 `archive/bless/qemu-basic-gatt-peripheral.json` 广播身份，避免并行测试串扰。

该外设现在只作为[`tests/peripherals/android`](../../tests/peripherals/archive/android/)项目fixture不可用时的手工fallback，用于开发期扫描和基础GATT smoke test；日常确定notification验证改用[Android开发外设](android-peripheral.md)。它不替代平台backend conformance、受控BLESS managed peripheral或最终自动HIL。2026-08-29已用Windows Central与HONOR 90 GT上的LightBlue实际通过本页`ble-scan`和`test fixture`：广播、连接、GATT discovery、read、write with/without response和subscribe/unsubscribe均成功。2026-08-30又在CCCD订阅成功期间分别手工和通过ADB更新`FFF2...`；Windows经RF读取到新值，但没有收到Notify/Indicate callback，因此该LightBlue配置不作为notification delivery gate。当前工具走Bleak，只证明OS/RF/fixture可用；Windows 结果属于已归档的历史兼容证据，不是当前维护平台或生产 gate。

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
pixi run just test fixture
```

只检查广播和 GATT 结构、不执行数据操作：

```sh
pixi run just test fixture --structure-only
```

自动验证可以确认订阅请求成功。当前Android LightBlue实测中，编辑`FFF2...`会更新可读取的GATT value，但不会向已订阅的Windows Central发送Notify/Indicate callback；notification delivery应等待可编程managed peripheral验证，不能把本fixture的无callback记为BleHub失败或通过。

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
