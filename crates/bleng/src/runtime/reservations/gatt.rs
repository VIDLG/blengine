use blehub::{BleEvent, BleEventError, CommandId, ConnectionId, GattDatabase, GattSessionId};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{connection::connection_lost, dispatch::DispatchReservation};

/// GATT state under one connection. A committed database survives while a
/// later discovery transition is in flight, so failed rediscovery cannot erase
/// the last usable session.
#[derive(Default)]
pub(super) struct GattReservation {
    pub(super) database: Option<GattDatabase>,
    pub(super) discovery: Option<GattDiscoveryReservation>,
}

impl GattReservation {
    pub(super) fn committed_database(&self, session_id: GattSessionId) -> Option<&GattDatabase> {
        self.database
            .as_ref()
            .filter(|database| database.gatt_session_id == session_id)
    }

    pub(super) fn has_pending_discovery(&self) -> bool {
        self.discovery.is_some()
    }

    pub(super) fn begin_discovery(&mut self, command_id: CommandId, session_id: GattSessionId) {
        self.discovery = Some(GattDiscoveryReservation::new(command_id, session_id));
    }

    /// Returns whether a new database was committed. Failed rediscovery keeps
    /// the old database; callers settle the discovery waiter before calling this.
    pub(super) fn commit_completed_discovery(&mut self) -> bool {
        if !self
            .discovery
            .as_ref()
            .is_some_and(GattDiscoveryReservation::is_retirable)
        {
            return false;
        }
        let discovery = self.discovery.take().expect("completed discovery exists");
        if discovery.is_successful() {
            self.database = discovery.draft_database;
            true
        } else {
            false
        }
    }
}

/// One in-flight GATT discovery/rediscovery transition.
pub(super) struct GattDiscoveryReservation {
    pub(super) dispatch: DispatchReservation,
    session_id: GattSessionId,
    pub(super) terminal: Option<Result<(), BleEventError>>,
    /// Database drafted by this discovery; it becomes committed only after
    /// the discovery terminal succeeds.
    pub(super) draft_database: Option<GattDatabase>,
}

impl GattDiscoveryReservation {
    pub(super) fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::GattDiscovered {
                command_id,
                database,
            } => {
                self.dispatch.command_id == *command_id
                    && self.session_id == database.gatt_session_id
                    && expected == database.connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.dispatch.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    fn new(command_id: CommandId, session_id: GattSessionId) -> Self {
        Self {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            terminal: None,
            draft_database: None,
        }
    }

    fn is_retirable(&self) -> bool {
        self.dispatch
            .is_complete(&self.terminal, self.draft_database.is_some())
    }

    fn is_successful(&self) -> bool {
        self.draft_database.is_some() && self.terminal == Some(Ok(()))
    }

    pub(super) fn waiter_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.dispatch.waiter_result(&self.terminal, || {
            self.draft_database
                .clone()
                .map(WaiterSuccess::GattDiscovered)
        })
    }
}
