use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BleErrorCategory,
    BleEvent, BleEventError, CommandDispatchError, CommandId, ConnectCommand, ConnectionId,
    ConnectionState, ConnectionStateResult, DisconnectCommand, DiscoverGattCommand,
    GattCharacteristicId, GattDatabase, GattSessionId, InitializeCommand, NotificationAction,
    NotificationMode, NotificationModeRequest, NotificationState, PeripheralId,
    ReadCharacteristicCommand, ScanOptions, ScanSessionId, ScanStopReason, ScanStopResult,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
};

use crate::{
    EventScenario, RecordedCommand, advertisement_observation_fixture,
    duplicate_gatt_database_fixture,
};

#[derive(Clone, Debug)]
pub struct InitializationRulesScenario {
    pub pre_initialize_scan: StartScanCommand,
    pub initialize: InitializeCommand,
    pub repeated_initialize: InitializeCommand,
    pub snapshot: BleEvent,
    pub rejection: CommandDispatchError,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn initialization_rules() -> InitializationRulesScenario {
    let pre_initialize_scan = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 101 },
        options: ScanOptions::default(),
    };
    let initialize = InitializeCommand {
        command_id: CommandId { value: 2 },
    };
    let repeated_initialize = InitializeCommand {
        command_id: CommandId { value: 3 },
    };
    let snapshot = BleEvent::AdapterSnapshotChanged {
        command_id: Some(initialize.command_id),
        snapshot: AdapterSnapshot {
            state: AdapterState::PoweredOff,
            scan_authorization: AuthorizationState::Denied,
            connection_authorization: AuthorizationState::NotDetermined,
            name: None,
        },
    };
    let rejection = CommandDispatchError::InvalidState {
        detail: "backend has not been initialized or initialize was already attempted".to_owned(),
    };
    let backend_scenarios = vec![
        EventScenario::reject(
            RecordedCommand::StartScan(pre_initialize_scan.clone()),
            rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::Initialize(initialize),
            vec![snapshot.clone(), succeeded(initialize.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::Initialize(repeated_initialize),
            rejection.clone(),
        ),
    ];
    InitializationRulesScenario {
        pre_initialize_scan,
        initialize,
        repeated_initialize,
        snapshot,
        rejection,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct FailedInitializationScenario {
    pub initialize: InitializeCommand,
    pub retry: InitializeCommand,
    pub terminal: BleEvent,
    pub retry_rejection: CommandDispatchError,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn failed_initialization_cannot_retry() -> FailedInitializationScenario {
    let initialize = InitializeCommand {
        command_id: CommandId { value: 4 },
    };
    let retry = InitializeCommand {
        command_id: CommandId { value: 5 },
    };
    let terminal = failed_with_category(
        initialize.command_id,
        BleErrorCategory::Backend,
        "initialization setup failed",
    );
    let retry_rejection = CommandDispatchError::InvalidState {
        detail: "initialize was already attempted for this backend lifecycle".to_owned(),
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::Initialize(initialize),
            vec![terminal.clone()],
        ),
        EventScenario::reject(RecordedCommand::Initialize(retry), retry_rejection.clone()),
    ];
    FailedInitializationScenario {
        initialize,
        retry,
        terminal,
        retry_rejection,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct PassiveDiagnosticScenario {
    pub adapter_changed: BleEvent,
    pub backend_error: BleEvent,
}

pub fn passive_adapter_and_backend_error() -> PassiveDiagnosticScenario {
    PassiveDiagnosticScenario {
        adapter_changed: BleEvent::AdapterSnapshotChanged {
            command_id: None,
            snapshot: AdapterSnapshot {
                state: AdapterState::PoweredOff,
                scan_authorization: AuthorizationState::Authorized,
                connection_authorization: AuthorizationState::Authorized,
                name: Some("Test Adapter".to_owned()),
            },
        },
        backend_error: BleEvent::BackendError {
            error: BleEventError {
                category: BleErrorCategory::Backend,
                message: "unscoped backend fault".to_owned(),
                platform_details: None,
            },
        },
    }
}

#[derive(Clone, Debug)]
pub struct ScanExclusivityScenario {
    pub first: StartScanCommand,
    pub rejected_conflict: StartScanCommand,
    pub accepted_conflict: StartScanCommand,
    pub suspended: BleEvent,
    pub rejection: CommandDispatchError,
    pub accepted_failure: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn scan_exclusivity() -> ScanExclusivityScenario {
    let first = StartScanCommand {
        command_id: CommandId { value: 10 },
        scan_session_id: ScanSessionId { value: 110 },
        options: ScanOptions::default(),
    };
    let rejected_conflict = StartScanCommand {
        command_id: CommandId { value: 11 },
        scan_session_id: ScanSessionId { value: 111 },
        options: ScanOptions::default(),
    };
    let accepted_conflict = StartScanCommand {
        command_id: CommandId { value: 12 },
        scan_session_id: ScanSessionId { value: 112 },
        options: ScanOptions::default(),
    };
    let rejection = CommandDispatchError::InvalidState {
        detail: "a physical scan is already active".to_owned(),
    };
    let accepted_failure = failed_with_category(
        accepted_conflict.command_id,
        BleErrorCategory::InvalidState,
        "a physical scan is already active",
    );
    let suspended = BleEvent::ScanStopped {
        scan_id: first.scan_session_id,
        result: ScanStopResult::Ok {
            reason: ScanStopReason::SystemSuspended,
        },
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::StartScan(first.clone()),
            vec![
                BleEvent::ScanStarted {
                    command_id: first.command_id,
                    scan_id: first.scan_session_id,
                },
                succeeded(first.command_id),
            ],
        ),
        EventScenario::reject(
            RecordedCommand::StartScan(rejected_conflict.clone()),
            rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::StartScan(accepted_conflict.clone()),
            vec![accepted_failure.clone()],
        ),
    ];
    ScanExclusivityScenario {
        first,
        rejected_conflict,
        accepted_conflict,
        suspended,
        rejection,
        accepted_failure,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct AdvertisementIdentityScenario {
    pub first_start: StartScanCommand,
    pub first_stop: StopScanCommand,
    pub second_start: StartScanCommand,
    pub first_observation: AdvertisementObservation,
    pub second_observation: AdvertisementObservation,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn advertisement_identity_across_scans() -> AdvertisementIdentityScenario {
    let peripheral_id = PeripheralId { value: 2010 };
    let first_start = StartScanCommand {
        command_id: CommandId { value: 100 },
        scan_session_id: ScanSessionId { value: 1100 },
        options: ScanOptions::default(),
    };
    let first_stop = StopScanCommand {
        command_id: CommandId { value: 101 },
        scan_session_id: first_start.scan_session_id,
    };
    let second_start = StartScanCommand {
        command_id: CommandId { value: 102 },
        scan_session_id: ScanSessionId { value: 1101 },
        options: ScanOptions::default(),
    };
    let mut first_observation = advertisement_observation_fixture();
    first_observation.scan_session_id = first_start.scan_session_id;
    first_observation.peripheral_id = peripheral_id;
    let second_observation = AdvertisementObservation {
        scan_session_id: second_start.scan_session_id,
        peripheral_id,
        local_name: None,
        advertised_services: Vec::new(),
        manufacturer_data: Vec::new(),
        service_data: Vec::new(),
        rssi: None,
        connectable: None,
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::StartScan(first_start.clone()),
            vec![
                BleEvent::ScanStarted {
                    command_id: first_start.command_id,
                    scan_id: first_start.scan_session_id,
                },
                succeeded(first_start.command_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::StopScan(first_stop),
            vec![
                BleEvent::ScanStopped {
                    scan_id: first_stop.scan_session_id,
                    result: ScanStopResult::Ok {
                        reason: ScanStopReason::Requested {
                            command_id: first_stop.command_id,
                        },
                    },
                },
                succeeded(first_stop.command_id),
            ],
        ),
        EventScenario::complete(
            RecordedCommand::StartScan(second_start.clone()),
            vec![
                BleEvent::ScanStarted {
                    command_id: second_start.command_id,
                    scan_id: second_start.scan_session_id,
                },
                succeeded(second_start.command_id),
            ],
        ),
    ];
    AdvertisementIdentityScenario {
        first_start,
        first_stop,
        second_start,
        first_observation,
        second_observation,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct ConnectionExclusivityScenario {
    pub first: ConnectCommand,
    pub rejected_conflict: ConnectCommand,
    pub accepted_conflict: ConnectCommand,
    pub connected: BleEvent,
    pub rejection: CommandDispatchError,
    pub accepted_failure: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn connection_exclusivity() -> ConnectionExclusivityScenario {
    let peripheral_id = PeripheralId { value: 210 };
    let first = ConnectCommand {
        command_id: CommandId { value: 20 },
        connection_id: ConnectionId { value: 220 },
        peripheral_id,
    };
    let rejected_conflict = ConnectCommand {
        command_id: CommandId { value: 21 },
        connection_id: ConnectionId { value: 221 },
        peripheral_id,
    };
    let accepted_conflict = ConnectCommand {
        command_id: CommandId { value: 22 },
        connection_id: ConnectionId { value: 222 },
        peripheral_id,
    };
    let connected = BleEvent::ConnectionStateChanged {
        command_id: Some(first.command_id),
        connection_id: first.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    };
    let rejection = CommandDispatchError::InvalidState {
        detail: "the peripheral already has an active connection lifecycle".to_owned(),
    };
    let accepted_failure = failed_with_category(
        accepted_conflict.command_id,
        BleErrorCategory::InvalidState,
        "the peripheral already has an active connection lifecycle",
    );
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::Connect(first),
            vec![connected.clone(), succeeded(first.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::Connect(rejected_conflict),
            rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::Connect(accepted_conflict),
            vec![accepted_failure.clone()],
        ),
    ];
    ConnectionExclusivityScenario {
        first,
        rejected_conflict,
        accepted_conflict,
        connected,
        rejection,
        accepted_failure,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct ProvenConnectCancellationScenario {
    pub connect: ConnectCommand,
    pub disconnect: DisconnectCommand,
    pub connect_terminal: BleEvent,
    pub disconnected: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn proven_connect_cancellation() -> ProvenConnectCancellationScenario {
    let connect = ConnectCommand {
        command_id: CommandId { value: 30 },
        connection_id: ConnectionId { value: 330 },
        peripheral_id: PeripheralId { value: 230 },
    };
    let disconnect = DisconnectCommand {
        command_id: CommandId { value: 31 },
        connection_id: connect.connection_id,
    };
    let connect_terminal = failed_with_category(
        connect.command_id,
        BleErrorCategory::Cancelled,
        "the pending connect was cancelled by disconnect",
    );
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: Some(disconnect.command_id),
        connection_id: disconnect.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let backend_scenarios = vec![
        EventScenario::accept(RecordedCommand::Connect(connect)),
        EventScenario::complete(
            RecordedCommand::Disconnect(disconnect),
            vec![
                connect_terminal.clone(),
                disconnected.clone(),
                succeeded(disconnect.command_id),
            ],
        ),
    ];
    ProvenConnectCancellationScenario {
        connect,
        disconnect,
        connect_terminal,
        disconnected,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct IndependentConnectFailureScenario {
    pub connect: ConnectCommand,
    pub disconnect: DisconnectCommand,
    pub connect_failed: BleEvent,
    pub connect_terminal: BleEvent,
    pub disconnected: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn independent_connect_failure() -> IndependentConnectFailureScenario {
    let connect = ConnectCommand {
        command_id: CommandId { value: 40 },
        connection_id: ConnectionId { value: 440 },
        peripheral_id: PeripheralId { value: 240 },
    };
    let disconnect = DisconnectCommand {
        command_id: CommandId { value: 41 },
        connection_id: connect.connection_id,
    };
    let error = BleEventError {
        category: BleErrorCategory::Connection,
        message: "the native connect failed independently".to_owned(),
        platform_details: None,
    };
    let connect_failed = BleEvent::ConnectionStateChanged {
        command_id: Some(connect.command_id),
        connection_id: connect.connection_id,
        result: ConnectionStateResult::Err {
            error: error.clone(),
        },
    };
    let connect_terminal = BleEvent::CommandCompleted {
        command_id: connect.command_id,
        error: Some(error),
    };
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: Some(disconnect.command_id),
        connection_id: disconnect.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let backend_scenarios = vec![
        EventScenario::accept(RecordedCommand::Connect(connect)),
        EventScenario::complete(
            RecordedCommand::Disconnect(disconnect),
            vec![
                connect_failed.clone(),
                connect_terminal.clone(),
                disconnected.clone(),
                succeeded(disconnect.command_id),
            ],
        ),
    ];
    IndependentConnectFailureScenario {
        connect,
        disconnect,
        connect_failed,
        connect_terminal,
        disconnected,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct UnconfirmedConnectCancellationScenario {
    pub connect: ConnectCommand,
    pub disconnect: DisconnectCommand,
    pub disconnected: BleEvent,
    pub late_connected: BleEvent,
    pub late_terminal: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn unconfirmed_connect_cancellation() -> UnconfirmedConnectCancellationScenario {
    let connect = ConnectCommand {
        command_id: CommandId { value: 50 },
        connection_id: ConnectionId { value: 550 },
        peripheral_id: PeripheralId { value: 250 },
    };
    let disconnect = DisconnectCommand {
        command_id: CommandId { value: 51 },
        connection_id: connect.connection_id,
    };
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: Some(disconnect.command_id),
        connection_id: disconnect.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let late_connected = BleEvent::ConnectionStateChanged {
        command_id: Some(connect.command_id),
        connection_id: connect.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    };
    let late_terminal = succeeded(connect.command_id);
    let backend_scenarios = vec![
        EventScenario::accept(RecordedCommand::Connect(connect)),
        EventScenario::complete(
            RecordedCommand::Disconnect(disconnect),
            vec![disconnected.clone(), succeeded(disconnect.command_id)],
        ),
    ];
    UnconfirmedConnectCancellationScenario {
        connect,
        disconnect,
        disconnected,
        late_connected,
        late_terminal,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct DisconnectCleanupScenario {
    pub disconnect: DisconnectCommand,
    pub repeated: DisconnectCommand,
    pub unknown: DisconnectCommand,
    pub disconnected: BleEvent,
    pub rejection: CommandDispatchError,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn disconnect_cleanup() -> DisconnectCleanupScenario {
    let connection_id = ConnectionId { value: 6600 };
    let disconnect = DisconnectCommand {
        command_id: CommandId { value: 60 },
        connection_id,
    };
    let repeated = DisconnectCommand {
        command_id: CommandId { value: 61 },
        connection_id,
    };
    let unknown = DisconnectCommand {
        command_id: CommandId { value: 62 },
        connection_id: ConnectionId { value: 6601 },
    };
    let disconnected = BleEvent::ConnectionStateChanged {
        command_id: Some(disconnect.command_id),
        connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let rejection = CommandDispatchError::InvalidState {
        detail: "connection lifecycle is not active".to_owned(),
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::Disconnect(disconnect),
            vec![disconnected.clone(), succeeded(disconnect.command_id)],
        ),
        EventScenario::reject(RecordedCommand::Disconnect(repeated), rejection.clone()),
        EventScenario::reject(RecordedCommand::Disconnect(unknown), rejection.clone()),
    ];
    DisconnectCleanupScenario {
        disconnect,
        repeated,
        unknown,
        disconnected,
        rejection,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct PassiveDisconnectRaceScenario {
    pub disconnect: DisconnectCommand,
    pub passive_disconnected: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn passive_disconnect_race() -> PassiveDisconnectRaceScenario {
    let disconnect = DisconnectCommand {
        command_id: CommandId { value: 63 },
        connection_id: ConnectionId { value: 6602 },
    };
    let passive_disconnected = BleEvent::ConnectionStateChanged {
        command_id: None,
        connection_id: disconnect.connection_id,
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    };
    let backend_scenarios = vec![EventScenario::complete(
        RecordedCommand::Disconnect(disconnect),
        vec![succeeded(disconnect.command_id)],
    )];
    PassiveDisconnectRaceScenario {
        disconnect,
        passive_disconnected,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct RepeatedStopScenario {
    pub first: StopScanCommand,
    pub repeated: StopScanCommand,
    pub stopped: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn repeated_stop() -> RepeatedStopScenario {
    let scan_session_id = ScanSessionId { value: 11 };
    let first_id = CommandId { value: 7 };
    let repeated_id = CommandId { value: 8 };
    let first = StopScanCommand {
        command_id: first_id,
        scan_session_id,
    };
    let repeated = StopScanCommand {
        command_id: repeated_id,
        scan_session_id,
    };
    let stopped = BleEvent::ScanStopped {
        scan_id: scan_session_id,
        result: ScanStopResult::Ok {
            reason: ScanStopReason::Requested {
                command_id: first_id,
            },
        },
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::StopScan(first),
            vec![stopped.clone(), succeeded(first_id)],
        ),
        EventScenario::complete(
            RecordedCommand::StopScan(repeated),
            vec![succeeded(repeated_id)],
        ),
    ];
    RepeatedStopScenario {
        first,
        repeated,
        stopped,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct GattDiscoveryScenario {
    pub database: GattDatabase,
    pub success: DiscoverGattCommand,
    pub failure: DiscoverGattCommand,
    pub discovered: BleEvent,
    pub failure_terminal: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn gatt_discovery_all_or_error() -> GattDiscoveryScenario {
    let database = duplicate_gatt_database_fixture();
    let success_id = CommandId { value: 7 };
    let failure_id = CommandId { value: 8 };
    let success = DiscoverGattCommand {
        command_id: success_id,
        connection_id: database.connection_id,
        gatt_session_id: database.gatt_session_id,
    };
    let failure = DiscoverGattCommand {
        command_id: failure_id,
        connection_id: database.connection_id,
        gatt_session_id: GattSessionId { value: 402 },
    };
    let discovered = BleEvent::GattDiscovered {
        command_id: success_id,
        database: database.clone(),
    };
    let failure_terminal = failed(failure_id);
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(success),
            vec![discovered.clone(), succeeded(success_id)],
        ),
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(failure),
            vec![failure_terminal.clone()],
        ),
    ];
    GattDiscoveryScenario {
        database,
        success,
        failure,
        discovered,
        failure_terminal,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct GattInvalidationScenario {
    pub old_discover: DiscoverGattCommand,
    pub old_enable: SetNotificationsCommand,
    pub rejected_old_read: ReadCharacteristicCommand,
    pub new_discover: DiscoverGattCommand,
    pub new_read: ReadCharacteristicCommand,
    pub old_database: BleEvent,
    pub old_enabled: BleEvent,
    pub new_database: BleEvent,
    pub new_read_value: BleEvent,
    pub late_old_value: BleEvent,
    pub invalidated_rejection: CommandDispatchError,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn gatt_invalidation_isolation() -> GattInvalidationScenario {
    let connection_id = ConnectionId { value: 1200 };
    let old_gatt_session_id = GattSessionId { value: 1201 };
    let new_gatt_session_id = GattSessionId { value: 1202 };
    let subscription_id = SubscriptionId { value: 1203 };
    let old_discover = DiscoverGattCommand {
        command_id: CommandId { value: 110 },
        connection_id,
        gatt_session_id: old_gatt_session_id,
    };
    let mut old_database_value = duplicate_gatt_database_fixture();
    old_database_value.connection_id = connection_id;
    old_database_value.gatt_session_id = old_gatt_session_id;
    let characteristic_id = old_database_value.services[0].characteristics[0].id;
    let old_enable = SetNotificationsCommand {
        command_id: CommandId { value: 111 },
        connection_id,
        gatt_session_id: old_gatt_session_id,
        subscription_id,
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let rejected_old_read = ReadCharacteristicCommand {
        command_id: CommandId { value: 112 },
        connection_id,
        gatt_session_id: old_gatt_session_id,
        characteristic_id,
    };
    let new_discover = DiscoverGattCommand {
        command_id: CommandId { value: 113 },
        connection_id,
        gatt_session_id: new_gatt_session_id,
    };
    let mut new_database_value = duplicate_gatt_database_fixture();
    new_database_value.connection_id = connection_id;
    new_database_value.gatt_session_id = new_gatt_session_id;
    let new_read = ReadCharacteristicCommand {
        command_id: CommandId { value: 114 },
        connection_id,
        gatt_session_id: new_gatt_session_id,
        characteristic_id,
    };
    let old_database = BleEvent::GattDiscovered {
        command_id: old_discover.command_id,
        database: old_database_value,
    };
    let old_enabled = BleEvent::NotificationStateChanged {
        command_id: old_enable.command_id,
        connection_id,
        gatt_session_id: old_gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Notification),
        },
    };
    let new_database = BleEvent::GattDiscovered {
        command_id: new_discover.command_id,
        database: new_database_value,
    };
    let new_read_value = BleEvent::CharacteristicRead {
        command_id: new_read.command_id,
        connection_id,
        gatt_session_id: new_gatt_session_id,
        characteristic_id,
        value: vec![5],
    };
    let late_old_value = BleEvent::NotificationReceived {
        connection_id,
        gatt_session_id: old_gatt_session_id,
        subscription_id,
        characteristic_id,
        value: vec![6],
    };
    let invalidated_rejection = CommandDispatchError::InvalidState {
        detail: "the native GATT map has been invalidated".to_owned(),
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(old_discover),
            vec![old_database.clone(), succeeded(old_discover.command_id)],
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(old_enable),
            vec![old_enabled.clone(), succeeded(old_enable.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::ReadCharacteristic(rejected_old_read),
            invalidated_rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::DiscoverGatt(new_discover),
            vec![new_database.clone(), succeeded(new_discover.command_id)],
        ),
        EventScenario::complete(
            RecordedCommand::ReadCharacteristic(new_read),
            vec![new_read_value.clone(), succeeded(new_read.command_id)],
        ),
    ];
    GattInvalidationScenario {
        old_discover,
        old_enable,
        rejected_old_read,
        new_discover,
        new_read,
        old_database,
        old_enabled,
        new_database,
        new_read_value,
        late_old_value,
        invalidated_rejection,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct ReadSubscriptionConflictScenario {
    pub pending_read: ReadCharacteristicCommand,
    pub enable_during_pending_read: SetNotificationsCommand,
    pub enable: SetNotificationsCommand,
    pub read_during_pending_enable: ReadCharacteristicCommand,
    pub read_during_active_subscription: ReadCharacteristicCommand,
    pub disable: SetNotificationsCommand,
    pub read_during_pending_disable: ReadCharacteristicCommand,
    pub repeated_disable: SetNotificationsCommand,
    pub unknown_disable: SetNotificationsCommand,
    pub read_completed: BleEvent,
    pub enabled: BleEvent,
    pub disabled: BleEvent,
    pub overlap_rejection: CommandDispatchError,
    pub inactive_rejection: CommandDispatchError,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn read_subscription_conflicts() -> ReadSubscriptionConflictScenario {
    let connection_id = ConnectionId { value: 660 };
    let gatt_session_id = GattSessionId { value: 661 };
    let characteristic_id = GattCharacteristicId { value: 662 };
    let subscription_id = SubscriptionId { value: 663 };
    let pending_read = ReadCharacteristicCommand {
        command_id: CommandId { value: 60 },
        connection_id,
        gatt_session_id,
        characteristic_id,
    };
    let enable_during_pending_read = SetNotificationsCommand {
        command_id: CommandId { value: 61 },
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let enable = SetNotificationsCommand {
        command_id: CommandId { value: 62 },
        ..enable_during_pending_read
    };
    let read_during_pending_enable = ReadCharacteristicCommand {
        command_id: CommandId { value: 63 },
        ..pending_read
    };
    let read_during_active_subscription = ReadCharacteristicCommand {
        command_id: CommandId { value: 64 },
        ..pending_read
    };
    let disable = SetNotificationsCommand {
        command_id: CommandId { value: 65 },
        action: NotificationAction::Disable,
        ..enable
    };
    let read_during_pending_disable = ReadCharacteristicCommand {
        command_id: CommandId { value: 66 },
        ..pending_read
    };
    let repeated_disable = SetNotificationsCommand {
        command_id: CommandId { value: 67 },
        ..disable
    };
    let unknown_disable = SetNotificationsCommand {
        command_id: CommandId { value: 68 },
        subscription_id: SubscriptionId { value: 664 },
        ..disable
    };
    let read_completed = BleEvent::CharacteristicRead {
        command_id: pending_read.command_id,
        connection_id,
        gatt_session_id,
        characteristic_id,
        value: vec![1, 2, 3],
    };
    let enabled = BleEvent::NotificationStateChanged {
        command_id: enable.command_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Notification),
        },
    };
    let disabled = BleEvent::NotificationStateChanged {
        command_id: disable.command_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Disabled,
    };
    let overlap_rejection = CommandDispatchError::InvalidState {
        detail: "read and subscription lifecycle overlap".to_owned(),
    };
    let inactive_rejection = CommandDispatchError::InvalidState {
        detail: "subscription is not active".to_owned(),
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::ReadCharacteristic(pending_read),
            vec![read_completed.clone(), succeeded(pending_read.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::SetNotifications(enable_during_pending_read),
            overlap_rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(enable),
            vec![enabled.clone(), succeeded(enable.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::ReadCharacteristic(read_during_pending_enable),
            overlap_rejection.clone(),
        ),
        EventScenario::reject(
            RecordedCommand::ReadCharacteristic(read_during_active_subscription),
            overlap_rejection.clone(),
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(disable),
            vec![disabled.clone(), succeeded(disable.command_id)],
        ),
        EventScenario::reject(
            RecordedCommand::ReadCharacteristic(read_during_pending_disable),
            overlap_rejection.clone(),
        ),
        EventScenario::reject(
            RecordedCommand::SetNotifications(repeated_disable),
            inactive_rejection.clone(),
        ),
        EventScenario::reject(
            RecordedCommand::SetNotifications(unknown_disable),
            inactive_rejection.clone(),
        ),
    ];
    ReadSubscriptionConflictScenario {
        pending_read,
        enable_during_pending_read,
        enable,
        read_during_pending_enable,
        read_during_active_subscription,
        disable,
        read_during_pending_disable,
        repeated_disable,
        unknown_disable,
        read_completed,
        enabled,
        disabled,
        overlap_rejection,
        inactive_rejection,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct ReadAfterDisableScenario {
    pub allowed: ReadCharacteristicCommand,
    pub allowed_event: BleEvent,
    pub conservative: ReadCharacteristicCommand,
    pub conservative_rejection: CommandDispatchError,
    pub allowed_backend_scenarios: Vec<EventScenario>,
    pub conservative_backend_scenarios: Vec<EventScenario>,
}

pub fn read_after_disable() -> ReadAfterDisableScenario {
    let allowed = ReadCharacteristicCommand {
        command_id: CommandId { value: 70 },
        connection_id: ConnectionId { value: 770 },
        gatt_session_id: GattSessionId { value: 771 },
        characteristic_id: GattCharacteristicId { value: 772 },
    };
    let conservative = ReadCharacteristicCommand {
        command_id: CommandId { value: 71 },
        ..allowed
    };
    let allowed_event = BleEvent::CharacteristicRead {
        command_id: allowed.command_id,
        connection_id: allowed.connection_id,
        gatt_session_id: allowed.gatt_session_id,
        characteristic_id: allowed.characteristic_id,
        value: vec![4, 5, 6],
    };
    let conservative_rejection = CommandDispatchError::InvalidState {
        detail: "late notification values cannot be classified safely in this GATT session"
            .to_owned(),
    };
    let allowed_backend_scenarios = vec![EventScenario::complete(
        RecordedCommand::ReadCharacteristic(allowed),
        vec![allowed_event.clone(), succeeded(allowed.command_id)],
    )];
    let conservative_backend_scenarios = vec![EventScenario::reject(
        RecordedCommand::ReadCharacteristic(conservative),
        conservative_rejection.clone(),
    )];
    ReadAfterDisableScenario {
        allowed,
        allowed_event,
        conservative,
        conservative_rejection,
        allowed_backend_scenarios,
        conservative_backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct NotificationCallbackTimingScenario {
    pub enable: SetNotificationsCommand,
    pub disable: SetNotificationsCommand,
    pub read: ReadCharacteristicCommand,
    pub enabled: BleEvent,
    pub disabled: BleEvent,
    pub early_value: BleEvent,
    pub disabling_value: BleEvent,
    pub late_value: BleEvent,
    pub read_value: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn notification_callback_timing() -> NotificationCallbackTimingScenario {
    let connection_id = ConnectionId { value: 880 };
    let gatt_session_id = GattSessionId { value: 881 };
    let subscription_id = SubscriptionId { value: 882 };
    let characteristic_id = GattCharacteristicId { value: 883 };
    let enable = SetNotificationsCommand {
        command_id: CommandId { value: 80 },
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let disable = SetNotificationsCommand {
        command_id: CommandId { value: 81 },
        action: NotificationAction::Disable,
        ..enable
    };
    let read = ReadCharacteristicCommand {
        command_id: CommandId { value: 82 },
        connection_id,
        gatt_session_id,
        characteristic_id,
    };
    let enabled = BleEvent::NotificationStateChanged {
        command_id: enable.command_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Notification),
        },
    };
    let disabled = BleEvent::NotificationStateChanged {
        command_id: disable.command_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Disabled,
    };
    let notification = |value| BleEvent::NotificationReceived {
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        value,
    };
    let early_value = notification(vec![1]);
    let disabling_value = notification(vec![2]);
    let late_value = notification(vec![3]);
    let read_value = BleEvent::CharacteristicRead {
        command_id: read.command_id,
        connection_id,
        gatt_session_id,
        characteristic_id,
        value: vec![4],
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::SetNotifications(enable),
            vec![enabled.clone(), succeeded(enable.command_id)],
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(disable),
            vec![disabled.clone(), succeeded(disable.command_id)],
        ),
        EventScenario::complete(
            RecordedCommand::ReadCharacteristic(read),
            vec![read_value.clone(), succeeded(read.command_id)],
        ),
    ];
    NotificationCallbackTimingScenario {
        enable,
        disable,
        read,
        enabled,
        disabled,
        early_value,
        disabling_value,
        late_value,
        read_value,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct FailedNotificationEnableScenario {
    pub enable: SetNotificationsCommand,
    pub terminal: BleEvent,
    pub early_value: BleEvent,
    pub late_value: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn failed_notification_enable() -> FailedNotificationEnableScenario {
    let enable = SetNotificationsCommand {
        command_id: CommandId { value: 90 },
        connection_id: ConnectionId { value: 990 },
        gatt_session_id: GattSessionId { value: 991 },
        subscription_id: SubscriptionId { value: 992 },
        characteristic_id: GattCharacteristicId { value: 993 },
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let terminal = failed_with_category(
        enable.command_id,
        BleErrorCategory::Gatt,
        "the notification enable operation failed",
    );
    let notification = |value| BleEvent::NotificationReceived {
        connection_id: enable.connection_id,
        gatt_session_id: enable.gatt_session_id,
        subscription_id: enable.subscription_id,
        characteristic_id: enable.characteristic_id,
        value,
    };
    let early_value = notification(vec![1]);
    let late_value = notification(vec![2]);
    let backend_scenarios = vec![EventScenario::complete(
        RecordedCommand::SetNotifications(enable),
        vec![terminal.clone()],
    )];
    FailedNotificationEnableScenario {
        enable,
        terminal,
        early_value,
        late_value,
        backend_scenarios,
    }
}

#[derive(Clone, Debug)]
pub struct NotificationLifecycleScenario {
    pub enable: SetNotificationsCommand,
    pub disable: SetNotificationsCommand,
    pub enabled: BleEvent,
    pub disabled: BleEvent,
    pub value: BleEvent,
    pub backend_scenarios: Vec<EventScenario>,
}

pub fn notification_lifecycle() -> NotificationLifecycleScenario {
    let connection_id = ConnectionId { value: 2 };
    let gatt_session_id = GattSessionId { value: 3 };
    let subscription_id = SubscriptionId { value: 4 };
    let characteristic_id = GattCharacteristicId { value: 5 };
    let enable_id = CommandId { value: 6 };
    let disable_id = CommandId { value: 7 };
    let enable = SetNotificationsCommand {
        command_id: enable_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        action: NotificationAction::Enable {
            mode: NotificationModeRequest::PlatformDefault,
        },
    };
    let disable = SetNotificationsCommand {
        command_id: disable_id,
        action: NotificationAction::Disable,
        ..enable
    };
    let enabled = BleEvent::NotificationStateChanged {
        command_id: enable_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Enabled {
            actual_mode: Some(NotificationMode::Notification),
        },
    };
    let disabled = BleEvent::NotificationStateChanged {
        command_id: disable_id,
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        state: NotificationState::Disabled,
    };
    let value = BleEvent::NotificationReceived {
        connection_id,
        gatt_session_id,
        subscription_id,
        characteristic_id,
        value: vec![1, 2, 3],
    };
    let backend_scenarios = vec![
        EventScenario::complete(
            RecordedCommand::SetNotifications(enable),
            vec![enabled.clone(), succeeded(enable_id)],
        ),
        EventScenario::complete(
            RecordedCommand::SetNotifications(disable),
            vec![disabled.clone(), succeeded(disable_id)],
        ),
    ];
    NotificationLifecycleScenario {
        enable,
        disable,
        enabled,
        disabled,
        value,
        backend_scenarios,
    }
}

fn succeeded(command_id: CommandId) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: None,
    }
}

fn failed(command_id: CommandId) -> BleEvent {
    failed_with_category(
        command_id,
        BleErrorCategory::Backend,
        "scripted completion failure",
    )
}

fn failed_with_category(
    command_id: CommandId,
    category: BleErrorCategory,
    message: &str,
) -> BleEvent {
    BleEvent::CommandCompleted {
        command_id,
        error: Some(BleEventError {
            category,
            message: message.to_owned(),
            platform_details: None,
        }),
    }
}
