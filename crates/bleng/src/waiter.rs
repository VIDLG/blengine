//! Actor-owned caller waiters.
//!
//! A waiter is only the caller-facing half of an operation. The actor-owned
//! reservation state is the single source of BLE operation evidence; this
//! registry stores the expectation and delivers the final result exactly once.
//! Retiring a waiter on timeout or cancellation never claims that native BLE
//! work was cancelled. Late callbacks remain the responsibility of the actor's
//! reservations.
//!
//! All registry methods are synchronous and must be called by the owning actor.

use std::collections::HashMap;

use blehub::{
    AdapterSnapshot, BleEventError, CommandDispatchError, CommandId, ConnectionId,
    GattCharacteristicId, GattDatabase, GattSessionId, NotificationMode, SubscriptionId,
};
use tokio::sync::oneshot;

const MAX_PENDING: usize = 64;

/// Identifies the operation-specific result that a caller is waiting for.
///
/// The expectation is deliberately kept with the waiter rather than the native
/// backend. Reservation state owns callback correlation; this value tells the
/// registry which connection-bound waiters must be failed after a passive loss
/// and documents the type of result the actor will send.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum WaiterExpectation {
    #[cfg(test)]
    TerminalOnly,
    Initialized,
    ScanStarted(blehub::ScanSessionId),
    ScanStopped(blehub::ScanSessionId),
    Connected(ConnectionId),
    Disconnected(ConnectionId),
    RssiRead(ConnectionId),
    GattDiscovered(ConnectionId, GattSessionId),
    CharacteristicRead(ConnectionId, GattSessionId, GattCharacteristicId),
    CharacteristicWritten(ConnectionId, GattSessionId, GattCharacteristicId),
    NotificationsEnabled(
        ConnectionId,
        GattSessionId,
        SubscriptionId,
        GattCharacteristicId,
    ),
    NotificationsDisabled(
        ConnectionId,
        GattSessionId,
        SubscriptionId,
        GattCharacteristicId,
    ),
}

impl WaiterExpectation {
    /// Returns the connection whose lifecycle can invalidate this waiter.
    /// Explicit disconnect waiters are excluded: a matching disconnect event
    /// must complete them successfully rather than turn them into errors.
    pub(crate) fn connection_id(self) -> Option<ConnectionId> {
        match self {
            Self::Connected(connection_id)
            | Self::RssiRead(connection_id)
            | Self::GattDiscovered(connection_id, _)
            | Self::CharacteristicRead(connection_id, _, _)
            | Self::CharacteristicWritten(connection_id, _, _)
            | Self::NotificationsEnabled(connection_id, _, _, _)
            | Self::NotificationsDisabled(connection_id, _, _, _) => Some(connection_id),
            #[cfg(test)]
            Self::TerminalOnly => None,
            Self::Initialized
            | Self::ScanStarted(_)
            | Self::ScanStopped(_)
            | Self::Disconnected(_) => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum WaiterSuccess {
    Initialized(AdapterSnapshot),
    ScanStarted(blehub::ScanSessionId),
    ScanStopped(blehub::ScanSessionId),
    Connected(ConnectionId),
    Disconnected(ConnectionId),
    RssiRead(ConnectionId, i16),
    GattDiscovered(GattDatabase),
    CharacteristicRead {
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        value: Vec<u8>,
    },
    CharacteristicWritten(ConnectionId, GattSessionId, GattCharacteristicId),
    NotificationsEnabled {
        connection_id: ConnectionId,
        gatt_session_id: GattSessionId,
        subscription_id: SubscriptionId,
        characteristic_id: GattCharacteristicId,
        actual_mode: Option<NotificationMode>,
    },
    NotificationsDisabled(
        ConnectionId,
        GattSessionId,
        SubscriptionId,
        GattCharacteristicId,
    ),
}

/// Internal operation outcomes at the actor/waiter boundary.
///
/// This type intentionally does not implement `std::error::Error`: it is an
/// actor-internal control result. The public error boundary owns diagnostic
/// chaining and UniFFI conversion.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum WaiterError {
    Closed,
    Cancelled,
    Timeout,
    TransportFault,
    DispatchRejected(CommandDispatchError),
    Operation(BleEventError),
    InvalidCommandId,
    CapacityExceeded,
    ScanBusy,
    ScanNotActive,
    PeripheralBusy,
    ConnectionNotActive,
    GattBusy,
    GattNotReady,
    OperationBusy,
    SubscriptionNotActive,
    Disconnected,
    /// The facade has no attached Hub to which it can dispatch a command.
    BackendNotAssembled,
    /// The attached Hub exists, but initialization is incomplete.
    NotInitialized,
    IdExhausted,
}

/// The only reasons that can permanently close a `WaiterRegistry`.
///
/// Unlike `WaiterError`, this is a registry-wide lifecycle decision: it fails
/// all pending waiters and rejects future registrations with the same reason.
/// Keeping this type separate prevents per-operation errors such as timeout,
/// cancellation, or GATT busy from permanently closing the entire registry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum WaiterCloseReason {
    /// The owner explicitly closed the runtime.
    Closed,
    /// A runtime fault made further operation dispatch or delivery unsafe.
    TransportFault,
}

impl From<WaiterCloseReason> for WaiterError {
    fn from(reason: WaiterCloseReason) -> Self {
        match reason {
            WaiterCloseReason::Closed => Self::Closed,
            WaiterCloseReason::TransportFault => Self::TransportFault,
        }
    }
}

/// Caller-facing waiter entry. BLE evidence intentionally does not live here;
/// the matching actor reservation remains alive after this entry is retired.
struct WaiterEntry {
    expectation: WaiterExpectation,
    result_tx: oneshot::Sender<Result<WaiterSuccess, WaiterError>>,
}

/// Registry local to one actor / BLeng instance.
///
/// The registry owns waiter admission, exactly-once result delivery, timeout /
/// cancellation retirement, passive-disconnect failure, and close failure. It
/// does not parse backend events or own native-operation state.
#[derive(Default)]
pub(crate) struct WaiterRegistry {
    pending: HashMap<CommandId, WaiterEntry>,
    last_id: Option<u64>,
    closed: Option<WaiterCloseReason>,
}

impl WaiterRegistry {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn register(
        &mut self,
        command_id: CommandId,
        expectation: WaiterExpectation,
    ) -> Result<oneshot::Receiver<Result<WaiterSuccess, WaiterError>>, WaiterError> {
        if let Some(reason) = self.closed {
            return Err(reason.into());
        }
        let value: u64 = command_id.into();
        if self.last_id.is_some_and(|last| value <= last) {
            return Err(WaiterError::InvalidCommandId);
        }
        // A caller can cancel its ask before receiving the result receiver. Reap only
        // those abandoned entries; never reuse their command IDs.
        self.pending.retain(|_, entry| !entry.result_tx.is_closed());
        if self.pending.len() >= MAX_PENDING {
            return Err(WaiterError::CapacityExceeded);
        }
        let (result_tx, result_rx) = oneshot::channel();
        self.pending.insert(
            command_id,
            WaiterEntry {
                expectation,
                result_tx,
            },
        );
        self.last_id = Some(value);
        Ok(result_rx)
    }

    /// Settles one waiter exactly once. Removing the entry before sending the
    /// result makes late or duplicate actor attempts harmless.
    pub(crate) fn finish(
        &mut self,
        command_id: CommandId,
        result: Result<WaiterSuccess, WaiterError>,
    ) {
        if let Some(entry) = self.pending.remove(&command_id) {
            let _ = entry.result_tx.send(result);
        }
    }

    /// Fails only connection-bound waiters selected by actor-owned reservations.
    /// The actor supplies accepted command IDs so a passive callback buffered
    /// before dispatch acknowledgement cannot prematurely retire its waiter.
    pub(crate) fn disconnect_commands(&mut self, command_ids: impl IntoIterator<Item = CommandId>) {
        for command_id in command_ids {
            if self
                .pending
                .get(&command_id)
                .is_some_and(|entry| entry.expectation.connection_id().is_some())
            {
                self.finish(command_id, Err(WaiterError::Disconnected));
            }
        }
    }

    /// Retires the waiter only; native work is not cancelled here.
    pub(crate) fn cancel(&mut self, command_id: CommandId) {
        self.finish(command_id, Err(WaiterError::Cancelled));
    }

    /// Called when the owner deadline expires; native work may still be live.
    pub(crate) fn timeout(&mut self, command_id: CommandId) {
        self.finish(command_id, Err(WaiterError::Timeout));
    }

    /// Permanently closes the registry and fails every pending waiter.
    /// The first reason is retained for both existing and future callers;
    /// repeated closes cannot replace it or reopen admission.
    pub(crate) fn close(&mut self, reason: WaiterCloseReason) {
        if self.closed.is_some() {
            return;
        }
        self.closed = Some(reason);
        for (_, entry) in self.pending.drain() {
            let _ = entry.result_tx.send(Err(reason.into()));
        }
    }
}

#[cfg(test)]
#[path = "tests/waiter_tests.rs"]
mod tests;
