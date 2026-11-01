use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    pin::Pin,
    sync::Arc,
};

use crate::recovery::{RecoveryAction, RecoveryContext, RecoveryOperationId, RecoverySignal};
use blehub::{BleBackend, BluetoothUuid, NotificationModeRequest, ScanOptions};
use bleng::Bleng;

use crate::error::{RecoveryTargetError, SupervisorError};

/// Counts reported by a platform session after its BLeng owner has shut down.
pub type ResourceSnapshot = BTreeMap<String, u64>;

/// Cross-platform BLE target needed for a real recovery attempt.
///
/// Native adapters map these existing BleHub contract values to their platform
/// scan/GATT APIs. Peer handles and native objects remain platform-owned.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecoveryTarget {
    pub scan_options: ScanOptions,
    pub service_uuid: BluetoothUuid,
    pub characteristic_uuid: BluetoothUuid,
    pub notification_mode: NotificationModeRequest,
    pub expected_value: Vec<u8>,
}

impl RecoveryTarget {
    /// Validates and canonicalizes all UUIDs before native execution.
    pub fn canonicalized(&self) -> Result<Self, RecoveryTargetError> {
        let service_uuid = BluetoothUuid::canonicalize(&self.service_uuid.value)
            .ok_or(RecoveryTargetError::InvalidServiceUuid)?;
        let characteristic_uuid = BluetoothUuid::canonicalize(&self.characteristic_uuid.value)
            .ok_or(RecoveryTargetError::InvalidCharacteristicUuid)?;
        if self
            .scan_options
            .name_prefix
            .as_ref()
            .is_some_and(String::is_empty)
        {
            return Err(RecoveryTargetError::EmptyScanNamePrefix);
        }
        let mut scan_options = self.scan_options.clone();
        scan_options.service_uuids = scan_options
            .service_uuids
            .iter()
            .enumerate()
            .map(|(index, uuid)| {
                BluetoothUuid::canonicalize(&uuid.value)
                    .ok_or(RecoveryTargetError::InvalidScanServiceUuid { index })
            })
            .collect::<Result<_, _>>()?;
        Ok(Self {
            scan_options,
            service_uuid,
            characteristic_uuid,
            notification_mode: self.notification_mode,
            expected_value: self.expected_value.clone(),
        })
    }
}

/// The native half of one owner/backend lifetime.
///
/// `backend()` is transferred into `Bleng::assemble_backend`. Once assembly
/// has been attempted, `Bleng` owns backend disposal. `dispose_unassembled()`
/// is only for failures before that transfer; calling it after assembly would
/// create a double-dispose race. After `Bleng::shutdown()`, the supervisor
/// asks this object for the platform resource snapshot.
pub trait PlatformOwnerSession: Send + Sync {
    fn context(&self) -> &RecoveryContext;

    fn backend(&self) -> Arc<dyn BleBackend>;

    fn dispose_unassembled(&self) -> Result<(), SupervisorError>;

    /// Starts delivery of the owner's public event stream after assembly.
    fn start_event_reader(&self, owner: Arc<Bleng>) -> Result<(), SupervisorError>;

    /// Waits until the event reader has drained after owner shutdown.
    fn wait_event_reader(
        &self,
    ) -> Pin<Box<dyn Future<Output = Result<(), SupervisorError>> + Send + '_>>;

    fn resource_snapshot(
        &self,
    ) -> Pin<Box<dyn Future<Output = Result<ResourceSnapshot, SupervisorError>> + Send + '_>>;

    /// Executes one platform-specific recovery action and returns its evidence.
    ///
    /// The returned signal is later fenced with this session's context by the
    /// Rust supervisor. Platform code must not relabel an old callback using a
    /// newer context and must return bounded failure as a recovery signal.
    fn execute_recovery_action(
        &self,
        sequence: u64,
        action: PlatformRecoveryAction,
    ) -> Pin<Box<dyn Future<Output = Result<PlatformActionResult, SupervisorError>> + Send + '_>>;
}

/// Creates one native platform session for a requested owner context.
pub trait PlatformOwnerSessionFactory: Send + Sync {
    fn create(
        &self,
        context: &RecoveryContext,
    ) -> Result<Box<dyn PlatformOwnerSession>, SupervisorError>;
}

/// The subset of recovery work that requires platform BLE capabilities.
///
/// Owner replacement is intentionally absent: `SessionSupervisor` performs
/// `RetireOldOwner` and `CreateFreshOwner` itself. The platform executes one
/// action and returns a context-local [`RecoverySignal`] as evidence.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PlatformRecoveryAction {
    WaitForRadioOn,
    AwaitOldOperationTerminals {
        remaining: BTreeSet<RecoveryOperationId>,
    },
    AttemptSameOwnerRecovery {
        after_central_recreation: bool,
        target: RecoveryTarget,
    },
    RecreateCentral,
    AttemptFreshOwnerRecovery {
        target: RecoveryTarget,
    },
}

impl PlatformRecoveryAction {
    /// Converts one policy action into the platform-executable subset.
    ///
    /// Owner-boundary actions stay in the Rust supervisor and are rejected here
    /// instead of being accidentally sent to a native adapter.
    pub(crate) fn from_recovery_action(
        action: &RecoveryAction,
        target: &RecoveryTarget,
    ) -> Result<Self, SupervisorError> {
        match action {
            RecoveryAction::WaitForRadioOn => Ok(Self::WaitForRadioOn),
            RecoveryAction::AwaitOldOperationTerminals { remaining } => {
                Ok(Self::AwaitOldOperationTerminals {
                    remaining: remaining.clone(),
                })
            }
            RecoveryAction::AttemptSameOwnerRecovery {
                after_central_recreation,
            } => Ok(Self::AttemptSameOwnerRecovery {
                after_central_recreation: *after_central_recreation,
                target: target.clone(),
            }),
            RecoveryAction::RecreateCentral => Ok(Self::RecreateCentral),
            RecoveryAction::AttemptFreshOwnerRecovery => Ok(Self::AttemptFreshOwnerRecovery {
                target: target.clone(),
            }),
            RecoveryAction::RetireOldOwner | RecoveryAction::CreateFreshOwner => {
                Err(SupervisorError::RecoveryActionNotPlatform {
                    action: "owner or host recovery action",
                })
            }
        }
    }
}

/// Evidence returned by a platform action. The sequence is echoed by the
/// native adapter so a late result from an older action cannot advance policy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlatformActionResult {
    pub sequence: u64,
    pub signal: RecoverySignal,
}
