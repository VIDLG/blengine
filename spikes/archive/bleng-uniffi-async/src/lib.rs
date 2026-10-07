//! Isolated async UniFFI mechanism probe; not a BLeng facade or production image.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use tokio::sync::{Notify, oneshot};

uniffi::setup_scaffolding!();

// The real BLeng foreign owner is enabled only in this isolated image.
// Neither it nor this toy component is linked by the production image.
pub use blehub::*;
pub use bleng::BlengForeignProbe;

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum ProbeError {
    #[error("probe is closed")]
    Closed,
    #[error("probe worker stopped")]
    WorkerStopped,
}

#[derive(uniffi::Object)]
pub struct AsyncProbe {
    runtime: tokio::runtime::Runtime,
    closed: AtomicBool,
    waiting: AtomicBool,
    waiting_dropped: AtomicBool,
    release: Notify,
}

struct ObserveFutureDrop<'a>(&'a AtomicBool);

impl Drop for ObserveFutureDrop<'_> {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

#[uniffi::export]
impl AsyncProbe {
    #[uniffi::constructor]
    pub fn new() -> Result<Arc<Self>, ProbeError> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_time()
            .build()
            .map_err(|_| ProbeError::WorkerStopped)?;
        Ok(Arc::new(Self {
            runtime,
            closed: AtomicBool::new(false),
            waiting: AtomicBool::new(false),
            waiting_dropped: AtomicBool::new(false),
            release: Notify::new(),
        }))
    }

    pub async fn round_trip(&self, value: u64) -> Result<u64, ProbeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ProbeError::Closed);
        }
        let (send, receive) = oneshot::channel();
        self.runtime.spawn(async move {
            tokio::task::yield_now().await;
            let _ = send.send(value);
        });
        receive.await.map_err(|_| ProbeError::WorkerStopped)
    }

    // Mechanism-only: exercise a foreign trait defined by the *other* UniFFI
    // component from Rust's blocking lane. This is not Hub/BLeng assembly.
    pub async fn probe_foreign_backend(
        &self,
        backend: Arc<dyn blehub::BleBackend>,
    ) -> Result<u32, ProbeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ProbeError::Closed);
        }
        self.runtime
            .spawn_blocking(move || {
                let version = backend
                    .capabilities()
                    .map_err(|_| ProbeError::WorkerStopped)?
                    .contract_version;
                backend.dispose().map_err(|_| ProbeError::WorkerStopped)?;
                Ok(version)
            })
            .await
            .map_err(|_| ProbeError::WorkerStopped)?
    }

    // A releasable pending future for observing generated-language cancellation.
    pub async fn wait_for_release(&self) -> Result<u64, ProbeError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ProbeError::Closed);
        }
        let _drop_observer = ObserveFutureDrop(&self.waiting_dropped);
        self.waiting.store(true, Ordering::Release);
        self.release.notified().await;
        Ok(17)
    }

    pub fn was_waiting_future_dropped(&self) -> bool {
        self.waiting_dropped.load(Ordering::Acquire)
    }

    pub fn is_waiting(&self) -> bool {
        self.waiting.load(Ordering::Acquire)
    }

    pub fn release_waiter(&self) {
        self.release.notify_one();
    }

    pub fn shutdown(&self) {
        self.closed.store(true, Ordering::Release);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owned_runtime_can_complete_without_a_caller_tokio_runtime() {
        let probe = AsyncProbe::new().unwrap();
        let host = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        assert_eq!(host.block_on(probe.round_trip(u64::MAX)).unwrap(), u64::MAX);
        probe.shutdown();
        assert!(matches!(
            host.block_on(probe.round_trip(1)),
            Err(ProbeError::Closed)
        ));
    }
}
