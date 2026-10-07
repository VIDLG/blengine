//! Per-Hub actor state and facade. The mailbox protocol in messages.rs adapts
//! requests and asynchronous inputs to actor methods; state and reconciliation stay private.
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
mod lifecycle;
mod messages;
mod owner;
mod reservations;

use actor::BlengActor;
pub(super) use backend_port::{AssemblyError, BackendPort};
pub(super) use lifecycle::{RuntimeLifecycle, RuntimePhase};
pub(super) use owner::{EventReadError, RuntimeOwner};
