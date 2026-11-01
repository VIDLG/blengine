//! Reservation data model shared by command admission and event reconciliation.
//!
//! These actor-confined values record dispatch acknowledgements and correlated
//! native evidence. They own neither runtime lifecycle, caller waiters, executors,
//! nor foreign cleanup. Their shared completion bit is observation only; the actor
//! remains the sole authority for evidence, retirement and caller delivery.
//!
//! Implementation modules stay private. Their public owner APIs are exposed only
//! through the runtime-scoped re-exports below; shared implementation details use
//! `pub(super)`, and record-local fields and helpers remain private.
//!
//! Read `dispatch` first: `CommandProgress` owns acknowledgement, bounded buffering,
//! and the authoritative first terminal; typed domain evidence stays in each record.
//! Follow it with
//! `initialization` and `scan`. `connections` owns the peripheral-keyed collection
//! and global dispatch-pending routing priority; command-correlated lookup is
//! provided by the actor command directory. `connection` owns one connection record
//! and its operation reconciliation. Then read the subordinate `gatt`, `io`, and
//! `subscription` records. Collection ownership is distinct from record-local
//! evidence and retirement; command route metadata belongs to the actor directory.
//! The collection resolves identities; connection-local validators preserve each operation's error
//! precedence and share child-command submission facts, not a universal busy policy. `ReservationCoordinator`
//! is the actor-facing aggregate: it owns the three roots and centralizes route-directed
//! coordination, while each root remains the authority for its own domain evidence.
//!
//! A connection always owns its GATT state; committed database and discovery are
//! independently optional so rediscovery can preserve the old database. Subscription
//! routes use Enabling/Active/Disabling states; rejection and retirement explicitly
//! choose Keep/Remove, and failed disable restores the active route.
//!
//! Commands win the caller submission before installing a reservation, then spawn
//! native dispatch without an intervening await. Losing cancellation creates
//! no reservation to roll back. Accepted native work outlives caller waiters.
//!
//! Live callbacks first pass through gate_event(), before public publication or
//! evidence recording. This is a dispatch-evidence gate, not an ownership query
//! or a filter: Ok(Some(event)) passes both unclaimed callbacks and owned callbacks
//! whose dispatch result no longer requires buffering. Ok(None) means the event
//! was retained for ordered replay, not discarded. Err means overflow or conflicting
//! evidence; the actor faults the runtime. Dispatch resolution transfers buffered
//! callbacks for replay directly into normal processing, without gating them again.
//!
//! Result APIs answer different questions. resolve_dispatch(None) means no matching
//! reservation; record_terminal() returns ownership where routing needs it, not
//! operation success. waiter_result(None) can mean either unowned or incomplete;
//! a present result is deliverable but does not itself advance or retire state.
//! Leaf waiter_result() methods without a command ID assume the caller already
//! selected the owning record. None must not be treated as an admission failure.
//!
//! Events record correlated evidence, settle affected waiters, then retire or
//! commit records. Retirement must depend on owned evidence, not an unrelated
//! raw event, and must retain incomplete start/dispatch evidence for late callbacks.
//! Failed rediscovery preserves the committed database. Discovery is admitted
//! only after all subscriptions are disabled; database preservation alone cannot
//! prove that native subscription routes survived a platform rediscovery.
//!

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId,
    GattSessionId, NotificationModeRequest, PeripheralId, ScanSessionId, SubscriptionId,
};

use crate::{
    runtime::shared::CompletionObserver,
    waiter::{WaiterError, WaiterOutcome},
};

use super::{CommandDirectory, CommandRoute};

mod connection;
mod connections;
mod dispatch;
mod gatt;
mod initialization;
mod io;
mod scan;
mod subscription;

#[cfg(test)]
mod tests;

pub(super) use connection::connection_lost;
pub(super) use connections::ConnectionReservations;
pub(super) use dispatch::{
    CancellationDecision, CancellationReceiver, DispatchAction, DispatchResolution,
    ReconciliationFault,
};
pub(super) use gatt::GattAccess;
pub(super) use initialization::InitializationState;
pub(super) use scan::ScanState;

/// The command directory and three actor-owned reservation roots.
///
/// Domain-specific admission and evidence rules remain on `InitializationState`,
/// `ScanState`, and `ConnectionReservations`. This coordinator owns their shared
/// command lifecycle and provides command/event operations that select one root,
/// so the actor does not repeat the same `CommandRoute` match in several methods.
#[derive(Default)]
pub(super) struct ReservationCoordinator {
    commands: CommandDirectory,
    initialization: InitializationState,
    scan: ScanState,
    connections: ConnectionReservations,
}

impl ReservationCoordinator {
    delegate::delegate! {
        to self.commands {

            #[call(insert)]
            pub(super) fn register_command(&mut self, command_id: CommandId, route: CommandRoute);
            #[call(set_completion)]
            pub(super) fn set_command_completion(
                &mut self,
                command_id: CommandId,
                completion: CompletionObserver,
            );
            #[call(completion)]
            pub(super) fn command_completion(
                &self,
                command_id: CommandId,
            ) -> Option<CompletionObserver>;
            #[call(remove_many)]
            pub(super) fn remove_commands(&mut self, command_ids: impl IntoIterator<Item = CommandId>);
        }

        to self.initialization {
            pub(super) fn require_initialized(&self) -> Result<(), WaiterError>;
            pub(super) fn require_not_started(&self) -> Result<(), WaiterError>;
            pub(super) fn reserve_initialize(&mut self, command_id: CommandId);
        }

        to self.connections {
            pub(super) fn is_foreign_disconnect(&self, event: &BleEvent) -> bool;
            pub(super) fn accepted_command_ids(&self, connection_id: ConnectionId) -> Vec<CommandId>;
            pub(super) fn disconnect_command(&self, connection_id: ConnectionId) -> Option<CommandId>;
            pub(super) fn require_peripheral_available(&self, peripheral_id: PeripheralId) -> Result<(), WaiterError>;
            pub(super) fn validate_disconnect(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;
            pub(super) fn validate_gatt_discovery(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;
            pub(super) fn validate_rssi(&self, connection_id: ConnectionId) -> Result<PeripheralId, WaiterError>;
            pub(super) fn validate_gatt_access(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                access: GattAccess,
            ) -> Result<PeripheralId, WaiterError>;
            pub(super) fn validate_notification_enable(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            ) -> Result<PeripheralId, WaiterError>;
            pub(super) fn validate_notification_disable(
                &self,
                connection_id: ConnectionId,
                session_id: GattSessionId,
                subscription_id: SubscriptionId,
            ) -> Result<(PeripheralId, GattCharacteristicId), WaiterError>;
            pub(super) fn reserve_connection(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                connection_id: ConnectionId,
            );
            pub(super) fn reserve_disconnect(&mut self, peripheral_id: PeripheralId, command_id: CommandId);
            pub(super) fn reserve_gatt_discovery(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
            );
            pub(super) fn reserve_rssi(&mut self, peripheral_id: PeripheralId, command_id: CommandId);
            pub(super) fn reserve_read(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );
            pub(super) fn reserve_write(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
            );
            pub(super) fn reserve_notification_enable(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                subscription_id: SubscriptionId,
                session_id: GattSessionId,
                characteristic_id: GattCharacteristicId,
                mode: NotificationModeRequest,
            );
            pub(super) fn reserve_notification_disable(
                &mut self,
                peripheral_id: PeripheralId,
                command_id: CommandId,
                subscription_id: SubscriptionId,
            );
        }

        to self.scan {
            pub(super) fn stop_command(&self) -> Option<CommandId>;
            pub(super) fn require_scan_available(&self) -> Result<(), WaiterError>;
            pub(super) fn validate_scan_stop(&self, session_id: ScanSessionId) -> Result<(), WaiterError>;
            pub(super) fn reserve_scan_start(&mut self, command_id: CommandId, session_id: ScanSessionId);
            pub(super) fn reserve_scan_stop(&mut self, command_id: CommandId);
        }
    }

    pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        let route = self.commands.route(command_id)?;
        match route {
            CommandRoute::Initialization => self.initialization.completion_observer(command_id),
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.completion_observer(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .completion_observer_for(connection_id, command_id),
            #[cfg(test)]
            CommandRoute::Test => None,
        }
    }

    /// Links the reservation-owned completion latch to the command directory.
    /// The caller-facing submission receives the returned clone separately.
    pub(super) fn attach_completion_observer(
        &mut self,
        command_id: CommandId,
    ) -> CompletionObserver {
        let completion = self
            .completion_observer(command_id)
            .expect("reserved progress");
        self.set_command_completion(command_id, completion.clone());
        completion
    }

    pub(super) fn request_cancellation(
        &mut self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        let route = self.commands.route(command_id)?;
        match route {
            CommandRoute::Initialization => Some(CancellationDecision::NotCancellable),
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.request_cancellation(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .request_cancellation_for(connection_id, command_id),
            #[cfg(test)]
            CommandRoute::Test => None,
        }
    }

    pub(super) fn take_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.take_cancellation_dispatch(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .take_cancellation_dispatch_for(connection_id, command_id),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    pub(super) fn resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.resolve_cancellation(command_id, result)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .resolve_cancellation_for(connection_id, command_id, result),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    pub(super) fn clear_cancellation(&mut self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.clear_cancellation(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .clear_cancellation_for(connection_id, command_id),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    pub(super) fn cancellation_requested(&self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.cancellation_requested(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .cancellation_requested_for(connection_id, command_id),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    fn take_cancellation_public_events(&mut self, command_id: CommandId) -> Vec<BleEvent> {
        let Some(route) = self.commands.route(command_id) else {
            return Vec::new();
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => self
                .scan
                .take_cancellation_public_events(command_id)
                .unwrap_or_default(),
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .take_cancellation_public_events_for(connection_id, command_id)
                .unwrap_or_default(),
            CommandRoute::Initialization => Vec::new(),
            #[cfg(test)]
            CommandRoute::Test => Vec::new(),
        }
    }

    /// Resolves cancellation before draining events released by that resolution.
    /// The order preserves publication after the control receipt is recorded.
    pub(super) fn resolve_cancellation_and_drain_events(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> Vec<BleEvent> {
        self.resolve_cancellation(command_id, result);
        self.take_cancellation_public_events(command_id)
    }

    pub(super) fn allow_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<bool, ReconciliationFault> {
        if let Some(CommandRoute::Connection { connection_id, .. }) = event
            .command_id()
            .and_then(|command_id| self.commands.route(command_id))
        {
            return Ok(self
                .connections
                .allow_publication_for(connection_id, event)?
                .unwrap_or(true));
        }
        if let Some(allow) = self.scan.allow_publication(event)? {
            return Ok(allow);
        }
        Ok(self.connections.allow_publication(event)?.unwrap_or(true))
    }

    pub(super) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(event) = self.initialization.gate_event(event)? else {
            return Ok(None);
        };
        let Some(event) = (match event
            .command_id()
            .and_then(|command_id| self.commands.route(command_id))
        {
            Some(CommandRoute::Connection {
                connection_id,
                kind,
            }) => self
                .connections
                .gate_event_for(connection_id, kind, event)?,
            _ => self.connections.gate_event(event)?,
        }) else {
            return Ok(None);
        };
        self.scan.gate_event(event)
    }

    pub(super) fn record_event_evidence(
        &mut self,
        event: &BleEvent,
    ) -> Result<(), ReconciliationFault> {
        match event {
            BleEvent::AdapterSnapshotChanged { .. } => self.initialization.record_event(event),
            BleEvent::ScanStarted { .. } | BleEvent::ScanStopped { .. } => {
                self.scan.record_event(event)
            }
            BleEvent::CommandCompleted { command_id, error } => {
                self.record_terminal(*command_id, error.clone());
            }
            _ => return self.connections.record_event(event),
        }
        Ok(())
    }

    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        let Some(route) = self.commands.route(command_id) else {
            return DispatchResolution::default();
        };
        match route {
            CommandRoute::Initialization => self
                .initialization
                .resolve_dispatch(command_id, result)
                .unwrap_or_default(),
            CommandRoute::ScanStart | CommandRoute::ScanStop => self
                .scan
                .resolve_dispatch(command_id, result)
                .unwrap_or_default(),
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .resolve_dispatch_for(connection_id, command_id, result)
                .unwrap_or_default(),
            #[cfg(test)]
            CommandRoute::Test => DispatchResolution::default(),
        }
    }

    pub(super) fn record_terminal(&mut self, command_id: CommandId, error: Option<BleEventError>) {
        let Some(route) = self.commands.route(command_id) else {
            return;
        };
        let result = error.map_or(Ok(()), Err);
        match route {
            CommandRoute::Initialization => {
                self.initialization.record_terminal(command_id, &result);
            }
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.record_terminal(command_id, &result);
            }
            CommandRoute::Connection { connection_id, .. } => {
                self.connections
                    .record_terminal_for(connection_id, command_id, &result);
            }
            #[cfg(test)]
            CommandRoute::Test => {}
        }
    }

    pub(super) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        let route = self.commands.route(command_id)?;
        match route {
            CommandRoute::Initialization => self.initialization.waiter_result(command_id),
            CommandRoute::ScanStart | CommandRoute::ScanStop => self.scan.waiter_result(command_id),
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .waiter_result_for(connection_id, command_id),
            #[cfg(test)]
            CommandRoute::Test => None,
        }
    }

    pub(super) fn advance_and_retire(&mut self) -> Result<(), WaiterError> {
        let mut retired = self
            .initialization
            .advance_after_settlement()?
            .into_iter()
            .collect::<Vec<_>>();
        retired.extend(self.connections.retire_completed());
        retired.extend(self.scan.retire_completed());
        self.remove_commands(retired);
        Ok(())
    }
}
