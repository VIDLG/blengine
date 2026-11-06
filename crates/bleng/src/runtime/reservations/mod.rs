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
//! Read `command_progress`, `cancellation`, and `publication` first:
//! `CommandProgress` owns acknowledgement, bounded dispatch buffering, and the
//! authoritative first terminal; cancellation owns its control receipt; publication
//! owns scope-level public event ordering. Typed domain evidence stays in each record.
//! Follow these shared coordination modules with
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
//! reservation; try_record_terminal() returns ownership where routing needs it, not
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
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult, GattCharacteristicId, GattSessionId, NotificationModeRequest,
    PeripheralId, ScanSessionId, SubscriptionId,
};

use crate::{
    runtime::shared::CompletionObserver,
    waiter::{WaiterError, WaiterOutcome},
};

use super::{CommandDirectory, CommandRoute};

mod cancellation;
mod command_progress;
mod connection;
mod connections;
mod gatt;
mod initialization;
mod io;
mod publication;
mod scan;
mod subscription;
mod subscriptions;

#[cfg(test)]
mod tests;

pub(super) use cancellation::{CancellationDecision, CancellationReceiver};
use command_progress::{BufferedEventDisposition, CommandProgress};
pub(super) use command_progress::{DispatchAction, DispatchResolution, ReconciliationFault};

pub(super) use connections::ConnectionReservations;

pub(super) use initialization::InitializationState;
pub(super) use publication::PublicationDecision;
use publication::{PublicationGate, PublicationRelease, ScopedPublication};
pub(super) use scan::ScanState;

/// Selects operation-specific GATT admission rules, not native capabilities.
#[derive(Clone, Copy, Debug)]
pub(super) enum GattAccess {
    Read,
    Write,
    EnableNotifications,
}

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

#[derive(Clone, Copy)]
enum PublicationTarget {
    Publish,
    Scan,
    Connection(ConnectionId),
    Command(CommandId),
}

impl ReservationCoordinator {
    delegate::delegate! {
        to self.commands {

            #[call(insert)]
            pub(super) fn register_command(&mut self, command_id: CommandId, route: CommandRoute);
            #[call(set_completion)]
            fn set_command_completion(
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
                .completion_observer_for_conn(connection_id, command_id),
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
                .request_cancellation_for_conn(connection_id, command_id),
            #[cfg(test)]
            CommandRoute::Test => None,
        }
    }

    pub(super) fn try_claim_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.try_claim_cancellation_dispatch(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .try_claim_cancellation_dispatch_for_conn(connection_id, command_id),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    fn try_resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.try_resolve_cancellation(command_id, result)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .try_resolve_cancellation_for_conn(connection_id, command_id, result),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    pub(super) fn try_clear_cancellation(&mut self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.try_clear_cancellation(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .try_clear_cancellation_for_conn(connection_id, command_id),
            CommandRoute::Initialization => false,
            #[cfg(test)]
            CommandRoute::Test => false,
        }
    }

    pub(super) fn is_cancellation_requested(&self, command_id: CommandId) -> bool {
        let Some(route) = self.commands.route(command_id) else {
            return false;
        };
        match route {
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.is_cancellation_requested(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .is_cancellation_requested_for_conn(connection_id, command_id),
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
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.take_cancellation_public_events(command_id)
            }
            CommandRoute::Connection { connection_id, .. } => self
                .connections
                .take_cancellation_public_events_for_conn(connection_id, command_id),
            CommandRoute::Initialization => Vec::new(),
            #[cfg(test)]
            CommandRoute::Test => Vec::new(),
        }
    }

    /// Records the control receipt before releasing public events made ready by it.
    /// Another pending cancellation keeps its scope's FIFO buffered, so this need not drain it.
    /// Returned events are publication-only; their internal evidence was already recorded.
    pub(super) fn resolve_cancellation_and_release_public_events(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> Vec<BleEvent> {
        self.try_resolve_cancellation(command_id, result);
        self.take_cancellation_public_events(command_id)
    }

    fn route_for_event(&self, event: &BleEvent) -> Option<CommandRoute> {
        event
            .command_id()
            .and_then(|command_id| self.commands.route(command_id))
    }

    fn publication_target(&self, event: &BleEvent) -> PublicationTarget {
        match self.route_for_event(event) {
            Some(CommandRoute::Connection { connection_id, .. }) => {
                PublicationTarget::Connection(connection_id)
            }
            Some(CommandRoute::ScanStart | CommandRoute::ScanStop) => PublicationTarget::Scan,
            Some(CommandRoute::Initialization) => PublicationTarget::Publish,
            #[cfg(test)]
            Some(CommandRoute::Test) => PublicationTarget::Publish,
            None => match event {
                BleEvent::ScanStarted { .. }
                | BleEvent::AdvertisementObserved { .. }
                | BleEvent::ScanStopped { .. } => PublicationTarget::Scan,
                BleEvent::ConnectionStateChanged { connection_id, .. }
                | BleEvent::CharacteristicRead { connection_id, .. }
                | BleEvent::CharacteristicWritten { connection_id, .. }
                | BleEvent::NotificationStateChanged { connection_id, .. }
                | BleEvent::NotificationReceived { connection_id, .. }
                | BleEvent::RssiRead { connection_id, .. } => {
                    PublicationTarget::Connection(*connection_id)
                }
                BleEvent::GattDiscovered { database, .. } => {
                    PublicationTarget::Connection(database.connection_id)
                }
                BleEvent::CommandCompleted { command_id, .. } => {
                    PublicationTarget::Command(*command_id)
                }
                BleEvent::AdapterSnapshotChanged { .. } | BleEvent::BackendError { .. } => {
                    PublicationTarget::Publish
                }
            },
        }
    }

    pub(super) fn decide_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        match self.publication_target(event) {
            PublicationTarget::Publish => Ok(PublicationDecision::Publish),
            PublicationTarget::Scan => self.scan.decide_publication(event),
            PublicationTarget::Connection(connection_id) => self
                .connections
                .decide_publication_for_conn(connection_id, event),
            PublicationTarget::Command(command_id) => self
                .connections
                .decide_publication_for_cmd(command_id, event),
        }
    }

    pub(super) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(event) = self.initialization.gate_event(event)? else {
            return Ok(None);
        };
        let route = self.route_for_event(&event);
        let event = match (event, route) {
            // State callbacks affect a connection scope, not just their named command.
            (event @ BleEvent::ConnectionStateChanged { .. }, _) => {
                self.connections.gate_event(event)?
            }
            (event, Some(CommandRoute::Connection { connection_id, .. })) => {
                self.connections.gate_event_for_conn(connection_id, event)?
            }
            (event, _) => self.connections.gate_event(event)?,
        };
        match event {
            Some(event) => self.scan.gate_event(event),
            None => Ok(None),
        }
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
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                let resolution = self.scan.resolve_dispatch(command_id, result);
                // Start rejection releases the session; stop rejection releases its
                // child. Neither command remains for the retirement pass to find.
                if result.is_err() && resolution.is_some() {
                    self.remove_commands([command_id]);
                }
                resolution.unwrap_or_default()
            }
            CommandRoute::Connection { connection_id, .. } => {
                let resolution =
                    self.connections
                        .resolve_dispatch_for_conn(connection_id, command_id, result);
                // Rejection may have already released the reservation, leaving no
                // command for the later retirement pass to discover.
                if result.is_err() && resolution.is_some() {
                    self.remove_commands([command_id]);
                }
                resolution.unwrap_or_default()
            }
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
                self.initialization.try_record_terminal(command_id, &result);
            }
            CommandRoute::ScanStart | CommandRoute::ScanStop => {
                self.scan.try_record_terminal(command_id, &result);
            }
            CommandRoute::Connection { connection_id, .. } => {
                self.connections
                    .record_terminal_for_conn(connection_id, command_id, &result);
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
                .waiter_result_for_conn(connection_id, command_id),
            #[cfg(test)]
            CommandRoute::Test => None,
        }
    }

    /// Finishes state transitions enabled by settled evidence and releases completed
    /// reservation records from the initialization, connection, and scan roots.
    ///
    /// `advance` means changing a root's logical phase, such as initialization
    /// becoming ready. `release` means removing a reservation and its command route
    /// after its native/domain lifecycle is complete; it does not cancel native work
    /// or erase an already delivered waiter result.
    pub(super) fn advance_and_retire(&mut self) -> Result<(), WaiterError> {
        // An initialization failure faults the runtime, but completed reservations in the other
        // roots can still be released in this actor turn before the error is returned.
        let initialization = self.initialization.advance_after_settlement();
        let mut retired = Vec::new();
        if let Ok(Some(command_id)) = &initialization {
            retired.push(*command_id);
        }
        retired.extend(self.connections.retire_completed());
        retired.extend(self.scan.retire_completed());
        self.remove_commands(retired);
        initialization.map(|_| ())
    }
}

/// Identifies loss of the expected connection. A command-correlated error alone
/// belongs to that command; only an uncorrelated error establishes passive loss.
pub(super) fn is_connection_lost(event: &BleEvent, expected: ConnectionId) -> bool {
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
