use blehub::{BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattSessionId};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{connection::connection_lost, dispatch::CommandProgress};

pub(super) struct RssiReservation {
    pub(super) progress: CommandProgress,
    pub(super) value: Option<i16>,
}

impl RssiReservation {
    /// Dispatch timing applies only to buffering, not domain evidence recording.
    pub(super) fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Correlates RSSI data, terminal, or connection loss; not a success predicate.
    pub(super) fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::RssiRead {
                command_id,
                connection_id,
                ..
            } => self.progress.command_id == *command_id && expected == *connection_id,
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::RssiRead(connection_id, self.value?)))
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value.is_some())
    }
}

pub(super) struct ReadReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    pub(super) value: Option<Vec<u8>>,
}

impl ReadReservation {
    /// Dispatch timing applies only to buffering, not domain evidence recording.
    pub(super) fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Correlates read data, terminal, or connection loss; not a success predicate.
    pub(super) fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CharacteristicRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                ..
            } => {
                self.progress.command_id == *command_id
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn new(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            characteristic_id,
            value: None,
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::CharacteristicRead {
            connection_id,
            gatt_session_id: self.session_id,
            characteristic_id: self.characteristic_id,
            value: self.value.clone()?,
        }))
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value.is_some())
    }
}

pub(super) struct WriteReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    pub(super) written: bool,
}

impl WriteReservation {
    /// Dispatch timing applies only to buffering, not domain evidence recording.
    pub(super) fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Correlates write evidence, terminal, or connection loss; not a success predicate.
    pub(super) fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
            } => {
                self.progress.command_id == *command_id
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn new(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            characteristic_id,
            written: false,
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.written
            .then_some(Ok(WaiterSuccess::CharacteristicWritten(
                connection_id,
                self.session_id,
                self.characteristic_id,
            )))
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.written)
    }
}
