use blehub::{BleEvent, BleEventError, CommandId};

use crate::waiter::WaiterError;

#[derive(Debug)]
pub enum ReconciliationFault {
    BufferOverflow,
    UnexpectedNotificationMode,
    DispatchEvidenceConflict,
}

#[derive(Default)]
pub enum DispatchAction {
    #[default]
    None,
    StartCleanup,
    FaultRuntime(ReconciliationFault),
}

#[derive(Default)]
pub struct DispatchResolution {
    pub events_to_replay: Vec<BleEvent>,
    pub action: DispatchAction,
}

/// How to handle one buffered event after dispatch rejection. The reservation
/// supplies the context-dependent decision; it is not an intrinsic event property.
pub(super) enum BufferedEventDisposition {
    Replay,
    Discard,
    Conflict,
}

/// Common dispatch and terminal progress, not a generic BLE operation.
/// Domain evidence and operation-specific error precedence remain in each record.
pub(super) struct CommandProgress {
    pub(super) command_id: CommandId,
    dispatch_accepted: bool,
    buffered_events: Vec<BleEvent>,
    terminal_result: Option<Result<(), BleEventError>>,
}

impl CommandProgress {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            dispatch_accepted: false,
            buffered_events: Vec::new(),
            terminal_result: None,
        }
    }

    pub(super) fn is_awaiting_dispatch_result(&self) -> bool {
        !self.dispatch_accepted
    }

    pub(super) fn buffer(&mut self, event: BleEvent) -> Result<(), ReconciliationFault> {
        const PREDISPATCH_EVENTS: usize = 64;
        if self.buffered_events.len() >= PREDISPATCH_EVENTS {
            return Err(ReconciliationFault::BufferOverflow);
        }
        self.buffered_events.push(event);
        Ok(())
    }

    /// Transfers evidence without acknowledging dispatch or choosing a replay policy.
    fn take_buffered_events(&mut self) -> Vec<BleEvent> {
        std::mem::take(&mut self.buffered_events)
    }

    /// Acknowledges acceptance and transfers early callbacks for ordered replay.
    /// Normal acceptance requests no cleanup or fault action.
    pub(super) fn accepted_resolution(&mut self) -> DispatchResolution {
        self.dispatch_accepted = true;
        DispatchResolution {
            events_to_replay: self.take_buffered_events(),
            action: DispatchAction::None,
        }
    }

    /// Drains early evidence without acknowledging acceptance. The reservation
    /// chooses replay, discard, or conflict for each event and owns resource teardown.
    /// Any conflict overrides the clean-rejection action, but classification continues
    /// so permitted events retain their order. The actor executes the resulting action.
    pub(super) fn rejected_resolution(
        &mut self,
        on_clean_rejection: DispatchAction,
        classify: impl Fn(&BleEvent) -> BufferedEventDisposition,
    ) -> DispatchResolution {
        let mut resolution = DispatchResolution {
            events_to_replay: Vec::new(),
            action: on_clean_rejection,
        };
        for event in self.take_buffered_events() {
            match classify(&event) {
                BufferedEventDisposition::Replay => resolution.events_to_replay.push(event),
                BufferedEventDisposition::Discard => {}
                BufferedEventDisposition::Conflict => {
                    resolution.action =
                        DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict);
                }
            }
        }
        resolution
    }

    /// The first terminal is authoritative; duplicate callbacks cannot replace it.
    pub(super) fn record_terminal(&mut self, result: &Result<(), BleEventError>) {
        if self.terminal_result.is_none() {
            self.terminal_result = Some(result.clone());
        }
    }

    pub(super) fn has_terminal(&self) -> bool {
        self.terminal_result.is_some()
    }

    pub(super) fn terminal_succeeded(&self) -> bool {
        self.terminal_result == Some(Ok(()))
    }

    pub(super) fn terminal_failed(&self) -> bool {
        self.terminal_result.as_ref().is_some_and(Result::is_err)
    }

    /// Returns the recorded terminal only after dispatch acceptance. Success says
    /// nothing about domain evidence; the reservation must still supply that evidence.
    pub(super) fn terminal_result(&self) -> Option<Result<(), WaiterError>> {
        if self.is_awaiting_dispatch_result() {
            return None;
        }
        self.terminal_result
            .clone()
            .map(|result| result.map_err(WaiterError::Operation))
    }

    /// Command evidence is complete after accepted dispatch and a terminal failure,
    /// or a terminal success with all required correlated domain evidence.
    /// This neither depends on a live waiter nor proves native resources can retire.
    pub(super) fn is_complete(&self, domain_evidence_ready: bool) -> bool {
        if !self.dispatch_accepted {
            return false;
        }
        match self.terminal_result.as_ref() {
            None => false,
            Some(Err(_)) => true,
            Some(Ok(())) => domain_evidence_ready,
        }
    }
}
