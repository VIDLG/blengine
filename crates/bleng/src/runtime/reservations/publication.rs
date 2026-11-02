use blehub::{BleEvent, CommandId};

use super::command_progress::ReconciliationFault;

/// Maximum number of public events retained by one connection or scan scope
/// while cancellation ordering is unresolved.
pub(super) const MAX_DEFERRED_PUBLIC_EVENTS_PER_SCOPE: usize = 64;

/// Publication ordering owned by one connection or scan session. Pending
/// control delays the whole scope so passive teardown cannot overtake earlier
/// success. Owners supply their existing progress facts; this is not a routing
/// registry.
#[derive(Default)]
pub(crate) struct ScopedPublication {
    // This is a scope-wide FIFO, not a per-command queue. A passive event may
    // have no command ID but still must not overtake an unresolved cancellation
    // in the same connection or scan session.
    events: Vec<BleEvent>,
}

/// Independent facts selected by the owning reservation for one publication
/// decision. An accepted target cancellation and another pending cancellation
/// can coexist, so these facts must remain separate.
#[derive(Clone, Copy)]
pub(crate) struct PublicationGate {
    pub(super) cancellation_accepted_for_event: bool,
    pub(super) cancellation_pending: bool,
}

/// Facts needed when one cancellation control result releases a publication
/// scope. The two fields describe different things: whether this command was
/// accepted, and whether another cancellation still blocks the scope.
#[derive(Clone, Copy)]
pub(crate) struct PublicationRelease {
    pub(super) cancellation_accepted: bool,
    pub(super) another_cancellation_pending: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PublicationDecision {
    /// Publish the event immediately.
    Publish,
    /// Retain the event and publish it after the pending control is resolved.
    Defer,
    /// Deliberately suppress a non-terminal event belonging to an accepted
    /// cancellation request.
    Suppress,
}

impl ScopedPublication {
    pub(super) fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// Chooses the public disposition for one event. Internal evidence is
    /// recorded separately; this method only controls app-facing publication.
    pub(super) fn decide(
        &mut self,
        event: &BleEvent,
        gate: PublicationGate,
    ) -> Result<PublicationDecision, ReconciliationFault> {
        // Accepted cancellation suppresses ordinary events for that command,
        // but never its terminal. The terminal remains the authoritative end
        // of the operation even when the caller stopped waiting for it.
        if gate.cancellation_accepted_for_event
            && !matches!(event, BleEvent::CommandCompleted { .. })
        {
            return Ok(PublicationDecision::Suppress);
        }
        // A pending control blocks the whole scope. Once the FIFO is non-empty,
        // keep deferring even if this particular event is not command-correlated;
        // otherwise a later event could overtake an earlier buffered event.
        if gate.cancellation_pending || !self.events.is_empty() {
            if self.events.len() >= MAX_DEFERRED_PUBLIC_EVENTS_PER_SCOPE {
                return Err(ReconciliationFault::BufferOverflow);
            }
            self.events.push(event.clone());
            return Ok(PublicationDecision::Defer);
        }
        Ok(PublicationDecision::Publish)
    }

    /// Applies one cancellation result and releases public events when the
    /// entire scope is no longer blocked. The current result may be complete
    /// while another command in the same scope still has pending control.
    pub(super) fn resolve(
        &mut self,
        command_id: CommandId,
        release: PublicationRelease,
    ) -> Vec<BleEvent> {
        if release.cancellation_accepted {
            // Remove only ordinary events for the accepted cancellation target.
            // Events from sibling commands and the target's terminal remain.
            self.events.retain(|event| {
                event.command_id() != Some(command_id)
                    || matches!(event, BleEvent::CommandCompleted { .. })
            });
        }
        if release.another_cancellation_pending {
            // Keep the FIFO intact for the next resolution; returning an empty
            // vector here does not mean the buffered events were discarded.
            return Vec::new();
        }
        // `take` both preserves the original order and reopens immediate
        // publication for the next event in this scope.
        std::mem::take(&mut self.events)
    }
}
