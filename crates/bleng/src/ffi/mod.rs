//! Stable UniFFI-facing exports for the BLeng facade.
//!
//! Implementations remain in `facade` and `error`; this module is only the
//! ABI-facing aggregation point and does not define a second BLeng contract.

pub use crate::error::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
};
pub use crate::facade::{
    AssociateCompanionDeviceOperation, Bleng, BondOperation, ConnectOperation,
    DisableNotificationsOperation, DisconnectOperation, DiscoverGattOperation,
    EnableNotificationsOperation, InitializeOperation, MtuOperation, NotificationSubscription,
    ReadDescriptorOperation, ReadOperation, ReadRssiOperation, SetConnectionPriorityOperation,
    StartScanOperation, StopScanOperation, WriteDescriptorOperation, WriteOperation,
};
