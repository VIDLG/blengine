use super::support::{
    Arc, BleHub, CommandId, ConnectCommand, ConnectionId, Duration, EventIngressStatus,
    EventScenario, FakeBackend, RecordedCommand, RuntimeOwner, WaiterError, WaiterExpectation,
    command_completed, connect_scenario, connected_event,
};
use crate::runtime::{BlengActor, actor::IdGenerator};
use kameo::prelude::{Context, Message};

#[test]
fn maximum_id_is_issued_once_and_exhaustion_never_wraps_or_mutates_sequence() {
    let mut sequence = IdGenerator {
        last_issued: u64::MAX - 1,
    };
    assert_eq!(sequence.allocate(), Ok(u64::MAX));
    for _ in 0..3 {
        assert_eq!(sequence.allocate(), Err(WaiterError::IdExhausted));
        assert_eq!(sequence.last_issued, u64::MAX);
    }
}

struct CheckConsumedIds;

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
        self.cancel_waiter(1.into());
        assert_eq!(receivers[0].try_recv(), Ok(Err(WaiterError::Cancelled)));

        assert_eq!(self.allocate_resource_id()?, 66);
        let (command, mut receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(
            command,
            66.into(),
            "failed registration must consume its command ID"
        );
        self.cancel_waiter(command);
        assert_eq!(receiver.try_recv(), Ok(Err(WaiterError::Cancelled)));

        let (next_command, _receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(next_command, 67.into(), "cancellation must not reuse an ID");
        assert_eq!(self.allocate_resource_id()?, 67);
        assert_eq!(self.allocate_resource_id()?, 68);
        self.cancel_waiter(next_command);
        let (next_command, _receiver) = self.register_waiter(WaiterExpectation::TerminalOnly)?;
        assert_eq!(
            next_command,
            68.into(),
            "resource allocation must not advance commands"
        );
        Ok(())
    }
}

#[test]
fn failed_waiter_registration_and_cancellation_consume_ids_without_reuse() {
    let owner = RuntimeOwner::new().unwrap();
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        owner.actor.ask(CheckConsumedIds).await.unwrap();
        owner.close().await.unwrap();
    });
}

#[test]
fn equal_valued_command_and_connection_ids_still_require_exact_correlation() {
    for terminal_first in [false, true] {
        let owner = RuntimeOwner::new_assuming_initialized().unwrap();
        let second_connect = RecordedCommand::Connect(ConnectCommand {
            command_id: 2.into(),
            connection_id: 2.into(),
            peripheral_id: 20.into(),
        });
        let (backend, handle) = FakeBackend::scripted([
            connect_scenario(1, 1, 10),
            EventScenario::accept(second_connect.clone()),
        ]);
        owner
            .attach_hub(BleHub::new(Arc::new(backend), owner.event_sink()).unwrap())
            .unwrap();
        let host = tokio::runtime::Builder::new_multi_thread()
            .enable_time()
            .build()
            .unwrap();
        host.block_on(async {
            let deadline = Duration::from_secs(2);
            assert_eq!(owner.connect(10.into(), deadline).await, Ok(1.into()));
            for _ in 0..2 {
                owner.next_event(deadline).await.unwrap().unwrap();
            }
            let mut pending = Box::pin(owner.connect(20.into(), deadline));
            tokio::time::timeout(deadline, async {
                loop {
                    tokio::select! {
                        result = &mut pending => panic!("connect completed before callbacks: {result:?}"),
                        _ = tokio::task::yield_now() => {}
                    }
                    if handle.commands().last() == Some(&second_connect) {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            for event in [connected_event(1, 2), connected_event(2, 1), command_completed(1, None)] {
                assert_eq!(handle.emit(event.clone()), EventIngressStatus::Delivered);
                assert_eq!(owner.next_event(deadline).await, Ok(Some(event)));
            }
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut pending).await.is_err());

            let domain = connected_event(2, 2);
            let terminal = command_completed(2, None);
            let (first, second) = if terminal_first { (terminal, domain) } else { (domain, terminal) };
            assert_eq!(handle.emit(first.clone()), EventIngressStatus::Delivered);
            assert_eq!(owner.next_event(deadline).await, Ok(Some(first)));
            assert!(tokio::time::timeout(Duration::from_millis(20), &mut pending).await.is_err());
            assert_eq!(handle.emit(second), EventIngressStatus::Delivered);
            assert_eq!(pending.await, Ok(ConnectionId { value: 2 }));
            assert!(!owner.faulted_for_test());
            owner.close().await.unwrap();
        });
        assert_eq!(handle.commands().len(), 2);
        assert_eq!(handle.dispose_count(), 1);
    }
}
