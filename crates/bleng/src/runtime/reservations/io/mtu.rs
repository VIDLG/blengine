use blehub::{BleEvent, CommandId, ConnectionId, MtuInfo};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::super::{CommandProgress, is_connection_lost};

pub(super) struct MtuReservation {
    pub(super) progress: CommandProgress,
    value_evidence: Option<MtuInfo>,
}

impl MtuReservation {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            value_evidence: None,
        }
    }

    /// Records the first correlated MTU snapshot; terminal and loss events only
    /// identify the operation and do not provide its success value.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.value_evidence.is_none()
            && self.matches_related_event(event, expected)
            && let BleEvent::MtuRead { info, .. } = event
        {
            self.value_evidence = Some(*info);
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
        Some(Ok(WaiterSuccess::MtuRead(
            connection_id,
            self.value_evidence?,
        )))
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value_evidence.is_some())
    }

    /// Correlates MTU data, terminal, or connection loss; not a success predicate.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::MtuRead {
                command_id,
                connection_id,
                ..
            } => self.progress.owns_command(*command_id) && expected == *connection_id,
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
