//! Backend assembly and non-blocking event ingress for one BLeng runtime.

use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::Arc,
};

use blehub::{
    BackendAssembly, BleBackend, BleEvent, BleEventSink, BleHubError, EventSinkError,
    attach_backend,
};
use kameo::prelude::ActorRef;

use crate::cleanup::{AssemblyAdmissionError, CleanupError};

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
            // Keep admission outside the unwind boundary. The blocking worker
            // owns failure accounting even after its async caller is dropped.
            let assembled = catch_unwind(AssertUnwindSafe(|| {
                let sink: Arc<dyn BleEventSink> = self.clone();
                let assembly = BackendAssembly::new(sink);
                attach_backend(assembly.ticket(), backend)?;
                assembly.finish()
            }));
            match assembled {
                Ok(Ok(hub)) => admission
                    .commit(hub, || self.lifecycle.is_open())
                    .map_err(AssemblyError::from),
                Ok(Err(error)) => {
                    if matches!(error, BleHubError::BackendCleanup { .. }) {
                        admission.record_failure(CleanupError::Backend(error.clone()));
                        self.lifecycle.fault();
                    }
                    Err(AssemblyError::Backend(error))
                }
                Err(_) => {
                    admission.record_failure(CleanupError::WorkerStopped);
                    self.lifecycle.fault();
                    Err(AssemblyError::WorkerStopped)
                }
            }
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
