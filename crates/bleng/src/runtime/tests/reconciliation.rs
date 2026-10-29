use blehub::{
    BleErrorCategory, BleEvent, BleEventError, CommandDispatchError, NotificationModeRequest,
    ScanStopReason, ScanStopResult,
};

use crate::runtime::reservations::{
    ConnectionReservations, DispatchAction, InitializationState, ReconciliationFault, ScanState,
};

use super::support::{
    WaiterError, WaiterSuccess, command_completed, connected_event, disconnected_event,
    init_snapshot, init_snapshot_event,
};

#[test]
fn pre_ack_buffer_is_bounded_and_replays_correlated_evidence_in_order() {
    let mut initialization = InitializationState::default();
    initialization.reserve_initialize(1.into());
    let events: Vec<_> = (0..64)
        .map(|index| {
            let mut snapshot = init_snapshot();
            snapshot.name = Some(index.to_string());
            BleEvent::AdapterSnapshotChanged {
                command_id: Some(1.into()),
                snapshot,
            }
        })
        .collect();
    for event in &events {
        assert_eq!(initialization.gate_event(event.clone()).unwrap(), None);
    }
    let foreign = BleEvent::AdapterSnapshotChanged {
        command_id: Some(99.into()),
        snapshot: init_snapshot(),
    };
    assert_eq!(
        initialization.gate_event(foreign.clone()).unwrap(),
        Some(foreign)
    );
    assert!(matches!(
        initialization.gate_event(events[0].clone()),
        Err(ReconciliationFault::BufferOverflow)
    ));

    let resolution = initialization.resolve_dispatch(1.into(), &Ok(())).unwrap();
    assert!(matches!(resolution.action, DispatchAction::None));
    assert_eq!(resolution.events_to_replay, events);
    for event in &resolution.events_to_replay {
        initialization.record_event(event);
    }
    assert_eq!(initialization.waiter_result(1.into()), None);
    assert!(initialization.record_terminal(1.into(), &Ok(())));
    let mut expected_snapshot = init_snapshot();
    expected_snapshot.name = Some("0".to_owned());
    assert_eq!(
        initialization.waiter_result(1.into()),
        Some(Ok(WaiterSuccess::Initialized(expected_snapshot))),
        "replay preserves the first correlated snapshot, not the latest duplicate"
    );
}

#[test]
fn init_publication_uses_one_64_event_buffer_and_keeps_earlier_passive_snapshots_immediate() {
    for terminal_first in [false, true] {
        let mut initialization = InitializationState::default();
        initialization.reserve_initialize(1.into());
        let passive = |index: usize| {
            let mut snapshot = init_snapshot();
            snapshot.name = Some(format!("passive-{index}"));
            BleEvent::AdapterSnapshotChanged {
                command_id: None,
                snapshot,
            }
        };
        let earlier = passive(0);
        assert_eq!(
            initialization.gate_event(earlier.clone()).unwrap(),
            Some(earlier)
        );
        let domain = init_snapshot_event(Some(1));
        let terminal = command_completed(1, None);
        let (first, last) = if terminal_first {
            (terminal, domain)
        } else {
            (domain, terminal)
        };
        assert_eq!(initialization.gate_event(first.clone()).unwrap(), None);
        let mut events = vec![first];
        // The second correlated event comes after the passive updates, so even
        // terminal-first input cannot use a passive snapshot as success evidence.
        for index in 1..=62 {
            let event = passive(index);
            assert_eq!(initialization.gate_event(event.clone()).unwrap(), None);
            events.push(event);
        }
        assert_eq!(initialization.gate_event(last.clone()).unwrap(), None);
        events.push(last);
        let foreign = init_snapshot_event(Some(99));
        assert_eq!(
            initialization.gate_event(foreign.clone()).unwrap(),
            Some(foreign)
        );
        assert!(matches!(
            initialization.gate_event(passive(63)),
            Err(ReconciliationFault::BufferOverflow)
        ));
        let resolution = initialization.resolve_dispatch(1.into(), &Ok(())).unwrap();
        assert!(matches!(resolution.action, DispatchAction::None));
        assert_eq!(resolution.events_to_replay, events);
        for event in &resolution.events_to_replay[..63] {
            if let BleEvent::CommandCompleted { command_id, .. } = event {
                initialization.record_terminal(*command_id, &Ok(()));
            } else {
                initialization.record_event(event);
            }
        }
        assert_eq!(initialization.waiter_result(1.into()), None);
        let last = resolution.events_to_replay.last().unwrap();
        if let BleEvent::CommandCompleted { command_id, .. } = last {
            initialization.record_terminal(*command_id, &Ok(()));
        } else {
            initialization.record_event(last);
        }
        assert_eq!(
            initialization.waiter_result(1.into()),
            Some(Ok(WaiterSuccess::Initialized(init_snapshot())))
        );
        initialization.advance_after_settlement().unwrap();
        let after_ack = passive(64);
        assert_eq!(
            initialization.gate_event(after_ack.clone()).unwrap(),
            Some(after_ack)
        );
    }
}

#[test]
fn initialization_dispatch_rejection_is_final_and_correlates_late_evidence() {
    let mut initialization = InitializationState::default();
    initialization.reserve_initialize(1.into());
    let rejection = Err(CommandDispatchError::InvalidState {
        detail: "initialization rejected without callbacks".to_owned(),
    });
    let resolution = initialization
        .resolve_dispatch(1.into(), &rejection)
        .unwrap();
    assert!(matches!(resolution.action, DispatchAction::StartCleanup));
    assert!(resolution.events_to_replay.is_empty());
    assert_eq!(
        initialization.require_not_started(),
        Err(WaiterError::OperationBusy)
    );
    assert_eq!(
        initialization.require_initialized(),
        Err(WaiterError::NotInitialized)
    );

    // Duplicate dispatch results cannot revive or re-settle a rejected operation.
    assert!(initialization.resolve_dispatch(1.into(), &Ok(())).is_none());
    assert!(
        initialization
            .resolve_dispatch(1.into(), &rejection)
            .is_none()
    );
    for foreign in [
        init_snapshot_event(None),
        init_snapshot_event(Some(99)),
        command_completed(99, None),
    ] {
        assert_eq!(
            initialization.gate_event(foreign.clone()).unwrap(),
            Some(foreign)
        );
    }
    for late in [init_snapshot_event(Some(1)), command_completed(1, None)] {
        assert!(matches!(
            initialization.gate_event(late.clone()),
            Err(ReconciliationFault::DispatchEvidenceConflict)
        ));
        initialization.record_event(&late);
    }
    assert!(!initialization.record_terminal(1.into(), &Ok(())));
    assert_eq!(initialization.waiter_result(1.into()), None);
    initialization.advance_after_settlement().unwrap();
    assert_eq!(
        initialization.require_initialized(),
        Err(WaiterError::NotInitialized)
    );
}

#[test]
fn scan_stop_rejection_discards_terminals_and_preserves_replay_order_across_conflicts() {
    for include_conflict in [false, true] {
        let mut scan = ScanState::default();
        scan.reserve_scan_start(1.into(), 10.into());
        scan.resolve_dispatch(1.into(), &Ok(())).unwrap();
        scan.record_event(&BleEvent::ScanStarted {
            command_id: 1.into(),
            scan_id: 10.into(),
        });
        assert!(scan.record_terminal(1.into(), &Ok(())));
        scan.reserve_scan_stop(2.into());

        let terminal = BleEvent::CommandCompleted {
            command_id: 2.into(),
            error: None,
        };
        let suspended = BleEvent::ScanStopped {
            scan_id: 10.into(),
            result: ScanStopResult::Ok {
                reason: ScanStopReason::SystemSuspended,
            },
        };
        let failed = BleEvent::ScanStopped {
            scan_id: 10.into(),
            result: ScanStopResult::Err {
                error: BleEventError {
                    category: BleErrorCategory::AdapterUnavailable,
                    message: "adapter stopped scanning independently".to_owned(),
                    platform_details: None,
                },
            },
        };
        let mut events = vec![terminal.clone(), suspended.clone()];
        if include_conflict {
            events.push(BleEvent::ScanStopped {
                scan_id: 10.into(),
                result: ScanStopResult::Ok {
                    reason: ScanStopReason::Requested {
                        command_id: 2.into(),
                    },
                },
            });
        }
        events.extend([terminal, failed.clone()]);
        for event in events {
            assert_eq!(scan.gate_event(event).unwrap(), None);
        }

        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "stop rejected after early callbacks".to_owned(),
        });
        let resolution = scan.resolve_dispatch(2.into(), &rejection).unwrap();
        assert_eq!(resolution.events_to_replay, vec![suspended, failed]);
        if include_conflict {
            assert!(matches!(
                resolution.action,
                DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict)
            ));
        } else {
            assert!(matches!(resolution.action, DispatchAction::None));
        }
        assert_eq!(scan.stop_command(), None);
        assert_eq!(scan.validate_scan_stop(10.into()), Ok(()));
        assert_eq!(scan.require_scan_available(), Err(WaiterError::ScanBusy));

        // Resolution only transfers evidence. The actor decides whether to replay;
        // a conflicting result would fault before publishing any of these events.
        if !include_conflict {
            for event in &resolution.events_to_replay {
                scan.record_event(event);
            }
            scan.retire_completed();
            assert_eq!(scan.require_scan_available(), Ok(()));
        }
    }
}

#[test]
fn rejection_replay_preserves_distinct_io_and_subscription_loss_policies() {
    for (subscription, include_passive) in
        [(false, false), (true, false), (false, true), (true, true)]
    {
        let mut connections = ConnectionReservations::default();
        connections.reserve_connection(1.into(), 1.into(), 10.into());
        connections.resolve_dispatch(1.into(), &Ok(())).unwrap();
        connections.record_event(&connected_event(1, 10)).unwrap();
        connections.record_terminal(1.into(), &Ok(()));
        if subscription {
            connections.reserve_notification_enable(
                1.into(),
                2.into(),
                40.into(),
                20.into(),
                30.into(),
                NotificationModeRequest::PlatformDefault,
            );
        } else {
            connections.reserve_read(1.into(), 2.into(), 20.into(), 30.into());
        }

        // A commanded loss belonging to the connect lifecycle is not foreign,
        // but only subscription rejection permits replaying this kind of loss.
        let loss = disconnected_event(Some(1), 10);
        assert!(!connections.is_foreign_disconnect(&loss));
        let mut events = vec![loss.clone()];
        if include_passive {
            events.push(disconnected_event(None, 10));
        }
        for event in &events {
            assert_eq!(connections.gate_event(event.clone()).unwrap(), None);
        }
        let expected_replay: Vec<_> = events
            .into_iter()
            .filter(|event| subscription || event.command_id().is_none())
            .collect();
        let rejection = Err(CommandDispatchError::InvalidState {
            detail: "rejected after native connection loss".to_owned(),
        });
        let resolution = connections.resolve_dispatch(2.into(), &rejection).unwrap();
        // A conflict must not stop classification of subsequent passive events.
        assert_eq!(resolution.events_to_replay, expected_replay);
        assert_eq!(
            connections.waiter_result(1.into()),
            Some(Ok(WaiterSuccess::Connected(10.into())))
        );
        assert_eq!(connections.validate_gatt_discovery(10.into()), Ok(1.into()));
        assert!(connections.resolve_dispatch(2.into(), &Ok(())).is_none());
        if subscription {
            assert!(matches!(resolution.action, DispatchAction::None));

            assert_eq!(
                connections.validate_notification_disable(10.into(), 20.into(), 40.into()),
                Err(WaiterError::SubscriptionNotActive)
            );
            // The actor, not resolve_dispatch(), decides when to record replay.
            for event in &resolution.events_to_replay {
                connections.record_event(event).unwrap();
            }
            connections.retire_completed();
            assert_eq!(connections.require_peripheral_available(1.into()), Ok(()));
            assert_eq!(
                connections.validate_rssi(10.into()),
                Err(WaiterError::ConnectionNotActive)
            );
        } else {
            assert!(matches!(
                resolution.action,
                DispatchAction::FaultRuntime(ReconciliationFault::DispatchEvidenceConflict)
            ));

            assert_eq!(connections.validate_gatt_discovery(10.into()), Ok(1.into()));
        }
    }
}
