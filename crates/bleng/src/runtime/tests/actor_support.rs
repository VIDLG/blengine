use crate::{
    runtime::{
        BlengActor, RuntimePhase, messages::OperationSpec, reservations::GattAccess,
        shared::OperationSubmission,
    },
    waiter::{WaiterError, WaiterExpectation, WaiterOutcome, WaiterSuccess},
};
use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleEvent, CommandId, DiscoverGattCommand,
    GattDatabase, GattSessionId, InitializeCommand, NotificationModeRequest, PeripheralId,
    SubscriptionId,
};
use kameo::prelude::{Context, Message};
use std::{
    sync::{Arc, mpsc},
    time::Duration,
};
use tokio::sync::{Notify, oneshot};

type WaiterReceiver = oneshot::Receiver<WaiterOutcome>;

impl BlengActor {
    /// Registers a direct actor-test waiter using the test-only allocator.
    /// Production submissions allocate IDs in RuntimeOwner instead.
    pub(super) fn register_waiter(
        &mut self,
        expectation: WaiterExpectation,
    ) -> Result<(CommandId, WaiterReceiver), WaiterError> {
        let id = self.test_command_id_gen.allocate()?.into();
        let result_rx = self.waiters.register(id, expectation)?;
        Ok((id, result_rx))
    }
}

pub struct AssumeInitialized;

impl Message<AssumeInitialized> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, _: AssumeInitialized, _: &mut Context<Self, Self::Reply>) {
        // Establish fixture readiness through the production state transitions,
        // without consuming caller command IDs or publishing synthetic events.
        let command_id = CommandId::from(0);
        self.initialization.reserve_initialize(command_id);
        self.initialization
            .resolve_dispatch(command_id, &Ok(()))
            .unwrap();
        self.initialization
            .record_event(&BleEvent::AdapterSnapshotChanged {
                command_id: Some(command_id),
                snapshot: AdapterSnapshot {
                    state: AdapterState::PoweredOn,
                    scan_authorization: AuthorizationState::Authorized,
                    connection_authorization: AuthorizationState::Authorized,
                    name: None,
                },
            });
        assert!(self.initialization.record_terminal(command_id, &Ok(())));
        self.initialization.advance_after_settlement().unwrap();
        self.initialization.require_initialized().unwrap();
    }
}

pub struct RegisterWaiter {
    pub id: CommandId,
    pub expectation: WaiterExpectation,
}

impl Message<RegisterWaiter> for BlengActor {
    type Reply = Result<oneshot::Receiver<WaiterOutcome>, WaiterError>;

    async fn handle(
        &mut self,
        message: RegisterWaiter,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        match self.lifecycle.phase() {
            RuntimePhase::Closed => return Err(WaiterError::Closed),
            RuntimePhase::Faulted => return Err(WaiterError::TransportFault),
            _ => {}
        }
        let (allocated, result_rx) = self.register_waiter(message.expectation)?;
        assert_eq!(
            allocated, message.id,
            "fixture command IDs must be sequential"
        );
        Ok(result_rx)
    }
}

pub struct HoldActor {
    pub entered: Arc<Notify>,
    pub release: Arc<Notify>,
}

impl Message<HoldActor> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: HoldActor, _: &mut Context<Self, Self::Reply>) {
        message.entered.notify_one();
        message.release.notified().await;
    }
}

impl OperationSpec for BeginInitializeWithBlockedReservation {
    type Resource = ();
    type Output = ();
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(_: (), _: (), _: WaiterSuccess) -> Option<()> {
        panic!("cancelled initialization must not reach success decoding")
    }
}

pub struct BeginInitializeWithBlockedReservation {
    pub submission: OperationSubmission,
    pub entered_tx: oneshot::Sender<()>,
    pub release_rx: mpsc::Receiver<()>,
}

impl Message<BeginInitializeWithBlockedReservation> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginInitializeWithBlockedReservation,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            self.require_open()?;
            if self.initialization.require_not_started().is_err() {
                return Err(WaiterError::OperationBusy);
            }
            let command_id = self
                .register_operation_waiter(&message.submission, WaiterExpectation::Initialized)?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                move |actor| {
                    actor.initialization.reserve_initialize(command_id);
                    message
                        .entered_tx
                        .send(())
                        .expect("begin caller dropped before reservation");
                    // Hold the synchronous submission open after try_claim wins, before the begin reply.
                    message
                        .release_rx
                        .recv_timeout(Duration::from_secs(5))
                        .expect("reservation gate was not released");
                },
                move |hub| hub.initialize(InitializeCommand { command_id }),
            )?;
            Ok(())
        }
        .await;
        submission.resolve(result);
    }
}

impl OperationSpec for CancelledRediscovery {
    type Resource = ();
    type Output = ();
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(_: (), _: (), _: WaiterSuccess) -> Option<()> {
        panic!("cancelled rediscovery must not reach success decoding")
    }
}

pub struct CancelledRediscovery {
    pub submission: OperationSubmission,
    pub gate: HoldActor,
    pub database: GattDatabase,
    pub checked_tx: oneshot::Sender<(CommandId, GattSessionId)>,
}

impl Message<CancelledRediscovery> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: CancelledRediscovery,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            message.gate.entered.notify_one();
            message.gate.release.notified().await;
            let connection_id = message.database.connection_id;
            let key = self.connections.validate_gatt_discovery(connection_id)?;
            let session_id = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::GattDiscovered(connection_id, session_id),
            )?;
            let mut reserved = false;
            assert_eq!(
                self.launch_dispatch(
                    ctx,
                    &message.submission,
                    command_id,
                    |actor| {
                        reserved = true;
                        actor
                            .connections
                            .reserve_gatt_discovery(key, command_id, session_id);
                    },
                    move |hub| {
                        hub.discover_gatt(DiscoverGattCommand {
                            command_id,
                            connection_id,
                            gatt_session_id: session_id,
                        })
                    },
                ),
                Err(WaiterError::Cancelled)
            );
            assert!(
                !reserved,
                "cancelled rediscovery must never install a draft"
            );
            assert!(!self.waiters.contains(command_id));
            assert_eq!(
                self.connections.validate_gatt_discovery(connection_id),
                Ok(key)
            );
            let characteristic_id = message.database.services[0].characteristics[0].id;
            assert_eq!(
                self.connections.validate_gatt_access(
                    connection_id,
                    message.database.gatt_session_id,
                    characteristic_id,
                    GattAccess::Read,
                ),
                Ok(key),
                "the committed session remains readable without a pending discovery"
            );
            assert_eq!(
                self.connections.validate_gatt_access(
                    connection_id,
                    session_id,
                    characteristic_id,
                    GattAccess::Read
                ),
                Err(WaiterError::GattNotReady),
                "the cancelled draft session must not become usable"
            );
            assert!(
                self.connections
                    .resolve_dispatch(command_id, &Ok(()))
                    .is_none()
            );
            message.checked_tx.send((command_id, session_id)).unwrap();
            Err(WaiterError::Cancelled)
        }
        .await;
        submission.resolve(result);
    }
}

impl OperationSpec for CancelledConnectionLeaf {
    type Resource = ();
    type Output = ();
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(_: (), _: (), _: WaiterSuccess) -> Option<()> {
        panic!("cancelled connection leaf must not reach success decoding")
    }
}

pub struct CancelledConnectionLeaf {
    pub submission: OperationSubmission,
    pub gate: HoldActor,
    pub peripheral_id: PeripheralId,
    pub database: GattDatabase,
    pub active_subscription: SubscriptionId,
    pub expectation: WaiterExpectation,
    pub checked_tx: oneshot::Sender<()>,
}

impl Message<CancelledConnectionLeaf> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: CancelledConnectionLeaf,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            let key = message.peripheral_id;
            let connection_id = message.database.connection_id;
            let session_id = message.database.gatt_session_id;
            let characteristic_id = message.database.services[0].characteristics[0].id;
            let provisional_subscription = 100.into();
            let expectation = message.expectation;
            let cancelling_rssi = matches!(expectation, WaiterExpectation::RssiRead(_));
            let sibling_expectation = if cancelling_rssi {
                WaiterExpectation::CharacteristicRead(connection_id, session_id, characteristic_id)
            } else {
                WaiterExpectation::RssiRead(connection_id)
            };
            let (sibling_id, mut sibling_rx) = self.register_waiter(sibling_expectation)?;
            // Retain an unrelated pending sibling without dispatching native work.
            if cancelling_rssi {
                self.connections
                    .reserve_read(key, sibling_id, session_id, characteristic_id);
            } else {
                self.connections.reserve_rssi(key, sibling_id);
            }
            message.gate.entered.notify_one();
            message.gate.release.notified().await;
            let command_id = self.register_operation_waiter(&message.submission, expectation)?;
            let mut reserved = false;
            assert_eq!(
                self.launch_dispatch(
                    ctx,
                    &message.submission,
                    command_id,
                    |actor| {
                        reserved = true;
                        match expectation {
                            WaiterExpectation::RssiRead(_) => {
                                actor.connections.reserve_rssi(key, command_id)
                            }
                            WaiterExpectation::CharacteristicRead(..) => actor
                                .connections
                                .reserve_read(key, command_id, session_id, characteristic_id),
                            WaiterExpectation::CharacteristicWritten(..) => actor
                                .connections
                                .reserve_write(key, command_id, session_id, characteristic_id),
                            WaiterExpectation::Disconnected(_) => {
                                actor.connections.reserve_disconnect(key, command_id)
                            }
                            WaiterExpectation::NotificationsEnabled(..) => {
                                actor.connections.reserve_notification_enable(
                                    key,
                                    command_id,
                                    provisional_subscription,
                                    session_id,
                                    characteristic_id,
                                    NotificationModeRequest::PlatformDefault,
                                )
                            }
                            WaiterExpectation::NotificationsDisabled(..) => {
                                actor.connections.reserve_notification_disable(
                                    key,
                                    command_id,
                                    message.active_subscription,
                                )
                            }
                            _ => unreachable!(),
                        }
                    },
                    move |_| panic!("cancelled {expectation:?} must never dispatch"),
                ),
                Err(WaiterError::Cancelled)
            );
            assert!(!reserved, "{expectation:?} must skip its reserve closure");
            assert!(
                !self.waiters.contains(command_id),
                "cancelled waiter must be retired"
            );
            assert!(matches!(
                sibling_rx.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ));
            assert_eq!(
                self.connections.waiter_result(1.into()),
                Some(Ok(WaiterSuccess::Connected(connection_id)))
            );
            assert!(
                self.connections
                    .resolve_dispatch(command_id, &Ok(()))
                    .is_none()
            );
            // A real correlated callback must still belong to the sibling's unresolved submission.
            let (callback, expected) = if cancelling_rssi {
                let value = vec![0x56, 0x78];
                (
                    BleEvent::CharacteristicRead {
                        command_id: sibling_id,
                        connection_id,
                        gatt_session_id: session_id,
                        characteristic_id,
                        value: value.clone(),
                    },
                    WaiterSuccess::CharacteristicRead {
                        connection_id,
                        gatt_session_id: session_id,
                        characteristic_id,
                        value,
                    },
                )
            } else {
                (
                    BleEvent::RssiRead {
                        command_id: sibling_id,
                        connection_id,
                        rssi: -42,
                    },
                    WaiterSuccess::RssiRead(connection_id, -42),
                )
            };
            assert_eq!(self.connections.gate_event(callback.clone()).unwrap(), None);
            assert_eq!(self.connections.waiter_result(sibling_id), None);
            let resolution = self
                .connections
                .resolve_dispatch(sibling_id, &Ok(()))
                .unwrap();
            assert!(matches!(
                resolution.action,
                crate::runtime::reservations::DispatchAction::None
            ));
            assert_eq!(resolution.events_to_replay, vec![callback]);
            for event in &resolution.events_to_replay {
                self.connections.record_event(event).unwrap();
            }
            assert_eq!(self.connections.waiter_result(sibling_id), None);
            self.connections.record_terminal(sibling_id, &Ok(()));
            assert_eq!(
                self.connections.waiter_result(sibling_id),
                Some(Ok(expected.clone()))
            );
            self.try_settle_waiter(sibling_id);
            assert_eq!(sibling_rx.try_recv(), Ok(Ok(expected)));
            self.connections.retire_completed();
            assert_eq!(self.connections.waiter_result(sibling_id), None);
            assert_eq!(self.connections.validate_rssi(connection_id), Ok(key));
            assert_eq!(
                self.connections.validate_gatt_access(
                    connection_id,
                    session_id,
                    characteristic_id,
                    GattAccess::Write
                ),
                Ok(key),
                "the committed database remains usable without a draft or cancelled I/O slot"
            );
            assert_eq!(
                self.connections.validate_notification_disable(
                    connection_id,
                    session_id,
                    provisional_subscription
                ),
                Err(WaiterError::SubscriptionNotActive)
            );
            assert_eq!(
                self.connections.validate_notification_disable(
                    connection_id,
                    session_id,
                    message.active_subscription
                ),
                Ok((key, characteristic_id)),
                "the active route keeps its exact session/characteristic and no disable transition"
            );
            assert_eq!(
                self.connections.validate_notification_enable(
                    connection_id,
                    session_id,
                    characteristic_id
                ),
                Err(WaiterError::OperationBusy),
                "the existing route still excludes a duplicate enable"
            );
            assert_eq!(
                self.connections.validate_gatt_access(
                    connection_id,
                    session_id,
                    characteristic_id,
                    GattAccess::Read
                ),
                Err(WaiterError::OperationBusy),
                "the active route still excludes reads of its characteristic"
            );
            assert_eq!(
                self.connections.validate_gatt_discovery(connection_id),
                Err(WaiterError::GattBusy)
            );
            message.checked_tx.send(()).unwrap();
            Err(WaiterError::Cancelled)
        }
        .await;
        submission.resolve(result);
    }
}

impl OperationSpec for BeginInitializeAfterCleanup {
    type Resource = ();
    type Output = ();
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(_: (), _: (), _: WaiterSuccess) -> Option<()> {
        panic!("cleanup before launch must not reach success decoding")
    }
}

pub struct BeginInitializeAfterCleanup {
    pub submission: OperationSubmission,
    pub phase: RuntimePhase,
    pub expected_error: WaiterError,
}

impl Message<BeginInitializeAfterCleanup> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginInitializeAfterCleanup,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = async {
            self.require_open()?;
            self.initialization.require_not_started()?;
            let command_id = self
                .register_operation_waiter(&message.submission, WaiterExpectation::Initialized)?;
            match message.phase {
                RuntimePhase::Open => self.lifecycle.start_cleanup(),
                RuntimePhase::Closed => {
                    self.lifecycle.mark_closed();
                    self.lifecycle.start_cleanup();
                }
                RuntimePhase::Faulted => self.lifecycle.fault(),
            }
            let error = self
                .launch_dispatch(
                    ctx,
                    &message.submission,
                    command_id,
                    |_| panic!("cleanup before launch must not reserve initialization"),
                    |_| panic!("cleanup before launch must not dispatch a backend command"),
                )
                .expect_err("cleanup before launch must reject dispatch");
            assert_eq!(error, message.expected_error);
            assert!(!self.waiters.contains(command_id));
            assert_eq!(self.initialization.require_not_started(), Ok(()));
            assert_eq!(self.lifecycle.phase(), message.phase);
            // A handler error leaves guard retirement to run_operation, without acknowledging launch.
            Err(error)
        }
        .await;
        submission.resolve(result);
    }
}

pub struct CheckConsumedIds;

impl Message<CheckConsumedIds> for BlengActor {
    type Reply = Result<(), WaiterError>;

    async fn handle(
        &mut self,
        _: CheckConsumedIds,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let mut receivers = Vec::new();
        for expected in 1..=64 {
            assert_eq!(self.allocate_resource_id()?, expected);
            let (command, receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
            assert_eq!(command, CommandId { value: expected });
            receivers.push(receiver);
        }

        // Resource-creating commands allocate their resource before registering a waiter.
        assert_eq!(self.allocate_resource_id()?, 65);
        assert!(matches!(
            self.register_waiter(WaiterExpectation::TerminalOnly),
            Err(WaiterError::CapacityExceeded)
        ));
        self.waiters.cancel(1.into());
        assert_eq!(receivers[0].try_recv(), Ok(Err(WaiterError::Cancelled)));

        assert_eq!(self.allocate_resource_id()?, 66);
        let (command, mut receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(
            command,
            66.into(),
            "failed registration must consume its command ID"
        );
        self.waiters.cancel(command);
        assert_eq!(receiver.try_recv(), Ok(Err(WaiterError::Cancelled)));

        let (next_command, _receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(next_command, 67.into(), "cancellation must not reuse an ID");
        assert_eq!(self.allocate_resource_id()?, 67);
        assert_eq!(self.allocate_resource_id()?, 68);
        self.waiters.cancel(next_command);
        let (next_command, _receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(
            next_command,
            68.into(),
            "resource allocation must not advance commands"
        );
        Ok(())
    }
}
