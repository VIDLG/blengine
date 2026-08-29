mod registry;
mod state;

use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, LazyLock},
};

pub(crate) use registry::{BackendEventIngressRegistration, BackendEventIngressRegistry};
pub(crate) use state::EventIngressState;

use crate::{BleEvent, EventSinkError};

#[boltffi::data]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct BackendEventIngressToken {
    pub value: u64,
}

impl BackendEventIngressToken {
    pub const fn new(value: u64) -> Self {
        Self { value }
    }

    pub const fn is_valid(self) -> bool {
        self.value != 0
    }
}

#[boltffi::data]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EventIngressStatus {
    Delivered,
    Closed,
    InvalidEvent { message: String },
    DownstreamFailed { message: String },
}

pub trait BleEventSink: Send + Sync {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError>;
}

static BACKEND_EVENT_INGRESS_REGISTRY: LazyLock<Arc<BackendEventIngressRegistry>> =
    LazyLock::new(|| Arc::new(BackendEventIngressRegistry::new()));

pub(crate) fn global_backend_event_ingress_registry() -> &'static Arc<BackendEventIngressRegistry> {
    &BACKEND_EVENT_INGRESS_REGISTRY
}

pub(crate) fn push_backend_event_with_registry(
    registry: &BackendEventIngressRegistry,
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus {
    let Some(state) = registry.lookup(token).and_then(|state| state.upgrade()) else {
        return EventIngressStatus::Closed;
    };
    state.push(event)
}

fn push_backend_event_contained(
    registry: &BackendEventIngressRegistry,
    token: BackendEventIngressToken,
    event: BleEvent,
) -> EventIngressStatus {
    catch_unwind(AssertUnwindSafe(|| {
        push_backend_event_with_registry(registry, token, event)
    }))
    .unwrap_or_else(|_| EventIngressStatus::DownstreamFailed {
        message: "downstream panicked while handling an event".to_owned(),
    })
}

#[boltffi::export]
pub fn push_backend_event(token: BackendEventIngressToken, event: BleEvent) -> EventIngressStatus {
    push_backend_event_contained(
        global_backend_event_ingress_registry().as_ref(),
        token,
        event,
    )
}

#[cfg(test)]
mod tests {
    use std::{
        collections::HashSet,
        sync::{Barrier, Mutex, Weak, mpsc},
        thread,
        time::Duration,
    };

    use super::*;
    use crate::{BleEventPayload, CommandId, ScanSessionId};

    struct AcceptingSink;

    impl BleEventSink for AcceptingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            Ok(())
        }
    }

    struct PanickingSink;

    impl BleEventSink for PanickingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            panic!("intentional downstream panic")
        }
    }

    struct BlockingSink {
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    }

    impl BleEventSink for BlockingSink {
        fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
            self.entered.wait();
            self.release.wait();
            Ok(())
        }
    }

    fn scan_started() -> BleEvent {
        BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id: CommandId::new(1),
                scan_id: ScanSessionId::new(2),
            },
        }
    }

    #[test]
    fn zero_token_registration_is_rejected() {
        let registry = Arc::new(BackendEventIngressRegistry::new());

        assert!(
            registry
                .register(
                    BackendEventIngressToken::new(0),
                    Weak::<EventIngressState>::new(),
                )
                .is_err()
        );
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn concurrent_token_allocations_are_nonzero_and_unique() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let values = Arc::new(Mutex::new(Vec::new()));
        let workers = (0..16)
            .map(|_| {
                let registry = Arc::clone(&registry);
                let values = Arc::clone(&values);
                thread::spawn(move || {
                    let allocated = (0..1_000)
                        .map(|_| registry.allocate_token().unwrap().value)
                        .collect::<Vec<_>>();
                    values.lock().unwrap().extend(allocated);
                })
            })
            .collect::<Vec<_>>();

        for worker in workers {
            worker.join().unwrap();
        }

        let values = values.lock().unwrap();
        assert_eq!(values.len(), 16_000);
        assert!(values.iter().all(|value| *value != 0));
        assert_eq!(values.iter().copied().collect::<HashSet<_>>().len(), 16_000);
    }

    #[test]
    fn token_allocation_exhaustion_does_not_wrap() {
        let registry = BackendEventIngressRegistry::new();
        registry.set_next_token(u64::MAX - 1);

        assert_eq!(registry.allocate_token().unwrap().value, u64::MAX - 1);
        assert!(registry.allocate_token().is_err());
        assert!(registry.allocate_token().is_err());
    }

    #[test]
    fn registry_weak_entry_does_not_retain_ingress_state() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = registry.allocate_token().unwrap();
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let registration = registry.register(token, Arc::downgrade(&state)).unwrap();

        assert_eq!(Arc::strong_count(&state), 1);
        drop(state);

        assert!(registry.lookup(token).unwrap().upgrade().is_none());
        assert_eq!(
            push_backend_event_with_registry(&registry, token, scan_started()),
            EventIngressStatus::Closed
        );
        registration.unregister();
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn registration_failure_leaves_the_existing_entry_unchanged() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = registry.allocate_token().unwrap();
        let first = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let registration = registry.register(token, Arc::downgrade(&first)).unwrap();
        let second = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));

        assert!(registry.register(token, Arc::downgrade(&second)).is_err());
        assert_eq!(registry.len(), 1);
        assert!(
            registry
                .lookup(token)
                .unwrap()
                .ptr_eq(&Arc::downgrade(&first))
        );

        registration.unregister();
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn invalid_event_is_rejected_before_downstream_delivery() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = registry.allocate_token().unwrap();
        let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
        let registration = registry.register(token, Arc::downgrade(&state)).unwrap();
        let invalid = BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id: CommandId::new(0),
                scan_id: ScanSessionId::new(2),
            },
        };

        assert!(matches!(
            push_backend_event_contained(&registry, token, invalid),
            EventIngressStatus::InvalidEvent { .. }
        ));
        registration.unregister();
    }

    #[test]
    fn downstream_panic_is_contained_and_close_recovers_poison() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = registry.allocate_token().unwrap();
        let state = Arc::new(EventIngressState::new(Box::new(PanickingSink)));
        let registration = registry.register(token, Arc::downgrade(&state)).unwrap();

        assert!(matches!(
            push_backend_event_contained(&registry, token, scan_started()),
            EventIngressStatus::DownstreamFailed { .. }
        ));
        state.close_and_drain();
        registration.unregister();
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn close_waits_for_in_flight_delivery() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        let token = registry.allocate_token().unwrap();
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let state = Arc::new(EventIngressState::new(Box::new(BlockingSink {
            entered: Arc::clone(&entered),
            release: Arc::clone(&release),
        })));
        let registration = registry.register(token, Arc::downgrade(&state)).unwrap();

        let push_registry = Arc::clone(&registry);
        let push = thread::spawn(move || {
            push_backend_event_contained(&push_registry, token, scan_started())
        });
        entered.wait();

        let close_state = Arc::clone(&state);
        let (closed_tx, closed_rx) = mpsc::channel();
        let close = thread::spawn(move || {
            close_state.close_and_drain();
            closed_tx.send(()).unwrap();
        });
        assert!(closed_rx.recv_timeout(Duration::from_millis(20)).is_err());

        release.wait();
        assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
        closed_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        close.join().unwrap();
        registration.unregister();
    }

    #[test]
    fn ten_thousand_registration_lifecycles_return_to_baseline() {
        let registry = Arc::new(BackendEventIngressRegistry::new());
        for _ in 0..10_000 {
            let token = registry.allocate_token().unwrap();
            let state = Arc::new(EventIngressState::new(Box::new(AcceptingSink)));
            let registration = registry.register(token, Arc::downgrade(&state)).unwrap();
            assert_eq!(
                push_backend_event_with_registry(&registry, token, scan_started()),
                EventIngressStatus::Delivered
            );
            registration.unregister();
        }
        assert_eq!(registry.len(), 0);
    }
}
