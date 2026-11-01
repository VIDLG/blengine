//! Scan session ownership and explicit stop command evidence.
//!
//! `ScanState` owns at most one `ScanReservation`. That session owns both the
//! `stopped_evidence` fact and an optional `StopReservation`; they are related but distinct:
//!
//! - `stopped_evidence` records valid native session-end evidence. System suspension or
//!   native scan failure can establish it without any explicit stop command.
//! - `StopReservation` occupies the session's explicit stop command slot and
//!   retains dispatch progress, terminal results, and native stop errors. Its
//!   lifetime is independent of the caller's waiter, but owned by the session.
//!
//! These combinations describe different situations, not redundant state:
//!
//! | stopped_evidence | stop | Meaning |
//! |---------|------|---------|
//! | false | None | No observed session end and no explicit stop command. |
//! | false | Some | Explicit stop is reserved; session end is not yet observed. |
//! | true | None | Session ended without a retained explicit stop command. |
//! | true | Some | Session end is observed; stop command evidence is still retained. |
//!
//! Session end alone does not prove successful stop command completion: dispatch
//! acceptance and a successful terminal are also required. Settlement and retirement
//! combine the session facts with command evidence. Keep them together under the
//! session owner, but do not move `stopped_evidence` into the optional child: passive teardown
//! must remain representable when no stop command exists. Nesting expresses ownership;
//! a reservation need not exist independently to reserve a slot and retain evidence.

use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ScanSessionId, ScanStopReason,
    ScanStopResult,
};

use super::super::shared::CompletionObserver;
use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::dispatch::{
    BufferedEventDisposition, CancellationDecision, CommandProgress, DispatchAction,
    DispatchResolution, ReconciliationFault,
};

/// Owns the single scan slot, command admission, and correlated start/stop evidence.
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
pub struct ScanState {
    phase: ScanPhase,
}

#[derive(Default)]
enum ScanPhase {
    #[default]
    Idle,
    // Keep the idle actor compact; allocate once per admitted scan session.
    Reserved(Box<ScanReservation>),
}

impl ScanState {
    /// Gates live callbacks before publication; this is not an ownership query.
    /// Some(event) passes through, including owned events needing no buffering.
    /// None retains the event pending dispatch resolution; overflow is an error.
    pub fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(scan) = self.reserved_mut() else {
            return Ok(Some(event));
        };
        scan.gate_event(event)
    }

    pub fn record_event(&mut self, event: &BleEvent) {
        if let Some(scan) = self.reserved_mut() {
            scan.record_event(event);
        }
    }

    /// Returns None when neither the scan start nor its stop owns this command.
    /// Otherwise updates reservations and returns ordered replay plus actor actions.
    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let scan = self.reserved_mut()?;
        if scan.start_progress.owns_command(command_id) {
            if result.is_ok() {
                return Some(scan.start_progress.accepted_resolution());
            }
            // A rejected start with correlated callbacks may already own a native scan.
            let resolution = scan
                .start_progress
                .rejected_resolution(DispatchAction::None, |_| BufferedEventDisposition::Conflict);
            self.phase = ScanPhase::Idle;
            return Some(resolution);
        }
        let stop = scan.stop_for_mut(command_id)?;
        let resolution = stop.resolve_dispatch(result);
        if result.is_err() {
            scan.stop = None;
        }
        Some(resolution)
    }

    pub fn stop_command(&self) -> Option<CommandId> {
        self.reserved()?
            .stop
            .as_ref()
            .map(|stop| stop.progress.command_id())
    }

    pub fn request_cancellation(&mut self, command_id: CommandId) -> Option<CancellationDecision> {
        let scan = self.reserved_mut()?;
        if scan.start_progress.owns_command(command_id) {
            return Some(scan.start_progress.request_cancellation());
        }
        if scan
            .stop
            .as_ref()
            .is_some_and(|stop| stop.progress.owns_command(command_id))
        {
            return Some(CancellationDecision::NotCancellable);
        }
        None
    }

    fn progress(&self, command_id: CommandId) -> Option<&CommandProgress> {
        let scan = self.reserved()?;
        if scan.start_progress.owns_command(command_id) {
            return Some(&scan.start_progress);
        }
        scan.stop_for(command_id).map(|stop| &stop.progress)
    }

    pub fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.progress(command_id)
            .map(CommandProgress::completion_observer)
    }

    pub fn allow_publication(
        &mut self,
        event: &BleEvent,
    ) -> Result<Option<bool>, ReconciliationFault> {
        let Some(scan) = self.reserved() else {
            return Ok(None);
        };
        if !scan.matches_publication_scope(event) {
            return Ok(None);
        }
        let accepted_target = event
            .command_id()
            .and_then(|id| self.progress(id))
            .is_some_and(CommandProgress::cancellation_accepted);
        let scan = self.reserved_mut().expect("matched scan publication scope");
        let pending = scan.has_pending_cancellation();
        scan.publication
            .allow(event, pending, accepted_target)
            .map(Some)
    }

    pub fn take_cancellation_public_events(
        &mut self,
        command_id: CommandId,
    ) -> Option<Vec<BleEvent>> {
        let accepted = self.progress(command_id)?.cancellation_accepted();
        let scan = self.reserved_mut()?;
        let pending = scan.has_pending_cancellation();
        Some(scan.publication.resolve(command_id, accepted, pending))
    }

    pub fn take_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.take_cancellation_dispatch(command_id))
    }

    pub fn resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.resolve_cancellation(command_id, result))
    }

    pub fn clear_cancellation(&mut self, command_id: CommandId) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.clear_cancellation(command_id))
    }

    pub fn cancellation_requested(&self, command_id: CommandId) -> bool {
        self.reserved()
            .is_some_and(|scan| scan.cancellation_requested(command_id))
    }

    pub fn require_scan_available(&self) -> Result<(), WaiterError> {
        match &self.phase {
            ScanPhase::Idle => Ok(()),
            ScanPhase::Reserved(_) => Err(WaiterError::ScanBusy),
        }
    }

    pub fn validate_scan_stop(&self, session_id: ScanSessionId) -> Result<(), WaiterError> {
        self.reserved()
            .ok_or(WaiterError::ScanNotActive)?
            .validate_stop(session_id)
    }

    /// Admission and caller submission must succeed before installation.
    pub fn reserve_scan_start(&mut self, command_id: CommandId, session_id: ScanSessionId) {
        assert!(
            self.require_scan_available().is_ok(),
            "validated scan slot is occupied"
        );
        self.phase = ScanPhase::Reserved(Box::new(ScanReservation::new(command_id, session_id)));
    }

    /// Installs stop only after start completion, admission, and caller submission.
    /// Invalid installation is an invariant violation, not a normal error path.
    pub fn reserve_scan_stop(&mut self, command_id: CommandId) {
        self.reserved_mut()
            .expect("validated scan reservation")
            .reserve_scan_stop(command_id);
    }

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

    /// Returns command ownership, including duplicates, not operation success.
    pub fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(scan) = self.reserved_mut() else {
            return false;
        };
        scan.record_terminal(command_id, result)
    }

    /// Queries a deliverable result without retiring the session or stop command.
    /// None means unowned or incomplete, not operation failure.
    pub fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        self.reserved()?.waiter_result(command_id)
    }

    /// Called after waiter settlement, including when the caller already retired.
    /// Returns every command whose reservation was actually released.
    pub fn retire_completed(&mut self) -> Vec<CommandId> {
        let Some(scan) = self.reserved_mut() else {
            return Vec::new();
        };
        if scan.is_retirable() {
            let mut retired = vec![scan.start_progress.command_id()];
            if let Some(stop) = &scan.stop {
                retired.push(stop.progress.command_id());
            }
            self.phase = ScanPhase::Idle;
            return retired;
        }
        scan.retire_failed_stop().into_iter().collect()
    }
}

/// Retained until accepted and late native start/stop callbacks are accounted for.
struct ScanReservation {
    // Start command progress; stop has its own command and acknowledgement.
    start_progress: CommandProgress,
    // One 64-event publication cap covers start/stop and passive scan events.
    publication: super::dispatch::ScopedPublication,
    session_id: ScanSessionId,
    // Session-level teardown evidence. ScanStopped::Err has no command ID and can
    // race start, so it must not be attributed to start as a start-domain error.
    // If an explicit stop exists, StopReservation separately retains that error
    // for the stop waiter; without one, this remains only a session-end fact.
    stopped_evidence: bool,
    started_evidence: bool,
    stop: Option<StopReservation>,
}

impl ScanReservation {
    fn has_pending_cancellation(&self) -> bool {
        self.start_progress.is_awaiting_cancellation_result()
            || self
                .stop
                .as_ref()
                .is_some_and(|stop| stop.progress.is_awaiting_cancellation_result())
    }

    fn matches_publication_scope(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::ScanStarted { scan_id, .. } | BleEvent::ScanStopped { scan_id, .. } => {
                *scan_id == self.session_id
            }
            BleEvent::AdvertisementObserved { advertisement } => {
                advertisement.scan_session_id == self.session_id
            }
            BleEvent::CommandCompleted { command_id, .. } => {
                self.start_progress.owns_command(*command_id)
                    || self.stop_for(*command_id).is_some()
            }
            _ => false,
        }
    }

    /// Routes unresolved start evidence before stop evidence within this session.
    fn take_cancellation_dispatch(&mut self, command_id: CommandId) -> bool {
        if self.start_progress.owns_command(command_id) {
            return self.start_progress.take_cancellation_dispatch();
        }
        self.stop
            .as_mut()
            .filter(|stop| stop.progress.owns_command(command_id))
            .is_some_and(|stop| stop.progress.take_cancellation_dispatch())
    }

    fn resolve_cancellation(
        &mut self,
        command_id: CommandId,
        result: Result<(), WaiterError>,
    ) -> bool {
        if self.start_progress.owns_command(command_id) {
            self.start_progress.resolve_cancellation(result);
            return true;
        }
        let Some(stop) = self
            .stop
            .as_mut()
            .filter(|stop| stop.progress.owns_command(command_id))
        else {
            return false;
        };
        stop.progress.resolve_cancellation(result);
        true
    }

    fn clear_cancellation(&mut self, command_id: CommandId) -> bool {
        if self.start_progress.owns_command(command_id) {
            self.start_progress.clear_cancellation();
            return true;
        }
        let Some(stop) = self
            .stop
            .as_mut()
            .filter(|stop| stop.progress.owns_command(command_id))
        else {
            return false;
        };
        stop.progress.clear_cancellation();
        true
    }

    fn cancellation_requested(&self, command_id: CommandId) -> bool {
        if self.start_progress.owns_command(command_id) {
            return self.start_progress.cancellation_requested();
        }
        self.stop
            .as_ref()
            .filter(|stop| stop.progress.owns_command(command_id))
            .is_some_and(|stop| stop.progress.cancellation_requested())
    }

    fn gate_event(&mut self, event: BleEvent) -> Result<Option<BleEvent>, ReconciliationFault> {
        if self.should_buffer_start_event(&event) {
            self.start_progress.buffer(event)?;
            return Ok(None);
        }
        if let Some(stop) = self.stop.as_mut()
            && stop.should_buffer_event(&event, self.session_id)
        {
            stop.progress.buffer(event)?;
            return Ok(None);
        }
        Ok(Some(event))
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

    fn record_event(&mut self, event: &BleEvent) {
        match event {
            BleEvent::ScanStarted {
                command_id,
                scan_id,
            } if self.start_progress.owns_command(*command_id) && *scan_id == self.session_id => {
                self.started_evidence = true;
            }
            BleEvent::ScanStopped { scan_id, result } if *scan_id == self.session_id => {
                if let Some(stop) = self.stop.as_mut() {
                    stop.record_stop_result(result);
                }
                match result {
                    // Passive native teardown ends the session even without a stop command.
                    ScanStopResult::Err { .. }
                    | ScanStopResult::Ok {
                        reason: ScanStopReason::SystemSuspended,
                    } => {
                        self.stopped_evidence = true;
                    }
                    ScanStopResult::Ok {
                        reason: ScanStopReason::Requested { command_id },
                    } => {
                        if let Some(stop) = &self.stop
                            && stop.confirms_requested_stop(*command_id)
                        {
                            self.stopped_evidence = true;
                        }
                    }
                }
            }
            _ => {}
        }
    }

    fn new(command_id: CommandId, session_id: ScanSessionId) -> Self {
        Self {
            start_progress: CommandProgress::new(command_id),
            publication: super::dispatch::ScopedPublication::default(),
            session_id,
            stopped_evidence: false,
            started_evidence: false,
            stop: None,
        }
    }

    fn validate_stop(&self, session: ScanSessionId) -> Result<(), WaiterError> {
        if self.session_id != session || self.start_failed() || self.stopped_evidence {
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
        self.stop = Some(StopReservation {
            progress: CommandProgress::new(command_id),
            stop_error_evidence: None,
        });
    }

    fn start_failed(&self) -> bool {
        self.start_progress.terminal_failed()
    }

    fn stop_for(&self, command_id: CommandId) -> Option<&StopReservation> {
        self.stop
            .as_ref()
            .filter(|stop| stop.progress.owns_command(command_id))
    }

    fn stop_for_mut(&mut self, command_id: CommandId) -> Option<&mut StopReservation> {
        self.stop
            .as_mut()
            .filter(|stop| stop.progress.owns_command(command_id))
    }

    /// Returns whether scan start or stop owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if self.start_progress.owns_command(command_id) {
            self.start_progress.record_terminal(result);
            return true;
        }
        if let Some(stop) = self.stop_for_mut(command_id) {
            stop.progress.record_terminal(result);
            return true;
        }
        false
    }

    fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        if self.start_progress.owns_command(command_id) {
            if let Err(error) = self.start_progress.terminal_result()? {
                return Some(Err(error));
            }
            return self
                .started_evidence
                .then_some(Ok(WaiterSuccess::ScanStarted(self.session_id)));
        }
        self.stop_for(command_id)?
            .waiter_result(self.session_id, self.stopped_evidence)
    }

    /// Pure session-retirement decision, evaluated after waiter settlement.
    /// Complete command evidence does not itself prove the session has ended.
    fn is_retirable(&self) -> bool {
        if !self.publication.is_empty() {
            return false;
        }
        // Native end cannot release a start whose domain/terminal is still missing.
        if !self.start_progress.is_complete(self.started_evidence) {
            return false;
        }
        if let Some(stop) = &self.stop
            && !stop.is_complete(self.stopped_evidence)
        {
            return false;
        }
        // A failed stop alone leaves the scan running; retire only its child slot.
        self.start_failed() || self.stopped_evidence
    }

    /// Releases only a failed stop command, leaving the session available to retry.
    /// The caller has already settled its waiter and checked whole-session retirement.
    fn retire_failed_stop(&mut self) -> Option<CommandId> {
        let stop = self.stop.as_ref()?;
        if !stop.can_retire_after_failure() {
            return None;
        }
        let command_id = stop.progress.command_id();
        self.stop = None;
        Some(command_id)
    }
}

/// Reserves the scan session's explicit stop slot and retains command evidence
/// independently of the caller's waiter. Owned and retired by the scan session.
struct StopReservation {
    progress: CommandProgress,
    stop_error_evidence: Option<BleEventError>,
}

impl StopReservation {
    fn resolve_dispatch(
        &mut self,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        if result.is_ok() {
            return self.progress.accepted_resolution();
        }
        self.progress
            .rejected_resolution(DispatchAction::None, |event| match event {
                // Passive native teardown does not prove the rejected stop ran.
                BleEvent::ScanStopped {
                    result:
                        ScanStopResult::Ok {
                            reason: ScanStopReason::SystemSuspended,
                        }
                        | ScanStopResult::Err { .. },
                    ..
                } => BufferedEventDisposition::Replay,
                BleEvent::ScanStopped { .. } => BufferedEventDisposition::Conflict,
                // A rejected command's terminal cannot complete the native scan.
                _ => BufferedEventDisposition::Discard,
            })
    }

    /// Records native stop failure after the parent has correlated the session.
    /// Successful results do not erase failure evidence or settle the command alone.
    fn record_stop_result(&mut self, result: &ScanStopResult) {
        if let ScanStopResult::Err { error } = result {
            self.stop_error_evidence = Some(error.clone());
        }
    }

    /// Requested teardown must belong to this stop and follow dispatch acceptance.
    /// This confirms session end, not completion of the stop command's terminal.
    fn confirms_requested_stop(&self, command_id: CommandId) -> bool {
        self.progress.owns_command(command_id) && !self.progress.is_awaiting_dispatch_result()
    }

    /// Buffers related events only while stop dispatch is unresolved, including
    /// passive teardown. Requested-stop identity is checked when recording.
    fn should_buffer_event(&self, event: &BleEvent, session_id: ScanSessionId) -> bool {
        if !self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        match event {
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            BleEvent::ScanStopped { scan_id, .. } => *scan_id == session_id,
            _ => false,
        }
    }

    /// Enough evidence to settle the command, even if its caller has retired.
    /// Native failure proves teardown and does not require a terminal callback.
    fn is_complete(&self, stopped_evidence: bool) -> bool {
        if self.progress.is_awaiting_dispatch_result() {
            return false;
        }
        if self.stop_error_evidence.is_some() {
            return true;
        }
        self.progress.is_complete(stopped_evidence)
    }

    /// A confirmed terminal failure can release this slot after waiter settlement.
    /// The parent has already checked whether the entire session should retire.
    fn can_retire_after_failure(&self) -> bool {
        !self.progress.is_awaiting_dispatch_result() && self.progress.terminal_failed()
    }

    fn waiter_result(
        &self,
        session_id: ScanSessionId,
        stopped_evidence: bool,
    ) -> Option<WaiterOutcome> {
        if !self.is_complete(stopped_evidence) {
            return None;
        }
        if let Some(error) = &self.stop_error_evidence {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::ScanStopped(session_id)))
    }
}
