//! Private per-Hub runtime used by the public `facade` module; the runtime does
//! not depend on facade types. The mailbox protocol in `messages.rs` adapts
//! requests and asynchronous inputs to actor methods; state and reconciliation
//! stay private. `shared.rs` holds cross-module ID allocation and predispatch
//! cancellation/claim mechanics, not native BLE reservations.
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
mod command;
mod lifecycle;
mod messages;
mod operation;
mod owner;
mod reservations;
mod shared;

use actor::BlengActor;
pub(super) use backend_port::{AssemblyError, BackendPort};

pub(super) use command::{CommandDirectory, CommandRoute};
pub(super) use lifecycle::{RuntimeLifecycle, RuntimePhase};
pub(crate) use messages::{
    BeginConnect, BeginDisableNotifications, BeginDisconnect, BeginDiscoverGatt,
    BeginEnableNotifications, BeginInitialize, BeginRead, BeginRssi, BeginScan, BeginStop,
    BeginWrite, OperationSpec,
};
pub(crate) use operation::{OperationControl, OperationHandoff, ResultSlot};
pub(super) use owner::{EventReadError, RuntimeOwner, TaskFailure};
