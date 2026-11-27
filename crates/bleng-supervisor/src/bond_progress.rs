//! One bond command's own progress, observed on the owner's single event reader.
//!
//! The terminal bond state stays exactly where it was: `BondOperation::wait` consumes it once and
//! is the only success receipt. This lane is an **observation** of the platform-reported states that
//! lead there (`Bonding` then `Bonded`, or `Bonding` then a failure), so UI can show progress without
//! turning an interim state into a result.
//!
//! Binding is by command id, so two bonds on one owner cannot observe each other's states. The lane
//! is registered *before* the bond is submitted, so the first state cannot slip in between. States
//! are bounded; overflow ends the lane instead of dropping progress silently. A retained end can be
//! read repeatedly, while exactly-once still belongs to `wait`.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, MutexGuard, atomic::AtomicBool};

use blehub::{
    BleEvent, BleEventError, BondState, CommandId, ConnectionId, ConnectionState,
    ConnectionStateResult,
};
use bleng::BondOperation;
use tokio::sync::Notify;

use crate::SupervisorOperationError;
use crate::pull_guard::PullGuard;
use crate::recovery::RecoveryContext;

/// A bond has at most a handful of platform states; a larger queue means the consumer stopped
/// pulling, and the lane ends rather than growing without bound.
pub const MAX_BOND_STATES: usize = 8;

#[derive(Debug, thiserror::Error)]
pub enum BondSubmissionError {
    #[error("bond submission rejected: {error}")]
    Owner {
        #[from]
        error: SupervisorOperationError,
    },
    #[error("owner retired before bond submission")]
    OwnerRetired,
    #[error("bond progress lane bookkeeping unavailable")]
    Unavailable,
}

/// The original bond operation plus the progress lane registered before it was submitted.
pub struct BondHandoff {
    pub operation: Arc<BondOperation>,
    pub lane: Arc<BondProgressDelivery>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BondProgressError {
    /// Another progress pull is already pending on this lane.
    Pending,
    /// Lane bookkeeping was poisoned or the projection dropped this lane.
    Unavailable,
}

/// Why a progress lane ended. The end is retained and may be read repeatedly.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BondProgressEnd {
    /// The direction's terminal state was delivered (for example `bonded` for a create).
    Terminal,
    /// The bounded progress queue overflowed, so continuing would hide a gap.
    Overflow,
    /// The owner retired; observation cannot continue.
    OwnerRetired,
    /// The link ended before the bond reached a terminal state.
    ConnectionLost,
}

/// One step read from a bond progress lane.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BondProgressStep {
    /// A platform-reported state; `ended` is true only for the terminal state.
    State { state: BondState, ended: bool },
    /// The lane ended without another state.
    End(BondProgressEnd),
    /// The bond's own command completed with a platform error.
    Failed(BleEventError),
}

#[derive(Debug, Default)]
struct ProgressState {
    states: VecDeque<(BondState, bool)>,
    /// The retained end/error; the first one wins.
    retained: Option<Retained>,
}

#[derive(Debug, Clone)]
enum Retained {
    End(BondProgressEnd),
    Failed(BleEventError),
}

/// One owner/command-bound progress endpoint.
///
/// Holds no native handle and no strong owner reference, so a retained endpoint cannot keep a
/// retired owner alive.
pub struct BondProgressDelivery {
    source_context: RecoveryContext,
    command_id: CommandId,
    connection: ConnectionId,
    success_state: BondState,
    state: Mutex<ProgressState>,
    changed: Notify,
    pulling: AtomicBool,
}

impl std::fmt::Debug for BondProgressDelivery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BondProgressDelivery")
            .field("source_context", &self.source_context)
            .field("command_id", &self.command_id)
            .field("connection", &self.connection)
            .field("state", &self.state)
            .field("pulling", &self.pulling)
            .finish()
    }
}

impl BondProgressDelivery {
    pub(crate) fn new(
        source_context: RecoveryContext,
        command_id: CommandId,
        connection: ConnectionId,
        success_state: BondState,
    ) -> Self {
        Self {
            source_context,
            command_id,
            connection,
            success_state,
            state: Mutex::new(ProgressState::default()),
            changed: Notify::new(),
            pulling: AtomicBool::new(false),
        }
    }

    fn lock(&self) -> MutexGuard<'_, ProgressState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Offers one canonical event. Only this lane's own command and connection are accepted.
    pub(crate) fn offer(&self, event: &BleEvent) {
        let mut state = self.lock();
        if state.retained.is_some() {
            return;
        }
        match event {
            BleEvent::BondStateChanged {
                command_id,
                connection_id,
                state: bond,
            } if command_id.is_some_and(|id| id == self.command_id)
                && *connection_id == self.connection =>
            {
                if state.states.len() >= MAX_BOND_STATES {
                    state.states.clear();
                    state.retained = Some(Retained::End(BondProgressEnd::Overflow));
                } else {
                    let ended = *bond == self.success_state;
                    state.states.push_back((*bond, ended));
                    if ended {
                        state.retained = Some(Retained::End(BondProgressEnd::Terminal));
                    }
                }
            }
            BleEvent::CommandCompleted { command_id, error } if *command_id == self.command_id => {
                match error {
                    Some(error) => {
                        state.states.clear();
                        state.retained = Some(Retained::Failed(error.clone()));
                    }
                    // A successful completion without a reported terminal state still ends the
                    // lane; the original `wait` owns the typed result.
                    None => {
                        state.retained = Some(Retained::End(BondProgressEnd::Terminal));
                    }
                }
            }
            BleEvent::ConnectionStateChanged {
                connection_id,
                result,
                ..
            } if *connection_id == self.connection
                && matches!(
                    result,
                    ConnectionStateResult::Err { .. }
                        | ConnectionStateResult::Ok {
                            state: ConnectionState::Disconnected
                        }
                ) =>
            {
                state.retained = Some(Retained::End(BondProgressEnd::ConnectionLost));
            }
            _ => return,
        }
        drop(state);
        self.changed.notify_one();
    }

    /// The owner retired: this lane cannot outlive it.
    pub(crate) fn close(&self) {
        let mut state = self.lock();
        if state.retained.is_none() {
            state.retained = Some(Retained::End(BondProgressEnd::OwnerRetired));
        }
        drop(state);
        self.changed.notify_one();
    }

    pub(crate) fn fail_unavailable(&self) {
        let mut state = self.lock();
        // Bookkeeping failure must not overwrite an outcome already retained: the first end wins.
        if state.retained.is_none() {
            state.retained = Some(Retained::End(BondProgressEnd::OwnerRetired));
        }
        drop(state);
        self.changed.notify_one();
    }

    /// Cancel-safe pending pull. Dropping this future never cancels the bond.
    pub async fn next_state(&self) -> Result<BondProgressStep, BondProgressError> {
        let Some(_pull) = PullGuard::acquire(&self.pulling) else {
            return Err(BondProgressError::Pending);
        };
        loop {
            let changed = self.changed.notified();
            {
                let mut state = self.lock();
                if let Some((bond, ended)) = state.states.pop_front() {
                    return Ok(BondProgressStep::State { state: bond, ended });
                }
                if let Some(retained) = &state.retained {
                    return Ok(match retained {
                        Retained::End(end) => BondProgressStep::End(*end),
                        Retained::Failed(error) => BondProgressStep::Failed(error.clone()),
                    });
                }
            }
            changed.await;
        }
    }
}

#[cfg(test)]
#[path = "tests/bond_progress.rs"]
mod tests;
