use blehub::{
    BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattDescriptorId, GattSessionId,
};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::super::{CommandProgress, is_connection_lost};

pub(super) struct DescriptorReadReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    pub(super) descriptor_id: GattDescriptorId,
    value_evidence: Option<Vec<u8>>,
}

impl DescriptorReadReservation {
    pub(super) fn new(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        descriptor_id: GattDescriptorId,
    ) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            characteristic_id,
            descriptor_id,
            value_evidence: None,
        }
    }

    /// Records the first correlated descriptor value; terminal and loss events do not carry it.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.value_evidence.is_none()
            && self.matches_related_event(event, expected)
            && let BleEvent::DescriptorRead { value, .. } = event
        {
            self.value_evidence = Some(value.clone());
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
        Some(Ok(WaiterSuccess::DescriptorRead {
            connection_id,
            gatt_session_id: self.session_id,
            characteristic_id: self.characteristic_id,
            descriptor_id: self.descriptor_id,
            value: self.value_evidence.clone()?,
        }))
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value_evidence.is_some())
    }

    /// Correlates read data, terminal, or connection loss; not a success predicate.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::DescriptorRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                descriptor_id,
                ..
            } => {
                self.progress.owns_command(*command_id)
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && self.descriptor_id == *descriptor_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
