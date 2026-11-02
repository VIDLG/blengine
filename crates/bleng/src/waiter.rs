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

use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

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
    SubmissionQueueFull,
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
    OperationNotFound(CommandId),
    OperationAlreadyCompleted(CommandId),
    OperationNotCancellable(CommandId),
    /// A typed operation's one-shot result receiver was already consumed.
    OperationResultConsumed,
    /// The attached Hub exists, but initialization is incomplete.
    NotInitialized,
    IdExhausted,
}

/// One actor-to-caller result; not evidence that native BLE work has retired.
pub(crate) type WaiterOutcome = Result<WaiterSuccess, WaiterError>;

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

impl WaiterError {
    /// Maps the backend receipt for an explicit cancellation request.
    /// Ordinary operation dispatches must keep using `DispatchRejected`.
    pub(crate) fn from_cancellation_dispatch(error: CommandDispatchError) -> Self {
        match error {
            CommandDispatchError::OperationNotFound { target_command_id } => {
                Self::OperationNotFound(target_command_id)
            }
            CommandDispatchError::OperationAlreadyCompleted { target_command_id } => {
                Self::OperationAlreadyCompleted(target_command_id)
            }
            CommandDispatchError::OperationNotCancellable { target_command_id } => {
                Self::OperationNotCancellable(target_command_id)
            }
            error => Self::DispatchRejected(error),
        }
    }
}

/// Shared access to one sender, never a second delivery channel. Local queued
/// cancellation and actor settlement compete to take it exactly once.
#[derive(Clone)]
pub(crate) struct WaiterSender(Arc<Mutex<Option<oneshot::Sender<WaiterOutcome>>>>);

impl WaiterSender {
    pub(crate) fn new() -> (Self, oneshot::Receiver<WaiterOutcome>) {
        let (sender, receiver) = oneshot::channel();
        (Self(Arc::new(Mutex::new(Some(sender)))), receiver)
    }

    pub(crate) fn send(&self, result: WaiterOutcome) {
        if let Some(sender) = self.0.lock().expect("waiter sender lock").take() {
            let _ = sender.send(result);
        }
    }

    pub(crate) fn is_closed(&self) -> bool {
        self.0
            .lock()
            .expect("waiter sender lock")
            .as_ref()
            .is_none_or(oneshot::Sender::is_closed)
    }
}

/// Caller-facing waiter entry. BLE evidence intentionally does not live here;
/// the matching actor reservation remains alive after this entry is retired.
struct WaiterEntry {
    expectation: WaiterExpectation,
    result_tx: WaiterSender,
    // The BLE operation can finish while its cancellation request is still awaiting
    // a backend receipt. For example, a read succeeds, then the connection is lost:
    // if cancellation is rejected, wait() must receive that original read result,
    // not a Disconnected result recomputed from the now-changed connection state.
    // If cancellation is accepted, the waiter receives Cancelled instead.
    deferred_result: Option<WaiterOutcome>,
}

/// Registry local to one actor / BLeng instance.
///
/// The registry owns waiter admission, exactly-once result delivery, timeout /
/// cancellation retirement, passive-disconnect failure, and close failure. It
/// does not parse backend events or own native-operation state.
#[derive(Default)]
pub(crate) struct WaiterRegistry {
    pending: HashMap<CommandId, WaiterEntry>,
    closed: Option<WaiterCloseReason>,
}

impl WaiterRegistry {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    #[cfg(test)]
    pub(crate) fn register(
        &mut self,
        command_id: CommandId,
        expectation: WaiterExpectation,
    ) -> Result<oneshot::Receiver<WaiterOutcome>, WaiterError> {
        let (result_tx, result_rx) = WaiterSender::new();
        self.register_sender(command_id, expectation, result_tx)?;
        Ok(result_rx)
    }

    pub(crate) fn register_sender(
        &mut self,
        command_id: CommandId,
        expectation: WaiterExpectation,
        result_tx: WaiterSender,
    ) -> Result<(), WaiterError> {
        let error = if let Some(reason) = self.closed {
            Some(reason.into())
        } else if command_id.value == 0 || self.pending.contains_key(&command_id) {
            Some(WaiterError::InvalidCommandId)
        } else {
            self.pending.retain(|_, entry| !entry.result_tx.is_closed());
            (self.pending.len() >= MAX_PENDING).then_some(WaiterError::CapacityExceeded)
        };
        if let Some(error) = error {
            // The submission owner linearizes admission rejection versus local
            // cancellation before publishing on this shared sender.
            return Err(error);
        }
        self.pending.insert(
            command_id,
            WaiterEntry {
                expectation,
                result_tx,
                deferred_result: None,
            },
        );
        Ok(())
    }

    /// Removes an admission draft without publishing an outcome. Its submission
    /// still owns the shared sender and arbitrates rejection versus cancellation.
    pub(crate) fn discard(&mut self, command_id: CommandId) {
        self.pending.remove(&command_id);
    }

    pub(crate) fn contains(&self, command_id: CommandId) -> bool {
        self.pending.contains_key(&command_id)
    }

    /// Retains the first complete outcome while native cancellation is undecided.
    /// This uses the existing bounded waiter entry and original result channel;
    /// timeout, caller retirement and close still remove the entire entry.
    pub(crate) fn defer_result(&mut self, command_id: CommandId, result: WaiterOutcome) {
        if let Some(entry) = self.pending.get_mut(&command_id) {
            entry.deferred_result.get_or_insert(result);
        }
    }

    /// Releases a retained outcome after cancellation rejection, without looking
    /// at connection state that may have changed since completion.
    pub(crate) fn try_finish_deferred(&mut self, command_id: CommandId) -> bool {
        let Some(result) = self
            .pending
            .get_mut(&command_id)
            .and_then(|entry| entry.deferred_result.take())
        else {
            return false;
        };
        self.finish(command_id, result);
        true
    }

    /// Settles one waiter exactly once. Removing the entry before sending the
    /// result makes late or duplicate actor attempts harmless.
    pub(crate) fn finish(&mut self, command_id: CommandId, result: WaiterOutcome) {
        if let Some(entry) = self.pending.remove(&command_id) {
            entry.result_tx.send(result);
        }
    }

    /// Fails only connection-bound waiters selected by actor-owned reservations.
    /// The actor supplies accepted command IDs so a passive callback buffered
    /// before dispatch acknowledgement cannot prematurely retire its waiter.
    /// A previously completed outcome waiting for its control receipt survives;
    /// a cancellation request without a result does not delay loss.
    pub(crate) fn disconnect_commands(&mut self, command_ids: impl IntoIterator<Item = CommandId>) {
        for command_id in command_ids {
            if self.pending.get(&command_id).is_some_and(|entry| {
                entry.expectation.connection_id().is_some() && entry.deferred_result.is_none()
            }) {
                self.finish(command_id, Err(WaiterError::Disconnected));
            }
        }
    }

    /// Retires the waiter only; native work is not cancelled here.
    pub(crate) fn cancel(&mut self, command_id: CommandId) {
        self.finish(command_id, Err(WaiterError::Cancelled));
    }

    /// Called when the owner deadline expires; native work may still be live.
    #[cfg(test)]
    pub(crate) fn timeout(&mut self, command_id: CommandId) {
        self.finish(command_id, Err(WaiterError::Timeout));
    }

    /// Normal close preserves completed outcomes still awaiting control receipts
    /// and fails unfinished waiters. Transport faults override all retained results.
    /// The first reason is retained for both existing and future callers;
    /// repeated closes cannot replace it or reopen admission.
    pub(crate) fn close(&mut self, reason: WaiterCloseReason) {
        if self.closed.is_some() {
            return;
        }
        self.closed = Some(reason);
        for (_, entry) in self.pending.drain() {
            let result = match reason {
                WaiterCloseReason::Closed => {
                    entry.deferred_result.unwrap_or(Err(WaiterError::Closed))
                }
                WaiterCloseReason::TransportFault => Err(WaiterError::TransportFault),
            };
            entry.result_tx.send(result);
        }
    }
}

#[cfg(test)]
#[path = "tests/waiter_tests.rs"]
mod tests;
