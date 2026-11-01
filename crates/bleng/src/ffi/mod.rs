//! Stable UniFFI-facing exports for the BLeng facade.
//!
//! Implementations remain in `facade` and `error`; this module is only the
//! ABI-facing aggregation point and does not define a second BLeng contract.

pub use crate::error::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};
pub use crate::facade::{
    Bleng, ConnectOperation, DisableNotificationsOperation, DisconnectOperation,
    DiscoverGattOperation, EnableNotificationsOperation, InitializeOperation, ReadOperation,
    ReadRssiOperation, StartScanOperation, StopScanOperation, WriteOperation,
};
