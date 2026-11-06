//! One-shot RSSI/read/write command storage within one connection.
//! GATT database state and subscription routes have separate owners.

use std::collections::HashMap;

use blehub::{
    BleEvent, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId, GattSessionId,
};

use crate::waiter::WaiterOutcome;

use super::{BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution};

mod read;
mod rssi;
mod write;

use read::ReadReservation;
use rssi::RssiReservation;
use write::WriteReservation;

/// Connection-local reservations for one-shot RSSI/read/write commands.
/// This is a command ledger, not the native execution queue.
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

    pub(super) fn progress_for_cmd(&self, command_id: CommandId) -> Option<&CommandProgress> {
        if let Some(slot) = self.rssi.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.read.get(&command_id) {
            return Some(&slot.progress);
        }
        self.write.get(&command_id).map(|slot| &slot.progress)
    }

    pub(super) fn progress_for_cmd_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<&mut CommandProgress> {
        if let Some(slot) = self.rssi.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.read.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        self.write
            .get_mut(&command_id)
            .map(|slot| &mut slot.progress)
    }

    /// Reconciles dispatch before removing a rejected command's evidence owner.
    /// The connection supplies rejection policy; this collection owns entry lifetime.
    /// None means the command is not owned, not that dispatch failed.
    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
        classify_rejection: impl Fn(&BleEvent) -> BufferedEventDisposition,
    ) -> Option<DispatchResolution> {
        let progress = self.progress_for_cmd_mut(command_id)?;
        if result.is_ok() {
            return Some(progress.resolve_accepted_dispatch());
        }

        let resolution =
            progress.resolve_rejected_dispatch(DispatchAction::NoAction, classify_rejection);
        // Command IDs are unique across these maps. Classify and drain callbacks
        // before dropping the entry, which also confirms its completion observer.
        self.rssi.remove(&command_id);
        self.read.remove(&command_id);
        self.write.remove(&command_id);
        Some(resolution)
    }

    pub(super) fn pending_rssi_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        // Loss is connection-scoped even when it carries another command's ID.
        // Other callbacks use the command key, then validate the full identity.
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .rssi
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .rssi
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_read_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .read
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .read
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_write_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .write
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .write
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
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
}
