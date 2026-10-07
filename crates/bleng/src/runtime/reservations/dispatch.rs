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

/// Common dispatch-acknowledgement boundary, not a generic BLE operation.
/// Domain evidence and operation-specific error precedence remain in each record.
pub(super) struct DispatchReservation {
    pub(super) command_id: CommandId,
    dispatch_accepted: bool,
    pub(super) buffered: Vec<BleEvent>,
}

impl DispatchReservation {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            command_id,
            dispatch_accepted: false,
            buffered: Vec::new(),
        }
    }

    pub(super) fn is_awaiting_dispatch_ack(&self) -> bool {
        !self.dispatch_accepted
    }

    fn accept(&mut self) -> Vec<BleEvent> {
        self.dispatch_accepted = true;
        std::mem::take(&mut self.buffered)
    }

    pub(super) fn buffer(&mut self, event: BleEvent) -> Result<(), ReconcileFault> {
        const PREDISPATCH_EVENTS: usize = 64;
        if self.buffered.len() >= PREDISPATCH_EVENTS {
            return Err(ReconcileFault::BufferOverflow);
        }
        self.buffered.push(event);
        Ok(())
    }

    pub(super) fn accepted_resolution(&mut self) -> DispatchResolution {
        DispatchResolution {
            replay: self.accept(),
            ..Default::default()
        }
    }

    /// Terminal failure wins over success evidence, but neither is deliverable
    /// until native dispatch has been acknowledged. The success closure is lazy.
    pub(super) fn waiter_result(
        &self,
        terminal: &Option<Result<(), BleEventError>>,
        success: impl FnOnce() -> Option<WaiterSuccess>,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        if self.is_awaiting_dispatch_ack() {
            return None;
        }
        match terminal.as_ref()? {
            Err(error) => Some(Err(WaiterError::Operation(error.clone()))),
            Ok(()) => success().map(Ok),
        }
    }

    /// Completion is independent of whether a caller still has a waiter.
    pub(super) fn is_complete(
        &self,
        terminal: &Option<Result<(), BleEventError>>,
        domain_ready: bool,
    ) -> bool {
        self.dispatch_accepted
            && terminal
                .as_ref()
                .is_some_and(|result| result.is_err() || domain_ready)
    }
}

/// The first terminal is authoritative; duplicate callbacks cannot replace it.
pub(super) fn record_terminal(
    terminal: &mut Option<Result<(), BleEventError>>,
    result: &Result<(), BleEventError>,
) {
    if terminal.is_none() {
        *terminal = Some(result.clone());
    }
}
