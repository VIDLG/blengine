use blehub::{AdapterSnapshot, BluetoothUuid, NotificationModeRequest, ScanOptions};
use bleng::BlengError;
use thiserror::Error;

use crate::recovery::{
    OwnerBackendIdentity, OwnerGeneration, RecoveryAction, RecoveryAttemptId, RecoveryContext,
    RecoveryLink, RecoveryMode, RecoveryObservation, RecoveryOperationId, RecoveryOutcome,
    RecoveryPolicyConfig, RecoverySignal,
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

/// Evidence returned after the initial Rust-owned owner has been assembled and initialized.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOwnerStart {
    pub context: ForeignOwnerContext,
    pub initialization_command_id: u64,
    pub adapter_snapshot: AdapterSnapshot,
}

impl From<crate::OwnerStart> for ForeignOwnerStart {
    fn from(start: crate::OwnerStart) -> Self {
        Self {
            context: ForeignOwnerContext::from(&start.context),
            initialization_command_id: start.initialization_command_id.value,
            adapter_snapshot: start.adapter_snapshot,
        }
    }
}

/// Terminal owner state and cleanup outcome. Closed does not imply resource-zero.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOwnerClose {
    pub owner_closed: bool,
    pub failure: Option<ForeignOwnerCloseFailure>,
}

/// Identity-free product failure; both strings are fixed by Rust's error taxonomy.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignOwnerCloseFailure {
    pub code: String,
    pub message: String,
}

impl ForeignOwnerClose {
    pub(super) fn busy() -> Self {
        Self::not_closed(
            "supervisor_busy",
            "supervisor operation is already in progress",
        )
    }

    pub(super) fn unavailable() -> Self {
        Self::not_closed("supervisor_unavailable", "supervisor is unavailable")
    }

    fn not_closed(code: &str, message: &str) -> Self {
        Self {
            owner_closed: false,
            failure: Some(ForeignOwnerCloseFailure::new(code, message)),
        }
    }
}

impl ForeignOwnerCloseFailure {
    fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.to_owned(),
            message: message.to_owned(),
        }
    }

    pub(super) fn from_supervisor(error: &SupervisorError) -> Self {
        let (code, message) = match error {
            SupervisorError::AlreadyActive => {
                ("already_active", "supervisor already has an active session")
            }
            SupervisorError::AlreadyStarted => (
                "already_started",
                "supervisor has already started its initial session",
            ),
            SupervisorError::NoActiveSession => {
                ("no_active_session", "supervisor has no active session")
            }
            SupervisorError::Platform { .. } => {
                ("platform_failure", "platform session operation failed")
            }
            SupervisorError::Bleng(_) => ("bleng_failure", "BLeng operation failed"),
            SupervisorError::Recovery(_) => (
                "recovery_failure",
                "recovery policy rejected the observation",
            ),
            SupervisorError::RecoveryConfiguration(_) => (
                "recovery_configuration",
                "recovery barrier configuration rejected",
            ),
            SupervisorError::PlatformContextMismatch => (
                "platform_context_mismatch",
                "platform session context does not match the requested context",
            ),
            SupervisorError::RequiredResourcesNotConfigured => (
                "required_resources_not_configured",
                "supervisor must configure at least one required zero resource",
            ),
            SupervisorError::BlankRequiredResourceName => (
                "blank_required_resource_name",
                "required zero resource name must not be blank",
            ),
            SupervisorError::MissingResource { .. } => (
                "missing_resource",
                "required resource is missing from the retirement snapshot",
            ),
            SupervisorError::NativeResourcesNotZero { .. } => (
                "native_resources_not_zero",
                "native resources remained after retirement",
            ),
            SupervisorError::StartupCleanup { .. } => (
                "startup_cleanup_failed",
                "session startup and cleanup failed",
            ),
            SupervisorError::RetirementCleanup { .. } => (
                "retirement_cleanup_failed",
                "owner retirement had multiple failures",
            ),
            SupervisorError::RecoveryActionNotPending { .. } => (
                "recovery_action_not_pending",
                "recovery action is not pending",
            ),
            SupervisorError::RecoveryActionNotPlatform { .. } => (
                "recovery_action_not_platform",
                "recovery action is not executable by the platform session",
            ),
            SupervisorError::RecoveryTargetMissing { .. } => {
                ("recovery_target_missing", "recovery target is required")
            }
            SupervisorError::InvalidRecoveryTarget(_) => {
                ("invalid_recovery_target", "recovery target is invalid")
            }
            SupervisorError::RecoveryActionSequenceMismatch { .. } => (
                "recovery_action_sequence_mismatch",
                "recovery action sequence does not match",
            ),
        };
        Self::new(code, message)
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

impl From<RecoveryLink> for ForeignRecoveryLink {
    fn from(link: RecoveryLink) -> Self {
        Self {
            connection_id: link.connection_id().as_str().to_owned(),
            gatt_session_id: link.gatt_session_id().as_str().to_owned(),
            subscription_id: link.subscription_id().as_str().to_owned(),
        }
    }
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
    pub recovery_target: Option<ForeignRecoveryTarget>,
}

impl TryFrom<ForeignSupervisorConfig> for SupervisorConfig {
    type Error = SupervisorFfiError;

    fn try_from(config: ForeignSupervisorConfig) -> Result<Self, Self::Error> {
        let ForeignSupervisorConfig {
            recovery,
            recovery_target,
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
            recovery_target: recovery_target.map(RecoveryTarget::from),
        })
    }
}

/// External signals that a native host may submit to start or explicitly stop recovery.
/// Platform-generated progress must return through `ForeignPlatformActionResult` instead.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignExternalRecoverySignal {
    RadioLost,
    ManualReconnectRequired,
}

impl From<ForeignExternalRecoverySignal> for RecoverySignal {
    fn from(signal: ForeignExternalRecoverySignal) -> Self {
        match signal {
            ForeignExternalRecoverySignal::RadioLost => Self::RadioLost,
            ForeignExternalRecoverySignal::ManualReconnectRequired => {
                Self::ManualReconnectRequired {
                    reason: "manual reconnect requested by host".to_owned(),
                }
            }
        }
    }
}

/// FFI representation of a platform-observed recovery signal.
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

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignRecoveryMode {
    SameOwner,
    FreshOwner,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignRecoveryOutcome {
    Recovered {
        context: ForeignOwnerContext,
        mode: ForeignRecoveryMode,
        link: ForeignRecoveryLink,
    },
    PlatformTimeout {
        context: ForeignOwnerContext,
        reason: String,
    },
    ManualReconnectRequired {
        context: ForeignOwnerContext,
        reason: String,
    },
}

impl From<RecoveryOutcome> for ForeignRecoveryOutcome {
    fn from(outcome: RecoveryOutcome) -> Self {
        match outcome {
            RecoveryOutcome::Recovered {
                context,
                mode,
                link,
            } => Self::Recovered {
                context: ForeignOwnerContext::from(&context),
                mode: match mode {
                    RecoveryMode::SameOwner => ForeignRecoveryMode::SameOwner,
                    RecoveryMode::FreshOwner => ForeignRecoveryMode::FreshOwner,
                },
                link: link.into(),
            },
            RecoveryOutcome::PlatformTimeout { context, reason } => Self::PlatformTimeout {
                context: ForeignOwnerContext::from(&context),
                reason,
            },
            RecoveryOutcome::ManualReconnectRequired { context, reason } => {
                Self::ManualReconnectRequired {
                    context: ForeignOwnerContext::from(&context),
                    reason,
                }
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignRecoveryPlan {
    pub execution_context: ForeignOwnerContext,
    pub sequence: u64,
    pub actions: Vec<ForeignRecoveryAction>,
    pub outcome: Option<ForeignRecoveryOutcome>,
}

impl From<crate::recovery::RecoveryPlan> for ForeignRecoveryPlan {
    fn from(plan: crate::recovery::RecoveryPlan) -> Self {
        let execution_context = ForeignOwnerContext::from(plan.execution_context());
        let sequence = plan.sequence();
        let outcome = plan
            .outcome()
            .as_ref()
            .cloned()
            .map(ForeignRecoveryOutcome::from);
        let actions = plan
            .into_actions()
            .into_iter()
            .map(ForeignRecoveryAction::from)
            .collect();
        Self {
            execution_context,
            sequence,
            actions,
            outcome,
        }
    }
}

/// Errors crossing the native factory/session FFI boundary.
#[derive(Clone, Debug, Error, uniffi::Error)]
pub enum SupervisorFfiError {
    #[error("invalid supervisor context: {detail}")]
    InvalidContext { detail: String },
    #[error("invalid supervisor configuration: {detail}")]
    InvalidConfiguration { detail: String },
    #[error("supervisor has no active owner context")]
    NoActiveContext,
    /// Another lifecycle operation currently holds the supervisor. This is not a failure of the
    /// requested work: nothing was dispatched, and retrying after it finishes is the answer.
    #[error("supervisor operation is already in progress")]
    Busy,
    /// The bounded owner-assembly/initialization wait expired. Native work may still be running;
    /// this is a caller-visible classification, not proof that the platform stopped.
    #[error("supervisor operation timed out: {detail}")]
    Timeout { detail: String },
    #[error("native platform session failed: {detail}")]
    Platform { detail: String },
}

impl SupervisorFfiError {
    fn from_recovery(error: impl std::fmt::Display) -> Self {
        Self::InvalidContext {
            detail: error.to_string(),
        }
    }

    pub(super) fn from_recovery_configuration(error: impl std::fmt::Display) -> Self {
        Self::InvalidConfiguration {
            detail: error.to_string(),
        }
    }

    pub(super) fn into_supervisor(self) -> SupervisorError {
        SupervisorError::Platform {
            detail: self.to_string(),
        }
    }

    pub(super) fn from_supervisor(error: SupervisorError) -> Self {
        match error {
            SupervisorError::RequiredResourcesNotConfigured => Self::InvalidConfiguration {
                detail: "supervisor must configure at least one required zero resource".to_owned(),
            },
            SupervisorError::BlankRequiredResourceName => Self::InvalidConfiguration {
                detail: "required zero resource name must not be blank".to_owned(),
            },
            SupervisorError::RecoveryConfiguration(error) => Self::InvalidConfiguration {
                detail: error.to_string(),
            },
            SupervisorError::Bleng(BlengError::Timeout) => Self::Timeout {
                detail: error.to_string(),
            },
            error => Self::Platform {
                detail: error.to_string(),
            },
        }
    }
}
