use blehub::{AdapterSnapshot, BleEvent, BleEventError, CommandDispatchError, CommandId};

use crate::waiter::{WaiterError, WaiterSuccess};

use super::dispatch::{CommandProgress, DispatchEffect, DispatchResolution, ReconcileFault};

/// Readiness is distinct from runtime lifecycle and adapter power.
/// A Pending record survives waiter retirement so late callbacks remain correlated.
#[derive(Default)]
pub enum InitializationState {
    #[default]
    NotStarted,
    Pending(InitReservation),
    Ready,
}

impl InitializationState {
    pub fn buffer_before_dispatch(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconcileFault> {
        if let Some(init) = self.pending_mut()
            && init.progress.is_awaiting_dispatch_ack()
            && init.matches_event(&event)
        {
            init.progress.buffer(event)?;
            return Ok(None);
        }
        Ok(Some(event))
    }

    pub fn record_event(&mut self, event: &BleEvent) {
        if let Some(init) = self.pending_mut()
            && init.matches_event(event)
            && let BleEvent::AdapterSnapshotChanged { snapshot, .. } = event
            && init.snapshot.is_none()
        {
            init.snapshot = Some(snapshot.clone());
        }
    }

    pub fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let init = self
            .pending_mut()
            .filter(|init| init.progress.command_id == command_id)?;
        Some(if result.is_err() {
            DispatchResolution {
                effect: if init.progress.has_buffered_events() {
                    DispatchEffect::Fault(ReconcileFault::ContradictoryDispatch)
                } else {
                    DispatchEffect::Cleanup
                },
                ..Default::default()
            }
        } else {
            init.progress.accepted_resolution()
        })
    }

    pub fn reserve_initialize(&mut self, command_id: CommandId) {
        *self = Self::Pending(InitReservation::new(command_id));
    }

    fn has_started(&self) -> bool {
        !matches!(self, Self::NotStarted)
    }

    pub fn require_not_started(&self) -> Result<(), WaiterError> {
        if self.has_started() {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    pub fn require_initialized(&self) -> Result<(), WaiterError> {
        if matches!(self, Self::Ready) {
            Ok(())
        } else {
            Err(WaiterError::NotInitialized)
        }
    }

    fn pending(&self) -> Option<&InitReservation> {
        match self {
            Self::Pending(reservation) => Some(reservation),
            _ => None,
        }
    }

    fn pending_mut(&mut self) -> Option<&mut InitReservation> {
        match self {
            Self::Pending(reservation) => Some(reservation),
            _ => None,
        }
    }

    pub fn has_failed(&self) -> bool {
        self.pending().is_some_and(|init| {
            !init.progress.is_awaiting_dispatch_ack() && init.progress.terminal_failed()
        })
    }

    /// Returns whether initialization owns the command, not whether it succeeded.
    /// A matching duplicate still returns true; the first terminal is preserved.
    pub fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(init) = self
            .pending_mut()
            .filter(|init| init.progress.command_id == command_id)
        else {
            return false;
        };
        init.progress.record_terminal(result);
        true
    }

    pub fn waiter_result(
        &self,
        command_id: CommandId,
    ) -> Option<Result<WaiterSuccess, WaiterError>> {
        let init = self
            .pending()
            .filter(|init| init.progress.command_id == command_id)?;
        init.progress
            .waiter_result(|| init.snapshot.clone().map(WaiterSuccess::Initialized))
    }

    /// Called only after the actor has settled the result from this evidence.
    pub fn complete_if_ready(&mut self) {
        if self.pending().is_some_and(|init| {
            init.progress.is_complete(init.snapshot.is_some()) && init.progress.terminal_succeeded()
        }) {
            *self = Self::Ready;
        }
    }
}

/// Dispatch, correlated snapshot, and terminal may arrive in either order.
/// The enum payload matches its owner's visibility, but exposes no record internals.
pub struct InitReservation {
    progress: CommandProgress,
    snapshot: Option<AdapterSnapshot>,
}

impl InitReservation {
    fn matches_event(&self, event: &BleEvent) -> bool {
        match event {
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(command_id),
                ..
            }
            | BleEvent::CommandCompleted { command_id, .. } => {
                *command_id == self.progress.command_id
            }
            _ => false,
        }
    }

    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            snapshot: None,
        }
    }
}
