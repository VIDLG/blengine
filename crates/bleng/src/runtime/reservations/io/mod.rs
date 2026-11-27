//! One-shot RSSI/read/write command storage within one connection.
//! GATT database state and subscription routes have separate owners.

use std::collections::HashMap;

use blehub::{
    BleEvent, BondState, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId,
    GattDescriptorId, GattSessionId,
};

use crate::waiter::WaiterOutcome;

use super::{BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution};

mod bond;
mod descriptor_read;
mod descriptor_write;
mod mtu;
mod priority;
mod read;
mod refresh;
mod rssi;
mod write;

use bond::BondReservation;
use descriptor_read::DescriptorReadReservation;
use descriptor_write::DescriptorWriteReservation;
use mtu::MtuReservation;
use priority::PriorityReservation;
use read::ReadReservation;
use refresh::RefreshReservation;
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
    mtu: HashMap<CommandId, MtuReservation>,
    priority: HashMap<CommandId, PriorityReservation>,
    refresh: HashMap<CommandId, RefreshReservation>,
    bond: HashMap<CommandId, BondReservation>,
    read: HashMap<CommandId, ReadReservation>,
    write: HashMap<CommandId, WriteReservation>,
    descriptor_read: HashMap<CommandId, DescriptorReadReservation>,
    descriptor_write: HashMap<CommandId, DescriptorWriteReservation>,
}

impl IoReservations {
    pub(super) fn reserve_rssi(&mut self, command_id: CommandId) {
        self.rssi
            .insert(command_id, RssiReservation::new(command_id));
    }

    pub(super) fn reserve_mtu(&mut self, command_id: CommandId) {
        self.mtu.insert(command_id, MtuReservation::new(command_id));
    }

    pub(super) fn reserve_create_bond(&mut self, command_id: CommandId) {
        self.bond.insert(
            command_id,
            BondReservation::new(command_id, BondState::Bonded),
        );
    }

    pub(super) fn reserve_remove_bond(&mut self, command_id: CommandId) {
        self.bond.insert(
            command_id,
            BondReservation::new(command_id, BondState::None),
        );
    }

    pub(super) fn reserve_set_connection_priority(&mut self, command_id: CommandId) {
        self.priority
            .insert(command_id, PriorityReservation::new(command_id));
    }

    pub(super) fn reserve_refresh_gatt_cache(&mut self, command_id: CommandId) {
        self.refresh
            .insert(command_id, RefreshReservation::new(command_id));
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

    pub(super) fn reserve_descriptor_read(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        descriptor_id: GattDescriptorId,
    ) {
        self.descriptor_read.insert(
            command_id,
            DescriptorReadReservation::new(
                command_id,
                session_id,
                characteristic_id,
                descriptor_id,
            ),
        );
    }

    pub(super) fn reserve_descriptor_write(
        &mut self,
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        descriptor_id: GattDescriptorId,
    ) {
        self.descriptor_write.insert(
            command_id,
            DescriptorWriteReservation::new(
                command_id,
                session_id,
                characteristic_id,
                descriptor_id,
            ),
        );
    }

    pub(super) fn len(&self) -> usize {
        self.rssi.len()
            + self.mtu.len()
            + self.priority.len()
            + self.refresh.len()
            + self.bond.len()
            + self.read.len()
            + self.write.len()
            + self.descriptor_read.len()
            + self.descriptor_write.len()
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
            || self
                .descriptor_read
                .values()
                .any(|slot| slot.characteristic_id == characteristic_id)
            || self
                .descriptor_write
                .values()
                .any(|slot| slot.characteristic_id == characteristic_id)
    }

    pub(super) fn progress_for_cmd(&self, command_id: CommandId) -> Option<&CommandProgress> {
        if let Some(slot) = self.rssi.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.mtu.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.priority.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.refresh.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.bond.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.read.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.write.get(&command_id) {
            return Some(&slot.progress);
        }
        if let Some(slot) = self.descriptor_read.get(&command_id) {
            return Some(&slot.progress);
        }
        self.descriptor_write
            .get(&command_id)
            .map(|slot| &slot.progress)
    }

    pub(super) fn progress_for_cmd_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<&mut CommandProgress> {
        if let Some(slot) = self.rssi.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.mtu.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.priority.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.refresh.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.bond.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.read.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.write.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        if let Some(slot) = self.descriptor_read.get_mut(&command_id) {
            return Some(&mut slot.progress);
        }
        self.descriptor_write
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
        self.mtu.remove(&command_id);
        self.priority.remove(&command_id);
        self.refresh.remove(&command_id);
        self.bond.remove(&command_id);
        self.read.remove(&command_id);
        self.write.remove(&command_id);
        self.descriptor_read.remove(&command_id);
        self.descriptor_write.remove(&command_id);
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

    pub(super) fn pending_mtu_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .mtu
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .mtu
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_priority_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .priority
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .priority
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_refresh_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .refresh
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .refresh
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_bond_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .bond
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .bond
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

    pub(super) fn pending_descriptor_read_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .descriptor_read
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .descriptor_read
                .get_mut(&event.command_id()?)
                .filter(|slot| slot.should_buffer_event(event, connection_id)),
        };
        slot.map(|slot| &mut slot.progress)
    }

    pub(super) fn pending_descriptor_write_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        let slot = match event {
            BleEvent::ConnectionStateChanged { .. } => self
                .descriptor_write
                .values_mut()
                .find(|slot| slot.should_buffer_event(event, connection_id)),
            _ => self
                .descriptor_write
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
            BleEvent::MtuRead { command_id, .. } => {
                if let Some(slot) = self.mtu.get_mut(command_id) {
                    slot.record_event(event, connection_id);
                }
            }
            BleEvent::BondStateChanged { command_id, .. } => {
                if let Some(command_id) = command_id
                    && let Some(slot) = self.bond.get_mut(command_id)
                {
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
            BleEvent::DescriptorRead { command_id, .. } => {
                if let Some(slot) = self.descriptor_read.get_mut(command_id) {
                    slot.record_event(event, connection_id);
                }
            }
            BleEvent::DescriptorWritten { command_id, .. } => {
                if let Some(slot) = self.descriptor_write.get_mut(command_id) {
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
        self.mtu.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.priority.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.refresh.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.bond.retain(|command_id, slot| {
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
        self.descriptor_read.retain(|command_id, slot| {
            let keep = !slot.is_retirable();
            if !keep {
                retired.push(*command_id);
            }
            keep
        });
        self.descriptor_write.retain(|command_id, slot| {
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
        if let Some(slot) = self.mtu.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.priority.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.refresh.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.bond.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.read.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.write.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        if let Some(slot) = self.descriptor_read.get(&command_id) {
            return slot.waiter_result(connection_id);
        }
        self.descriptor_write
            .get(&command_id)
            .and_then(|slot| slot.waiter_result(connection_id))
    }

    pub(super) fn progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        self.rssi
            .values()
            .map(|slot| &slot.progress)
            .chain(self.mtu.values().map(|slot| &slot.progress))
            .chain(self.priority.values().map(|slot| &slot.progress))
            .chain(self.refresh.values().map(|slot| &slot.progress))
            .chain(self.bond.values().map(|slot| &slot.progress))
            .chain(self.read.values().map(|slot| &slot.progress))
            .chain(self.write.values().map(|slot| &slot.progress))
            .chain(self.descriptor_read.values().map(|slot| &slot.progress))
            .chain(self.descriptor_write.values().map(|slot| &slot.progress))
    }
}
