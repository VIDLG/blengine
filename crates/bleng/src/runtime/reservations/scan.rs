use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ScanSessionId, ScanStopReason,
    ScanStopResult,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::dispatch::{
    BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
    ReconciliationFault,
};

/// Owns the single scan slot and its transition back to idle.
/// Reserved includes pending, running, and stopped-but-unsettled sessions;
/// native stop alone does not make the slot available for another scan.
#[derive(Default)]
pub enum ScanState {
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
        if let Some(scan) = self.reserved_mut() {
            if scan.progress.is_awaiting_dispatch_result() && scan.matches_start_event(&event) {
                scan.progress.buffer(event)?;
                return Ok(None);
            }
            if let Some(stop) = scan.stop.as_mut()
                && stop.progress.is_awaiting_dispatch_result()
                && stop.matches_event(&event, scan.session_id)
            {
                stop.progress.buffer(event)?;
                return Ok(None);
            }
        }
        Ok(Some(event))
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
        if scan.progress.command_id == command_id {
            return Some(if result.is_err() {
                // A rejected start with correlated callbacks may already own a native scan.
                let resolution = scan
                    .progress
                    .rejected_resolution(DispatchAction::None, |_| {
                        BufferedEventDisposition::Conflict
                    });
                *self = Self::Idle;
                resolution
            } else {
                scan.progress.accepted_resolution()
            });
        }
        let stop = scan.stop_for_mut(command_id)?;
        Some(if result.is_err() {
            let resolution = stop
                .progress
                .rejected_resolution(DispatchAction::None, |event| {
                    match event {
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
                    }
                });
            scan.stop = None;
            resolution
        } else {
            stop.progress.accepted_resolution()
        })
    }

    pub fn stop_command(&self) -> Option<CommandId> {
        self.reserved()?
            .stop
            .as_ref()
            .map(|stop| stop.progress.command_id)
    }

    pub fn require_scan_available(&self) -> Result<(), WaiterError> {
        match self {
            Self::Idle => Ok(()),
            Self::Reserved(_) => Err(WaiterError::ScanBusy),
        }
    }

    pub fn validate_scan_stop(&self, session_id: ScanSessionId) -> Result<(), WaiterError> {
        self.reserved()
            .ok_or(WaiterError::ScanNotActive)?
            .validate_stop(session_id)
    }

    /// Admission and caller handoff must succeed before installation.
    pub fn reserve_scan_start(&mut self, command_id: CommandId, session_id: ScanSessionId) {
        assert!(
            matches!(self, Self::Idle),
            "validated scan slot is occupied"
        );
        *self = Self::Reserved(Box::new(ScanReservation::new(command_id, session_id)));
    }

    pub fn reserve_scan_stop(&mut self, command_id: CommandId) {
        self.reserved_mut()
            .expect("validated scan reservation")
            .reserve_scan_stop(command_id);
    }

    fn reserved(&self) -> Option<&ScanReservation> {
        match self {
            Self::Idle => None,
            Self::Reserved(scan) => Some(scan),
        }
    }

    fn reserved_mut(&mut self) -> Option<&mut ScanReservation> {
        match self {
            Self::Idle => None,
            Self::Reserved(scan) => Some(scan),
        }
    }

    /// Returns command ownership, including duplicates, not operation success.
    pub fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        self.reserved_mut()
            .is_some_and(|scan| scan.record_terminal(command_id, result))
    }

    /// Queries a deliverable result without retiring the session or stop command.
    /// None means unowned or incomplete, not operation failure.
    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.reserved()?.waiter_result(command_id)
    }

    /// Called after waiter settlement, including when the caller already retired.
    /// Removes the session only when native end and outstanding command evidence
    /// permit it; otherwise retains the session or retires only a failed stop.
    pub fn retire_completed(&mut self) {
        if self
            .reserved_mut()
            .is_some_and(ScanReservation::retire_completed)
        {
            *self = Self::Idle;
        }
    }
}

/// Retained until accepted and late native start/stop callbacks are accounted for.
// The public enum carries this opaque payload; its fields stay local to this module.
pub struct ScanReservation {
    progress: CommandProgress,
    session_id: ScanSessionId,
    stopped: bool,
    started: bool,
    stop: Option<StopReservation>,
}

impl ScanReservation {
    fn matches_start_event(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::ScanStarted { command_id, .. }
            | BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.progress.command_id
            }
            BleEvent::AdvertisementObserved { advertisement } => {
                advertisement.scan_session_id == self.session_id
            }
            BleEvent::ScanStopped { scan_id, .. } => *scan_id == self.session_id,
            _ => false,
        }
    }

    fn record_event(&mut self, event: &BleEvent) {
        match event {
            BleEvent::ScanStarted {
                command_id,
                scan_id,
            } if *command_id == self.progress.command_id && *scan_id == self.session_id => {
                self.started = true;
            }
            BleEvent::ScanStopped {
                scan_id,
                result: ScanStopResult::Err { error },
            } if *scan_id == self.session_id => {
                // Native scan failure proves teardown, unlike a rejected stop command.
                self.stopped = true;
                if let Some(stop) = self.stop.as_mut() {
                    stop.stop_error = Some(error.clone());
                }
            }
            BleEvent::ScanStopped {
                scan_id,
                result: ScanStopResult::Ok { reason },
            } if *scan_id == self.session_id => {
                self.stopped |= match reason {
                    ScanStopReason::SystemSuspended => true,
                    ScanStopReason::Requested { command_id } => {
                        self.stop.as_ref().is_some_and(|stop| {
                            stop.progress.command_id == *command_id
                                && !stop.progress.is_awaiting_dispatch_result()
                        })
                    }
                };
            }
            _ => {}
        }
    }

    fn new(command_id: CommandId, session_id: ScanSessionId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            session_id,
            stopped: false,
            started: false,
            stop: None,
        }
    }

    fn validate_stop(&self, session: ScanSessionId) -> Result<(), WaiterError> {
        if self.session_id != session || self.has_failed() || self.stopped {
            return Err(WaiterError::ScanNotActive);
        }
        // Native stop must wait for start dispatch and domain/terminal completion.
        if !self.progress.is_complete(self.started) || self.stop.is_some() {
            return Err(WaiterError::ScanBusy);
        }
        Ok(())
    }

    fn reserve_scan_stop(&mut self, command_id: CommandId) {
        self.stop = Some(StopReservation {
            progress: CommandProgress::new(command_id),
            stop_error: None,
        });
    }

    fn has_failed(&self) -> bool {
        self.progress.terminal_failed()
    }

    fn stop_for(&self, command_id: CommandId) -> Option<&StopReservation> {
        self.stop
            .as_ref()
            .filter(|stop| stop.progress.command_id == command_id)
    }

    fn stop_for_mut(&mut self, command_id: CommandId) -> Option<&mut StopReservation> {
        self.stop
            .as_mut()
            .filter(|stop| stop.progress.command_id == command_id)
    }

    /// Returns whether scan start or stop owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if self.progress.command_id == command_id {
            self.progress.record_terminal(result);
            return true;
        }
        if let Some(stop) = self.stop_for_mut(command_id) {
            stop.progress.record_terminal(result);
            return true;
        }
        false
    }

    fn waiter_result(&self, command_id: CommandId) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.progress.command_id == command_id {
            if let Err(error) = self.progress.terminal_result()? {
                return Some(Err(error));
            }
            return self
                .started
                .then_some(Ok(WaiterSuccess::ScanStarted(self.session_id)));
        }
        self.stop_for(command_id)?
            .waiter_result(self.session_id, self.stopped)
    }

    /// Settle the waiter before calling this. An error stop callback also proves
    /// native teardown, even if the explicit stop terminal has not arrived.
    fn retire_completed(&mut self) -> bool {
        if let Some(stop) = &self.stop {
            if !stop.progress.is_awaiting_dispatch_result()
                && self.stopped
                && (stop.progress.has_terminal() || stop.stop_error.is_some())
            {
                return true;
            }
            if !stop.progress.is_awaiting_dispatch_result() && stop.progress.terminal_failed() {
                self.stop = None;
            }
        }
        // Native end does not settle a start whose domain/terminal is still
        // missing. Keep its evidence until settlement can precede retirement.
        self.stop.is_none()
            && self.progress.is_complete(self.started)
            && (self.has_failed() || self.stopped)
    }
}

struct StopReservation {
    progress: CommandProgress,
    stop_error: Option<BleEventError>,
}

impl StopReservation {
    fn matches_event(&self, event: &BleEvent, session_id: ScanSessionId) -> bool {
        match event {
            BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.progress.command_id
            }
            BleEvent::ScanStopped { scan_id, .. } => *scan_id == session_id,
            _ => false,
        }
    }

    fn waiter_result(
        &self,
        session_id: ScanSessionId,
        stopped: bool,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.progress.is_awaiting_dispatch_result() {
            return None;
        }
        if let Some(error) = &self.stop_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        stopped.then_some(Ok(WaiterSuccess::ScanStopped(session_id)))
    }
}
