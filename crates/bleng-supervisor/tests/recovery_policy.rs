use std::collections::BTreeSet;

use bleng_supervisor::{
    BackendId, OwnerBackendIdentity, OwnerGeneration, OwnerId, RecoveryAction, RecoveryAttemptId,
    RecoveryConnectionId, RecoveryContext, RecoveryError, RecoveryGattSessionId, RecoveryLink,
    RecoveryMode, RecoveryObservation, RecoveryOperationId, RecoveryOutcome, RecoveryPhase,
    RecoveryPlan, RecoveryPolicy, RecoveryPolicyConfig, RecoverySignal, RecoverySubscriptionId,
};

fn context() -> RecoveryContext {
    context_for("owner-old", "backend-old", 1)
}

fn context_for(owner: &str, backend: &str, generation: u64) -> RecoveryContext {
    RecoveryContext::new(
        identity(owner, backend),
        OwnerGeneration::new(generation).unwrap(),
        RecoveryAttemptId::new(1).unwrap(),
    )
}

fn identity(owner: &str, backend: &str) -> OwnerBackendIdentity {
    OwnerBackendIdentity::new(owner, backend).unwrap()
}

fn link(connection: &str, gatt_session: &str, subscription: &str) -> RecoveryLink {
    RecoveryLink::new(connection, gatt_session, subscription).unwrap()
}

fn operation(id: &str) -> RecoveryOperationId {
    RecoveryOperationId::new(id).unwrap()
}

fn terminals(ids: &[&str]) -> BTreeSet<RecoveryOperationId> {
    ids.iter().map(|id| operation(id)).collect()
}

fn apply_with_context(
    policy: &mut RecoveryPolicy,
    context: RecoveryContext,
    signal: RecoverySignal,
) -> Result<Vec<RecoveryAction>, RecoveryError> {
    policy
        .apply(RecoveryObservation::new(context, signal))
        .map(RecoveryPlan::into_actions)
}

fn apply(
    policy: &mut RecoveryPolicy,
    signal: RecoverySignal,
) -> Result<Vec<RecoveryAction>, RecoveryError> {
    let context = policy.active_context();
    apply_with_context(policy, context.clone(), signal)
}

fn policy(ids: &[&str]) -> RecoveryPolicy {
    RecoveryPolicy::new(RecoveryPolicyConfig {
        initial_context: context(),
        old_link: Some(link("connection-old", "gatt-old", "subscription-old")),
        expected_terminals: terminals(ids),
        allow_central_recreation: true,
        allow_fresh_owner: true,
    })
}

#[test]
fn typed_id_constructors_reject_empty_values() {
    assert_eq!(
        OwnerId::new(""),
        Err(RecoveryError::InvalidIdentityField("owner_id"))
    );
    assert_eq!(
        BackendId::new(""),
        Err(RecoveryError::InvalidIdentityField("backend_id"))
    );
    assert_eq!(
        RecoveryConnectionId::new(""),
        Err(RecoveryError::InvalidLinkField("connection_id"))
    );
    assert_eq!(
        RecoveryGattSessionId::new(""),
        Err(RecoveryError::InvalidLinkField("gatt_session_id"))
    );
    assert_eq!(
        RecoverySubscriptionId::new(""),
        Err(RecoveryError::InvalidLinkField("subscription_id"))
    );
    assert_eq!(
        RecoveryOperationId::new(""),
        Err(RecoveryError::EmptyOperationId)
    );
    assert_eq!(
        OwnerGeneration::new(0),
        Err(RecoveryError::InvalidOwnerGeneration)
    );
    assert_eq!(
        RecoveryAttemptId::new(0),
        Err(RecoveryError::InvalidRecoveryAttemptId)
    );
}

fn start_recovery(policy: &mut RecoveryPolicy, ids: &[&str]) {
    assert_eq!(
        apply(policy, RecoverySignal::RadioLost).unwrap()[0],
        RecoveryAction::WaitForRadioOn
    );
    assert_eq!(
        apply(policy, RecoverySignal::RadioOn).unwrap(),
        vec![RecoveryAction::AwaitOldOperationTerminals {
            remaining: terminals(ids),
        }]
    );
    for id in ids {
        let actions = apply(
            policy,
            RecoverySignal::OldOperationTerminal {
                operation_id: operation(id),
            },
        )
        .unwrap();
        if *id == ids.last().copied().unwrap() {
            assert_eq!(
                actions,
                vec![RecoveryAction::AttemptSameOwnerRecovery {
                    after_central_recreation: false,
                }]
            );
        }
    }
}

#[test]
fn old_owner_or_attempt_observations_are_rejected() {
    let mut policy = policy(&[]);
    let old_context = RecoveryContext::new(
        identity("owner-old", "backend-old"),
        OwnerGeneration::new(1).unwrap(),
        RecoveryAttemptId::new(2).unwrap(),
    );
    assert_eq!(
        policy.apply(RecoveryObservation::new(
            old_context.clone(),
            RecoverySignal::RadioLost
        )),
        Err(RecoveryError::ObservationContextMismatch {
            expected: Box::new(context()),
            received: Box::new(old_context),
        })
    );
}

#[test]
fn terminal_policy_rejects_late_observation_before_context_validation() {
    let mut policy = policy(&[]);
    apply(
        &mut policy,
        RecoverySignal::PlatformTimedOut {
            reason: "budget".into(),
        },
    )
    .unwrap();
    assert_eq!(
        apply_with_context(
            &mut policy,
            context_for("owner-new", "backend-new", 2),
            RecoverySignal::FreshOwnerCreated,
        ),
        Err(RecoveryError::AlreadyTerminal)
    );
}

#[test]
fn radio_on_cannot_bypass_old_terminal_barrier() {
    let mut policy = policy(&["read", "write"]);
    start_recovery(&mut policy, &["read", "write"]);
    assert_eq!(policy.phase(), RecoveryPhase::RecoveringSameOwner);
}

#[test]
fn duplicate_and_unknown_terminals_are_rejected() {
    let mut policy = policy(&["read"]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OldOperationTerminal {
                operation_id: operation("unknown"),
            },
        ),
        Err(RecoveryError::UnknownOperation(operation("unknown")))
    );
    apply(
        &mut policy,
        RecoverySignal::OldOperationTerminal {
            operation_id: operation("read"),
        },
    )
    .unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OldOperationTerminal {
                operation_id: operation("read"),
            },
        ),
        Err(RecoveryError::DuplicateOperationTerminal(operation("read")))
    );
}

#[test]
fn late_old_operation_terminal_cannot_restart_same_owner_recovery() {
    let mut policy = policy(&["read"]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(
        &mut policy,
        RecoverySignal::OldOperationTerminal {
            operation_id: operation("read"),
        },
    )
    .unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::RecoveringSameOwner);

    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OldOperationTerminal {
                operation_id: operation("read"),
            },
        ),
        Err(RecoveryError::LateOldOperationTerminal(operation("read")))
    );
    assert_eq!(policy.phase(), RecoveryPhase::RecoveringSameOwner);
}

#[test]
fn same_owner_recovery_requires_old_identity_and_new_link() {
    let mut policy = policy(&[]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::RecoveringSameOwner);
    assert!(matches!(
        apply_with_context(
            &mut policy,
            context_for("other-owner", "backend-old", 1),
            RecoverySignal::SameOwnerRecovered {
                link: link("connection-new", "gatt-new", "subscription-new"),
            },
        ),
        Err(RecoveryError::ObservationContextMismatch { .. })
    ));
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecovered {
            link: link("connection-new", "gatt-new", "subscription-new"),
        },
    )
    .unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::Recovered);
    assert_eq!(policy.mode(), Some(RecoveryMode::SameOwner));
}

#[test]
fn central_recreation_is_single_fallback_before_retirement() {
    let mut policy = policy(&[]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::SameOwnerRecoveryFailed {
                reason: "no advertisement".into(),
            },
        )
        .unwrap(),
        vec![RecoveryAction::RecreateCentral]
    );
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OwnerRetired {
                resources_zero: true
            }
        ),
        Err(RecoveryError::CentralRecreationPending)
    );
    assert_eq!(
        apply(&mut policy, RecoverySignal::CentralRecreated).unwrap(),
        vec![RecoveryAction::AttemptSameOwnerRecovery {
            after_central_recreation: true,
        }]
    );
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::SameOwnerRecoveryFailed {
                reason: "still absent".into(),
            },
        )
        .unwrap(),
        vec![RecoveryAction::RetireOldOwner]
    );
}

#[test]
fn central_recreation_failure_completes_before_retirement() {
    let mut policy = policy(&[]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::SameOwnerRecoveryFailed {
                reason: "same owner unavailable".into(),
            },
        )
        .unwrap(),
        vec![RecoveryAction::RecreateCentral]
    );
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::CentralRecreationFailed {
                reason: "central unavailable".into(),
            },
        )
        .unwrap(),
        vec![RecoveryAction::RetireOldOwner]
    );
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OwnerRetired {
                resources_zero: true,
            },
        )
        .unwrap(),
        vec![RecoveryAction::CreateFreshOwner]
    );
}

#[test]
fn returned_plan_is_fenced_to_the_observation_context() {
    let mut policy = policy(&[]);
    let plan = policy
        .apply(RecoveryObservation::new(
            context(),
            RecoverySignal::RadioLost,
        ))
        .unwrap();
    assert_eq!(plan.execution_context(), &context());
    assert_eq!(plan.sequence(), 1);
    assert_eq!(plan.actions(), &[RecoveryAction::WaitForRadioOn]);

    let next_plan = policy
        .apply(RecoveryObservation::new(context(), RecoverySignal::RadioOn))
        .unwrap();
    assert_eq!(next_plan.sequence(), 2);
    assert_eq!(
        next_plan.actions(),
        &[RecoveryAction::AttemptSameOwnerRecovery {
            after_central_recreation: false,
        }]
    );
}

#[test]
fn fresh_owner_plan_uses_the_adopted_context() {
    let mut policy = policy(&[]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    apply(&mut policy, RecoverySignal::CentralRecreated).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    apply(
        &mut policy,
        RecoverySignal::OwnerRetired {
            resources_zero: true,
        },
    )
    .unwrap();
    let fresh_context = context_for("owner-new", "backend-new", 2);
    let plan = policy
        .apply(RecoveryObservation::new(
            fresh_context.clone(),
            RecoverySignal::FreshOwnerCreated,
        ))
        .unwrap();
    assert_eq!(plan.execution_context(), &fresh_context);
    assert_eq!(plan.actions(), &[RecoveryAction::AttemptFreshOwnerRecovery]);
}

#[test]
fn fresh_owner_requires_resource_zero_and_non_reused_identity() {
    let mut policy = RecoveryPolicy::new(RecoveryPolicyConfig {
        initial_context: context_for("owner-old", "backend-old", 2),
        old_link: Some(link("connection-old", "gatt-old", "subscription-old")),
        expected_terminals: terminals(&[]),
        allow_central_recreation: true,
        allow_fresh_owner: true,
    });
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    apply(&mut policy, RecoverySignal::CentralRecreated).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::OwnerRetired {
                resources_zero: false,
            }
        ),
        Err(RecoveryError::OwnerResourcesNotZero)
    );
    apply(
        &mut policy,
        RecoverySignal::OwnerRetired {
            resources_zero: true,
        },
    )
    .unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::CreatingFreshOwner);
    assert!(matches!(
        apply_with_context(
            &mut policy,
            context_for("owner-new", "backend-new", 1),
            RecoverySignal::FreshOwnerCreated,
        ),
        Err(RecoveryError::ObservationContextMismatch { .. })
    ));
    assert_eq!(
        apply_with_context(
            &mut policy,
            context_for("owner-old", "backend-new", 3),
            RecoverySignal::FreshOwnerCreated,
        ),
        Err(RecoveryError::FreshOwnerReused)
    );
    assert_eq!(
        apply_with_context(
            &mut policy,
            context_for("owner-new", "backend-old", 3),
            RecoverySignal::FreshOwnerCreated,
        ),
        Err(RecoveryError::FreshOwnerReused)
    );
    apply_with_context(
        &mut policy,
        context_for("owner-new", "backend-new", 3),
        RecoverySignal::FreshOwnerCreated,
    )
    .unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::RecoveringFreshOwner);
    apply(
        &mut policy,
        RecoverySignal::FreshOwnerRecoveryFailed {
            reason: "fresh owner unavailable".into(),
        },
    )
    .unwrap();
    assert_eq!(
        policy.outcome(),
        Some(&RecoveryOutcome::ManualReconnectRequired {
            context: context_for("owner-new", "backend-new", 3),
            reason: "fresh owner unavailable".into(),
        })
    );
}

#[test]
fn fresh_owner_recovery_rejects_link_reuse_and_publishes_mode() {
    let mut policy = policy(&[]);
    apply(&mut policy, RecoverySignal::RadioLost).unwrap();
    apply(&mut policy, RecoverySignal::RadioOn).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    apply(&mut policy, RecoverySignal::CentralRecreated).unwrap();
    apply(
        &mut policy,
        RecoverySignal::SameOwnerRecoveryFailed {
            reason: "none".into(),
        },
    )
    .unwrap();
    apply(
        &mut policy,
        RecoverySignal::OwnerRetired {
            resources_zero: true,
        },
    )
    .unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::CreatingFreshOwner);
    apply_with_context(
        &mut policy,
        context_for("owner-new", "backend-new", 2),
        RecoverySignal::FreshOwnerCreated,
    )
    .unwrap();
    assert_eq!(
        apply(
            &mut policy,
            RecoverySignal::FreshOwnerRecovered {
                link: link("connection-old", "gatt-old", "subscription-old"),
            },
        ),
        Err(RecoveryError::LinkReused)
    );
    apply(
        &mut policy,
        RecoverySignal::FreshOwnerRecovered {
            link: link("connection-fresh", "gatt-fresh", "subscription-fresh"),
        },
    )
    .unwrap();
    assert_eq!(policy.mode(), Some(RecoveryMode::FreshOwner));
    let fresh_context = context_for("owner-new", "backend-new", 2);
    assert_eq!(*policy.active_context(), fresh_context);
    assert!(matches!(
        policy.outcome(),
        Some(RecoveryOutcome::Recovered {
            context,
            mode: RecoveryMode::FreshOwner,
            ..
        }) if *context == fresh_context
    ));
}

#[test]
fn terminal_outcomes_reject_late_signals() {
    let mut policy = policy(&[]);
    apply(
        &mut policy,
        RecoverySignal::PlatformTimedOut {
            reason: "budget".into(),
        },
    )
    .unwrap();
    assert_eq!(policy.phase(), RecoveryPhase::PlatformTimeout);
    assert_eq!(
        apply(&mut policy, RecoverySignal::RadioLost),
        Err(RecoveryError::AlreadyTerminal)
    );
}

#[test]
fn timeout_and_manual_reconnect_are_distinct_outcomes() {
    let mut timeout = policy(&[]);
    apply(
        &mut timeout,
        RecoverySignal::PlatformTimedOut {
            reason: "budget".into(),
        },
    )
    .unwrap();
    assert_eq!(
        timeout.outcome(),
        Some(&RecoveryOutcome::PlatformTimeout {
            context: context(),
            reason: "budget".into(),
        })
    );

    let mut manual = policy(&[]);
    apply(
        &mut manual,
        RecoverySignal::ManualReconnectRequired {
            reason: "user action".into(),
        },
    )
    .unwrap();
    assert_eq!(
        manual.outcome(),
        Some(&RecoveryOutcome::ManualReconnectRequired {
            context: context(),
            reason: "user action".into(),
        })
    );
}
