use std::{collections::BTreeSet, sync::Arc};

use blehub::{
    AdapterSnapshot, BluetoothUuid, BondState, CommandId, ConnectionId, ConnectionPriority,
    DeviceQueryKind, GattCharacteristicId, GattDescriptorId, GattSessionId,
    NotificationModeRequest, PeripheralId, ScanOptions, ScanSessionId, SubscriptionId, WriteType,
};

use crate::recovery::{
    RecoveryAction, RecoveryContext, RecoveryLink, RecoveryObservation, RecoveryOperationId,
    RecoveryPhase, RecoveryPlan, RecoveryPolicy, RecoveryPolicyConfig, RecoverySignal,
};
use bleng::{
    AssociateCompanionDeviceOperation, Bleng, BondOperation, DisableNotificationsOperation,
    DisconnectOperation, DiscoverGattOperation, EnableNotificationsOperation, MtuOperation,
    QueryDevicesOperation, ReadDescriptorOperation, ReadOperation, ReadRssiOperation,
    RefreshGattCacheOperation, SetAdapterEnabledOperation, SetConnectionPriorityOperation,
    StartScanOperation, StopScanOperation, WriteDescriptorOperation, WriteOperation,
};

use crate::{
    AdapterHandoff, AdapterSubmissionError,
    bond_progress::{BondHandoff, BondSubmissionError},
    companion_presence::{CompanionPresenceHandoff, CompanionPresenceSubmissionError},
    connection_delivery::{ConnectionHandoff, ConnectionSubmissionError},
    error::{SupervisorError, SupervisorOperationError},
    notification_delivery::{NotificationHandoff, NotificationSource, NotificationSubmissionError},
    observation::ObservationProjectionLease,
    platform::{
        PlatformOwnerSession, PlatformOwnerSessionFactory, PlatformRecoveryAction, RecoveryTarget,
        ResourceSnapshot,
    },
    scan_delivery::{ScanHandoff, ScanSubmissionError},
};

/// Validated, typed inputs used to construct one Rust recovery supervisor.
///
/// Foreign/FFI representations are converted into this value before the
/// supervisor is created. Recovery-policy inputs stay grouped under `recovery`
/// so the supervisor does not expose a second, partially flattened policy API.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SupervisorConfig {
    pub recovery: RecoveryPolicyConfig,
    pub recovery_target: Option<RecoveryTarget>,
}

const INITIALIZE_TIMEOUT_MS: u64 = 30_000;

struct ActiveOwnerSession {
    // Drop the publication capability before the owner and platform, including on unwinding.
    observation_projection_lease: ObservationProjectionLease,
    bleng: Arc<Bleng>,
    platform: Box<dyn PlatformOwnerSession>,
    initialization_command_id: CommandId,
    adapter_snapshot: AdapterSnapshot,
}

/// Evidence returned when the initial owner has been fully assembled and initialized.
///
/// The initialization command ID and adapter snapshot come from the same Rust-owned operation;
/// native hosts must consume this value instead of reconstructing startup identity from callbacks.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnerStart {
    pub context: RecoveryContext,
    pub initialization_command_id: CommandId,
    pub adapter_snapshot: AdapterSnapshot,
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
        self.observation_projection_lease.revoke();
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

    fn active_start(&self) -> Option<OwnerStart> {
        let Self::Active(active) = self else {
            return None;
        };
        Some(OwnerStart {
            context: active.platform.context().clone(),
            initialization_command_id: active.initialization_command_id,
            adapter_snapshot: active.adapter_snapshot.clone(),
        })
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
    recovery_target: Option<RecoveryTarget>,
    // The latest policy plan authorizes an action that has not been dispatched.
    pending_plan: Option<RecoveryPlan>,
    last_retired_resources: Option<ResourceSnapshot>,
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
            recovery_target,
        } = config;
        let policy = RecoveryPolicy::new(recovery);
        let required_zero_resource_names = factory.required_zero_resource_names();
        if required_zero_resource_names.is_empty() {
            return Err(SupervisorError::RequiredResourcesNotConfigured);
        }
        if required_zero_resource_names
            .iter()
            .any(|name| name.trim().is_empty())
        {
            return Err(SupervisorError::BlankRequiredResourceName);
        }
        let recovery_target = recovery_target
            .map(|target| target.canonicalized())
            .transpose()?;
        Ok(Self {
            factory,
            policy,
            required_zero_resource_names,
            owner_lifecycle: OwnerLifecycle::NotStarted,
            pending_plan: None,
            last_retired_resources: None,
            recovery_target,
        })
    }

    pub fn policy(&self) -> &RecoveryPolicy {
        &self.policy
    }

    /// Returns startup evidence for the currently assembled owner, if one exists.
    pub fn active_owner_start(&self) -> Option<OwnerStart> {
        self.owner_lifecycle.active_start()
    }

    /// Whether shutdown permanently closed this supervisor, independent of cleanup success.
    pub fn is_closed(&self) -> bool {
        matches!(self.owner_lifecycle, OwnerLifecycle::Closed)
    }

    /// Returns the last validated resource-zero snapshot, if an owner was retired.
    pub fn last_retired_resources(&self) -> Option<&ResourceSnapshot> {
        self.last_retired_resources.as_ref()
    }

    /// Installs the baseline link and operation-terminal barrier once the initial owner has
    /// observed all identities needed for recovery. The caller cannot change it after recovery
    /// starts, and an absent/empty barrier is only valid when explicitly configured as such.
    pub fn configure_old_operation_barrier(
        &mut self,
        old_link: Option<RecoveryLink>,
        expected_terminals: BTreeSet<RecoveryOperationId>,
    ) -> Result<(), SupervisorError> {
        self.policy
            .configure_old_operation_barrier(old_link, expected_terminals)
            .map_err(SupervisorError::RecoveryConfiguration)
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

    /// Installs bounded Rust observation delivery atomically with original BLeng submission.
    /// The returned operation still owns start waiting/cancellation; delivery is not start success
    /// or native control. Dropping/detaching delivery does not stop a native scan.
    ///
    /// Lane-attached submissions are named `*_view` so the plain command name always means the raw
    /// typed operation; see [`Self::begin_start_scan`].
    pub fn begin_scan_view(
        &self,
        context: &RecoveryContext,
        options: ScanOptions,
    ) -> Result<ScanHandoff, ScanSubmissionError> {
        let active = self.operation_session(context)?;
        active
            .observation_projection_lease
            .projection()
            .submit_scan(|| {
                active
                    .bleng
                    .begin_start_scan(options)
                    .map_err(SupervisorOperationError::from)
            })
    }

    /// Installs one adapter-state observation lane for this owner.
    ///
    /// Observation only: the adapter, the engine and every operation keep running, and retiring or
    /// detaching the lane changes none of them. There is no native submission to interleave with,
    /// so this needs none of scan's start-handshake bookkeeping.
    pub fn observe_adapter(
        &self,
        context: &RecoveryContext,
    ) -> Result<AdapterHandoff, AdapterSubmissionError> {
        let active = self.operation_session(context)?;
        let delivery = active
            .observation_projection_lease
            .projection()
            .observe_adapter()?;
        Ok(AdapterHandoff {
            bleng: Arc::clone(&active.bleng),
            delivery,
        })
    }

    /// Installs one companion-presence observation lane for this owner.
    ///
    /// Observation only: the association, the adapter, the engine and every operation keep running,
    /// and retiring or detaching the lane changes none of them. Presence is edge-triggered, so the
    /// lane reports transitions observed after it was installed, never a replayed current state.
    pub fn observe_companion_presence(
        &self,
        context: &RecoveryContext,
    ) -> Result<CompanionPresenceHandoff, CompanionPresenceSubmissionError> {
        let active = self.operation_session(context)?;
        let delivery = active
            .observation_projection_lease
            .projection()
            .observe_companion_presence()?;
        Ok(CompanionPresenceHandoff {
            bleng: Arc::clone(&active.bleng),
            delivery,
        })
    }

    /// Installs one bounded notification value lane atomically with the original enable
    /// submission, so the enable's own state change can bind it and no value is lost in between.
    pub fn begin_notifications_view(
        &self,
        context: &RecoveryContext,
        source: NotificationSource,
        mode: NotificationModeRequest,
    ) -> Result<NotificationHandoff, NotificationSubmissionError> {
        let active = self.operation_session(context)?;
        active
            .observation_projection_lease
            .projection()
            .submit_notifications(source, || {
                active
                    .bleng
                    .begin_enable_notifications(
                        source.connection,
                        source.session,
                        source.characteristic,
                        mode,
                    )
                    .map_err(SupervisorOperationError::from)
            })
    }

    /// Submits synchronously after fencing the request to the active owner and policy.
    /// BLeng retains ownership of admission, result waiting, deadlines and cancellation.
    pub fn begin_start_scan(
        &self,
        context: &RecoveryContext,
        options: ScanOptions,
    ) -> Result<Arc<StartScanOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_start_scan(options)?)
    }

    pub fn begin_stop_scan(
        &self,
        context: &RecoveryContext,
        session: ScanSessionId,
    ) -> Result<Arc<StopScanOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_stop_scan(session)?)
    }

    /// Installs the connection-end lane atomically with the original connect submission, so the
    /// connect's own state change binds it and a later end cannot be missed in between.
    ///
    /// A connect capability always carries its end lane here; there is no raw connect at this layer.
    pub fn begin_connect_view(
        &self,
        context: &RecoveryContext,
        peripheral: PeripheralId,
    ) -> Result<ConnectionHandoff, ConnectionSubmissionError> {
        self.begin_connect_view_with_options(context, peripheral, false)
    }

    /// Same as [`Self::begin_connect_view`], with an optional auto-connect link request. A platform
    /// that cannot express it rejects the command rather than silently connecting once.
    pub fn begin_connect_view_with_options(
        &self,
        context: &RecoveryContext,
        peripheral: PeripheralId,
        auto_connect: bool,
    ) -> Result<ConnectionHandoff, ConnectionSubmissionError> {
        let active = self.operation_session(context)?;
        active
            .observation_projection_lease
            .projection()
            .submit_connect(|| {
                active
                    .bleng
                    .begin_connect_with_options(peripheral, auto_connect)
                    .map_err(SupervisorOperationError::from)
            })
    }

    pub fn begin_disconnect(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<DisconnectOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_disconnect(connection)?)
    }

    pub fn begin_discover_gatt(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<DiscoverGattOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_discover_gatt(connection)?)
    }

    pub fn begin_read_characteristic(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
    ) -> Result<Arc<ReadOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_read_characteristic(
            connection,
            session,
            characteristic,
        )?)
    }

    pub fn begin_write_characteristic(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        value: Vec<u8>,
        write_type: WriteType,
    ) -> Result<Arc<WriteOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_write_characteristic(
            connection,
            session,
            characteristic,
            value,
            write_type,
        )?)
    }

    pub fn begin_enable_notifications(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> Result<Arc<EnableNotificationsOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_enable_notifications(
            connection,
            session,
            characteristic,
            mode,
        )?)
    }

    pub fn begin_disable_notifications(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        subscription: SubscriptionId,
    ) -> Result<Arc<DisableNotificationsOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_disable_notifications(
            connection,
            session,
            subscription,
        )?)
    }

    pub fn begin_read_rssi(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<ReadRssiOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_read_rssi(connection)?)
    }

    pub fn begin_request_mtu(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        mtu: u16,
    ) -> Result<Arc<MtuOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_request_mtu(connection, mtu)?)
    }

    pub fn begin_query_mtu(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<MtuOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_query_mtu(connection)?)
    }

    pub fn begin_read_descriptor(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
    ) -> Result<Arc<ReadDescriptorOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_read_descriptor(
            connection,
            session,
            characteristic,
            descriptor,
        )?)
    }

    pub fn begin_write_descriptor(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        session: GattSessionId,
        characteristic: GattCharacteristicId,
        descriptor: GattDescriptorId,
        value: Vec<u8>,
    ) -> Result<Arc<WriteDescriptorOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_write_descriptor(
            connection,
            session,
            characteristic,
            descriptor,
            value,
        )?)
    }

    pub fn begin_set_connection_priority(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        priority: ConnectionPriority,
    ) -> Result<Arc<SetConnectionPriorityOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_set_connection_priority(connection, priority)?)
    }

    /// Requests a radio power change where the platform supports it. Success only means the
    /// platform accepted the request; the adapter observation lane reports the actual state.
    pub fn begin_set_adapter_enabled(
        &self,
        context: &RecoveryContext,
        enabled: bool,
    ) -> Result<Arc<SetAdapterEnabledOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_set_adapter_enabled(enabled)?)
    }

    /// Requests one companion-device association. Success is the platform's association receipt,
    /// never a present device or a link; read the companion inventory afterwards for the list.
    pub fn begin_associate_companion(
        &self,
        context: &RecoveryContext,
        service_uuids: Vec<BluetoothUuid>,
        display_name: Option<String>,
        device_profile: Option<String>,
        notify_on_device_nearby: bool,
    ) -> Result<Arc<AssociateCompanionDeviceOperation>, SupervisorOperationError> {
        Ok(self.operation_owner(context)?.begin_associate_companion(
            service_uuids,
            display_name,
            device_profile,
            notify_on_device_nearby,
        )?)
    }

    /// Reads a read-only system inventory. Results are data, not connectable capabilities.
    pub fn begin_query_devices(
        &self,
        context: &RecoveryContext,
        kind: DeviceQueryKind,
        service_uuids: Vec<BluetoothUuid>,
    ) -> Result<Arc<QueryDevicesOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_query_devices(kind, service_uuids)?)
    }

    pub fn begin_create_bond(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        pin: Option<String>,
    ) -> Result<Arc<BondOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_create_bond(connection, pin)?)
    }

    pub fn begin_remove_bond(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<BondOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_remove_bond(connection)?)
    }

    /// Installs a progress lane atomically with the create-bond submission. The terminal receipt
    /// stays on `BondOperation::wait`; this lane only reports interim platform states.
    pub fn begin_create_bond_view(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
        pin: Option<String>,
    ) -> Result<BondHandoff, BondSubmissionError> {
        let active = self.operation_session(context)?;
        active
            .observation_projection_lease
            .projection()
            .submit_bond(connection, BondState::Bonded, || {
                active
                    .bleng
                    .begin_create_bond(connection, pin)
                    .map_err(SupervisorOperationError::from)
            })
    }

    /// Installs a progress lane atomically with the remove-bond submission.
    pub fn begin_remove_bond_view(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<BondHandoff, BondSubmissionError> {
        let active = self.operation_session(context)?;
        active
            .observation_projection_lease
            .projection()
            .submit_bond(connection, BondState::None, || {
                active
                    .bleng
                    .begin_remove_bond(connection)
                    .map_err(SupervisorOperationError::from)
            })
    }

    /// Requests invalidation of the platform's cached GATT topology for this connection.
    pub fn begin_refresh_gatt_cache(
        &self,
        context: &RecoveryContext,
        connection: ConnectionId,
    ) -> Result<Arc<RefreshGattCacheOperation>, SupervisorOperationError> {
        Ok(self
            .operation_owner(context)?
            .begin_refresh_gatt_cache(connection)?)
    }

    pub(crate) fn operation_owner(
        &self,
        context: &RecoveryContext,
    ) -> Result<&Bleng, SupervisorOperationError> {
        Ok(&self.operation_session(context)?.bleng)
    }

    fn operation_session(
        &self,
        context: &RecoveryContext,
    ) -> Result<&ActiveOwnerSession, SupervisorOperationError> {
        let OwnerLifecycle::Active(active) = &self.owner_lifecycle else {
            return Err(SupervisorOperationError::NoActiveSession);
        };
        // Check both authorities before calling any begin method: BLeng allocates its
        // command identity during submission, even when later actor admission rejects it.
        if context != active.platform.context() || context != self.policy.active_context() {
            return Err(SupervisorOperationError::StaleContext);
        }
        if !matches!(
            self.policy.phase(),
            RecoveryPhase::Active | RecoveryPhase::Recovered
        ) {
            return Err(SupervisorOperationError::RecoveryNotReady);
        }
        Ok(active)
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
    pub async fn start(&mut self) -> Result<OwnerStart, SupervisorError> {
        // Initial startup is one-shot: OwnerLifecycle distinguishes an active owner from
        // an already-retired initial owner.
        self.owner_lifecycle.require_initial_start()?;

        // RecoveryPolicy starts in Active and cannot advance before an initial owner exists:
        // observe() rejects NotStarted. The lifecycle guards above are therefore the complete
        // startup admission check at this boundary.
        let context = self.policy.active_context().clone();
        let active = self.create_session(&context).await?;
        let start = OwnerStart {
            context,
            initialization_command_id: active.initialization_command_id,
            adapter_snapshot: active.adapter_snapshot.clone(),
        };
        self.owner_lifecycle = OwnerLifecycle::Active(active);
        Ok(start)
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
        let resources = self.shutdown_active(active).await?;
        self.last_retired_resources = Some(resources);
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

    /// Drives platform actions until the next owner-boundary action or terminal plan.
    ///
    /// Each platform action remains single-use and sequence-fenced. This method does not execute
    /// `RetireOldOwner` or `CreateFreshOwner`; callers must invoke the corresponding owner-boundary
    /// method explicitly after inspecting the returned plan.
    pub async fn drive_platform_actions_until_boundary(
        &mut self,
    ) -> Result<RecoveryPlan, SupervisorError> {
        loop {
            let plan = self.execute_platform_action().await?;
            let Some(action) = plan.next_action() else {
                return Ok(plan);
            };
            if matches!(
                action,
                RecoveryAction::RetireOldOwner | RecoveryAction::CreateFreshOwner
            ) {
                return Ok(plan);
            }
        }
    }

    /// Shuts down the BLeng owner, then validates native resource retirement.
    pub async fn shutdown(&mut self) -> Result<ResourceSnapshot, SupervisorError> {
        let active = self.owner_lifecycle.take_active_for_shutdown()?;
        self.pending_plan = None;
        let resources = self.shutdown_active(active).await?;
        self.last_retired_resources = Some(resources.clone());
        Ok(resources)
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
            PlatformRecoveryAction::from_recovery_action(action, self.recovery_target.as_ref())?;
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
        let observation_projection_lease = ObservationProjectionLease::new(context.clone());
        if let Err(primary_error) = platform.start_event_reader(
            Arc::clone(&owner),
            observation_projection_lease.projection(),
        ) {
            observation_projection_lease.revoke();
            return Err(self
                .cleanup_after_backend_transfer(platform.as_ref(), owner, primary_error)
                .await);
        }

        // Initialization is part of owner assembly: every session action assumes a ready
        // backend, and the event reader must already be active to observe the adapter snapshot.
        let initialization = match owner.begin_initialize() {
            Ok(operation) => operation,
            Err(primary_error) => {
                observation_projection_lease.revoke();
                return Err(self
                    .cleanup_after_backend_transfer(platform.as_ref(), owner, primary_error.into())
                    .await);
            }
        };
        let initialization_command_id = initialization.command_id();
        let adapter_snapshot = match initialization.wait(INITIALIZE_TIMEOUT_MS).await {
            Ok(snapshot) => snapshot,
            Err(primary_error) => {
                observation_projection_lease.revoke();
                return Err(self
                    .cleanup_after_backend_transfer(platform.as_ref(), owner, primary_error.into())
                    .await);
            }
        };

        Ok(ActiveOwnerSession {
            observation_projection_lease,
            bleng: owner,
            platform,
            initialization_command_id,
            adapter_snapshot,
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
        if let Some(snapshot) = &snapshot
            && let Err(error) = self.validate_resources(snapshot)
        {
            failures.push(("resource validation", error));
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
