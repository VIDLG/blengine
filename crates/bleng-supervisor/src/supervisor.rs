use std::{collections::BTreeSet, sync::Arc};

use crate::recovery::{
    RecoveryAction, RecoveryContext, RecoveryObservation, RecoveryPlan, RecoveryPolicy,
    RecoveryPolicyConfig, RecoverySignal,
};
use bleng::Bleng;

use crate::{
    error::SupervisorError,
    platform::{
        PlatformOwnerSession, PlatformOwnerSessionFactory, PlatformRecoveryAction, RecoveryTarget,
        ResourceSnapshot,
    },
};

/// Validated, typed inputs used to construct one Rust recovery supervisor.
///
/// Foreign/FFI representations are converted into this value before the
/// supervisor is created. Recovery-policy inputs stay grouped under `recovery`
/// so the supervisor does not expose a second, partially flattened policy API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SupervisorConfig {
    pub recovery: RecoveryPolicyConfig,
    pub required_zero_resource_names: BTreeSet<String>,
    pub recovery_target: RecoveryTarget,
}

struct ActiveOwnerSession {
    bleng: Arc<Bleng>,
    platform: Box<dyn PlatformOwnerSession>,
}

struct OwnerRetirementEvidence {
    owner_shutdown: Result<(), SupervisorError>,
    reader_drain: Result<(), SupervisorError>,
    resources: Result<ResourceSnapshot, SupervisorError>,
}

impl OwnerRetirementEvidence {
    /// Runs every retirement barrier in the required order and retains each result.
    async fn collect(bleng: &Bleng, platform: &dyn PlatformOwnerSession) -> Self {
        // A failure in one step must not prevent the event reader from draining or the platform
        // from reporting resources. The caller decides error precedence after all steps run.
        let owner_shutdown = bleng.shutdown().await.map_err(SupervisorError::from);
        let reader_drain = platform.wait_event_reader().await;
        let resources = platform.resource_snapshot().await;
        Self {
            owner_shutdown,
            reader_drain,
            resources,
        }
    }
}

impl ActiveOwnerSession {
    /// Runs every retirement barrier and retains each result for the supervisor to classify.
    async fn shutdown(self) -> OwnerRetirementEvidence {
        OwnerRetirementEvidence::collect(&self.bleng, self.platform.as_ref()).await
    }
}

enum OwnerLifecycle {
    NotStarted,
    /// The old owner is retired while recovery is waiting for a fresh owner.
    Vacant,
    Active(ActiveOwnerSession),
    /// The supervisor was permanently shut down and cannot accept more work.
    Closed,
}

impl OwnerLifecycle {
    fn require_initial_start(&self) -> Result<(), SupervisorError> {
        match self {
            Self::NotStarted => Ok(()),
            Self::Active(_) => Err(SupervisorError::AlreadyActive),
            Self::Vacant | Self::Closed => Err(SupervisorError::AlreadyStarted),
        }
    }

    fn require_vacant(&self) -> Result<(), SupervisorError> {
        match self {
            Self::Vacant => Ok(()),
            Self::Active(_) => Err(SupervisorError::AlreadyActive),
            Self::NotStarted | Self::Closed => Err(SupervisorError::NoActiveSession),
        }
    }

    /// Observations are allowed after initial startup has begun, including while the
    /// old owner is vacant during the owner-retirement transition.
    fn require_started(&self) -> Result<(), SupervisorError> {
        if matches!(self, Self::NotStarted | Self::Closed) {
            return Err(SupervisorError::NoActiveSession);
        }
        Ok(())
    }

    fn active_context(&self) -> Option<&RecoveryContext> {
        match self {
            Self::Active(active) => Some(active.platform.context()),
            Self::NotStarted | Self::Vacant | Self::Closed => None,
        }
    }

    fn require_active(&self) -> Result<&ActiveOwnerSession, SupervisorError> {
        match self {
            Self::Active(active) => Ok(active),
            Self::NotStarted | Self::Vacant | Self::Closed => Err(SupervisorError::NoActiveSession),
        }
    }

    fn take_active_for_recovery(&mut self) -> Result<ActiveOwnerSession, SupervisorError> {
        match std::mem::replace(self, Self::Vacant) {
            Self::Active(active) => Ok(active),
            Self::NotStarted | Self::Vacant | Self::Closed => Err(SupervisorError::NoActiveSession),
        }
    }

    fn take_active_for_shutdown(&mut self) -> Result<ActiveOwnerSession, SupervisorError> {
        match std::mem::replace(self, Self::Closed) {
            Self::Active(active) => Ok(active),
            Self::NotStarted | Self::Vacant | Self::Closed => Err(SupervisorError::NoActiveSession),
        }
    }

    fn is_active(&self) -> bool {
        matches!(self, Self::Active(_))
    }
}

/// Coordinates policy and one active BLeng/platform owner session.
///
/// The core executes the owner-boundary actions whose ordering is independent of platform BLE:
/// initial assembly, recovery retirement, fresh-owner assembly, and resource-zero validation.
/// `execute_platform_action` drives one platform action at a time and feeds its context-bound
/// signal back into the policy; platform code never performs owner replacement.
pub struct SessionSupervisor {
    factory: Arc<dyn PlatformOwnerSessionFactory>,
    policy: RecoveryPolicy,
    required_zero_resource_names: BTreeSet<String>,
    owner_lifecycle: OwnerLifecycle,
    recovery_target: RecoveryTarget,
    // The latest policy plan authorizes one action that has not been dispatched.
    pending_plan: Option<RecoveryPlan>,
}

impl SessionSupervisor {
    /// Creates a supervisor around an initial, still-active recovery policy.
    /// No native owner is assembled until [`Self::start`] succeeds.
    pub fn new(
        factory: Arc<dyn PlatformOwnerSessionFactory>,
        config: SupervisorConfig,
    ) -> Result<Self, SupervisorError> {
        let SupervisorConfig {
            recovery,
            required_zero_resource_names,
            recovery_target,
        } = config;
        let policy = RecoveryPolicy::new(recovery);
        let recovery_target = recovery_target.canonicalized()?;
        Ok(Self {
            factory,
            policy,
            required_zero_resource_names,
            owner_lifecycle: OwnerLifecycle::NotStarted,
            pending_plan: None,
            recovery_target,
        })
    }

    pub fn policy(&self) -> &RecoveryPolicy {
        &self.policy
    }

    // These are pure lifecycle queries. The policy context and the actually assembled owner
    // context are intentionally not interchangeable during fresh-owner creation.
    delegate::delegate! {
        to self.owner_lifecycle {
            /// Returns the context of the actually assembled native owner, if one exists.
            pub fn active_context(&self) -> Option<&RecoveryContext>;

            pub fn is_active(&self) -> bool;
        }
    }

    /// Applies one fenced observation without executing its returned actions.
    ///
    /// The returned plan is also retained as the only authorization for the
    /// owner-boundary actions that it contains. A later observation supersedes
    /// this plan, so an older plan cannot retire or replace a newer owner.
    pub fn observe(
        &mut self,
        observation: RecoveryObservation,
    ) -> Result<RecoveryPlan, SupervisorError> {
        self.owner_lifecycle.require_started()?;
        let plan = self.policy.apply(observation)?;
        self.pending_plan = Some(plan.clone());
        Ok(plan)
    }

    /// Creates and assembles the initial owner for the policy's current context.
    pub async fn start(&mut self) -> Result<(), SupervisorError> {
        // Initial startup is one-shot: OwnerLifecycle distinguishes an active owner from
        // an already-retired initial owner.
        self.owner_lifecycle.require_initial_start()?;

        // RecoveryPolicy starts in Active and cannot advance before an initial owner exists:
        // observe() rejects NotStarted. The lifecycle guards above are therefore the complete
        // startup admission check at this boundary.
        let context = self.policy.active_context().clone();
        let active = self.create_session(&context).await?;
        self.owner_lifecycle = OwnerLifecycle::Active(active);
        Ok(())
    }

    /// Creates the next owner after policy has admitted the fresh-owner transition.
    ///
    /// The native side supplies the new logical identity and generation. The session is fully
    /// assembled before `FreshOwnerCreated` is applied, so a failed creation cannot advance the
    /// policy or leave a partially active owner behind.
    pub async fn create_fresh_owner(
        &mut self,
        context: RecoveryContext,
    ) -> Result<RecoveryPlan, SupervisorError> {
        self.owner_lifecycle.require_vacant()?;
        // The policy-issued action is the phase authorization. A CreateFreshOwner action can
        // only be emitted from CreatingFreshOwner, so do not duplicate that state-machine check.
        self.consume_pending_action("CreateFreshOwner", RecoveryAction::CreateFreshOwner)?;

        let previous_policy = self.policy.clone();
        let active = self.create_session(&context).await?;
        self.owner_lifecycle = OwnerLifecycle::Active(active);
        match self.observe(RecoveryObservation::new(
            context,
            RecoverySignal::FreshOwnerCreated,
        )) {
            Ok(plan) => Ok(plan),
            Err(primary) => {
                self.policy = previous_policy;
                match self.shutdown().await {
                    Ok(_) => Err(primary),
                    Err(cleanup_error) => Err(SupervisorError::StartupCleanup {
                        primary_error: primary.to_string(),
                        cleanup_error: cleanup_error.to_string(),
                    }),
                }
            }
        }
    }

    /// Retires the active owner and advances policy to the fresh-owner creation action.
    ///
    /// Unlike ordinary shutdown, this method is part of the recovery transition and owns the
    /// `OwnerRetired` signal after resource-zero validation. Callers must not synthesize that
    /// signal separately.
    pub async fn retire_for_recovery(&mut self) -> Result<RecoveryPlan, SupervisorError> {
        self.owner_lifecycle.require_active()?;
        // The policy-issued action is the phase authorization. A RetireOldOwner action can only
        // be emitted from RecoveringSameOwner, so do not duplicate that state-machine check.
        self.consume_pending_action("RetireOldOwner", RecoveryAction::RetireOldOwner)?;
        let active = self.owner_lifecycle.take_active_for_recovery()?;
        self.shutdown_active(active).await?;
        let context = self.policy.active_context().clone();
        self.observe(RecoveryObservation::new(
            context,
            RecoverySignal::OwnerRetired {
                resources_zero: true,
            },
        ))
    }

    /// Executes the next platform action from the current plan.
    ///
    /// The platform result is immediately wrapped in the active session's
    /// context and fed back through the policy. This is the Rust-side action
    /// loop; native adapters only perform one bounded platform operation and
    /// report evidence.
    pub async fn execute_platform_action(&mut self) -> Result<RecoveryPlan, SupervisorError> {
        let (sequence, action) = self.pending_platform_action()?;
        let active = self.owner_lifecycle.require_active()?;
        let context = active.platform.context().clone();
        self.pending_plan = None;

        let result = active
            .platform
            .execute_recovery_action(sequence, action)
            .await?;
        if result.sequence != sequence {
            return Err(SupervisorError::RecoveryActionSequenceMismatch {
                expected: sequence,
                received: result.sequence,
            });
        }
        self.observe(RecoveryObservation::new(context, result.signal))
    }

    /// Shuts down the BLeng owner, then validates native resource retirement.
    pub async fn shutdown(&mut self) -> Result<ResourceSnapshot, SupervisorError> {
        let active = self.owner_lifecycle.take_active_for_shutdown()?;
        self.pending_plan = None;
        self.shutdown_active(active).await
    }

    /// Returns the platform command for the plan's first ordered action.
    ///
    /// A plan is not a bag of capabilities: later actions must not become
    /// executable merely because an earlier owner-bound action was skipped.
    fn pending_platform_action(&self) -> Result<(u64, PlatformRecoveryAction), SupervisorError> {
        let plan = self.require_pending_plan("platform recovery action")?;
        let Some(action) = plan.next_action() else {
            return Err(SupervisorError::RecoveryActionNotPlatform {
                action: "owner or host recovery action",
            });
        };
        let platform_action =
            PlatformRecoveryAction::from_recovery_action(action, &self.recovery_target)?;
        Ok((plan.sequence(), platform_action))
    }

    fn consume_pending_action(
        &mut self,
        action_name: &'static str,
        expected: RecoveryAction,
    ) -> Result<(), SupervisorError> {
        if !self
            .require_pending_plan(action_name)?
            .authorizes_next_action(&expected)
        {
            return Err(SupervisorError::RecoveryActionNotPending {
                action: action_name,
            });
        }
        self.pending_plan = None;
        Ok(())
    }

    fn require_pending_plan(
        &self,
        action_name: &'static str,
    ) -> Result<&RecoveryPlan, SupervisorError> {
        self.pending_plan
            .as_ref()
            .ok_or(SupervisorError::RecoveryActionNotPending {
                action: action_name,
            })
    }

    async fn create_session(
        &self,
        context: &RecoveryContext,
    ) -> Result<ActiveOwnerSession, SupervisorError> {
        let platform = self.factory.create(context)?;
        if platform.context() != context {
            let primary_error = SupervisorError::PlatformContextMismatch;
            return Err(Self::cleanup_before_backend_transfer(
                platform,
                primary_error,
            ));
        }

        let owner = match Bleng::new() {
            Ok(owner) => owner,
            Err(primary_error) => {
                return Err(Self::cleanup_before_backend_transfer(
                    platform,
                    primary_error.into(),
                ));
            }
        };
        let backend = platform.backend();

        // After this call begins, Bleng/BleHub owns backend disposal on both
        // success and assembly failure. Do not call dispose_unassembled here.
        if let Err(primary_error) = owner.assemble_backend(backend).await {
            return Err(self
                .cleanup_after_backend_transfer(platform.as_ref(), owner, primary_error.into())
                .await);
        }
        if let Err(primary_error) = platform.start_event_reader(Arc::clone(&owner)) {
            return Err(self
                .cleanup_after_backend_transfer(platform.as_ref(), owner, primary_error)
                .await);
        }

        Ok(ActiveOwnerSession {
            bleng: owner,
            platform,
        })
    }

    async fn shutdown_active(
        &self,
        active: ActiveOwnerSession,
    ) -> Result<ResourceSnapshot, SupervisorError> {
        let evidence = active.shutdown().await;
        let mut failures = Vec::new();

        if let Err(error) = evidence.owner_shutdown {
            failures.push(("owner shutdown", error));
        }
        if let Err(error) = evidence.reader_drain {
            failures.push(("event reader drain", error));
        }

        let snapshot = match evidence.resources {
            Ok(snapshot) => Some(snapshot),
            Err(error) => {
                failures.push(("resource snapshot", error));
                None
            }
        };
        if let Some(snapshot) = &snapshot {
            if let Err(error) = self.validate_resources(snapshot) {
                failures.push(("resource validation", error));
            }
        }

        if failures.is_empty() {
            return Ok(snapshot.expect("successful retirement must produce a resource snapshot"));
        }

        let (primary_stage, primary_error) = failures.remove(0);
        if failures.is_empty() {
            return Err(primary_error);
        }
        let additional_failures = failures
            .into_iter()
            .map(|(stage, error)| format!("{stage}: {error}"))
            .collect::<Vec<_>>()
            .join("; ");
        Err(SupervisorError::RetirementCleanup {
            primary_error: format!("{primary_stage}: {primary_error}"),
            additional_failures,
        })
    }

    /// Cleans up after backend ownership has transferred to `Bleng`.
    ///
    /// The backend must be retired through the full shutdown barrier; calling
    /// `dispose_unassembled` here would risk double disposal.
    async fn cleanup_after_backend_transfer(
        &self,
        platform: &dyn PlatformOwnerSession,
        owner: Arc<Bleng>,
        primary_error: SupervisorError,
    ) -> SupervisorError {
        let evidence = OwnerRetirementEvidence::collect(&owner, platform).await;
        let owner_cleanup = evidence.owner_shutdown;
        let reader_cleanup = evidence.reader_drain;
        let snapshot_cleanup = evidence
            .resources
            .and_then(|snapshot| self.validate_resources(&snapshot));
        match (owner_cleanup, reader_cleanup, snapshot_cleanup) {
            (Ok(()), Ok(()), Ok(())) => primary_error,
            (owner, reader, snapshot) => SupervisorError::StartupCleanup {
                primary_error: primary_error.to_string(),
                cleanup_error: format!(
                    "owner_shutdown={owner:?}, event_reader_drain={reader:?}, resource_snapshot_or_validation={snapshot:?}"
                ),
            },
        }
    }

    /// Cleans up a platform session before backend ownership transfers to `Bleng`.
    ///
    /// At this point `Bleng` does not own the backend, so the platform's
    /// unassembled disposal path is the only safe cleanup operation.
    fn cleanup_before_backend_transfer(
        platform: Box<dyn PlatformOwnerSession>,
        primary_error: SupervisorError,
    ) -> SupervisorError {
        match platform.dispose_unassembled() {
            Ok(()) => primary_error,
            Err(cleanup_error) => SupervisorError::StartupCleanup {
                primary_error: primary_error.to_string(),
                cleanup_error: cleanup_error.to_string(),
            },
        }
    }

    fn validate_resources(&self, snapshot: &ResourceSnapshot) -> Result<(), SupervisorError> {
        for resource in &self.required_zero_resource_names {
            if !snapshot.contains_key(resource) {
                return Err(SupervisorError::MissingResource {
                    resource: resource.clone(),
                });
            }
        }
        if snapshot.values().any(|count| *count != 0) {
            return Err(SupervisorError::NativeResourcesNotZero {
                snapshot: snapshot.clone(),
            });
        }
        Ok(())
    }
}
