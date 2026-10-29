use std::{
    sync::{
        Arc, Barrier, Mutex, Weak,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use crate::{
    AdvertisementObservation, BLEHUB_CONTRACT_VERSION, BackendCapabilities,
    BackendEventIngressHandle, BleBackend, BleEvent, BleEventSink, BleHubError, BluetoothUuid,
    CancelOperationRequest, CharacteristicProperties, CommandDispatchError, CommandId,
    ConnectCommand, ConnectionId, DisconnectCommand, DiscoverGattCommand, EventIngressStatus,
    EventSinkError, GattCharacteristic, GattCharacteristicId, GattDatabase, GattDescriptor,
    GattDescriptorId, GattService, GattServiceId, GattSessionId, InitializeCommand,
    MAX_SCAN_NAME_PREFIX_BYTES, MAX_WRITE_VALUE_BYTES, PeripheralId, Platform,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId, ServiceData,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
    WriteType,
};

use super::BleHub;

struct ConstructorBackend {
    state: Arc<ConstructorBackendState>,
    capabilities: Result<BackendCapabilities, BleHubError>,
    bind_result: Result<(), BleHubError>,
    dispose_result: Result<(), BleHubError>,
    push_during_bind: bool,
    dispose_barriers: Option<(Arc<Barrier>, Arc<Barrier>)>,
    blocking_initialize: Option<BlockingCommand>,
}

struct BlockingCommand {
    entered: mpsc::Sender<()>,
    emit: Mutex<mpsc::Receiver<()>>,
    release: Mutex<mpsc::Receiver<()>>,
    order: Arc<Mutex<Vec<&'static str>>>,
}

struct ChannelSink(mpsc::Sender<BleEvent>);

impl BleEventSink for ChannelSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        self.0.send(event).unwrap();
        Ok(())
    }
}

struct ConstructorBackendState {
    ingress: Mutex<Option<Arc<BackendEventIngressHandle>>>,
    observed_ingress: Mutex<Option<Weak<BackendEventIngressHandle>>>,
    bind_count: AtomicUsize,
    dispatch_count: AtomicUsize,
    scan_commands: Mutex<Vec<StartScanCommand>>,
    write_commands: Mutex<Vec<WriteCharacteristicCommand>>,
    dispose_count: AtomicUsize,
    bind_push_status: Mutex<Option<EventIngressStatus>>,
}

impl ConstructorBackend {
    fn new(
        capabilities: Result<BackendCapabilities, BleHubError>,
        bind_result: Result<(), BleHubError>,
        dispose_result: Result<(), BleHubError>,
        push_during_bind: bool,
    ) -> (Self, Arc<ConstructorBackendState>) {
        let state = Arc::new(ConstructorBackendState {
            ingress: Mutex::new(None),
            observed_ingress: Mutex::new(None),
            bind_count: AtomicUsize::new(0),
            dispatch_count: AtomicUsize::new(0),
            scan_commands: Mutex::new(Vec::new()),
            write_commands: Mutex::new(Vec::new()),
            dispose_count: AtomicUsize::new(0),
            bind_push_status: Mutex::new(None),
        });
        (
            Self {
                state: Arc::clone(&state),
                capabilities,
                bind_result,
                dispose_result,
                push_during_bind,
                dispose_barriers: None,
                blocking_initialize: None,
            },
            state,
        )
    }

    fn reject_dispatch(&self) -> Result<(), CommandDispatchError> {
        self.state.dispatch_count.fetch_add(1, Ordering::AcqRel);
        Err(CommandDispatchError::InvalidState {
            detail: "constructor test backend does not dispatch commands".to_owned(),
        })
    }
}

impl BleBackend for ConstructorBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        self.capabilities.clone()
    }

    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError> {
        self.state.bind_count.fetch_add(1, Ordering::AcqRel);
        *self.state.observed_ingress.lock().unwrap() = Some(Arc::downgrade(&ingress));
        self.bind_result.clone()?;
        if self.push_during_bind {
            *self.state.bind_push_status.lock().unwrap() = Some(ingress.push(scan_started()));
        }
        *self.state.ingress.lock().unwrap() = Some(ingress);
        Ok(())
    }

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        let Some(blocking) = &self.blocking_initialize else {
            return self.reject_dispatch();
        };
        self.state.dispatch_count.fetch_add(1, Ordering::AcqRel);
        blocking.order.lock().unwrap().push("command-entered");
        blocking.entered.send(()).unwrap();
        blocking
            .emit
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        let ingress = self.state.ingress.lock().unwrap().clone().unwrap();
        assert_eq!(
            ingress.push(BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: None
            }),
            EventIngressStatus::Delivered
        );
        blocking.order.lock().unwrap().push("command-event");
        blocking
            .release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        blocking.order.lock().unwrap().push("command-exit");
        Ok(())
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.state.scan_commands.lock().unwrap().push(command);
        self.reject_dispatch()
    }

    fn stop_scan(&self, _: StopScanCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn disconnect(&self, _: DisconnectCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn discover_gatt(&self, _: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn read_characteristic(
        &self,
        _: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        self.state.write_commands.lock().unwrap().push(command);
        self.reject_dispatch()
    }

    fn set_notifications(&self, _: SetNotificationsCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn read_rssi(&self, _: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        self.reject_dispatch()
    }

    fn cancel_operation(
        &self,
        request: CancelOperationRequest,
    ) -> Result<(), CommandDispatchError> {
        Err(CommandDispatchError::OperationNotCancellable {
            target_command_id: request.target_command_id,
        })
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        if let Some(blocking) = &self.blocking_initialize {
            blocking.order.lock().unwrap().push("cleanup");
        }
        self.state.dispose_count.fetch_add(1, Ordering::AcqRel);
        if let Some((entered, release)) = &self.dispose_barriers {
            entered.wait();
            release.wait();
        }
        self.state.ingress.lock().unwrap().take();
        self.dispose_result.clone()
    }
}

struct DropTrackingSink {
    drop_count: Arc<AtomicUsize>,
}

impl BleEventSink for DropTrackingSink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        Ok(())
    }
}

impl Drop for DropTrackingSink {
    fn drop(&mut self) {
        self.drop_count.fetch_add(1, Ordering::AcqRel);
    }
}

struct BlockingSink {
    entered: Arc<Barrier>,
    release: Arc<Barrier>,
}

impl BleEventSink for BlockingSink {
    fn on_event(&self, _: BleEvent) -> Result<(), EventSinkError> {
        self.entered.wait();
        self.release.wait();
        Ok(())
    }
}

fn tracking_sink() -> (Arc<dyn BleEventSink>, Arc<AtomicUsize>) {
    let drop_count = Arc::new(AtomicUsize::new(0));
    (
        Arc::new(DropTrackingSink {
            drop_count: Arc::clone(&drop_count),
        }),
        drop_count,
    )
}

fn backend(
    capabilities: Result<BackendCapabilities, BleHubError>,
    bind_result: Result<(), BleHubError>,
    dispose_result: Result<(), BleHubError>,
) -> (Arc<ConstructorBackend>, Arc<ConstructorBackendState>) {
    let (backend, state) =
        ConstructorBackend::new(capabilities, bind_result, dispose_result, false);
    (Arc::new(backend), state)
}

fn observed_ingress(state: &ConstructorBackendState) -> Weak<BackendEventIngressHandle> {
    state.observed_ingress.lock().unwrap().clone().unwrap()
}

fn blocking_dispose_backend(
    entered: Arc<Barrier>,
    release: Arc<Barrier>,
) -> (Arc<ConstructorBackend>, Arc<ConstructorBackendState>) {
    let (mut backend, state) =
        backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    Arc::get_mut(&mut backend).unwrap().dispose_barriers = Some((entered, release));
    (backend, state)
}

fn scan_started() -> BleEvent {
    BleEvent::ScanStarted {
        command_id: CommandId { value: 1 },
        scan_id: ScanSessionId { value: 2 },
    }
}

#[test]
fn dispose_waits_for_backend_command_exit_without_closing_its_ingress_early() {
    let (entered_tx, entered_rx) = mpsc::channel();
    let (emit_tx, emit_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let (event_tx, event_rx) = mpsc::channel();
    let order = Arc::new(Mutex::new(Vec::new()));
    let (mut backend, state) =
        backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    Arc::get_mut(&mut backend).unwrap().blocking_initialize = Some(BlockingCommand {
        entered: entered_tx,
        emit: Mutex::new(emit_rx),
        release: Mutex::new(release_rx),
        order: Arc::clone(&order),
    });
    let hub = BleHub::new(backend, Arc::new(ChannelSink(event_tx))).unwrap();
    let late_ingress = state.ingress.lock().unwrap().clone().unwrap();
    let command = InitializeCommand {
        command_id: CommandId { value: 71 },
    };
    let dispatch_hub = Arc::clone(&hub);
    let dispatch = thread::spawn(move || dispatch_hub.initialize(command));
    entered_rx.recv_timeout(Duration::from_secs(5)).unwrap();

    let (disposed_tx, disposed_rx) = mpsc::channel();
    let dispose_hub = Arc::clone(&hub);
    let dispose = thread::spawn(move || disposed_tx.send(dispose_hub.dispose()).unwrap());
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let lifecycle = hub.lifecycle.0.lock().unwrap();
        if matches!(*lifecycle, super::LifecycleState::Closing) {
            break;
        }
        assert!(
            matches!(*lifecycle, super::LifecycleState::Open),
            "dispose finished before the blocked command exited"
        );
        drop(lifecycle);
        assert!(
            Instant::now() < deadline,
            "dispose never claimed cleanup ownership"
        );
        thread::yield_now();
    }
    // Closing is observed, but the entered backend command still owns a read
    // guard. Deadlines only bound failed handshakes; they do not prove ordering.
    assert!(matches!(
        hub.backend.try_write(),
        Err(std::sync::TryLockError::WouldBlock)
    ));
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);
    assert_eq!(disposed_rx.try_recv(), Err(mpsc::TryRecvError::Empty));

    emit_tx.send(()).unwrap();
    let terminal = BleEvent::CommandCompleted {
        command_id: command.command_id,
        error: None,
    };
    assert_eq!(
        event_rx.recv_timeout(Duration::from_secs(5)).unwrap(),
        terminal
    );
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);
    assert_eq!(disposed_rx.try_recv(), Err(mpsc::TryRecvError::Empty));

    release_tx.send(()).unwrap();
    assert_eq!(dispatch.join().unwrap(), Ok(()));
    assert_eq!(
        disposed_rx.recv_timeout(Duration::from_secs(5)).unwrap(),
        Ok(())
    );
    dispose.join().unwrap();
    hub.dispose().unwrap();
    assert_eq!(state.dispatch_count.load(Ordering::Acquire), 1);
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(
        *order.lock().unwrap(),
        [
            "command-entered",
            "command-event",
            "command-exit",
            "cleanup"
        ]
    );
    assert_eq!(late_ingress.push(terminal), EventIngressStatus::Closed);
    assert_eq!(event_rx.try_recv(), Err(mpsc::TryRecvError::Disconnected));
}

#[test]
fn facade_rejects_invalid_scan_and_write_without_dispatch_or_events_at_utf8_boundary() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let (event_tx, event_rx) = mpsc::channel();
    let hub = BleHub::new(backend, Arc::new(ChannelSink(event_tx))).unwrap();
    let name_at_limit = format!("{}ab", "测".repeat(82));
    assert_eq!(name_at_limit.len(), MAX_SCAN_NAME_PREFIX_BYTES);
    let name_over_limit = format!("{name_at_limit}x");
    assert_eq!(name_over_limit.len(), MAX_SCAN_NAME_PREFIX_BYTES + 1);
    assert!(name_over_limit.chars().count() < MAX_SCAN_NAME_PREFIX_BYTES);
    let assert_no_dispatch_or_events = || {
        assert_eq!(state.dispatch_count.load(Ordering::Acquire), 0);
        assert!(state.scan_commands.lock().unwrap().is_empty());
        assert!(state.write_commands.lock().unwrap().is_empty());
        assert_eq!(event_rx.try_recv(), Err(mpsc::TryRecvError::Empty));
    };

    for (index, options) in [
        ScanOptions {
            service_uuids: vec![BluetoothUuid {
                value: "not-a-uuid".to_owned(),
            }],
            ..ScanOptions::default()
        },
        ScanOptions {
            name_prefix: Some(String::new()),
            ..ScanOptions::default()
        },
        ScanOptions {
            name_prefix: Some(name_over_limit),
            ..ScanOptions::default()
        },
    ]
    .into_iter()
    .enumerate()
    {
        assert!(matches!(
            hub.start_scan(StartScanCommand {
                command_id: CommandId {
                    value: index as u64 + 1
                },
                scan_session_id: ScanSessionId {
                    value: index as u64 + 10
                },
                options,
            }),
            Err(CommandDispatchError::InvalidRequest { .. })
        ));
        assert_no_dispatch_or_events();
    }
    for (index, write_type) in [WriteType::WithResponse, WriteType::WithoutResponse]
        .into_iter()
        .enumerate()
    {
        assert!(matches!(
            hub.write_characteristic(WriteCharacteristicCommand {
                command_id: CommandId {
                    value: index as u64 + 20
                },
                connection_id: ConnectionId { value: 30 },
                gatt_session_id: GattSessionId { value: 31 },
                characteristic_id: GattCharacteristicId { value: 32 },
                value: vec![0; MAX_WRITE_VALUE_BYTES + 1],
                write_type,
            }),
            Err(CommandDispatchError::InvalidRequest { .. })
        ));
        assert_no_dispatch_or_events();
    }

    let valid_scan = StartScanCommand {
        command_id: CommandId { value: 100 },
        scan_session_id: ScanSessionId { value: 101 },
        options: ScanOptions {
            name_prefix: Some(name_at_limit),
            ..ScanOptions::default()
        },
    };
    let valid_write = WriteCharacteristicCommand {
        command_id: CommandId { value: 102 },
        connection_id: ConnectionId { value: 30 },
        gatt_session_id: GattSessionId { value: 31 },
        characteristic_id: GattCharacteristicId { value: 32 },
        value: vec![0; MAX_WRITE_VALUE_BYTES],
        write_type: WriteType::WithResponse,
    };
    let backend_rejection = CommandDispatchError::InvalidState {
        detail: "constructor test backend does not dispatch commands".to_owned(),
    };
    // These controls must reach the rejecting backend, not fail the Hub guard.
    assert_eq!(
        hub.start_scan(valid_scan.clone()),
        Err(backend_rejection.clone())
    );
    assert_eq!(
        hub.write_characteristic(valid_write.clone()),
        Err(backend_rejection)
    );
    assert_eq!(*state.scan_commands.lock().unwrap(), vec![valid_scan]);
    assert_eq!(*state.write_commands.lock().unwrap(), vec![valid_write]);
    assert_eq!(state.dispatch_count.load(Ordering::Acquire), 2);
    assert_eq!(event_rx.try_recv(), Err(mpsc::TryRecvError::Empty));
}

#[test]
fn capabilities_failure_disposes_before_binding() {
    let (backend, state) = backend(
        Err(BleHubError::BackendAssembly {
            detail: "capabilities unavailable".to_owned(),
        }),
        Ok(()),
        Ok(()),
    );
    let (sink, sink_drop_count) = tracking_sink();

    let result = BleHub::new(backend, sink);

    assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
    assert_eq!(state.bind_count.load(Ordering::Acquire), 0);
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn assembly_rollback_dispose_failure_is_reported() {
    let (backend, state) = backend(
        Err(BleHubError::BackendAssembly {
            detail: "capabilities unavailable".to_owned(),
        }),
        Ok(()),
        Err(BleHubError::BackendCleanup {
            detail: "rollback cleanup failed".to_owned(),
            platform_details: None,
        }),
    );
    let (sink, sink_drop_count) = tracking_sink();

    let result = BleHub::new(backend, sink);

    match result {
        Err(BleHubError::BackendCleanup { detail, .. }) => {
            assert!(detail.contains("capabilities"));
            assert!(detail.contains("rollback cleanup failed"));
        }
        _ => panic!("expected reported rollback cleanup error"),
    }
    assert_eq!(state.bind_count.load(Ordering::Acquire), 0);
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn bind_failure_does_not_retain_ingress_root() {
    let (backend, state) = backend(
        Ok(BackendCapabilities::new(Platform::Test)),
        Err(BleHubError::BackendAssembly {
            detail: "bind rejected".to_owned(),
        }),
        Ok(()),
    );
    let (sink, sink_drop_count) = tracking_sink();

    let result = BleHub::new(backend, sink);

    assert!(matches!(result, Err(BleHubError::BackendAssembly { .. })));
    assert!(observed_ingress(&state).upgrade().is_none());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn invalid_contract_version_rolls_back_before_binding() {
    let (backend, state) = backend(
        Ok(BackendCapabilities {
            contract_version: BLEHUB_CONTRACT_VERSION + 1,
            ..BackendCapabilities::new(Platform::Test)
        }),
        Ok(()),
        Ok(()),
    );
    let (sink, sink_drop_count) = tracking_sink();

    let result = BleHub::new(backend, sink);

    assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
    assert_eq!(state.bind_count.load(Ordering::Acquire), 0);
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn cancel_operation_preserves_target_identity_when_backend_rejects_it() {
    let (backend, _) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let hub = BleHub::new(backend, tracking_sink().0).unwrap();
    let target_command_id = CommandId { value: 42 };

    assert_eq!(
        hub.cancel_operation(target_command_id),
        Err(CommandDispatchError::OperationNotCancellable { target_command_id })
    );
}

#[test]
fn push_during_bind_is_closed_and_rejects_construction() {
    let (backend, state) = ConstructorBackend::new(
        Ok(BackendCapabilities::new(Platform::Test)),
        Ok(()),
        Ok(()),
        true,
    );
    let (sink, sink_drop_count) = tracking_sink();

    let result = BleHub::new(Arc::new(backend), sink);

    assert!(matches!(result, Err(BleHubError::FfiContract { .. })));
    assert_eq!(
        *state.bind_push_status.lock().unwrap(),
        Some(EventIngressStatus::Closed)
    );
    assert!(observed_ingress(&state).upgrade().is_none());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn successful_dispose_releases_backend_ingress_root() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let (sink, sink_drop_count) = tracking_sink();
    let hub = BleHub::new(backend, sink).unwrap();
    let ingress = observed_ingress(&state);
    assert!(ingress.upgrade().is_some());

    hub.dispose().unwrap();
    hub.dispose().unwrap();

    assert!(ingress.upgrade().is_none());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn last_hub_drop_disposes_once_and_releases_sink_and_ingress_root() {
    for cleanup_result in [
        Ok(()),
        Err(BleHubError::BackendCleanup {
            detail: "scripted Drop cleanup failure".to_owned(),
            platform_details: None,
        }),
    ] {
        let (backend, state) = backend(
            Ok(BackendCapabilities::new(Platform::Test)),
            Ok(()),
            cleanup_result,
        );
        let extra_backend = Arc::clone(&backend);
        let (sink, sink_drop_count) = tracking_sink();
        let sink_weak = Arc::downgrade(&sink);
        let hub = BleHub::new(backend, sink).unwrap();
        let last_hub = Arc::clone(&hub);
        let hub_weak = Arc::downgrade(&hub);
        let ingress_state = Arc::downgrade(&hub.ingress);
        let ingress_root = observed_ingress(&state);
        assert_eq!(
            ingress_root.upgrade().unwrap().push(scan_started()),
            EventIngressStatus::Delivered
        );

        drop(hub);
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 0);
        assert!(ingress_root.upgrade().is_some());

        drop(last_hub);

        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
        assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
        assert!(hub_weak.upgrade().is_none());
        assert!(sink_weak.upgrade().is_none());
        assert!(ingress_state.upgrade().is_none());
        assert!(ingress_root.upgrade().is_none());
        assert!(state.ingress.lock().unwrap().is_none());
        assert_eq!(Arc::strong_count(&extra_backend), 1);
        drop(extra_backend);
        assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    }
}

#[test]
fn failing_dispose_still_releases_backend_ingress_root() {
    let cleanup_error = BleHubError::BackendCleanup {
        detail: "scripted cleanup failure".to_owned(),
        platform_details: None,
    };
    let (backend, state) = backend(
        Ok(BackendCapabilities::new(Platform::Test)),
        Ok(()),
        Err(cleanup_error.clone()),
    );
    let (sink, sink_drop_count) = tracking_sink();
    let hub = BleHub::new(backend, sink).unwrap();
    let ingress = observed_ingress(&state);

    assert_eq!(hub.dispose(), Err(cleanup_error));

    assert!(ingress.upgrade().is_none());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn late_temporary_callback_is_closed_after_dispose() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let (sink, sink_drop_count) = tracking_sink();
    let hub = BleHub::new(backend, sink).unwrap();
    let callback = state.ingress.lock().unwrap().clone().unwrap();

    hub.dispose().unwrap();

    assert_eq!(callback.push(scan_started()), EventIngressStatus::Closed);
    assert_eq!(sink_drop_count.load(Ordering::Acquire), 1);
}

#[test]
fn nested_invalid_events_are_rejected_without_sink_delivery() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let (event_tx, event_rx) = mpsc::channel();
    let hub = BleHub::new(backend, Arc::new(ChannelSink(event_tx))).unwrap();
    let ingress = observed_ingress(&state).upgrade().unwrap();
    let uuid = BluetoothUuid::canonicalize("180d").unwrap();
    let advertisement = AdvertisementObservation {
        scan_session_id: ScanSessionId { value: 1 },
        peripheral_id: PeripheralId { value: 2 },
        local_name: None,
        advertised_services: vec![uuid.clone()],
        manufacturer_data: Vec::new(),
        service_data: vec![
            ServiceData {
                service_uuid: uuid.clone(),
                payload: vec![1],
            },
            ServiceData {
                service_uuid: uuid.clone(),
                payload: vec![2],
            },
        ],
        rssi: None,
        connectable: None,
    };
    let first_service = GattService {
        id: GattServiceId { value: 1 },
        uuid,
        primary: Some(true),
        characteristics: vec![GattCharacteristic {
            id: GattCharacteristicId { value: 1 },
            uuid: BluetoothUuid::canonicalize("2a37").unwrap(),
            properties: CharacteristicProperties {
                readable: true,
                writable_with_response: false,
                writable_without_response: false,
                notifiable: true,
                indicatable: false,
            },
            descriptors: vec![GattDescriptor {
                id: GattDescriptorId { value: 1 },
                uuid: BluetoothUuid::canonicalize("2902").unwrap(),
            }],
        }],
    };
    let mut second_service = first_service.clone();
    second_service.id = GattServiceId { value: 2 };
    second_service.characteristics[0].id = GattCharacteristicId { value: 2 };
    second_service.characteristics[0].descriptors[0].id = GattDescriptorId { value: 2 };
    let database = GattDatabase {
        connection_id: ConnectionId { value: 3 },
        gatt_session_id: GattSessionId { value: 4 },
        services: vec![first_service, second_service],
    };
    let gatt_event = |database| BleEvent::GattDiscovered {
        command_id: CommandId { value: 5 },
        database,
    };
    let mut invalid_events = Vec::new();
    for invalid_uuid in [
        "",
        "180D",
        "0000180D-0000-1000-8000-00805F9B34FB",
        "not-a-uuid",
    ] {
        let invalid_uuid = BluetoothUuid {
            value: invalid_uuid.to_owned(),
        };
        let mut invalid_advertisement = advertisement.clone();
        invalid_advertisement.service_data[1].service_uuid = invalid_uuid.clone();
        invalid_events.push((
            BleEvent::AdvertisementObserved {
                advertisement: invalid_advertisement,
            },
            "service_data[1].service_uuid",
        ));
        for (level, path) in [
            (0, "services[1].uuid"),
            (1, "services[1].characteristics[0].uuid"),
            (2, "services[1].characteristics[0].descriptors[0].uuid"),
        ] {
            let mut invalid_database = database.clone();
            let service = &mut invalid_database.services[1];
            match level {
                0 => service.uuid = invalid_uuid.clone(),
                1 => service.characteristics[0].uuid = invalid_uuid.clone(),
                2 => service.characteristics[0].descriptors[0].uuid = invalid_uuid.clone(),
                _ => unreachable!(),
            }
            invalid_events.push((gatt_event(invalid_database), path));
        }
    }
    for (level, path) in [
        (0, "services[1].characteristics[0].id"),
        (1, "services[1].characteristics[0].descriptors[0].id"),
    ] {
        let mut invalid_database = database.clone();
        let first = &database.services[0].characteristics[0];
        let second = &mut invalid_database.services[1].characteristics[0];
        match level {
            0 => second.id = first.id,
            1 => second.descriptors[0].id = first.descriptors[0].id,
            _ => unreachable!(),
        }
        invalid_events.push((gatt_event(invalid_database), path));
    }

    for (event, path) in invalid_events {
        let status = ingress.push(event.clone());
        assert!(
            matches!(&status, EventIngressStatus::InvalidEvent { message } if message.contains(path)),
            "expected InvalidEvent at {path}, got {status:?} for {event:?}"
        );
        assert_eq!(event_rx.try_recv(), Err(mpsc::TryRecvError::Empty));
    }

    // Rejection must neither close ingress nor forbid duplicate UUID instances with distinct IDs.
    for event in [
        BleEvent::AdvertisementObserved { advertisement },
        gatt_event(database),
    ] {
        assert_eq!(ingress.push(event.clone()), EventIngressStatus::Delivered);
        assert_eq!(
            event_rx.recv_timeout(Duration::from_secs(5)).unwrap(),
            event
        );
    }
    assert_eq!(event_rx.try_recv(), Err(mpsc::TryRecvError::Empty));
    hub.dispose().unwrap();
}

#[test]
fn dispose_releases_command_gate_before_waiting_for_ingress_drain() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let hub = BleHub::new(
        backend,
        Arc::new(BlockingSink {
            entered: Arc::clone(&entered),
            release: Arc::clone(&release),
        }),
    )
    .unwrap();
    let callback = state.ingress.lock().unwrap().clone().unwrap();
    let push = thread::spawn(move || callback.push(scan_started()));
    entered.wait();

    let dispose_hub = Arc::clone(&hub);
    let dispose = thread::spawn(move || dispose_hub.dispose());
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if let Ok(backend_slot) = hub.backend.try_read()
            && backend_slot.is_none()
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "dispose did not release the command gate after closing the backend slot"
        );
        thread::yield_now();
    }

    let command = StartScanCommand {
        command_id: CommandId { value: 3 },
        scan_session_id: ScanSessionId { value: 4 },
        options: ScanOptions {
            service_uuids: Vec::new(),
            name_prefix: None,
            company_id: None,
            allow_duplicates: false,
        },
    };
    assert_eq!(hub.start_scan(command), Err(CommandDispatchError::Closed));
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);

    release.wait();
    assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
    assert_eq!(dispose.join().unwrap(), Ok(()));
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
}

#[test]
fn concurrent_dispose_waits_for_the_first_cleanup() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let hub = BleHub::new(
        backend,
        Arc::new(BlockingSink {
            entered: Arc::clone(&entered),
            release: Arc::clone(&release),
        }),
    )
    .unwrap();
    let callback = state.ingress.lock().unwrap().clone().unwrap();
    let push = thread::spawn(move || callback.push(scan_started()));
    entered.wait();

    let first_hub = Arc::clone(&hub);
    let first = thread::spawn(move || first_hub.dispose());
    let second_hub = Arc::clone(&hub);
    let (done_tx, done_rx) = mpsc::channel();
    let second = thread::spawn(move || {
        let result = second_hub.dispose();
        done_tx.send(result).unwrap();
    });

    assert!(done_rx.recv_timeout(Duration::from_millis(50)).is_err());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 0);

    release.wait();
    assert_eq!(push.join().unwrap(), EventIngressStatus::Delivered);
    assert_eq!(first.join().unwrap(), Ok(()));
    assert_eq!(
        done_rx.recv_timeout(Duration::from_secs(1)).unwrap(),
        Ok(())
    );
    second.join().unwrap();
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
}

#[test]
fn concurrent_dispose_waits_during_backend_cleanup() {
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let (backend, state) = blocking_dispose_backend(Arc::clone(&entered), Arc::clone(&release));
    let hub = BleHub::new(
        backend,
        Arc::new(DropTrackingSink {
            drop_count: Arc::new(AtomicUsize::new(0)),
        }),
    )
    .unwrap();
    let first_hub = Arc::clone(&hub);
    let first = thread::spawn(move || first_hub.dispose());
    entered.wait();
    let second_hub = Arc::clone(&hub);
    let (done_tx, done_rx) = mpsc::channel();
    let second = thread::spawn(move || done_tx.send(second_hub.dispose()).unwrap());
    assert!(done_rx.recv_timeout(Duration::from_millis(50)).is_err());
    assert_eq!(state.dispose_count.load(Ordering::Acquire), 1);
    release.wait();
    assert_eq!(first.join().unwrap(), Ok(()));
    assert_eq!(
        done_rx.recv_timeout(Duration::from_secs(1)).unwrap(),
        Ok(())
    );
    second.join().unwrap();
}

#[test]
fn multiple_close_observers_receive_the_first_cleanup_error() {
    let cleanup_error = BleHubError::BackendCleanup {
        detail: "first cleanup failure".to_owned(),
        platform_details: None,
    };
    let (backend, _) = backend(
        Ok(BackendCapabilities::new(Platform::Test)),
        Ok(()),
        Err(cleanup_error.clone()),
    );
    let hub = BleHub::new(
        backend,
        Arc::new(DropTrackingSink {
            drop_count: Arc::new(AtomicUsize::new(0)),
        }),
    )
    .unwrap();
    assert_eq!(hub.dispose(), Err(cleanup_error.clone()));
    let observers: Vec<_> = (0..4)
        .map(|_| {
            let hub = Arc::clone(&hub);
            thread::spawn(move || hub.dispose())
        })
        .collect();
    for observer in observers {
        assert_eq!(observer.join().unwrap(), Err(cleanup_error.clone()));
    }
}

#[test]
fn extra_backend_arc_does_not_keep_ingress_root_after_dispose() {
    let (backend, state) = backend(Ok(BackendCapabilities::new(Platform::Test)), Ok(()), Ok(()));
    let extra_backend = Arc::clone(&backend);
    let (sink, _) = tracking_sink();
    let hub = BleHub::new(backend, sink).unwrap();
    let ingress = observed_ingress(&state);

    hub.dispose().unwrap();

    assert!(ingress.upgrade().is_none());
    assert_eq!(Arc::strong_count(&extra_backend), 1);
}
