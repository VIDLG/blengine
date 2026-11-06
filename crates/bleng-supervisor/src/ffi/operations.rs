//! Native-only, context-fenced submission to the supervisor's current BLeng owner.
//!
//! Each method returns the existing engine handle. Waiting and cancellation do not
//! borrow the supervisor, so neither can block its recovery or shutdown lifecycle.

use std::sync::{Arc, TryLockError};

use blehub::{
    ConnectionId, GattCharacteristicId, GattSessionId, NotificationModeRequest, PeripheralId,
    ScanOptions, ScanSessionId, SubscriptionId, WriteType,
};
use bleng::{
    ConnectOperation, DisableNotificationsOperation, DisconnectOperation, DiscoverGattOperation,
    EnableNotificationsOperation, ReadOperation, ReadRssiOperation, StartScanOperation,
    StopScanOperation, WriteOperation,
};

use super::super::scan_session::{ScanSessionError, SupervisedScan};
use super::super::types::ForeignOwnerContext;
use super::{Supervisor, SupervisorState};
use crate::{RecoveryContext, ScanSubmissionError, SessionSupervisor, SupervisorOperationError};

#[cfg(test)]
#[path = "tests/operations.rs"]
mod tests;

#[uniffi::export]
impl Supervisor {
    /// Creates an owner-bound scan proxy without exposing the original resource IDs to its caller.
    /// The proxy weakly references this Supervisor; retained scans cannot keep its owner open.
    pub fn begin_scan(
        self: Arc<Self>,
        context: ForeignOwnerContext,
        options: ScanOptions,
    ) -> Result<Arc<SupervisedScan>, ScanSessionError> {
        let handoff = self
            .submit_operation::<_, ScanSubmissionError>(context.clone(), |supervisor, context| {
                supervisor.begin_scan(context, options)
            })
            .map_err(ScanSessionError::from)?;
        Ok(SupervisedScan::new(&self, context, handoff))
    }

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

    pub fn begin_connect(
        &self,
        context: ForeignOwnerContext,
        peripheral: PeripheralId,
    ) -> Result<Arc<ConnectOperation>, SupervisorOperationError> {
        self.submit_operation(context, |supervisor, context| {
            supervisor.begin_connect(context, peripheral)
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
