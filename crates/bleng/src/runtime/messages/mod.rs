//! Actor mailbox protocol: command requests, asynchronous inputs, and waiter control.
//!
//! `Begin*` messages own bounded eager submissions. Handlers validate BLE state,
//! register the submission's original sender, claim dispatch, and install native
//! reservations. Admission errors settle that same result channel; there is no
//! admission reply channel or caller-side begin task. Resource metadata is written
//! once by admission, before the actor can reconcile backend evidence.
//! `IngressEvent` and `DispatchResolved` carry callbacks and native receipts;
//! cancellation after claim and reservation retirement remain actor-owned.
//!
//! Success without a payload is `()`, not an invented acknowledgement object.
//! In particular, backend dispatch `Ok(())` means accepted for dispatch, not BLE
//! completion. `DispatchResolved` carries the full result to the actor; actual
//! operation data and completion arrive through correlated callback evidence.

use blehub::{
    AdapterSnapshot, AssociateCompanionDeviceCommand, BleEvent, BluetoothUuid, BondState,
    CommandDispatchError, CommandId, ConnectCommand, ConnectionId, ConnectionPriority,
    CreateBondCommand, DeviceQueryKind, DisconnectCommand, DiscoverGattCommand,
    GattCharacteristicId, GattDatabase, GattDescriptorId, GattSessionId, InitializeCommand,
    MtuCommand, MtuInfo, NotificationAction, NotificationMode, NotificationModeRequest,
    PeripheralId, QueryDevicesCommand, ReadCharacteristicCommand, ReadDescriptorCommand,
    ReadRssiCommand, RefreshGattCacheCommand, RemoveBondCommand, ScanOptions, ScanSessionId,
    SetAdapterEnabledCommand, SetConnectionPriorityCommand, SetNotificationsCommand,
    StartScanCommand, StopScanCommand, SubscriptionId, WriteCharacteristicCommand,
    WriteDescriptorCommand, WriteType,
};
use kameo::prelude::{Context, Message};

use crate::waiter::{WaiterError, WaiterExpectation, WaiterSuccess};

use super::{actor::BlengActor, reservations::GattAccess, shared::OperationSubmission};

mod adapter;
mod companion;
mod connection;
mod io;
mod scan;
mod subscription;

pub(crate) use adapter::{BeginInitialize, BeginQueryDevices, BeginSetAdapterEnabled};
pub(crate) use companion::BeginAssociateCompanion;
pub(crate) use connection::{
    BeginConnect, BeginCreateBond, BeginDisconnect, BeginDiscoverGatt, BeginMtu,
    BeginRefreshGattCache, BeginRemoveBond, BeginRssi, BeginSetConnectionPriority,
};
pub(crate) use io::{BeginRead, BeginReadDescriptor, BeginWrite, BeginWriteDescriptor};
pub(crate) use scan::{BeginScan, BeginStop};
pub(crate) use subscription::{BeginDisableNotifications, BeginEnableNotifications};

// Command requests and typed result decoding.

/// Ties a submitted request to its actor-allocated resource and typed success.
///
/// Correlation is a bounded snapshot of request IDs, not native state or an
/// operation result. The owner extracts it before moving the message into
/// bounded enqueue, without retaining or cloning the request payload.
/// Decoding has no lifecycle dependency: the owner handles a rejected success.
pub(crate) trait OperationSpec: Send + 'static {
    /// Actor-allocated ID, or `()` for commands creating no resource.
    type Resource: Send + Sync + Copy + 'static;
    type Output: Send + 'static;
    type Correlation: Send + 'static;

    fn correlation(&self) -> Self::Correlation;

    /// Checks the success variant and identities, then moves out its payload.
    fn decode_success(
        correlation: Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output>;
}

// Backend ingress.

/// One backend callback, distinct from native dispatch and cancellation acknowledgements.
pub(super) struct IngressEvent(pub(super) BleEvent);

impl Message<IngressEvent> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        IngressEvent(event): IngressEvent,
        _: &mut Context<Self, Self::Reply>,
    ) {
        self.handle_ingress_event(event);
    }
}

// Native dispatch acknowledgement.

/// Native dispatch acknowledgement delivered back through the actor mailbox.
pub(super) struct DispatchResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), CommandDispatchError>,
}

impl Message<DispatchResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: DispatchResolved, ctx: &mut Context<Self, Self::Reply>) {
        self.handle_dispatch_result(message.id, message.result, ctx.actor_ref().clone());
    }
}

// Native cancellation acknowledgement.

/// Native cancellation acknowledgement returned through the actor mailbox.
/// The result covers backend acceptance of the control request, not BLE teardown.
pub(super) struct CancellationResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), WaiterError>,
}

impl Message<CancellationResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: CancellationResolved, _: &mut Context<Self, Self::Reply>) {
        self.handle_cancellation_result(message.id, message.result);
    }
}

// Caller-side explicit operation control.

/// Requests cancellation of an existing command without allocating a new ID.
/// The actor reply is a receipt channel for native control acceptance; native
/// stop and recovery remain asynchronous and belong to the original reservation.
pub(super) struct RequestCancellation {
    pub(super) command_id: CommandId,
}

impl Message<RequestCancellation> for BlengActor {
    type Reply = Result<super::reservations::CancellationReceiver, WaiterError>;

    async fn handle(
        &mut self,
        message: RequestCancellation,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        self.request_operation_cancellation(message.command_id, ctx.actor_ref().clone())
    }
}

#[cfg(test)]
#[path = "../tests/operation_results.rs"]
mod operation_result_tests;
