use blehub::{
    BleEvent, BleEventError, CommandDispatchError, CommandId, ScanSessionId, ScanStopReason,
    ScanStopResult,
};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::dispatch::{
    DispatchEffect, DispatchReservation, DispatchResolution, ReconcileFault, record_terminal,
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
    pub fn buffer_before_dispatch(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconcileFault> {
        if let Some(scan) = self.reserved_mut() {
            if scan.dispatch.is_awaiting_dispatch_ack() && scan.matches_start_event(&event) {
                scan.dispatch.buffer(event)?;
                return Ok(None);
            }
            if let Some(stop) = scan.stop.as_mut()
                && stop.dispatch.is_awaiting_dispatch_ack()
                && stop.matches_event(&event, scan.session_id)
            {
                stop.dispatch.buffer(event)?;
                return Ok(None);
            }
        }
        Ok(Some(event))
    }

    pub fn record_event(&mut self, event: &BleEvent) -> Result<(), ReconcileFault> {
        if let Some(scan) = self.reserved_mut() {
            scan.record_event(event);
        }
        Ok(())
    }

    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let scan = self.reserved_mut()?;
        if scan.dispatch.command_id == command_id {
            return Some(if result.is_err() {
                // A rejected start with correlated callbacks may already own a native scan.
                let contradictory = !scan.dispatch.buffered.is_empty();
                self.reject_start(command_id);
                DispatchResolution {
                    effect: if contradictory {
                        DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
                    } else {
                        DispatchEffect::None
                    },
                    ..Default::default()
                }
            } else {
                scan.dispatch.accepted_resolution()
            });
        }
        let stop = scan
            .stop
            .as_mut()
            .filter(|stop| stop.dispatch.command_id == command_id)?;
        Some(if result.is_err() {
            let contradictory = stop
                .dispatch
                .buffered
                .iter()
                .any(|event| matches!(event, BleEvent::ScanStopped { .. }));
            scan.stop = None;
            DispatchResolution {
                effect: if contradictory {
                    DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
                } else {
                    DispatchEffect::None
                },
                ..Default::default()
            }
        } else {
            stop.dispatch.accepted_resolution()
        })
    }

    pub fn stop_command(&self) -> Option<CommandId> {
        self.reserved()?
            .stop
            .as_ref()
            .map(|stop| stop.dispatch.command_id)
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

    /// A late rejection must never release a different scan's reservation.
    fn reject_start(&mut self, command_id: CommandId) {
        if self
            .reserved()
            .is_some_and(|scan| scan.dispatch.command_id == command_id)
        {
            *self = Self::Idle;
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

    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        self.reserved()?.waiter_result(command_id)
    }

    /// Called after waiter settlement, including for late native evidence.
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
    dispatch: DispatchReservation,
    session_id: ScanSessionId,
    stopped: bool,
    started: bool,
    start_terminal: Option<Result<(), BleEventError>>,
    stop: Option<StopReservation>,
}

impl ScanReservation {
    fn matches_start_event(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::ScanStarted { command_id, .. }
            | BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.dispatch.command_id
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
            } if *command_id == self.dispatch.command_id && *scan_id == self.session_id => {
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
                            stop.dispatch.command_id == *command_id
                                && !stop.dispatch.is_awaiting_dispatch_ack()
                        })
                    }
                };
            }
            _ => {}
        }
    }

    fn new(command_id: CommandId, session_id: ScanSessionId) -> Self {
        Self {
            dispatch: DispatchReservation::new(command_id),
            session_id,
            stopped: false,
            started: false,
            start_terminal: None,
            stop: None,
        }
    }

    fn validate_stop(&self, session: ScanSessionId) -> Result<(), WaiterError> {
        if self.session_id != session || self.has_failed() || self.stopped {
            return Err(WaiterError::ScanNotActive);
        }
        // Native stop must wait for start dispatch and domain/terminal completion.
        if self.dispatch.is_awaiting_dispatch_ack()
            || self.start_terminal.is_none()
            || !self.started
            || self.stop.is_some()
        {
            return Err(WaiterError::ScanBusy);
        }
        Ok(())
    }

    fn reserve_scan_stop(&mut self, command_id: CommandId) {
        self.stop = Some(StopReservation {
            dispatch: DispatchReservation::new(command_id),
            terminal: None,
            stop_error: None,
        });
    }

    fn has_failed(&self) -> bool {
        self.start_terminal.as_ref().is_some_and(Result::is_err)
    }

    /// Returns whether scan start or stop owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        if self.dispatch.command_id == command_id {
            record_terminal(&mut self.start_terminal, result);
            return true;
        }
        if let Some(stop) = self
            .stop
            .as_mut()
            .filter(|stop| stop.dispatch.command_id == command_id)
        {
            record_terminal(&mut stop.terminal, result);
            return true;
        }
        false
    }

    fn waiter_result(&self, command_id: CommandId) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.dispatch.command_id == command_id {
            return self.dispatch.waiter_result(&self.start_terminal, || {
                self.started
                    .then_some(WaiterSuccess::ScanStarted(self.session_id))
            });
        }
        self.stop
            .as_ref()
            .filter(|stop| stop.dispatch.command_id == command_id)?
            .waiter_result(self.session_id, self.stopped)
    }

    /// Settle the waiter before calling this. An error stop callback also proves
    /// native teardown, even if the explicit stop terminal has not arrived.
    fn retire_completed(&mut self) -> bool {
        if let Some(stop) = &self.stop {
            if !stop.dispatch.is_awaiting_dispatch_ack()
                && self.stopped
                && (stop.terminal.is_some() || stop.stop_error.is_some())
            {
                return true;
            }
            if !stop.dispatch.is_awaiting_dispatch_ack()
                && stop.terminal.as_ref().is_some_and(Result::is_err)
            {
                self.stop = None;
            }
        }
        // Native end does not settle a start whose domain/terminal is still
        // missing. Keep its evidence until settlement can precede retirement.
        self.stop.is_none()
            && self
                .dispatch
                .is_complete(&self.start_terminal, self.started)
            && (self.has_failed() || self.stopped)
    }
}

struct StopReservation {
    dispatch: DispatchReservation,
    terminal: Option<Result<(), BleEventError>>,
    stop_error: Option<BleEventError>,
}

impl StopReservation {
    fn matches_event(&self, event: &BleEvent, session_id: ScanSessionId) -> bool {
        match event {
            BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.dispatch.command_id
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
        if self.dispatch.is_awaiting_dispatch_ack() {
            return None;
        }
        if let Some(error) = &self.stop_error {
            return Some(Err(WaiterError::Operation(error.clone())));
        }
        self.dispatch.waiter_result(&self.terminal, || {
            stopped.then_some(WaiterSuccess::ScanStopped(session_id))
        })
    }
}
