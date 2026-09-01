use super::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct SubscriptionCorrelation {
    pub(super) connection: ConnectionCorrelation,
    pub(super) generation: u64,
    pub(super) gatt_session_id: GattSessionId,
    pub(super) subscription_id: SubscriptionId,
    pub(super) characteristic_id: GattCharacteristicId,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SubscriptionPhase {
    Registering { command_id: CommandId },
    Enabling { command_id: CommandId },
    Active,
    Disabling { command_id: CommandId },
    DisabledPendingRevoke,
    Revoking { command_id: CommandId },
}

pub(super) struct SubscriptionRoute {
    pub(super) correlation: SubscriptionCorrelation,
    pub(super) phase: SubscriptionPhase,
    pub(super) characteristic: WinrtGattCharacteristic,
    pub(super) value_changed_token: Option<i64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SubscriptionIoAction {
    Enable { actual_mode: NotificationMode },
    Disable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct SubscriptionIoCommand {
    pub(super) command: SetNotificationsCommand,
    pub(super) correlation: SubscriptionCorrelation,
    pub(super) action: SubscriptionIoAction,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum IoCommand {
    Read(ReadCharacteristicCommand),
    Write(WriteCharacteristicCommand),
    Subscription(SubscriptionIoCommand),
}

impl IoCommand {
    pub(super) fn command_id(&self) -> CommandId {
        match self {
            Self::Read(command) => command.command_id,
            Self::Write(command) => command.command_id,
            Self::Subscription(command) => command.command.command_id,
        }
    }

    fn connection_id(&self) -> blehub::ConnectionId {
        match self {
            Self::Read(command) => command.connection_id,
            Self::Write(command) => command.connection_id,
            Self::Subscription(command) => command.command.connection_id,
        }
    }

    fn gatt_session_id(&self) -> GattSessionId {
        match self {
            Self::Read(command) => command.gatt_session_id,
            Self::Write(command) => command.gatt_session_id,
            Self::Subscription(command) => command.command.gatt_session_id,
        }
    }

    fn characteristic_id(&self) -> GattCharacteristicId {
        match self {
            Self::Read(command) => command.characteristic_id,
            Self::Write(command) => command.characteristic_id,
            Self::Subscription(command) => command.command.characteristic_id,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct IoCorrelation {
    pub(super) connection: ConnectionCorrelation,
    pub(super) generation: u64,
    pub(super) command: IoCommand,
}

pub(super) struct QueuedIo {
    pub(super) correlation: IoCorrelation,
    pub(super) characteristic: WinrtGattCharacteristic,
    pub(super) operation: PendingIoOperationState,
    pub(super) completion: Option<Result<IoSuccess, BleEventError>>,
}

pub(super) enum PendingIoOperationState {
    Reserved,
    Running(PendingIoOperation),
}

impl PendingIoOperationState {
    pub(super) fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Reserved => Ok(()),
            Self::Running(operation) => operation.cancel(),
        }
    }
}

pub(super) enum PendingIoOperation {
    Read(IAsyncOperation<GattReadResult>),
    Write(IAsyncOperation<GattWriteResult>),
    Cccd(IAsyncOperation<GattWriteResult>),
}

impl PendingIoOperation {
    fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Read(operation) => operation.Cancel(),
            Self::Write(operation) => operation.Cancel(),
            Self::Cccd(operation) => operation.Cancel(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) enum IoSuccess {
    Read(Vec<u8>),
    Write,
    Cccd,
}

#[derive(Clone)]
struct IoStart {
    correlation: IoCorrelation,
    characteristic: WinrtGattCharacteristic,
}

pub(super) fn begin_read(
    backend: &WindowsBackend,
    command: ReadCharacteristicCommand,
) -> Result<(), CommandDispatchError> {
    begin_io(backend, IoCommand::Read(command))
}

pub(super) fn begin_write(
    backend: &WindowsBackend,
    command: WriteCharacteristicCommand,
) -> Result<(), CommandDispatchError> {
    begin_io(backend, IoCommand::Write(command))
}

pub(super) fn begin_set_notifications(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
) -> Result<(), CommandDispatchError> {
    match command.action {
        NotificationAction::Enable { mode } => begin_enable_notifications(backend, command, mode),
        NotificationAction::Disable => begin_disable_notifications(backend, command),
    }
}

struct EnableReservation {
    correlation: SubscriptionCorrelation,
    characteristic: WinrtGattCharacteristic,
    actual_mode: NotificationMode,
}

enum EnableDispatch {
    Unsupported {
        ingress: std::sync::Arc<blehub::BackendEventIngressHandle>,
        requested_mode: NotificationMode,
    },
    Register(EnableReservation),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ModeSelection {
    Supported(NotificationMode),
    StrictUnsupported(NotificationMode),
    NoSupportedMode,
}

pub(super) fn select_notification_mode(
    request: NotificationModeRequest,
    properties: CharacteristicProperties,
) -> ModeSelection {
    match request {
        NotificationModeRequest::PlatformDefault if properties.notifiable => {
            ModeSelection::Supported(NotificationMode::Notification)
        }
        NotificationModeRequest::PlatformDefault if properties.indicatable => {
            ModeSelection::Supported(NotificationMode::Indication)
        }
        NotificationModeRequest::PlatformDefault => ModeSelection::NoSupportedMode,
        NotificationModeRequest::RequireNotification if properties.notifiable => {
            ModeSelection::Supported(NotificationMode::Notification)
        }
        NotificationModeRequest::RequireNotification => {
            ModeSelection::StrictUnsupported(NotificationMode::Notification)
        }
        NotificationModeRequest::RequireIndication if properties.indicatable => {
            ModeSelection::Supported(NotificationMode::Indication)
        }
        NotificationModeRequest::RequireIndication => {
            ModeSelection::StrictUnsupported(NotificationMode::Indication)
        }
    }
}

fn begin_enable_notifications(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
    request: NotificationModeRequest,
) -> Result<(), CommandDispatchError> {
    let delivery_guard = lock_delivery(&backend.delivery);
    let dispatch = reserve_notification_enable(backend, command, request)?;
    let EnableDispatch::Register(reservation) = dispatch else {
        let EnableDispatch::Unsupported {
            ingress,
            requested_mode,
        } = dispatch
        else {
            return Err(CommandDispatchError::BackendUnavailable {
                detail: "Windows notification reservation entered an invalid state".to_owned(),
                platform_details: None,
            });
        };
        push_events(
            &ingress,
            vec![BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: Some(simple_event_error(
                    BleErrorCategory::NotSupported,
                    format!(
                        "characteristic does not support strictly requested {requested_mode:?} mode"
                    ),
                )),
            }],
            "reject unsupported strict Windows notification mode after acceptance",
        );
        drop(delivery_guard);
        return Ok(());
    };

    let weak_state = std::sync::Arc::downgrade(&backend.state);
    let callback_delivery = std::sync::Arc::clone(&backend.delivery);
    let callback_correlation = reservation.correlation;
    let callback_resource =
        track_callback(&backend.state, CallbackResource::CharacteristicValueChanged);
    let handler = TypedEventHandler::<WinrtGattCharacteristic, GattValueChangedEventArgs>::new(
        move |_sender, args| {
            let _callback_resource = &callback_resource;
            let Some(args) = args.as_ref() else {
                return Ok(());
            };
            let value = match args
                .CharacteristicValue()
                .and_then(|value| read_buffer(&value))
            {
                Ok(value) => value,
                Err(error) => {
                    tracing::warn!(
                        target: "blehub_windows::gatt",
                        detail = %error,
                        "failed to copy Windows GATT notification value"
                    );
                    return Ok(());
                }
            };
            if let Some(state) = weak_state.upgrade() {
                handle_notification_value(&state, &callback_delivery, callback_correlation, value);
            }
            Ok(())
        },
    );
    let token = match reservation.characteristic.ValueChanged(&handler) {
        Ok(token) => token,
        Err(error) => {
            rollback_notification_registration(&backend.state, reservation.correlation);
            drop(delivery_guard);
            return Err(dispatch_error(
                "register GATT characteristic value-changed callback",
                error,
            ));
        }
    };

    let queued = retain_notification_handler_and_enqueue(
        &backend.state,
        command,
        reservation.correlation,
        token,
        reservation.actual_mode,
    );
    let (connection, should_drive) = match queued {
        Some(value) => value,
        None => {
            rollback_notification_registration(&backend.state, reservation.correlation);
            drop(delivery_guard);
            log_cleanup_failure(
                reservation.characteristic.RemoveValueChanged(token),
                "remove callback after retired notification registration",
            );
            return Err(CommandDispatchError::BackendUnavailable {
                detail: "Windows notification route retired during dispatch".to_owned(),
                platform_details: None,
            });
        }
    };
    drop(delivery_guard);
    if should_drive {
        run_io_driver(&backend.state, connection);
    }
    Ok(())
}

fn reserve_notification_enable(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
    request: NotificationModeRequest,
) -> Result<EnableDispatch, CommandDispatchError> {
    let mut state = lock_state(&backend.state);
    let (connection, characteristic) = validate_notification_target(&state, command)?;
    let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection is no longer available for notification configuration".to_owned(),
        });
    };
    if subscription_id_is_active_or_transitioning(
        gatt.subscriptions.keys().copied(),
        command.subscription_id,
    ) {
        return Err(CommandDispatchError::InvalidRequest {
            detail: "the subscription ID is currently active or transitioning for this connection"
                .to_owned(),
        });
    }
    if gatt
        .subscription_by_characteristic
        .contains_key(&command.characteristic_id)
    {
        return Err(CommandDispatchError::InvalidState {
            detail: "the characteristic already has a notification subscription route".to_owned(),
        });
    }
    if queued_read_conflicts(gatt, command.characteristic_id) {
        return Err(CommandDispatchError::InvalidState {
            detail: "a characteristic read is active or queued for the notification target"
                .to_owned(),
        });
    }

    let selection = select_notification_mode(request, characteristic.properties);
    if selection == ModeSelection::NoSupportedMode {
        return Err(CommandDispatchError::InvalidRequest {
            detail: "characteristic advertises neither Notify nor Indicate".to_owned(),
        });
    }
    if let ModeSelection::StrictUnsupported(requested_mode) = selection {
        let ingress = state
            .ingress()
            .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                detail: "Windows event ingress disappeared during notification dispatch".to_owned(),
                platform_details: None,
            })?;
        return Ok(EnableDispatch::Unsupported {
            ingress,
            requested_mode,
        });
    }
    let ModeSelection::Supported(actual_mode) = selection else {
        return Err(CommandDispatchError::BackendUnavailable {
            detail: "Windows notification mode selection entered an invalid state".to_owned(),
            platform_details: None,
        });
    };
    let generation = state.gatt_io_generations.allocate_one().ok_or_else(|| {
        CommandDispatchError::BackendUnavailable {
            detail: "Windows GATT I/O generation space is exhausted".to_owned(),
            platform_details: None,
        }
    })?;
    let correlation = SubscriptionCorrelation {
        connection,
        generation,
        gatt_session_id: command.gatt_session_id,
        subscription_id: command.subscription_id,
        characteristic_id: command.characteristic_id,
    };
    let route = SubscriptionRoute {
        correlation,
        phase: SubscriptionPhase::Registering {
            command_id: command.command_id,
        },
        characteristic: characteristic.native.clone(),
        value_changed_token: None,
    };
    let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection retired while reserving the notification route".to_owned(),
        });
    };
    gatt.subscription_by_characteristic
        .insert(command.characteristic_id, command.subscription_id);
    gatt.subscriptions.insert(command.subscription_id, route);
    Ok(EnableDispatch::Register(EnableReservation {
        correlation,
        characteristic: characteristic.native,
        actual_mode,
    }))
}

pub(super) fn subscription_id_is_active_or_transitioning(
    active_or_transitioning_ids: impl IntoIterator<Item = SubscriptionId>,
    subscription_id: SubscriptionId,
) -> bool {
    active_or_transitioning_ids
        .into_iter()
        .any(|active_id| active_id == subscription_id)
}

fn rollback_notification_registration(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: SubscriptionCorrelation,
) {
    let mut current = lock_state(state);
    let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
        return;
    };
    if gatt
        .subscriptions
        .get(&correlation.subscription_id)
        .is_some_and(|route| {
            route.correlation == correlation
                && matches!(route.phase, SubscriptionPhase::Registering { .. })
        })
    {
        gatt.subscriptions.remove(&correlation.subscription_id);
        gatt.subscription_by_characteristic
            .remove(&correlation.characteristic_id);
    }
}

fn retain_notification_handler_and_enqueue(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    command: SetNotificationsCommand,
    correlation: SubscriptionCorrelation,
    token: i64,
    actual_mode: NotificationMode,
) -> Option<(ConnectionCorrelation, bool)> {
    let mut current = lock_state(state);
    let gatt = connected_gatt_mut(&mut current, correlation.connection)?;
    let route = gatt.subscriptions.get_mut(&correlation.subscription_id)?;
    if route.correlation != correlation
        || route.phase
            != (SubscriptionPhase::Registering {
                command_id: command.command_id,
            })
    {
        return None;
    }
    route.value_changed_token = Some(token);
    route.phase = SubscriptionPhase::Enabling {
        command_id: command.command_id,
    };
    let should_drive = gatt.io_queue.is_empty();
    gatt.io_queue.push_back(QueuedIo {
        correlation: IoCorrelation {
            connection: correlation.connection,
            generation: correlation.generation,
            command: IoCommand::Subscription(SubscriptionIoCommand {
                command,
                correlation,
                action: SubscriptionIoAction::Enable { actual_mode },
            }),
        },
        characteristic: route.characteristic.clone(),
        operation: PendingIoOperationState::Reserved,
        completion: None,
    });
    if should_drive {
        gatt.io_driver_running = true;
    }
    Some((correlation.connection, should_drive))
}

enum DisableDispatch {
    WriteCccd {
        connection: ConnectionCorrelation,
        should_drive: bool,
    },
    RetryCallbackRevoke {
        ingress: std::sync::Arc<blehub::BackendEventIngressHandle>,
        removal: Box<PreparedSubscriptionRemoval>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum NotificationDisableDecision {
    WriteCccd,
    RetryCallbackRevoke,
    Reject,
}

pub(super) fn notification_disable_decision(
    phase: SubscriptionPhase,
) -> NotificationDisableDecision {
    match phase {
        SubscriptionPhase::Active => NotificationDisableDecision::WriteCccd,
        SubscriptionPhase::DisabledPendingRevoke => {
            NotificationDisableDecision::RetryCallbackRevoke
        }
        _ => NotificationDisableDecision::Reject,
    }
}

fn begin_disable_notifications(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
) -> Result<(), CommandDispatchError> {
    let delivery_guard = lock_delivery(&backend.delivery);
    let dispatch = reserve_notification_disable(backend, command)?;
    drop(delivery_guard);
    match dispatch {
        DisableDispatch::WriteCccd {
            connection,
            should_drive,
        } => {
            if should_drive {
                run_io_driver(&backend.state, connection);
            }
        }
        DisableDispatch::RetryCallbackRevoke { ingress, removal } => {
            complete_detached_subscription_removal(
                &backend.state,
                &backend.delivery,
                Some(ingress),
                *removal,
                "retry Windows notification callback revocation",
            );
        }
    }
    Ok(())
}

fn reserve_notification_disable(
    backend: &WindowsBackend,
    command: SetNotificationsCommand,
) -> Result<DisableDispatch, CommandDispatchError> {
    let mut state = lock_state(&backend.state);
    let (connection, _) = validate_notification_target(&state, command)?;
    let (correlation, phase) = {
        let Some(gatt) = connected_gatt(&state, connection) else {
            return Err(CommandDispatchError::InvalidState {
                detail: "connection is no longer available for notification configuration"
                    .to_owned(),
            });
        };
        let Some(route) = gatt.subscriptions.get(&command.subscription_id) else {
            return Err(CommandDispatchError::InvalidState {
                detail: "the subscription ID is unknown or no longer active".to_owned(),
            });
        };
        if route.correlation.connection != connection
            || route.correlation.gatt_session_id != command.gatt_session_id
            || route.correlation.characteristic_id != command.characteristic_id
        {
            return Err(CommandDispatchError::InvalidRequest {
                detail: "the subscription scope does not match the active route".to_owned(),
            });
        }
        (route.correlation, route.phase)
    };

    match notification_disable_decision(phase) {
        NotificationDisableDecision::WriteCccd => {
            let generation = state.gatt_io_generations.allocate_one().ok_or_else(|| {
                CommandDispatchError::BackendUnavailable {
                    detail: "Windows GATT I/O generation space is exhausted".to_owned(),
                    platform_details: None,
                }
            })?;
            let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "connection is no longer available for notification configuration"
                        .to_owned(),
                });
            };
            let Some(route) = gatt.subscriptions.get_mut(&command.subscription_id) else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "the subscription ID is unknown or no longer active".to_owned(),
                });
            };
            route.phase = SubscriptionPhase::Disabling {
                command_id: command.command_id,
            };
            let characteristic = route.characteristic.clone();
            let should_drive = gatt.io_queue.is_empty();
            gatt.io_queue.push_back(QueuedIo {
                correlation: IoCorrelation {
                    connection,
                    generation,
                    command: IoCommand::Subscription(SubscriptionIoCommand {
                        command,
                        correlation,
                        action: SubscriptionIoAction::Disable,
                    }),
                },
                characteristic,
                operation: PendingIoOperationState::Reserved,
                completion: None,
            });
            if should_drive {
                gatt.io_driver_running = true;
            }
            Ok(DisableDispatch::WriteCccd {
                connection,
                should_drive,
            })
        }
        NotificationDisableDecision::RetryCallbackRevoke => {
            let ingress =
                state
                    .ingress()
                    .ok_or_else(|| CommandDispatchError::BackendUnavailable {
                        detail: "Windows event ingress disappeared during notification dispatch"
                            .to_owned(),
                        platform_details: None,
                    })?;
            let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "connection is no longer available for notification configuration"
                        .to_owned(),
                });
            };
            let Some(route) = gatt.subscriptions.get_mut(&command.subscription_id) else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "the subscription ID is unknown or no longer active".to_owned(),
                });
            };
            route.phase = SubscriptionPhase::Revoking {
                command_id: command.command_id,
            };
            let removal = PreparedSubscriptionRemoval {
                command: SubscriptionIoCommand {
                    command,
                    correlation,
                    action: SubscriptionIoAction::Disable,
                },
                decision: SubscriptionCompletionDecision::RetryCallbackRevoke,
                characteristic: route.characteristic.clone(),
                value_changed_token: route.value_changed_token.take(),
                error: None,
            };
            gatt.cleanup_in_progress = true;
            Ok(DisableDispatch::RetryCallbackRevoke {
                ingress,
                removal: Box::new(removal),
            })
        }
        NotificationDisableDecision::Reject => Err(CommandDispatchError::InvalidState {
            detail: "the subscription is not active or is already transitioning".to_owned(),
        }),
    }
}

fn validate_notification_target(
    state: &BackendState,
    command: SetNotificationsCommand,
) -> Result<(ConnectionCorrelation, RetainedGattCharacteristic), CommandDispatchError> {
    ensure_ready(state)?;
    let generation = state
        .active_connections
        .get(&command.connection_id)
        .copied()
        .ok_or_else(|| CommandDispatchError::InvalidState {
            detail: "connection lifecycle is not active".to_owned(),
        })?;
    let connection = state.connections.get(&generation).ok_or_else(|| {
        CommandDispatchError::BackendUnavailable {
            detail: "Windows connection ownership is transitioning".to_owned(),
            platform_details: None,
        }
    })?;
    let ConnectionPhase::Connected { gatt, .. } = &connection.phase else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection is not ready for notification configuration".to_owned(),
        });
    };
    let characteristic =
        resolve_active_characteristic(gatt, command.gatt_session_id, command.characteristic_id)?;
    Ok((connection.correlation, characteristic))
}

fn resolve_active_characteristic(
    gatt: &ConnectionGattState,
    gatt_session_id: GattSessionId,
    characteristic_id: GattCharacteristicId,
) -> Result<RetainedGattCharacteristic, CommandDispatchError> {
    ensure_gatt_cleanup_idle(gatt)?;
    if gatt.pending.is_some() {
        return Err(CommandDispatchError::InvalidState {
            detail: "GATT discovery is pending for this connection".to_owned(),
        });
    }
    let active = gatt
        .active
        .as_ref()
        .ok_or_else(|| CommandDispatchError::InvalidState {
            detail: "no discovered GATT database is active for this connection".to_owned(),
        })?;
    if active.gatt_session_id != gatt_session_id {
        return Err(CommandDispatchError::InvalidState {
            detail: "the GATT session ID is stale or does not match the active database".to_owned(),
        });
    }
    active
        .characteristics
        .get(&characteristic_id)
        .cloned()
        .ok_or_else(|| CommandDispatchError::InvalidState {
            detail: "the GATT characteristic ID is stale or not present in the active database"
                .to_owned(),
        })
}

fn queued_read_conflicts(
    gatt: &ConnectionGattState,
    characteristic_id: GattCharacteristicId,
) -> bool {
    gatt.io_queue.iter().any(|queued| {
        io_command_is_conflicting_read(&queued.correlation.command, characteristic_id)
    })
}

pub(super) fn io_command_is_conflicting_read(
    command: &IoCommand,
    characteristic_id: GattCharacteristicId,
) -> bool {
    matches!(
        command,
        IoCommand::Read(read) if read.characteristic_id == characteristic_id
    )
}

pub(super) fn subscription_phase_blocks_read(phase: SubscriptionPhase) -> bool {
    matches!(
        phase,
        SubscriptionPhase::Registering { .. }
            | SubscriptionPhase::Enabling { .. }
            | SubscriptionPhase::Active
            | SubscriptionPhase::Disabling { .. }
            | SubscriptionPhase::Revoking { .. }
    )
}

fn begin_io(backend: &WindowsBackend, command: IoCommand) -> Result<(), CommandDispatchError> {
    let (connection, should_drive) = reserve_io(backend, command)?;
    let services_changed_pending = should_drive && drive_io_queue(&backend.state, connection);
    if services_changed_pending {
        drain_deferred_services_changed(&backend.state, connection);
    }
    Ok(())
}

fn reserve_io(
    backend: &WindowsBackend,
    command: IoCommand,
) -> Result<(ConnectionCorrelation, bool), CommandDispatchError> {
    let mut state = lock_state(&backend.state);
    let (connection, characteristic) = validate_io(&state, &command)?;
    let generation = state.gatt_io_generations.allocate_one().ok_or_else(|| {
        CommandDispatchError::BackendUnavailable {
            detail: "Windows GATT I/O generation space is exhausted".to_owned(),
            platform_details: None,
        }
    })?;
    let correlation = IoCorrelation {
        connection,
        generation,
        command,
    };
    let Some(gatt) = connected_gatt_mut(&mut state, connection) else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection is no longer available for GATT I/O".to_owned(),
        });
    };
    let should_drive = gatt.io_queue.is_empty();
    gatt.io_queue.push_back(QueuedIo {
        correlation,
        characteristic: characteristic.native,
        operation: PendingIoOperationState::Reserved,
        completion: None,
    });
    if should_drive {
        gatt.io_driver_running = true;
    }
    Ok((connection, should_drive))
}

fn validate_io(
    state: &BackendState,
    command: &IoCommand,
) -> Result<(ConnectionCorrelation, RetainedGattCharacteristic), CommandDispatchError> {
    ensure_ready(state)?;
    validate_io_shape(command)?;
    let generation = state
        .active_connections
        .get(&command.connection_id())
        .copied()
        .ok_or_else(|| CommandDispatchError::InvalidState {
            detail: "connection lifecycle is not active".to_owned(),
        })?;
    let connection = state.connections.get(&generation).ok_or_else(|| {
        CommandDispatchError::BackendUnavailable {
            detail: "Windows connection ownership is transitioning".to_owned(),
            platform_details: None,
        }
    })?;
    if connection.correlation.connection_id != command.connection_id() {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection correlation no longer matches the GATT I/O request".to_owned(),
        });
    }
    let ConnectionPhase::Connected { gatt, .. } = &connection.phase else {
        return Err(CommandDispatchError::InvalidState {
            detail: "connection is not ready for GATT I/O".to_owned(),
        });
    };
    let characteristic = resolve_active_characteristic(
        gatt,
        command.gatt_session_id(),
        command.characteristic_id(),
    )?;
    if matches!(command, IoCommand::Read(_))
        && gatt
            .subscription_by_characteristic
            .get(&command.characteristic_id())
            .and_then(|subscription_id| gatt.subscriptions.get(subscription_id))
            .is_some_and(|route| subscription_phase_blocks_read(route.phase))
    {
        return Err(CommandDispatchError::InvalidState {
            detail: "the characteristic has an enabling, active, or disabling subscription route"
                .to_owned(),
        });
    }
    validate_io_property(command, characteristic.properties)?;
    Ok((connection.correlation, characteristic))
}

pub(super) fn validate_io_shape(command: &IoCommand) -> Result<(), CommandDispatchError> {
    if let IoCommand::Write(command) = command
        && command.value.len() > MAX_WRITE_VALUE_BYTES
    {
        return Err(CommandDispatchError::InvalidRequest {
            detail: format!(
                "value contains {} bytes; maximum is {MAX_WRITE_VALUE_BYTES}",
                command.value.len()
            ),
        });
    }
    Ok(())
}

pub(super) fn validate_io_property(
    command: &IoCommand,
    properties: CharacteristicProperties,
) -> Result<(), CommandDispatchError> {
    let mismatch = match command {
        IoCommand::Read(_) if !properties.readable => {
            Some("characteristic does not advertise the Read property")
        }
        IoCommand::Write(command)
            if command.write_type == WriteType::WithResponse
                && !properties.writable_with_response =>
        {
            Some("characteristic does not advertise the Write property")
        }
        IoCommand::Write(command)
            if command.write_type == WriteType::WithoutResponse
                && !properties.writable_without_response =>
        {
            Some("characteristic does not advertise the WriteWithoutResponse property")
        }
        _ => None,
    };
    mismatch.map_or(Ok(()), |detail| {
        Err(CommandDispatchError::InvalidRequest {
            detail: detail.to_owned(),
        })
    })
}

enum IoDriverAction {
    Start(IoStart),
    Complete {
        command: IoCommand,
        result: Result<IoSuccess, BleEventError>,
        ingress: Option<std::sync::Arc<blehub::BackendEventIngressHandle>>,
    },
    Wait,
    DrainServicesChanged,
    Gone,
}

fn drive_io_queue(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) -> bool {
    loop {
        let delivery = delivery_for_state(state);
        let delivery_guard = lock_delivery(&delivery);
        let action = {
            let mut current = lock_state(state);
            let ingress = current.ingress();
            let Some(gatt) = connected_gatt_mut(&mut current, connection) else {
                return false;
            };
            if gatt.services_changed_pending {
                gatt.io_driver_running = false;
                IoDriverAction::DrainServicesChanged
            } else if let Some((command, result)) = gatt.io_queue.front_mut().and_then(|queued| {
                queued
                    .completion
                    .take()
                    .map(|result| (queued.correlation.command.clone(), result))
            }) {
                let _ = gatt.io_queue.pop_front();
                IoDriverAction::Complete {
                    command,
                    result,
                    ingress,
                }
            } else if let Some(queued) = gatt.io_queue.front() {
                match queued.operation {
                    PendingIoOperationState::Reserved => {
                        let start = IoStart {
                            correlation: queued.correlation.clone(),
                            characteristic: queued.characteristic.clone(),
                        };
                        gatt.io_transition_in_progress = true;
                        IoDriverAction::Start(start)
                    }
                    PendingIoOperationState::Running(_) => {
                        gatt.io_driver_running = false;
                        IoDriverAction::Wait
                    }
                }
            } else {
                gatt.io_driver_running = false;
                IoDriverAction::Gone
            }
        };

        match action {
            IoDriverAction::Start(start) => {
                drop(delivery_guard);
                let operation = match start_io(&start) {
                    Ok(operation) => operation,
                    Err(error) => {
                        finish_io_transition(state, &start.correlation);
                        submit_io_completion(state, start.correlation, Err(error));
                        continue;
                    }
                };
                if !store_io_operation(state, &start.correlation, &operation) {
                    log_cleanup_failure(
                        operation.cancel(),
                        "cancel retired Windows GATT I/O operation",
                    );
                    return false;
                }
                let completion_registration =
                    install_io_completion_handler(state, start.correlation.clone(), &operation);
                finish_io_transition(state, &start.correlation);
                if let Err(error) = completion_registration {
                    submit_io_completion(
                        state,
                        start.correlation,
                        Err(event_error(
                            BleErrorCategory::Gatt,
                            "register Windows GATT I/O completion",
                            error,
                        )),
                    );
                    log_cleanup_failure(
                        operation.cancel(),
                        "cancel GATT I/O after completion registration failure",
                    );
                }
            }
            IoDriverAction::Complete {
                command,
                result,
                ingress,
            } => match prepare_io_completion(state, command, result) {
                PreparedIoCompletion::Events(events) => {
                    if let Some(ingress) = ingress {
                        push_events(&ingress, events, "complete Windows GATT I/O");
                    }
                    drop(delivery_guard);
                }
                PreparedIoCompletion::RemoveSubscription(removal) => {
                    drop(delivery_guard);
                    complete_detached_subscription_removal(
                        state,
                        &delivery,
                        ingress,
                        *removal,
                        "complete Windows GATT I/O",
                    );
                }
            },
            IoDriverAction::Wait | IoDriverAction::Gone => {
                drop(delivery_guard);
                return false;
            }
            IoDriverAction::DrainServicesChanged => {
                drop(delivery_guard);
                return true;
            }
        }
    }
}

fn run_io_driver(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    connection: ConnectionCorrelation,
) {
    let services_changed_pending = drive_io_queue(state, connection);
    if services_changed_pending {
        drain_deferred_services_changed(state, connection);
    }
}

fn start_io(start: &IoStart) -> Result<PendingIoOperation, BleEventError> {
    match &start.correlation.command {
        IoCommand::Read(_) => start
            .characteristic
            .ReadValueWithCacheModeAsync(BluetoothCacheMode::Uncached)
            .map(PendingIoOperation::Read)
            .map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "start uncached GATT characteristic read",
                    error,
                )
            }),
        IoCommand::Write(command) => {
            let writer = DataWriter::new().map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "create GATT characteristic write buffer",
                    error,
                )
            })?;
            writer.WriteBytes(&command.value).map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "copy GATT characteristic write value",
                    error,
                )
            })?;
            let buffer = writer.DetachBuffer().map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "detach GATT characteristic write buffer",
                    error,
                )
            })?;
            start
                .characteristic
                .WriteValueWithResultAndOptionAsync(&buffer, map_write_option(command.write_type))
                .map(PendingIoOperation::Write)
                .map_err(|error| {
                    event_error(
                        BleErrorCategory::Gatt,
                        "start GATT characteristic write",
                        error,
                    )
                })
        }
        IoCommand::Subscription(command) => start
            .characteristic
            .WriteClientCharacteristicConfigurationDescriptorWithResultAsync(cccd_value(
                command.action,
            ))
            .map(PendingIoOperation::Cccd)
            .map_err(|error| {
                event_error(
                    BleErrorCategory::Gatt,
                    "start GATT client-characteristic configuration write",
                    error,
                )
            }),
    }
}

pub(super) fn cccd_value(
    action: SubscriptionIoAction,
) -> GattClientCharacteristicConfigurationDescriptorValue {
    match action {
        SubscriptionIoAction::Enable {
            actual_mode: NotificationMode::Notification,
        } => GattClientCharacteristicConfigurationDescriptorValue::Notify,
        SubscriptionIoAction::Enable {
            actual_mode: NotificationMode::Indication,
        } => GattClientCharacteristicConfigurationDescriptorValue::Indicate,
        SubscriptionIoAction::Disable => GattClientCharacteristicConfigurationDescriptorValue::None,
    }
}

pub(super) fn map_write_option(write_type: WriteType) -> GattWriteOption {
    match write_type {
        WriteType::WithResponse => GattWriteOption::WriteWithResponse,
        WriteType::WithoutResponse => GattWriteOption::WriteWithoutResponse,
    }
}

fn store_io_operation(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: &IoCorrelation,
    operation: &PendingIoOperation,
) -> bool {
    let mut current = lock_state(state);
    connected_gatt_mut(&mut current, correlation.connection)
        .and_then(|gatt| gatt.io_queue.front_mut())
        .is_some_and(|queued| {
            if queued.correlation != *correlation
                || !matches!(queued.operation, PendingIoOperationState::Reserved)
            {
                return false;
            }
            queued.operation = PendingIoOperationState::Running(clone_io_operation(operation));
            true
        })
}

fn clone_io_operation(operation: &PendingIoOperation) -> PendingIoOperation {
    match operation {
        PendingIoOperation::Read(operation) => PendingIoOperation::Read(operation.clone()),
        PendingIoOperation::Write(operation) => PendingIoOperation::Write(operation.clone()),
        PendingIoOperation::Cccd(operation) => PendingIoOperation::Cccd(operation.clone()),
    }
}

fn finish_io_transition(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: &IoCorrelation,
) {
    let mut current = lock_state(state);
    if let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection)
        && gatt
            .io_queue
            .front()
            .is_some_and(|queued| queued.correlation == *correlation)
    {
        gatt.io_transition_in_progress = false;
    }
}

fn io_operation_is_current(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: &IoCorrelation,
) -> bool {
    let current = lock_state(state);
    connected_gatt(&current, correlation.connection)
        .and_then(|gatt| gatt.io_queue.front())
        .is_some_and(|queued| {
            queued.correlation == *correlation
                && matches!(queued.operation, PendingIoOperationState::Running(_))
        })
}

fn install_io_completion_handler(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: IoCorrelation,
    operation: &PendingIoOperation,
) -> windows::core::Result<()> {
    match operation {
        PendingIoOperation::Read(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_correlation = correlation.clone();
            let callback_resource = track_callback(state, CallbackResource::CharacteristicRead);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && io_operation_is_current(&state, &callback_correlation)
                {
                    let result = io_completion_result(
                        operation.as_ref(),
                        status,
                        "uncached GATT characteristic read",
                        |operation| operation.GetResults(),
                    )
                    .and_then(process_read_result)
                    .map(IoSuccess::Read);
                    submit_io_completion(&state, callback_correlation.clone(), result);
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
        PendingIoOperation::Write(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_correlation = correlation.clone();
            let callback_resource = track_callback(state, CallbackResource::CharacteristicWrite);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && io_operation_is_current(&state, &callback_correlation)
                {
                    let result = io_completion_result(
                        operation.as_ref(),
                        status,
                        "GATT characteristic write",
                        |operation| operation.GetResults(),
                    )
                    .and_then(process_write_result)
                    .map(|()| IoSuccess::Write);
                    submit_io_completion(&state, callback_correlation.clone(), result);
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
        PendingIoOperation::Cccd(operation) => {
            let weak_state = std::sync::Arc::downgrade(state);
            let callback_correlation = correlation.clone();
            let callback_resource = track_callback(state, CallbackResource::CccdWrite);
            let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
                let _callback_resource = &callback_resource;
                if let Some(state) = weak_state.upgrade()
                    && io_operation_is_current(&state, &callback_correlation)
                {
                    let result = io_completion_result(
                        operation.as_ref(),
                        status,
                        "GATT client-characteristic configuration write",
                        |operation| operation.GetResults(),
                    )
                    .and_then(process_cccd_write_result)
                    .map(|()| IoSuccess::Cccd);
                    submit_io_completion(&state, callback_correlation.clone(), result);
                }
                Ok(())
            });
            operation.SetCompleted(&handler)
        }
    }
}

fn io_completion_result<T, O>(
    operation: Option<&IAsyncOperation<T>>,
    status: AsyncStatus,
    operation_name: &'static str,
    get_results: impl FnOnce(&IAsyncOperation<T>) -> windows::core::Result<O>,
) -> Result<O, BleEventError>
where
    T: windows::core::RuntimeType,
{
    if status != AsyncStatus::Completed {
        let category = if status == AsyncStatus::Canceled {
            BleErrorCategory::Cancelled
        } else {
            BleErrorCategory::Gatt
        };
        return Err(simple_event_error(
            category,
            format!("{operation_name} ended with {status:?}"),
        ));
    }
    let Some(operation) = operation else {
        return Err(simple_event_error(
            BleErrorCategory::Internal,
            format!("{operation_name} completion omitted the operation"),
        ));
    };
    get_results(operation).map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            &format!("complete Windows {operation_name}"),
            error,
        )
    })
}

fn process_read_result(result: GattReadResult) -> Result<Vec<u8>, BleEventError> {
    let status = result.Status().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read GATT characteristic read status",
            error,
        )
    })?;
    let protocol_error = if status == GattCommunicationStatus::ProtocolError {
        result.protocol_error()?
    } else {
        None
    };
    ensure_communication_success_with_protocol(status, protocol_error, "GATT characteristic read")?;
    let value = result.Value().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "read GATT characteristic value buffer",
            error,
        )
    })?;
    read_buffer(&value).map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            "copy GATT characteristic value buffer",
            error,
        )
    })
}

fn process_write_result(result: GattWriteResult) -> Result<(), BleEventError> {
    process_gatt_write_result(result, "GATT characteristic write")
}

fn process_cccd_write_result(result: GattWriteResult) -> Result<(), BleEventError> {
    process_gatt_write_result(result, "GATT client-characteristic configuration write")
}

fn process_gatt_write_result(
    result: GattWriteResult,
    operation: &'static str,
) -> Result<(), BleEventError> {
    let status = result.Status().map_err(|error| {
        event_error(
            BleErrorCategory::Gatt,
            &format!("read {operation} status"),
            error,
        )
    })?;
    let protocol_error = if status == GattCommunicationStatus::ProtocolError {
        result.protocol_error()?
    } else {
        None
    };
    ensure_communication_success_with_protocol(status, protocol_error, operation)
}

trait ProtocolErrorResult {
    fn protocol_error(&self) -> Result<Option<u8>, BleEventError>;
}

impl ProtocolErrorResult for GattReadResult {
    fn protocol_error(&self) -> Result<Option<u8>, BleEventError> {
        extract_protocol_error(
            self.ProtocolError(),
            "read GATT characteristic read protocol error",
        )
    }
}

impl ProtocolErrorResult for GattWriteResult {
    fn protocol_error(&self) -> Result<Option<u8>, BleEventError> {
        extract_protocol_error(
            self.ProtocolError(),
            "read GATT characteristic write protocol error",
        )
    }
}

fn extract_protocol_error(
    value: windows::core::Result<windows::Foundation::IReference<u8>>,
    context: &'static str,
) -> Result<Option<u8>, BleEventError> {
    let value = value.map_err(|error| event_error(BleErrorCategory::Gatt, context, error))?;
    if value.as_raw().is_null() {
        return Ok(None);
    }
    value
        .Value()
        .map(Some)
        .map_err(|error| event_error(BleErrorCategory::Gatt, context, error))
}

fn submit_io_completion(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    correlation: IoCorrelation,
    result: Result<IoSuccess, BleEventError>,
) {
    let should_run = {
        let mut current = lock_state(state);
        let Some(gatt) = connected_gatt_mut(&mut current, correlation.connection) else {
            return;
        };
        let Some(queued) = gatt.io_queue.front_mut() else {
            return;
        };
        if queued.correlation != correlation || queued.completion.is_some() {
            return;
        }
        queued.completion = Some(result);
        if gatt.io_driver_running {
            false
        } else {
            gatt.io_driver_running = true;
            true
        }
    };
    if should_run {
        run_io_driver(state, correlation.connection);
    }
}

pub(super) fn io_completion_events(
    command: IoCommand,
    result: Result<IoSuccess, BleEventError>,
) -> Vec<BleEvent> {
    match result {
        Ok(IoSuccess::Read(value)) => vec![
            BleEvent::CharacteristicRead {
                command_id: command.command_id(),
                connection_id: command.connection_id(),
                gatt_session_id: command.gatt_session_id(),
                characteristic_id: command.characteristic_id(),
                value,
            },
            BleEvent::CommandCompleted {
                command_id: command.command_id(),
                error: None,
            },
        ],
        Ok(IoSuccess::Write) => vec![
            BleEvent::CharacteristicWritten {
                command_id: command.command_id(),
                connection_id: command.connection_id(),
                gatt_session_id: command.gatt_session_id(),
                characteristic_id: command.characteristic_id(),
            },
            BleEvent::CommandCompleted {
                command_id: command.command_id(),
                error: None,
            },
        ],
        Ok(IoSuccess::Cccd) => Vec::new(),
        Err(error) => vec![BleEvent::CommandCompleted {
            command_id: command.command_id(),
            error: Some(error),
        }],
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SubscriptionCompletionDecision {
    Activate,
    RemoveAfterEnableFailure,
    RemoveAfterDisableSuccess,
    RetryCallbackRevoke,
    RestoreActive,
    Stale,
}

pub(super) fn subscription_completion_decision(
    phase: SubscriptionPhase,
    action: SubscriptionIoAction,
    succeeded: bool,
    command_id: CommandId,
) -> SubscriptionCompletionDecision {
    match (phase, action, succeeded) {
        (
            SubscriptionPhase::Enabling {
                command_id: pending,
            },
            SubscriptionIoAction::Enable { .. },
            true,
        ) if pending == command_id => SubscriptionCompletionDecision::Activate,
        (
            SubscriptionPhase::Enabling {
                command_id: pending,
            },
            SubscriptionIoAction::Enable { .. },
            false,
        ) if pending == command_id => SubscriptionCompletionDecision::RemoveAfterEnableFailure,
        (
            SubscriptionPhase::Disabling {
                command_id: pending,
            },
            SubscriptionIoAction::Disable,
            true,
        ) if pending == command_id => SubscriptionCompletionDecision::RemoveAfterDisableSuccess,
        (
            SubscriptionPhase::Disabling {
                command_id: pending,
            },
            SubscriptionIoAction::Disable,
            false,
        ) if pending == command_id => SubscriptionCompletionDecision::RestoreActive,
        _ => SubscriptionCompletionDecision::Stale,
    }
}

enum PreparedIoCompletion {
    Events(Vec<BleEvent>),
    RemoveSubscription(Box<PreparedSubscriptionRemoval>),
}

struct PreparedSubscriptionRemoval {
    command: SubscriptionIoCommand,
    decision: SubscriptionCompletionDecision,
    characteristic: WinrtGattCharacteristic,
    value_changed_token: Option<i64>,
    error: Option<BleEventError>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum SubscriptionRemovalOutcome {
    RemoveRoute,
    RetainDisabledPendingRevoke,
    Stale,
}

pub(super) fn subscription_removal_outcome(
    decision: SubscriptionCompletionDecision,
    native_succeeded: bool,
) -> SubscriptionRemovalOutcome {
    match (decision, native_succeeded) {
        (SubscriptionCompletionDecision::RemoveAfterEnableFailure, _) => {
            SubscriptionRemovalOutcome::RemoveRoute
        }
        (
            SubscriptionCompletionDecision::RemoveAfterDisableSuccess
            | SubscriptionCompletionDecision::RetryCallbackRevoke,
            true,
        ) => SubscriptionRemovalOutcome::RemoveRoute,
        (
            SubscriptionCompletionDecision::RemoveAfterDisableSuccess
            | SubscriptionCompletionDecision::RetryCallbackRevoke,
            false,
        ) => SubscriptionRemovalOutcome::RetainDisabledPendingRevoke,
        _ => SubscriptionRemovalOutcome::Stale,
    }
}

fn prepare_io_completion(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    command: IoCommand,
    result: Result<IoSuccess, BleEventError>,
) -> PreparedIoCompletion {
    let IoCommand::Subscription(subscription) = command else {
        return PreparedIoCompletion::Events(io_completion_events(command, result));
    };
    let succeeded = matches!(result, Ok(IoSuccess::Cccd));
    let error = match result {
        Err(error) => Some(error),
        Ok(IoSuccess::Cccd) => None,
        Ok(_) => Some(simple_event_error(
            BleErrorCategory::Internal,
            "notification CCCD operation completed with an unrelated result",
        )),
    };
    prepare_subscription_completion(state, subscription, succeeded, error)
}

fn prepare_subscription_completion(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    command: SubscriptionIoCommand,
    succeeded: bool,
    error: Option<BleEventError>,
) -> PreparedIoCompletion {
    let mut current = lock_state(state);
    let Some(gatt) = connected_gatt_mut(&mut current, command.correlation.connection) else {
        return PreparedIoCompletion::Events(Vec::new());
    };
    let Some(route) = gatt
        .subscriptions
        .get_mut(&command.correlation.subscription_id)
    else {
        return PreparedIoCompletion::Events(Vec::new());
    };
    if route.correlation != command.correlation {
        return PreparedIoCompletion::Events(Vec::new());
    }
    let decision = subscription_completion_decision(
        route.phase,
        command.action,
        succeeded,
        command.command.command_id,
    );
    match decision {
        SubscriptionCompletionDecision::Activate => {
            route.phase = SubscriptionPhase::Active;
            PreparedIoCompletion::Events(notification_enabled_events(command))
        }
        SubscriptionCompletionDecision::RestoreActive => {
            route.phase = SubscriptionPhase::Active;
            PreparedIoCompletion::Events(vec![notification_terminal(
                command.command.command_id,
                error.unwrap_or_else(|| {
                    simple_event_error(
                        BleErrorCategory::Gatt,
                        "notification disable failed without a Windows error",
                    )
                }),
            )])
        }
        SubscriptionCompletionDecision::RemoveAfterEnableFailure
        | SubscriptionCompletionDecision::RemoveAfterDisableSuccess => {
            route.phase = SubscriptionPhase::Revoking {
                command_id: command.command.command_id,
            };
            let removal = PreparedSubscriptionRemoval {
                command,
                decision,
                characteristic: route.characteristic.clone(),
                value_changed_token: route.value_changed_token.take(),
                error,
            };
            gatt.cleanup_in_progress = true;
            PreparedIoCompletion::RemoveSubscription(Box::new(removal))
        }
        SubscriptionCompletionDecision::RetryCallbackRevoke
        | SubscriptionCompletionDecision::Stale => PreparedIoCompletion::Events(Vec::new()),
    }
}

fn complete_detached_subscription_removal(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    delivery: &std::sync::Arc<std::sync::Mutex<()>>,
    ingress: Option<std::sync::Arc<blehub::BackendEventIngressHandle>>,
    removal: PreparedSubscriptionRemoval,
    event_context: &'static str,
) {
    let native_result = remove_value_changed(&removal.characteristic, removal.value_changed_token);
    let delivery_guard = lock_delivery(delivery);
    let events = finish_subscription_removal(state, &removal, native_result);
    if let Some(ingress) = ingress {
        push_events(&ingress, events, event_context);
    }
    let connection = removal.command.correlation.connection;
    let deferred = finish_cleanup_suppression(state, connection);
    drop(delivery_guard);
    drain_deferred_cleanup(state, connection, deferred);
}

fn finish_subscription_removal(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    removal: &PreparedSubscriptionRemoval,
    native_result: windows::core::Result<()>,
) -> Vec<BleEvent> {
    let command = removal.command;
    let route_matches = |route: &SubscriptionRoute| {
        route.correlation == command.correlation
            && route.phase
                == (SubscriptionPhase::Revoking {
                    command_id: command.command.command_id,
                })
    };

    match removal.decision {
        SubscriptionCompletionDecision::RemoveAfterEnableFailure => {
            let removed = {
                let mut current = lock_state(state);
                let Some(gatt) = connected_gatt_mut(&mut current, command.correlation.connection)
                else {
                    return Vec::new();
                };
                if !gatt
                    .subscriptions
                    .get(&command.correlation.subscription_id)
                    .is_some_and(route_matches)
                {
                    return Vec::new();
                }
                gatt.subscription_by_characteristic
                    .remove(&command.correlation.characteristic_id);
                gatt.subscriptions
                    .remove(&command.correlation.subscription_id)
                    .is_some()
            };
            if !removed {
                return Vec::new();
            }
            log_cleanup_failure(
                native_result,
                "remove callback after failed notification enable",
            );
            vec![notification_terminal(
                command.command.command_id,
                removal.error.clone().unwrap_or_else(|| {
                    simple_event_error(
                        BleErrorCategory::Gatt,
                        "notification enable failed without a Windows error",
                    )
                }),
            )]
        }
        SubscriptionCompletionDecision::RemoveAfterDisableSuccess
        | SubscriptionCompletionDecision::RetryCallbackRevoke => {
            let native_error = native_result.err();
            let outcome = subscription_removal_outcome(removal.decision, native_error.is_none());
            {
                let mut current = lock_state(state);
                let Some(gatt) = connected_gatt_mut(&mut current, command.correlation.connection)
                else {
                    return Vec::new();
                };
                if !gatt
                    .subscriptions
                    .get(&command.correlation.subscription_id)
                    .is_some_and(route_matches)
                {
                    return Vec::new();
                }
                match outcome {
                    SubscriptionRemovalOutcome::RemoveRoute => {
                        gatt.subscription_by_characteristic
                            .remove(&command.correlation.characteristic_id);
                        let _ = gatt
                            .subscriptions
                            .remove(&command.correlation.subscription_id);
                    }
                    SubscriptionRemovalOutcome::RetainDisabledPendingRevoke => {
                        if let Some(route) = gatt
                            .subscriptions
                            .get_mut(&command.correlation.subscription_id)
                        {
                            route.phase = SubscriptionPhase::DisabledPendingRevoke;
                            route.value_changed_token = removal.value_changed_token;
                        }
                    }
                    SubscriptionRemovalOutcome::Stale => return Vec::new(),
                }
            }
            match native_error {
                None => notification_disabled_events(command.command),
                Some(error) => notification_revoke_failure_events(
                    command.command,
                    event_error(
                        BleErrorCategory::Gatt,
                        "remove GATT characteristic value-changed callback",
                        error,
                    ),
                ),
            }
        }
        _ => Vec::new(),
    }
}

pub(super) fn notification_enabled_events(command: SubscriptionIoCommand) -> Vec<BleEvent> {
    let SubscriptionIoAction::Enable { actual_mode } = command.action else {
        return vec![notification_terminal(
            command.command.command_id,
            simple_event_error(
                BleErrorCategory::Internal,
                "notification enable finalized with a disable action",
            ),
        )];
    };
    vec![
        BleEvent::NotificationStateChanged {
            command_id: command.command.command_id,
            connection_id: command.command.connection_id,
            gatt_session_id: command.command.gatt_session_id,
            subscription_id: command.command.subscription_id,
            characteristic_id: command.command.characteristic_id,
            state: NotificationState::Enabled {
                actual_mode: Some(actual_mode),
            },
        },
        BleEvent::CommandCompleted {
            command_id: command.command.command_id,
            error: None,
        },
    ]
}

pub(super) fn notification_disabled_events(command: SetNotificationsCommand) -> Vec<BleEvent> {
    vec![
        BleEvent::NotificationStateChanged {
            command_id: command.command_id,
            connection_id: command.connection_id,
            gatt_session_id: command.gatt_session_id,
            subscription_id: command.subscription_id,
            characteristic_id: command.characteristic_id,
            state: NotificationState::Disabled,
        },
        BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: None,
        },
    ]
}

pub(super) fn notification_revoke_failure_events(
    command: SetNotificationsCommand,
    error: BleEventError,
) -> Vec<BleEvent> {
    vec![notification_terminal(command.command_id, error)]
}

fn notification_terminal(command_id: CommandId, error: BleEventError) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: Some(error),
    }
}

fn route_accepts_notification(
    route: &SubscriptionRoute,
    correlation: SubscriptionCorrelation,
) -> bool {
    correlation_accepts_notification(route.correlation, route.phase, correlation)
}

pub(super) fn correlation_accepts_notification(
    route: SubscriptionCorrelation,
    phase: SubscriptionPhase,
    callback: SubscriptionCorrelation,
) -> bool {
    route == callback
        && matches!(
            phase,
            SubscriptionPhase::Enabling { .. }
                | SubscriptionPhase::Active
                | SubscriptionPhase::Disabling { .. }
        )
}

fn handle_notification_value(
    state: &std::sync::Arc<std::sync::Mutex<BackendState>>,
    delivery: &std::sync::Arc<std::sync::Mutex<()>>,
    correlation: SubscriptionCorrelation,
    value: Vec<u8>,
) {
    let should_deliver = {
        let current = lock_state(state);
        connected_gatt(&current, correlation.connection)
            .and_then(|gatt| gatt.subscriptions.get(&correlation.subscription_id))
            .is_some_and(|route| route_accepts_notification(route, correlation))
    };
    if !should_deliver {
        return;
    }

    let _delivery_guard = lock_delivery(delivery);
    let ingress = {
        let current = lock_state(state);
        connected_gatt(&current, correlation.connection)
            .and_then(|gatt| gatt.subscriptions.get(&correlation.subscription_id))
            .filter(|route| route_accepts_notification(route, correlation))
            .and_then(|_| current.ingress())
    };
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            vec![BleEvent::NotificationReceived {
                connection_id: correlation.connection.connection_id,
                gatt_session_id: correlation.gatt_session_id,
                subscription_id: correlation.subscription_id,
                characteristic_id: correlation.characteristic_id,
                value,
            }],
            "deliver Windows GATT notification value",
        );
    }
}

pub(super) fn io_failure_events(
    commands: impl IntoIterator<Item = IoCommand>,
    category: BleErrorCategory,
    message: &'static str,
) -> Vec<BleEvent> {
    commands
        .into_iter()
        .map(|command| BleEvent::CommandCompleted {
            command_id: command.command_id(),
            error: Some(simple_event_error(category, message)),
        })
        .collect()
}

pub(super) fn cleanup_io_queue(
    queue: VecDeque<QueuedIo>,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    if let Some(queued) = queue.front() {
        record_connection_cleanup(
            queued.operation.cancel(),
            "cancel active GATT I/O",
            &mut first_error,
        );
    }
    drop(queue);
}

pub(super) fn take_subscriptions(gatt: &mut ConnectionGattState) -> Vec<SubscriptionRoute> {
    gatt.subscription_by_characteristic.clear();
    std::mem::take(&mut gatt.subscriptions)
        .into_values()
        .collect()
}

pub(super) fn cleanup_subscription_routes(
    routes: Vec<SubscriptionRoute>,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    for route in routes {
        cleanup_subscription_route(route, first_error.as_deref_mut());
    }
}

fn cleanup_subscription_route(
    route: SubscriptionRoute,
    mut first_error: Option<&mut Option<blehub::BleHubError>>,
) {
    record_connection_cleanup(
        remove_value_changed(&route.characteristic, route.value_changed_token),
        "remove GATT characteristic value-changed callback",
        &mut first_error,
    );
}

fn remove_value_changed(
    characteristic: &WinrtGattCharacteristic,
    token: Option<i64>,
) -> windows::core::Result<()> {
    token.map_or(Ok(()), |token| characteristic.RemoveValueChanged(token))
}
