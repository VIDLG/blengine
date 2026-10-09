use blehub::{AdapterSnapshot, BleEvent, BleEventError, CommandDispatchError, CommandId};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::dispatch::{
    BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
    ReconciliationFault,
};

/// Owns initialization admission, correlated evidence, and readiness transitions.
/// Readiness is distinct from runtime lifecycle and adapter power.
/// Ready retains no snapshot: initialize() returns its completion evidence, while
/// subsequent adapter snapshots remain public events for caller-owned state.
/// A Pending record survives waiter retirement so late callbacks remain correlated.
///
/// The actor checks admission before registering a waiter, then installs the
/// reservation only after claiming the caller handoff. Live callbacks pass through
/// gate_event() before publication and evidence recording; buffered callbacks are
/// replayed after resolve_dispatch(). After recording evidence, the actor settles
/// any caller waiter before advance_after_settlement() can release pending state.
/// This owner neither settles waiters nor performs runtime lifecycle actions.
#[derive(Default)]
pub struct InitializationState {
    phase: InitializationPhase,
}

#[derive(Default)]
enum InitializationPhase {
    #[default]
    NotStarted,
    Pending(InitReservation),
    Ready,
    // Retain only correlation for escaped callbacks, not pending operation evidence.
    Rejected {
        command_id: CommandId,
    },
}

impl InitializationState {
    /// Gates a live callback on dispatch evidence, before public publication.
    /// Some(event) passes through, including owned events needing no buffering;
    /// this is not an ownership query. None retains the event pending resolution.
    /// Overflow or evidence contradicting rejection is an error.
    pub fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        if let InitializationPhase::Rejected { command_id } = &self.phase
            && matches_initialization_event(&event, *command_id)
        {
            // A callback contradicts rejection even if it escaped after the result.
            return Err(ReconciliationFault::DispatchEvidenceConflict);
        }
        if let Some(init) = self.pending_mut()
            && init.should_buffer_event(&event)
        {
            init.progress.buffer(event)?;
            return Ok(None);
        }
        Ok(Some(event))
    }

    pub fn record_event(&mut self, event: &BleEvent) {
        if let BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            snapshot,
        } = event
            && let Some(init) = self.pending_for_mut(*command_id)
            && init.snapshot.is_none()
        {
            init.snapshot = Some(snapshot.clone());
        }
    }

    /// Returns None if no pending initialization owns this dispatch result.
    /// Otherwise updates state and returns ordered replay plus actor-owned actions.
    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let init = self.pending_for_mut(command_id)?;
        Some(if result.is_err() {
            let resolution = init
                .progress
                .rejected_resolution(DispatchAction::StartCleanup, |_| {
                    BufferedEventDisposition::Conflict
                });
            self.phase = InitializationPhase::Rejected { command_id };
            resolution
        } else {
            init.progress.accepted_resolution()
        })
    }

    /// Installs the reservation after admission and caller handoff succeed.
    /// A second installation is an invariant violation, not a normal error path.
    pub fn reserve_initialize(&mut self, command_id: CommandId) {
        assert!(
            self.require_not_started().is_ok(),
            "validated initialization has already started"
        );
        self.phase = InitializationPhase::Pending(InitReservation::new(command_id));
    }

    pub fn require_not_started(&self) -> Result<(), WaiterError> {
        if !matches!(self.phase, InitializationPhase::NotStarted) {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub fn require_initialized(&self) -> Result<(), WaiterError> {
        if matches!(self.phase, InitializationPhase::Ready) {
            Ok(())
        } else {
            Err(WaiterError::NotInitialized)
        }
    }

    fn pending(&self) -> Option<&InitReservation> {
        match &self.phase {
            InitializationPhase::Pending(reservation) => Some(reservation),
            _ => None,
        }
    }

    fn pending_mut(&mut self) -> Option<&mut InitReservation> {
        match &mut self.phase {
            InitializationPhase::Pending(reservation) => Some(reservation),
            _ => None,
        }
    }

    fn pending_for(&self, command_id: CommandId) -> Option<&InitReservation> {
        self.pending()
            .filter(|init| init.progress.command_id == command_id)
    }

    fn pending_for_mut(&mut self, command_id: CommandId) -> Option<&mut InitReservation> {
        self.pending_mut()
            .filter(|init| init.progress.command_id == command_id)
    }

    /// Returns whether initialization owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    pub fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(init) = self.pending_for_mut(command_id) else {
            return false;
        };
        init.progress.record_terminal(result);
        true
    }

    /// Returns a deliverable result without advancing or releasing pending state.
    /// None means either the command is not owned or its evidence is incomplete;
    /// it is not a command-ownership probe. The actor owns waiter delivery.
    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        let init = self.pending_for(command_id)?;
        if let Err(error) = init.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::Initialized(init.snapshot.clone()?)))
    }

    /// Advances readiness after waiter settlement, even when no caller remains.
    /// Incomplete evidence stays pending; success with a snapshot enters Ready.
    /// Accepted terminal failure is returned for actor-owned runtime faulting.
    /// Ok(()) does not imply readiness: it also covers pending or inactive phases.
    pub fn advance_after_settlement(&mut self) -> Result<(), WaiterError> {
        let Some(init) = self.pending() else {
            return Ok(());
        };
        let Some(terminal) = init.progress.terminal_result() else {
            return Ok(());
        };
        terminal?;
        if init.snapshot.is_some() {
            self.phase = InitializationPhase::Ready;
        }
        Ok(())
    }
}

/// Dispatch, correlated snapshot, and terminal may arrive in either order.
/// The pending record and all phase transitions stay private to this owner.
struct InitReservation {
    progress: CommandProgress,
    snapshot: Option<AdapterSnapshot>,
}

impl InitReservation {
    /// Buffering needs both unresolved dispatch and command correlation.
    /// The pure matcher also handles late callbacks after rejection.
    fn should_buffer_event(&self, event: &BleEvent) -> bool {
        self.progress.is_awaiting_dispatch_result()
            && matches_initialization_event(event, self.progress.command_id)
    }

    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            snapshot: None,
        }
    }
}

fn matches_initialization_event(event: &BleEvent, expected: CommandId) -> bool {
    match event {
        BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            ..
        }
        | BleEvent::CommandCompleted { command_id, .. } => *command_id == expected,
        _ => false,
    }
}
