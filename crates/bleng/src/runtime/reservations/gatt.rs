use blehub::{
    BleEvent, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId, GattDatabase,
    GattSessionId,
};

use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::{
    BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
    is_connection_lost,
};

/// GATT state under one connection. A committed database survives while a
/// later discovery transition is in flight, so failed rediscovery cannot erase
/// the last usable session. Read/write commands and subscription routes belong
/// to sibling owners; this is not an umbrella for every GATT operation.
#[derive(Default)]
pub(super) struct GattReservation {
    database: Option<GattDatabase>,
    discovery: Option<GattDiscoveryReservation>,
}

impl GattReservation {
    pub(super) fn reserve_discovery(&mut self, command_id: CommandId, session_id: GattSessionId) {
        self.discovery = Some(GattDiscoveryReservation::new(command_id, session_id));
    }

    pub(super) fn require_characteristic(
        &self,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
    ) -> Result<(), WaiterError> {
        let database = self
            .database
            .as_ref()
            .filter(|database| database.gatt_session_id == session_id)
            .ok_or(WaiterError::GattNotReady)?;
        if !database.services.iter().any(|service| {
            service
                .characteristics
                .iter()
                .any(|characteristic| characteristic.id == characteristic_id)
        }) {
            return Err(WaiterError::GattNotReady);
        }
        Ok(())
    }

    delegate::delegate! {
        to self.discovery {
            #[call(is_some)]
            pub(super) fn has_pending_discovery(&self) -> bool;
        }
    }

    pub(super) fn progress(&self) -> Option<&CommandProgress> {
        self.discovery.as_ref().map(|discovery| &discovery.progress)
    }

    pub(super) fn progress_mut(&mut self) -> Option<&mut CommandProgress> {
        self.discovery
            .as_mut()
            .map(|discovery| &mut discovery.progress)
    }

    pub(super) fn pending_progress_for_event_mut(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
    ) -> Option<&mut CommandProgress> {
        self.discovery
            .as_mut()
            .filter(|discovery| discovery.should_buffer_event(event, connection_id))
            .map(|discovery| &mut discovery.progress)
    }

    /// Reconciles early evidence before releasing a rejected discovery slot.
    /// The connection supplies rejection policy; the committed database is untouched.
    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
        classify_rejection: impl Fn(&BleEvent) -> BufferedEventDisposition,
    ) -> Option<DispatchResolution> {
        let discovery = self
            .discovery
            .as_mut()
            .filter(|discovery| discovery.progress.owns_command(command_id))?;
        if result.is_ok() {
            return Some(discovery.progress.resolve_accepted_dispatch());
        }

        let resolution = discovery
            .progress
            .resolve_rejected_dispatch(DispatchAction::NoAction, classify_rejection);
        self.discovery = None;
        Some(resolution)
    }

    pub(super) fn record_event(&mut self, event: &BleEvent, connection_id: ConnectionId) {
        if let Some(discovery) = self.discovery.as_mut() {
            discovery.record_event(event, connection_id);
        }
    }

    /// Queries the matching discovery without committing or retiring its draft.
    pub(super) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        self.discovery
            .as_ref()
            .filter(|discovery| discovery.progress.owns_command(command_id))?
            .waiter_result()
    }

    /// Retires discovery only after dispatch acknowledgement and completion.
    /// Returns the retired command while preserving the committed database rules.
    pub(super) fn retire_completed_discovery(&mut self) -> Option<CommandId> {
        if !self
            .discovery
            .as_ref()
            .is_some_and(GattDiscoveryReservation::is_retirable)
        {
            return None;
        }
        let discovery = self.discovery.take().expect("completed discovery exists");
        let command_id = discovery.progress.command_id();
        if discovery.is_successful() {
            self.database = discovery.draft_database;
        }
        Some(command_id)
    }
}

/// One in-flight GATT discovery/rediscovery transition.
struct GattDiscoveryReservation {
    progress: CommandProgress,
    session_id: GattSessionId,
    /// Database drafted by this discovery; it becomes committed only after
    /// accepted dispatch and successful terminal, following waiter settlement.
    draft_database: Option<GattDatabase>,
}

impl GattDiscoveryReservation {
    fn new(command_id: CommandId, session_id: GattSessionId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            draft_database: None,
        }
    }

    /// Dispatch timing applies only to buffering, not domain evidence recording.
    fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    /// Records the latest correlated discovery draft. Terminal and loss events
    /// are related for routing but do not carry a database draft.
    fn record_event(&mut self, event: &BleEvent, expected: ConnectionId) {
        if self.matches_related_event(event, expected)
            && let BleEvent::GattDiscovered { database, .. } = event
        {
            self.draft_database = Some(database.clone());
        }
    }

    /// Queries this already-selected discovery; None means incomplete evidence.
    /// Does not commit the draft or retire discovery; settlement must happen first.
    fn waiter_result(&self) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::GattDiscovered(
            self.draft_database.clone()?,
        )))
    }

    fn is_retirable(&self) -> bool {
        self.progress.is_complete(self.draft_database.is_some())
    }

    fn is_successful(&self) -> bool {
        self.draft_database.is_some() && self.progress.has_successful_terminal()
    }

    /// Correlates database, terminal, or connection loss; not a success predicate.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::GattDiscovered {
                command_id,
                database,
            } => {
                self.progress.owns_command(*command_id)
                    && self.session_id == database.gatt_session_id
                    && expected == database.connection_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
