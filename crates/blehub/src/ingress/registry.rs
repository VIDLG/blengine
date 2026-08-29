use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

use arc_swap::ArcSwap;

use crate::BleHubError;

use super::{BackendEventIngressToken, EventIngressState};

type RegistrySnapshot = HashMap<u64, Weak<EventIngressState>>;

pub(crate) struct BackendEventIngressRegistry {
    next_token: AtomicU64,
    entries: ArcSwap<RegistrySnapshot>,
    writes: Mutex<()>,
}

impl BackendEventIngressRegistry {
    pub(crate) fn new() -> Self {
        Self {
            next_token: AtomicU64::new(1),
            entries: ArcSwap::from_pointee(HashMap::new()),
            writes: Mutex::new(()),
        }
    }

    pub(crate) fn allocate_token(&self) -> Result<BackendEventIngressToken, BleHubError> {
        self.next_token
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |value| {
                value.checked_add(1)
            })
            .map(BackendEventIngressToken::new)
            .map_err(|_| BleHubError::FfiContract {
                detail: "event ingress token space exhausted".to_owned(),
            })
    }

    pub(crate) fn register(
        self: &Arc<Self>,
        token: BackendEventIngressToken,
        state: Weak<EventIngressState>,
    ) -> Result<BackendEventIngressRegistration, BleHubError> {
        if !token.is_valid() {
            return Err(BleHubError::FfiContract {
                detail: "event ingress token must be nonzero".to_owned(),
            });
        }

        let _write = self
            .writes
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.entries.load().contains_key(&token.value) {
            return Err(BleHubError::FfiContract {
                detail: "event ingress token is already registered".to_owned(),
            });
        }

        self.entries.rcu(|current| {
            let mut next = (**current).clone();
            next.insert(token.value, state.clone());
            next
        });

        Ok(BackendEventIngressRegistration {
            registry: Arc::clone(self),
            token,
            registered: AtomicBool::new(true),
        })
    }

    pub(crate) fn lookup(
        &self,
        token: BackendEventIngressToken,
    ) -> Option<Weak<EventIngressState>> {
        self.entries.load().get(&token.value).cloned()
    }

    fn unregister(&self, token: BackendEventIngressToken) {
        let _write = self
            .writes
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !self.entries.load().contains_key(&token.value) {
            return;
        }
        self.entries.rcu(|current| {
            let mut next = (**current).clone();
            next.remove(&token.value);
            next
        });
    }

    #[cfg(test)]
    pub(crate) fn set_next_token(&self, value: u64) {
        self.next_token.store(value, Ordering::Release);
    }

    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        self.entries.load().len()
    }
}

pub(crate) struct BackendEventIngressRegistration {
    registry: Arc<BackendEventIngressRegistry>,
    token: BackendEventIngressToken,
    registered: AtomicBool,
}

impl BackendEventIngressRegistration {
    pub(crate) fn unregister(&self) {
        if self.registered.swap(false, Ordering::AcqRel) {
            self.registry.unregister(self.token);
        }
    }
}

impl Drop for BackendEventIngressRegistration {
    fn drop(&mut self) {
        self.unregister();
    }
}
