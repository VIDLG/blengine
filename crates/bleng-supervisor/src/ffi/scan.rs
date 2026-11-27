//! Native event-reader handoff; this is not a JS scan or listener API.

use std::sync::Arc;

use blehub::BleEvent;

use super::types::ForeignOwnerContext;
use crate::{ScanEvent, ScanEventEnvelope, SessionObservationProjection};

/// An observation carrying the context of the owner that produced it.
/// Downstream queues must compare the full context before applying this event.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignScanEventEnvelope {
    pub source_context: ForeignOwnerContext,
    pub event: ScanEvent,
}

impl From<ScanEventEnvelope> for ForeignScanEventEnvelope {
    fn from(envelope: ScanEventEnvelope) -> Self {
        let (context, event) = envelope.into_parts();
        Self {
            source_context: ForeignOwnerContext::from(&context),
            event,
        }
    }
}

/// Rust-created capability bound to one owner, retained only by its native reader.
/// Native callers cannot construct, rebind or reactivate this endpoint.
#[derive(uniffi::Object)]
pub struct SessionEventProjection {
    inner: Arc<SessionObservationProjection>,
}

impl SessionEventProjection {
    pub(super) fn new(inner: Arc<SessionObservationProjection>) -> Arc<Self> {
        Arc::new(Self { inner })
    }
}

#[uniffi::export]
impl SessionEventProjection {
    /// Projects scan events synchronously without consuming another event stream.
    /// None means non-scan or revoked, not native stop or successful cleanup.
    /// Retirement does not retract an envelope already returned before revocation.
    pub fn project(&self, event: BleEvent) -> Option<ForeignScanEventEnvelope> {
        self.inner
            .project(event)
            .map(ForeignScanEventEnvelope::from)
    }
}
