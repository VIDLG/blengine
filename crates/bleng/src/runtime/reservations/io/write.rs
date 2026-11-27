use blehub::{BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattSessionId};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::super::{CommandProgress, is_connection_lost};

pub(super) struct WriteReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    written_evidence: bool,
}

impl WriteReservation {
    pub(super) fn new(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            characteristic_id,
            written_evidence: false,
        }
    }

    /// Records correlated write acknowledgement evidence; the command terminal
    /// remains a separate completion requirement.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.matches_related_event(event, expected)
            && matches!(event, BleEvent::CharacteristicWritten { .. })
        {
            self.written_evidence = true;
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
        self.written_evidence
            .then_some(Ok(WaiterSuccess::CharacteristicWritten(
                connection_id,
                self.session_id,
                self.characteristic_id,
            )))
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.written_evidence)
    }

    /// Correlates write evidence, terminal, or connection loss; not a success predicate.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
            } => {
                self.progress.owns_command(*command_id)
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
