use std::sync::Arc;

use blehub::*;
use blehub_testkit::*;

#[test]
fn duplicate_uuid_instances_keep_distinct_opaque_ids() {
    let database = duplicate_gatt_database_fixture();
    assert_eq!(database.services[0].uuid, database.services[1].uuid);
    assert_ne!(database.services[0].id, database.services[1].id);
    assert_eq!(
        database.services[0].characteristics[0].uuid,
        database.services[1].characteristics[0].uuid
    );
    assert_ne!(
        database.services[0].characteristics[0].id,
        database.services[1].characteristics[0].id
    );
    assert_eq!(
        database.services[0].characteristics[0].descriptors[0].uuid,
        database.services[1].characteristics[0].descriptors[0].uuid
    );
    assert_ne!(
        database.services[0].characteristics[0].descriptors[0].id,
        database.services[1].characteristics[0].descriptors[0].id
    );
}

#[test]
fn unified_event_catalog_crosses_the_production_ingress_unchanged() {
    let expected = unified_event_catalog_fixture();
    let (backend, handle) = FakeBackend::scripted([]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();

    let statuses = expected
        .iter()
        .cloned()
        .map(|event| handle.emit(event))
        .collect::<Vec<_>>();

    assert_eq!(sink.events(), expected);
    assert_eq!(statuses, vec![EventIngressStatus::Delivered; 17]);
    hub.dispose().unwrap();
}

#[test]
fn ingress_rejects_noncanonical_uuids_and_duplicate_gatt_ids() {
    let (backend, handle) = FakeBackend::scripted([]);
    let sink = RecordingSink::new();
    let hub = BleHub::new(Arc::new(backend), Box::new(sink.clone())).unwrap();
    let mut advertisement = advertisement_observation_fixture();
    advertisement.advertised_services[0].value = "180D".to_owned();
    assert_eq!(
        handle.emit(BleEvent::AdvertisementObserved { advertisement }),
        EventIngressStatus::InvalidEvent {
            message: "advertised_services[0] is not a canonical Bluetooth UUID".to_owned(),
        }
    );

    let mut database = duplicate_gatt_database_fixture();
    database.services[1].id = database.services[0].id;
    assert_eq!(
        handle.emit(BleEvent::GattDiscovered {
            command_id: CommandId { value: 1 },
            database,
        }),
        EventIngressStatus::InvalidEvent {
            message: "services[1].id duplicates another GATT service ID".to_owned(),
        }
    );
    assert!(sink.events().is_empty());
    hub.dispose().unwrap();
}

#[test]
fn gatt_discovery_event_owns_session_ids_only_inside_the_database() {
    let event = BleEvent::GattDiscovered {
        command_id: CommandId { value: 1 },
        database: duplicate_gatt_database_fixture(),
    };
    let BleEvent::GattDiscovered {
        command_id,
        database,
    } = event
    else {
        unreachable!();
    };
    assert_eq!(command_id, CommandId { value: 1 });
    assert_eq!(database.connection_id, ConnectionId { value: 301 });
    assert_eq!(database.gatt_session_id, GattSessionId { value: 401 });
}
