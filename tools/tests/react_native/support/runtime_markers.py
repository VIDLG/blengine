"""RN-only runtime flow samples used by offline ledger tests."""

from __future__ import annotations

from tests.support.rf_lab.markers import (
    GATT_MARKERS,
    MARKERS,
    SERVICE,
)

CONNECT_MARKERS = [
    *MARKERS[:-2],
    f"rf:step:connecting:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connected:passed:commandId=9005:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:disconnected:passed:commandId=9006:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    MARKERS[-2],
    f"rf-connect:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:disconnectCommandId=9006:scanSessionId=9004",
]

RSSI_MARKERS = [
    *CONNECT_MARKERS[:8],
    f"rf:step:rssi:passed:commandId=9026:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:rssiDbm=-65",
    *CONNECT_MARKERS[8:-1],
    f"rf-rssi:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:rssiCommandId=9026:rssiDbm=-65:disconnectCommandId=9006:scanSessionId=9004",
]

NOTIFICATION_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:notification-enable:passed:commandId=9015:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:actualMode=Notification",
    "rf:step:notification-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:value=ef",
    "rf:step:notification-disable:passed:commandId=9016:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:state=Disabled:postDisableSilenceMs=2000",
    *GATT_MARKERS[9:-1],
    f"rf-notification:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:enableCommandId=9015:disableCommandId=9016:subscriptionId=9017:notificationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004",
]

INDICATION_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:indication-rejection:passed:commandId=9018:connectionId=9007:gattSessionId=9009:subscriptionId=9022:characteristicId=fff20000:error=not_supported",
    "rf:step:indication-enable:passed:commandId=9019:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:actualMode=Indication",
    "rf:step:indication-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:value=ef",
    "rf:step:indication-disable:passed:commandId=9020:connectionId=9007:gattSessionId=9009:subscriptionId=9021:characteristicId=fff50000:state=Disabled:postDisableSilenceMs=2000",
    *GATT_MARKERS[9:-1],
    f"rf-indication:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:rejectionCommandId=9018:enableCommandId=9019:disableCommandId=9020:subscriptionId=9021:indicationValue=ef:postDisableSilenceMs=2000:disconnectCommandId=9006:scanSessionId=9004",
]

PASSIVE_DISCONNECT_MARKERS = [
    *GATT_MARKERS[:9],
    "rf:step:notification-enable:passed:commandId=9015:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:actualMode=Notification",
    "rf:step:notification-value:passed:connectionId=9007:gattSessionId=9009:subscriptionId=9017:characteristicId=fff20000:value=ef",
    f"rf:step:disconnected:passed:commandId=none:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:connection-silence:passed:connectionId=9007:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    f"rf:step:recovery-connecting:passed:commandId=9023:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-connected:passed:commandId=9023:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-disconnected:passed:commandId=9025:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}",
    f"rf:step:recovery-connection-silence:passed:connectionId=9024:peripheralId=42:serviceUuid={SERVICE}:observationMs=1000",
    GATT_MARKERS[-2],
    f"rf-passive-disconnect:passed:serviceUuid={SERVICE}:peripheralId=42:connectionId=9007:connectCommandId=9005:gattCommandId=9008:subscriptionId=9017:recoveryConnectCommandId=9023:recoveryConnectionId=9024:recoveryDisconnectCommandId=9025:notificationValue=ef:passiveDisconnectCommandId=none:scanSessionId=9004",
]
