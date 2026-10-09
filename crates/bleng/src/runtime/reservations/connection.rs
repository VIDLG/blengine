use std::collections::HashMap;

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{
    dispatch::{
        BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
        ReconciliationFault,
    },
    gatt::{GattAccess, GattReservation},
    io::{ReadReservation, RssiReservation, WriteReservation},
    subscription::{SubscriptionDisposition, SubscriptionReservation},
};

#[derive(Clone, Copy)]
pub(super) enum PreAckRoute {
    Disconnect,
    Discovery,
    Rssi,
    Read,
    Write,
    Subscription,
    Connect,
}

/// A connection owns its GATT session, I/O slots, and subscriptions.
/// Keeping these under one peripheral key prevents cross-device callback routing.
pub(super) struct ConnectionReservation {
    pub(super) progress: CommandProgress,
    pub(super) connection_id: ConnectionId,
    connected: bool,
    disconnected: bool,
    connect_error: Option<BleEventError>,
    pub(super) disconnect: Option<DisconnectReservation>,
    pub(super) gatt: GattReservation,
    pub(super) read: Option<ReadReservation>,
    pub(super) rssi: Option<RssiReservation>,
    pub(super) write: Option<WriteReservation>,
    pub(super) subscriptions: HashMap<SubscriptionId, SubscriptionReservation>,
}

impl ConnectionReservation {
    /// Selects a command awaiting dispatch resolution in the requested category.
    /// Related events include terminals and connection loss, not just success data;
    /// recording checks the domain variant separately. Categories can overlap.
    /// Each reservation combines dispatch timing and correlation; this method
    /// selects a target within the category chosen by the collection's global order.
    /// None means no pending buffer target, not that this connection is unrelated.
    pub(super) fn pre_ack_route(
        &mut self,
        route: PreAckRoute,
        event: &BleEvent,
    ) -> Option<&mut CommandProgress> {
        let connection_id = self.connection_id;
        match route {
            PreAckRoute::Disconnect => self
                .disconnect
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            PreAckRoute::Discovery => self
                .gatt
                .discovery
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            PreAckRoute::Rssi => self
                .rssi
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            PreAckRoute::Read => self
                .read
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            PreAckRoute::Write => self
                .write
                .as_mut()
                .filter(|slot| slot.should_buffer_event(event, connection_id))
                .map(|slot| &mut slot.progress),
            PreAckRoute::Subscription => {
                self.subscriptions
                    .iter_mut()
                    .find_map(|(subscription_id, route)| {
                        if route.should_buffer_event(event, connection_id, *subscription_id) {
                            route.progress_mut()
                        } else {
                            None
                        }
                    })
            }
            PreAckRoute::Connect => {
                if self.should_buffer_connect_event(event) {
                    Some(&mut self.progress)
                } else {
                    None
                }
            }
        }
    }

    /// Buffer broadly before acceptance, but record only exact connection/command
    /// evidence after replay. Collection-wide operation priority stays with the caller.
    fn should_buffer_connect_event(&self, event: &BleEvent) -> bool {
        if !self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        match event {
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                ..
            } => {
                *connection_id == self.connection_id
                    || *command_id == Some(self.progress.command_id)
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.progress.command_id
            }
            _ => false,
        }
    }

    pub(super) fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconciliationFault> {
        let connection_id = self.connection_id;
        match event {
            BleEvent::ConnectionStateChanged {
                command_id, result, ..
            } => {
                match (command_id, result) {
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Connected,
                        },
                    ) if *command_id == self.progress.command_id => {
                        self.connected = true;
                    }
                    (Some(command_id), ConnectionStateResult::Err { error })
                        if *command_id == self.progress.command_id =>
                    {
                        self.connect_error = Some(error.clone());
                    }
                    (None, ConnectionStateResult::Err { .. })
                    | (
                        None,
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) => {
                        self.disconnected = true;
                    }
                    (
                        Some(command_id),
                        ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected,
                        },
                    ) if *command_id == self.progress.command_id
                        || self
                            .disconnect
                            .as_ref()
                            .is_some_and(|slot| slot.progress.command_id == *command_id) =>
                    {
                        self.disconnected = true;
                    }
                    _ => {}
                }
                if let Some(slot) = self.disconnect.as_mut()
                    && slot.matches_related_event(event, connection_id)
                {
                    // Passive errors prove teardown but do not replace the
                    // explicit disconnect command's authoritative terminal.
                    slot.domain = true;
                }
            }
            BleEvent::GattDiscovered { database, .. } => {
                if let Some(discovery) = self.gatt.discovery.as_mut()
                    && discovery.matches_related_event(event, connection_id)
                {
                    // Discovery deliberately keeps the latest correlated draft.
                    discovery.draft_database = Some(database.clone());
                }
            }
            BleEvent::RssiRead { rssi, .. } => {
                if let Some(slot) = self.rssi.as_mut()
                    && slot.matches_related_event(event, connection_id)
                    && slot.value.is_none()
                {
                    slot.value = Some(*rssi);
                }
            }
            BleEvent::CharacteristicRead { value, .. } => {
                if let Some(read) = self.read.as_mut()
                    && read.matches_related_event(event, connection_id)
                    && read.value.is_none()
                {
                    read.value = Some(value.clone());
                }
            }
            BleEvent::CharacteristicWritten { .. } => {
                if let Some(write) = self.write.as_mut()
                    && write.matches_related_event(event, connection_id)
                {
                    write.written = true;
                }
            }
            BleEvent::NotificationStateChanged {
                subscription_id, ..
            } => {
                if let Some(route) = self.subscriptions.get_mut(subscription_id) {
                    route.record_event(event, connection_id, *subscription_id)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn resolve_operation_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let connection_id = self.connection_id;
        // Taking the discovery slot never removes its separately committed database.
        if let Some(resolution) = resolve_pending_slot(
            &mut self.gatt.discovery,
            command_id,
            result,
            connection_id,
            |slot| &mut slot.progress,
        ) {
            return Some(resolution);
        }
        if let Some(resolution) =
            resolve_pending_slot(&mut self.rssi, command_id, result, connection_id, |slot| {
                &mut slot.progress
            })
        {
            return Some(resolution);
        }
        if let Some(resolution) =
            resolve_pending_slot(&mut self.read, command_id, result, connection_id, |slot| {
                &mut slot.progress
            })
        {
            return Some(resolution);
        }
        if let Some(resolution) =
            resolve_pending_slot(&mut self.write, command_id, result, connection_id, |slot| {
                &mut slot.progress
            })
        {
            return Some(resolution);
        }
        if let Some(subscription_id) =
            self.subscriptions
                .iter()
                .find_map(|(subscription_id, route)| {
                    route
                        .progress()
                        .filter(|progress| progress.command_id == command_id)
                        .map(|_| *subscription_id)
                })
        {
            let progress = self
                .subscriptions
                .get_mut(&subscription_id)
                .and_then(SubscriptionReservation::progress_mut)
                .expect("matching transition exists");
            return Some(if result.is_err() {
                // Subscription rejection also permits commanded connection loss.
                let resolution = progress.rejected_resolution(DispatchAction::None, |event| {
                    if connection_lost(event, connection_id) {
                        BufferedEventDisposition::Replay
                    } else {
                        BufferedEventDisposition::Conflict
                    }
                });
                self.reject_subscription_transition(subscription_id);
                resolution
            } else {
                progress.accepted_resolution()
            });
        }
        resolve_pending_slot(
            &mut self.disconnect,
            command_id,
            result,
            connection_id,
            |slot| &mut slot.progress,
        )
    }

    pub(super) fn contains_command(&self, command_id: CommandId) -> bool {
        self.command_progresses()
            .any(|dispatch| dispatch.command_id == command_id)
    }

    fn require_connected(&self) -> Result<(), WaiterError> {
        if !self.connected || self.disconnected || self.has_failed() {
            return Err(WaiterError::ConnectionNotActive);
        }
        Ok(())
    }

    fn has_pending_child_handoff(&self) -> bool {
        self.child_progresses()
            .any(CommandProgress::is_awaiting_dispatch_result)
    }

    fn has_characteristic_io(&self) -> bool {
        self.read.is_some() || self.write.is_some()
    }

    pub(super) fn validate_disconnect(&self) -> Result<(), WaiterError> {
        // An accepted connect owns a native lifecycle even before Connected arrives.
        if self.disconnected || self.has_failed() || self.progress.is_awaiting_dispatch_result() {
            return Err(WaiterError::ConnectionNotActive);
        }
        if self.disconnect.is_some() || self.has_pending_child_handoff() {
            // Pre-dispatch siblings must learn acceptance before passive loss.
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn validate_gatt_discovery(&self) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some()
            || self
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.progress.is_awaiting_dispatch_result())
            || self.gatt.has_pending_discovery()
            || self.has_characteristic_io()
            || !self.subscriptions.is_empty()
        {
            return Err(WaiterError::GattBusy);
        }
        Ok(())
    }

    pub(super) fn validate_rssi(&self) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.rssi.is_some() || self.disconnect.is_some() || self.has_pending_child_handoff() {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    /// Keep error precedence explicit: connection, handoff, target, I/O, discovery.
    pub(super) fn validate_gatt_access(
        &self,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        access: GattAccess,
    ) -> Result<(), WaiterError> {
        self.require_connected()?;
        if self.disconnect.is_some()
            || self
                .rssi
                .as_ref()
                .is_some_and(|slot| slot.progress.is_awaiting_dispatch_result())
        {
            return Err(WaiterError::OperationBusy);
        }
        let database = self
            .gatt
            .committed_database(session_id)
            .ok_or(WaiterError::GattNotReady)?;
        if !database.services.iter().any(|service| {
            service
                .characteristics
                .iter()
                .any(|characteristic| characteristic.id == characteristic_id)
        }) {
            return Err(WaiterError::GattNotReady);
        }
        if self.has_characteristic_io()
            || self
                .subscriptions
                .values()
                .any(SubscriptionReservation::is_transitioning)
            || (matches!(access, GattAccess::Read)
                && self
                    .subscriptions
                    .values()
                    .any(|route| route.characteristic_id == characteristic_id))
        {
            return Err(WaiterError::OperationBusy);
        }
        if self.gatt.has_pending_discovery() {
            return Err(WaiterError::GattBusy);
        }
        // Enable keeps its existing precedence: pending discovery before duplicate route.
        if matches!(access, GattAccess::EnableNotifications)
            && self
                .subscriptions
                .values()
                .any(|route| route.characteristic_id == characteristic_id)
        {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn validate_notification_disable(
        &self,
        session_id: GattSessionId,
        subscription_id: SubscriptionId,
    ) -> Result<GattCharacteristicId, WaiterError> {
        // Subscription identity/activity precedes connection and sibling-busy checks.
        let route = self
            .subscriptions
            .get(&subscription_id)
            .filter(|route| route.session_id == session_id && route.is_active())
            .ok_or(WaiterError::SubscriptionNotActive)?;
        if route.is_transitioning() {
            return Err(WaiterError::OperationBusy);
        }
        self.require_connected()?;
        if self.disconnect.is_some()
            || self.gatt.has_pending_discovery()
            || self.has_characteristic_io()
            || self.has_pending_child_handoff()
        {
            return Err(WaiterError::OperationBusy);
        }
        Ok(route.characteristic_id)
    }

    pub(super) fn new(command_id: CommandId, connection_id: ConnectionId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            connection_id,
            connected: false,
            disconnected: false,
            connect_error: None,
            disconnect: None,
            gatt: GattReservation::default(),
            read: None,
            rssi: None,
            write: None,
            subscriptions: HashMap::new(),
        }
    }

    /// Rejected disable restores Active; rejected enable removes its provisional route.
    fn reject_subscription_transition(&mut self, subscription_id: SubscriptionId) {
        let route = self
            .subscriptions
            .get_mut(&subscription_id)
            .expect("matching subscription exists");
        if matches!(route.reject_transition(), SubscriptionDisposition::Remove) {
            self.subscriptions.remove(&subscription_id);
        }
    }

    fn has_failed(&self) -> bool {
        self.connect_error.is_some()
    }

    /// Called after waiter settlement, even when the caller already retired.
    pub(super) fn retire_completed_operations(&mut self) {
        self.subscriptions
            .retain(|_, route| matches!(route.retire_transition(), SubscriptionDisposition::Keep));
        if self.disconnect.as_ref().is_some_and(|slot| {
            !slot.progress.is_awaiting_dispatch_result() && slot.progress.terminal_failed()
        }) {
            self.disconnect = None;
        }
        if self
            .rssi
            .as_ref()
            .is_some_and(RssiReservation::is_retirable)
        {
            self.rssi = None;
        }
        if self
            .read
            .as_ref()
            .is_some_and(ReadReservation::is_retirable)
        {
            self.read = None;
        }
        if self
            .write
            .as_ref()
            .is_some_and(WriteReservation::is_retirable)
        {
            self.write = None;
        }
        self.gatt.retire_completed_discovery();
    }

    /// A disconnected connection cannot be removed until all dispatch handoffs
    /// have resolved; an explicit disconnect also needs its own terminal/domain.
    pub(super) fn is_retirable(&self) -> bool {
        let pending_disconnect = self.disconnect.as_ref().is_some_and(|slot| {
            slot.progress.is_awaiting_dispatch_result()
                || !slot.progress.has_terminal()
                || !slot.domain
        });
        !pending_disconnect
            && !self
                .command_progresses()
                .any(CommandProgress::is_awaiting_dispatch_result)
            && (self.disconnected || self.has_failed() || self.progress.terminal_failed())
    }

    fn connect_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.progress.is_awaiting_dispatch_result() {
            return None;
        }
        if let Some(error) = &self.connect_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if self.disconnected {
            return Some(Err(WaiterError::Disconnected));
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.connected
            .then_some(Ok(WaiterSuccess::Connected(self.connection_id)))
    }

    /// Queries without consuming evidence; None means unowned or incomplete.
    /// Waiter settlement must precede any retirement that releases this evidence.
    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.progress.command_id == command_id {
            return self.connect_result();
        }
        if let Some(disconnect) = self
            .disconnect
            .as_ref()
            .filter(|disconnect| disconnect.progress.command_id == command_id)
        {
            return disconnect.waiter_result(self.connection_id);
        }
        if let Some(gatt) = self
            .gatt
            .discovery
            .as_ref()
            .filter(|gatt| gatt.progress.command_id == command_id)
        {
            return gatt.waiter_result();
        }
        if let Some(rssi) = self
            .rssi
            .as_ref()
            .filter(|rssi| rssi.progress.command_id == command_id)
        {
            return rssi.waiter_result(self.connection_id);
        }
        if let Some(read) = self
            .read
            .as_ref()
            .filter(|read| read.progress.command_id == command_id)
        {
            return read.waiter_result(self.connection_id);
        }
        if let Some(write) = self
            .write
            .as_ref()
            .filter(|write| write.progress.command_id == command_id)
        {
            return write.waiter_result(self.connection_id);
        }
        self.subscriptions
            .iter()
            .find_map(|(subscription_id, route)| {
                route.waiter_result(command_id, self.connection_id, *subscription_id)
            })
    }

    /// Returns command ownership, including duplicates, not operation success.
    /// The owning progress preserves the first terminal.
    pub(super) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if let Some(progress) = self
            .command_progresses_mut()
            .find(|progress| progress.command_id == command_id)
        {
            progress.record_terminal(result);
            return true;
        }
        false
    }

    pub(super) fn accepted_command_ids(&self) -> Vec<CommandId> {
        self.command_progresses()
            .filter(|dispatch| !dispatch.is_awaiting_dispatch_result())
            .map(|dispatch| dispatch.command_id)
            .collect()
    }

    /// One inventory of child commands supports admission and lifecycle accounting.
    fn child_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        [
            self.disconnect.as_ref().map(|slot| &slot.progress),
            self.gatt.discovery.as_ref().map(|slot| &slot.progress),
            self.rssi.as_ref().map(|slot| &slot.progress),
            self.read.as_ref().map(|slot| &slot.progress),
            self.write.as_ref().map(|slot| &slot.progress),
        ]
        .into_iter()
        .flatten()
        .chain(
            self.subscriptions
                .values()
                .filter_map(SubscriptionReservation::progress),
        )
    }

    fn command_progresses(&self) -> impl Iterator<Item = &CommandProgress> {
        std::iter::once(&self.progress).chain(self.child_progresses())
    }

    fn command_progresses_mut(&mut self) -> impl Iterator<Item = &mut CommandProgress> {
        [
            Some(&mut self.progress),
            self.disconnect.as_mut().map(|slot| &mut slot.progress),
            self.gatt.discovery.as_mut().map(|slot| &mut slot.progress),
            self.rssi.as_mut().map(|slot| &mut slot.progress),
            self.read.as_mut().map(|slot| &mut slot.progress),
            self.write.as_mut().map(|slot| &mut slot.progress),
        ]
        .into_iter()
        .flatten()
        .chain(
            self.subscriptions
                .values_mut()
                .filter_map(SubscriptionReservation::progress_mut),
        )
    }
}

pub(super) struct DisconnectReservation {
    pub(super) progress: CommandProgress,
    pub(super) domain: bool,
}

impl DisconnectReservation {
    /// Dispatch timing applies only to buffering, not domain evidence recording.
    fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Matches related disconnect, terminal, and passive failure events, not success.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::ConnectionStateChanged {
                command_id,
                connection_id,
                result:
                    ConnectionStateResult::Ok {
                        state: ConnectionState::Disconnected,
                    },
            } => {
                *connection_id == expected
                    && (command_id.is_none() || *command_id == Some(self.progress.command_id))
            }
            BleEvent::ConnectionStateChanged {
                command_id: None,
                connection_id,
                result: ConnectionStateResult::Err { .. },
            } => *connection_id == expected,
            BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.progress.command_id
            }
            _ => false,
        }
    }

    fn waiter_result(
        &self,
        connection_id: ConnectionId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        self.domain
            .then_some(Ok(WaiterSuccess::Disconnected(connection_id)))
    }
}

pub fn connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            connection_id,
            result: ConnectionStateResult::Ok { state: ConnectionState::Disconnected },
            ..
        } if *connection_id == expected
    ) || matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            connection_id,
            result: ConnectionStateResult::Err { .. },
        } if *connection_id == expected
    )
}

fn passive_connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
    matches!(
        event,
        BleEvent::ConnectionStateChanged {
            command_id: None,
            ..
        }
    ) && connection_lost(event, expected)
}

/// Ordinary child commands discard only their pending slot on rejection and
/// permit passive loss replay. Subscriptions deliberately use a different policy.
fn resolve_pending_slot<T>(
    slot: &mut Option<T>,
    command_id: CommandId,
    result: &Result<(), CommandDispatchError>,
    connection_id: ConnectionId,
    progress: impl FnOnce(&mut T) -> &mut CommandProgress,
) -> Option<DispatchResolution> {
    let command = progress(slot.as_mut()?);
    if command.command_id != command_id {
        return None;
    }
    Some(if result.is_err() {
        let resolution = command.rejected_resolution(DispatchAction::None, |event| {
            if passive_connection_lost(event, connection_id) {
                BufferedEventDisposition::Replay
            } else {
                BufferedEventDisposition::Conflict
            }
        });
        *slot = None;
        resolution
    } else {
        command.accepted_resolution()
    })
}
