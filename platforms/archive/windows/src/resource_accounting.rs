#[cfg(feature = "resource-accounting")]
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

#[derive(Clone, Copy)]
pub(crate) enum CallbackResource {
    AdapterLookup,
    RadioLookup,
    DeviceLookup,
    GattProbe,
    ServiceDiscovery,
    CharacteristicDiscovery,
    DescriptorDiscovery,
    CharacteristicRead,
    CharacteristicWrite,
    CccdWrite,
    RadioStateChanged,
    AdvertisementReceived,
    AdvertisementStopped,
    ConnectionStatusChanged,
    GattServicesChanged,
    CharacteristicValueChanged,
}

impl CallbackResource {
    #[cfg(feature = "resource-accounting")]
    const COUNT: usize = 16;

    #[cfg(feature = "resource-accounting")]
    const fn index(self) -> usize {
        self as usize
    }
}

#[derive(Clone, Default)]
pub(crate) struct ResourceLedger {
    #[cfg(feature = "resource-accounting")]
    counters: Arc<ResourceCounters>,
}

impl ResourceLedger {
    pub(crate) fn track(&self, resource: CallbackResource) -> CallbackResourceGuard {
        #[cfg(feature = "resource-accounting")]
        self.counters.live[resource.index()].fetch_add(1, Ordering::AcqRel);
        #[cfg(not(feature = "resource-accounting"))]
        let _ = resource;

        CallbackResourceGuard {
            #[cfg(feature = "resource-accounting")]
            counters: Arc::clone(&self.counters),
            #[cfg(feature = "resource-accounting")]
            resource,
        }
    }

    #[cfg(feature = "resource-accounting")]
    pub(crate) fn probe(&self) -> WindowsResourceProbe {
        WindowsResourceProbe {
            counters: Arc::clone(&self.counters),
        }
    }
}

#[must_use = "the guard must be retained by the native callback delegate"]
pub(crate) struct CallbackResourceGuard {
    #[cfg(feature = "resource-accounting")]
    counters: Arc<ResourceCounters>,
    #[cfg(feature = "resource-accounting")]
    resource: CallbackResource,
}

#[cfg(feature = "resource-accounting")]
impl Drop for CallbackResourceGuard {
    fn drop(&mut self) {
        self.counters.live[self.resource.index()].fetch_sub(1, Ordering::AcqRel);
    }
}

#[cfg(feature = "resource-accounting")]
struct ResourceCounters {
    live: [AtomicUsize; CallbackResource::COUNT],
}

#[cfg(feature = "resource-accounting")]
impl Default for ResourceCounters {
    fn default() -> Self {
        Self {
            live: std::array::from_fn(|_| AtomicUsize::new(0)),
        }
    }
}

#[cfg(feature = "resource-accounting")]
#[doc(hidden)]
#[derive(Clone)]
pub struct WindowsResourceProbe {
    counters: Arc<ResourceCounters>,
}

#[cfg(feature = "resource-accounting")]
impl WindowsResourceProbe {
    pub fn snapshot(&self) -> WindowsCallbackResourceSnapshot {
        let load = |resource: CallbackResource| {
            self.counters.live[resource.index()].load(Ordering::Acquire)
        };
        WindowsCallbackResourceSnapshot {
            adapter_lookup: load(CallbackResource::AdapterLookup),
            radio_lookup: load(CallbackResource::RadioLookup),
            device_lookup: load(CallbackResource::DeviceLookup),
            gatt_probe: load(CallbackResource::GattProbe),
            service_discovery: load(CallbackResource::ServiceDiscovery),
            characteristic_discovery: load(CallbackResource::CharacteristicDiscovery),
            descriptor_discovery: load(CallbackResource::DescriptorDiscovery),
            characteristic_read: load(CallbackResource::CharacteristicRead),
            characteristic_write: load(CallbackResource::CharacteristicWrite),
            cccd_write: load(CallbackResource::CccdWrite),
            radio_state_changed: load(CallbackResource::RadioStateChanged),
            advertisement_received: load(CallbackResource::AdvertisementReceived),
            advertisement_stopped: load(CallbackResource::AdvertisementStopped),
            connection_status_changed: load(CallbackResource::ConnectionStatusChanged),
            gatt_services_changed: load(CallbackResource::GattServicesChanged),
            characteristic_value_changed: load(CallbackResource::CharacteristicValueChanged),
        }
    }
}

#[cfg(feature = "resource-accounting")]
#[doc(hidden)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WindowsCallbackResourceSnapshot {
    pub adapter_lookup: usize,
    pub radio_lookup: usize,
    pub device_lookup: usize,
    pub gatt_probe: usize,
    pub service_discovery: usize,
    pub characteristic_discovery: usize,
    pub descriptor_discovery: usize,
    pub characteristic_read: usize,
    pub characteristic_write: usize,
    pub cccd_write: usize,
    pub radio_state_changed: usize,
    pub advertisement_received: usize,
    pub advertisement_stopped: usize,
    pub connection_status_changed: usize,
    pub gatt_services_changed: usize,
    pub characteristic_value_changed: usize,
}

#[cfg(feature = "resource-accounting")]
impl WindowsCallbackResourceSnapshot {
    pub fn live_async_completion_callbacks(self) -> usize {
        self.adapter_lookup
            + self.radio_lookup
            + self.device_lookup
            + self.gatt_probe
            + self.service_discovery
            + self.characteristic_discovery
            + self.descriptor_discovery
            + self.characteristic_read
            + self.characteristic_write
            + self.cccd_write
    }

    pub fn live_event_callbacks(self) -> usize {
        self.radio_state_changed
            + self.advertisement_received
            + self.advertisement_stopped
            + self.connection_status_changed
            + self.gatt_services_changed
            + self.characteristic_value_changed
    }

    pub fn is_zero(self) -> bool {
        self.live_async_completion_callbacks() == 0 && self.live_event_callbacks() == 0
    }
}

#[cfg(feature = "resource-accounting")]
#[doc(hidden)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WindowsLogicalResourceSnapshot {
    pub ingress_roots: usize,
    pub pending_initialization_operations: usize,
    pub adapter_roots: usize,
    pub radio_roots: usize,
    pub radio_event_tokens: usize,
    pub scan_roots: usize,
    pub connection_roots: usize,
    pub owns_adapter_lease: bool,
}

#[cfg(feature = "resource-accounting")]
impl WindowsLogicalResourceSnapshot {
    pub fn is_zero(self) -> bool {
        self.ingress_roots == 0
            && self.pending_initialization_operations == 0
            && self.adapter_roots == 0
            && self.radio_roots == 0
            && self.radio_event_tokens == 0
            && self.scan_roots == 0
            && self.connection_roots == 0
            && !self.owns_adapter_lease
    }
}

#[cfg(all(test, feature = "resource-accounting"))]
mod tests {
    use super::*;

    #[test]
    fn callback_guards_measure_delegate_lifetime_until_drop() {
        let ledger = ResourceLedger::default();
        let probe = ledger.probe();
        let radio = ledger.track(CallbackResource::RadioStateChanged);
        let read = ledger.track(CallbackResource::CharacteristicRead);

        let live = probe.snapshot();
        assert_eq!(live.radio_state_changed, 1);
        assert_eq!(live.characteristic_read, 1);
        assert_eq!(live.live_event_callbacks(), 1);
        assert_eq!(live.live_async_completion_callbacks(), 1);

        drop(radio);
        assert_eq!(probe.snapshot().live_event_callbacks(), 0);
        drop(read);
        assert!(probe.snapshot().is_zero());
    }
}
