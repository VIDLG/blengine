//! Native/backend event ingress into the runtime actor.

use std::sync::Arc;

use blehub::{BleEvent, BleEventSink, EventSinkError};
use kameo::prelude::ActorRef;

use super::{BlengActor, RuntimePhase, RuntimeStatus};

struct EventBridge {
    actor: ActorRef<BlengActor>,
    status: Arc<RuntimeStatus>,
}

/// Actor-mailbox message for one event arriving from the native/backend ingress.
///
/// This newtype keeps the external ingress path distinct from the `BleEvent`
/// domain value itself. It prevents accidental coupling if the actor later
/// receives `BleEvent` values through another internal path and leaves room for
/// ingress metadata without changing the shared BleHub event contract.
pub(crate) struct IngressEvent(pub(crate) BleEvent);

pub(super) fn event_sink(
    actor: ActorRef<BlengActor>,
    status: Arc<RuntimeStatus>,
) -> Arc<dyn BleEventSink> {
    Arc::new(EventBridge { actor, status })
}

impl BleEventSink for EventBridge {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        if self.status.phase() != RuntimePhase::Open {
            return Err(EventSinkError::Rejected {
                detail: "BLeng ingress is closed or faulted".to_owned(),
            });
        }
        self.actor
            .tell(IngressEvent(event))
            .try_send()
            .map_err(|_| {
                self.status.fault();
                EventSinkError::Rejected {
                    detail: "BLeng actor mailbox unavailable".to_owned(),
                }
            })
    }
}
