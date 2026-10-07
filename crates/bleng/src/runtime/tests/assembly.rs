use super::support::*;
use crate::runtime::AssemblyError;
use blehub::{BLEHUB_CONTRACT_VERSION, Platform};
use std::sync::mpsc::{self, Receiver, SyncSender};

#[test]
fn owned_assembly_routes_production_hub_events_into_the_runtime() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([
        EventScenario::emit_before_return(
            init_command(1),
            vec![init_snapshot_event(Some(1)), command_completed(1, None)],
        ),
        EventScenario::emit_before_return(
            RecordedCommand::StartScan(StartScanCommand {
                command_id: CommandId { value: 2 },
                scan_session_id: ScanSessionId { value: 3 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 3 },
                },
                command_completed(2, None),
            ],
        ),
    ]);
    owner
        .assembly_port()
        .assemble_backend(Arc::new(backend))
        .unwrap();
    let late = handle.ingress_for_callback().unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_secs(2)).await,
            Ok(init_snapshot())
        );
        assert_eq!(
            owner
                .start_scan(ScanOptions::default(), Duration::from_secs(2))
                .await,
            Ok(ScanSessionId { value: 3 })
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.commands().len(), 2);
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(
        late.push(command_completed(2, None)),
        EventIngressStatus::Closed
    );
}

#[test]
fn rejected_owned_assembly_rolls_back_without_attaching_a_half_hub() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted_with_capabilities(
        BackendCapabilities {
            contract_version: BLEHUB_CONTRACT_VERSION + 1,
            ..BackendCapabilities::new(Platform::Test)
        },
        [],
    );
    assert!(matches!(
        owner.assembly_port().assemble_backend(Arc::new(backend)),
        Err(AssemblyError::Backend(BleHubError::FfiContract { .. }))
    ));
    assert!(handle.ingress_for_callback().is_none());
    assert_eq!(handle.dispose_count(), 1);
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.initialize(Duration::from_millis(20)).await,
            Err(WaiterError::BackendNotAssembled)
        );
        owner.close().await.unwrap();
    });
    assert_eq!(handle.dispose_count(), 1);
}

struct Gate {
    entered: SyncSender<()>,
    release: Mutex<Receiver<()>>,
}

impl Gate {
    fn wait(&self) {
        self.entered.send(()).unwrap();
        self.release
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(3))
            .expect("blocked backend was not released");
    }
}

struct GatedAssemblyBackend {
    inner: FakeBackend,
    capabilities_gate: Option<Gate>,
    dispose_gate: Option<Gate>,
}

impl BleBackend for GatedAssemblyBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        if let Some(gate) = &self.capabilities_gate {
            gate.wait();
        }
        self.inner.capabilities()
    }

    delegate::delegate! {
        to self.inner {
            fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
            fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;
            fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
            fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
        }
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        if let Some(gate) = &self.dispose_gate {
            gate.wait();
        }
        self.inner.dispose()
    }
}

#[test]
fn close_waits_for_admitted_foreign_attach_and_rejects_late_assembly() {
    let owner = RuntimeOwner::new().unwrap();
    let port = owner.assembly_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered,
                release: Mutex::new(resume),
            }),
            dispose_gate: None,
        }))
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();

    let (duplicate, _) = FakeBackend::scripted([]);
    assert_eq!(
        port.assemble_backend(Arc::new(duplicate)),
        Err(AssemblyError::Admission(
            AssemblyAdmissionError::AdditionalHubRejected
        ))
    );
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(20), owner.close())
                .await
                .is_err()
        );
    });
    let (late_backend, late_handle) = FakeBackend::scripted([]);
    assert_eq!(
        port.assemble_backend(Arc::new(late_backend)),
        Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
    );
    assert!(late_handle.ingress_for_callback().is_none());
    release.send(()).unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close()).await,
            Ok(Ok(()))
        );
    });
    assert_eq!(
        attach.join().unwrap(),
        Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
    );
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(late_handle.dispose_count(), 0);
}

#[test]
fn assembly_rejects_hub_when_phase_closes_before_cleanup_starts() {
    let owner = RuntimeOwner::new().unwrap();
    let port = owner.assembly_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered,
                release: Mutex::new(resume),
            }),
            dispose_gate: None,
        }))
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    port.status.close();
    release.send(()).unwrap();
    assert_eq!(
        attach.join().unwrap(),
        Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
    );
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close()).await,
            Ok(Ok(()))
        );
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn transport_fault_waits_for_admitted_foreign_attach() {
    let owner = RuntimeOwner::new().unwrap();
    let port = owner.assembly_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered,
                release: Mutex::new(resume),
            }),
            dispose_gate: None,
        }))
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    port.status.fault();
    let (late_backend, late_handle) = FakeBackend::scripted([]);
    assert_eq!(
        port.assemble_backend(Arc::new(late_backend)),
        Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
    );
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(20), owner.close())
                .await
                .is_err()
        );
    });
    release.send(()).unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close()).await,
            Ok(Ok(()))
        );
    });
    assert_eq!(
        attach.join().unwrap(),
        Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
    );
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(late_handle.dispose_count(), 0);
}

#[test]
fn cancelled_close_waiter_does_not_cancel_owner_shutdown() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    owner
        .assembly_port()
        .assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: None,
            dispose_gate: Some(Gate {
                entered,
                release: Mutex::new(resume),
            }),
        }))
        .unwrap();
    let mut completed = owner.shutdown_result.subscribe();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(20), owner.close())
                .await
                .is_err()
        );
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(handle.dispose_count(), 0);
    release.send(()).unwrap();
    host.block_on(async {
        tokio::time::timeout(Duration::from_secs(2), async {
            while completed.borrow_and_update().is_none() {
                completed.changed().await.unwrap();
            }
        })
        .await
        .unwrap();
        assert_eq!(completed.borrow().clone(), Some(Ok(())));
        assert_eq!(owner.close().await, Ok(()));
    });
    assert!(owner.is_closed());
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn cancelled_assembly_waiter_leaves_admitted_attach_owned_by_close() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert!(
            tokio::time::timeout(
                Duration::from_millis(20),
                owner.assemble_backend(Arc::new(GatedAssemblyBackend {
                    inner: backend,
                    capabilities_gate: Some(Gate {
                        entered,
                        release: Mutex::new(resume),
                    }),
                    dispose_gate: None,
                }))
            )
            .await
            .is_err()
        );
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    let (duplicate, duplicate_handle) = FakeBackend::scripted([]);
    host.block_on(async {
        assert_eq!(
            owner.assemble_backend(Arc::new(duplicate)).await,
            Err(AssemblyError::Admission(
                AssemblyAdmissionError::AdditionalHubRejected
            ))
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(20), owner.close())
                .await
                .is_err()
        );
    });
    release.send(()).unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close()).await,
            Ok(Ok(()))
        );
    });
    assert_eq!(handle.dispose_count(), 1);
    assert_eq!(duplicate_handle.dispose_count(), 0);
}

#[test]
fn close_waits_for_foreign_assembly_failure_rollback() {
    let owner = RuntimeOwner::new().unwrap();
    let port = owner.assembly_port();
    let (backend, handle) = FakeBackend::scripted_with_capabilities(
        BackendCapabilities {
            contract_version: BLEHUB_CONTRACT_VERSION + 1,
            ..BackendCapabilities::new(Platform::Test)
        },
        [],
    );
    let (entered, observed) = mpsc::sync_channel(1);
    let (release, resume) = mpsc::sync_channel(1);
    let attach = std::thread::spawn(move || {
        port.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: None,
            dispose_gate: Some(Gate {
                entered,
                release: Mutex::new(resume),
            }),
        }))
    });
    observed.recv_timeout(Duration::from_secs(2)).unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert!(
            tokio::time::timeout(Duration::from_millis(20), owner.close())
                .await
                .is_err()
        );
    });
    assert_eq!(handle.dispose_count(), 0);
    release.send(()).unwrap();
    host.block_on(async {
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), owner.close()).await,
            Ok(Ok(()))
        );
    });
    assert!(matches!(
        attach.join().unwrap(),
        Err(AssemblyError::Backend(BleHubError::FfiContract { .. }))
    ));
    assert_eq!(handle.dispose_count(), 1);
}
