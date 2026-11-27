//! GATT discovery/read/write/RSSI projection over the shared fake-backend fixture.
//!
//! The point of these tests is that the *boundary* carries capabilities, not handles: a
//! characteristic in JS is the only way to address an attribute, and a replaced topology cannot be
//! dispatched through.

use super::*;
use blehub::{
    BondState, CharacteristicProperties, CreateBondCommand, GattCharacteristic,
    GattCharacteristicId, GattDatabase, GattDescriptor, GattDescriptorId, GattService,
    GattServiceId, GattSessionId,
};

const GATT_SESSION: u64 = 3;
const SERVICE_A: u64 = 501;
const SERVICE_B: u64 = 502;
const CHARACTERISTIC_A: u64 = 601;
const CHARACTERISTIC_B: u64 = 602;
const CCCD: &str = "00002902-0000-1000-8000-00805f9b34fb";

fn properties() -> CharacteristicProperties {
    CharacteristicProperties {
        readable: true,
        writable_with_response: true,
        writable_without_response: false,
        notifiable: true,
        indicatable: false,
    }
}

fn gatt_uuid(value: &str) -> BluetoothUuid {
    BluetoothUuid::canonicalize(value).expect("canonical UUID")
}

fn database(connection: u64, session: u64) -> GattDatabase {
    let service_uuid = gatt_uuid("0000180d-0000-1000-8000-00805f9b34fb");
    let characteristic_uuid = gatt_uuid("00002a37-0000-1000-8000-00805f9b34fb");
    let cccd = gatt_uuid(CCCD);
    let service = |id: u64, primary: bool, characteristic: u64, descriptor: u64| GattService {
        id: GattServiceId::from(id),
        uuid: service_uuid.clone(),
        primary: Some(primary),
        characteristics: vec![GattCharacteristic {
            id: GattCharacteristicId::from(characteristic),
            uuid: characteristic_uuid.clone(),
            properties: properties(),
            descriptors: vec![GattDescriptor {
                id: GattDescriptorId::from(descriptor),
                uuid: cccd.clone(),
            }],
        }],
    };
    GattDatabase {
        connection_id: connection.into(),
        gatt_session_id: GattSessionId::from(session),
        services: vec![
            service(SERVICE_A, true, CHARACTERISTIC_A, 701),
            service(SERVICE_B, false, CHARACTERISTIC_B, 702),
        ],
    }
}

fn discover(command: u64, connection: u64, session: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
        }),
        vec![
            BleEvent::GattDiscovered {
                command_id: command.into(),
                database: database(connection, session),
            },
            completed(command),
        ],
    )
}

fn read(
    command: u64,
    connection: u64,
    session: u64,
    characteristic: u64,
    value: Vec<u8>,
) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::ReadCharacteristic(ReadCharacteristicCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
            characteristic_id: characteristic.into(),
        }),
        vec![
            BleEvent::CharacteristicRead {
                command_id: command.into(),
                connection_id: connection.into(),
                gatt_session_id: session.into(),
                characteristic_id: characteristic.into(),
                value,
            },
            completed(command),
        ],
    )
}

fn write(
    command: u64,
    connection: u64,
    session: u64,
    characteristic: u64,
    value: Vec<u8>,
) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::WriteCharacteristic(WriteCharacteristicCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
            characteristic_id: characteristic.into(),
            value,
            write_type: WriteType::WithResponse,
        }),
        vec![
            BleEvent::CharacteristicWritten {
                command_id: command.into(),
                connection_id: connection.into(),
                gatt_session_id: session.into(),
                characteristic_id: characteristic.into(),
            },
            completed(command),
        ],
    )
}

fn rssi(command: u64, connection: u64, rssi: i16) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::ReadRssi(ReadRssiCommand {
            command_id: command.into(),
            connection_id: connection.into(),
        }),
        vec![
            BleEvent::RssiRead {
                command_id: command.into(),
                connection_id: connection.into(),
                rssi,
            },
            completed(command),
        ],
    )
}

fn mtu(command: u64, connection: u64, request: Option<u16>, info: MtuInfo) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::Mtu(MtuCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            request,
        }),
        vec![
            BleEvent::MtuRead {
                command_id: command.into(),
                connection_id: connection.into(),
                info,
            },
            completed(command),
        ],
    )
}

fn priority(command: u64, connection: u64, priority: ConnectionPriority) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::SetConnectionPriority(SetConnectionPriorityCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            priority,
        }),
        vec![completed(command)],
    )
}

fn bond(command: u64, connection: u64, state: BondState) -> EventScenario {
    bond_with_pin(command, connection, None, state)
}

fn bond_with_pin(
    command: u64,
    connection: u64,
    pin: Option<&str>,
    state: BondState,
) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::CreateBond(CreateBondCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            pin: pin.map(str::to_owned),
        }),
        vec![
            BleEvent::BondStateChanged {
                command_id: Some(command.into()),
                connection_id: connection.into(),
                state,
            },
            completed(command),
        ],
    )
}

fn descriptor_read(
    command: u64,
    connection: u64,
    session: u64,
    characteristic: u64,
    descriptor: u64,
    value: Vec<u8>,
) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::ReadDescriptor(ReadDescriptorCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
            characteristic_id: characteristic.into(),
            descriptor_id: descriptor.into(),
        }),
        vec![
            BleEvent::DescriptorRead {
                command_id: command.into(),
                connection_id: connection.into(),
                gatt_session_id: session.into(),
                characteristic_id: characteristic.into(),
                descriptor_id: descriptor.into(),
                value,
            },
            completed(command),
        ],
    )
}

fn descriptor_write(
    command: u64,
    connection: u64,
    session: u64,
    characteristic: u64,
    descriptor: u64,
    value: Vec<u8>,
) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::WriteDescriptor(WriteDescriptorCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
            characteristic_id: characteristic.into(),
            descriptor_id: descriptor.into(),
            value,
        }),
        vec![
            BleEvent::DescriptorWritten {
                command_id: command.into(),
                connection_id: connection.into(),
                gatt_session_id: session.into(),
                characteristic_id: characteristic.into(),
                descriptor_id: descriptor.into(),
            },
            completed(command),
        ],
    )
}

fn advertised() -> Vec<EventScenario> {
    vec![
        start(
            2,
            1,
            vec![
                started(2, 1),
                observed_ad(advertisement(1, 0)),
                completed(2),
            ],
        ),
        connect(3, 2, 17, vec![connected(3, 2), completed(3)]),
    ]
}

fn connect(command: u64, connection: u64, peripheral: u64, events: Vec<BleEvent>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::Connect(ConnectCommand {
            auto_connect: false,
            command_id: command.into(),
            connection_id: connection.into(),
            peripheral_id: peripheral.into(),
        }),
        events,
    )
}

fn connected(command: u64, connection: u64) -> BleEvent {
    BleEvent::ConnectionStateChanged {
        command_id: Some(command.into()),
        connection_id: connection.into(),
        result: ConnectionStateResult::Ok {
            state: ConnectionState::Connected,
        },
    }
}

async fn begin_session(
    fixture: &mut Fixture,
) -> (
    Arc<crate::SupervisedConnection>,
    Arc<crate::SupervisedGattSession>,
) {
    let connection = begin_connection(fixture).await;
    let session = connection
        .begin_discover_gatt()
        .discovery
        .expect("discovery submission")
        .wait_result(WAIT_MS as f64)
        .await
        .session
        .expect("discovered session");
    (connection, session)
}

#[test]
fn discovery_projects_the_exact_snapshot_as_capabilities_without_handles() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([discover(
            4,
            2,
            GATT_SESSION,
        )]));
        fixture.supervisor().start().await.unwrap();
        let (_, session) = begin_session(&mut fixture).await;

        let services = session.services();
        assert_eq!(services.len(), 2);
        assert_eq!(
            services[0].uuid().value,
            "0000180d-0000-1000-8000-00805f9b34fb"
        );
        assert_eq!(services[0].primary(), Some(true));
        assert_eq!(services[1].primary(), Some(false));
        let characteristics = services[0].characteristics();
        assert_eq!(characteristics.len(), 1);
        assert_eq!(
            characteristics[0].uuid().value,
            "00002a37-0000-1000-8000-00805f9b34fb"
        );
        assert_eq!(characteristics[0].properties(), properties());
        // Descriptors are capabilities: the handle stays native and the UUID is data.
        let descriptors = characteristics[0].descriptors();
        assert_eq!(descriptors.len(), 1);
        assert_eq!(descriptors[0].uuid().value, CCCD);

        // A rediscovery later produces a different session identity, never a re-pointed old one.
        assert!(session.same_session(Arc::clone(&session)));
        fixture.shutdown().await;
    });
}

#[test]
fn characteristic_read_and_write_are_single_consumer_receipts() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            read(5, 2, GATT_SESSION, CHARACTERISTIC_A, vec![0x10, 0x11]),
            write(6, 2, GATT_SESSION, CHARACTERISTIC_A, vec![0x20]),
            read(7, 2, GATT_SESSION, CHARACTERISTIC_A, Vec::new()),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (_, session) = begin_session(&mut fixture).await;
        let characteristic = session.services()[0].characteristics()[0].clone();

        // An invalid timeout is an input failure that dispatches nothing.
        let dispatched = fixture.source.backend.commands().len();
        let read = characteristic.begin_read().read.expect("read submission");
        assert_failure(
            read.wait_result(0.5).await.failure,
            ForeignOperationFailureDomain::Input,
            "invalid_timeout",
        );
        assert_eq!(fixture.source.backend.commands().len(), dispatched);

        let value = read.wait_result(WAIT_MS as f64).await;
        assert!(value.failure.is_none(), "{:?}", value.failure);
        assert_eq!(value.value, Some(vec![0x10, 0x11]));
        assert_failure(
            read.wait_result(WAIT_MS as f64).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_result_consumed",
        );

        let write = characteristic
            .begin_write(vec![0x20], WriteType::WithResponse)
            .write
            .expect("write submission");
        let receipt = write.wait_result(WAIT_MS as f64).await;
        assert!(receipt.failure.is_none(), "{:?}", receipt.failure);
        assert_failure(
            write.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );

        // An empty value is a real read outcome, not a missing one.
        let empty = characteristic.begin_read().read.expect("read submission");
        let value = empty.wait_result(WAIT_MS as f64).await;
        assert!(value.failure.is_none());
        assert_eq!(value.value, Some(Vec::new()));
        fixture.shutdown().await;
    });
}

#[test]
fn a_replaced_topology_cannot_be_dispatched_through() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            // The second discovery replaces the reservation with a new session identity.
            discover(5, 2, GATT_SESSION + 1),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (connection, old) = begin_session(&mut fixture).await;
        let stale = old.services()[0].characteristics()[0].clone();
        let replacement = connection
            .begin_discover_gatt()
            .discovery
            .expect("discovery submission")
            .wait_result(WAIT_MS as f64)
            .await
            .session
            .expect("replacement session");
        assert!(!old.same_session(replacement));

        // Submission stays bounded and accepted; admission is decided by Rust, so the rejection
        // arrives on the original wait and nothing is dispatched.
        let read = stale.begin_read().read.expect("bounded submission");
        assert_failure(
            read.wait_result(WAIT_MS as f64).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.not_ready.gatt_not_ready",
        );
        let dispatched = fixture.source.backend.commands().len();
        let write = stale
            .begin_write(vec![0x01], WriteType::WithResponse)
            .write
            .expect("bounded submission");
        assert_failure(
            write.wait_result(WAIT_MS as f64).await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.not_ready.gatt_not_ready",
        );
        assert_eq!(fixture.source.backend.commands().len(), dispatched);
        fixture.shutdown().await;
    });
}

#[test]
fn rssi_is_a_bounded_receipt_that_preserves_zero_and_negative_values() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            rssi(5, 2, 0),
            rssi(6, 2, -71),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (connection, _) = begin_session(&mut fixture).await;

        let reading = connection.begin_read_rssi().rssi.expect("rssi submission");
        let zero = reading.wait_result(WAIT_MS as f64).await;
        assert!(zero.failure.is_none());
        assert_eq!(zero.rssi, Some(0));
        let negative = connection.begin_read_rssi().rssi.expect("rssi submission");
        assert_eq!(negative.wait_result(WAIT_MS as f64).await.rssi, Some(-71));
        // Cancelling an already-completed operation is a typed receipt, not a rollback claim.
        assert_failure(
            reading.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn mtu_query_and_request_report_platform_limits_without_echoing_the_input() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            mtu(
                5,
                2,
                None,
                MtuInfo {
                    mtu: None,
                    maximum_write_with_response: Some(182),
                    maximum_write_without_response: Some(185),
                },
            ),
            mtu(
                6,
                2,
                Some(247),
                MtuInfo {
                    mtu: Some(185),
                    maximum_write_with_response: Some(182),
                    maximum_write_without_response: Some(185),
                },
            ),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (connection, _) = begin_session(&mut fixture).await;

        // A query is still a bounded typed receipt, and an absent MTU stays absent.
        let query = connection.begin_query_mtu().mtu.expect("query submission");
        let snapshot = query.wait_result(WAIT_MS as f64).await;
        assert!(snapshot.failure.is_none());
        let snapshot = snapshot.info.expect("query receipt");
        assert_eq!(snapshot.mtu, None);
        assert_eq!(snapshot.maximum_write_with_response, Some(182));
        assert_eq!(snapshot.maximum_write_without_response, Some(185));

        // The request value is an input; the receipt is the platform's own negotiated report.
        let request = connection
            .begin_request_mtu(247)
            .mtu
            .expect("request submission");
        let snapshot = request.wait_result(WAIT_MS as f64).await;
        assert!(snapshot.failure.is_none());
        assert_eq!(snapshot.info.expect("request receipt").mtu, Some(185));
        // Cancelling an already-completed operation is a typed receipt, not a rollback claim.
        assert_failure(
            request.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn descriptor_read_and_write_are_single_consumer_capability_receipts() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            descriptor_read(5, 2, GATT_SESSION, CHARACTERISTIC_A, 701, vec![0x01, 0x00]),
            descriptor_write(6, 2, GATT_SESSION, CHARACTERISTIC_A, 701, vec![0x00, 0x00]),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (_, session) = begin_session(&mut fixture).await;
        let descriptor = session.services()[0].characteristics()[0].descriptors()[0].clone();

        // A descriptor is only addressable through the characteristic that discovered it.
        assert_eq!(descriptor.uuid().value, CCCD);
        let read = descriptor
            .begin_read()
            .read
            .expect("descriptor read submission");
        let value = read.wait_result(WAIT_MS as f64).await;
        assert!(
            value.failure.is_none(),
            "descriptor read failure: {:?}",
            value.failure
        );
        assert_eq!(value.value, Some(vec![0x01, 0x00]));
        // A value receipt is single-consumer.
        let consumed = read.wait_result(WAIT_MS as f64).await;
        assert_failure(
            consumed.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_result_consumed",
        );

        let write = descriptor
            .begin_write(vec![0x00, 0x00])
            .write
            .expect("descriptor write submission");
        let receipt = write.wait_result(WAIT_MS as f64).await;
        assert!(receipt.failure.is_none());
        assert_failure(
            write.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn connection_priority_is_a_typed_terminal_receipt_without_a_link_mode() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            priority(5, 2, ConnectionPriority::High),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (connection, _) = begin_session(&mut fixture).await;

        let receipt = connection
            .begin_set_connection_priority(ConnectionPriority::High)
            .priority
            .expect("priority submission");
        let result = receipt.wait_result(WAIT_MS as f64).await;
        // The platform reports no effective mode, so the receipt is failure-free with no value.
        assert!(result.failure.is_none());
        assert_failure(
            receipt.cancel_result().await.failure,
            ForeignOperationFailureDomain::Operation,
            "bleng.operation_already_completed",
        );
        fixture.shutdown().await;
    });
}

#[test]
fn bonding_reports_the_platform_terminal_state() {
    run(|| async {
        let mut fixture = Fixture::new(
            advertised()
                .into_iter()
                .chain([discover(4, 2, GATT_SESSION), bond(5, 2, BondState::Bonded)]),
        );
        fixture.supervisor().start().await.unwrap();
        let (connection, _) = begin_session(&mut fixture).await;

        let receipt = connection
            .begin_create_bond(None)
            .bond
            .expect("bond submission");
        let result = receipt.wait_result(WAIT_MS as f64).await;
        assert!(result.failure.is_none());
        assert_eq!(result.state, Some(BondState::Bonded));
        fixture.shutdown().await;
    });
}

#[test]
fn legacy_pairing_pin_is_forwarded_to_the_platform_command() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            bond_with_pin(5, 2, Some("123456"), BondState::Bonded),
        ]));
        fixture.supervisor().start().await.unwrap();
        let (connection, _) = begin_session(&mut fixture).await;

        let receipt = connection
            .begin_create_bond(Some("123456".to_owned()))
            .bond
            .expect("bond submission");
        let result = receipt.wait_result(WAIT_MS as f64).await;
        assert!(result.failure.is_none());
        assert_eq!(result.state, Some(BondState::Bonded));
        fixture.shutdown().await;
    });
}

#[test]
fn a_retired_supervisor_leaves_every_gatt_capability_detached() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([discover(
            4,
            2,
            GATT_SESSION,
        )]));
        fixture.supervisor().start().await.unwrap();
        let (connection, session) = begin_session(&mut fixture).await;
        let characteristic = session.services()[0].characteristics()[0].clone();
        fixture.shutdown().await;

        for failure in [
            connection.begin_discover_gatt().failure,
            connection.begin_read_rssi().failure,
            connection.begin_query_mtu().failure,
            connection.begin_request_mtu(247).failure,
            connection
                .begin_set_connection_priority(ConnectionPriority::Balanced)
                .failure,
            characteristic.descriptors()[0].begin_read().failure,
            characteristic.descriptors()[0]
                .begin_write(vec![0x00])
                .failure,
            characteristic.begin_read().failure,
            characteristic
                .begin_write(vec![0x01], WriteType::WithResponse)
                .failure,
        ] {
            assert_failure(
                failure,
                ForeignOperationFailureDomain::Lifecycle,
                "supervisor.no_active_session",
            );
        }
    });
}
