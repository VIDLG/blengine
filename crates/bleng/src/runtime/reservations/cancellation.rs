use crate::waiter::WaiterError;
use tokio::sync::watch;

pub(crate) type CancellationReceiver = watch::Receiver<Option<Result<(), WaiterError>>>;
type CancellationReceiptSender = watch::Sender<Option<Result<(), WaiterError>>>;

// The cancellation request has two independent dimensions:
//
// 1. `control_result`: whether the native cancellation control has produced a
//    local acceptance/rejection result;
// 2. `native_dispatch_claimed`: whether this request owns the one native cancel
//    dispatch opportunity.
//
// Every combination is valid. In particular, the result may arrive before the
// original command dispatch is rejected, so these facts must not be collapsed
// into one linear phase enum.
pub(super) struct CancellationRequest {
    control_result: Option<Result<(), WaiterError>>,
    native_dispatch_claimed: bool,
    receipt: CancellationReceiptSender,
}

#[derive(Default)]
pub(super) enum CancellationState {
    #[default]
    Idle,
    // One allocation per request cycle keeps every idle command compact.
    // Transitions reuse this block; clear/retirement releases it and its sender.
    Active(Box<CancellationRequest>),
}

pub(crate) enum CancellationDecision {
    Accepted(CancellationReceiver),
    AlreadyRequested(CancellationReceiver),
    AlreadyCompleted,
    NotCancellable,
}

impl CancellationState {
    pub(super) fn is_active(&self) -> bool {
        matches!(self, Self::Active(_))
    }

    pub(super) fn is_accepted(&self) -> bool {
        matches!(self, Self::Active(request) if request.control_result == Some(Ok(())))
    }

    pub(super) fn is_awaiting_result(&self) -> bool {
        matches!(self, Self::Active(request) if request.control_result.is_none())
    }

    pub(super) fn subscribe(&self) -> Option<CancellationReceiver> {
        match self {
            Self::Idle => None,
            Self::Active(request) => Some(request.receipt.subscribe()),
        }
    }

    pub(super) fn resolve(&mut self, result: Result<(), WaiterError>) {
        let Self::Active(request) = self else {
            return;
        };
        request.control_result = Some(result.clone());
        request.receipt.send_replace(Some(result));
    }

    pub(super) fn clear(&mut self) {
        *self = Self::Idle;
    }

    pub(super) fn try_claim_native_dispatch(&mut self) -> bool {
        let Self::Active(request) = self else {
            return false;
        };
        if request.native_dispatch_claimed {
            return false;
        }
        request.native_dispatch_claimed = true;
        true
    }

    pub(super) fn request(&mut self, terminal_recorded: bool) -> CancellationDecision {
        if let Some(receipt) = self.subscribe() {
            return CancellationDecision::AlreadyRequested(receipt);
        }
        if terminal_recorded {
            return CancellationDecision::AlreadyCompleted;
        }
        let (result_tx, result_rx) = watch::channel(None);
        *self = Self::Active(Box::new(CancellationRequest {
            control_result: None,
            native_dispatch_claimed: false,
            receipt: result_tx,
        }));
        CancellationDecision::Accepted(result_rx)
    }
}
