//! One companion-device association request, correlated by its own command and terminal.
//!
//! Association is a top-level host-platform action: it owns no connection, scan session or GATT
//! resource, and the platform — not this engine — decides which peer the user picked in the system
//! chooser. Its only evidence is the platform's `CommandCompleted`, so readiness is accepted
//! dispatch plus that terminal. The resulting association is deliberately not part of this
//! reservation: callers read `DeviceQueryKind::Companion` afterwards for the association list. The
//! shared lifecycle lives in [`TopLevelCommand`].

use blehub::{BleEvent, BleEventError, CommandDispatchError, CommandId};

use super::super::shared::CompletionObserver;
use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::top_level::{TopLevelCommand, top_level_owner};
use super::{CancellationDecision, DispatchResolution, ReconciliationFault};

#[derive(Default)]
pub(in crate::runtime) struct CompanionAssociationState {
    policy: TopLevelCommand,
}

impl CompanionAssociationState {
    pub(in crate::runtime) fn require_available(&self) -> Result<(), WaiterError> {
        self.policy.require_available()
    }

    pub(in crate::runtime) fn reserve(&mut self, command_id: CommandId) {
        self.policy.reserve(command_id, "companion association");
    }

    pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.policy.completion_observer(command_id)
    }

    pub(in crate::runtime) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        self.policy.gate_event(event, top_level_owner)
    }

    pub(in crate::runtime) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        self.policy.resolve_dispatch(command_id, result)
    }

    /// Returns whether this reservation owns the command, not whether it succeeded.
    pub(in crate::runtime) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        self.policy.record_terminal(command_id, result)
    }

    pub(in crate::runtime) fn request_cancellation(
        &self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        self.policy.request_cancellation(command_id)
    }

    /// The answer is "the platform reported an association"; the association itself is data the
    /// caller reads back through an inventory query.
    pub(in crate::runtime) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        self.policy
            .progress(command_id)?
            .terminal_result()
            .map(|result| result.map(|()| WaiterSuccess::CompanionAssociationRequested))
    }

    pub(in crate::runtime) fn retire_completed(&mut self) -> Option<CommandId> {
        self.policy.retire_completed(false)
    }
}
