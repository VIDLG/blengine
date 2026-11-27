//! Connect/disconnect projection over the same fixture as the scan view: one supervisor, one
//! owner, one fake backend.

use super::*;

fn disconnect(command: u64, connection: u64, events: Vec<BleEvent>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::Disconnect(DisconnectCommand {
            command_id: command.into(),
            connection_id: connection.into(),
        }),
        events,
    )
}

const CONNECTION: u64 = 2;

fn disconnected(command: u64, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command.into()),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    }
}

/// One observed advertisement's peripheral capability, from the fixture's started supervisor.
async fn begin_peripheral(fixture: &mut Fixture) -> Arc<SupervisedPeripheral> {
    let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
    assert!(result.failure.is_none());
    let scan = result.scan.expect("exactly the scan branch");
    assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
    fixture.source.observed(completed(2)).await;
    let observation = scan.next_observation(WAIT_MS as f64).await;
    assert!(observation.failure.is_none() && observation.ended.is_none());
    observation.advertisement.expect("advertisement").peripheral
}

#[test]
fn connect_and_disconnect_are_owner_scoped_capabilities_with_typed_receipts() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([disconnect(
            4,
            CONNECTION,
            vec![disconnected(4, CONNECTION), completed(4)],
        )]));
        fixture.supervisor().start().await.unwrap();
        let peripheral = begin_peripheral(&mut fixture).await;

        let begun = peripheral.begin_connect(false);
        assert!(begun.failure.is_none());
        let connect = begun.connect.expect("bounded connect submission");
        // A projected timeout is an input failure and allocates no further command.
        let dispatched = fixture.source.backend.commands().len();
        assert_failure(
            connect.wait_result(0.5).await.failure,
            ForeignOperationFailureDomain::Input,
            "invalid_timeout",
        );
        assert_eq!(fixture.source.backend.commands().len(), dispatched);

        let result = connect.wait_result(WAIT_MS as f64).await;
        assert!(result.failure.is_none(), "{:?}", result.failure);
        let connection = result
            .connection
            .expect("owner-bound connection capability");
        assert!(connection.same_connection(Arc::clone(&connection)));

        let begun = connection.begin_disconnect();
        assert!(begun.failure.is_none());
        let disconnect = begun.disconnect.expect("bounded disconnect submission");
        assert_failure(
            disconnect.wait_result(0.0).await.failure,
            ForeignOperationFailureDomain::Input,
            "invalid_timeout",
        );
        let done = disconnect.wait_result(WAIT_MS as f64).await;
        assert!(done.failure.is_none(), "{:?}", done.failure);
        assert_failure(
            disconnect.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        assert_failure(
            disconnect.wait_result(WAIT_MS as f64).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_result_consumed",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn shutdown_leaves_projected_capabilities_closed_instead_of_rebinding() {
    run(|| async {
        let mut fixture = Fixture::new(advertised());
        fixture.supervisor().start().await.unwrap();
        let peripheral = begin_peripheral(&mut fixture).await;
        let connection = peripheral
            .begin_connect(false)
            .connect
            .expect("connect")
            .wait_result(WAIT_MS as f64)
            .await
            .connection
            .expect("connection");
        fixture.shutdown().await;
        assert_failure(
            peripheral.begin_connect(false).failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.no_active_session",
        );
        assert_failure(
            connection.begin_disconnect().failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.no_active_session",
        );
    });
}
