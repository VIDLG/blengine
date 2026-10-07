use blehub::{BleEvent, BleEventError, CommandId};

use crate::waiter::{WaiterError, WaiterSuccess};

#[derive(Debug)]
pub enum ReconcileFault {
    BufferOverflow,
    UnexpectedNotificationMode,
    ContradictoryDispatch,
}

#[derive(Default)]
pub enum DispatchEffect {
    #[default]
    None,
    Cleanup,
    Fault(ReconcileFault),
}

#[derive(Default)]
pub struct DispatchResolution {
    pub replay: Vec<BleEvent>,
    pub effect: DispatchEffect,
}

/// Common dispatch and terminal progress, not a generic BLE operation.
/// Domain evidence and operation-specific error precedence remain in each record.
pub(super) struct CommandProgress {
    pub(super) command_id: CommandId,
    dispatch_accepted: bool,
    buffered: Vec<BleEvent>,
    terminal: Option<Result<(), BleEventError>>,
}

impl CommandProgress {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            dispatch_accepted: false,
            buffered: Vec::new(),
            terminal: None,
        }
    }

    pub(super) fn is_awaiting_dispatch_ack(&self) -> bool {
        !self.dispatch_accepted
    }

    fn accept(&mut self) -> Vec<BleEvent> {
        self.dispatch_accepted = true;
        self.take_buffered_events()
    }

    pub(super) fn buffer(&mut self, event: BleEvent) -> Result<(), ReconcileFault> {
        const PREDISPATCH_EVENTS: usize = 64;
        if self.buffered.len() >= PREDISPATCH_EVENTS {
            return Err(ReconcileFault::BufferOverflow);
        }
        self.buffered.push(event);
        Ok(())
    }

    pub(super) fn has_buffered_events(&self) -> bool {
        !self.buffered.is_empty()
    }

    /// Transfers evidence without acknowledging dispatch or choosing a replay policy.
    pub(super) fn take_buffered_events(&mut self) -> Vec<BleEvent> {
        std::mem::take(&mut self.buffered)
    }

    pub(super) fn accepted_resolution(&mut self) -> DispatchResolution {
        DispatchResolution {
            replay: self.accept(),
            ..Default::default()
        }
    }

    /// The first terminal is authoritative; duplicate callbacks cannot replace it.
    pub(super) fn record_terminal(&mut self, result: &Result<(), BleEventError>) {
        if self.terminal.is_none() {
            self.terminal = Some(result.clone());
        }
    }

    pub(super) fn has_terminal(&self) -> bool {
        self.terminal.is_some()
    }

    pub(super) fn terminal_succeeded(&self) -> bool {
        self.terminal == Some(Ok(()))
    }

    pub(super) fn terminal_failed(&self) -> bool {
        self.terminal.as_ref().is_some_and(Result::is_err)
    }

    /// Terminal failure wins over success evidence, but neither is deliverable
    /// until native dispatch has been acknowledged. The success closure is lazy.
    pub(super) fn waiter_result(
        &self,
        success: impl FnOnce() -> Option<WaiterSuccess>,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.is_awaiting_dispatch_ack() {
            return None;
        }
        match self.terminal.as_ref()? {
            Err(error) => Some(Err(WaiterError::Operation(error.clone()))),
            Ok(()) => success().map(Ok),
        }
    }

    /// Completion is independent of whether a caller still has a waiter.
    pub(super) fn is_complete(&self, domain_ready: bool) -> bool {
        self.dispatch_accepted
            && self
                .terminal
                .as_ref()
                .is_some_and(|result| result.is_err() || domain_ready)
    }
}
