//! Reservation data model shared by command admission and event reconciliation.
//!
//! These actor-confined values record dispatch acknowledgements and correlated
//! native evidence. They own neither runtime lifecycle, caller waiters, executors,
//! synchronization, nor foreign cleanup. Commands/events own transport; the actor
//! owns registration and caller delivery.
//!
//! Implementation modules stay private. Their public owner APIs are exposed only
//! through the runtime-scoped re-exports below; shared implementation details use
//! `pub(super)`, and record-local fields and helpers remain private.
//!
//! Read `dispatch` first: `CommandProgress` owns acknowledgement, bounded buffering,
//! and the authoritative first terminal; typed domain evidence stays in each record.
//! Follow it with
//! `initialization` and `scan`. `connections` owns the peripheral-keyed collection
//! and global pre-ack routing priority; `connection` owns one connection record
//! and its operation reconciliation. Then read the subordinate `gatt`, `io`, and
//! `subscription` records. Collection ownership is distinct from record-local
//! evidence and retirement; there is no second command-routing map. The collection
//! resolves identities; connection-local validators preserve each operation's error
//! precedence and share child-command handoff facts, not a universal busy policy.
//!
//! A connection always owns its GATT state; committed database and discovery are
//! independently optional so rediscovery can preserve the old database. Subscription
//! routes use Enabling/Active/Disabling states; rejection and retirement explicitly
//! choose Keep/Remove, and failed disable restores the active route.
//!
//! Commands win the caller handoff before installing a reservation, then spawn
//! native dispatch without an intervening await. Losing cancellation creates
//! no reservation to roll back. Accepted native work outlives caller waiters.
//!
//! Events record correlated evidence, settle affected waiters, then retire or
//! commit records. Retirement must depend on owned evidence, not an unrelated
//! raw event, and must retain incomplete start/dispatch evidence for late callbacks.
//! Failed rediscovery preserves the committed database. Discovery is admitted
//! only after all subscriptions are disabled; database preservation alone cannot
//! prove that native subscription routes survived a platform rediscovery.
//!

mod connection;
mod connections;
mod dispatch;
mod gatt;
mod initialization;
mod io;
mod scan;
mod subscription;

#[cfg(test)]
mod tests;

pub(super) use connection::connection_lost;
pub(super) use connections::ConnectionReservations;
pub(super) use dispatch::{DispatchEffect, DispatchResolution, ReconcileFault};
pub(super) use gatt::GattAccess;
pub(super) use initialization::InitializationState;
pub(super) use scan::ScanState;
