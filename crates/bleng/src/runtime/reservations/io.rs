use std::collections::HashMap;

use blehub::{BleEvent, CommandId, ConnectionId, GattCharacteristicId, GattSessionId};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::{connection::connection_lost, dispatch::CommandProgress};

pub(super) struct RssiReservation {
    pub(super) progress: CommandProgress,
    value_evidence: Option<i16>,
}

impl RssiReservation {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            value_evidence: None,
        }
    }

    /// Records the first correlated RSSI value; terminal and loss events only
    /// identify the operation and do not provide its success value.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.value_evidence.is_none()
            && self.matches_related_event(event, expected)
            && let BleEvent::RssiRead { rssi, .. } = event
        {
            self.value_evidence = Some(*rssi);
        }
    }

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
            } => self.progress.owns_command(*command_id) && expected == *connection_id,
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => connection_lost(event, expected),
        }
    }

    pub(super) fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::RssiRead(
            connection_id,
            self.value_evidence?,
        )))
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value_evidence.is_some())
    }
}

pub(super) struct ReadReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    value_evidence: Option<Vec<u8>>,
}

impl ReadReservation {
    /// Records the first correlated characteristic value; terminal and loss
    /// events do not provide the successful read payload.
    pub(super) fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.value_evidence.is_none()
            && self.matches_related_event(event, expected)
            && let BleEvent::CharacteristicRead { value, .. } = event
        {
            self.value_evidence = Some(value.clone());
        }
    }

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
                self.progress.owns_command(*command_id)
                    && self.session_id == *gatt_session_id
                    && self.characteristic_id == *characteristic_id
                    && expected == *connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
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
            value_evidence: None,
        }
    }

    pub(super) fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::CharacteristicRead {
            connection_id,
            gatt_session_id: self.session_id,
            characteristic_id: self.characteristic_id,
            value: self.value_evidence.clone()?,
        }))
    }
    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.value_evidence.is_some())
    }
}

pub(super) struct WriteReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
    written_evidence: bool,
}

impl WriteReservation {
    /// Records correlated write acknowledgement evidence. The terminal event
    /// remains separate because it describes command dispatch, not the domain.
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

    /// Correlates write evidence, terminal, or connection loss; not a success predicate.
    pub(super) fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
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
            written_evidence: false,
        }
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
}

/// The maintained Apple and Android native queues currently use this per-connection
/// bound. Subscription transitions are counted by `ConnectionReservation` because
/// they are stored alongside their long-lived subscription route.
pub(super) const MAX_PENDING_NATIVE_OPERATIONS_PER_CONNECTION: usize = 1_024;

/// Connection-local storage for queued GATT child operations.
///
/// The maps are keyed by command ID so each accepted child keeps independent
/// evidence, cancellation, routing, and retirement even while native execution
/// remains FIFO per connection.
#[derive(Default)]
pub(super) struct IoReservations {
    rssi: HashMap<CommandId, RssiReservation>,
    read: HashMap<CommandId, ReadReservation>,
    write: HashMap<CommandId, WriteReservation>,
}

impl IoReservations {
    pub(super) fn reserve_rssi(&mut self, command_id: CommandId) {
        self.rssi
            .insert(command_id, RssiReservation::new(command_id));
    }

    pub(super) fn reserve_read(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.read.insert(
            command_id,
            ReadReservation::new(command_id, session_id, characteristic_id),
        );
    }

    pub(super) fn reserve_write(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) {
        self.write.insert(
            command_id,
            WriteReservation::new(command_id, session_id, characteristic_id),
        );
    }

    pub(super) fn len(&self) -> usize {
        self.rssi.len() + self.read.len() + self.write.len()
    }

    pub(super) fn has_characteristic_operation_for(
        &self,
        characteristic_id: GattCharacteristicId,
    ) -> bool {
        self.read
            .values()
            .any(|slot| slot.characteristic_id == characteristic_id)
            || self
                .write
                .values()
                .any(|slot| slot.characteristic_id == characteristic_id)
    }

    pub(super) fn rssi_is_awaiting_dispatch(&self) -> bool {
        self.rssi
            .values()
            .any(|slot| slot.progress.is_awaiting_dispatch_result())
    }

    pub(super) fn rssi_mut(&mut self, command_id: CommandId) -> Option<&mut RssiReservation> {
        self.rssi.get_mut(&command_id)
    }

    pub(super) fn read_mut(&mut self, command_id: CommandId) -> Option<&mut ReadReservation> {
        self.read.get_mut(&command_id)
    }

    pub(super) fn write_mut(&mut self, command_id: CommandId) -> Option<&mut WriteReservation> {
        self.write.get_mut(&command_id)
    }

    pub(super) fn remove_rssi(&mut self, command_id: CommandId) {
        self.rssi.remove(&command_id);
    }

    pub(super) fn remove_read(&mut self, command_id: CommandId) {
        self.read.remove(&command_id);
    }

    pub(super) fn remove_write(&mut self, command_id: CommandId) {
        self.write.remove(&command_id);
    }

    pub(super) fn pending_rssi_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut RssiReservation> {
        self.rssi
            .values_mut()
            .find(|slot| slot.should_buffer_event(event, connection_id))
    }

    pub(super) fn pending_read_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut ReadReservation> {
        self.read
            .values_mut()
            .find(|slot| slot.should_buffer_event(event, connection_id))
    }

    pub(super) fn pending_write_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut WriteReservation> {
        self.write
            .values_mut()
            .find(|slot| slot.should_buffer_event(event, connection_id))
    }

    pub(super) fn record_event(&mut self, event: &BleEvent, connection_id: ConnectionId) {
        match event {
            BleEvent::RssiRead { command_id, .. } => {
                if let Some(slot) = self.rssi.get_mut(command_id) {
                    slot.record_event(event, connection_id);
                }
            }
            BleEvent::CharacteristicRead { command_id, .. } => {
                if let Some(slot) = self.read.get_mut(command_id) {
                    slot.record_event(event, connection_id);
                }
            }
            BleEvent::CharacteristicWritten { command_id, .. } => {
                if let Some(slot) = self.write.get_mut(command_id) {
                    slot.record_event(event, connection_id);
                }
            }
            _ => {}
        }
    }

    pub(super) fn retire_completed(&mut self) -> Vec<CommandId> {
        let mut retired = Vec::new();
        self.rssi.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.read.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.write.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        retired
    }

    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
        connection_id: ConnectionId,
    ) -> Option<WaiterOutcome> {
        if let Some(slot) = self.rssi.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.read.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        self.write
            .get(&command_id)
            .and_then(|slot| slot.waiter_result(connection_id))
    }

    pub(super) fn progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        self.rssi
            .values()
            .map(|slot| &slot.progress)
            .chain(self.read.values().map(|slot| &slot.progress))
            .chain(self.write.values().map(|slot| &slot.progress))
    }

    pub(super) fn progresses_mut(&mut self) -> impl Iterator<Item = &mut CommandProgress> {
        self.rssi
            .values_mut()
            .map(|slot| &mut slot.progress)
            .chain(self.read.values_mut().map(|slot| &mut slot.progress))
            .chain(self.write.values_mut().map(|slot| &mut slot.progress))
    }
}
