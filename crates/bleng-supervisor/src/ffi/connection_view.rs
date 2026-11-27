//! Product projection of the existing connect/disconnect operations, without another lifecycle.
//!
//! A connection capability is created by Rust and carries the exact owner context that produced
//! it, so native hosts never hold a bare `ConnectionId` and cannot address another owner's
//! connection. Submission, waiting, deadlines and cancellation stay in BLeng.

use std::sync::{Arc, Weak};

use blehub::ConnectionId;
use bleng::{ConnectOperation, DisconnectOperation};

use super::failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    ble_failure, bleng_failure, normalize_timeout,
};
use super::{ForeignOwnerContext, SupervisedPeripheral, Supervisor};
use crate::{
    ConnectionEnd, ConnectionEndBy, ConnectionLane, ConnectionLaneError, ConnectionSubmissionError,
    SupervisorOperationError,
};

/// Bounded connect submission. A present capability is not an established connection.
#[derive(Clone, uniffi::Record)]
pub struct ForeignConnectBeginResult {
    pub connect: Option<Arc<SupervisedConnect>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignConnectBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            connect: None,
            failure: Some(failure),
        }
    }
}

/// Original connect result. An absent failure means BLeng reported success; it is consumed once.
#[derive(Clone, uniffi::Record)]
pub struct ForeignConnectResult {
    pub connection: Option<Arc<SupervisedConnection>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignConnectResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            connection: None,
            failure: Some(failure),
        }
    }
}

/// Bounded disconnect submission; again a capability, not a completed teardown.
#[derive(Clone, uniffi::Record)]
pub struct ForeignDisconnectBeginResult {
    pub disconnect: Option<Arc<SupervisedDisconnect>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignDisconnectBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            disconnect: None,
            failure: Some(failure),
        }
    }
}

/// Who ended the link, as far as this capability could observe it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignConnectionEndBy {
    /// A disconnect this capability submitted is the command that produced the terminal.
    Operator,
    /// The link ended without this capability requesting it. `failure` carries the platform's own
    /// report when there was one; its absence is not evidence of a clean peer request.
    Unrequested,
    /// The owner retired, so no further end can be observed. Observation ended, which is not
    /// evidence that the link is down.
    Owner,
}

/// One connection's retained terminal.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignConnectionEnd {
    pub by: ForeignConnectionEndBy,
    pub failure: Option<ForeignOperationFailure>,
}

/// One end observation: the retained terminal, or why it could not be observed this time.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignConnectionEndObservation {
    pub ended: Option<ForeignConnectionEnd>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignConnectionEndObservation {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            ended: None,
            failure: Some(failure),
        }
    }
}

/// Single-consumer connect result with independent cancellation.
#[derive(uniffi::Object)]
pub struct SupervisedConnect {
    supervisor: Weak<Supervisor>,
    context: ForeignOwnerContext,
    operation: Arc<ConnectOperation>,
    /// Created with the connect submission; moves into the connection capability.
    lane: Arc<ConnectionLane>,
    /// Advertisement name from the peripheral capability that submitted this connect.
    local_name: Option<String>,
}

/// Owner-bound connection capability. It holds no native handle, only the identity BLeng returned
/// for the owner that produced it.
#[derive(uniffi::Object)]
pub struct SupervisedConnection {
    pub(in crate::ffi) supervisor: Weak<Supervisor>,
    pub(in crate::ffi) context: ForeignOwnerContext,
    pub(in crate::ffi) id: ConnectionId,
    /// This connection's own end, and the runtime that bounds a caller-supplied deadline.
    lane: Arc<ConnectionLane>,
    operation: Arc<ConnectOperation>,
    /// Advertisement name observed on the peripheral that submitted this connect.
    local_name: Option<String>,
}

/// Single-consumer disconnect result with independent cancellation.
#[derive(uniffi::Object)]
pub struct SupervisedDisconnect {
    operation: Arc<DisconnectOperation>,
}

#[uniffi::export]
impl SupervisedPeripheral {
    /// Submits through this peripheral's own owner context while the supervisor lock is held, so
    /// owner retirement cannot slip between validation and submission.
    ///
    /// `auto_connect` asks the platform for an auto-connect link; a platform that cannot express
    /// it rejects the command with `NotSupported`.
    pub fn begin_connect(&self, auto_connect: bool) -> ForeignConnectBeginResult {
        let Some(supervisor) = self.supervisor.upgrade() else {
            return ForeignConnectBeginResult::failed(supervisor_unavailable());
        };
        match supervisor.submit_operation::<_, ConnectionSubmissionError>(
            self.context.clone(),
            |supervisor, context| {
                supervisor.begin_connect_view_with_options(context, self.id, auto_connect)
            },
        ) {
            Ok(handoff) => ForeignConnectBeginResult {
                connect: Some(Arc::new(SupervisedConnect {
                    supervisor: self.supervisor.clone(),
                    context: self.context.clone(),
                    operation: handoff.operation,
                    lane: handoff.lane,
                    local_name: self.local_name.clone(),
                })),
                failure: None,
            },
            Err(error) => ForeignConnectBeginResult::failed(error.into()),
        }
    }
}

#[uniffi::export]
impl SupervisedConnect {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignConnectResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignConnectResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(id) => ForeignConnectResult {
                connection: Some(Arc::new(SupervisedConnection {
                    supervisor: self.supervisor.clone(),
                    context: self.context.clone(),
                    id,
                    lane: Arc::clone(&self.lane),
                    operation: Arc::clone(&self.operation),
                    local_name: self.local_name.clone(),
                })),
                failure: None,
            },
            Err(error) => ForeignConnectResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

#[uniffi::export]
impl SupervisedConnection {
    /// Identity of this exact owner-scoped capability, never a device address.
    pub fn same_connection(&self, other: Arc<Self>) -> bool {
        self.context == other.context && self.id == other.id
    }

    /// Whether this connection's own lane currently sees the link as up.
    ///
    /// A connect still awaiting the platform's confirmation reports `false`, and so does an ended
    /// lane. It is the engine's observation, not a fresh platform probe of the peer.
    pub fn is_connected(&self) -> bool {
        self.lane.is_connected()
    }

    /// The peer's self-reported advertisement name, when the peripheral capability carried one.
    ///
    /// This is the name observed in the advertisement that produced the peripheral, not a fresh
    /// platform read and not a claim about a live link. It is `None` when the peer advertised no
    /// name and for a capability adopted from an inventory key.
    pub fn local_name(&self) -> Option<String> {
        self.local_name.clone()
    }

    pub fn begin_disconnect(&self) -> ForeignDisconnectBeginResult {
        let Some(supervisor) = self.supervisor.upgrade() else {
            return ForeignDisconnectBeginResult::failed(supervisor_unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            self.context.clone(),
            |supervisor, context| supervisor.begin_disconnect(context, self.id),
        ) {
            Ok(operation) => {
                // Which command produced a terminal is attribution, not a guess: only a request
                // that reached the platform may label this link's end as operator-requested.
                self.lane.expect_operator_disconnect();
                ForeignDisconnectBeginResult {
                    disconnect: Some(Arc::new(SupervisedDisconnect { operation })),
                    failure: None,
                }
            }
            Err(error) => ForeignDisconnectBeginResult::failed(error.into()),
        }
    }

    /// The one terminal this link has, retained after it happens.
    ///
    /// A deadline is the caller's own bound, never a statement about the link: it reports an
    /// observation failure and the end stays retained for the next call. Exactly-once belongs to
    /// the connect result, which is a different fact.
    pub async fn await_end(&self, timeout_ms: f64) -> ForeignConnectionEndObservation {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignConnectionEndObservation::failed(*failure),
        };
        let lane = Arc::clone(&self.lane);
        let observation = self
            .operation
            .observe_with_timeout(timeout_ms, async move { lane.next_end().await });
        match observation.await {
            Ok(Ok(end)) => ForeignConnectionEndObservation {
                ended: Some(foreign_end(end)),
                failure: None,
            },
            Ok(Err(error)) => ForeignConnectionEndObservation::failed(lane_failure(error)),
            Err(error) => ForeignConnectionEndObservation::failed(bleng_failure(
                ForeignOperationFailureDomain::Observation,
                error,
            )),
        }
    }
}

#[uniffi::export]
impl SupervisedDisconnect {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignOperationCommandResult::from_failure(Some(*failure));
            }
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    /// Requests cancellation without consuming the result; no remote rollback.
    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }
}

impl From<ConnectionSubmissionError> for ForeignOperationFailure {
    fn from(error: ConnectionSubmissionError) -> Self {
        match error {
            ConnectionSubmissionError::Owner { error } => error.into(),
            ConnectionSubmissionError::OwnerRetired => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Lifecycle,
                "delivery.owner_retired",
                "connection source owner retired",
            ),
            ConnectionSubmissionError::Unavailable => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Delivery,
                "delivery.unavailable",
                "connection lane synchronization failed",
            ),
        }
    }
}

fn foreign_end(end: ConnectionEnd) -> ForeignConnectionEnd {
    ForeignConnectionEnd {
        by: match end.by {
            ConnectionEndBy::Operator => ForeignConnectionEndBy::Operator,
            ConnectionEndBy::Unrequested => ForeignConnectionEndBy::Unrequested,
            ConnectionEndBy::Owner => ForeignConnectionEndBy::Owner,
        },
        failure: end
            .error
            .map(|error| ble_failure(ForeignOperationFailureDomain::Operation, "ble", error)),
    }
}

fn lane_failure(error: ConnectionLaneError) -> ForeignOperationFailure {
    match error {
        ConnectionLaneError::Pending => ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Observation,
            "observation.pending",
            "another connection end pull is already pending",
        ),
        ConnectionLaneError::Unavailable => ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Delivery,
            "delivery.unavailable",
            "connection end bookkeeping is unavailable",
        ),
    }
}

fn supervisor_unavailable() -> ForeignOperationFailure {
    ForeignOperationFailure::new(
        ForeignOperationFailureDomain::Lifecycle,
        "supervisor.unavailable",
        "supervisor is unavailable",
    )
}
