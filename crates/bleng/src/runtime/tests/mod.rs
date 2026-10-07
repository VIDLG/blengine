//! Owner and end-to-end runtime tests, compiled as children of owner.rs.
//! Actor-private assertions and fixture handlers live in the actor's test children.

mod assembly;
mod characteristic_io;
mod connection;
mod gatt;
mod id_allocation;
mod initialization;
mod lifecycle;
mod multi_device;
mod notifications;
mod owner;
mod reconciliation;
mod rssi;
mod scan;
mod support;
mod transport;
