use blehub::{AdvertisementObservation, BleEvent, CommandId, ScanSessionId, ScanStopResult};

use crate::recovery::RecoveryContext;

/// The scan-only subset of the owner's existing public event stream.
#[derive(Clone, Debug, PartialEq, Eq)]
#[cfg_attr(feature = "ffi", derive(uniffi::Enum))]
pub enum ScanEvent {
    Started {
        command_id: CommandId,
        scan_id: ScanSessionId,
    },
    Advertisement {
        advertisement: AdvertisementObservation,
    },
    Stopped {
        scan_id: ScanSessionId,
        result: ScanStopResult,
    },
}

/// An observation stamped with its immutable source owner, not the current active owner.
///
/// Delivery may outlive retirement. Downstream consumers must match the full source context;
/// numeric scan and peripheral IDs alone do not identify an owner lifetime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScanEventEnvelope {
    source_context: RecoveryContext,
    event: ScanEvent,
}

impl ScanEventEnvelope {
    pub fn source_context(&self) -> &RecoveryContext {
        &self.source_context
    }

    pub fn event(&self) -> &ScanEvent {
        &self.event
    }

    pub fn into_parts(self) -> (RecoveryContext, ScanEvent) {
        (self.source_context, self.event)
    }

    pub(crate) fn new(source_context: RecoveryContext, event: ScanEvent) -> Self {
        Self {
            source_context,
            event,
        }
    }
}

/// Maps exactly the three scan variants while preserving their complete payloads.
pub(crate) fn scan_event(event: BleEvent) -> Option<ScanEvent> {
    match event {
        BleEvent::ScanStarted {
            command_id,
            scan_id,
        } => Some(ScanEvent::Started {
            command_id,
            scan_id,
        }),
        BleEvent::AdvertisementObserved { advertisement } => {
            Some(ScanEvent::Advertisement { advertisement })
        }
        BleEvent::ScanStopped { scan_id, result } => Some(ScanEvent::Stopped { scan_id, result }),
        _ => None,
    }
}
