use blehub::{BleEvent, BondState, CommandId, ConnectionId};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::super::{CommandProgress, is_connection_lost};

/// One bounded bond (pairing) request for a connection.
///
/// `Bonding` is progress, not a result: the reservation only records a terminal `Bonded` state as
/// its success value. A platform-reported failure, or a connection loss, ends the operation through
/// the terminal/loss paths that the command progress already owns.
pub(super) struct BondReservation {
    pub(super) progress: CommandProgress,
    success_state: BondState,
    value_evidence: Option<BondState>,
}

impl BondReservation {
    pub(super) fn new(command_id: CommandId, success_state: BondState) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            success_state,
            value_evidence: None,
        }
    }

    /// Records only the direction's terminal state; an interim `Bonding` is never evidence.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.value_evidence.is_some() || !self.matches_related_event(event, expected) {
            return;
        }
        if let BleEvent::BondStateChanged { state, .. } = event
            && *state == self.success_state
        {
            self.value_evidence = Some(*state);
        }
    }

    /// Dispatch timing applies only to buffering, not domain evidence recording.
    pub(super) fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    pub(super) fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::BondState(
            connection_id,
            self.value_evidence?,
        )))
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value_evidence.is_some())
    }

    /// Correlates bond state, terminal, or connection loss; not a success predicate.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::BondStateChanged {
                command_id,
                connection_id,
                ..
            } => {
                command_id.is_some_and(|command_id| self.progress.owns_command(command_id))
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
