//! Native-only, context-fenced submission to the supervisor's current BLeng owner.
//!
//! Each method returns the existing engine handle. Waiting and cancellation do not
//! borrow the supervisor, so neither can block its recovery or shutdown lifecycle.

use std::sync::{Arc, TryLockError};

use blehub::{
    BluetoothUuid, ConnectionId, ConnectionPriority, DeviceQueryKind, GattCharacteristicId,
    GattDescriptorId, GattSessionId, NotificationModeRequest, PeripheralId, ScanOptions,
    ScanSessionId, SubscriptionId, WriteType,
};
use bleng::{
    AssociateCompanionDeviceOperation, BondOperation, ConnectOperation,
    DisableNotificationsOperation, DisconnectOperation, DiscoverGattOperation,
    EnableNotificationsOperation, MtuOperation, QueryDevicesOperation, ReadDescriptorOperation,
    ReadOperation, ReadRssiOperation, RefreshGattCacheOperation, SetAdapterEnabledOperation,
    SetConnectionPriorityOperation, StartScanOperation, StopScanOperation,
    WriteDescriptorOperation, WriteOperation,
};

use super::super::failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    bleng_failure, normalize_timeout,
};
use super::super::types::ForeignOwnerContext;
use super::{Supervisor, SupervisorState};
use crate::{RecoveryContext, SessionSupervisor, SupervisorOperationError};

#[cfg(test)]
#[path = "tests/operations.rs"]
mod tests;

/// One bounded companion-association receipt. Success only means the platform reported an
/// association; it is neither a present device nor a link.
#[derive(uniffi::Object)]
pub struct SupervisedCompanionAssociation {
    operation: Arc<AssociateCompanionDeviceOperation>,
}

/// Bounded companion-association submission. A present capability is not a completed
/// association.
#[derive(Clone, uniffi::Record)]
pub struct ForeignCompanionAssociationBeginResult {
    pub association: Option<Arc<SupervisedCompanionAssociation>>,
    pub failure: Option<ForeignOperationFailure>,
}

/// One bounded adapter-power receipt. Success only means the platform accepted the request.
#[derive(uniffi::Object)]
pub struct SupervisedAdapterControl {
    operation: Arc<SetAdapterEnabledOperation>,
}

/// Bounded adapter-power submission. A present capability is not a completed power change.
#[derive(Clone, uniffi::Record)]
pub struct ForeignAdapterControlBeginResult {
    pub control: Option<Arc<SupervisedAdapterControl>>,
    pub failure: Option<ForeignOperationFailure>,
}

/// One read-only inventory entry; not a connectable capability.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignDeviceRecord {
    pub key: String,
    pub local_name: Option<String>,
    pub connected: bool,
    pub bonded: bool,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignDeviceQueryBeginResult {
    pub query: Option<Arc<SupervisedDeviceQuery>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignDeviceListResult {
    pub devices: Option<Vec<ForeignDeviceRecord>>,
    pub failure: Option<ForeignOperationFailure>,
}

/// One read-only system inventory query.
#[derive(uniffi::Object)]
pub struct SupervisedDeviceQuery {
    operation: Arc<QueryDevicesOperation>,
}

/// One bounded GATT-cache refresh receipt.
#[derive(uniffi::Object)]
pub struct SupervisedRefreshGattCache {
    pub(in crate::ffi) operation: Arc<RefreshGattCacheOperation>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignRefreshBeginResult {
    pub refresh: Option<Arc<SupervisedRefreshGattCache>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[uniffi::export]
impl Supervisor {
    /// Accepts a native request scoped to the context that produced its inputs.
    /// Success means bounded submission, not actor admission or BLE success.
    pub fn begin_start_scan(
        &self,
        context: ForeignOwnerContext,
        options: ScanOptions,
    ) -> Result<Arc<StartScanOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_start_scan(context, options)
        })
    }

    pub fn begin_stop_scan(
        &self,
        context: ForeignOwnerContext,
        session: ScanSessionId,
    ) -> Result<Arc<StopScanOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_stop_scan(context, session)
        })
    }

    /// Engine-level binder for one connect. The returned operation is the whole capability here: a
    /// product that needs to observe the link's end uses `SupervisedPeripheral::begin_connect`,
    /// which keeps the end lane this raw entry point drops.
    pub fn begin_connect(
        &self,
        context: ForeignOwnerContext,
        peripheral: PeripheralId,
    ) -> Result<Arc<ConnectOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor
                .begin_connect_view(context, peripheral)
                .map(|handoff| handoff.operation)
                .map_err(SupervisorOperationError::from)
        })
    }

    pub fn begin_disconnect(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> Result<Arc<DisconnectOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_disconnect(context, connection)
        })
    }

    pub fn begin_discover_gatt(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> Result<Arc<DiscoverGattOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_discover_gatt(context, connection)
        })
    }

    pub fn begin_read_characteristic(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
    ) -> Result<Arc<ReadOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_read_characteristic(context, connection, session, characteristic)
        })
    }

    pub fn begin_write_characteristic(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
    ) -> Result<Arc<WriteOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_write_characteristic(
                context,
                connection,
                session,
                characteristic,
                value,
                write_type,
            )
        })
    }

    pub fn begin_enable_notifications(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> Result<Arc<EnableNotificationsOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_enable_notifications(
                context,
                connection,
                session,
                characteristic,
                mode,
            )
        })
    }

    pub fn begin_disable_notifications(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
    ) -> Result<Arc<DisableNotificationsOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_disable_notifications(context, connection, session, subscription)
        })
    }

    pub fn begin_read_rssi(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> Result<Arc<ReadRssiOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_read_rssi(context, connection)
        })
    }

    pub fn begin_request_mtu(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        mtu: u16,
    ) -> Result<Arc<MtuOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_request_mtu(context, connection, mtu)
        })
    }

    pub fn begin_query_mtu(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> Result<Arc<MtuOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_query_mtu(context, connection)
        })
    }

    pub fn begin_read_descriptor(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
    ) -> Result<Arc<ReadDescriptorOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_read_descriptor(
                context,
                connection,
                session,
                characteristic,
                descriptor,
            )
        })
    }

    pub fn begin_write_descriptor(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        value: Vec<u8>,
    ) -> Result<Arc<WriteDescriptorOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_write_descriptor(
                context,
                connection,
                session,
                characteristic,
                descriptor,
                value,
            )
        })
    }

    pub fn begin_set_connection_priority(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        priority: ConnectionPriority,
    ) -> Result<Arc<SetConnectionPriorityOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_set_connection_priority(context, connection, priority)
        })
    }

    /// Requests a radio power change where the platform supports it. Success only means the
    /// request was accepted; the adapter lane reports the actual state.
    pub fn begin_set_adapter_enabled(
        &self,
        context: ForeignOwnerContext,
        enabled: bool,
    ) -> Result<Arc<SupervisedAdapterControl>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor
                .begin_set_adapter_enabled(context, enabled)
                .map(|operation| Arc::new(SupervisedAdapterControl { operation }))
        })
    }

    /// Record-based variant that preserves the typed failure across the Nitro bridge.
    pub fn begin_set_adapter_enabled_view(
        &self,
        context: ForeignOwnerContext,
        enabled: bool,
    ) -> ForeignAdapterControlBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor.begin_set_adapter_enabled(context, enabled)
        }) {
            Ok(operation) => ForeignAdapterControlBeginResult {
                control: Some(Arc::new(SupervisedAdapterControl { operation })),
                failure: None,
            },
            Err(error) => {
                let failure: Box<ForeignOperationFailure> = error.into();
                ForeignAdapterControlBeginResult {
                    control: None,
                    failure: Some(*failure),
                }
            }
        }
    }

    /// Requests one companion-device association where the platform supports it. Success only means
    /// the platform reported an association; read the companion inventory for the list.
    pub fn begin_associate_companion(
        &self,
        context: ForeignOwnerContext,
        service_uuids: Vec<String>,
        display_name: Option<String>,
        device_profile: Option<String>,
        notify_on_device_nearby: bool,
    ) -> Result<Arc<SupervisedCompanionAssociation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor
                .begin_associate_companion(
                    context,
                    service_uuids
                        .into_iter()
                        .map(|value| BluetoothUuid { value })
                        .collect(),
                    display_name,
                    device_profile,
                    notify_on_device_nearby,
                )
                .map(|operation| Arc::new(SupervisedCompanionAssociation { operation }))
        })
    }

    /// Record-based variant that preserves the typed failure across the Nitro bridge.
    pub fn begin_associate_companion_view(
        &self,
        context: ForeignOwnerContext,
        service_uuids: Vec<String>,
        display_name: Option<String>,
        device_profile: Option<String>,
        notify_on_device_nearby: bool,
    ) -> ForeignCompanionAssociationBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor
                .begin_associate_companion(
                    context,
                    service_uuids
                        .into_iter()
                        .map(|value| BluetoothUuid { value })
                        .collect(),
                    display_name,
                    device_profile,
                    notify_on_device_nearby,
                )
                .map(|operation| Arc::new(SupervisedCompanionAssociation { operation }))
        }) {
            Ok(association) => ForeignCompanionAssociationBeginResult {
                association: Some(association),
                failure: None,
            },
            Err(error) => {
                let failure: Box<ForeignOperationFailure> = error.into();
                ForeignCompanionAssociationBeginResult {
                    association: None,
                    failure: Some(*failure),
                }
            }
        }
    }

    /// Reads a read-only system inventory. Results are data, not connectable capabilities.
    pub fn begin_query_devices_view(
        &self,
        context: ForeignOwnerContext,
        kind: DeviceQueryKind,
        service_uuids: Vec<String>,
    ) -> ForeignDeviceQueryBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor.begin_query_devices(
                context,
                kind,
                service_uuids
                    .into_iter()
                    .map(|value| BluetoothUuid { value })
                    .collect(),
            )
        }) {
            Ok(operation) => ForeignDeviceQueryBeginResult {
                query: Some(Arc::new(SupervisedDeviceQuery { operation })),
                failure: None,
            },
            Err(error) => {
                let failure: Box<ForeignOperationFailure> = error.into();
                ForeignDeviceQueryBeginResult {
                    query: None,
                    failure: Some(*failure),
                }
            }
        }
    }

    /// Requests invalidation of the platform's cached GATT topology for this connection.
    pub fn begin_refresh_gatt_cache_view(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> ForeignRefreshBeginResult {
        match self.submit_operation(context, |supervisor, context| {
            supervisor.begin_refresh_gatt_cache(context, connection)
        }) {
            Ok(operation) => ForeignRefreshBeginResult {
                refresh: Some(Arc::new(SupervisedRefreshGattCache { operation })),
                failure: None,
            },
            Err(error) => {
                let failure: Box<ForeignOperationFailure> = error.into();
                ForeignRefreshBeginResult {
                    refresh: None,
                    failure: Some(*failure),
                }
            }
        }
    }

    pub fn begin_create_bond(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
        pin: Option<String>,
    ) -> Result<Arc<BondOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_create_bond(context, connection, pin)
        })
    }

    pub fn begin_remove_bond(
        &self,
        context: ForeignOwnerContext,
        connection: ConnectionId,
    ) -> Result<Arc<BondOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_remove_bond(context, connection)
        })
    }
}

#[uniffi::export]
impl SupervisedCompanionAssociation {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignOperationCommandResult::from_failure(Some(*failure)),
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

#[uniffi::export]
impl SupervisedAdapterControl {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignOperationCommandResult::from_failure(Some(*failure)),
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

#[uniffi::export]
impl SupervisedDeviceQuery {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignDeviceListResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignDeviceListResult {
                    devices: None,
                    failure: Some(*failure),
                };
            }
        };
        match self.operation.wait(timeout_ms).await {
            Ok(devices) => ForeignDeviceListResult {
                devices: Some(
                    devices
                        .into_iter()
                        .map(|device| ForeignDeviceRecord {
                            key: device.key,
                            local_name: device.local_name,
                            connected: device.connected,
                            bonded: device.bonded,
                        })
                        .collect(),
                ),
                failure: None,
            },
            Err(error) => ForeignDeviceListResult {
                devices: None,
                failure: Some(bleng_failure(
                    ForeignOperationFailureDomain::Operation,
                    error,
                )),
            },
        }
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

#[uniffi::export]
impl SupervisedRefreshGattCache {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignOperationCommandResult::from_failure(Some(*failure)),
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

impl Supervisor {
    pub(in crate::ffi) fn submit_operation<T, E: From<SupervisorOperationError>>(
        &self,
        context: ForeignOwnerContext,
        submit: impl FnOnce(&SessionSupervisor, &RecoveryContext) -> Result<T, E>,
    ) -> Result<T, E> {
        let context = RecoveryContext::try_from(context).map_err(|error| {
            SupervisorOperationError::InvalidContext {
                detail: error.to_string(),
            }
        })?;
        // Begin is a bounded submission boundary, not a wait for another caller's
        // observation/lifecycle lock. Keep this guard through validation + submit,
        // so owner retirement cannot slip between the two. No await occurs here.
        let inner = self.inner.try_lock().map_err(|error| match error {
            TryLockError::WouldBlock => SupervisorOperationError::SupervisorBusy,
            TryLockError::Poisoned(_) => SupervisorOperationError::SupervisorUnavailable,
        })?;
        let supervisor = match &*inner {
            SupervisorState::Available(supervisor) => supervisor,
            SupervisorState::Busy => return Err(SupervisorOperationError::SupervisorBusy.into()),
            SupervisorState::Interrupted => {
                return Err(SupervisorOperationError::SupervisorUnavailable.into());
            }
        };
        submit(supervisor, &context)
    }
}
