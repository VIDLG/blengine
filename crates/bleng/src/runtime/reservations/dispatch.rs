use blehub::{BleEvent, BleEventError, CommandId};

use crate::waiter::WaiterError;
use tokio::sync::watch;

use super::super::shared::CompletionObserver;

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

pub(crate) type CancellationReceiver = watch::Receiver<Option<Result<(), WaiterError>>>;
type CancellationReceiptSender = watch::Sender<Option<Result<(), WaiterError>>>;

// watch publishes the receipt; protocol phase and acceptance stay actor-owned.
#[derive(Clone, Copy)]
enum CancellationPhase {
    Requested,
    // Native control dispatch has been claimed, not native teardown completed.
    Dispatched,
    // Local resolution can precede original dispatch rejection. Preserve whether
    // dispatch was claimed independently of the observable receipt's value.
    AcceptedUnclaimed,
    RejectedUnclaimed,
    AcceptedClaimed,
    RejectedClaimed,
}

struct CancellationRequest {
    phase: CancellationPhase,
    receipt: CancellationReceiptSender,
}

#[derive(Default)]
enum CancellationState {
    #[default]
    Idle,
    // One allocation per request cycle keeps every idle command compact.
    // Transitions reuse this block; clear/retirement releases it and its sender.
    Active(Box<CancellationRequest>),
}

impl CancellationState {
    fn phase(&self) -> Option<CancellationPhase> {
        match self {
            Self::Idle => None,
            Self::Active(request) => Some(request.phase),
        }
    }

    fn subscribe(&self) -> Option<CancellationReceiver> {
        match self {
            Self::Idle => None,
            Self::Active(request) => Some(request.receipt.subscribe()),
        }
    }
}

pub(crate) enum CancellationDecision {
    Accepted(CancellationReceiver),
    AlreadyRequested(CancellationReceiver),
    AlreadyCompleted,
    NotCancellable,
}

/// Publication only, owned by one connection or scan session. Pending control
/// delays the whole scope so passive teardown cannot overtake earlier success.
/// Owners supply their existing progress facts; this is not a routing registry.
#[derive(Default)]
pub(super) struct ScopedPublication {
    events: Vec<BleEvent>,
}

impl ScopedPublication {
    pub(super) fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    pub(super) fn allow(
        &mut self,
        event: &BleEvent,
        pending_control: bool,
        accepted_target: bool,
    ) -> Result<bool, ReconciliationFault> {
        if accepted_target && !matches!(event, BleEvent::CommandCompleted { .. }) {
            return Ok(false);
        }
        if pending_control || !self.events.is_empty() {
            // One aggregate hard cap per connection/scan, not per command.
            const MAX_DEFERRED_PUBLIC_EVENTS_PER_SCOPE: usize = 64;
            if self.events.len() >= MAX_DEFERRED_PUBLIC_EVENTS_PER_SCOPE {
                return Err(ReconciliationFault::BufferOverflow);
            }
            self.events.push(event.clone());
            return Ok(false);
        }
        Ok(true)
    }

    pub(super) fn resolve(
        &mut self,
        command_id: CommandId,
        accepted: bool,
        pending_control: bool,
    ) -> Vec<BleEvent> {
        if accepted {
            self.events.retain(|event| {
                event.command_id() != Some(command_id)
                    || matches!(event, BleEvent::CommandCompleted { .. })
            });
        }
        if pending_control {
            return Vec::new();
        }
        std::mem::take(&mut self.events)
    }
}

/// Common dispatch and terminal progress, not a generic BLE operation.
/// Domain evidence and operation-specific error precedence remain in each record.
pub(super) struct CommandProgress {
    command_id: CommandId,
    completion: CompletionObserver,
    dispatch_accepted: bool,
    buffered_events: Vec<BleEvent>,
    terminal_result: Option<Result<(), BleEventError>>,
    cancellation: CancellationState,
}

impl CommandProgress {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            completion: CompletionObserver::default(),
            dispatch_accepted: false,
            buffered_events: Vec::new(),
            terminal_result: None,
            cancellation: CancellationState::Idle,
        }
    }

    pub(super) fn completion_observer(&self) -> CompletionObserver {
        self.completion.clone()
    }

    pub(super) fn cancellation_accepted(&self) -> bool {
        matches!(
            self.cancellation.phase(),
            Some(CancellationPhase::AcceptedUnclaimed | CancellationPhase::AcceptedClaimed)
        )
    }

    pub(super) fn command_id(&self) -> CommandId {
        self.command_id
    }

    pub(super) fn owns_command(&self, command_id: CommandId) -> bool {
        self.command_id == command_id
    }

    pub(super) fn is_awaiting_dispatch_result(&self) -> bool {
        !self.dispatch_accepted
    }

    pub(super) fn cancellation_requested(&self) -> bool {
        !matches!(self.cancellation, CancellationState::Idle)
    }

    pub(super) fn is_awaiting_cancellation_result(&self) -> bool {
        matches!(
            self.cancellation.phase(),
            Some(CancellationPhase::Requested | CancellationPhase::Dispatched)
        )
    }

    pub(super) fn request_cancellation(&mut self) -> CancellationDecision {
        if let Some(receipt) = self.cancellation.subscribe() {
            return CancellationDecision::AlreadyRequested(receipt);
        }
        if self.terminal_result.is_some() {
            return CancellationDecision::AlreadyCompleted;
        }
        let (result_tx, result_rx) = watch::channel(None);
        self.cancellation = CancellationState::Active(Box::new(CancellationRequest {
            phase: CancellationPhase::Requested,
            receipt: result_tx,
        }));
        CancellationDecision::Accepted(result_rx)
    }

    pub(super) fn resolve_cancellation(&mut self, result: Result<(), WaiterError>) {
        let CancellationState::Active(request) = &mut self.cancellation else {
            return;
        };
        let claimed = matches!(
            request.phase,
            CancellationPhase::Dispatched
                | CancellationPhase::AcceptedClaimed
                | CancellationPhase::RejectedClaimed
        );
        request.phase = match (claimed, result.is_ok()) {
            (false, true) => CancellationPhase::AcceptedUnclaimed,
            (false, false) => CancellationPhase::RejectedUnclaimed,
            (true, true) => CancellationPhase::AcceptedClaimed,
            (true, false) => CancellationPhase::RejectedClaimed,
        };
        request.receipt.send_replace(Some(result));
    }

    pub(super) fn clear_cancellation(&mut self) {
        self.cancellation = CancellationState::Idle;
    }

    /// Claims the one native cancellation dispatch after acceptance.
    /// A pre-dispatch cancellation remains latched until dispatch resolves.
    pub(super) fn take_cancellation_dispatch(&mut self) -> bool {
        if self.is_awaiting_dispatch_result() {
            return false;
        }
        let CancellationState::Active(request) = &mut self.cancellation else {
            return false;
        };
        request.phase = match request.phase {
            CancellationPhase::Requested => CancellationPhase::Dispatched,
            CancellationPhase::AcceptedUnclaimed => CancellationPhase::AcceptedClaimed,
            CancellationPhase::RejectedUnclaimed => CancellationPhase::RejectedClaimed,
            _ => return false,
        };
        true
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
        if self.dispatch_accepted {
            // A duplicate acknowledgement cannot replay early callbacks again.
            return DispatchResolution::default();
        }
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
    /// Keep the receipt owner alive if native evidence beats its cancellation
    /// control result. This neither depends on a live waiter nor proves native rollback.
    pub(super) fn is_complete(&self, domain_evidence_ready: bool) -> bool {
        if !self.dispatch_accepted || self.is_awaiting_cancellation_result() {
            return false;
        }
        match self.terminal_result.as_ref() {
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
        self.completion.confirm_completed();
    }
}
