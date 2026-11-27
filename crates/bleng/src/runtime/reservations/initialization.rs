use blehub::{AdapterSnapshot, BleEvent, BleEventError, CommandDispatchError, CommandId};

use super::super::shared::CompletionObserver;
use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::{
    BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
    ReconciliationFault,
};

/// Owns initialization admission, correlated evidence, and readiness transitions.
/// Readiness is distinct from runtime lifecycle and adapter power.
/// Ready retains no snapshot: initialize() returns its completion evidence, while
/// subsequent adapter snapshots remain public events for caller-owned state.
/// A Reserved record survives waiter retirement so late callbacks remain correlated.
///
/// The actor checks admission before registering a waiter, then installs the
/// reservation only after claiming the caller submission. Live callbacks pass through
/// gate_event() before publication and evidence recording; buffered callbacks are
/// replayed after resolve_dispatch(). After recording evidence, the actor settles
/// any caller waiter before advance_after_settlement() can release the reservation.
/// This owner neither settles waiters nor performs runtime lifecycle actions.
#[derive(Default)]
pub(in crate::runtime) struct InitializationState {
    phase: InitializationPhase,
}

#[derive(Default)]
enum InitializationPhase {
    #[default]
    NotStarted,
    Reserved(InitReservation),
    Ready,
    // Retain only correlation for escaped callbacks, not the reservation's evidence.
    Rejected {
        command_id: CommandId,
    },
}

impl InitializationState {
    /// Gates a live callback on dispatch evidence, before public publication.
    /// Some(event) passes through, including owned events needing no buffering;
    /// this is not an ownership query. None retains the event pending resolution.
    /// Overflow or evidence contradicting rejection is an error.
    pub(in crate::runtime) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        match &mut self.phase {
            InitializationPhase::Reserved(init) => init.gate_event(event),
            InitializationPhase::Rejected { command_id }
                if matches_initialization_event(&event, *command_id) =>
            {
                // A callback contradicts rejection even if it escaped after the result.
                Err(ReconciliationFault::DispatchEvidenceConflict)
            }
            InitializationPhase::NotStarted
            | InitializationPhase::Ready
            | InitializationPhase::Rejected { .. } => Ok(Some(event)),
        }
    }

    pub(in crate::runtime) fn record_event(&mut self, event: &BleEvent) {
        if let Some(init) = self.reserved_mut() {
            init.record_event(event);
        }
    }

    /// Returns None if no reserved initialization owns this dispatch result.
    /// Otherwise updates state and returns ordered replay plus actor-owned actions.
    pub(in crate::runtime) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let init = self.reserved_for_cmd_mut(command_id)?;
        let resolution = init.resolve_dispatch(result);
        if result.is_err() {
            self.phase = InitializationPhase::Rejected { command_id };
        }
        Some(resolution)
    }

    /// Installs the reservation after admission and caller submission succeed.
    /// A second installation is an invariant violation, not a normal error path.
    pub(in crate::runtime) fn reserve_initialize(&mut self, command_id: CommandId) {
        assert!(
            self.require_not_started().is_ok(),
            "validated initialization has already started"
        );
        self.phase = InitializationPhase::Reserved(InitReservation::new(command_id));
    }

    pub(in crate::runtime) fn require_not_started(&self) -> Result<(), WaiterError> {
        if !matches!(self.phase, InitializationPhase::NotStarted) {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.reserved_for_cmd(command_id)
            .map(|init| init.progress.completion_observer())
    }

    pub(in crate::runtime) fn require_initialized(&self) -> Result<(), WaiterError> {
        if matches!(self.phase, InitializationPhase::Ready) {
            Ok(())
        } else {
            Err(WaiterError::NotInitialized)
        }
    }

    /// Returns the retained reservation, even after its caller stops waiting.
    fn reserved(&self) -> Option<&InitReservation> {
        match &self.phase {
            InitializationPhase::Reserved(reservation) => Some(reservation),
            _ => None,
        }
    }

    fn reserved_mut(&mut self) -> Option<&mut InitReservation> {
        match &mut self.phase {
            InitializationPhase::Reserved(reservation) => Some(reservation),
            _ => None,
        }
    }

    fn reserved_for_cmd(&self, command_id: CommandId) -> Option<&InitReservation> {
        self.reserved()
            .filter(|init| init.progress.owns_command(command_id))
    }

    fn reserved_for_cmd_mut(&mut self, command_id: CommandId) -> Option<&mut InitReservation> {
        self.reserved_mut()
            .filter(|init| init.progress.owns_command(command_id))
    }

    /// Returns whether initialization owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    pub(in crate::runtime) fn try_record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(init) = self.reserved_for_cmd_mut(command_id) else {
            return false;
        };
        init.progress.record_terminal(result);
        true
    }

    delegate::delegate! {
        to |command_id: CommandId| self.reserved_for_cmd(command_id)? {
            /// Returns a deliverable result without advancing or releasing the reservation.
            /// None means either the command is not owned or its evidence is incomplete;
            /// it is not a command-ownership probe. The actor owns waiter delivery.
            pub(in crate::runtime) fn waiter_result(&self) -> Option<WaiterOutcome>;
        }
    }

    /// Advances readiness after waiter settlement, even when no caller remains.
    /// Incomplete evidence retains the reservation; success with a snapshot enters Ready.
    /// Accepted terminal failure is returned for actor-owned runtime faulting.
    /// `None` means the evidence is incomplete or there is no reservation.
    /// `Some` identifies the command whose native reservation just retired.
    pub(in crate::runtime) fn advance_after_settlement(
        &mut self,
    ) -> Result<Option<CommandId>, WaiterError> {
        let Some(init) = self.reserved() else {
            return Ok(None);
        };
        if !init.is_ready()? {
            return Ok(None);
        }

        let command_id = init.progress.command_id();
        self.phase = InitializationPhase::Ready;
        Ok(Some(command_id))
    }
}

/// Dispatch, correlated snapshot, and terminal may arrive in either order.
/// The reserved record and all phase transitions stay private to this owner.
struct InitReservation {
    progress: CommandProgress,
    // First answer for this initialization command; never the latest-state cache.
    snapshot_evidence: Option<AdapterSnapshot>,
    // Initialization chooses WHICH passive updates must follow its buffered
    // evidence; CommandProgress owns the bounded FIFO and ordered draining.
    // This latch matters only while dispatch acknowledgement is pending, so it
    // needs no reset after acceptance. It does not imply snapshot_evidence is set:
    // a buffered snapshot has not yet been replayed into record_event().
    buffering_adapter_updates: bool,
}

impl InitReservation {
    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            snapshot_evidence: None,
            buffering_adapter_updates: false,
        }
    }

    fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        if !self.should_buffer_event(&event) {
            return Ok(Some(event));
        }
        self.progress.buffer_dispatch_pending_event(event)?;
        // The first withheld correlated event (snapshot or terminal) starts
        // the barrier: later passive adapter updates must queue behind it.
        self.buffering_adapter_updates = true;
        Ok(None)
    }

    /// Earlier passive observations remain immediate. After correlated evidence,
    /// preserve received snapshot order until dispatch resolves. Rejected-phase
    /// late callbacks are checked by InitializationState before this.
    fn should_buffer_event(&self, event: &BleEvent) -> bool {
        self.progress.is_awaiting_dispatch_result()
            && (matches_initialization_event(event, self.progress.command_id())
                || (self.buffering_adapter_updates
                    && matches!(
                        event,
                        BleEvent::AdapterSnapshotChanged {
                            command_id: None,
                            ..
                        }
                    )))
    }

    /// Selects the first command-correlated snapshot as this initialization's
    /// answer, not as a cache of the latest adapter state. Later correlated
    /// snapshots do not replace it, even if their contents differ; passive
    /// updates belong only to the public event stream. Result selection here
    /// does not suppress either kind of event from publication.
    fn record_event(&mut self, event: &BleEvent) {
        if let BleEvent::AdapterSnapshotChanged {
            command_id: Some(command_id),
            snapshot,
        } = event
            && self.progress.owns_command(*command_id)
            && self.snapshot_evidence.is_none()
        {
            self.snapshot_evidence = Some(snapshot.clone());
        }
    }

    fn resolve_dispatch(
        &mut self,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        if result.is_ok() {
            return self.progress.resolve_accepted_dispatch();
        }
        self.progress
            .resolve_rejected_dispatch(DispatchAction::StartCleanup, |_| {
                BufferedEventDisposition::Conflict
            })
    }

    fn waiter_result(&self) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::Initialized(
            self.snapshot_evidence.clone()?,
        )))
    }

    /// Checks readiness without cloning the snapshot or changing the phase.
    /// Terminal failure must surface even when the snapshot is still missing.
    fn is_ready(&self) -> Result<bool, WaiterError> {
        let Some(terminal) = self.progress.terminal_result() else {
            return Ok(false);
        };
        terminal?;
        Ok(self.snapshot_evidence.is_some())
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
