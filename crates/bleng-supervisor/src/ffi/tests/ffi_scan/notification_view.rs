//! Notification subscription projection: one bounded lane per subscription, bound by its own
//! enable completion, with no subscription identity crossing the boundary.

use super::*;
use crate::{ForeignNotificationEndReason, ForeignOperationFailureDomain, MAX_NOTIFICATION_VALUES};
use blehub::{BleErrorCategory, BleEventError};
use blehub::{
    CharacteristicProperties, GattCharacteristic, GattCharacteristicId, GattDatabase,
    GattDescriptor, GattDescriptorId, GattService, GattServiceId, GattSessionId,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState,
    SetNotificationsCommand, SubscriptionId,
};

const GATT_SESSION: u64 = 3;
const SUBSCRIPTION: u64 = 4;
const CHARACTERISTIC: u64 = 601;
const ENABLE_COMMAND: u64 = 5;
const DISABLE_COMMAND: u64 = 6;
/// A second attribute on the same discovered session: one owner, two live subscriptions.
/// Resource and command ids are allocated in submission order (scan 1 / link 2 / gatt 3, commands
/// from 2), so a second enable after one discovery is command 6 and subscription 5.
const OTHER_SUBSCRIPTION: u64 = 5;
const OTHER_ENABLE_COMMAND: u64 = 6;
const OTHER_CHARACTERISTIC: u64 = 602;

fn gatt_uuid(value: &str) -> BluetoothUuid {
    BluetoothUuid::canonicalize(value).expect("canonical UUID")
}

fn database(connection: u64, session: u64) -> GattDatabase {
    GattDatabase {
        connection_id: connection.into(),
        gatt_session_id: GattSessionId::from(session),
        services: vec![GattService {
            id: GattServiceId::from(501),
            uuid: gatt_uuid("0000180d-0000-1000-8000-00805f9b34fb"),
            primary: Some(true),
            characteristics: vec![GattCharacteristic {
                id: GattCharacteristicId::from(CHARACTERISTIC),
                uuid: gatt_uuid("00002a37-0000-1000-8000-00805f9b34fb"),
                properties: CharacteristicProperties {
                    readable: true,
                    writable_with_response: false,
                    writable_without_response: false,
                    notifiable: true,
                    indicatable: true,
                },
                descriptors: vec![GattDescriptor {
                    id: GattDescriptorId::from(701),
                    uuid: gatt_uuid("00002902-0000-1000-8000-00805f9b34fb"),
                }],
            }],
        }],
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

fn state_change(
    command: u64,
    connection: u64,
    session: u64,
    subscription: u64,
    state: NotificationState,
) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id: command.into(),
        connection_id: connection.into(),
        gatt_session_id: session.into(),
        subscription_id: subscription.into(),
        characteristic_id: CHARACTERISTIC.into(),
        state,
    }
}

fn enabled_event(actual_mode: Option<NotificationMode>) -> BleEvent {
    state_change(
        ENABLE_COMMAND,
        2,
        GATT_SESSION,
        SUBSCRIPTION,
        NotificationState::Enabled { actual_mode },
    )
}

fn disabled_event() -> BleEvent {
    state_change(
        DISABLE_COMMAND,
        2,
        GATT_SESSION,
        SUBSCRIPTION,
        NotificationState::Disabled,
    )
}

fn enable(actual_mode: Option<NotificationMode>) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::SetNotifications(SetNotificationsCommand {
            command_id: ENABLE_COMMAND.into(),
            connection_id: 2.into(),
            gatt_session_id: GATT_SESSION.into(),
            subscription_id: SUBSCRIPTION.into(),
            characteristic_id: CHARACTERISTIC.into(),
            action: NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        }),
        vec![enabled_event(actual_mode), completed(ENABLE_COMMAND)],
    )
}

fn disable() -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::SetNotifications(SetNotificationsCommand {
            command_id: DISABLE_COMMAND.into(),
            connection_id: 2.into(),
            gatt_session_id: GATT_SESSION.into(),
            subscription_id: SUBSCRIPTION.into(),
            characteristic_id: CHARACTERISTIC.into(),
            action: NotificationAction::Disable,
        }),
        vec![disabled_event(), completed(DISABLE_COMMAND)],
    )
}

fn notified(session: u64, subscription: u64, value: Vec<u8>) -> BleEvent {
    BleEvent::NotificationReceived {
        connection_id: 2.into(),
        gatt_session_id: GattSessionId::from(session),
        subscription_id: SubscriptionId::from(subscription),
        characteristic_id: CHARACTERISTIC.into(),
        value,
    }
}

fn notified_from(session: u64, subscription: u64, characteristic: u64, value: Vec<u8>) -> BleEvent {
    BleEvent::NotificationReceived {
        connection_id: 2.into(),
        gatt_session_id: GattSessionId::from(session),
        subscription_id: SubscriptionId::from(subscription),
        characteristic_id: characteristic.into(),
        value,
    }
}

/// The same discovered session, plus a second notifiable attribute.
fn database_with_two_targets(connection: u64, session: u64) -> GattDatabase {
    let mut database = database(connection, session);
    let first = &database.services[0].characteristics[0];
    let second = GattCharacteristic {
        id: GattCharacteristicId::from(OTHER_CHARACTERISTIC),
        uuid: gatt_uuid("00002a38-0000-1000-8000-00805f9b34fb"),
        properties: first.properties,
        descriptors: vec![GattDescriptor {
            id: GattDescriptorId::from(702),
            uuid: gatt_uuid("00002902-0000-1000-8000-00805f9b34fb"),
        }],
    };
    database.services[0].characteristics.push(second);
    database
}

fn discover_two_targets(command: u64, connection: u64, session: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::DiscoverGatt(DiscoverGattCommand {
            command_id: command.into(),
            connection_id: connection.into(),
            gatt_session_id: session.into(),
        }),
        vec![
            BleEvent::GattDiscovered {
                command_id: command.into(),
                database: database_with_two_targets(connection, session),
            },
            completed(command),
        ],
    )
}

fn state_change_for(
    command: u64,
    session: u64,
    subscription: u64,
    characteristic: u64,
    state: NotificationState,
) -> BleEvent {
    BleEvent::NotificationStateChanged {
        command_id: command.into(),
        connection_id: 2.into(),
        gatt_session_id: session.into(),
        subscription_id: subscription.into(),
        characteristic_id: characteristic.into(),
        state,
    }
}

fn enable_on(command: u64, subscription: u64, characteristic: u64) -> EventScenario {
    EventScenario::emit_before_return(
        RecordedCommand::SetNotifications(SetNotificationsCommand {
            command_id: command.into(),
            connection_id: 2.into(),
            gatt_session_id: GATT_SESSION.into(),
            subscription_id: subscription.into(),
            characteristic_id: characteristic.into(),
            action: NotificationAction::Enable {
                mode: NotificationModeRequest::PlatformDefault,
            },
        }),
        vec![
            state_change_for(
                command,
                GATT_SESSION,
                subscription,
                characteristic,
                NotificationState::Enabled { actual_mode: None },
            ),
            completed(command),
        ],
    )
}

/// The shared preamble plus one discovered characteristic.
async fn discovered_characteristic(
    fixture: &mut Fixture,
) -> Arc<crate::SupervisedGattCharacteristic> {
    let connection = begin_connection(fixture).await;
    let session = connection
        .begin_discover_gatt()
        .discovery
        .expect("discovery submission")
        .wait_result(WAIT_MS as f64)
        .await
        .session
        .expect("discovered session");
    session.services()[0].characteristics()[0].clone()
}

/// Submits an enable and drives the owner's reader through its own state change, the way a real
/// platform reader does continuously.
async fn subscribe(
    fixture: &mut Fixture,
    characteristic: &Arc<crate::SupervisedGattCharacteristic>,
    actual_mode: Option<NotificationMode>,
) -> Arc<crate::SupervisedNotifications> {
    let notifications = characteristic
        .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
        .notifications
        .expect("bounded submission");
    fixture.source.observed(enabled_event(actual_mode)).await;
    fixture.source.observed(completed(ENABLE_COMMAND)).await;
    notifications
}

#[test]
fn a_subscription_delivers_values_in_order_and_reports_its_actual_mode() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            enable(Some(NotificationMode::Indication)),
        ]));
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = subscribe(
            &mut fixture,
            &characteristic,
            Some(NotificationMode::Indication),
        )
        .await;

        let receipt = notifications.wait_result(WAIT_MS as f64).await;
        assert!(receipt.failure.is_none(), "{:?}", receipt.failure);
        // The actual mode comes back with the enable, not from a second event lookup.
        assert_eq!(receipt.actual_mode, Some(NotificationMode::Indication));
        assert!(notifications.delivery_snapshot().bound);

        for value in [vec![0x01], Vec::new(), vec![0x02, 0x03]] {
            fixture
                .source
                .emit_and_observe(notified(GATT_SESSION, SUBSCRIPTION, value.clone()))
                .await;
            let observation = notifications.next_observation(WAIT_MS as f64).await;
            assert!(observation.failure.is_none(), "{:?}", observation.failure);
            assert!(observation.ended.is_none());
            // An empty value is a real notification, not a missing one.
            assert_eq!(observation.value, Some(value));
        }
        fixture.shutdown().await;
    });
}

#[test]
fn a_disable_ends_the_stream_and_never_synthesizes_another_value() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            enable(None),
            disable(),
        ]));
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = subscribe(&mut fixture, &characteristic, None).await;
        let receipt = notifications.wait_result(WAIT_MS as f64).await;
        assert!(receipt.failure.is_none());
        // An unreported mode is not "notification".
        assert_eq!(receipt.actual_mode, None);

        let disabled = notifications
            .begin_disable_view()
            .operation
            .expect("disable submission");
        fixture.source.observed(disabled_event()).await;
        fixture.source.observed(completed(DISABLE_COMMAND)).await;
        assert!(disabled.wait_result(WAIT_MS as f64).await.failure.is_none());
        let ended = notifications.next_observation(WAIT_MS as f64).await;
        assert!(ended.value.is_none() && ended.failure.is_none());
        assert_eq!(
            ended.ended.unwrap().reason,
            ForeignNotificationEndReason::Disabled
        );
        // The end is a retained fact, so a consumer polling on its own schedule still learns it.
        for _ in 0..2 {
            let again = notifications.next_observation(WAIT_MS as f64).await;
            assert!(again.value.is_none() && again.failure.is_none());
            assert_eq!(
                again.ended.unwrap().reason,
                ForeignNotificationEndReason::Disabled
            );
        }
        fixture.shutdown().await;
        // Retirement replaces the retained end with a definite owner-closed terminal.
        let retired = notifications.next_observation(WAIT_MS as f64).await;
        assert_eq!(
            retired.ended.unwrap().reason,
            ForeignNotificationEndReason::OwnerClosed
        );
    });
}

#[test]
fn disable_before_the_subscription_is_observed_is_refused_not_guessed() {
    run(|| async {
        let mut fixture = Fixture::new(advertised().into_iter().chain([discover(
            4,
            2,
            GATT_SESSION,
        )]));
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = characteristic
            .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
            .notifications
            .expect("bounded submission");
        let refused = notifications.begin_disable_view();
        assert_failure(
            refused.failure,
            ForeignOperationFailureDomain::Submission,
            "notification.subscription_unknown",
        );
        assert!(refused.operation.is_none());
        fixture.shutdown().await;
    });
}

#[test]
fn a_slow_consumer_ends_only_its_observation_lane_instead_of_dropping_values() {
    run(|| async {
        let mut fixture = Fixture::new(
            advertised()
                .into_iter()
                .chain([discover(4, 2, GATT_SESSION), enable(None)]),
        );
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = subscribe(&mut fixture, &characteristic, None).await;
        assert!(
            notifications
                .wait_result(WAIT_MS as f64)
                .await
                .failure
                .is_none()
        );

        // No pull happens: the backlog must refuse more rather than silently discard.
        for index in 0..MAX_NOTIFICATION_VALUES {
            fixture
                .source
                .emit_and_observe(notified(GATT_SESSION, SUBSCRIPTION, vec![index as u8]))
                .await;
        }
        assert_eq!(
            notifications.delivery_snapshot().queued_values,
            MAX_NOTIFICATION_VALUES as u64
        );
        fixture
            .source
            .emit_and_observe(notified(GATT_SESSION, SUBSCRIPTION, vec![0xff]))
            .await;

        // Overflow ends only this observation lane, not the subscription or the runtime: the unread
        // backlog is discarded because ordering/completeness can no longer be claimed.
        let observation = notifications.next_observation(WAIT_MS as f64).await;
        assert!(
            observation.value.is_none(),
            "an overflowed stream must not deliver a value as if nothing happened"
        );
        assert!(observation.failure.is_none());
        assert_eq!(
            observation.ended.unwrap().reason,
            ForeignNotificationEndReason::Overflow
        );
        assert_eq!(notifications.delivery_snapshot().queued_values, 0);
        fixture.shutdown().await;
    });
}

#[test]
fn a_retired_owner_ends_the_stream_as_owner_closed_without_native_teardown_claims() {
    run(|| async {
        let mut fixture = Fixture::new(
            advertised()
                .into_iter()
                .chain([discover(4, 2, GATT_SESSION), enable(None)]),
        );
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = subscribe(&mut fixture, &characteristic, None).await;
        assert!(
            notifications
                .wait_result(WAIT_MS as f64)
                .await
                .failure
                .is_none()
        );
        fixture.shutdown().await;

        let ended = notifications.next_observation(WAIT_MS as f64).await;
        assert!(ended.value.is_none() && ended.failure.is_none());
        assert_eq!(
            ended.ended.unwrap().reason,
            ForeignNotificationEndReason::OwnerClosed
        );
        assert_failure(
            characteristic
                .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
                .failure,
            ForeignOperationFailureDomain::Lifecycle,
            "supervisor.no_active_session",
        );
    });
}

#[test]
fn a_second_subscription_never_takes_over_the_first_stream() {
    run(|| async {
        // Two live subscriptions on one owner. Scopes are per lane: the earlier enable must keep
        // delivering its own values after a later enable is installed, otherwise a second link (or
        // a second attribute) silently steals the first subscription's stream.
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover_two_targets(4, 2, GATT_SESSION),
            enable_on(ENABLE_COMMAND, SUBSCRIPTION, CHARACTERISTIC),
            enable_on(
                OTHER_ENABLE_COMMAND,
                OTHER_SUBSCRIPTION,
                OTHER_CHARACTERISTIC,
            ),
        ]));
        fixture.supervisor().start().await.unwrap();
        let connection = begin_connection(&mut fixture).await;
        let session = connection
            .begin_discover_gatt()
            .discovery
            .expect("discovery submission")
            .wait_result(WAIT_MS as f64)
            .await
            .session
            .expect("discovered session");
        let characteristics = session.services()[0].characteristics();
        let first_characteristic = characteristics[0].clone();
        let second_characteristic = characteristics[1].clone();
        assert_eq!(characteristics.len(), 2);

        let first = first_characteristic
            .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
            .notifications
            .expect("first bounded submission");
        fixture
            .source
            .observed(state_change_for(
                ENABLE_COMMAND,
                GATT_SESSION,
                SUBSCRIPTION,
                CHARACTERISTIC,
                NotificationState::Enabled { actual_mode: None },
            ))
            .await;
        fixture.source.observed(completed(ENABLE_COMMAND)).await;
        assert!(first.wait_result(WAIT_MS as f64).await.failure.is_none());

        let second = second_characteristic
            .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
            .notifications
            .expect("second bounded submission");
        fixture
            .source
            .observed(state_change_for(
                OTHER_ENABLE_COMMAND,
                GATT_SESSION,
                OTHER_SUBSCRIPTION,
                OTHER_CHARACTERISTIC,
                NotificationState::Enabled { actual_mode: None },
            ))
            .await;
        fixture
            .source
            .observed(completed(OTHER_ENABLE_COMMAND))
            .await;
        assert!(second.wait_result(WAIT_MS as f64).await.failure.is_none());

        // The first subscription is still the only consumer of its own attribute.
        fixture
            .source
            .emit_and_observe(notified_from(
                GATT_SESSION,
                SUBSCRIPTION,
                CHARACTERISTIC,
                vec![0x01],
            ))
            .await;
        assert_eq!(second.delivery_snapshot().queued_values, 0);
        let delivered = first.next_observation(WAIT_MS as f64).await;
        assert!(delivered.failure.is_none(), "{:?}", delivered.failure);
        assert_eq!(delivered.value, Some(vec![0x01]));

        // And the second lane is bound to its own attribute, not to the newest submission.
        fixture
            .source
            .emit_and_observe(notified_from(
                GATT_SESSION,
                OTHER_SUBSCRIPTION,
                OTHER_CHARACTERISTIC,
                vec![0x02],
            ))
            .await;
        let delivered = second.next_observation(WAIT_MS as f64).await;
        assert_eq!(delivered.value, Some(vec![0x02]));
        assert_eq!(first.delivery_snapshot().queued_values, 0);
        fixture.shutdown().await;
    });
}

#[test]
fn a_refused_enable_does_not_block_the_retry_on_the_same_characteristic() {
    run(|| async {
        // The command is admitted and the peer then refuses the CCC write. The lane exists but
        // never binds: that is a failed subscription, not an occupied slot. The surface must still
        // accept a second enable for the same attribute — otherwise a retry reports the lane's own
        // bookkeeping instead of the peer's answer, which is what a single-slot lane did.
        let mut fixture = Fixture::new(advertised().into_iter().chain([
            discover(4, 2, GATT_SESSION),
            EventScenario::emit_before_return(
                RecordedCommand::SetNotifications(SetNotificationsCommand {
                    command_id: ENABLE_COMMAND.into(),
                    connection_id: 2.into(),
                    gatt_session_id: GATT_SESSION.into(),
                    subscription_id: SUBSCRIPTION.into(),
                    characteristic_id: CHARACTERISTIC.into(),
                    action: NotificationAction::Enable {
                        mode: NotificationModeRequest::PlatformDefault,
                    },
                }),
                vec![BleEvent::CommandCompleted {
                    command_id: ENABLE_COMMAND.into(),
                    error: Some(BleEventError {
                        category: BleErrorCategory::Gatt,
                        message: "ATT 0x0e: CCC write refused".to_owned(),
                        platform_details: None,
                    }),
                }],
            ),
            enable_on(6, 5, CHARACTERISTIC),
        ]));
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;

        let refused = characteristic
            .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
            .notifications
            .expect("bounded submission");
        let receipt = refused.wait_result(WAIT_MS as f64).await;
        assert!(
            receipt.failure.is_some(),
            "the peer's refusal must surface as a failed subscription"
        );
        assert!(!refused.delivery_snapshot().bound);

        // The retry is admitted even though the refused lane is still held by this caller.
        let retry = characteristic
            .begin_enable_notifications(NotificationModeRequest::PlatformDefault)
            .notifications
            .expect("a refused enable must not occupy the attribute");
        fixture
            .source
            .observed(state_change_for(
                6,
                GATT_SESSION,
                5,
                CHARACTERISTIC,
                NotificationState::Enabled { actual_mode: None },
            ))
            .await;
        fixture.source.observed(completed(6)).await;
        assert!(retry.wait_result(WAIT_MS as f64).await.failure.is_none());

        // And the retried lane delivers, while the refused one stays silent on the same attribute.
        fixture
            .source
            .emit_and_observe(notified_from(GATT_SESSION, 5, CHARACTERISTIC, vec![0x2a]))
            .await;
        let delivered = retry.next_observation(WAIT_MS as f64).await;
        assert_eq!(delivered.value, Some(vec![0x2a]));
        assert_eq!(refused.delivery_snapshot().queued_values, 0);
        fixture.shutdown().await;
    });
}

#[test]
fn a_value_from_another_subscription_is_never_delivered() {
    run(|| async {
        let mut fixture = Fixture::new(
            advertised()
                .into_iter()
                .chain([discover(4, 2, GATT_SESSION), enable(None)]),
        );
        fixture.supervisor().start().await.unwrap();
        let characteristic = discovered_characteristic(&mut fixture).await;
        let notifications = subscribe(&mut fixture, &characteristic, None).await;
        assert!(
            notifications
                .wait_result(WAIT_MS as f64)
                .await
                .failure
                .is_none()
        );

        // Another subscription's values, and another session's values, are not this stream.
        fixture
            .source
            .emit_and_observe(notified(GATT_SESSION, SUBSCRIPTION + 1, vec![0xaa]))
            .await;
        fixture
            .source
            .emit_and_observe(notified(GATT_SESSION + 1, SUBSCRIPTION, vec![0xbb]))
            .await;
        assert_eq!(notifications.delivery_snapshot().queued_values, 0);
        fixture
            .source
            .emit_and_observe(notified(GATT_SESSION, SUBSCRIPTION, vec![0xcc]))
            .await;
        let observation = notifications.next_observation(WAIT_MS as f64).await;
        assert_eq!(observation.value, Some(vec![0xcc]));
        fixture.shutdown().await;
    });
}
