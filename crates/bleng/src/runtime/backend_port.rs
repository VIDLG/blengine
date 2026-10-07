//! Backend assembly and non-blocking event ingress for one BLeng runtime.

use std::sync::Arc;

use blehub::{
    BackendAssembly, BleBackend, BleEvent, BleEventSink, BleHubError, EventSinkError,
    attach_backend,
};
use kameo::prelude::ActorRef;

use crate::cleanup::AssemblyAdmissionError;

use super::{BlengActor, RuntimeLifecycle, messages::IngressEvent};

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum AssemblyError {
    Admission(AssemblyAdmissionError),
    Backend(BleHubError),
    WorkerStopped,
}

impl From<AssemblyAdmissionError> for AssemblyError {
    fn from(error: AssemblyAdmissionError) -> Self {
        Self::Admission(error)
    }
}

/// One shared backend boundary for assembly and callback delivery.
/// The Hub retains this same allocation as a BleEventSink trait object; it
/// receives no assembly interface and the port never retains the runtime owner.
pub(crate) struct BackendPort {
    actor: ActorRef<BlengActor>,
    lifecycle: Arc<RuntimeLifecycle>,
}

impl BackendPort {
    pub(super) fn new(actor: ActorRef<BlengActor>, lifecycle: Arc<RuntimeLifecycle>) -> Self {
        Self { actor, lifecycle }
    }

    // The host must run this synchronous foreign boundary off the actor and
    // UI threads. No ticket escapes: admission spans attach, claim, and cleanup.
    pub(crate) fn assemble_backend(
        self: &Arc<Self>,
        backend: Arc<dyn BleBackend>,
    ) -> Result<(), AssemblyError> {
        self.lifecycle.with_admission(|admission| {
            let sink: Arc<dyn BleEventSink> = self.clone();
            let assembly = BackendAssembly::new(sink);
            attach_backend(assembly.ticket(), backend).map_err(AssemblyError::Backend)?;
            let hub = assembly.finish().map_err(AssemblyError::Backend)?;
            admission
                .commit(hub, || self.lifecycle.is_open())
                .map_err(AssemblyError::from)
        })
    }
}

impl BleEventSink for BackendPort {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        if !self.lifecycle.is_open() {
            return Err(EventSinkError::Rejected {
                detail: "BLeng ingress is closed or faulted".to_owned(),
            });
        }
        self.actor
            .tell(IngressEvent(event))
            .try_send()
            .map_err(|_| {
                self.lifecycle.fault();
                EventSinkError::Rejected {
                    detail: "BLeng actor mailbox unavailable".to_owned(),
                }
            })
    }
}
