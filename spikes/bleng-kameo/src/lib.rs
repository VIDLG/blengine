//! Standalone Kameo evaluation for the future BLeng async facade.
//!
//! The model in this file is deliberately small: it models operation and
//! connection identity, not the production BleHub contract. The point is to
//! verify which lifecycle and correlation rules must live above Kameo.

use kameo::mailbox;
use kameo::prelude::*;
use std::collections::{HashMap, HashSet};
use tokio::sync::Notify;

const MAILBOX_CAPACITY: usize = 2;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct WaiterKey {
    pub command_id: u64,
    pub connection_id: u64,
    pub generation: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DomainEvent {
    ReadValue,
    WriteAccepted,
    Notification,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalEvent {
    Completed,
    Failed,
    Disconnected,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WaiterState {
    Pending,
    DomainObserved,
    Completed,
    TimedOut,
    Cancelled,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Reply)]
pub enum EventOutcome {
    IgnoredUnknown,
    IgnoredStale,
    DomainObserved,
    Completed,
    AlreadyTerminal,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Reply)]
pub enum RegisterOutcome {
    Registered,
    RejectedDuplicate,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Reply)]
pub struct WaiterSnapshot {
    pub key: WaiterKey,
    pub state: WaiterState,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Reply)]
pub struct DeviceSnapshot {
    pub connection_id: u64,
    pub active_waiters: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LabEvent {
    Domain(DomainEvent),
    Terminal(TerminalEvent),
}

#[derive(Clone, Copy, Debug)]
pub struct RegisterWaiter {
    pub key: WaiterKey,
}

#[derive(Clone, Copy, Debug)]
pub struct PushEvent {
    pub key: WaiterKey,
    pub event: LabEvent,
}

#[derive(Clone, Copy, Debug)]
pub struct TimeoutWaiter {
    pub key: WaiterKey,
}

#[derive(Clone, Copy, Debug)]
pub struct CancelWaiter {
    pub key: WaiterKey,
}

#[derive(Clone, Copy, Debug)]
pub struct InspectWaiter {
    pub key: WaiterKey,
}

#[derive(Clone, Copy, Debug)]
pub struct InspectDevice {
    pub connection_id: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct HoldMailbox {
    pub started: &'static Notify,
    pub release: &'static Notify,
}

#[derive(Actor)]
pub struct LabActor {
    waiters: HashMap<WaiterKey, WaiterState>,
    connections: HashSet<u64>,
}

impl LabActor {
    pub fn new() -> Self {
        Self {
            waiters: HashMap::new(),
            connections: HashSet::new(),
        }
    }
}

impl Default for LabActor {
    fn default() -> Self {
        Self::new()
    }
}

impl Message<RegisterWaiter> for LabActor {
    type Reply = RegisterOutcome;

    async fn handle(
        &mut self,
        message: RegisterWaiter,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        if self.waiters.contains_key(&message.key) {
            return RegisterOutcome::RejectedDuplicate;
        }
        self.connections.insert(message.key.connection_id);
        self.waiters.insert(message.key, WaiterState::Pending);
        RegisterOutcome::Registered
    }
}

impl Message<PushEvent> for LabActor {
    type Reply = EventOutcome;

    async fn handle(
        &mut self,
        message: PushEvent,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let Some(state) = self.waiters.get_mut(&message.key) else {
            return EventOutcome::IgnoredUnknown;
        };

        match (*state, message.event) {
            (WaiterState::Pending, LabEvent::Domain(_)) => {
                *state = WaiterState::DomainObserved;
                EventOutcome::DomainObserved
            }
            (WaiterState::DomainObserved, LabEvent::Terminal(_))
            | (WaiterState::Pending, LabEvent::Terminal(_)) => {
                *state = WaiterState::Completed;
                EventOutcome::Completed
            }
            (WaiterState::Completed | WaiterState::TimedOut | WaiterState::Cancelled, _) => {
                EventOutcome::AlreadyTerminal
            }
            (WaiterState::DomainObserved, LabEvent::Domain(_)) => EventOutcome::IgnoredStale,
        }
    }
}

impl Message<TimeoutWaiter> for LabActor {
    type Reply = bool;

    async fn handle(
        &mut self,
        message: TimeoutWaiter,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let Some(state) = self.waiters.get_mut(&message.key) else {
            return false;
        };
        if matches!(*state, WaiterState::Pending | WaiterState::DomainObserved) {
            *state = WaiterState::TimedOut;
            true
        } else {
            false
        }
    }
}

impl Message<CancelWaiter> for LabActor {
    type Reply = bool;

    async fn handle(
        &mut self,
        message: CancelWaiter,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let Some(state) = self.waiters.get_mut(&message.key) else {
            return false;
        };
        if matches!(*state, WaiterState::Pending | WaiterState::DomainObserved) {
            *state = WaiterState::Cancelled;
            true
        } else {
            false
        }
    }
}

impl Message<InspectWaiter> for LabActor {
    type Reply = Option<WaiterSnapshot>;

    async fn handle(
        &mut self,
        message: InspectWaiter,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.waiters
            .get(&message.key)
            .copied()
            .map(|state| WaiterSnapshot {
                key: message.key,
                state,
            })
    }
}

impl Message<InspectDevice> for LabActor {
    type Reply = DeviceSnapshot;

    async fn handle(
        &mut self,
        message: InspectDevice,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        DeviceSnapshot {
            connection_id: message.connection_id,
            active_waiters: self
                .waiters
                .iter()
                .filter(|(key, state)| {
                    key.connection_id == message.connection_id
                        && matches!(state, WaiterState::Pending | WaiterState::DomainObserved)
                })
                .count(),
        }
    }
}

impl Message<HoldMailbox> for LabActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: HoldMailbox,
        _: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        message.started.notify_one();
        message.release.notified().await;
    }
}

pub struct PanicMessage;

impl Message<PanicMessage> for LabActor {
    type Reply = ();

    async fn handle(&mut self, _: PanicMessage, _: &mut Context<Self, Self::Reply>) -> Self::Reply {
        panic!("intentional spike panic: actor must fail closed");
    }
}

pub fn spawn_lab() -> ActorRef<LabActor> {
    LabActor::spawn_with_mailbox(LabActor::new(), mailbox::bounded(MAILBOX_CAPACITY))
}

#[cfg(test)]
async fn ask<M>(
    actor: &ActorRef<LabActor>,
    message: M,
) -> Result<
    <<LabActor as Message<M>>::Reply as Reply>::Ok,
    kameo::error::SendError<M, <<LabActor as Message<M>>::Reply as Reply>::Error>,
>
where
    LabActor: Message<M>,
    M: Send + 'static,
{
    actor.ask(message).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn key(command_id: u64, connection_id: u64, generation: u64) -> WaiterKey {
        WaiterKey {
            command_id,
            connection_id,
            generation,
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn bounded_mailbox_has_explicit_capacity_and_timeout() {
        let actor = spawn_lab();
        let started = Box::leak(Box::new(Notify::new()));
        let release = Box::leak(Box::new(Notify::new()));

        actor
            .tell(HoldMailbox { started, release })
            .await
            .expect("first message accepted");
        started.notified().await;

        actor
            .tell(HoldMailbox { started, release })
            .try_send()
            .expect("the first queued message fits in the explicit mailbox");
        actor
            .tell(HoldMailbox { started, release })
            .try_send()
            .expect("the second queued message fits in the explicit mailbox");
        let error = actor
            .tell(HoldMailbox { started, release })
            .try_send()
            .expect_err("the next message must observe mailbox overflow");
        assert!(matches!(error, kameo::error::SendError::MailboxFull(_)));
        let error = actor
            .tell(HoldMailbox { started, release })
            .mailbox_timeout(Duration::from_millis(10))
            .await
            .expect_err("a full mailbox must have bounded backpressure");
        assert!(matches!(error, kameo::error::SendError::Timeout(_)));

        for _ in 0..3 {
            release.notify_waiters();
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        actor.stop_gracefully().await.expect("actor stops");
        actor.wait_for_shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn waiter_requires_domain_and_terminal_semantics_and_completes_once() {
        let actor = spawn_lab();
        let waiter = key(1, 10, 1);
        assert_eq!(
            ask(&actor, RegisterWaiter { key: waiter }).await.unwrap(),
            RegisterOutcome::Registered
        );
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: waiter,
                    event: LabEvent::Domain(DomainEvent::ReadValue),
                },
            )
            .await
            .unwrap(),
            EventOutcome::DomainObserved
        );
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: waiter,
                    event: LabEvent::Terminal(TerminalEvent::Completed),
                },
            )
            .await
            .unwrap(),
            EventOutcome::Completed
        );
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: waiter,
                    event: LabEvent::Terminal(TerminalEvent::Completed),
                },
            )
            .await
            .unwrap(),
            EventOutcome::AlreadyTerminal
        );
        assert_eq!(
            ask(&actor, InspectWaiter { key: waiter })
                .await
                .unwrap()
                .unwrap()
                .state,
            WaiterState::Completed
        );
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn two_devices_interleave_without_cross_completing_waiters() {
        let actor = spawn_lab();
        let device_a = key(1, 100, 1);
        let device_b = key(2, 200, 1);
        for waiter in [device_a, device_b] {
            assert_eq!(
                ask(&actor, RegisterWaiter { key: waiter }).await.unwrap(),
                RegisterOutcome::Registered
            );
        }

        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: device_b,
                    event: LabEvent::Domain(DomainEvent::Notification),
                },
            )
            .await
            .unwrap(),
            EventOutcome::DomainObserved
        );
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: device_a,
                    event: LabEvent::Terminal(TerminalEvent::Disconnected),
                },
            )
            .await
            .unwrap(),
            EventOutcome::Completed
        );
        assert_eq!(
            ask(&actor, InspectDevice { connection_id: 200 })
                .await
                .unwrap()
                .active_waiters,
            1
        );
        assert_eq!(
            ask(&actor, InspectWaiter { key: device_b })
                .await
                .unwrap()
                .unwrap()
                .state,
            WaiterState::DomainObserved
        );
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn timeout_cancellation_and_late_generation_events_fail_closed() {
        let actor = spawn_lab();
        let timed_out = key(3, 300, 1);
        let cancelled = key(4, 300, 1);
        let new_generation = key(5, 300, 2);
        for waiter in [timed_out, cancelled, new_generation] {
            assert_eq!(
                ask(&actor, RegisterWaiter { key: waiter }).await.unwrap(),
                RegisterOutcome::Registered
            );
        }
        assert!(ask(&actor, TimeoutWaiter { key: timed_out }).await.unwrap());
        assert!(ask(&actor, CancelWaiter { key: cancelled }).await.unwrap());

        for stale in [timed_out, cancelled] {
            assert_eq!(
                ask(
                    &actor,
                    PushEvent {
                        key: stale,
                        event: LabEvent::Terminal(TerminalEvent::Completed),
                    },
                )
                .await
                .unwrap(),
                EventOutcome::AlreadyTerminal
            );
        }
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: key(99, 300, 1),
                    event: LabEvent::Terminal(TerminalEvent::Completed),
                },
            )
            .await
            .unwrap(),
            EventOutcome::IgnoredUnknown
        );
        assert_eq!(
            ask(
                &actor,
                PushEvent {
                    key: new_generation,
                    event: LabEvent::Terminal(TerminalEvent::Completed),
                },
            )
            .await
            .unwrap(),
            EventOutcome::Completed
        );
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn handler_returns_after_dispatch_and_does_not_wait_for_callback() {
        let actor = spawn_lab();
        let waiter = key(6, 600, 1);
        let registration = tokio::time::timeout(
            Duration::from_millis(100),
            ask(&actor, RegisterWaiter { key: waiter }),
        )
        .await
        .expect("registration is not blocked by a future callback")
        .unwrap();
        assert_eq!(registration, RegisterOutcome::Registered);

        let completion = tokio::time::timeout(
            Duration::from_millis(100),
            ask(
                &actor,
                PushEvent {
                    key: waiter,
                    event: LabEvent::Terminal(TerminalEvent::Completed),
                },
            ),
        )
        .await
        .expect("later event is handled")
        .unwrap();
        assert_eq!(completion, EventOutcome::Completed);
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn panic_stops_actor_and_does_not_look_like_successful_recovery() {
        let actor = spawn_lab();
        actor
            .tell(PanicMessage)
            .await
            .expect("panic message was accepted by the mailbox");
        actor.wait_for_shutdown().await;
        let error = ask(&actor, InspectDevice { connection_id: 1 })
            .await
            .expect_err("a stopped actor rejects messages after a panic");
        assert!(matches!(
            error,
            kameo::error::SendError::ActorNotRunning(_) | kameo::error::SendError::ActorStopped
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn stop_is_terminal_and_new_messages_are_rejected() {
        let actor = spawn_lab();
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
        let error = ask(&actor, InspectDevice { connection_id: 1 })
            .await
            .expect_err("stopped actor rejects new messages");
        assert!(matches!(
            error,
            kameo::error::SendError::ActorNotRunning(_) | kameo::error::SendError::ActorStopped
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancellation_is_idempotent_and_close_has_no_active_waiters() {
        let actor = spawn_lab();
        let waiter = key(7, 700, 1);
        assert_eq!(
            ask(&actor, RegisterWaiter { key: waiter }).await.unwrap(),
            RegisterOutcome::Registered
        );
        assert!(ask(&actor, CancelWaiter { key: waiter }).await.unwrap());
        assert!(!ask(&actor, CancelWaiter { key: waiter }).await.unwrap());
        assert_eq!(
            ask(&actor, InspectDevice { connection_id: 700 })
                .await
                .unwrap()
                .active_waiters,
            0
        );
        actor.stop_gracefully().await.unwrap();
        actor.wait_for_shutdown().await;
    }
}
