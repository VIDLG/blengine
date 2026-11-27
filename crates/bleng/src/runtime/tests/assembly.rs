use super::support::{
    Arc, AssemblyAdmissionError, BackendCapabilities, BackendEventIngressHandle, BleBackend,
    BleEvent, BleHubError, CancelOperationRequest, CommandDispatchError, CommandId, ConnectCommand,
    DisconnectCommand, DiscoverGattCommand, Duration, EventIngressStatus, EventScenario,
    FakeBackend, InitializeCommand, Mutex, ReadCharacteristicCommand, ReadRssiCommand,
    RecordedCommand, RuntimeOwner, ScanOptions, ScanSessionId, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, WaiterError, WriteCharacteristicCommand, command_completed,
    init_command, init_snapshot, init_snapshot_event,
};
use crate::runtime::{AssemblyError, RuntimeLifecycle};
use blehub::{BLEHUB_CONTRACT_VERSION, Platform};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    mpsc::{self, Receiver, SyncSender},
};

#[test]
fn lifecycle_rejects_admission_after_close_or_fault_without_running_foreign_work() {
    for faulted in [false, true] {
        let lifecycle = RuntimeLifecycle::new();
        if faulted {
            lifecycle.fault();
        } else {
            // Exercise the phase gate before cleanup itself has started.
            lifecycle.mark_closed();
        }
        let result: Result<(), AssemblyAdmissionError> = lifecycle.with_admission(|_| {
            panic!("foreign assembly must not run after close or fault");
        });
        assert_eq!(result, Err(AssemblyAdmissionError::Closed));
    }
}

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
                scan_session_id: ScanSessionId { value: 1 },
                options: ScanOptions::default(),
            }),
            vec![
                BleEvent::ScanStarted {
                    command_id: CommandId { value: 2 },
                    scan_id: ScanSessionId { value: 1 },
                },
                command_completed(2, None),
            ],
        ),
    ]);
    owner
        .backend_port()
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
            Ok(ScanSessionId { value: 1 })
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
        owner.backend_port().assemble_backend(Arc::new(backend)),
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
        let (replacement, replacement_handle) = FakeBackend::scripted([]);
        owner.assemble_backend(Arc::new(replacement)).await.unwrap();
        owner.close().await.unwrap();
        owner.close().await.unwrap();
        assert_eq!(replacement_handle.dispose_count(), 1);
    });
    assert_eq!(handle.dispose_count(), 1);
}

#[derive(Clone, Copy, Debug)]
enum AssemblyFailure {
    RollbackError,
    CapabilitiesPanic,
    RollbackPanic,
}

struct FailingAssemblyBackend {
    inner: FakeBackend,
    gate: Gate,
    failure: AssemblyFailure,
    dispose_attempts: Arc<AtomicUsize>,
}

impl BleBackend for FailingAssemblyBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        if matches!(self.failure, AssemblyFailure::CapabilitiesPanic) {
            self.gate.wait();
            panic!("controlled assembly capabilities panic");
        }
        Err(BleHubError::FfiContract {
            detail: "controlled assembly rejection".to_owned(),
        })
    }

    delegate::delegate! {
        to self.inner {
            fn bind_event_ingress(&self, ingress: Arc<BackendEventIngressHandle>) -> Result<(), BleHubError>;
            fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError>;
            fn set_adapter_enabled(&self, command: blehub::SetAdapterEnabledCommand) -> Result<(), CommandDispatchError>;
            fn query_devices(&self, command: blehub::QueryDevicesCommand) -> Result<(), CommandDispatchError>;
            fn associate_companion_device(&self, command: blehub::AssociateCompanionDeviceCommand) -> Result<(), CommandDispatchError>;
            fn refresh_gatt_cache(&self, command: blehub::RefreshGattCacheCommand) -> Result<(), CommandDispatchError>;
            fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
            fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
            fn mtu(&self, command: blehub::MtuCommand) -> Result<(), CommandDispatchError>;
            fn set_connection_priority(&self, command: blehub::SetConnectionPriorityCommand) -> Result<(), CommandDispatchError>;
            fn create_bond(&self, command: blehub::CreateBondCommand) -> Result<(), CommandDispatchError>;
            fn remove_bond(&self, command: blehub::RemoveBondCommand) -> Result<(), CommandDispatchError>;
            fn read_descriptor(&self, command: blehub::ReadDescriptorCommand) -> Result<(), CommandDispatchError>;
            fn write_descriptor(&self, command: blehub::WriteDescriptorCommand) -> Result<(), CommandDispatchError>;
            fn cancel_operation(&self, request: CancelOperationRequest) -> Result<(), CommandDispatchError>;
        }
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.dispose_attempts.fetch_add(1, Ordering::SeqCst);
        self.gate.wait();
        if matches!(self.failure, AssemblyFailure::RollbackPanic) {
            panic!("controlled assembly rollback panic");
        }
        Err(BleHubError::BackendCleanup {
            detail: "controlled rollback cleanup failure".to_owned(),
            platform_details: None,
        })
    }
}

fn assert_failed_assembly_is_retained(
    failure: AssemblyFailure,
    drop_waiter: bool,
    concurrent_close: bool,
) {
    use crate::{cleanup::CleanupError, runtime::RuntimePhase};
    use std::{future::Future, task::Poll};

    let owner = RuntimeOwner::new().unwrap();
    let (backend, _) = FakeBackend::scripted([]);
    let dispose_attempts = Arc::new(AtomicUsize::new(0));
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    // Release the bounded native gate even if an assertion fails.
    let release = scopeguard::guard(release_tx, |sender| {
        let _ = sender.try_send(());
    });
    let backend = Arc::new(FailingAssemblyBackend {
        inner: backend,
        gate: Gate {
            entered_tx,
            release_rx: Mutex::new(release_rx),
        },
        failure,
        dispose_attempts: Arc::clone(&dispose_attempts),
    });
    let host = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        // Use the real async owner entry: dropping this future detaches its
        // running spawn_blocking job, rather than catching a synchronous panic.
        let mut assembly = Some(Box::pin(owner.assemble_backend(backend)));
        assert!(std::future::poll_fn(|cx| {
            Poll::Ready(assembly.as_mut().unwrap().as_mut().poll(cx))
        })
        .await
        .is_pending());
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        if drop_waiter {
            drop(assembly.take());
        }
        if concurrent_close {
            assert!(
                tokio::time::timeout(Duration::from_millis(20), owner.close())
                    .await
                    .is_err(),
                "close must wait for the admitted assembly"
            );
        }
        release.try_send(()).unwrap();
        let assembly_result = if let Some(assembly) = assembly {
            Some(tokio::time::timeout(Duration::from_secs(2), assembly).await.unwrap())
        } else {
            None
        };
        let expected_cleanup = match failure {
            AssemblyFailure::RollbackError => {
                // The canonical assembly error includes both the primary
                // rejection and the cleanup diagnostic, and must survive close.
                let error = if let Some(result) = assembly_result {
                    let Err(AssemblyError::Backend(error @ BleHubError::BackendCleanup { .. })) = result else {
                        panic!("unexpected assembly result: {result:?}");
                    };
                    error
                } else {
                    BleHubError::BackendCleanup {
                        detail: format!(
                            "assembly rollback at capabilities failed after `{}`: `{}`",
                            BleHubError::FfiContract {
                                detail: "controlled assembly rejection".to_owned(),
                            },
                            BleHubError::BackendCleanup {
                                detail: "controlled rollback cleanup failure".to_owned(),
                                platform_details: None,
                            },
                        ),
                        platform_details: None,
                    }
                };
                CleanupError::Backend(error)
            }
            AssemblyFailure::CapabilitiesPanic | AssemblyFailure::RollbackPanic => {
                if let Some(result) = assembly_result {
                    assert_eq!(result, Err(AssemblyError::WorkerStopped));
                }
                if !concurrent_close {
                    tokio::time::timeout(Duration::from_secs(2), async {
                        while owner.lifecycle.phase() == RuntimePhase::Open {
                            tokio::task::yield_now().await;
                        }
                    })
                    .await
                    .expect("detached assembly panic must fault its owner");
                    assert_eq!(owner.lifecycle.phase(), RuntimePhase::Faulted);
                }
                CleanupError::WorkerStopped
            }
        };
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), owner.close()).await.unwrap(),
                Err(expected_cleanup.clone()),
                "failure={failure:?}, drop_waiter={drop_waiter}, concurrent_close={concurrent_close}"
            );
        }
        let (replacement, replacement_handle) = FakeBackend::scripted([]);
        assert_eq!(
            owner.assemble_backend(Arc::new(replacement)).await,
            Err(AssemblyError::Admission(AssemblyAdmissionError::Closed))
        );
        assert_eq!(replacement_handle.dispose_count(), 0);
    });
    assert_eq!(
        dispose_attempts.load(Ordering::SeqCst),
        if matches!(failure, AssemblyFailure::CapabilitiesPanic) {
            0
        } else {
            1
        },
        "no implicit native retry"
    );
}

#[test]
fn assembly_rollback_failure_survives_retained_waiter_and_concurrent_close() {
    for concurrent_close in [false, true] {
        assert_failed_assembly_is_retained(AssemblyFailure::RollbackError, false, concurrent_close);
    }
}

#[test]
fn assembly_rollback_failure_survives_dropped_waiter_and_concurrent_close() {
    for concurrent_close in [false, true] {
        assert_failed_assembly_is_retained(AssemblyFailure::RollbackError, true, concurrent_close);
    }
}

#[test]
fn assembly_panic_survives_retained_waiter_and_concurrent_close() {
    for failure in [
        AssemblyFailure::CapabilitiesPanic,
        AssemblyFailure::RollbackPanic,
    ] {
        for concurrent_close in [false, true] {
            assert_failed_assembly_is_retained(failure, false, concurrent_close);
        }
    }
}

#[test]
fn assembly_panic_survives_dropped_waiter_and_concurrent_close() {
    for failure in [
        AssemblyFailure::CapabilitiesPanic,
        AssemblyFailure::RollbackPanic,
    ] {
        for concurrent_close in [false, true] {
            assert_failed_assembly_is_retained(failure, true, concurrent_close);
        }
    }
}

struct Gate {
    entered_tx: SyncSender<()>,
    release_rx: Mutex<Receiver<()>>,
}

impl Gate {
    fn wait(&self) {
        self.entered_tx.send(()).unwrap();
        self.release_rx
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
            fn set_adapter_enabled(&self, command: blehub::SetAdapterEnabledCommand) -> Result<(), CommandDispatchError>;
            fn query_devices(&self, command: blehub::QueryDevicesCommand) -> Result<(), CommandDispatchError>;
            fn associate_companion_device(&self, command: blehub::AssociateCompanionDeviceCommand) -> Result<(), CommandDispatchError>;
            fn refresh_gatt_cache(&self, command: blehub::RefreshGattCacheCommand) -> Result<(), CommandDispatchError>;
            fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError>;
            fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError>;
            fn connect(&self, command: ConnectCommand) -> Result<(), CommandDispatchError>;
            fn disconnect(&self, command: DisconnectCommand) -> Result<(), CommandDispatchError>;
            fn discover_gatt(&self, command: DiscoverGattCommand) -> Result<(), CommandDispatchError>;
            fn read_characteristic(&self, command: ReadCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn write_characteristic(&self, command: WriteCharacteristicCommand) -> Result<(), CommandDispatchError>;
            fn set_notifications(&self, command: SetNotificationsCommand) -> Result<(), CommandDispatchError>;
            fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError>;
            fn mtu(&self, command: blehub::MtuCommand) -> Result<(), CommandDispatchError>;
            fn set_connection_priority(&self, command: blehub::SetConnectionPriorityCommand) -> Result<(), CommandDispatchError>;
            fn create_bond(&self, command: blehub::CreateBondCommand) -> Result<(), CommandDispatchError>;
            fn remove_bond(&self, command: blehub::RemoveBondCommand) -> Result<(), CommandDispatchError>;
            fn read_descriptor(&self, command: blehub::ReadDescriptorCommand) -> Result<(), CommandDispatchError>;
            fn write_descriptor(&self, command: blehub::WriteDescriptorCommand) -> Result<(), CommandDispatchError>;
            fn cancel_operation(&self, request: CancelOperationRequest) -> Result<(), CommandDispatchError>;
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
    let port = owner.backend_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered_tx,
                release_rx: Mutex::new(release_rx),
            }),
            dispose_gate: None,
        }))
    });
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();

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
    release_tx.send(()).unwrap();
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
    let port = owner.backend_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered_tx,
                release_rx: Mutex::new(release_rx),
            }),
            dispose_gate: None,
        }))
    });
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    owner.lifecycle.mark_closed();
    release_tx.send(()).unwrap();
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
    let port = owner.backend_port();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let assembling = port.clone();
    let attach = std::thread::spawn(move || {
        assembling.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: Some(Gate {
                entered_tx,
                release_rx: Mutex::new(release_rx),
            }),
            dispose_gate: None,
        }))
    });
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    owner.lifecycle.fault();
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
    release_tx.send(()).unwrap();
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
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    owner
        .backend_port()
        .assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: None,
            dispose_gate: Some(Gate {
                entered_tx,
                release_rx: Mutex::new(release_rx),
            }),
        }))
        .unwrap();
    let mut shutdown_result_rx = owner.shutdown_result_rx.clone();
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
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(handle.dispose_count(), 0);
    release_tx.send(()).unwrap();
    host.block_on(async {
        let outcome = tokio::time::timeout(
            Duration::from_secs(2),
            shutdown_result_rx.wait_for(Option::is_some),
        )
        .await
        .unwrap()
        .unwrap()
        .clone();
        assert_eq!(outcome, Some(Ok(())));
        assert_eq!(owner.close().await, Ok(()));
    });
    assert!(owner.is_closed());
    assert_eq!(handle.dispose_count(), 1);
}

#[test]
fn cancelled_assembly_waiter_leaves_admitted_attach_owned_by_close() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, handle) = FakeBackend::scripted([]);
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
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
                        entered_tx,
                        release_rx: Mutex::new(release_rx),
                    }),
                    dispose_gate: None,
                }))
            )
            .await
            .is_err()
        );
    });
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
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
    release_tx.send(()).unwrap();
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
    let port = owner.backend_port();
    let (backend, handle) = FakeBackend::scripted_with_capabilities(
        BackendCapabilities {
            contract_version: BLEHUB_CONTRACT_VERSION + 1,
            ..BackendCapabilities::new(Platform::Test)
        },
        [],
    );
    let (entered_tx, entered_rx) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let attach = std::thread::spawn(move || {
        port.assemble_backend(Arc::new(GatedAssemblyBackend {
            inner: backend,
            capabilities_gate: None,
            dispose_gate: Some(Gate {
                entered_tx,
                release_rx: Mutex::new(release_rx),
            }),
        }))
    });
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
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
    release_tx.send(()).unwrap();
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
