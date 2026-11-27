use blehub::{BleEvent, BleEventError, CommandId};

use crate::{runtime::shared::CompletionObserver, waiter::WaiterError};

use super::cancellation::{CancellationDecision, CancellationState};

/// Maximum number of callbacks retained for one command before dispatch is
/// acknowledged.
const MAX_BUFFERED_DISPATCH_EVENTS_PER_COMMAND: usize = 64;

#[derive(Debug)]
pub(in crate::runtime) enum ReconciliationFault {
    BufferOverflow,
    UnexpectedNotificationMode,
    DispatchEvidenceConflict,
}

/// Runtime work requested by reconciliation; only the actor executes it.
/// This is not a backend BLE command or an already-completed lifecycle effect.
#[derive(Default)]
pub(in crate::runtime) enum DispatchAction {
    #[default]
    NoAction,
    StartCleanup,
    FaultRuntime(ReconciliationFault),
}

/// Transient work produced by reconciling a native dispatch return with early callbacks.
/// Transfers owned callbacks to the actor, which handles the action before permitted replay.
/// This is neither the operation's terminal result nor the caller submission/claim handoff;
/// it does not transfer native ownership or prove cleanup has completed.
#[derive(Default)]
pub(in crate::runtime) struct DispatchResolution {
    pub(in crate::runtime) events_to_replay: Vec<BleEvent>,
    pub(in crate::runtime) action: DispatchAction,
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
    command_id: CommandId,
    completion_observer: CompletionObserver,
    dispatch_accepted: bool,
    // Native callbacks can arrive during dispatch, before its result is reconciled.
    dispatch_pending_events: Vec<BleEvent>,
    terminal_evidence: Option<Result<(), BleEventError>>,
    cancellation: CancellationState,
}

impl CommandProgress {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            completion_observer: CompletionObserver::default(),
            dispatch_accepted: false,
            dispatch_pending_events: Vec::new(),
            terminal_evidence: None,
            cancellation: CancellationState::default(),
        }
    }

    pub(super) fn completion_observer(&self) -> CompletionObserver {
        self.completion_observer.clone()
    }

    delegate::delegate! {
        to self.cancellation {
            #[call(is_accepted)]
            pub(super) fn is_cancellation_accepted(&self) -> bool;
            #[call(has_request)]
            pub(super) fn is_cancellation_requested(&self) -> bool;
            #[call(is_awaiting_result)]
            pub(super) fn is_awaiting_cancellation_result(&self) -> bool;
            #[call(resolve)]
            pub(super) fn resolve_cancellation(&mut self, result: Result<(), WaiterError>);
            #[call(clear)]
            pub(super) fn clear_cancellation(&mut self);
        }
    }

    pub(super) fn command_id(&self) -> CommandId {
        self.command_id
    }

    pub(super) fn owns_command(&self, command_id: CommandId) -> bool {
        self.command_id == command_id
    }

    /// Matches this command only after dispatch acceptance, not terminal success.
    pub(super) fn owns_accepted_command(&self, command_id: CommandId) -> bool {
        self.owns_command(command_id) && self.dispatch_accepted
    }

    pub(super) fn is_awaiting_dispatch_result(&self) -> bool {
        !self.dispatch_accepted
    }

    pub(super) fn request_cancellation(&mut self) -> CancellationDecision {
        self.cancellation.request(self.terminal_evidence.is_some())
    }

    /// Claims the one native cancellation dispatch after acceptance.
    /// A pre-dispatch cancellation remains latched until dispatch resolves.
    pub(super) fn try_claim_cancellation_dispatch(&mut self) -> bool {
        if self.is_awaiting_dispatch_result() {
            return false;
        }
        self.cancellation.try_claim_native_dispatch()
    }

    pub(super) fn buffer_dispatch_pending_event(
        &mut self,
        event: BleEvent,
    ) -> Result<(), ReconciliationFault> {
        if self.dispatch_pending_events.len() >= MAX_BUFFERED_DISPATCH_EVENTS_PER_COMMAND {
            return Err(ReconciliationFault::BufferOverflow);
        }
        self.dispatch_pending_events.push(event);
        Ok(())
    }

    /// Transfers evidence without acknowledging dispatch or choosing a replay policy.
    fn take_dispatch_pending_events(&mut self) -> Vec<BleEvent> {
        std::mem::take(&mut self.dispatch_pending_events)
    }

    /// Acknowledges acceptance and transfers early callbacks for ordered replay.
    /// Normal acceptance requests no cleanup or fault action.
    pub(super) fn resolve_accepted_dispatch(&mut self) -> DispatchResolution {
        if self.dispatch_accepted {
            // A duplicate acknowledgement cannot replay early callbacks again.
            return DispatchResolution::default();
        }
        self.dispatch_accepted = true;
        DispatchResolution {
            events_to_replay: self.take_dispatch_pending_events(),
            action: DispatchAction::NoAction,
        }
    }

    /// Drains early evidence without acknowledging acceptance. The reservation
    /// chooses replay, discard, or conflict for each event and owns resource teardown.
    /// Replay and Discard retain action_if_no_conflict; any Conflict overrides it
    /// with a fault. Classification continues so permitted events retain their order.
    /// The actor executes the resulting action.
    pub(super) fn resolve_rejected_dispatch(
        &mut self,
        action_if_no_conflict: DispatchAction,
        classify: impl Fn(&BleEvent) -> BufferedEventDisposition,
    ) -> DispatchResolution {
        let mut resolution = DispatchResolution {
            events_to_replay: Vec::new(),
            action: action_if_no_conflict,
        };
        for event in self.take_dispatch_pending_events() {
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
        if self.terminal_evidence.is_none() {
            self.terminal_evidence = Some(result.clone());
        }
    }

    delegate::delegate! {
        to self.terminal_evidence {
            #[call(is_some)]
            pub(super) fn has_terminal(&self) -> bool;
        }
    }

    pub(super) fn has_successful_terminal(&self) -> bool {
        self.terminal_evidence == Some(Ok(()))
    }

    pub(super) fn has_failed_terminal(&self) -> bool {
        self.terminal_evidence.as_ref().is_some_and(Result::is_err)
    }

    /// Accepted dispatch and a failed terminal do not prove native teardown.
    /// The reservation still owns retirement policy and any additional barriers.
    pub(super) fn has_accepted_terminal_failure(&self) -> bool {
        self.dispatch_accepted && self.has_failed_terminal()
    }

    /// Returns the recorded terminal only after dispatch acceptance. Success says
    /// nothing about domain evidence; the reservation must still supply that evidence.
    pub(super) fn terminal_result(&self) -> Option<Result<(), WaiterError>> {
        if self.is_awaiting_dispatch_result() {
            return None;
        }
        self.terminal_evidence
            .clone()
            .map(|result| result.map_err(WaiterError::Operation))
    }

    /// Command evidence is complete after accepted dispatch and a terminal failure,
    /// or a terminal success with all required correlated domain evidence.
    /// Keep the receipt owner alive if native evidence beats its cancellation
    /// control result. This neither depends on a live waiter nor proves native rollback.
    pub(super) fn is_complete(&self, domain_evidence_ready: bool) -> bool {
        if !self.dispatch_accepted || self.is_awaiting_cancellation_result() {
            return false;
        }
        match self.terminal_evidence.as_ref() {
            None => false,
            Some(Err(_)) => true,
            Some(Ok(())) => domain_evidence_ready,
        }
    }
}

impl Drop for CommandProgress {
    fn drop(&mut self) {
        // Actor-owned retirement/rejection/loss ends the command even if its
        // caller receiver is gone. Close/Fault still take precedence in control.
        self.completion_observer.confirm_completed();
    }
}
