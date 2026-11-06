//! Scan session ownership and explicit stop command evidence.
//!
//! Processing order: gate unresolved dispatch, decide public visibility, record
//! internal evidence, settle waiters, then retire reservations. These are distinct
//! stages, not one completion flag. Dispatch buffering withholds all processing;
//! cancellation buffering withholds publication only, so evidence can still advance.
//! Releasing public events after cancellation never re-executes BLE commands.
//!
//! `ScanState` owns at most one `ScanReservation`. That session owns both the
//! `session_end_evidence` fact and an optional `StopReservation`; they are related but distinct:
//!
//! - `session_end_evidence` records valid native session-end evidence. System suspension or
//!   native scan failure can establish it without any explicit stop command.
//! - `StopReservation` occupies the session's explicit stop command slot and
//!   retains dispatch progress, terminal results, and native stop errors. Its
//!   lifetime is independent of the caller's waiter, but owned by the session.
//!
//! These combinations describe different situations, not redundant state:
//!
//! | session_end_evidence | stop | Meaning |
//! |---------|------|---------|
//! | false | None | No observed session end and no explicit stop command. |
//! | false | Some | Explicit stop is reserved; session end is not yet observed. |
//! | true | None | Session ended without a retained explicit stop command. |
//! | true | Some | Session end is observed; stop command evidence is still retained. |
//!
//! Session end alone does not prove successful stop command completion: dispatch
//! acceptance and a successful terminal are also required. Settlement and retirement
//! combine the session facts with command evidence. Keep them together under the
//! session owner, but do not move `session_end_evidence` into the optional child: passive teardown
//! must remain representable when no stop command exists. Nesting expresses ownership;
//! a reservation need not exist independently to reserve a slot and retain evidence.

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ScanSessionId, ScanStopReason,
    ScanStopResult,
};

use super::super::shared::CompletionObserver;
use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::{
    BufferedEventDisposition, CancellationDecision, CommandProgress, DispatchAction,
    DispatchResolution, PublicationGate, PublicationRelease, ReconciliationFault,
};

/// Owns scan-slot admission and Idle/Reserved transitions, forwarding session operations.
/// The retained ScanReservation owns command correlation, dispatch policy, and session facts.
/// The private phase is Idle or Reserved; Reserved includes pending, running,
/// stopping, and stopped-but-unsettled sessions, not just active native scanning.
///
/// The actor checks admission before registering a waiter and installs reservations
/// only after claiming the caller submission. Live events pass through gate_event();
/// dispatch resolution transfers buffered callbacks for ordered replay. The actor
/// records evidence and settles waiters before retire_completed() releases state,
/// even when a caller has already timed out or cancelled. This owner neither
/// publishes events nor owns waiter delivery or runtime lifecycle actions.
#[derive(Default)]
pub(in crate::runtime) struct ScanState {
    phase: ScanPhase,
}

#[derive(Default)]
enum ScanPhase {
    #[default]
    Idle,
    // Keep the idle actor compact; allocate once per admitted scan session.
    Reserved(Box<ScanReservation>),
}

/// A recognized dispatch result's effect on the session slot, alongside its
/// already-classified work. Releasing the slot is not a native cleanup verdict.
enum ScanDispatchResolution {
    KeepSession(DispatchResolution),
    ReleaseSession(DispatchResolution),
}

impl ScanState {
    // Admission and installation.

    pub(in crate::runtime) fn require_scan_available(&self) -> Result<(), WaiterError> {
        match &self.phase {
            ScanPhase::Idle => Ok(()),
            ScanPhase::Reserved(_) => Err(WaiterError::ScanBusy),
        }
    }

    pub(in crate::runtime) fn validate_scan_stop(
        &self,
        session_id: ScanSessionId,
    ) -> Result<(), WaiterError> {
        self.reserved()
            .ok_or(WaiterError::ScanNotActive)?
            .validate_stop(session_id)
    }

    /// Admission and caller submission must succeed before installation.
    pub(in crate::runtime) fn reserve_scan_start(
        &mut self,
        command_id: CommandId,
        session_id: ScanSessionId,
    ) {
        assert!(
            self.require_scan_available().is_ok(),
            "validated scan slot is occupied"
        );
        self.phase = ScanPhase::Reserved(Box::new(ScanReservation::new(command_id, session_id)));
    }

    delegate::delegate! {
        to self.reserved_mut().expect("validated scan reservation") {
            /// Installs stop only after start completion, admission, and caller submission.
            /// Invalid installation is an invariant violation, not a normal error path.
            pub(in crate::runtime) fn reserve_scan_stop(&mut self, command_id: CommandId);
        }
    }

    // Command queries.

    delegate::delegate! {
        to self.reserved()? {
            pub(in crate::runtime) fn stop_command(&self) -> Option<CommandId>;
            pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver>;
        }
    }

    // Dispatch and evidence.

    /// Gates live callbacks before publication; this is not an ownership query.
    /// Some(event) passes through, including owned events needing no buffering.
    /// None retains the event pending dispatch resolution; overflow is an error.
    pub(in crate::runtime) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(scan) = self.reserved_mut() else {
            return Ok(Some(event));
        };
        scan.gate_event(event)
    }

    /// Returns None when neither the scan start nor its stop owns this command.
    /// Otherwise updates reservations and returns ordered replay plus actor actions.
    pub(in crate::runtime) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        match self.reserved_mut()?.resolve_dispatch(command_id, result)? {
            ScanDispatchResolution::KeepSession(resolution) => Some(resolution),
            ScanDispatchResolution::ReleaseSession(resolution) => {
                self.phase = ScanPhase::Idle;
                Some(resolution)
            }
        }
    }

    pub(in crate::runtime) fn record_event(&mut self, event: &BleEvent) {
        if let Some(scan) = self.reserved_mut() {
            scan.record_event(event);
        }
    }

    /// Returns command ownership, including duplicates, not operation success.
    pub(in crate::runtime) fn try_record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(scan) = self.reserved_mut() else {
            return false;
        };
        scan.try_record_terminal(command_id, result)
    }

    // Cancellation.

    delegate::delegate! {
        to self.reserved_mut()? {
            pub(in crate::runtime) fn request_cancellation(
                &mut self,
                command_id: CommandId,
            ) -> Option<CancellationDecision>;
        }
    }

    pub(in crate::runtime) fn try_claim_cancellation_dispatch(
        &mut self,
        command_id: CommandId,
    ) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.try_claim_cancellation_dispatch(command_id))
    }

    pub(super) fn try_resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.try_resolve_cancellation(command_id, result))
    }

    pub(super) fn try_clear_cancellation(&mut self, command_id: CommandId) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.try_clear_cancellation(command_id))
    }

    pub(super) fn is_cancellation_requested(&self, command_id: CommandId) -> bool {
        self.reserved()
            .is_some_and(|scan| scan.is_cancellation_requested(command_id))
    }

    // Public event ordering.

    pub(super) fn decide_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<super::PublicationDecision, ReconciliationFault> {
        let Some(scan) = self.reserved_mut() else {
            return Ok(super::PublicationDecision::Publish);
        };
        scan.decide_publication(event)
    }

    pub(super) fn take_cancellation_public_events(
        &mut self,
        command_id: CommandId,
    ) -> Vec<BleEvent> {
        let Some(scan) = self.reserved_mut() else {
            return Vec::new();
        };
        scan.take_cancellation_public_events(command_id)
    }

    // Caller results and reservation retirement.

    delegate::delegate! {
        to self.reserved()? {
            /// Queries a deliverable result without retiring the session or stop command.
            /// None means unowned or incomplete, not operation failure.
            pub(in crate::runtime) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome>;
        }
    }

    /// Called after waiter settlement, even when the caller has already left.
    /// Prefer retiring the whole session and returning both start/stop command routes.
    /// Otherwise release only a failed stop, retaining the session for another attempt.
    pub(in crate::runtime) fn retire_completed(&mut self) -> Vec<CommandId> {
        let Some(scan) = self.reserved_mut() else {
            return Vec::new();
        };
        if scan.is_retirable() {
            let retired = scan.command_ids().collect();
            self.phase = ScanPhase::Idle;
            return retired;
        }
        scan.retire_failed_stop().into_iter().collect()
    }

    // Private session lookup.

    fn reserved(&self) -> Option<&ScanReservation> {
        match &self.phase {
            ScanPhase::Idle => None,
            ScanPhase::Reserved(scan) => Some(scan),
        }
    }

    fn reserved_mut(&mut self) -> Option<&mut ScanReservation> {
        match &mut self.phase {
            ScanPhase::Idle => None,
            ScanPhase::Reserved(scan) => Some(scan),
        }
    }
}

/// Owns the entire scan session, not just its initiating start command.
/// Retains command progress until late native start/stop callbacks are accounted for.
struct ScanReservation {
    // Start command progress; stop has its own command and acknowledgement.
    start_progress: CommandProgress,
    // One 64-event publication cap covers start/stop and passive scan events.
    publication: super::ScopedPublication,
    session_id: ScanSessionId,
    // These are independent, latched observations, not mutually exclusive phases:
    // session-end evidence can arrive before the matching ScanStarted callback.
    // Seeing that late start must not erase the already observed session end.
    // Session-level teardown evidence. ScanStopped::Err has no command ID and can
    // race start, so it must not be attributed to start as a start-domain error.
    // If an explicit stop exists, StopReservation separately retains that error
    // for the stop waiter; without one, this remains only a session-end fact.
    session_end_evidence: bool,
    // Start-command domain evidence, retained with the session's root command.
    // Matching ScanStarted was observed, not proof that scanning is still running.
    started_evidence: bool,
    stop: Option<StopReservation>,
}

impl ScanReservation {
    fn new(command_id: CommandId, session_id: ScanSessionId) -> Self {
        Self {
            start_progress: CommandProgress::new(command_id),
            publication: super::ScopedPublication::default(),
            session_id,
            session_end_evidence: false,
            started_evidence: false,
            stop: None,
        }
    }

    // Admission and installation.

    /// Checks whether this session can admit a new explicit stop, without reserving it.
    /// Inactive identity/end/failure takes precedence over incomplete start or an occupied slot.
    /// Callers must still install the stop only after claiming the submission.
    fn validate_stop(&self, session: ScanSessionId) -> Result<(), WaiterError> {
        // Inactive identity/end/failure wins over Busy: only a usable session can be busy.
        if self.session_id != session || self.is_start_failed() || self.session_end_evidence {
            return Err(WaiterError::ScanNotActive);
        }
        // Completion here means the start command is confirmed, not the scan has ended:
        // accepted dispatch + matching ScanStarted + successful terminal (failure was
        // excluded above). A running scan can stop once start is confirmed. A retained
        // stop reservation blocks another stop even if its caller has already timed out.
        if !self.start_progress.is_complete(self.started_evidence) || self.stop.is_some() {
            return Err(WaiterError::ScanBusy);
        }
        Ok(())
    }

    fn reserve_scan_stop(&mut self, command_id: CommandId) {
        assert!(
            self.validate_stop(self.session_id).is_ok(),
            "validated scan cannot accept a stop reservation"
        );
        self.stop = Some(StopReservation::new(command_id));
    }

    // Command queries.

    fn stop_command(&self) -> Option<CommandId> {
        self.stop.as_ref().map(StopReservation::command_id)
    }

    fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.progress_for_cmd(command_id)
            .map(CommandProgress::completion_observer)
    }

    /// Enumerates retained command identities, not pending waiters: a completed start
    /// remains correlated until the session itself retires.
    fn command_ids(&self) -> impl Iterator<Item = CommandId> + '_ {
        std::iter::once(self.start_progress.command_id()).chain(self.stop_command())
    }

    // Dispatch and evidence.

    /// Dispatch gate, distinct from the cancellation publication FIFO.
    /// Retained callbacks must not publish, record evidence, or settle waiters yet.
    fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        // Start gates advertisements and passive teardown as well as command callbacks.
        // None may affect the session lifecycle before start dispatch resolves.
        if self.should_buffer_start_event(&event) {
            self.start_progress.buffer_dispatch_pending_event(event)?;
            return Ok(None);
        }
        // Only callbacks not retained by start reach stop; each enters at most one buffer.
        match self.stop.as_mut() {
            Some(stop) => stop.gate_event(event, self.session_id),
            None => Ok(Some(event)),
        }
    }

    /// Classifies dispatch evidence and decides the session's slot disposition.
    /// Unknown command IDs leave this session and its stop child untouched.
    fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<ScanDispatchResolution> {
        if self.start_progress.owns_command(command_id) {
            if result.is_ok() {
                return Some(ScanDispatchResolution::KeepSession(
                    self.start_progress.resolve_accepted_dispatch(),
                ));
            }
            // Classify before releasing the slot: early callbacks contradict start rejection.
            // The actor executes any resulting fault; this layer only supplies the work.
            let resolution = self
                .start_progress
                .resolve_rejected_dispatch(DispatchAction::NoAction, |_| {
                    BufferedEventDisposition::Conflict
                });
            return Some(ScanDispatchResolution::ReleaseSession(resolution));
        }
        let resolution = self.stop_for_cmd_mut(command_id)?.resolve_dispatch(result);
        if result.is_err() {
            // Rejection releases only the stop child, not the scan session.
            // Independent teardown stays in the resolution for ordered actor replay.
            self.stop = None;
        }
        Some(ScanDispatchResolution::KeepSession(resolution))
    }

    /// Records domain evidence after dispatch gating or ordered dispatch-result replay.
    /// Public suppression does not skip this step; visibility and completion are independent.
    fn record_event(&mut self, event: &BleEvent) {
        match event {
            BleEvent::ScanStarted {
                command_id,
                scan_id,
            } if self.start_progress.owns_command(*command_id) && *scan_id == self.session_id => {
                self.started_evidence = true;
            }
            BleEvent::ScanStopped { scan_id, result } if *scan_id == self.session_id => {
                let confirms_session_end = match self.stop.as_mut() {
                    Some(stop) => stop.try_record_stop_result(result),
                    // Passive teardown does not require an explicit stop command.
                    // A Requested event without its retained command proves nothing here.
                    None => matches!(
                        result,
                        ScanStopResult::Err { .. }
                            | ScanStopResult::Ok {
                                reason: ScanStopReason::SystemSuspended,
                            }
                    ),
                };
                // Session-end evidence is latched; foreign late events cannot undo it.
                self.session_end_evidence |= confirms_session_end;
            }
            _ => {}
        }
    }

    /// Returns whether scan start or stop owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    fn try_record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(progress) = self.progress_for_cmd_mut(command_id) else {
            return false;
        };
        progress.record_terminal(result);
        true
    }

    // Cancellation.

    /// Admits or reuses a local cancellation request; does not call the backend.
    /// None means the command is unowned, whereas an owned stop supplies its explicit
    /// NotCancellable decision. The actor separately claims and dispatches accepted requests.
    fn request_cancellation(&mut self, command_id: CommandId) -> Option<CancellationDecision> {
        if self.start_progress.owns_command(command_id) {
            return Some(self.start_progress.request_cancellation());
        }
        self.stop_for_cmd(command_id)
            .map(StopReservation::request_cancellation)
    }

    /// Claims at most one native cancellation dispatch for the matching local request.
    /// False also covers unowned or not-yet-ready requests; no backend call happens here.
    fn try_claim_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        self.progress_for_cmd_mut(command_id)
            .is_some_and(CommandProgress::try_claim_cancellation_dispatch)
    }

    /// Applies a control result to the matching command's cancellation state.
    /// True reports command ownership, not accepted cancellation or an active request.
    /// The actor still owns publication release, waiter delivery, and retirement ordering.
    fn try_resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        let Some(progress) = self.progress_for_cmd_mut(command_id) else {
            return false;
        };
        progress.resolve_cancellation(result);
        true
    }

    /// Clears the request latch so normal settlement can resume after cancellation rejection.
    /// This neither erases terminal/domain evidence nor rolls back native work.
    /// True means the command was found, not that an active latch existed.
    fn try_clear_cancellation(&mut self, command_id: CommandId) -> bool {
        let Some(progress) = self.progress_for_cmd_mut(command_id) else {
            return false;
        };
        progress.clear_cancellation();
        true
    }

    fn is_cancellation_requested(&self, command_id: CommandId) -> bool {
        self.progress_for_cmd(command_id)
            .is_some_and(CommandProgress::is_cancellation_requested)
    }

    fn has_pending_cancellation(&self) -> bool {
        self.start_progress.is_awaiting_cancellation_result()
            || self
                .stop
                .as_ref()
                .is_some_and(|stop| stop.progress.is_awaiting_cancellation_result())
    }

    // Public event ordering.

    /// Controls public visibility, not command completion or resource retirement.
    /// ScopedPublication first suppresses accepted-cancellation non-terminals,
    /// then defers events blocked by ordering, and only otherwise publishes immediately.
    fn decide_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<super::PublicationDecision, ReconciliationFault> {
        if !self.matches_publication_scope(event) {
            // This session cannot block unrelated events; Publish is not identity validation.
            return Ok(super::PublicationDecision::Publish);
        }
        // Event-local fact: cancellation was accepted for its command. Missing identity is false.
        // This differs from the scope-wide pending-control fact below; do not merge the two.
        let cancellation_accepted = event
            .command_id()
            .and_then(|id| self.progress_for_cmd(id))
            .is_some_and(CommandProgress::is_cancellation_accepted);
        let gate = PublicationGate {
            cancellation_accepted_for_event: cancellation_accepted,
            cancellation_pending: self.has_pending_cancellation(),
        };
        self.publication.decide(event, gate)
    }

    /// Called after recording this cancellation result in command progress.
    /// Empty may mean unowned command, empty queue, or another pending control;
    /// it does not imply the queue was discarded. Returned events are publication-only:
    /// their internal evidence must not be recorded again.
    fn take_cancellation_public_events(&mut self, command_id: CommandId) -> Vec<BleEvent> {
        // Only None returns early. Some(false) still needs to release retained events.
        let Some(accepted) = self
            .progress_for_cmd(command_id)
            .map(CommandProgress::is_cancellation_accepted)
        else {
            return Vec::new();
        };
        // Recompute pending controls after the current result; pre-result state is stale.
        let pending = self.has_pending_cancellation();
        // Accepted cancellation filters this command's non-terminals. Pending controls
        // retain the remaining FIFO; otherwise it drains in order. Rejection filters nothing.
        self.publication.resolve(
            command_id,
            PublicationRelease {
                cancellation_accepted: accepted,
                another_cancellation_pending: pending,
            },
        )
    }

    // Caller results and reservation retirement.

    /// Selects the start or stop outcome from retained evidence without mutating it.
    /// None means unowned or incomplete, not success. Querying does not deliver the result
    /// or authorize retirement; the actor separately coordinates settlement and advancement.
    fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        if self.start_progress.owns_command(command_id) {
            // Terminal failure must surface without waiting for ScanStarted.
            // Terminal success alone is insufficient; matching start evidence is still required.
            if let Err(error) = self.start_progress.terminal_result()? {
                return Some(Err(error));
            }
            return self
                .started_evidence
                .then_some(Ok(WaiterSuccess::ScanStarted(self.session_id)));
        }
        self.stop_for_cmd(command_id)?
            .waiter_result(self.session_id, self.session_end_evidence)
    }

    /// Pure session-retirement decision, evaluated after waiter settlement.
    /// Drain public ordering first, then require complete start/stop evidence, then check
    /// session end or start failure. Native teardown cannot bypass the earlier barriers.
    fn is_retirable(&self) -> bool {
        if !self.publication.is_empty() {
            return false;
        }
        // Native end cannot release a start whose domain/terminal is still missing.
        if !self.start_progress.is_complete(self.started_evidence) {
            return false;
        }
        if let Some(stop) = &self.stop
            && !stop.is_complete(self.session_end_evidence)
        {
            return false;
        }
        // A failed stop alone leaves the scan running; retire only its child slot.
        self.is_start_failed() || self.session_end_evidence
    }

    /// Releases only a failed stop command, leaving the session available to retry.
    /// The caller has already settled its waiter and checked whole-session retirement.
    fn retire_failed_stop(&mut self) -> Option<CommandId> {
        let stop = self.stop.as_ref()?;
        if !stop.progress.has_accepted_terminal_failure() {
            return None;
        }
        let command_id = stop.command_id();
        self.stop = None;
        Some(command_id)
    }

    // Private command lookup and event predicates.

    fn progress_for_cmd(&self, command_id: CommandId) -> Option<&CommandProgress> {
        if self.start_progress.owns_command(command_id) {
            return Some(&self.start_progress);
        }
        self.stop_for_cmd(command_id).map(|stop| &stop.progress)
    }

    fn progress_for_cmd_mut(&mut self, command_id: CommandId) -> Option<&mut CommandProgress> {
        if self.start_progress.owns_command(command_id) {
            return Some(&mut self.start_progress);
        }
        self.stop_for_cmd_mut(command_id)
            .map(|stop| &mut stop.progress)
    }

    fn stop_for_cmd(&self, command_id: CommandId) -> Option<&StopReservation> {
        self.stop
            .as_ref()
            .filter(|stop| stop.owns_command(command_id))
    }

    fn stop_for_cmd_mut(&mut self, command_id: CommandId) -> Option<&mut StopReservation> {
        self.stop
            .as_mut()
            .filter(|stop| stop.owns_command(command_id))
    }

    /// Selects events that must share this session's cancellation publication FIFO.
    /// Session observations match by session ID, terminals by retained command ID.
    /// A scope match is deliberately broader than command-specific success evidence.
    fn matches_publication_scope(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::ScanStarted { scan_id, .. } | BleEvent::ScanStopped { scan_id, .. } => {
                *scan_id == self.session_id
            }
            BleEvent::AdvertisementObserved { advertisement } => {
                advertisement.scan_session_id == self.session_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress_for_cmd(*command_id).is_some()
            }
            _ => false,
        }
    }

    /// Buffers related command/session events only while start dispatch is unresolved.
    /// This combines dispatch timing and correlation, not start success evidence.
    fn should_buffer_start_event(&self, event: &BleEvent) -> bool {
        if !self.start_progress.is_awaiting_dispatch_result() {
            return false;
        }
        match event {
            BleEvent::ScanStarted { command_id, .. }
            | BleEvent::CommandCompleted { command_id, .. } => {
                self.start_progress.owns_command(*command_id)
            }
            // Native advertisements can reach the actor before it processes the
            // start dispatch result. Buffer until acceptance, not until ScanStarted.
            BleEvent::AdvertisementObserved { advertisement } => {
                advertisement.scan_session_id == self.session_id
            }
            // The same session can end independently while start dispatch is still
            // unresolved. Its teardown must not bypass acceptance/rejection either.
            BleEvent::ScanStopped { scan_id, .. } => *scan_id == self.session_id,
            _ => false,
        }
    }

    delegate::delegate! {
        to self.start_progress {
            #[call(has_failed_terminal)]
            fn is_start_failed(&self) -> bool;
        }
    }
}

/// Reserves the scan session's explicit stop slot and retains command evidence
/// independently of the caller's waiter. Owned and retired by the scan session.
struct StopReservation {
    // This explicit command's dispatch and terminal, separate from session end.
    progress: CommandProgress,
    // Error from this session's ScanStopped::Err while stop is reserved, not from
    // CommandCompleted (which progress already owns). It proves native teardown
    // and fails the stop waiter even if this command's terminal never arrives.
    stop_error_evidence: Option<BleEventError>,
}

impl StopReservation {
    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            stop_error_evidence: None,
        }
    }

    // Command identity.

    delegate::delegate! {
        to self.progress {
            fn command_id(&self) -> CommandId;
            fn owns_command(&self, command_id: CommandId) -> bool;
        }
    }

    // Dispatch and evidence.

    /// Passes unrelated or already-accepted callbacks through; otherwise retains them
    /// until dispatch resolves. The parent has already checked the start command's gate.
    fn gate_event(
        &mut self,
        event: BleEvent,
        session_id: ScanSessionId,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        if !self.should_buffer_event(&event, session_id) {
            return Ok(Some(event));
        }
        self.progress.buffer_dispatch_pending_event(event)?;
        Ok(None)
    }

    /// Reconciles this stop's dispatch return with callbacks withheld before it.
    /// Rejection must not treat independent teardown as proof this command executed.
    /// The returned work describes permitted replay and runtime actions; only the actor executes them.
    fn resolve_dispatch(
        &mut self,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        if result.is_ok() {
            return self.progress.resolve_accepted_dispatch();
        }
        let rejected_command_id = self.progress.command_id();
        self.progress
            .resolve_rejected_dispatch(DispatchAction::NoAction, |event| match event {
                // For ScanStopped, only Requested carries a command ID.
                // Evidence from this rejected command contradicts rejection.
                BleEvent::ScanStopped { .. } if event.command_id() == Some(rejected_command_id) => {
                    BufferedEventDisposition::Conflict
                }
                // Passive or foreign-command observations stay public. Recording
                // separately checks identity before treating them as session end.
                BleEvent::ScanStopped { .. } => BufferedEventDisposition::Replay,
                // A rejected command's terminal cannot complete the native scan.
                _ => BufferedEventDisposition::Discard,
            })
    }

    /// Records a session-correlated stop result and checks whether it proves session end.
    /// The parent already matched the session ID. A true result is teardown evidence,
    /// not command success: native failure also returns true and retains its error.
    fn try_record_stop_result(&mut self, result: &ScanStopResult) -> bool {
        match result {
            ScanStopResult::Err { error } => {
                self.stop_error_evidence = Some(error.clone());
                true
            }
            ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            } => true,
            ScanStopResult::Ok {
                reason: ScanStopReason::Requested { command_id },
            } => self.progress.owns_accepted_command(*command_id),
        }
    }

    // Cancellation.

    /// An installed teardown command is not cancellable. Predispatch submission
    /// cancellation is handled before this reservation exists, not by its progress.
    fn request_cancellation(&self) -> CancellationDecision {
        CancellationDecision::NotCancellable
    }

    // Caller result and completion policy.

    /// Combines this command's progress with session-end evidence supplied by its owner.
    /// The stop child does not own that fact: the session can end without any stop command.
    fn waiter_result(
        &self,
        session_id: ScanSessionId,
        session_end_evidence: bool,
    ) -> Option<WaiterOutcome> {
        if !self.is_complete(session_end_evidence) {
            return None;
        }
        // Prefer the recorded native scan error; it may complete stop without a terminal.
        // Otherwise preserve terminal failure, and return success only after all evidence.
        if let Some(error) = &self.stop_error_evidence {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::ScanStopped(session_id)))
    }

    /// Enough evidence to settle the command, even if its caller has retired.
    /// Native failure proves teardown and does not require a terminal callback.
    fn is_complete(&self, session_end_evidence: bool) -> bool {
        // Guard both paths, including native failure's terminal-free shortcut below.
        if self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        if self.stop_error_evidence.is_some() {
            return true;
        }
        self.progress.is_complete(session_end_evidence)
    }

    // Private event correlation.

    /// Buffers related events only while stop dispatch is unresolved, including
    /// passive teardown. Requested-stop identity is checked when recording.
    fn should_buffer_event(&self, event: &BleEvent, session_id: ScanSessionId) -> bool {
        if !self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        match event {
            BleEvent::CommandCompleted { command_id, .. } => self.owns_command(*command_id),
            BleEvent::ScanStopped { scan_id, .. } => *scan_id == session_id,
            _ => false,
        }
    }
}
