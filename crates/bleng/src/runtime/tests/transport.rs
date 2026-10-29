use super::support::{
    Arc, BackendCapabilities, BackendEventIngressHandle, BleBackend, BleHub, BleHubError,
    CancelOperationRequest, CommandDispatchError, ConnectCommand, DisconnectCommand,
    DiscoverGattCommand, Duration, EventIngressStatus, EventReadError, EventScenario, FakeBackend,
    InitializeCommand, Mutex, Notify, ReadCharacteristicCommand, ReadRssiCommand, RecordedCommand,
    RuntimeOwner, ScanOptions, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    WaiterError, WriteCharacteristicCommand, connect_scenario, connected_event, event,
    event_with_message, mpsc, rssi_command, rssi_terminal, rssi_value,
};
use crate::runtime::messages::{BeginConnect, BeginRssi, BeginScan};
use std::{
    future::{Future, poll_fn},
    sync::atomic::{AtomicUsize, Ordering},
    task::Poll,
};

// Legal production backends must not panic. This injects a Rust unwind at the
// foreign dispatch boundary to test retirement, not recovery from arbitrary FFI panics.
struct PanickingDispatchBackend {
    inner: FakeBackend,
    panic_entered: Arc<Notify>,
    panic_release: Mutex<mpsc::Receiver<()>>,
    rssi_entered: Arc<Notify>,
    rssi_release: Mutex<mpsc::Receiver<()>>,
    rssi_returning: Arc<Notify>,
    dispose_calls: Arc<AtomicUsize>,
}

impl BleBackend for PanickingDispatchBackend {
    delegate::delegate! {
        to self.inner {
            fn capabilities(&self) -> Result<BackendCapabilities, BleHubError>;
            fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
            fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
            fn cancel_operation(&self, request: CancelOperationRequest) -> Result<(), CommandDispatchError>;
        }
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.inner.start_scan(command)?;
        self.panic_entered.notify_one();
        self.panic_release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .expect("panic dispatch gate was not released");
        panic!("injected foreign dispatch panic");
    }

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        // Hold the acknowledgement after success callbacks, so late acceptance
        // would complete the RSSI waiter if retirement were incorrectly reversed.
        let result = self.inner.read_rssi(command);
        self.rssi_entered.notify_one();
        self.rssi_release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .expect("RSSI dispatch gate was not released");
        self.rssi_returning.notify_one();
        result
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.dispose_calls.fetch_add(1, Ordering::SeqCst);
        self.inner.dispose()
    }
}

#[test]
fn foreign_dispatch_panic_retires_all_pending_and_reader_without_worker_restart() {
    let owner = RuntimeOwner::new_assuming_initialized().unwrap();
    let scan_command = RecordedCommand::StartScan(StartScanCommand {
        command_id: 3.into(),
        scan_session_id: 2.into(),
        options: ScanOptions::default(),
    });
    let pending_connect = RecordedCommand::Connect(ConnectCommand {
        command_id: 4.into(),
        connection_id: 3.into(),
        peripheral_id: 20.into(),
    });
    let (backend, handle) = FakeBackend::scripted([
        connect_scenario(1, 1, 10),
        EventScenario::emit_before_return(
            rssi_command(2, 1),
            vec![rssi_value(2, 1, -47), rssi_terminal(2)],
        ),
        EventScenario::accept(scan_command.clone()),
        EventScenario::accept(pending_connect.clone()),
    ]);
    let panic_entered = Arc::new(Notify::new());
    let rssi_entered = Arc::new(Notify::new());
    let rssi_returning = Arc::new(Notify::new());
    let dispose_calls = Arc::new(AtomicUsize::new(0));
    let (panic_release, panic_rx) = mpsc::channel();
    let (rssi_release, rssi_rx) = mpsc::channel();
    // An assertion failure must not strand this test's blocking workers.
    let panic_release = scopeguard::guard(panic_release, |tx| {
        let _ = tx.send(());
    });
    let rssi_release = scopeguard::guard(rssi_release, |tx| {
        let _ = tx.send(());
    });
    let backend = PanickingDispatchBackend {
        inner: backend,
        panic_entered: Arc::clone(&panic_entered),
        panic_release: Mutex::new(panic_rx),
        rssi_entered: Arc::clone(&rssi_entered),
        rssi_release: Mutex::new(rssi_rx),
        rssi_returning: Arc::clone(&rssi_returning),
        dispose_calls: Arc::clone(&dispose_calls),
    };
    owner
        .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
        .unwrap();
    let late = handle.ingress_for_callback().unwrap();
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let bound = Duration::from_secs(2);
        tokio::time::timeout(bound, async {
            assert_eq!(owner.connect(10.into(), bound).await, Ok(1.into()));
            for expected in [
                connected_event(1, 1),
                super::support::command_completed(1, None),
            ] {
                assert_eq!(owner.next_event(bound).await, Ok(Some(expected)));
            }
            let rssi = owner
                .begin_operation(|submission| BeginRssi {
                    submission,
                    connection: 1.into(),
                })
                .unwrap();
            rssi_entered.notified().await;
            let scan = owner
                .begin_operation(|submission| BeginScan {
                    submission,
                    options: ScanOptions::default(),
                })
                .unwrap();
            panic_entered.notified().await;
            let connecting = owner
                .begin_operation(|submission| BeginConnect {
                    submission,
                    peripheral_id: 20.into(),
                })
                .unwrap();
            while handle.commands().len() != 4 {
                tokio::task::yield_now().await;
            }
            let commands = handle.commands();
            assert_eq!(commands[2..], [scan_command, pending_connect]);

            let rssi_slot = rssi.result_slot();
            let scan_slot = scan.result_slot();
            let connect_slot = connecting.result_slot();
            let rssi_wait = rssi_slot.wait(bound);
            let scan_wait = scan_slot.wait(bound);
            let connect_wait = connect_slot.wait(bound);
            let reader = owner.next_event(bound);
            tokio::pin!(rssi_wait, scan_wait, connect_wait, reader);
            poll_fn(|cx| {
                assert!(rssi_wait.as_mut().poll(cx).is_pending());
                assert!(scan_wait.as_mut().poll(cx).is_pending());
                assert!(connect_wait.as_mut().poll(cx).is_pending());
                assert!(reader.as_mut().poll(cx).is_pending());
                Poll::Ready(())
            })
            .await;
            assert!(!owner.faulted_for_test());
            panic_release.send(()).unwrap();
            let (rssi_result, scan_result, connect_result, read_result) =
                tokio::join!(rssi_wait, scan_wait, connect_wait, reader);
            assert_eq!(rssi_result, Err(WaiterError::TransportFault));
            assert_eq!(scan_result, Err(WaiterError::TransportFault));
            assert_eq!(connect_result, Err(WaiterError::TransportFault));
            assert_eq!(read_result, Err(EventReadError::Fault));
            assert!(owner.faulted_for_test());
            // BleHub disposal waits for in-flight foreign calls to leave its
            // command gate. Caller retirement must not wait for this worker.
            assert_eq!(dispose_calls.load(Ordering::SeqCst), 0);
            rssi_release.send(()).unwrap();
            rssi_returning.notified().await;
            while handle.dispose_count() == 0 {
                tokio::task::yield_now().await;
            }
            assert_eq!(dispose_calls.load(Ordering::SeqCst), 1);
            assert_eq!(late.push(rssi_value(2, 1, -47)), EventIngressStatus::Closed);
            assert_eq!(late.push(rssi_terminal(2)), EventIngressStatus::Closed);
            assert_eq!(
                owner.start_scan(ScanOptions::default(), bound).await,
                Err(WaiterError::TransportFault)
            );
            assert_eq!(
                owner.initialize(bound).await,
                Err(WaiterError::TransportFault)
            );
            assert_eq!(owner.next_event(bound).await, Err(EventReadError::Fault));
            assert_eq!(late.push(event()), EventIngressStatus::Closed);
            assert_eq!(
                handle.commands(),
                commands,
                "fault must not restart dispatch"
            );
            assert!(owner.faulted_for_test());
            assert_eq!(owner.close().await, Ok(()));
            assert_eq!(owner.close().await, Ok(()));
        })
        .await
        .expect("foreign dispatch panic retirement exceeded its bound");
    });
    drop(owner);
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(dispose_calls.load(Ordering::SeqCst), 1);
}

#[test]
fn concurrent_event_readers_consume_one_shared_fifo_without_duplication() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), owner.event_sink()).unwrap();
    owner.attach_hub(hub).unwrap();
    let first = event_with_message("first");
    let second = event_with_message("second");
    assert_eq!(
        backend_handle.emit(first.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        backend_handle.emit(second.clone()),
        EventIngressStatus::Delivered
    );

    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let (first_result, second_result) = tokio::join!(
            owner.next_event(Duration::from_secs(2)),
            owner.next_event(Duration::from_secs(2))
        );
        let received = [
            first_result.unwrap().unwrap(),
            second_result.unwrap().unwrap(),
        ];
        assert!(received.contains(&first));
        assert!(received.contains(&second));
        owner.close().await.unwrap();
    });
    assert_eq!(backend_handle.dispose_count(), 1);
}

#[test]
fn real_hub_ingress_reaches_bounded_actor_transport_and_closes() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let sink = owner.event_sink();
    let hub = BleHub::new(Arc::new(backend), Arc::clone(&sink)).unwrap();
    let late = backend_handle.ingress_for_callback().unwrap();
    owner.attach_hub(hub).unwrap();
    assert_eq!(backend_handle.emit(event()), EventIngressStatus::Delivered);
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(event()))
        );
        owner.close().await.unwrap();
        assert_eq!(owner.next_event(Duration::from_secs(2)).await, Ok(None));
    });
    assert!(sink.on_event(event()).is_err());
    assert!(
        !owner.faulted_for_test(),
        "normal close must not latch transport fault"
    );
    assert_eq!(late.push(event()), EventIngressStatus::Closed);
    assert_eq!(backend_handle.dispose_count(), 1);
}
