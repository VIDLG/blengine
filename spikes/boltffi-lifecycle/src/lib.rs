use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock, Weak};

use boltffi::*;

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StartScanCommand {
    pub command_id: u64,
    pub scan_session_id: u64,
}

#[data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BleEvent {
    pub sequence: u64,
    pub command_id: Option<u64>,
    pub payload: Vec<u8>,
}

#[data]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(i32)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
    DownstreamFailed,
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum BleHubError {
    #[error("BleHub is closed")]
    Closed,
    #[error("builder has already been finished")]
    AlreadyFinished,
    #[error("foreign callback failed unexpectedly")]
    UnexpectedCallback,
}

impl From<UnexpectedFfiCallbackError> for BleHubError {
    fn from(_: UnexpectedFfiCallbackError) -> Self {
        Self::UnexpectedCallback
    }
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum CommandDispatchError {
    #[error("BleHub is closed")]
    Closed,
    #[error("backend rejected the command")]
    Rejected,
    #[error("foreign callback failed unexpectedly")]
    UnexpectedCallback,
}

impl From<UnexpectedFfiCallbackError> for CommandDispatchError {
    fn from(_: UnexpectedFfiCallbackError) -> Self {
        Self::UnexpectedCallback
    }
}

#[boltffi::error]
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum EventSinkError {
    #[error("downstream rejected the event")]
    Rejected,
    #[error("foreign sink callback failed unexpectedly")]
    UnexpectedCallback,
}

impl From<UnexpectedFfiCallbackError> for EventSinkError {
    fn from(_: UnexpectedFfiCallbackError) -> Self {
        Self::UnexpectedCallback
    }
}

#[export]
pub trait BleBackend: Send + Sync {
    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
    fn dispose(&self) -> Result<(), BleHubError>;
}

#[export]
pub trait BleEventSink: Send + Sync {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError>;
}

struct BackendEventBridge {
    closed: AtomicBool,
    sink: Mutex<Option<Box<dyn BleEventSink>>>,
}

impl BackendEventBridge {
    fn new(sink: Box<dyn BleEventSink>) -> Self {
        Self {
            closed: AtomicBool::new(false),
            sink: Mutex::new(Some(sink)),
        }
    }

    fn close(&self) {
        self.closed.store(true, Ordering::Release);
        let mut sink = self.sink.lock().unwrap_or_else(|error| error.into_inner());
        sink.take();
    }

    fn push(&self, event: BleEvent) -> EventIngressStatus {
        if self.closed.load(Ordering::Acquire) {
            return EventIngressStatus::Closed;
        }

        let Ok(sink) = self.sink.lock() else {
            return EventIngressStatus::DownstreamFailed;
        };
        if self.closed.load(Ordering::Acquire) {
            return EventIngressStatus::Closed;
        }
        let Some(sink) = sink.as_deref() else {
            return EventIngressStatus::Closed;
        };

        match sink.on_event(event) {
            Ok(()) => EventIngressStatus::Delivered,
            Err(_) => EventIngressStatus::DownstreamFailed,
        }
    }
}

#[derive(Clone)]
pub struct BackendEventSinkHandle {
    inner: Weak<BackendEventBridge>,
}

#[export]
impl BackendEventSinkHandle {
    pub fn push(&self, event: BleEvent) -> EventIngressStatus {
        self.inner
            .upgrade()
            .map_or(EventIngressStatus::Closed, |inner| inner.push(event))
    }

    pub fn push_from_rust_thread(&self, event: BleEvent) -> EventIngressStatus {
        let ingress = self.clone();
        std::thread::spawn(move || ingress.push(event))
            .join()
            .unwrap_or(EventIngressStatus::DownstreamFailed)
    }
}

pub struct BleHubBuilder {
    bridge: Arc<BackendEventBridge>,
    finished: AtomicBool,
}

#[export]
impl BleHubBuilder {
    pub fn new(event_sink: Box<dyn BleEventSink>) -> Self {
        Self {
            bridge: Arc::new(BackendEventBridge::new(event_sink)),
            finished: AtomicBool::new(false),
        }
    }

    pub fn backend_event_sink(&self) -> BackendEventSinkHandle {
        BackendEventSinkHandle {
            inner: Arc::downgrade(&self.bridge),
        }
    }

    pub fn finish(&self, backend: Box<dyn BleBackend>) -> Result<BleHub, BleHubError> {
        self.finished
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| BleHubError::AlreadyFinished)?;

        Ok(BleHub {
            bridge: Arc::clone(&self.bridge),
            backend: RwLock::new(Some(backend)),
            closed: AtomicBool::new(false),
        })
    }
}

pub struct BleHub {
    bridge: Arc<BackendEventBridge>,
    backend: RwLock<Option<Box<dyn BleBackend>>>,
    closed: AtomicBool,
}

#[export]
impl BleHub {
    pub fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        let backend = self
            .backend
            .read()
            .map_err(|_| CommandDispatchError::Closed)?;
        if self.closed.load(Ordering::Acquire) {
            return Err(CommandDispatchError::Closed);
        }

        backend
            .as_ref()
            .ok_or(CommandDispatchError::Closed)?
            .start_scan(command)
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub fn dispose(&self) -> Result<(), BleHubError> {
        let mut backend = self.backend.write().map_err(|_| BleHubError::Closed)?;
        if self.closed.swap(true, Ordering::AcqRel) {
            return Ok(());
        }

        self.bridge.close();
        let result = backend.as_ref().map_or(Ok(()), |value| value.dispose());
        backend.take();
        result
    }
}

impl Drop for BleHub {
    fn drop(&mut self) {
        let Ok(backend) = self.backend.get_mut() else {
            return;
        };
        if self.closed.swap(true, Ordering::AcqRel) {
            return;
        }

        self.bridge.close();
        if let Some(value) = backend.take() {
            let _ = value.dispose();
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::thread;
    use std::time::Duration;

    use super::*;

    struct RecordingSink {
        events: Arc<Mutex<Vec<BleEvent>>>,
    }

    impl BleEventSink for RecordingSink {
        fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
            self.events.lock().unwrap().push(event);
            Ok(())
        }
    }

    struct SynchronousBackend {
        ingress: BackendEventSinkHandle,
        dispose_count: Arc<AtomicUsize>,
    }

    impl BleBackend for SynchronousBackend {
        fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
            assert_eq!(
                self.ingress.push(BleEvent {
                    sequence: 1,
                    command_id: Some(command.command_id),
                    payload: vec![1, 2, 3],
                }),
                EventIngressStatus::Delivered
            );
            Ok(())
        }

        fn dispose(&self) -> Result<(), BleHubError> {
            self.dispose_count.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    struct RejectingBackend;

    impl BleBackend for RejectingBackend {
        fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
            Err(CommandDispatchError::Rejected)
        }

        fn dispose(&self) -> Result<(), BleHubError> {
            Ok(())
        }
    }

    #[test]
    fn synchronous_callback_before_command_return_is_delivered() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let dispose_count = Arc::new(AtomicUsize::new(0));
        let builder = BleHubBuilder::new(Box::new(RecordingSink {
            events: Arc::clone(&events),
        }));
        let ingress = builder.backend_event_sink();
        let hub = builder
            .finish(Box::new(SynchronousBackend {
                ingress,
                dispose_count: Arc::clone(&dispose_count),
            }))
            .unwrap();

        hub.start_scan(StartScanCommand {
            command_id: 7,
            scan_session_id: 11,
        })
        .unwrap();

        assert_eq!(events.lock().unwrap().len(), 1);
        assert_eq!(events.lock().unwrap()[0].command_id, Some(7));
        hub.dispose().unwrap();
        hub.dispose().unwrap();
        assert_eq!(dispose_count.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn rejected_dispatch_emits_no_event() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let builder = BleHubBuilder::new(Box::new(RecordingSink {
            events: Arc::clone(&events),
        }));
        let hub = builder.finish(Box::new(RejectingBackend)).unwrap();

        assert_eq!(
            hub.start_scan(StartScanCommand {
                command_id: 1,
                scan_session_id: 2,
            }),
            Err(CommandDispatchError::Rejected)
        );
        assert!(events.lock().unwrap().is_empty());
    }

    #[test]
    fn late_callback_is_closed_and_not_delivered() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let builder = BleHubBuilder::new(Box::new(RecordingSink {
            events: Arc::clone(&events),
        }));
        let ingress = builder.backend_event_sink();
        let hub = builder.finish(Box::new(RejectingBackend)).unwrap();

        hub.dispose().unwrap();
        assert_eq!(
            ingress.push(BleEvent {
                sequence: 1,
                command_id: None,
                payload: vec![9],
            }),
            EventIngressStatus::Closed
        );
        assert!(events.lock().unwrap().is_empty());
    }

    struct SerializedSink {
        active: Arc<AtomicUsize>,
        maximum: Arc<AtomicUsize>,
    }

    impl BleEventSink for SerializedSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
            self.maximum.fetch_max(active, Ordering::SeqCst);
            thread::sleep(Duration::from_millis(2));
            self.active.fetch_sub(1, Ordering::SeqCst);
            Ok(())
        }
    }

    #[test]
    fn concurrent_pushes_are_serialized() {
        let active = Arc::new(AtomicUsize::new(0));
        let maximum = Arc::new(AtomicUsize::new(0));
        let builder = BleHubBuilder::new(Box::new(SerializedSink {
            active: Arc::clone(&active),
            maximum: Arc::clone(&maximum),
        }));
        let ingress = builder.backend_event_sink();

        let threads: Vec<_> = (0..16)
            .map(|sequence| {
                let ingress = ingress.clone();
                thread::spawn(move || {
                    ingress.push(BleEvent {
                        sequence,
                        command_id: None,
                        payload: vec![0; 244],
                    })
                })
            })
            .collect();

        for thread in threads {
            assert_eq!(thread.join().unwrap(), EventIngressStatus::Delivered);
        }
        assert_eq!(maximum.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn builder_can_only_finish_once() {
        let builder = BleHubBuilder::new(Box::new(RecordingSink {
            events: Arc::new(Mutex::new(Vec::new())),
        }));
        let _hub = builder.finish(Box::new(RejectingBackend)).unwrap();
        assert!(matches!(
            builder.finish(Box::new(RejectingBackend)),
            Err(BleHubError::AlreadyFinished)
        ));
    }
}
