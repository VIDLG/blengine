# ABI freeze inventory（人可读版）

> 本文件由 `pixi run python -m tools.freeze_inventory` 生成，并由
> `tools/tests/test_freeze_inventory.py` 断言与 `api-snapshots/` 一致。**不要手改。**

它是冻结**清单**，不是冻结公告：ABI 未冻结，Phase 退出条件不由本文改变。九份 reviewed 快照是
机械替身（任何漂移让 `snapshot-check` 失败），本文是给人签名看的同一份形状 + 快照里没有的两块
（JS/Nitro 面与失败 `code` 空间）。决定本身见
[`abi-freeze-review-packet.md`](abi-freeze-review-packet.md)。

## 1. Rust Stable UniFFI 形状

| component | rust | kotlin | swift |
|---|---|---|---|
| `blehub`（BleHub canonical core） | 77 个声明 | 27 个声明 | 77 个声明 |
| `bleng`（BLeng async runtime） | 29 个声明 | 47 个声明 | 48 个声明 |
| `supervisor`（BLeng supervisor (FFI owner)） | 112 个声明 | 75 个声明 | 140 个声明 |

### `blehub` 逐符号

- **rust**（77）：`AdapterSnapshot`、`AdapterState`、`AdvertisementObservation`、`AndroidScanMode`、`AndroidScanOptions`、`AndroidScanPhy`、`AssociateCompanionDeviceCommand`、`AuthorizationState`、`BackendAssembly`、`BackendCapabilities`、`BackendEventIngressHandle`、`BleBackend`、`BleErrorCategory`、`BleEvent`、`BleEventError`、`BleEventSink`、`BleHub`、`BleHubError`、`BluetoothUuid`、`BondState`、`CancelOperationRequest`、`CharacteristicProperties`、`CommandDispatchError`、`CommandId`、`CommandKind`、`ConnectCommand`、`ConnectionId`、`ConnectionPriority`、`ConnectionState`、`ConnectionStateResult`、`CreateBondCommand`、`DeviceQueryKind`、`DeviceRecord`、`DisconnectCommand`、`DiscoverGattCommand`、`EventIngressStatus`、`EventSinkError`、`GattCharacteristic`、`GattCharacteristicId`、`GattDatabase`、`GattDescriptor`、`GattDescriptorId`、`GattService`、`GattServiceId`、`GattSessionId`、`InitializeCommand`、`ManufacturerData`、`ManufacturerDataFilter`、`MtuCommand`、`MtuInfo`、`NotificationAction`、`NotificationMode`、`NotificationModeRequest`、`NotificationState`、`PeripheralId`、`Platform`、`PlatformErrorDetails`、`QueryDevicesCommand`、`ReadCharacteristicCommand`、`ReadDescriptorCommand`、`ReadRssiCommand`、`RefreshGattCacheCommand`、`RemoveBondCommand`、`ScanOptions`、`ScanSessionId`、`ScanStopReason`、`ScanStopResult`、`ServiceData`、`SetAdapterEnabledCommand`、`SetConnectionPriorityCommand`、`SetNotificationsCommand`、`StartScanCommand`、`StopScanCommand`、`SubscriptionId`、`WriteCharacteristicCommand`、`WriteDescriptorCommand`、`WriteType`
- **kotlin**（27）：`AdapterState`、`AndroidScanMode`、`AndroidScanPhy`、`AuthorizationState`、`BackendEventIngressHandle`、`BackendEventIngressHandleInterface`、`BleBackend`、`BleErrorCategory`、`BleEvent`、`BleHubException`、`BondState`、`CommandDispatchException`、`CommandKind`、`ConnectionPriority`、`ConnectionState`、`ConnectionStateResult`、`DeviceQueryKind`、`EventIngressStatus`、`EventSinkException`、`NotificationAction`、`NotificationMode`、`NotificationModeRequest`、`NotificationState`、`Platform`、`ScanStopReason`、`ScanStopResult`、`WriteType`
- **swift**（77）：`AdapterSnapshot`、`AdapterState`、`AdvertisementObservation`、`AndroidScanMode`、`AndroidScanOptions`、`AndroidScanPhy`、`AssociateCompanionDeviceCommand`、`AuthorizationState`、`BackendCapabilities`、`BackendEventIngressHandle`、`BackendEventIngressHandleProtocol`、`BleBackend`、`BleErrorCategory`、`BleEvent`、`BleEventError`、`BleEventSink`、`BleHubError`、`BleHubProtocol`、`BluetoothUuid`、`BondState`、`CancelOperationRequest`、`CharacteristicProperties`、`CommandDispatchError`、`CommandId`、`CommandKind`、`ConnectCommand`、`ConnectionId`、`ConnectionPriority`、`ConnectionState`、`ConnectionStateResult`、`CreateBondCommand`、`DeviceQueryKind`、`DeviceRecord`、`DisconnectCommand`、`DiscoverGattCommand`、`EventIngressStatus`、`EventSinkError`、`GattCharacteristic`、`GattCharacteristicId`、`GattDatabase`、`GattDescriptor`、`GattDescriptorId`、`GattService`、`GattServiceId`、`GattSessionId`、`InitializeCommand`、`ManufacturerData`、`ManufacturerDataFilter`、`MtuCommand`、`MtuInfo`、`NotificationAction`、`NotificationMode`、`NotificationModeRequest`、`NotificationState`、`PeripheralId`、`Platform`、`PlatformErrorDetails`、`QueryDevicesCommand`、`ReadCharacteristicCommand`、`ReadDescriptorCommand`、`ReadRssiCommand`、`RefreshGattCacheCommand`、`RemoveBondCommand`、`ScanOptions`、`ScanSessionId`、`ScanStopReason`、`ScanStopResult`、`ServiceData`、`SetAdapterEnabledCommand`、`SetConnectionPriorityCommand`、`SetNotificationsCommand`、`StartScanCommand`、`StopScanCommand`、`SubscriptionId`、`WriteCharacteristicCommand`、`WriteDescriptorCommand`、`WriteType`

### `bleng` 逐符号

- **rust**（29）：`AssociateCompanionDeviceOperation`、`Bleng`、`BlengBusyReason`、`BlengContractFailure`、`BlengError`、`BlengNotReadyReason`、`BlengResource`、`BondOperation`、`ConnectOperation`、`DisableNotificationsOperation`、`DisconnectOperation`、`DiscoverGattOperation`、`EnableNotificationsOperation`、`InitializeOperation`、`MtuOperation`、`NotificationSubscription`、`OperationFailureObserver`、`OperationFailureRegistration`、`QueryDevicesOperation`、`ReadDescriptorOperation`、`ReadOperation`、`ReadRssiOperation`、`RefreshGattCacheOperation`、`SetAdapterEnabledOperation`、`SetConnectionPriorityOperation`、`StartScanOperation`、`StopScanOperation`、`WriteDescriptorOperation`、`WriteOperation`
- **kotlin**（47）：`AssociateCompanionDeviceOperation`、`AssociateCompanionDeviceOperationInterface`、`Bleng`、`BlengBusyReason`、`BlengContractFailure`、`BlengException`、`BlengInterface`、`BlengNotReadyReason`、`BlengResource`、`BondOperation`、`BondOperationInterface`、`ConnectOperation`、`ConnectOperationInterface`、`DisableNotificationsOperation`、`DisableNotificationsOperationInterface`、`DisconnectOperation`、`DisconnectOperationInterface`、`DiscoverGattOperation`、`DiscoverGattOperationInterface`、`EnableNotificationsOperation`、`EnableNotificationsOperationInterface`、`InitializeOperation`、`InitializeOperationInterface`、`MtuOperation`、`MtuOperationInterface`、`QueryDevicesOperation`、`QueryDevicesOperationInterface`、`ReadDescriptorOperation`、`ReadDescriptorOperationInterface`、`ReadOperation`、`ReadOperationInterface`、`ReadRssiOperation`、`ReadRssiOperationInterface`、`RefreshGattCacheOperation`、`RefreshGattCacheOperationInterface`、`SetAdapterEnabledOperation`、`SetAdapterEnabledOperationInterface`、`SetConnectionPriorityOperation`、`SetConnectionPriorityOperationInterface`、`StartScanOperation`、`StartScanOperationInterface`、`StopScanOperation`、`StopScanOperationInterface`、`WriteDescriptorOperation`、`WriteDescriptorOperationInterface`、`WriteOperation`、`WriteOperationInterface`
- **swift**（48）：`AssociateCompanionDeviceOperation`、`AssociateCompanionDeviceOperationProtocol`、`Bleng`、`BlengBusyReason`、`BlengContractFailure`、`BlengError`、`BlengNotReadyReason`、`BlengProtocol`、`BlengResource`、`BondOperation`、`BondOperationProtocol`、`ConnectOperation`、`ConnectOperationProtocol`、`DisableNotificationsOperation`、`DisableNotificationsOperationProtocol`、`DisconnectOperation`、`DisconnectOperationProtocol`、`DiscoverGattOperation`、`DiscoverGattOperationProtocol`、`EnableNotificationsOperation`、`EnableNotificationsOperationProtocol`、`InitializeOperation`、`InitializeOperationProtocol`、`MtuOperation`、`MtuOperationProtocol`、`NotificationSubscription`、`QueryDevicesOperation`、`QueryDevicesOperationProtocol`、`ReadDescriptorOperation`、`ReadDescriptorOperationProtocol`、`ReadOperation`、`ReadOperationProtocol`、`ReadRssiOperation`、`ReadRssiOperationProtocol`、`RefreshGattCacheOperation`、`RefreshGattCacheOperationProtocol`、`SetAdapterEnabledOperation`、`SetAdapterEnabledOperationProtocol`、`SetConnectionPriorityOperation`、`SetConnectionPriorityOperationProtocol`、`StartScanOperation`、`StartScanOperationProtocol`、`StopScanOperation`、`StopScanOperationProtocol`、`WriteDescriptorOperation`、`WriteDescriptorOperationProtocol`、`WriteOperation`、`WriteOperationProtocol`

### `supervisor` 逐符号

- **rust**（112）：`ForeignAdapterControlBeginResult`、`ForeignAdapterEnd`、`ForeignAdapterEndReason`、`ForeignAdapterObservation`、`ForeignAdapterObservationBeginResult`、`ForeignAndroidScanOptions`、`ForeignBondBeginResult`、`ForeignBondProgress`、`ForeignBondValueResult`、`ForeignCompanionAssociationBeginResult`、`ForeignCompanionPresence`、`ForeignCompanionPresenceBeginResult`、`ForeignCompanionPresenceEnd`、`ForeignCompanionPresenceEndReason`、`ForeignCompanionPresenceObservation`、`ForeignConnectBeginResult`、`ForeignConnectResult`、`ForeignConnectionEnd`、`ForeignConnectionEndBy`、`ForeignConnectionEndObservation`、`ForeignDescriptorReadBeginResult`、`ForeignDescriptorReadValueResult`、`ForeignDescriptorWriteBeginResult`、`ForeignDeviceListResult`、`ForeignDeviceQueryBeginResult`、`ForeignDeviceRecord`、`ForeignDisconnectBeginResult`、`ForeignExternalRecoverySignal`、`ForeignGattDiscoveryBeginResult`、`ForeignGattDiscoveryResult`、`ForeignManufacturerDataFilter`、`ForeignMtuBeginResult`、`ForeignMtuValueResult`、`ForeignNotificationDeliverySnapshot`、`ForeignNotificationDisableResult`、`ForeignNotificationEnd`、`ForeignNotificationEndReason`、`ForeignNotificationObservation`、`ForeignNotificationSubscribeBeginResult`、`ForeignNotificationSubscribeResult`、`ForeignOperationCommandResult`、`ForeignOperationFailure`、`ForeignOperationFailureDomain`、`ForeignOwnerClose`、`ForeignOwnerCloseFailure`、`ForeignOwnerContext`、`ForeignOwnerStart`、`ForeignPeripheralAdoptResult`、`ForeignPlatformActionResult`、`ForeignPlatformOwnerSession`、`ForeignPlatformOwnerSessionFactory`、`ForeignPlatformOwnerSessionFactoryAdapter`、`ForeignPlatformRecoveryAction`、`ForeignPriorityBeginResult`、`ForeignReadBeginResult`、`ForeignReadValueResult`、`ForeignRecoveryAction`、`ForeignRecoveryLink`、`ForeignRecoveryMode`、`ForeignRecoveryObservation`、`ForeignRecoveryOutcome`、`ForeignRecoveryPlan`、`ForeignRecoveryPolicyConfig`、`ForeignRecoverySignal`、`ForeignRecoveryTarget`、`ForeignRefreshBeginResult`、`ForeignResourceCount`、`ForeignRssiBeginResult`、`ForeignRssiValueResult`、`ForeignScanAdvertisement`、`ForeignScanBeginResult`、`ForeignScanDeliverySnapshot`、`ForeignScanEnd`、`ForeignScanEndReason`、`ForeignScanEventEnvelope`、`ForeignScanObservation`、`ForeignScanOptions`、`ForeignScanStopResult`、`ForeignSupervisorConfig`、`ForeignWriteBeginResult`、`ScanEvent`、`SessionEventProjection`、`SupervisedAdapterControl`、`SupervisedAdapterObservation`、`SupervisedBond`、`SupervisedCompanionAssociation`、`SupervisedCompanionPresenceObservation`、`SupervisedConnect`、`SupervisedConnection`、`SupervisedConnectionPriority`、`SupervisedDescriptorRead`、`SupervisedDescriptorWrite`、`SupervisedDeviceQuery`、`SupervisedDisconnect`、`SupervisedGattCharacteristic`、`SupervisedGattDescriptor`、`SupervisedGattDiscovery`、`SupervisedGattService`、`SupervisedGattSession`、`SupervisedMtu`、`SupervisedNotificationDisable`、`SupervisedNotifications`、`SupervisedPeripheral`、`SupervisedRead`、`SupervisedRefreshGattCache`、`SupervisedRssi`、`SupervisedScan`、`SupervisedScanStop`、`SupervisedWrite`、`Supervisor`、`SupervisorFfiError`、`SupervisorOperationError`
- **kotlin**（75）：`ForeignAdapterEndReason`、`ForeignCompanionPresenceEndReason`、`ForeignConnectionEndBy`、`ForeignExternalRecoverySignal`、`ForeignNotificationEndReason`、`ForeignOperationFailureDomain`、`ForeignPlatformOwnerSession`、`ForeignPlatformOwnerSessionFactory`、`ForeignPlatformRecoveryAction`、`ForeignRecoveryAction`、`ForeignRecoveryMode`、`ForeignRecoveryOutcome`、`ForeignRecoverySignal`、`ForeignScanEndReason`、`ScanEvent`、`SessionEventProjection`、`SessionEventProjectionInterface`、`SupervisedAdapterControl`、`SupervisedAdapterControlInterface`、`SupervisedAdapterObservation`、`SupervisedAdapterObservationInterface`、`SupervisedBond`、`SupervisedBondInterface`、`SupervisedCompanionAssociation`、`SupervisedCompanionAssociationInterface`、`SupervisedCompanionPresenceObservation`、`SupervisedCompanionPresenceObservationInterface`、`SupervisedConnect`、`SupervisedConnectInterface`、`SupervisedConnection`、`SupervisedConnectionInterface`、`SupervisedConnectionPriority`、`SupervisedConnectionPriorityInterface`、`SupervisedDescriptorRead`、`SupervisedDescriptorReadInterface`、`SupervisedDescriptorWrite`、`SupervisedDescriptorWriteInterface`、`SupervisedDeviceQuery`、`SupervisedDeviceQueryInterface`、`SupervisedDisconnect`、`SupervisedDisconnectInterface`、`SupervisedGattCharacteristic`、`SupervisedGattCharacteristicInterface`、`SupervisedGattDescriptor`、`SupervisedGattDescriptorInterface`、`SupervisedGattDiscovery`、`SupervisedGattDiscoveryInterface`、`SupervisedGattService`、`SupervisedGattServiceInterface`、`SupervisedGattSession`、`SupervisedGattSessionInterface`、`SupervisedMtu`、`SupervisedMtuInterface`、`SupervisedNotificationDisable`、`SupervisedNotificationDisableInterface`、`SupervisedNotifications`、`SupervisedNotificationsInterface`、`SupervisedPeripheral`、`SupervisedPeripheralInterface`、`SupervisedRead`、`SupervisedReadInterface`、`SupervisedRefreshGattCache`、`SupervisedRefreshGattCacheInterface`、`SupervisedRssi`、`SupervisedRssiInterface`、`SupervisedScan`、`SupervisedScanInterface`、`SupervisedScanStop`、`SupervisedScanStopInterface`、`SupervisedWrite`、`SupervisedWriteInterface`、`Supervisor`、`SupervisorFfiException`、`SupervisorInterface`、`SupervisorOperationException`
- **swift**（140）：`ForeignAdapterControlBeginResult`、`ForeignAdapterEnd`、`ForeignAdapterEndReason`、`ForeignAdapterObservation`、`ForeignAdapterObservationBeginResult`、`ForeignAndroidScanOptions`、`ForeignBondBeginResult`、`ForeignBondProgress`、`ForeignBondValueResult`、`ForeignCompanionAssociationBeginResult`、`ForeignCompanionPresence`、`ForeignCompanionPresenceBeginResult`、`ForeignCompanionPresenceEnd`、`ForeignCompanionPresenceEndReason`、`ForeignCompanionPresenceObservation`、`ForeignConnectBeginResult`、`ForeignConnectResult`、`ForeignConnectionEnd`、`ForeignConnectionEndBy`、`ForeignConnectionEndObservation`、`ForeignDescriptorReadBeginResult`、`ForeignDescriptorReadValueResult`、`ForeignDescriptorWriteBeginResult`、`ForeignDeviceListResult`、`ForeignDeviceQueryBeginResult`、`ForeignDeviceRecord`、`ForeignDisconnectBeginResult`、`ForeignExternalRecoverySignal`、`ForeignGattDiscoveryBeginResult`、`ForeignGattDiscoveryResult`、`ForeignManufacturerDataFilter`、`ForeignMtuBeginResult`、`ForeignMtuValueResult`、`ForeignNotificationDeliverySnapshot`、`ForeignNotificationDisableResult`、`ForeignNotificationEnd`、`ForeignNotificationEndReason`、`ForeignNotificationObservation`、`ForeignNotificationSubscribeBeginResult`、`ForeignNotificationSubscribeResult`、`ForeignOperationCommandResult`、`ForeignOperationFailure`、`ForeignOperationFailureDomain`、`ForeignOwnerClose`、`ForeignOwnerCloseFailure`、`ForeignOwnerContext`、`ForeignOwnerStart`、`ForeignPeripheralAdoptResult`、`ForeignPlatformActionResult`、`ForeignPlatformOwnerSession`、`ForeignPlatformOwnerSessionFactory`、`ForeignPlatformRecoveryAction`、`ForeignPriorityBeginResult`、`ForeignReadBeginResult`、`ForeignReadValueResult`、`ForeignRecoveryAction`、`ForeignRecoveryLink`、`ForeignRecoveryMode`、`ForeignRecoveryObservation`、`ForeignRecoveryOutcome`、`ForeignRecoveryPlan`、`ForeignRecoveryPolicyConfig`、`ForeignRecoverySignal`、`ForeignRecoveryTarget`、`ForeignRefreshBeginResult`、`ForeignResourceCount`、`ForeignRssiBeginResult`、`ForeignRssiValueResult`、`ForeignScanAdvertisement`、`ForeignScanBeginResult`、`ForeignScanDeliverySnapshot`、`ForeignScanEnd`、`ForeignScanEndReason`、`ForeignScanEventEnvelope`、`ForeignScanObservation`、`ForeignScanOptions`、`ForeignScanStopResult`、`ForeignSupervisorConfig`、`ForeignWriteBeginResult`、`ScanEvent`、`SessionEventProjection`、`SessionEventProjectionProtocol`、`SupervisedAdapterControl`、`SupervisedAdapterControlProtocol`、`SupervisedAdapterObservation`、`SupervisedAdapterObservationProtocol`、`SupervisedBond`、`SupervisedBondProtocol`、`SupervisedCompanionAssociation`、`SupervisedCompanionAssociationProtocol`、`SupervisedCompanionPresenceObservation`、`SupervisedCompanionPresenceObservationProtocol`、`SupervisedConnect`、`SupervisedConnectProtocol`、`SupervisedConnection`、`SupervisedConnectionPriority`、`SupervisedConnectionPriorityProtocol`、`SupervisedConnectionProtocol`、`SupervisedDescriptorRead`、`SupervisedDescriptorReadProtocol`、`SupervisedDescriptorWrite`、`SupervisedDescriptorWriteProtocol`、`SupervisedDeviceQuery`、`SupervisedDeviceQueryProtocol`、`SupervisedDisconnect`、`SupervisedDisconnectProtocol`、`SupervisedGattCharacteristic`、`SupervisedGattCharacteristicProtocol`、`SupervisedGattDescriptor`、`SupervisedGattDescriptorProtocol`、`SupervisedGattDiscovery`、`SupervisedGattDiscoveryProtocol`、`SupervisedGattService`、`SupervisedGattServiceProtocol`、`SupervisedGattSession`、`SupervisedGattSessionProtocol`、`SupervisedMtu`、`SupervisedMtuProtocol`、`SupervisedNotificationDisable`、`SupervisedNotificationDisableProtocol`、`SupervisedNotifications`、`SupervisedNotificationsProtocol`、`SupervisedPeripheral`、`SupervisedPeripheralProtocol`、`SupervisedRead`、`SupervisedReadProtocol`、`SupervisedRefreshGattCache`、`SupervisedRefreshGattCacheProtocol`、`SupervisedRssi`、`SupervisedRssiProtocol`、`SupervisedScan`、`SupervisedScanProtocol`、`SupervisedScanStop`、`SupervisedScanStopProtocol`、`SupervisedWrite`、`SupervisedWriteProtocol`、`Supervisor`、`SupervisorFfiError`、`SupervisorOperationError`、`SupervisorProtocol`

## 2. JS / Nitro 面

### `BlengAdapterObservation.nitro.ts`

- `BlengAdapterSnapshotEvent`
- `BlengAdapterEndedEvent`
- `BlengAdapterObservation`
  - `nextEvent(timeoutMs: number): Promise<BlengAdapterNext>`
- `BlengAdapterObservationAccepted`
- 联合/别名：`BlengAdapterNext`、`BlengAdapterObservationSubmission`

### `BlengCentral.nitro.ts`

- `BlengAdapterSnapshot`
- `BlengCloseResult`
- `BlengInitializeOutcome`
- `BlengAdapterControl`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengAdapterControlAccepted`
- `BlengCompanionAssociation`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengCompanionAssociationAccepted`
- `BlengDeviceInfo`
- `BlengDeviceQueryOutcome`
- `BlengDeviceQuery`
  - `wait(timeoutMs: number): Promise<BlengDeviceQueryResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengDeviceQueryAccepted`
- `BlengPeripheralAdoptAccepted`
- `BlengCentral`
  - `initialize(): Promise<BlengInitializeResult>`
  - `beginScan(options: BlengScanOptions): BlengScanSubmission`
  - `beginAdapterControl(enabled: boolean): BlengAdapterControlSubmission`
  - `adoptPeripheral(key: string): BlengPeripheralAdoptSubmission`
  - `beginAdapterObservation(): BlengAdapterObservationSubmission`
  - `beginCompanionPresence(): BlengCompanionPresenceSubmission`
  - `close(): Promise<BlengCloseResult>`
- 联合/别名：`BlengAdapterState`、`BlengAuthorizationState`、`BlengInitializeResult`、`BlengAdapterControlSubmission`、`BlengCompanionAssociationSubmission`、`BlengDeviceQueryKind`、`BlengDeviceQueryResult`、`BlengDeviceQuerySubmission`、`BlengPeripheralAdoptSubmission`

### `BlengCompanionPresence.nitro.ts`

- `BlengCompanionPresenceEvent`
- `BlengCompanionPresenceEndedEvent`
- `BlengCompanionPresence`
  - `nextEvent(timeoutMs: number): Promise<BlengCompanionPresenceNext>`
- `BlengCompanionPresenceAccepted`
- 联合/别名：`BlengCompanionPresenceNext`、`BlengCompanionPresenceSubmission`

### `BlengConnection.nitro.ts`

- `BlengConnection`
  - `beginDisconnect(): BlengDisconnectSubmission`
  - `beginDiscoverGatt(): BlengGattDiscoverySubmission`
  - `beginReadRssi(): BlengRssiSubmission`
  - `beginRequestMtu(mtu: number): BlengMtuSubmission`
  - `beginQueryMtu(): BlengMtuSubmission`
  - `beginSetConnectionPriority(priority: BlengConnectionPriority): BlengPrioritySubmission`
  - `beginBond(pin?: string): BlengBondSubmission`
  - `beginRemoveBond(): BlengBondSubmission`
  - `beginGattCacheRefresh(): BlengGattCacheRefreshSubmission`
  - `isConnected(): boolean`
  - `localName(): string | undefined`
  - `awaitEnd(timeoutMs: number): Promise<BlengConnectionNext>`
- `BlengConnect`
  - `wait(timeoutMs: number): Promise<BlengConnectResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengDisconnect`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengPriority`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengBondProgress`
- `BlengBond`
  - `wait(timeoutMs: number): Promise<BlengBondResult>`
  - `cancel(): Promise<BlengReceipt>`
  - `nextState(timeoutMs: number): Promise<BlengBondProgressResult>`
- `BlengBondAccepted`
- `BlengBondOutcome`
- `BlengGattCacheRefresh`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengGattCacheRefreshAccepted`
- `BlengPriorityAccepted`
- `BlengConnectionEndedEvent`
- `BlengConnectAccepted`
- `BlengConnectOutcome`
- `BlengDisconnectAccepted`
- 联合/别名：`BlengConnectionPriority`、`BlengBondState`、`BlengBondSubmission`、`BlengBondResult`、`BlengBondProgressResult`、`BlengGattCacheRefreshSubmission`、`BlengPrioritySubmission`、`BlengConnectionNext`、`BlengConnectSubmission`、`BlengConnectResult`、`BlengDisconnectSubmission`

### `BlengEnd.nitro.ts`

- `BlengEnd`
- 联合/别名：`BlengEndReason`

### `BlengFailure.nitro.ts`

- `BlengPlatformDetails`
- `BlengFailureInfo`
- `BlengFailure`
- `BlengReceipt`
- 联合/别名：`BlengFailureDomain`、`BlengPlatform`

### `BlengGatt.nitro.ts`

- `BlengCharacteristicProperties`
- `BlengGattDescriptor`
  - `uuid(): string`
  - `beginRead(): BlengDescriptorReadSubmission`
  - `beginWrite(value: ArrayBuffer): BlengDescriptorWriteSubmission`
- `BlengDescriptorRead`
  - `wait(timeoutMs: number): Promise<BlengDescriptorReadResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengDescriptorWrite`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengDescriptorReadAccepted`
- `BlengDescriptorWriteAccepted`
- `BlengDescriptorReadOutcome`
- `BlengGattSession`
  - `services(): BlengGattService[]`
- `BlengGattService`
  - `uuid(): string`
  - `kind(): BlengServiceKind`
  - `characteristics(): BlengGattCharacteristic[]`
- `BlengGattCharacteristic`
  - `uuid(): string`
  - `properties(): BlengCharacteristicProperties`
  - `descriptors(): BlengGattDescriptor[]`
  - `beginRead(): BlengReadSubmission`
  - `beginWrite(value: ArrayBuffer, writeType: BlengWriteType): BlengWriteSubmission`
- `BlengGattDiscovery`
  - `wait(timeoutMs: number): Promise<BlengGattDiscoveryResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengRead`
  - `wait(timeoutMs: number): Promise<BlengReadResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengWrite`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengRssi`
  - `wait(timeoutMs: number): Promise<BlengRssiResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengMtuInfo`
- `BlengMtu`
  - `wait(timeoutMs: number): Promise<BlengMtuResult>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengGattDiscoveryAccepted`
- `BlengGattSessionOutcome`
- `BlengReadAccepted`
- `BlengWriteAccepted`
- `BlengRssiAccepted`
- `BlengMtuAccepted`
- `BlengReadOutcome`
- `BlengRssiOutcome`
- `BlengMtuOutcome`
- 联合/别名：`BlengWriteType`、`BlengDescriptorReadSubmission`、`BlengDescriptorReadResult`、`BlengDescriptorWriteSubmission`、`BlengServiceKind`、`BlengGattDiscoverySubmission`、`BlengGattDiscoveryResult`、`BlengReadSubmission`、`BlengWriteSubmission`、`BlengReadResult`、`BlengRssiSubmission`、`BlengRssiResult`、`BlengMtuSubmission`、`BlengMtuResult`

### `BlengScan.nitro.ts`

- `BlengScanOptions`
- `BlengAndroidScanOptions`
- `BlengManufacturerDataFilter`
- `BlengManufacturerData`
- `BlengServiceData`
- `BlengPeripheral`
  - `key(): string`
  - `localName(): string | undefined`
  - `beginConnect(): BlengConnectSubmission`
  - `beginAutoConnect(): BlengConnectSubmission`
- `BlengScanAdvertisement`
- `BlengScanAdvertisementEvent`
- `BlengScanEndedEvent`
- `BlengScanStop`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengScan`
  - `waitUntilStarted(timeoutMs: number): Promise<BlengReceipt>`
  - `cancelStart(): Promise<BlengReceipt>`
  - `isScanning(): boolean`
  - `nextEvent(timeoutMs: number): Promise<BlengScanNext>`
  - `beginStop(): BlengScanStopSubmission`
- `BlengScanAccepted`
- `BlengScanStopAccepted`
- 联合/别名：`BlengAndroidScanMode`、`BlengAndroidScanPhy`、`BlengScanNext`、`BlengScanSubmission`、`BlengScanStopSubmission`

### `BlengSubscription.nitro.ts`

- `BlengSubscription`
  - `waitUntilEnabled(timeoutMs: number): Promise<BlengSubscriptionResult>`
  - `nextEvent(timeoutMs: number): Promise<BlengSubscriptionNext>`
  - `beginDisable(): BlengSubscriptionDisableSubmission`
  - `detach(): void`
- `BlengSubscriptionDisable`
  - `wait(timeoutMs: number): Promise<BlengReceipt>`
  - `cancel(): Promise<BlengReceipt>`
- `BlengSubscriptionValueEvent`
- `BlengSubscriptionEndedEvent`
- `BlengSubscriptionAccepted`
- `BlengSubscriptionOutcome`
- `BlengSubscriptionDisableAccepted`
- 联合/别名：`BlengNotificationModeRequest`、`BlengNotificationMode`、`BlengSubscriptionNext`、`BlengSubscriptionSubmission`、`BlengSubscriptionResult`、`BlengSubscriptionDisableSubmission`

入口标识（D9）：包名 `@blehub/react-native-bleng`、`createBlengCentral()`、
`getBlengCentral()`、`BlengProvider`、`BlengCentral`、`BleHubBlengEngineLifecycle`。
D7 的派生面（同一 JS 面，冻结时一并算）：`pendingOperation`、`run`、`requireAccepted`、
`requireOutcome`、`AbortSignalLike`、`BlengOperationError`、`Operation`、`OperationSubmission`、`PendingOperation`、`isBlengFailure`、`shareLane`、`useBlengCentral`、`closeBlengCentral`。

## 3. 失败 `code` 空间（D4：只允许追加）

一个 `code` 只有在下列三种组合下才是 wire 字符串，所以按**形状**分组：同一形状内的
字面量就是全部可选值。
两端的 `BlengFailureMapping` 不做二次命名，桥级只额外产生一个 `bridgeFailure`。

### `bleng.<code>` — BlengError / dispatch 映射（33）

```text
closed
cancelled
operation_not_found
operation_already_completed
operation_not_cancellable
operation_result_consumed
disconnected
invalid_timeout
not_ready.backend_not_assembled
not_ready.not_initialized
not_ready.scan_not_active
not_ready.connection_not_active
not_ready.gatt_not_ready
not_ready.subscription_not_active
busy.scan
busy.peripheral
busy.gatt
busy.operation
busy.backend_slot_occupied
resource_exhausted.pending_waiters
resource_exhausted.submission_queue
resource_exhausted.identifiers
contract_failure.invalid_command_id
contract_failure.duplicate_hub_ownership
timeout
transport_fault
worker_stopped
invalid_request
invalid_state
resource_exhausted
backend_unavailable
not_supported
ffi_contract
```

### `<path>.<code>` — ble_failure / hub_failure，path ∈ `bleng.operation`、`bleng.assembly`、`bleng.cleanup`（18）

```text
permission
adapter_unavailable
invalid_state
not_found
timeout
cancelled
operation_aborted
connection
gatt
protocol
not_supported
backend
internal
backend_assembly
backend_cleanup
resource_busy
invalid_configuration
ffi_contract
```

### `原样` — supervisor 自己产生，不经前缀组合（6）

```text
supervisor.no_active_session
supervisor.stale_context
supervisor.recovery_not_ready
supervisor.busy
supervisor.unavailable
supervisor.invalid_context
```

新增 `code` 是允许的（追加）；重命名或删除需要解冻评审。

## 4. 明确不冻结

- marker 字符串、evidence JSON schema、fixture contract/profile、`tools/` 与 recipe、任何 `*_ledger`。
- 场景计划与宿主动作（`scene_plans.py`）：运行期数据，不是 ABI。
- native bundle id / package 名：留到真正对外发布时再核对（D9）。

## 5. 签名

| 项 | 决定 |
|---|---|
| D1 跨语言关闭命名 | 保持本地惯用名 |
| D2 事件读取粒度 | 按能力 pull |
| D3 deadline 表达 | 显式 `timeoutMs` |
| D4 错误 taxonomy | 形状冻结，`code` 只追加 |
| D5 可选字段语义 | 缺省即该分支 |
| D6 平台名类型 | `string` |
| D7 操作模型 | `begin*` / `wait` / `cancel` + 派生 await 层 |
| D8 版本策略 | 开发阶段不承诺兼容 |
| D9 公开包边界 | 冻结包名与入口 |

签署后，任何改动本文件所列形状的 commit 都必须走解冻/评审，而不是 `snapshot-update`。
