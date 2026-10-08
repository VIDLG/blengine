use blehub::{BleEvent, CommandId, ConnectionId, GattDatabase, GattSessionId};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::{connection::connection_lost, dispatch::CommandProgress};

/// GATT state under one connection. A committed database survives while a
/// later discovery transition is in flight, so failed rediscovery cannot erase
/// the last usable session.
#[derive(Default)]
pub(super) struct GattReservation {
    database: Option<GattDatabase>,
    pub(super) discovery: Option<GattDiscoveryReservation>,
}

/// Operation-specific admission policy; this does not describe native capabilities.
#[derive(Clone, Copy, Debug)]
pub enum GattAccess {
    Read,
    Write,
    EnableNotifications,
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

    /// Retires discovery only after dispatch acknowledgement and completion.
    /// Success requires both a database draft and a successful terminal; failure
    /// preserves the committed database. Callers settle the waiter before calling this.
    pub(super) fn retire_completed_discovery(&mut self) {
        if !self
            .discovery
            .as_ref()
            .is_some_and(GattDiscoveryReservation::is_retirable)
        {
            return;
        }
        let discovery = self.discovery.take().expect("completed discovery exists");
        if discovery.is_successful() {
            self.database = discovery.draft_database;
        }
    }
}

/// One in-flight GATT discovery/rediscovery transition.
pub(super) struct GattDiscoveryReservation {
    pub(super) progress: CommandProgress,
    session_id: GattSessionId,
    /// Database drafted by this discovery; it becomes committed only after
    /// accepted dispatch and successful terminal, following waiter settlement.
    pub(super) draft_database: Option<GattDatabase>,
}

impl GattDiscoveryReservation {
    pub(super) fn matches_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::GattDiscovered {
                command_id,
                database,
            } => {
                self.progress.command_id == *command_id
                    && self.session_id == database.gatt_session_id
                    && expected == database.connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.command_id == *command_id
            }
            _ => connection_lost(event, expected),
        }
    }

    fn new(command_id: CommandId, session_id: GattSessionId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            draft_database: None,
        }
    }

    fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.draft_database.is_some())
    }

    fn is_successful(&self) -> bool {
        self.draft_database.is_some() && self.progress.terminal_succeeded()
    }

    /// Queries this already-selected discovery; None means incomplete evidence.
    /// Does not commit the draft or retire discovery; settlement must happen first.
    pub(super) fn waiter_result(&self) -> Option<Result<WaiterSuccess, WaiterError>> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::GattDiscovered(
            self.draft_database.clone()?,
        )))
    }
}
