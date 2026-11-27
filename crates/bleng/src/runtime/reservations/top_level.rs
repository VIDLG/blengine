//! One top-level command: a request that owns no connection, scan session or GATT resource.
//!
//! Adapter power, device inventory and companion association share exactly one lifecycle — admit a
//! single slot, withhold evidence until dispatch is reconciled, record one terminal, retire once —
//! and differ only in the answer their waiter receives. Writing that lifecycle three times invited
//! three chances to drift, so it lives here once and the three states are thin shells over it.
//!
//! None of these commands is cancellable after dispatch: the platform call is not withdrawable, so
//! [`CancellationDecision::NotCancellable`] is the only answer this slot can give. A future
//! top-level command that *can* be withdrawn must not reuse this type.

use blehub::{BleEvent, BleEventError, CommandDispatchError, CommandId};

use super::super::shared::CompletionObserver;
use crate::waiter::WaiterError;

use super::{
    BufferedEventDisposition, CancellationDecision, CommandProgress, DispatchAction,
    DispatchResolution, ReconciliationFault,
};

pub(super) struct TopLevelCommand<E = ()> {
    reserved: Option<TopLevelReservation<E>>,
}

/// The only event that identifies a top-level command: its own `CommandCompleted`.
///
/// A shell whose command also carries a payload widens this; a codec that does not carry this
/// command's ID never reaches the slot.
pub(super) fn top_level_owner(event: &BleEvent) -> Option<CommandId> {
    match event {
        BleEvent::CommandCompleted { command_id, .. } => Some(*command_id),
        _ => None,
    }
}

struct TopLevelReservation<E> {
    progress: CommandProgress,
    evidence: Option<E>,
}

// Hand-written so the slot needs no `E: Default` bound.
impl<E> Default for TopLevelCommand<E> {
    fn default() -> Self {
        Self { reserved: None }
    }
}

impl<E> TopLevelCommand<E> {
    pub(super) fn require_available(&self) -> Result<(), WaiterError> {
        if self.reserved.is_some() {
            return Err(WaiterError::OperationBusy);
        }
        Ok(())
    }

    /// Installs the slot after admission and caller submission succeed.
    ///
    /// `command` names the caller-facing operation, so a violated admission precondition says which
    /// one was already reserved.
    pub(super) fn reserve(&mut self, command_id: CommandId, command: &str) {
        assert!(
            self.require_available().is_ok(),
            "validated {command} is already reserved"
        );
        self.reserved = Some(TopLevelReservation {
            progress: CommandProgress::new(command_id),
            evidence: None,
        });
    }

    pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.reserved_for_cmd(command_id)
            .map(|reserved| reserved.progress.completion_observer())
    }

    /// Withholds this command's evidence and terminal until dispatch is reconciled, so a fast
    /// platform callback cannot settle a waiter that dispatch rejection may still invalidate.
    ///
    /// `owner` decides which events carry this command's identity; the slot never guesses.
    pub(super) fn gate_event(
        &mut self,
        event: BleEvent,
        owner: impl Fn(&BleEvent) -> Option<CommandId>,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        let Some(command_id) = owner(&event) else {
            return Ok(Some(event));
        };
        let Some(reserved) = self.reserved_for_cmd_mut(command_id) else {
            return Ok(Some(event));
        };
        if reserved.progress.is_awaiting_dispatch_result() {
            reserved.progress.buffer_dispatch_pending_event(event)?;
            return Ok(None);
        }
        Ok(Some(event))
    }

    /// A rejected dispatch releases the slot immediately; any early terminal for it is discarded
    /// because rejection is the authoritative outcome.
    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        let resolution = {
            let progress = &mut self.reserved_for_cmd_mut(command_id)?.progress;
            if result.is_ok() {
                progress.resolve_accepted_dispatch()
            } else {
                progress.resolve_rejected_dispatch(DispatchAction::NoAction, |_| {
                    BufferedEventDisposition::Discard
                })
            }
        };
        if result.is_err() {
            self.reserved = None;
        }
        Some(resolution)
    }

    /// Returns whether this reservation owns the command, not whether it succeeded.
    pub(super) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        let Some(reserved) = self.reserved_for_cmd_mut(command_id) else {
            return false;
        };
        reserved.progress.record_terminal(result);
        true
    }

    /// The platform call cannot be withdrawn, so this only records the refusal.
    pub(super) fn request_cancellation(
        &self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        self.reserved_for_cmd(command_id)
            .map(|_| CancellationDecision::NotCancellable)
    }

    /// This command's dispatch/terminal progress, for the shell's waiter projection.
    pub(super) fn progress(&self, command_id: CommandId) -> Option<&CommandProgress> {
        Some(&self.reserved_for_cmd(command_id)?.progress)
    }

    /// The command's recorded answer, if it carries one.
    pub(super) fn evidence(&self, command_id: CommandId) -> Option<&E> {
        self.reserved_for_cmd(command_id)?.evidence.as_ref()
    }

    /// Records this command's answer; the first answer wins, exactly like the first terminal.
    pub(super) fn record_evidence(&mut self, command_id: CommandId, evidence: E) {
        if let Some(reserved) = self.reserved_for_cmd_mut(command_id)
            && reserved.evidence.is_none()
        {
            reserved.evidence = Some(evidence);
        }
    }

    /// Releases the slot once accepted dispatch and a terminal are recorded.
    ///
    /// `requires_evidence` is the shell's statement that a successful terminal is not a result by
    /// itself (an inventory query needs its list); a failed terminal retires regardless.
    pub(super) fn retire_completed(&mut self, requires_evidence: bool) -> Option<CommandId> {
        let reserved = self.reserved.as_ref()?;
        let evidence_ready = !requires_evidence || reserved.evidence.is_some();
        if !reserved.progress.is_complete(evidence_ready) {
            return None;
        }
        let command_id = reserved.progress.command_id();
        self.reserved = None;
        Some(command_id)
    }

    fn reserved_for_cmd(&self, command_id: CommandId) -> Option<&TopLevelReservation<E>> {
        self.reserved
            .as_ref()
            .filter(|reserved| reserved.progress.owns_command(command_id))
    }

    fn reserved_for_cmd_mut(
        &mut self,
        command_id: CommandId,
    ) -> Option<&mut TopLevelReservation<E>> {
        self.reserved
            .as_mut()
            .filter(|reserved| reserved.progress.owns_command(command_id))
    }
}
