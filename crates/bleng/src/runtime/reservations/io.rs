use blehub::{
    BleEvent, BleEventError, CommandId, ConnectionId, GattCharacteristicId, GattSessionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{connection::connection_lost, dispatch::DispatchReservation};

pub(super) struct RssiReservation {
    pub(super) dispatch: DispatchReservation,
    pub(super) value: Option<i16>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

impl RssiReservation {
    pub(super) fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::RssiRead {
                command_id,
                connection_id,
                ..
            } => self.dispatch.command_id == *command_id && expected == *connection_id,
            BleEvent::CommandCompleted { command_id, .. } => {
                self.dispatch.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch.waiter_result(&self.terminal, || {
            self.value
                .map(|value| WaiterSuccess::RssiRead(connection_id, value))
        })
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.dispatch
            .is_complete(&self.terminal, self.value.is_some())
    }
}

pub(super) struct ReadReservation {
    pub(super) dispatch: DispatchReservation,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    pub(super) value: Option<Vec<u8>>,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

impl ReadReservation {
    pub(super) fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CharacteristicRead {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
                ..
            } => {
                self.dispatch.command_id == *command_id
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.dispatch.command_id == *command_id
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
            dispatch: DispatchReservation::new(command_id),
            session_id,
            characteristic_id,
            value: None,
            terminal: None,
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch.waiter_result(&self.terminal, || {
            self.value
                .clone()
                .map(|value| WaiterSuccess::CharacteristicRead {
                    connection_id,
                    gatt_session_id: self.session_id,
                    characteristic_id: self.characteristic_id,
                    value,
                })
        })
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.dispatch
            .is_complete(&self.terminal, self.value.is_some())
    }
}

pub(super) struct WriteReservation {
    pub(super) dispatch: DispatchReservation,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    pub(super) written: bool,
    pub(super) terminal: Option<Result<(), BleEventError>>,
}

impl WriteReservation {
    pub(super) fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CharacteristicWritten {
                command_id,
                connection_id,
                gatt_session_id,
                characteristic_id,
            } => {
                self.dispatch.command_id == *command_id
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.dispatch.command_id == *command_id
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
            dispatch: DispatchReservation::new(command_id),
            session_id,
            characteristic_id,
            written: false,
            terminal: None,
        }
    }

    pub(super) fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch.waiter_result(&self.terminal, || {
            self.written.then_some(WaiterSuccess::CharacteristicWritten(
                connection_id,
                self.session_id,
                self.characteristic_id,
            ))
        })
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.dispatch.is_complete(&self.terminal, self.written)
    }
}
