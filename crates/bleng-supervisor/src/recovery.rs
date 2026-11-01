//! Platform-independent recovery policy for a native BLE session supervisor.
//!
//! This crate deliberately owns no runtime, native handle, executor, timer, or
//! `Bleng` instance. A platform supervisor translates native observations into
//! [`RecoverySignal`] values and returns context-fenced [`RecoveryPlan`]s.
//! The policy is therefore reusable by Apple, Android, and deterministic tests.

use std::collections::BTreeSet;

use derive_more::{Display, Into};
use getset::{CopyGetters, Getters};
use thiserror::Error;

/// Monotonic owner-lifetime identity within one supervisor namespace.
///
/// This value is not globally unique by itself. Pair it with `OwnerBackendIdentity`
/// when observations may cross process or supervisor boundaries.
#[derive(Clone, Copy, Debug, Display, Into, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OwnerGeneration(u64);

impl OwnerGeneration {
    pub fn new(value: u64) -> Result<Self, RecoveryError> {
        if value == 0 {
            return Err(RecoveryError::InvalidOwnerGeneration);
        }
        Ok(Self(value))
    }

    pub fn value(self) -> u64 {
        self.0
    }
}

/// Identity of one bounded recovery workflow that may span owner generations.
///
/// It is scoped by the recovery policy and is preserved when that policy moves
/// to a fresh owner during the same recovery workflow.
#[derive(Clone, Copy, Debug, Display, Into, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RecoveryAttemptId(u64);

impl RecoveryAttemptId {
    pub fn new(value: u64) -> Result<Self, RecoveryError> {
        if value == 0 {
            return Err(RecoveryError::InvalidRecoveryAttemptId);
        }
        Ok(Self(value))
    }

    pub fn value(self) -> u64 {
        self.0
    }
}

macro_rules! define_recovery_id {
    (
        $(#[$meta:meta])*
        $name:ident,
        $error:expr $(,)?
    ) => {
        $(#[$meta])*
        #[derive(Clone, Debug, Display, Into, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(String);

        impl $name {
            pub fn new(value: impl Into<String>) -> Result<Self, RecoveryError> {
                let value = value.into();
                if value.is_empty() {
                    return Err($error);
                }
                Ok(Self(value))
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
    };
}

define_recovery_id! {
    /// Identity of the logical owner lifetime that hosts a backend.
    OwnerId,
    RecoveryError::InvalidIdentityField("owner_id")
}

define_recovery_id! {
    /// Identity of one native backend instance.
    BackendId,
    RecoveryError::InvalidIdentityField("backend_id")
}

define_recovery_id! {
    /// Opaque connection identity used by the recovery policy.
    RecoveryConnectionId,
    RecoveryError::InvalidLinkField("connection_id")
}

define_recovery_id! {
    /// Opaque GATT-session identity used by the recovery policy.
    RecoveryGattSessionId,
    RecoveryError::InvalidLinkField("gatt_session_id")
}

define_recovery_id! {
    /// Opaque subscription identity used by the recovery policy.
    RecoverySubscriptionId,
    RecoveryError::InvalidLinkField("subscription_id")
}

define_recovery_id! {
    /// Identity of one pre-loss operation in the recovery terminal barrier.
    RecoveryOperationId,
    RecoveryError::EmptyOperationId
}

/// Stable identity of one logical owner/backend lifetime.
#[derive(Clone, Debug, Getters, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct OwnerBackendIdentity {
    #[get = "pub"]
    owner_id: OwnerId,
    #[get = "pub"]
    backend_id: BackendId,
}

impl OwnerBackendIdentity {
    pub fn new(
        owner_id: impl Into<String>,
        backend_id: impl Into<String>,
    ) -> Result<Self, RecoveryError> {
        Ok(Self {
            owner_id: OwnerId::new(owner_id)?,
            backend_id: BackendId::new(backend_id)?,
        })
    }

    /// Returns whether either native lifetime component was reused.
    ///
    /// A fresh owner must not retain the old owner or backend identity. The
    /// complete-identity equality case is already covered by either comparison
    /// below, so this intentionally uses only the two component checks.
    fn reuses_owner_or_backend_from(&self, previous: &Self) -> bool {
        self.owner_id() == previous.owner_id() || self.backend_id() == previous.backend_id()
    }
}

/// Provenance and fencing context for one owner, generation, and recovery attempt.
///
/// The context is captured when native work is created and carried with its
/// observation or execution plan. It is not a replacement for the policy's
/// mutable phase state.
#[derive(Clone, CopyGetters, Debug, Getters, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RecoveryContext {
    #[get = "pub"]
    owner_backend_identity: OwnerBackendIdentity,
    #[get_copy = "pub"]
    owner_generation: OwnerGeneration,
    #[get_copy = "pub"]
    recovery_attempt: RecoveryAttemptId,
}

impl RecoveryContext {
    pub fn new(
        owner_backend_identity: OwnerBackendIdentity,
        owner_generation: OwnerGeneration,
        recovery_attempt: RecoveryAttemptId,
    ) -> Self {
        Self {
            owner_backend_identity,
            owner_generation,
            recovery_attempt,
        }
    }

    /// Creates the next-owner context while preserving this recovery attempt.
    pub fn for_fresh_owner(
        &self,
        owner_backend_identity: OwnerBackendIdentity,
        owner_generation: OwnerGeneration,
    ) -> Self {
        Self {
            owner_backend_identity,
            owner_generation,
            recovery_attempt: self.recovery_attempt,
        }
    }

    fn reuses_owner_or_backend_from(&self, previous: &OwnerBackendIdentity) -> bool {
        self.owner_backend_identity()
            .reuses_owner_or_backend_from(previous)
    }

    fn is_valid_fresh_owner_after(
        &self,
        previous: &Self,
        old_identity: &OwnerBackendIdentity,
    ) -> bool {
        self.recovery_attempt() == previous.recovery_attempt()
            && self.owner_generation() > previous.owner_generation()
            && !self.reuses_owner_or_backend_from(old_identity)
    }
}

/// Native BLE identities belonging to one connected GATT session.
///
/// These are recovery-local opaque identities. Native adapters may derive them
/// from BleHub's numeric IDs, platform handles, or a composite generation, but
/// the policy does not depend on any one of those representations.
#[derive(Clone, Debug, Getters, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RecoveryLink {
    #[get = "pub"]
    connection_id: RecoveryConnectionId,
    #[get = "pub"]
    gatt_session_id: RecoveryGattSessionId,
    #[get = "pub"]
    subscription_id: RecoverySubscriptionId,
}

impl RecoveryLink {
    pub fn new(
        connection_id: impl Into<String>,
        gatt_session_id: impl Into<String>,
        subscription_id: impl Into<String>,
    ) -> Result<Self, RecoveryError> {
        Ok(Self {
            connection_id: RecoveryConnectionId::new(connection_id)?,
            gatt_session_id: RecoveryGattSessionId::new(gatt_session_id)?,
            subscription_id: RecoverySubscriptionId::new(subscription_id)?,
        })
    }
}

/// Which owner lifetime produced the recovered link.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryMode {
    SameOwner,
    FreshOwner,
}

/// Common cross-platform recovery phases.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecoveryPhase {
    Active,
    RadioLost,
    WaitingForRadioOn,
    RecoveringSameOwner,
    CreatingFreshOwner,
    RecoveringFreshOwner,
    Recovered,
    PlatformTimeout,
    ManualReconnectRequired,
}

/// Terminal result of a recovery attempt.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecoveryOutcome {
    Recovered {
        context: RecoveryContext,
        mode: RecoveryMode,
        link: RecoveryLink,
    },
    PlatformTimeout {
        context: RecoveryContext,
        reason: String,
    },
    ManualReconnectRequired {
        context: RecoveryContext,
        reason: String,
    },
}

/// Complete construction input for one recovery policy.
///
/// It contains both the initial recovery state and the fallback options. The
/// supervisor owns the policy after construction; callers should not assemble
/// a policy from separate positional arguments.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecoveryPolicyConfig {
    pub initial_context: RecoveryContext,
    pub old_link: Option<RecoveryLink>,
    pub expected_terminals: BTreeSet<RecoveryOperationId>,
    pub allow_central_recreation: bool,
    pub allow_fresh_owner: bool,
}

/// Signals observed by the native supervisor or supplied by a deterministic test.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecoverySignal {
    /// The platform has reported the loss. This signal is accepted once.
    RadioLost,
    /// One operation from the pre-loss terminal barrier has reached its terminal state.
    OldOperationTerminal { operation_id: RecoveryOperationId },
    /// The platform has reported that the radio is usable again. This does not
    /// bypass the old-operation terminal barrier.
    RadioOn,
    /// The current owner/backend found a new link after the initial recovery attempt.
    SameOwnerRecovered { link: RecoveryLink },
    /// The initial same-owner recovery attempt did not produce a usable link.
    SameOwnerRecoveryFailed { reason: String },
    /// The supervisor completed its single Central recreation fallback.
    CentralRecreated,
    /// The Central recreation fallback could not be completed.
    CentralRecreationFailed { reason: String },
    /// The old owner/backend is fully retired and all native resources are zero.
    OwnerRetired { resources_zero: bool },
    /// A new owner/backend has been created after old-owner retirement.
    FreshOwnerCreated,
    /// The fresh owner/backend found a new link.
    FreshOwnerRecovered { link: RecoveryLink },
    /// Fresh-owner recovery cannot produce a link.
    FreshOwnerRecoveryFailed { reason: String },
    /// The platform-specific bounded recovery budget expired.
    PlatformTimedOut { reason: String },
    /// The supervisor or host requires explicit user/application reconnect.
    ManualReconnectRequired { reason: String },
}

/// A recovery signal together with the source context that produced it.
///
/// This is a policy observation envelope, not a raw BLE event. A native
/// supervisor must bind `source_context` when it creates the callback or
/// operation; it must not read the current context later and use that to
/// relabel a late callback from an old owner.
#[derive(Clone, Debug, Getters, PartialEq, Eq)]
pub struct RecoveryObservation {
    #[get = "pub"]
    source_context: RecoveryContext,
    #[get = "pub"]
    signal: RecoverySignal,
}

impl RecoveryObservation {
    pub fn new(source_context: RecoveryContext, signal: RecoverySignal) -> Self {
        Self {
            source_context,
            signal,
        }
    }
}

/// Work that a native supervisor may perform next.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecoveryAction {
    WaitForRadioOn,
    AwaitOldOperationTerminals {
        remaining: BTreeSet<RecoveryOperationId>,
    },
    AttemptSameOwnerRecovery {
        after_central_recreation: bool,
    },
    RecreateCentral,
    RetireOldOwner,
    CreateFreshOwner,
    AttemptFreshOwnerRecovery,
}

/// Errors returned when an observation violates the recovery protocol.
#[derive(Clone, Debug, PartialEq, Eq, Error)]
pub enum RecoveryError {
    #[error("owner generation must be non-zero")]
    InvalidOwnerGeneration,
    #[error("recovery attempt ID must be non-zero")]
    InvalidRecoveryAttemptId,
    #[error("recovery observation context does not match the active policy")]
    ObservationContextMismatch {
        expected: Box<RecoveryContext>,
        received: Box<RecoveryContext>,
    },
    #[error("invalid session identity field: {0}")]
    InvalidIdentityField(&'static str),
    #[error("invalid recovery link field: {0}")]
    InvalidLinkField(&'static str),
    #[error("old operation terminal set contains an empty operation id")]
    EmptyOperationId,
    #[error("recovery signal is not valid in phase {phase:?}: {signal}")]
    UnexpectedSignal {
        phase: RecoveryPhase,
        signal: &'static str,
    },
    #[error("duplicate old operation terminal: {0}")]
    DuplicateOperationTerminal(RecoveryOperationId),
    #[error("late old operation terminal after same-owner recovery started: {0}")]
    LateOldOperationTerminal(RecoveryOperationId),
    #[error("unknown old operation: {0}")]
    UnknownOperation(RecoveryOperationId),
    #[error("fresh owner must differ from the old owner and backend")]
    FreshOwnerReused,
    #[error("central recreation is still pending")]
    CentralRecreationPending,
    #[error("recovery link was reused")]
    LinkReused,
    #[error("old operation terminal barrier is incomplete")]
    TerminalBarrierIncomplete,
    #[error("old owner resources are not zero")]
    OwnerResourcesNotZero,

    #[error("recovery plan sequence is exhausted")]
    PlanSequenceExhausted,
    #[error("recovery has already reached a terminal outcome")]
    AlreadyTerminal,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CentralRecreationState {
    NotAttempted,
    Pending,
    Completed,
}

/// Pure recovery state machine shared by native supervisors.
#[derive(Clone, CopyGetters, Debug, Getters)]
pub struct RecoveryPolicy {
    /// Immutable identity baseline captured when this recovery policy starts.
    #[get = "pub"]
    old_identity: OwnerBackendIdentity,
    /// Context currently accepted for observations and owned by this policy.
    #[get = "pub"]
    active_context: RecoveryContext,
    allow_central_recreation: bool,
    allow_fresh_owner: bool,
    #[get = "pub"]
    expected_terminals: BTreeSet<RecoveryOperationId>,
    #[get = "pub"]
    completed_terminals: BTreeSet<RecoveryOperationId>,
    used_links: BTreeSet<RecoveryLink>,
    #[get_copy = "pub"]
    phase: RecoveryPhase,
    plan_sequence: u64,
    central_recreation: CentralRecreationState,
    outcome: Option<RecoveryOutcome>,
}

/// A batch of work produced for one exact execution context and transition.
///
/// A supervisor may execute the actions asynchronously, but it must retain and
/// validate both the execution context and sequence before dispatching each
/// action. This prevents work emitted for a retired owner, or superseded work
/// from the same owner, from being applied after the policy has advanced.
#[derive(Clone, CopyGetters, Debug, Getters, PartialEq, Eq)]
pub struct RecoveryPlan {
    #[get = "pub"]
    execution_context: RecoveryContext,
    #[get_copy = "pub"]
    sequence: u64,
    #[get = "pub"]
    actions: Vec<RecoveryAction>,
}

impl RecoveryPlan {
    fn new(
        execution_context: RecoveryContext,
        sequence: u64,
        actions: Vec<RecoveryAction>,
    ) -> Self {
        Self {
            execution_context,
            sequence,
            actions,
        }
    }

    /// Returns the next ordered action, if this plan still has work to execute.
    pub fn next_action(&self) -> Option<&RecoveryAction> {
        self.actions.first()
    }

    /// Returns whether the plan authorizes this action as its next work item.
    ///
    /// Later actions remain blocked until the next ordered action is consumed.
    pub fn authorizes_next_action(&self, expected: &RecoveryAction) -> bool {
        self.next_action() == Some(expected)
    }

    /// Consumes the plan and returns its ordered work items.
    pub fn into_actions(self) -> Vec<RecoveryAction> {
        self.actions
    }
}

impl RecoveryPolicy {
    pub fn new(config: RecoveryPolicyConfig) -> Self {
        let RecoveryPolicyConfig {
            initial_context,
            old_link,
            expected_terminals,
            allow_central_recreation,
            allow_fresh_owner,
        } = config;
        let used_links = old_link.into_iter().collect();
        Self {
            old_identity: initial_context.owner_backend_identity().clone(),
            active_context: initial_context,
            allow_central_recreation,
            allow_fresh_owner,
            expected_terminals,
            completed_terminals: BTreeSet::new(),
            used_links,
            phase: RecoveryPhase::Active,
            plan_sequence: 0,
            central_recreation: CentralRecreationState::NotAttempted,
            outcome: None,
        }
    }

    pub fn mode(&self) -> Option<RecoveryMode> {
        match self.phase {
            RecoveryPhase::RecoveringSameOwner => Some(RecoveryMode::SameOwner),
            RecoveryPhase::CreatingFreshOwner | RecoveryPhase::RecoveringFreshOwner => {
                Some(RecoveryMode::FreshOwner)
            }
            RecoveryPhase::Recovered => self.outcome.as_ref().and_then(|outcome| match outcome {
                RecoveryOutcome::Recovered { mode, .. } => Some(*mode),
                _ => None,
            }),
            _ => None,
        }
    }

    pub fn outcome(&self) -> Option<&RecoveryOutcome> {
        self.outcome.as_ref()
    }

    fn remaining_terminals(&self) -> BTreeSet<RecoveryOperationId> {
        self.expected_terminals
            .difference(&self.completed_terminals)
            .cloned()
            .collect()
    }

    /// Applies one owner/attempt-fenced observation and returns context-fenced work.
    ///
    /// The transition order is deliberately fixed:
    ///
    /// 1. reject observations after a terminal outcome;
    /// 2. validate the source context against the active owner/attempt;
    /// 3. adopt a new context only for the validated fresh-owner transition;
    /// 4. validate and apply the signal for the current phase; and
    /// 5. return the resulting actions tagged with the post-transition context.
    ///
    /// The native supervisor owns execution of the returned plan. It must keep
    /// the execution context and sequence when dispatching those actions and retire
    /// stale plans instead of applying them to a later owner or transition.
    pub fn apply(
        &mut self,
        observation: RecoveryObservation,
    ) -> Result<RecoveryPlan, RecoveryError> {
        if self.outcome.is_some() {
            return Err(RecoveryError::AlreadyTerminal);
        }
        let RecoveryObservation {
            source_context,
            signal,
        } = observation;
        let context_changed = source_context != self.active_context;
        let is_fresh_owner_created = matches!(signal, RecoverySignal::FreshOwnerCreated);
        if context_changed {
            // Keep this specific failure ahead of the generic context mismatch:
            // a fresh owner must replace both native lifetime components, so
            // reusing either the old owner or the old backend is never admissible.
            if is_fresh_owner_created
                && source_context.reuses_owner_or_backend_from(&self.old_identity)
            {
                return Err(RecoveryError::FreshOwnerReused);
            }
            // A changed source context may only be adopted at the fresh-owner
            // boundary. All other signals must remain fenced to the active
            // context; otherwise a stale or misrouted observation could relabel
            // itself as a new owner. This validation also requires the same
            // attempt and a strictly newer generation.
            if !is_fresh_owner_created
                || self.phase != RecoveryPhase::CreatingFreshOwner
                || !source_context
                    .is_valid_fresh_owner_after(&self.active_context, &self.old_identity)
            {
                return Err(RecoveryError::ObservationContextMismatch {
                    expected: Box::new(self.active_context.clone()),
                    received: Box::new(source_context),
                });
            }
        }
        let sequence = self
            .plan_sequence
            .checked_add(1)
            .ok_or(RecoveryError::PlanSequenceExhausted)?;
        if context_changed {
            self.active_context = source_context;
        }

        let actions = match signal {
            RecoverySignal::RadioLost => self.radio_lost(),
            RecoverySignal::OldOperationTerminal { operation_id } => {
                self.old_operation_terminal(operation_id)
            }
            RecoverySignal::RadioOn => self.radio_on(),
            RecoverySignal::SameOwnerRecovered { link } => self.same_owner_recovered(link),
            RecoverySignal::SameOwnerRecoveryFailed { reason } => self.same_owner_failed(reason),
            RecoverySignal::CentralRecreated => self.central_recreated(),
            RecoverySignal::CentralRecreationFailed { reason } => self.central_failed(reason),
            RecoverySignal::OwnerRetired { resources_zero } => self.owner_retired(resources_zero),
            RecoverySignal::FreshOwnerCreated => self.fresh_owner_created(),
            RecoverySignal::FreshOwnerRecovered { link } => self.fresh_owner_recovered(link),
            RecoverySignal::FreshOwnerRecoveryFailed { reason } => self.fresh_failed(reason),
            RecoverySignal::PlatformTimedOut { reason } => self.finish_terminal(
                RecoveryPhase::PlatformTimeout,
                RecoveryOutcome::PlatformTimeout {
                    context: self.active_context.clone(),
                    reason,
                },
            ),
            RecoverySignal::ManualReconnectRequired { reason } => self.finish_terminal(
                RecoveryPhase::ManualReconnectRequired,
                RecoveryOutcome::ManualReconnectRequired {
                    context: self.active_context.clone(),
                    reason,
                },
            ),
        }?;
        self.plan_sequence = sequence;
        Ok(RecoveryPlan::new(
            self.active_context.clone(),
            sequence,
            actions,
        ))
    }

    fn radio_lost(&mut self) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::Active {
            return Err(self.unexpected_signal("RadioLost"));
        }
        self.phase = RecoveryPhase::RadioLost;
        let mut actions = vec![RecoveryAction::WaitForRadioOn];
        actions.extend(self.terminal_wait_action());
        Ok(actions)
    }

    /// Records one terminal result from the pre-loss operation barrier.
    ///
    /// Operations may finish before the radio returns or while the policy is
    /// waiting for radio-on. Once same-owner recovery starts, the barrier is
    /// closed; a late terminal must fail closed instead of producing another
    /// recovery plan.
    fn old_operation_terminal(
        &mut self,
        operation_id: RecoveryOperationId,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        match self.phase {
            RecoveryPhase::RadioLost | RecoveryPhase::WaitingForRadioOn => {}
            RecoveryPhase::RecoveringSameOwner => {
                return Err(RecoveryError::LateOldOperationTerminal(operation_id));
            }
            _ => return Err(self.unexpected_signal("OldOperationTerminal")),
        }
        if !self.expected_terminals.contains(&operation_id) {
            return Err(RecoveryError::UnknownOperation(operation_id));
        }
        if !self.completed_terminals.insert(operation_id.clone()) {
            return Err(RecoveryError::DuplicateOperationTerminal(operation_id));
        }
        // Once radio is usable and every old operation is terminal, the
        // same-owner recovery attempt may start. In RadioLost, completion is
        // remembered and RadioOn performs this transition later.
        if self.phase == RecoveryPhase::WaitingForRadioOn && self.terminals_complete() {
            return Ok(self.begin_same_owner_attempt(false));
        }
        Ok(self.terminal_wait_action())
    }

    fn radio_on(&mut self) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RadioLost {
            return Err(self.unexpected_signal("RadioOn"));
        }
        self.phase = RecoveryPhase::WaitingForRadioOn;
        if self.terminals_complete() {
            Ok(self.begin_same_owner_attempt(false))
        } else {
            Ok(self.terminal_wait_action())
        }
    }

    fn same_owner_recovered(
        &mut self,
        link: RecoveryLink,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringSameOwner {
            return Err(self.unexpected_signal("SameOwnerRecovered"));
        }
        self.finish_link(RecoveryMode::SameOwner, link)
    }

    fn same_owner_failed(&mut self, reason: String) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringSameOwner {
            return Err(self.unexpected_signal("SameOwnerRecoveryFailed"));
        }
        if self.allow_central_recreation
            && self.central_recreation == CentralRecreationState::NotAttempted
        {
            self.central_recreation = CentralRecreationState::Pending;
            return Ok(vec![RecoveryAction::RecreateCentral]);
        }
        self.begin_retirement(reason)
    }

    fn central_recreated(&mut self) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringSameOwner
            || self.central_recreation != CentralRecreationState::Pending
        {
            return Err(self.unexpected_signal("CentralRecreated"));
        }
        self.central_recreation = CentralRecreationState::Completed;
        Ok(self.begin_same_owner_attempt(true))
    }

    fn central_failed(&mut self, reason: String) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringSameOwner
            || self.central_recreation != CentralRecreationState::Pending
        {
            return Err(self.unexpected_signal("CentralRecreationFailed"));
        }
        self.central_recreation = CentralRecreationState::Completed;
        self.begin_retirement(reason)
    }

    fn owner_retired(
        &mut self,
        resources_zero: bool,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringSameOwner {
            return Err(self.unexpected_signal("OwnerRetired"));
        }
        if !resources_zero {
            return Err(RecoveryError::OwnerResourcesNotZero);
        }
        if self.central_recreation == CentralRecreationState::Pending {
            return Err(RecoveryError::CentralRecreationPending);
        }
        if !self.terminals_complete() {
            return Err(RecoveryError::TerminalBarrierIncomplete);
        }
        if !self.allow_fresh_owner {
            return Err(self.unexpected_signal("OwnerRetired"));
        }
        self.phase = RecoveryPhase::CreatingFreshOwner;
        Ok(vec![RecoveryAction::CreateFreshOwner])
    }

    fn fresh_owner_created(&mut self) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::CreatingFreshOwner {
            return Err(self.unexpected_signal("FreshOwnerCreated"));
        }
        if self
            .active_context
            .reuses_owner_or_backend_from(&self.old_identity)
        {
            return Err(RecoveryError::FreshOwnerReused);
        }
        self.phase = RecoveryPhase::RecoveringFreshOwner;
        Ok(vec![RecoveryAction::AttemptFreshOwnerRecovery])
    }

    fn fresh_owner_recovered(
        &mut self,
        link: RecoveryLink,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringFreshOwner {
            return Err(self.unexpected_signal("FreshOwnerRecovered"));
        }
        self.finish_link(RecoveryMode::FreshOwner, link)
    }

    fn fresh_failed(&mut self, reason: String) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.phase != RecoveryPhase::RecoveringFreshOwner {
            return Err(self.unexpected_signal("FreshOwnerRecoveryFailed"));
        }
        self.finish_terminal(
            RecoveryPhase::ManualReconnectRequired,
            RecoveryOutcome::ManualReconnectRequired {
                context: self.active_context.clone(),
                reason,
            },
        )
    }

    /// Emits the next same-owner attempt after the radio and old-operation
    /// barriers are satisfied. Central recreation reuses this transition but
    /// marks that the attempt follows the one permitted recreation fallback.
    fn begin_same_owner_attempt(&mut self, after_central_recreation: bool) -> Vec<RecoveryAction> {
        self.phase = RecoveryPhase::RecoveringSameOwner;
        vec![RecoveryAction::AttemptSameOwnerRecovery {
            after_central_recreation,
        }]
    }

    /// Chooses the fresh-owner fallback, or terminates with a manual-reconnect
    /// outcome when owner replacement is disabled. The phase remains
    /// `RecoveringSameOwner` while the retirement action is pending.
    fn begin_retirement(&mut self, reason: String) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if self.allow_fresh_owner {
            Ok(vec![RecoveryAction::RetireOldOwner])
        } else {
            self.finish_terminal(
                RecoveryPhase::ManualReconnectRequired,
                RecoveryOutcome::ManualReconnectRequired {
                    context: self.active_context.clone(),
                    reason,
                },
            )
        }
    }

    /// Records a new link and completes the policy. The returned action list is
    /// empty because the link itself is the terminal recovery result.
    fn finish_link(
        &mut self,
        mode: RecoveryMode,
        link: RecoveryLink,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        if !self.used_links.insert(link.clone()) {
            return Err(RecoveryError::LinkReused);
        }
        self.finish_terminal(
            RecoveryPhase::Recovered,
            RecoveryOutcome::Recovered {
                context: self.active_context.clone(),
                mode,
                link,
            },
        )
    }

    /// Stores a terminal outcome. Terminal plans intentionally contain no
    /// executable actions; callers consume the outcome instead.
    fn finish_terminal(
        &mut self,
        phase: RecoveryPhase,
        outcome: RecoveryOutcome,
    ) -> Result<Vec<RecoveryAction>, RecoveryError> {
        self.phase = phase;
        self.outcome = Some(outcome);
        Ok(Vec::new())
    }

    fn terminals_complete(&self) -> bool {
        self.expected_terminals == self.completed_terminals
    }

    /// Returns the barrier action only while old operations remain pending.
    /// An empty result means the barrier is already complete; the caller decides
    /// whether the next phase may start immediately.
    fn terminal_wait_action(&self) -> Vec<RecoveryAction> {
        if self.terminals_complete() {
            Vec::new()
        } else {
            vec![RecoveryAction::AwaitOldOperationTerminals {
                remaining: self.remaining_terminals(),
            }]
        }
    }

    /// Builds the protocol error for a signal that is invalid in the current phase.
    fn unexpected_signal(&self, signal: &'static str) -> RecoveryError {
        RecoveryError::UnexpectedSignal {
            phase: self.phase,
            signal,
        }
    }
}
