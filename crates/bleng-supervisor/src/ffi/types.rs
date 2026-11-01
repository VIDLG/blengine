use blehub::{BluetoothUuid, NotificationModeRequest, ScanOptions};
use thiserror::Error;

use crate::recovery::{
    OwnerBackendIdentity, OwnerGeneration, RecoveryAction, RecoveryAttemptId, RecoveryContext,
    RecoveryLink, RecoveryObservation, RecoveryOperationId, RecoveryPolicyConfig, RecoverySignal,
};
use crate::{PlatformRecoveryAction, RecoveryTarget, SupervisorConfig, SupervisorError};

/// FFI-safe owner/session context used by a native platform factory.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOwnerContext {
    pub owner_id: String,
    pub backend_id: String,
    pub owner_generation: u64,
    pub recovery_attempt: u64,
}

impl From<&RecoveryContext> for ForeignOwnerContext {
    fn from(context: &RecoveryContext) -> Self {
        Self {
            owner_id: context
                .owner_backend_identity()
                .owner_id()
                .as_str()
                .to_owned(),
            backend_id: context
                .owner_backend_identity()
                .backend_id()
                .as_str()
                .to_owned(),
            owner_generation: context.owner_generation().value(),
            recovery_attempt: context.recovery_attempt().value(),
        }
    }
}

impl TryFrom<ForeignOwnerContext> for RecoveryContext {
    type Error = SupervisorFfiError;

    fn try_from(context: ForeignOwnerContext) -> Result<Self, Self::Error> {
        let identity = OwnerBackendIdentity::new(context.owner_id, context.backend_id)
            .map_err(SupervisorFfiError::from_recovery)?;
        let owner_generation = OwnerGeneration::new(context.owner_generation)
            .map_err(SupervisorFfiError::from_recovery)?;
        let recovery_attempt = RecoveryAttemptId::new(context.recovery_attempt)
            .map_err(SupervisorFfiError::from_recovery)?;
        Ok(RecoveryContext::new(
            identity,
            owner_generation,
            recovery_attempt,
        ))
    }
}

/// One native resource count returned after owner retirement.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignResourceCount {
    pub name: String,
    pub count: u64,
}

/// Recovery link identity accepted by the supervisor facade.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryLink {
    pub connection_id: String,
    pub gatt_session_id: String,
    pub subscription_id: String,
}

impl TryFrom<ForeignRecoveryLink> for RecoveryLink {
    type Error = SupervisorFfiError;

    fn try_from(link: ForeignRecoveryLink) -> Result<Self, Self::Error> {
        RecoveryLink::new(
            link.connection_id,
            link.gatt_session_id,
            link.subscription_id,
        )
        .map_err(SupervisorFfiError::from_recovery)
    }
}

/// Cross-platform target specification for native recovery execution.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryTarget {
    pub scan_options: ScanOptions,
    pub service_uuid: BluetoothUuid,
    pub characteristic_uuid: BluetoothUuid,
    pub notification_mode: NotificationModeRequest,
    pub expected_value: Vec<u8>,
}

impl From<ForeignRecoveryTarget> for RecoveryTarget {
    fn from(target: ForeignRecoveryTarget) -> Self {
        Self {
            scan_options: target.scan_options,
            service_uuid: target.service_uuid,
            characteristic_uuid: target.characteristic_uuid,
            notification_mode: target.notification_mode,
            expected_value: target.expected_value,
        }
    }
}

impl From<RecoveryTarget> for ForeignRecoveryTarget {
    fn from(target: RecoveryTarget) -> Self {
        Self {
            scan_options: target.scan_options,
            service_uuid: target.service_uuid,
            characteristic_uuid: target.characteristic_uuid,
            notification_mode: target.notification_mode,
            expected_value: target.expected_value,
        }
    }
}

/// FFI configuration for one recovery policy.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryPolicyConfig {
    pub initial_context: ForeignOwnerContext,
    pub old_link: Option<ForeignRecoveryLink>,
    /// Decimal `CommandId.value` strings produced by the BLeng event stream.
    /// Native adapters must match these exactly; they must not relabel a late event.
    pub expected_old_operation_ids: Vec<String>,
    pub allow_central_recreation: bool,
    pub allow_fresh_owner: bool,
}

/// Complete FFI configuration for one supervisor.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignSupervisorConfig {
    pub recovery: ForeignRecoveryPolicyConfig,
    pub recovery_target: ForeignRecoveryTarget,
    pub required_zero_resource_names: Vec<String>,
}

impl TryFrom<ForeignSupervisorConfig> for SupervisorConfig {
    type Error = SupervisorFfiError;

    fn try_from(config: ForeignSupervisorConfig) -> Result<Self, Self::Error> {
        let ForeignSupervisorConfig {
            recovery,
            recovery_target,
            required_zero_resource_names,
        } = config;
        let ForeignRecoveryPolicyConfig {
            initial_context,
            old_link,
            expected_old_operation_ids,
            allow_central_recreation,
            allow_fresh_owner,
        } = recovery;
        let expected_terminals = expected_old_operation_ids
            .into_iter()
            .map(|id| RecoveryOperationId::new(id).map_err(SupervisorFfiError::from_recovery))
            .collect::<Result<_, _>>()?;
        Ok(Self {
            recovery: RecoveryPolicyConfig {
                initial_context: initial_context.try_into()?,
                old_link: old_link.map(RecoveryLink::try_from).transpose()?,
                expected_terminals,
                allow_central_recreation,
                allow_fresh_owner,
            },
            required_zero_resource_names: required_zero_resource_names.into_iter().collect(),
            recovery_target: recovery_target.into(),
        })
    }
}

/// FFI representation of one policy observation.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignRecoverySignal {
    RadioLost,
    OldOperationTerminal { operation_id: String },
    RadioOn,
    SameOwnerRecovered { link: ForeignRecoveryLink },
    SameOwnerRecoveryFailed { reason: String },
    CentralRecreated,
    CentralRecreationFailed { reason: String },
    OwnerRetired { resources_zero: bool },
    FreshOwnerCreated,
    FreshOwnerRecovered { link: ForeignRecoveryLink },
    FreshOwnerRecoveryFailed { reason: String },
    PlatformTimedOut { reason: String },
    ManualReconnectRequired { reason: String },
}

impl TryFrom<ForeignRecoverySignal> for RecoverySignal {
    type Error = SupervisorFfiError;

    fn try_from(signal: ForeignRecoverySignal) -> Result<Self, Self::Error> {
        Ok(match signal {
            ForeignRecoverySignal::RadioLost => Self::RadioLost,
            ForeignRecoverySignal::OldOperationTerminal { operation_id } => {
                Self::OldOperationTerminal {
                    operation_id: RecoveryOperationId::new(operation_id)
                        .map_err(SupervisorFfiError::from_recovery)?,
                }
            }
            ForeignRecoverySignal::RadioOn => Self::RadioOn,
            ForeignRecoverySignal::SameOwnerRecovered { link } => Self::SameOwnerRecovered {
                link: link.try_into()?,
            },
            ForeignRecoverySignal::SameOwnerRecoveryFailed { reason } => {
                Self::SameOwnerRecoveryFailed { reason }
            }
            ForeignRecoverySignal::CentralRecreated => Self::CentralRecreated,
            ForeignRecoverySignal::CentralRecreationFailed { reason } => {
                Self::CentralRecreationFailed { reason }
            }
            ForeignRecoverySignal::OwnerRetired { resources_zero } => {
                Self::OwnerRetired { resources_zero }
            }
            ForeignRecoverySignal::FreshOwnerCreated => Self::FreshOwnerCreated,
            ForeignRecoverySignal::FreshOwnerRecovered { link } => Self::FreshOwnerRecovered {
                link: link.try_into()?,
            },
            ForeignRecoverySignal::FreshOwnerRecoveryFailed { reason } => {
                Self::FreshOwnerRecoveryFailed { reason }
            }
            ForeignRecoverySignal::PlatformTimedOut { reason } => Self::PlatformTimedOut { reason },
            ForeignRecoverySignal::ManualReconnectRequired { reason } => {
                Self::ManualReconnectRequired { reason }
            }
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryObservation {
    pub source_context: ForeignOwnerContext,
    pub signal: ForeignRecoverySignal,
}

impl TryFrom<ForeignRecoveryObservation> for RecoveryObservation {
    type Error = SupervisorFfiError;

    fn try_from(observation: ForeignRecoveryObservation) -> Result<Self, Self::Error> {
        Ok(Self::new(
            observation.source_context.try_into()?,
            observation.signal.try_into()?,
        ))
    }
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignRecoveryAction {
    WaitForRadioOn,
    AwaitOldOperationTerminals { remaining: Vec<String> },
    AttemptSameOwnerRecovery { after_central_recreation: bool },
    RecreateCentral,
    RetireOldOwner,
    CreateFreshOwner,
    AttemptFreshOwnerRecovery,
}

impl From<RecoveryAction> for ForeignRecoveryAction {
    fn from(action: RecoveryAction) -> Self {
        match action {
            RecoveryAction::WaitForRadioOn => Self::WaitForRadioOn,
            RecoveryAction::AwaitOldOperationTerminals { remaining } => {
                Self::AwaitOldOperationTerminals {
                    remaining: remaining.into_iter().map(|id| id.to_string()).collect(),
                }
            }
            RecoveryAction::AttemptSameOwnerRecovery {
                after_central_recreation,
            } => Self::AttemptSameOwnerRecovery {
                after_central_recreation,
            },
            RecoveryAction::RecreateCentral => Self::RecreateCentral,
            RecoveryAction::RetireOldOwner => Self::RetireOldOwner,
            RecoveryAction::CreateFreshOwner => Self::CreateFreshOwner,
            RecoveryAction::AttemptFreshOwnerRecovery => Self::AttemptFreshOwnerRecovery,
        }
    }
}

/// Platform-only action passed to a native session executor.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignPlatformRecoveryAction {
    WaitForRadioOn,
    AwaitOldOperationTerminals {
        remaining: Vec<String>,
    },
    AttemptSameOwnerRecovery {
        after_central_recreation: bool,
        target: ForeignRecoveryTarget,
    },
    RecreateCentral,
    AttemptFreshOwnerRecovery {
        target: ForeignRecoveryTarget,
    },
}

impl From<PlatformRecoveryAction> for ForeignPlatformRecoveryAction {
    fn from(action: PlatformRecoveryAction) -> Self {
        match action {
            PlatformRecoveryAction::WaitForRadioOn => Self::WaitForRadioOn,
            PlatformRecoveryAction::AwaitOldOperationTerminals { remaining } => {
                Self::AwaitOldOperationTerminals {
                    remaining: remaining.into_iter().map(|id| id.to_string()).collect(),
                }
            }
            PlatformRecoveryAction::AttemptSameOwnerRecovery {
                after_central_recreation,
                target,
            } => Self::AttemptSameOwnerRecovery {
                after_central_recreation,
                target: target.into(),
            },
            PlatformRecoveryAction::RecreateCentral => Self::RecreateCentral,
            PlatformRecoveryAction::AttemptFreshOwnerRecovery { target } => {
                Self::AttemptFreshOwnerRecovery {
                    target: target.into(),
                }
            }
        }
    }
}

/// Sequence-echoed result returned by a native platform action.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignPlatformActionResult {
    pub sequence: u64,
    pub signal: ForeignRecoverySignal,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryPlan {
    pub execution_context: ForeignOwnerContext,
    pub sequence: u64,
    pub actions: Vec<ForeignRecoveryAction>,
}

impl From<crate::recovery::RecoveryPlan> for ForeignRecoveryPlan {
    fn from(plan: crate::recovery::RecoveryPlan) -> Self {
        let execution_context = ForeignOwnerContext::from(plan.execution_context());
        let sequence = plan.sequence();
        let actions = plan
            .into_actions()
            .into_iter()
            .map(ForeignRecoveryAction::from)
            .collect();
        Self {
            execution_context,
            sequence,
            actions,
        }
    }
}

/// Errors crossing the native factory/session FFI boundary.
#[derive(Clone, Debug, Error, uniffi::Error)]
pub enum SupervisorFfiError {
    #[error("invalid supervisor context: {detail}")]
    InvalidContext { detail: String },
    #[error("native platform session failed: {detail}")]
    Platform { detail: String },
}

impl SupervisorFfiError {
    fn from_recovery(error: impl std::fmt::Display) -> Self {
        Self::InvalidContext {
            detail: error.to_string(),
        }
    }

    pub(super) fn into_supervisor(self) -> SupervisorError {
        SupervisorError::Platform {
            detail: self.to_string(),
        }
    }

    pub(super) fn from_supervisor(error: SupervisorError) -> Self {
        Self::Platform {
            detail: error.to_string(),
        }
    }
}
