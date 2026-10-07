//! Foreign backend assembly and admission handoff for one BLeng runtime.

use std::sync::Arc;

use blehub::{BackendAssembly, BleBackend, BleEventSink, BleHubError, attach_backend};
use kameo::prelude::ActorRef;

use crate::cleanup::AssemblyAdmissionError;

use super::{BlengActor, RuntimePhase, RuntimeStatus, ingress::event_sink};

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

#[derive(Clone)]
pub(crate) struct AssemblyPort {
    actor: ActorRef<BlengActor>,
    pub(crate) status: Arc<RuntimeStatus>,
}

impl AssemblyPort {
    pub(super) fn new(actor: ActorRef<BlengActor>, status: Arc<RuntimeStatus>) -> Self {
        Self { actor, status }
    }

    /// Compose the backend assembly with the runtime ingress bridge.
    ///
    /// The factory is called during assembly, but the bridge implementation
    /// remains in `ingress.rs` because callback transport is independent of
    /// backend admission and cleanup ownership.
    pub(super) fn event_sink(&self) -> Arc<dyn BleEventSink> {
        event_sink(self.actor.clone(), Arc::clone(&self.status))
    }

    // The host must run this synchronous foreign boundary off the actor and
    // UI threads. No ticket escapes: admission spans attach, claim, and cleanup.
    pub(crate) fn assemble_backend(
        &self,
        backend: Arc<dyn BleBackend>,
    ) -> Result<(), AssemblyError> {
        self.status.cleanup.with_admission(
            || self.status.phase() == RuntimePhase::Open,
            |admission| {
                let assembly = BackendAssembly::new(self.event_sink());
                attach_backend(assembly.ticket(), backend).map_err(AssemblyError::Backend)?;
                let hub = assembly.finish().map_err(AssemblyError::Backend)?;
                admission
                    .commit(hub, || self.status.phase() == RuntimePhase::Open)
                    .map_err(AssemblyError::from)
            },
        )
    }
}
