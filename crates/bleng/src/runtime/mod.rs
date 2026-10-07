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

use std::{
    collections::HashMap,
    panic::{AssertUnwindSafe, catch_unwind},
    sync::Arc,
};

use blehub::{
    BleEvent, CommandDispatchError, CommandId, ConnectCommand, ConnectionId, DisconnectCommand,
    DiscoverGattCommand, GattCharacteristicId, GattSessionId, InitializeCommand,
    NotificationAction, NotificationMode, NotificationModeRequest, NotificationState, PeripheralId,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, SubscriptionId,
    WriteCharacteristicCommand, WriteType,
};

use crate::waiter::{WaiterError, WaiterSuccess};
use kameo::prelude::*;

use tokio::sync::oneshot;

mod assembly;
mod commands;
mod events;
mod ingress;
mod lifecycle;
mod owner;
mod state;
mod subscriptions;

pub(super) use assembly::{AssemblyError, AssemblyPort};
pub(super) use commands::{
    BeginConnect, BeginDisconnect, BeginDiscoverGatt, BeginInitialize, BeginRead, BeginRssi,
    BeginScan, BeginStop, BeginWrite, RetireWaiter,
};
pub(super) use events::DispatchResolved;
pub(super) use ingress::IngressEvent;
pub(super) use lifecycle::{RuntimePhase, RuntimeStatus};
#[cfg(test)]
pub(super) use owner::WaiterTicket;
pub(super) use owner::{BeginContext, EventReadError, RuntimeOwner};
pub(super) use state::{
    BlengActor, ConnectionReservation, GattReservation, ReservationRollback, WaiterReceiver,
};

#[cfg(test)]
mod tests;
