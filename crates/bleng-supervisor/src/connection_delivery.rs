//! One connection's own end, observed on the owner's single event reader.
//!
//! A link has exactly one terminal, and it is a *retained* fact rather than a consumable receipt:
//! reading it twice returns it twice, so a consumer that starts observing late — or that was busy
//! when the link ended — can still learn what happened. Exactly-once belongs to the connect result,
//! which is a different fact with a different owner.
//!
//! The lane is created when the connect is submitted, so it exists before the platform can publish
//! the connected state it binds to. Binding happens on *this* lane's own connect command, so two
//! connections of one owner cannot observe each other's end, and an unrelated connection ending
//! cannot make this one look finished.
//!
//! Attribution is a fact about this capability, never a guess about the peer. A terminal caused by
//! a disconnect this capability submitted is `Operator`; any other terminal is `Unrequested`,
//! because "the link ended and we did not ask" is all a central can know. A platform error is
//! carried exactly as reported; its absence is not evidence of a clean peer request.

use std::sync::{Arc, Mutex, MutexGuard, atomic::AtomicBool};

use blehub::{
    BleEvent, BleEventError, CommandId, ConnectionId, ConnectionState, ConnectionStateResult,
};
use bleng::ConnectOperation;
use tokio::sync::Notify;

use crate::SupervisorOperationError;
use crate::pull_guard::PullGuard;
use crate::recovery::RecoveryContext;

#[derive(Debug, thiserror::Error)]
pub enum ConnectionSubmissionError {
    #[error("connection submission rejected: {error}")]
    Owner {
        #[from]
        error: SupervisorOperationError,
    },
    #[error("owner retired before connection submission")]
    OwnerRetired,
    #[error("connection lane bookkeeping unavailable")]
    Unavailable,
}

/// The original connect operation plus the lane registered before it was submitted.
pub struct ConnectionHandoff {
    pub operation: Arc<ConnectOperation>,
    pub lane: Arc<ConnectionLane>,
}

/// Who ended the link, as far as this capability can observe it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionEndBy {
    /// A disconnect this capability submitted is the command that produced the terminal.
    Operator,
    /// The link ended without this capability requesting it. The peer, the local stack or an
    /// adapter reset are all possible; a reported platform error is the only further evidence.
    Unrequested,
    /// The owner retired, so nothing more can be observed. This is the end of *observation*, not
    /// evidence that the link is down.
    Owner,
}

/// The one terminal this lane retained. `error` is the platform's own report, when it gave one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectionEnd {
    pub by: ConnectionEndBy,
    pub error: Option<BleEventError>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionLaneError {
    /// Another end pull is already pending for this connection.
    Pending,
    /// Lane bookkeeping was poisoned or the projection dropped this lane.
    Unavailable,
}

/// What this lane currently knows about the link it follows.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Phase {
    /// Connect submitted; the platform has not confirmed *this* connection yet. A connect failure
    /// is reported by the original result and is deliberately not an end: no link was established.
    Connecting,
    Linked(ConnectionId),
    /// The first terminal wins; a later event cannot replace it.
    Ended(ConnectionEnd),
    /// Bookkeeping failed; observation can never resume.
    Closed,
}

#[derive(Debug)]
struct LaneState {
    phase: Phase,
    /// Set only after a disconnect of this capability was submitted to the platform.
    operator_requested: bool,
}

/// One owner/connection-bound end observation endpoint.
///
/// Only this connection's own events can end it: the lane binds on its own connect command and
/// then accepts only that connection's identity. Holds no native handle and no strong owner
/// reference, so a retained endpoint cannot keep a retired owner alive.
pub struct ConnectionLane {
    source_context: RecoveryContext,
    connect_command_id: CommandId,
    state: Mutex<LaneState>,
    changed: Notify,
    pulling: AtomicBool,
}

impl std::fmt::Debug for ConnectionLane {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectionLane")
            .field("source_context", &self.source_context)
            .field("connect_command_id", &self.connect_command_id)
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .finish()
    }
}

impl ConnectionLane {
    pub(crate) fn new(source_context: RecoveryContext, connect_command_id: CommandId) -> Self {
        Self {
            source_context,
            connect_command_id,
            state: Mutex::new(LaneState {
                phase: Phase::Connecting,
                operator_requested: false,
            }),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
        }
    }

    /// Whether this lane currently knows the link is up.
    ///
    /// A connect still in progress and an ended/closed lane both report `false`: this is the
    /// engine's own observation, never a fresh platform probe.
    pub fn is_connected(&self) -> bool {
        matches!(self.lock().phase, Phase::Linked(_))
    }

    fn lock(&self) -> MutexGuard<'_, LaneState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Offers one canonical event. Only this lane's own connect and connection are accepted.
    pub(crate) fn offer(&self, event: &BleEvent) {
        let BleEvent::ConnectionStateChanged {
            command_id,
            connection_id,
            result,
        } = event
        else {
            return;
        };
        let mut state = self.lock();
        let operator_requested = state.operator_requested;
        let next = transition(
            &state.phase,
            *command_id == Some(self.connect_command_id),
            *connection_id,
            result,
            operator_requested,
        );
        let Some(next) = next else { return };
        state.phase = next;
        // The state is only ever observable through `next_end`, so every change is worth a wake-up.
        drop(state);
        self.changed.notify_one();
    }

    /// Records that this capability submitted a disconnect for this link.
    ///
    /// Called after bounded submission returned, so the flag describes a request the platform
    /// received. A terminal that is already retained keeps its own attribution: the first end is
    /// the truth, and a later request cannot re-label it.
    pub(crate) fn expect_operator_disconnect(&self) {
        let mut state = self.lock();
        if matches!(state.phase, Phase::Ended(_) | Phase::Closed) {
            return;
        }
        state.operator_requested = true;
    }

    /// The owner retired: this capability cannot observe anything further.
    pub(crate) fn close(&self) {
        let mut state = self.lock();
        if matches!(state.phase, Phase::Ended(_) | Phase::Closed) {
            return;
        }
        state.phase = Phase::Ended(ConnectionEnd {
            by: ConnectionEndBy::Owner,
            error: None,
        });
        drop(state);
        self.changed.notify_one();
    }

    pub(crate) fn fail_unavailable(&self) {
        let mut state = self.lock();
        if matches!(state.phase, Phase::Ended(_) | Phase::Closed) {
            return;
        }
        state.phase = Phase::Closed;
        drop(state);
        self.changed.notify_one();
    }

    /// Cancel-safe pending pull. Dropping this future never ends or retires the link.
    ///
    /// At most one pull is pending: two independent consumers of one terminal would each report
    /// the same end, so the second caller is refused rather than silently racing the first.
    pub async fn next_end(&self) -> Result<ConnectionEnd, ConnectionLaneError> {
        let Some(_pull) = PullGuard::acquire(&self.pulling) else {
            return Err(ConnectionLaneError::Pending);
        };
        loop {
            let changed = self.changed.notified();
            {
                let state = self.lock();
                match &state.phase {
                    Phase::Ended(end) => return Ok(end.clone()),
                    Phase::Closed => return Err(ConnectionLaneError::Unavailable),
                    Phase::Connecting | Phase::Linked(_) => {}
                }
            }
            changed.await;
        }
    }
}

/// The next phase, or `None` when this event says nothing about this lane.
///
/// `is_own_connect` is true only for the connect command this lane was created for: a connected
/// state that another command or an unsolicited reconnect produced is not this lane's link.
fn transition(
    phase: &Phase,
    is_own_connect: bool,
    connection_id: ConnectionId,
    result: &ConnectionStateResult,
    operator_requested: bool,
) -> Option<Phase> {
    let by = if operator_requested {
        ConnectionEndBy::Operator
    } else {
        ConnectionEndBy::Unrequested
    };
    match phase {
        Phase::Ended(_) | Phase::Closed => None,
        Phase::Connecting => match result {
            // Bind on this lane's own connect completion, never on a later connected state that
            // some other command or an unsolicited reconnect produced.
            ConnectionStateResult::Ok {
                state: ConnectionState::Connected,
            } if is_own_connect => Some(Phase::Linked(connection_id)),
            _ => None,
        },
        Phase::Linked(linked) => {
            if *linked != connection_id {
                return None;
            }
            match result {
                ConnectionStateResult::Ok {
                    state: ConnectionState::Disconnected,
                } => Some(Phase::Ended(ConnectionEnd { by, error: None })),
                ConnectionStateResult::Err { error } => Some(Phase::Ended(ConnectionEnd {
                    by,
                    error: Some(error.clone()),
                })),
                // `Connecting`, `Disconnecting` and a repeated `Connected` are progress states.
                ConnectionStateResult::Ok { .. } => None,
            }
        }
    }
}

#[cfg(test)]
#[path = "tests/connection_delivery.rs"]
mod tests;
