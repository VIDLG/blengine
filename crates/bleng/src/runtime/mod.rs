//! Per-Hub actor state and facade. Command dispatch and event reconciliation
//! live in sibling submodules so they share this private ownership model.
//!
//! The runtime state is intentionally layered so multi-device operations remain
//! independent while each device's native operations stay serialized:
//!
//! ```text
//! BLeng instance
//! ├── backend slot
//! ├── scan session
//! └── connection map
//!     ├── peripheral connection
//!     ├── GATT session
//!     └── operation FIFO/transition
//! ```
//!
//! Different peripherals may progress concurrently. Within one connection,
//! GATT discovery, I/O, disconnect, and subscription transitions compete for
//! explicit slots and are correlated by connection/session generation state.

mod actor;
mod backend_port;
mod commands;

mod lifecycle;
mod owner;
mod reservations;
mod subscriptions;

use actor::{BlengActor, WaiterReceiver};
pub(super) use backend_port::{AssemblyError, BackendPort, IngressEvent};
pub(super) use commands::{
    BeginConnect, BeginDisconnect, BeginDiscoverGatt, BeginInitialize, BeginRead, BeginRssi,
    BeginScan, BeginStop, BeginWrite, RetireWaiter,
};

pub(super) use lifecycle::{RuntimeLifecycle, RuntimePhase};
use owner::BeginHandoff;
#[cfg(test)]
pub(super) use owner::WaiterTicket;
pub(super) use owner::{EventReadError, RuntimeOwner};
