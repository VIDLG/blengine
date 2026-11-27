//! The connection-end lane, driven through the fixture's real single event reader.
//!
//! Every assertion here is about what a central can actually know. An end that this capability did
//! not request is `Unrequested` even when the platform also reported an error, because "the peer
//! dropped it" is not something a central observes directly. A connect failure is not an end: no
//! link was established, and the original connect result already carries that fact.

use super::*;
use crate::{ForeignConnectionEndBy, SupervisedConnection};

const CONNECTION: u64 = 2;

fn disconnected(command: Option<u64>, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Disconnected,
        },
    }
}

fn link_failure(command: Option<u64>, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: command.map(Into::into),
        connection_id: connection.into(),
        result: ConnectionStateResult::Err {
            error: BleEventError {
                category: BleErrorCategory::Connection,
                message: "peer closed the link".to_owned(),
                platform_details: None,
            },
        },
    }
}

async fn connection(fixture: &mut Fixture) -> Arc<SupervisedConnection> {
    let peripheral = {
        let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
        let scan = result.scan.expect("scan submission");
        assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
        fixture.source.observed(completed(2)).await;
        scan.next_observation(WAIT_MS as f64)
            .await
            .advertisement
            .expect("advertisement")
            .peripheral
    };
    peripheral
        .begin_connect(false)
        .connect
        .expect("connect submission")
        .wait_result(WAIT_MS as f64)
        .await
        .connection
        .expect("connection capability")
}

/// The advertisements of one scan, one per peer, each with its own peripheral identity.
fn advertisement_from(scan: u64, peripheral: u64, marker: u8) -> BleEvent {
    let mut observation = advertisement(scan, marker);
    observation.peripheral_id = peripheral.into();
    BleEvent::AdvertisementObserved {
        advertisement: observation,
    }
}

/// Every peripheral one scan observed, oldest first.
async fn peripherals(fixture: &mut Fixture, expected: usize) -> Vec<Arc<SupervisedPeripheral>> {
    let result = Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
    let scan = result.scan.expect("scan submission");
    assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
    fixture.source.observed(completed(2)).await;
    let mut observed = Vec::new();
    for _ in 0..expected {
        observed.push(
            scan.next_observation(WAIT_MS as f64)
                .await
                .advertisement
                .expect("advertisement")
                .peripheral,
        );
    }
    observed
}

#[test]
fn an_unrequested_end_is_retained_and_never_claims_the_operator_asked() {
    run(|| async {
        let mut fixture = Fixture::new(advertised());
        fixture.supervisor().start().await.unwrap();
        let connection = connection(&mut fixture).await;

        fixture
            .source
            .emit_and_observe(disconnected(None, CONNECTION))
            .await;
        let first = connection.await_end(WAIT_MS as f64).await;
        assert!(first.failure.is_none(), "{:?}", first.failure);
        let ended = first.ended.expect("retained end");
        assert_eq!(ended.by, ForeignConnectionEndBy::Unrequested);
        assert!(ended.failure.is_none(), "{:?}", ended.failure);

        // A retained fact, not a consumable receipt: a second call still reports it.
        let again = connection.await_end(WAIT_MS as f64).await;
        assert_eq!(again.ended.expect("retained end").by, ended.by);
        fixture.shutdown().await;
    });
}

#[test]
fn a_platform_reported_link_failure_is_carried_without_becoming_a_request() {
    run(|| async {
        let mut fixture = Fixture::new(advertised());
        fixture.supervisor().start().await.unwrap();
        let connection = connection(&mut fixture).await;

        fixture
            .source
            .emit_and_observe(link_failure(None, CONNECTION))
            .await;
        let ended = connection
            .await_end(WAIT_MS as f64)
            .await
            .ended
            .expect("retained end");
        assert_eq!(ended.by, ForeignConnectionEndBy::Unrequested);
        let failure = ended.failure.expect("the platform's own report");
        assert_eq!(failure.domain, ForeignOperationFailureDomain::Operation);
        assert_eq!(failure.code, "ble.connection");
        assert_eq!(failure.message, "peer closed the link");
        fixture.shutdown().await;
    });
}

#[test]
fn the_operators_own_disconnect_is_the_only_operator_attributed_end() {
    run(|| async {
        let mut fixture =
            Fixture::new(
                advertised()
                    .into_iter()
                    .chain([EventScenario::emit_before_return(
                        RecordedCommand::Disconnect(DisconnectCommand {
                            command_id: 4.into(),
                            connection_id: CONNECTION.into(),
                        }),
                        vec![disconnected(Some(4), CONNECTION), completed(4)],
                    )]),
            );
        fixture.supervisor().start().await.unwrap();
        let connection = connection(&mut fixture).await;

        let begun = connection.begin_disconnect();
        assert!(begun.failure.is_none(), "{:?}", begun.failure);
        let receipt = begun
            .disconnect
            .expect("disconnect submission")
            .wait_result(WAIT_MS as f64)
            .await;
        assert!(receipt.failure.is_none(), "{:?}", receipt.failure);
        // The result resolves from the actor's waiter; the lane sees the same fact only when the
        // owner's single event reader consumes it, which is what this scene demands next.
        fixture
            .source
            .observed(disconnected(Some(4), CONNECTION))
            .await;

        let ended = connection
            .await_end(WAIT_MS as f64)
            .await
            .ended
            .expect("retained end");
        assert_eq!(ended.by, ForeignConnectionEndBy::Operator);
        assert!(ended.failure.is_none(), "{:?}", ended.failure);
        fixture.source.observed(completed(4)).await;
        fixture.shutdown().await;
    });
}

#[test]
fn a_connect_failure_is_a_result_and_not_a_link_end() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().take(1).chain([connect(
            3,
            CONNECTION,
            17,
            vec![
                BleEvent::ConnectionStateChanged {
                    command_id: Some(3.into()),
                    connection_id: CONNECTION.into(),
                    result: ConnectionStateResult::Err {
                        error: BleEventError {
                            category: BleErrorCategory::Connection,
                            message: "connect timed out".to_owned(),
                            platform_details: None,
                        },
                    },
                },
                completed(3),
            ],
        )]));
        fixture.supervisor().start().await.unwrap();
        let peripheral = {
            let result =
                Arc::clone(fixture.supervisor()).begin_scan_view(context(), scan_options());
            let scan = result.scan.expect("scan submission");
            assert!(scan.start_result(WAIT_MS as f64).await.failure.is_none());
            fixture.source.observed(completed(2)).await;
            scan.next_observation(WAIT_MS as f64)
                .await
                .advertisement
                .expect("advertisement")
                .peripheral
        };
        let begun = peripheral.begin_connect(false);
        // The connect's own result is the only surface that reports this failure.
        let result = begun
            .connect
            .expect("connect submission")
            .wait_result(WAIT_MS as f64)
            .await;
        assert!(result.connection.is_none());
        assert_eq!(
            result.failure.expect("reported connect failure").message,
            "connect timed out"
        );
        fixture.shutdown().await;
    });
}

#[test]
fn owner_retirement_ends_observation_without_claiming_the_link_is_down() {
    run(|| async {
        let mut fixture = Fixture::new(advertised());
        fixture.supervisor().start().await.unwrap();
        let connection = connection(&mut fixture).await;
        fixture.shutdown().await;

        let ended = connection
            .await_end(WAIT_MS as f64)
            .await
            .ended
            .expect("retained end");
        assert_eq!(ended.by, ForeignConnectionEndBy::Owner);
        assert!(ended.failure.is_none(), "{:?}", ended.failure);
        // The owner index is where a retired owner's lanes stay: no re-binding to a later owner.
        assert_eq!(
            connection.await_end(WAIT_MS as f64).await.ended.unwrap().by,
            ForeignConnectionEndBy::Owner
        );
    });
}

#[test]
fn two_connections_of_one_owner_end_independently() {
    // The product lane keeps one end lane per connection, so a link ending must not be
    // reported on a sibling — and the sibling must stay usable afterwards.
    run(|| async {
        let mut fixture = Fixture::new([
            start(
                2,
                1,
                vec![
                    started(2, 1),
                    advertisement_from(1, 17, 0),
                    advertisement_from(1, 18, 1),
                    completed(2),
                ],
            ),
            connect(3, 2, 17, vec![connected(3, 2), completed(3)]),
            connect(4, 3, 18, vec![connected(4, 3), completed(4)]),
        ]);
        fixture.supervisor().start().await.unwrap();
        let observed = peripherals(&mut fixture, 2).await;
        let first = observed[0]
            .begin_connect(false)
            .connect
            .expect("first connect")
            .wait_result(WAIT_MS as f64)
            .await
            .connection
            .expect("first connection");
        let second = observed[1]
            .begin_connect(false)
            .connect
            .expect("second connect")
            .wait_result(WAIT_MS as f64)
            .await
            .connection
            .expect("second connection");

        // Only the first peer goes away.
        fixture
            .source
            .emit_and_observe(disconnected(None, CONNECTION))
            .await;
        assert_eq!(
            first.await_end(WAIT_MS as f64).await.ended.unwrap().by,
            ForeignConnectionEndBy::Unrequested
        );
        // The sibling has nothing to report, and its lane is still open.
        let untouched = second.await_end(5.0).await;
        assert!(untouched.ended.is_none(), "{:?}", untouched.ended);
        assert_failure(
            untouched.failure,
            ForeignOperationFailureDomain::Observation,
            "bleng.timeout",
        );
        // The engine still admits work on the surviving link.
        let rssi = second.begin_read_rssi();
        assert!(rssi.failure.is_none(), "{:?}", rssi.failure);

        // Its own end arrives independently, and is attributed the same way.
        fixture
            .source
            .emit_and_observe(disconnected(None, CONNECTION + 1))
            .await;
        assert_eq!(
            second.await_end(WAIT_MS as f64).await.ended.unwrap().by,
            ForeignConnectionEndBy::Unrequested
        );
        fixture.shutdown().await;
    });
}

#[test]
fn another_connections_end_does_not_end_this_lane() {
    run(|| async {
        let mut fixture = Fixture::new(advertised());
        fixture.supervisor().start().await.unwrap();
        let connection = connection(&mut fixture).await;

        fixture
            .source
            .emit_and_observe(disconnected(None, CONNECTION + 7))
            .await;
        let observation = connection.await_end(5.0).await;
        assert!(observation.ended.is_none());
        // A deadline is the caller's bound, not a fact about the link.
        assert_failure(
            observation.failure,
            ForeignOperationFailureDomain::Observation,
            "bleng.timeout",
        );

        fixture
            .source
            .emit_and_observe(disconnected(None, CONNECTION))
            .await;
        assert_eq!(
            connection.await_end(WAIT_MS as f64).await.ended.unwrap().by,
            ForeignConnectionEndBy::Unrequested
        );
        fixture.shutdown().await;
    });
}
