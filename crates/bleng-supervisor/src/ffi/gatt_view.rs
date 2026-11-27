//! Product projection of GATT discovery and the characteristic operations that address it.
//!
//! Discovery returns a capability tree, not identifiers. A service, characteristic or descriptor
//! in JS is an object created by Rust that carries the exact owner, connection and GATT session
//! that discovered it; no `ConnectionId`, `GattSessionId`, `GattCharacteristicId` or
//! `GattDescriptorId` ever crosses the boundary. That makes a stale capability fail instead of
//! silently addressing a replacement session, and it keeps a rediscovery from being mistaken for
//! the previous topology.
//!
//! Notifications are deliberately absent: they need a bounded observation stream of their own, not
//! another request/response receipt. Only the request/response surface lives here.

use std::sync::{Arc, Weak};

use blehub::{
    BluetoothUuid, BondState, CharacteristicProperties, ConnectionId, ConnectionPriority,
    GattCharacteristicId, GattDatabase, GattDescriptorId, GattSessionId, MtuInfo, WriteType,
};
use bleng::{
    BondOperation, DiscoverGattOperation, MtuOperation, ReadDescriptorOperation, ReadOperation,
    ReadRssiOperation, SetConnectionPriorityOperation, WriteDescriptorOperation, WriteOperation,
};

use super::failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    ble_failure, bleng_failure, normalize_timeout,
};
use super::{
    ForeignOwnerContext, ForeignRefreshBeginResult, SupervisedConnection,
    SupervisedRefreshGattCache, Supervisor,
};
use crate::{
    BondProgressDelivery, BondProgressEnd, BondProgressError, BondProgressStep,
    BondSubmissionError, SupervisorOperationError,
};

/// Owner linkage plus the connection a capability belongs to.
#[derive(Clone)]
pub(in crate::ffi) struct ConnectionBinding {
    pub(in crate::ffi) supervisor: Weak<Supervisor>,
    pub(in crate::ffi) context: ForeignOwnerContext,
    pub(in crate::ffi) connection: ConnectionId,
}

/// The fixed addressing every capability derived from one discovery shares.
#[derive(Clone)]
pub(in crate::ffi) struct GattBinding {
    pub(in crate::ffi) connection: ConnectionBinding,
    pub(in crate::ffi) session: GattSessionId,
}

/// Bounded discovery submission. A present capability is not a discovered database.
#[derive(Clone, uniffi::Record)]
pub struct ForeignGattDiscoveryBeginResult {
    pub discovery: Option<Arc<SupervisedGattDiscovery>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignGattDiscoveryBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            discovery: None,
            failure: Some(failure),
        }
    }
}

/// Original discovery result; the topology is single-consumer.
#[derive(Clone, uniffi::Record)]
pub struct ForeignGattDiscoveryResult {
    pub session: Option<Arc<SupervisedGattSession>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignGattDiscoveryResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            session: None,
            failure: Some(failure),
        }
    }
}

/// A read result. `value` distinguishes "no value" from an empty characteristic value.
#[derive(Clone, uniffi::Record)]
pub struct ForeignReadValueResult {
    pub value: Option<Vec<u8>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignReadValueResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            value: None,
            failure: Some(failure),
        }
    }
}

/// A connected-RSSI result. Zero and negative values are real, so absence is `None`.
#[derive(Clone, uniffi::Record)]
pub struct ForeignRssiValueResult {
    pub rssi: Option<i16>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignRssiValueResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            rssi: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignReadBeginResult {
    pub read: Option<Arc<SupervisedRead>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignReadBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            read: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignWriteBeginResult {
    pub write: Option<Arc<SupervisedWrite>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignWriteBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            write: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignRssiBeginResult {
    pub rssi: Option<Arc<SupervisedRssi>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignRssiBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            rssi: None,
            failure: Some(failure),
        }
    }
}

/// An MTU query/request result. Every field is optional because platforms report different facts.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignMtuValueResult {
    pub info: Option<MtuInfo>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignMtuValueResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            info: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignMtuBeginResult {
    pub mtu: Option<Arc<SupervisedMtu>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignMtuBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            mtu: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignPriorityBeginResult {
    pub priority: Option<Arc<SupervisedConnectionPriority>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignPriorityBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            priority: None,
            failure: Some(failure),
        }
    }
}

/// A bond receipt. The state is the platform's report, not proof the link is encrypted.
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignBondValueResult {
    pub state: Option<BondState>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignBondValueResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            state: None,
            failure: Some(failure),
        }
    }
}

/// One platform-reported bond progress step.
///
/// `ended` is true only for the direction's terminal state or for the end of the lane; the
/// terminal receipt itself still comes from `wait_result`. A non-terminal record carries one of the
/// interim states the platform reported (typically `Bonding`).
#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignBondProgress {
    pub state: Option<BondState>,
    pub ended: bool,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignBondProgress {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            state: None,
            ended: true,
            failure: Some(failure),
        }
    }
}

impl From<BondSubmissionError> for ForeignOperationFailure {
    fn from(error: BondSubmissionError) -> Self {
        match error {
            BondSubmissionError::Owner { error } => error.into(),
            BondSubmissionError::OwnerRetired => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Lifecycle,
                "delivery.owner_retired",
                "bond source owner retired",
            ),
            BondSubmissionError::Unavailable => ForeignOperationFailure::new(
                ForeignOperationFailureDomain::Delivery,
                "delivery.unavailable",
                "bond progress lane synchronization failed",
            ),
        }
    }
}

fn bond_progress_end(end: BondProgressEnd) -> Option<ForeignOperationFailure> {
    match end {
        BondProgressEnd::Terminal => None,
        BondProgressEnd::Overflow => Some(ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Delivery,
            "bond_progress.overflow",
            "bond progress buffer overflowed and ended the lane",
        )),
        BondProgressEnd::OwnerRetired => Some(ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Lifecycle,
            "bond_progress.owner_retired",
            "the source owner retired before the bond ended",
        )),
        BondProgressEnd::ConnectionLost => Some(ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Operation,
            "bond_progress.connection_lost",
            "the link ended before the bond reached a terminal state",
        )),
    }
}

fn bond_progress_failure(error: BondProgressError) -> ForeignOperationFailure {
    match error {
        BondProgressError::Pending => ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Observation,
            "observation.pending",
            "another bond progress pull is already pending",
        ),
        BondProgressError::Unavailable => ForeignOperationFailure::new(
            ForeignOperationFailureDomain::Delivery,
            "delivery.unavailable",
            "bond progress bookkeeping is unavailable",
        ),
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignBondBeginResult {
    pub bond: Option<Arc<SupervisedBond>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignBondBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            bond: None,
            failure: Some(failure),
        }
    }
}

/// Single-consumer discovery result with independent cancellation.
#[derive(uniffi::Object)]
pub struct SupervisedGattDiscovery {
    binding: ConnectionBinding,
    operation: Arc<DiscoverGattOperation>,
}

/// One discovered topology. Every attribute capability below belongs to exactly one of these.
#[derive(uniffi::Object)]
pub struct SupervisedGattSession {
    binding: GattBinding,
    services: Vec<Arc<SupervisedGattService>>,
}

/// Data-only service projection. Nothing in the contract addresses a service directly, so it
/// carries no binding and no handle.
#[derive(uniffi::Object)]
pub struct SupervisedGattService {
    uuid: BluetoothUuid,
    primary: Option<bool>,
    characteristics: Vec<Arc<SupervisedGattCharacteristic>>,
}

/// An addressable characteristic. Reads and writes are admitted through its own binding.
#[derive(uniffi::Object)]
pub struct SupervisedGattCharacteristic {
    binding: GattBinding,
    uuid: BluetoothUuid,
    id: GattCharacteristicId,
    properties: CharacteristicProperties,
    /// Built once with the rest of the topology, so every access returns the same capabilities.
    descriptors: Vec<Arc<SupervisedGattDescriptor>>,
}

/// An addressable descriptor. Reads and writes are admitted through the characteristic and
/// session that discovered it, so a stale topology cannot address a replacement attribute.
#[derive(uniffi::Object)]
pub struct SupervisedGattDescriptor {
    binding: GattBinding,
    uuid: BluetoothUuid,
    characteristic_id: GattCharacteristicId,
    id: GattDescriptorId,
}

#[derive(uniffi::Object)]
pub struct SupervisedDescriptorRead {
    operation: Arc<ReadDescriptorOperation>,
}

#[derive(uniffi::Object)]
pub struct SupervisedDescriptorWrite {
    operation: Arc<WriteDescriptorOperation>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignDescriptorReadBeginResult {
    pub read: Option<Arc<SupervisedDescriptorRead>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignDescriptorReadBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            read: None,
            failure: Some(failure),
        }
    }
}

/// A descriptor value; absence is distinct from an empty descriptor.
#[derive(Clone, uniffi::Record)]
pub struct ForeignDescriptorReadValueResult {
    pub value: Option<Vec<u8>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignDescriptorReadValueResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            value: None,
            failure: Some(failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignDescriptorWriteBeginResult {
    pub write: Option<Arc<SupervisedDescriptorWrite>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignDescriptorWriteBeginResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            write: None,
            failure: Some(failure),
        }
    }
}

#[derive(uniffi::Object)]
pub struct SupervisedRead {
    operation: Arc<ReadOperation>,
}

#[derive(uniffi::Object)]
pub struct SupervisedWrite {
    operation: Arc<WriteOperation>,
}

#[derive(uniffi::Object)]
pub struct SupervisedRssi {
    operation: Arc<ReadRssiOperation>,
}

#[derive(uniffi::Object)]
pub struct SupervisedMtu {
    operation: Arc<MtuOperation>,
}

/// One bounded connection-priority receipt; the platform reports no effective link mode.
#[derive(uniffi::Object)]
pub struct SupervisedConnectionPriority {
    operation: Arc<SetConnectionPriorityOperation>,
}

/// One bounded bond receipt; success means the platform reported the requested terminal state.
#[derive(uniffi::Object)]
pub struct SupervisedBond {
    operation: Arc<BondOperation>,
    progress: Arc<BondProgressDelivery>,
}

impl SupervisedGattCharacteristic {
    pub(in crate::ffi) fn binding(&self) -> &GattBinding {
        &self.binding
    }

    pub(in crate::ffi) fn id(&self) -> GattCharacteristicId {
        self.id
    }
}

impl SupervisedGattDiscovery {
    fn new(binding: ConnectionBinding, operation: Arc<DiscoverGattOperation>) -> Arc<Self> {
        Arc::new(Self { binding, operation })
    }
}

impl SupervisedGattSession {
    /// Flattens one validated snapshot into capabilities. No filtering, sorting or deduplication
    /// happens here: Rust already validated the topology it returned.
    fn from_database(binding: ConnectionBinding, database: GattDatabase) -> Arc<Self> {
        // Rust owns the session identity; the capability adopts exactly what it returned.
        let gatt = GattBinding {
            connection: binding,
            session: database.gatt_session_id,
        };
        let services = database
            .services
            .into_iter()
            .map(|service| {
                let characteristics = service
                    .characteristics
                    .into_iter()
                    .map(|characteristic| {
                        let id = characteristic.id;
                        let descriptors = characteristic
                            .descriptors
                            .into_iter()
                            .map(|descriptor| {
                                Arc::new(SupervisedGattDescriptor {
                                    binding: gatt.clone(),
                                    uuid: descriptor.uuid,
                                    characteristic_id: id,
                                    id: descriptor.id,
                                })
                            })
                            .collect();
                        Arc::new(SupervisedGattCharacteristic {
                            binding: gatt.clone(),
                            uuid: characteristic.uuid,
                            id,
                            properties: characteristic.properties,
                            descriptors,
                        })
                    })
                    .collect();
                Arc::new(SupervisedGattService {
                    uuid: service.uuid,
                    primary: service.primary,
                    characteristics,
                })
            })
            .collect();
        Arc::new(Self {
            binding: gatt,
            services,
        })
    }
}

impl ConnectionBinding {
    pub(in crate::ffi) fn supervisor(&self) -> Option<Arc<Supervisor>> {
        self.supervisor.upgrade()
    }
}

fn unavailable() -> ForeignOperationFailure {
    ForeignOperationFailure::new(
        ForeignOperationFailureDomain::Lifecycle,
        "supervisor.unavailable",
        "supervisor is unavailable",
    )
}

#[uniffi::export]
impl SupervisedConnection {
    /// Starts one discovery for this exact connection. A later discovery yields a new session;
    /// capabilities from the previous session stay old instead of being re-pointed.
    pub fn begin_discover_gatt(&self) -> ForeignGattDiscoveryBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignGattDiscoveryBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_discover_gatt(context, binding.connection),
        ) {
            Ok(operation) => ForeignGattDiscoveryBeginResult {
                discovery: Some(SupervisedGattDiscovery::new(binding, operation)),
                failure: None,
            },
            Err(error) => ForeignGattDiscoveryBeginResult::failed(error.into()),
        }
    }

    /// Connected RSSI is a bounded request/response, not a subscription.
    pub fn begin_read_rssi(&self) -> ForeignRssiBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignRssiBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_read_rssi(context, binding.connection),
        ) {
            Ok(operation) => ForeignRssiBeginResult {
                rssi: Some(Arc::new(SupervisedRssi { operation })),
                failure: None,
            },
            Err(error) => ForeignRssiBeginResult::failed(error.into()),
        }
    }

    /// Requests an MTU where the platform supports it. A submission is not a negotiated value.
    pub fn begin_request_mtu(&self, mtu: u16) -> ForeignMtuBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignMtuBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_request_mtu(context, binding.connection, mtu),
        ) {
            Ok(operation) => ForeignMtuBeginResult {
                mtu: Some(Arc::new(SupervisedMtu { operation })),
                failure: None,
            },
            Err(error) => ForeignMtuBeginResult::failed(error.into()),
        }
    }

    /// Queries current link limits without requesting native negotiation.
    pub fn begin_query_mtu(&self) -> ForeignMtuBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignMtuBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_query_mtu(context, binding.connection),
        ) {
            Ok(operation) => ForeignMtuBeginResult {
                mtu: Some(Arc::new(SupervisedMtu { operation })),
                failure: None,
            },
            Err(error) => ForeignMtuBeginResult::failed(error.into()),
        }
    }

    /// Requests a link scheduling preference where the platform supports it.
    pub fn begin_set_connection_priority(
        &self,
        priority: ConnectionPriority,
    ) -> ForeignPriorityBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignPriorityBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| {
                supervisor.begin_set_connection_priority(context, binding.connection, priority)
            },
        ) {
            Ok(operation) => ForeignPriorityBeginResult {
                priority: Some(Arc::new(SupervisedConnectionPriority { operation })),
                failure: None,
            },
            Err(error) => ForeignPriorityBeginResult::failed(error.into()),
        }
    }

    /// Requests bonding where the platform supports it. A submission is not a bonded peer.
    /// `pin` is only the platform's numeric PIN pairing input; `None` leaves the system UI in charge.
    pub fn begin_create_bond(&self, pin: Option<String>) -> ForeignBondBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignBondBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, BondSubmissionError>(
            binding.context.clone(),
            |supervisor, context| {
                supervisor.begin_create_bond_view(context, binding.connection, pin)
            },
        ) {
            Ok(handoff) => ForeignBondBeginResult {
                bond: Some(Arc::new(SupervisedBond {
                    operation: handoff.operation,
                    progress: handoff.lane,
                })),
                failure: None,
            },
            Err(error) => ForeignBondBeginResult::failed(error.into()),
        }
    }

    /// Removes a bond where the platform supports it. A submission is not an unpaired peer.
    pub fn begin_remove_bond(&self) -> ForeignBondBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignBondBeginResult::failed(unavailable());
        };
        match supervisor.submit_operation::<_, BondSubmissionError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_remove_bond_view(context, binding.connection),
        ) {
            Ok(handoff) => ForeignBondBeginResult {
                bond: Some(Arc::new(SupervisedBond {
                    operation: handoff.operation,
                    progress: handoff.lane,
                })),
                failure: None,
            },
            Err(error) => ForeignBondBeginResult::failed(error.into()),
        }
    }
    /// Requests invalidation of the platform's cached GATT topology. A submission is not a
    /// refreshed cache; on success the engine drops this connection's discovered topology.
    pub fn begin_refresh_gatt_cache(&self) -> ForeignRefreshBeginResult {
        let binding = self.connection_binding();
        let Some(supervisor) = binding.supervisor() else {
            return ForeignRefreshBeginResult {
                refresh: None,
                failure: Some(unavailable()),
            };
        };
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.context.clone(),
            |supervisor, context| supervisor.begin_refresh_gatt_cache(context, binding.connection),
        ) {
            Ok(operation) => ForeignRefreshBeginResult {
                refresh: Some(Arc::new(SupervisedRefreshGattCache { operation })),
                failure: None,
            },
            Err(error) => {
                let failure: Box<ForeignOperationFailure> = error.into();
                ForeignRefreshBeginResult {
                    refresh: None,
                    failure: Some(*failure),
                }
            }
        }
    }
}

impl SupervisedConnection {
    fn connection_binding(&self) -> ConnectionBinding {
        ConnectionBinding {
            supervisor: self.supervisor.clone(),
            context: self.context.clone(),
            connection: self.id,
        }
    }
}

#[uniffi::export]
impl SupervisedGattDiscovery {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignGattDiscoveryResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignGattDiscoveryResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(database) => ForeignGattDiscoveryResult {
                session: Some(SupervisedGattSession::from_database(
                    self.binding.clone(),
                    database,
                )),
                failure: None,
            },
            Err(error) => ForeignGattDiscoveryResult::failed(bleng_failure(
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
impl SupervisedGattSession {
    /// Identity of this exact topology, never a device or attribute handle.
    pub fn same_session(&self, other: Arc<Self>) -> bool {
        self.binding.connection.context == other.binding.connection.context
            && self.binding.connection.connection == other.binding.connection.connection
            && self.binding.session == other.binding.session
    }

    pub fn services(&self) -> Vec<Arc<SupervisedGattService>> {
        self.services.clone()
    }
}

#[uniffi::export]
impl SupervisedGattService {
    pub fn uuid(&self) -> BluetoothUuid {
        self.uuid.clone()
    }

    /// `None` means the platform did not report primary/secondary.
    pub fn primary(&self) -> Option<bool> {
        self.primary
    }

    pub fn characteristics(&self) -> Vec<Arc<SupervisedGattCharacteristic>> {
        self.characteristics.clone()
    }
}

#[uniffi::export]
impl SupervisedGattCharacteristic {
    pub fn uuid(&self) -> BluetoothUuid {
        self.uuid.clone()
    }

    pub fn properties(&self) -> CharacteristicProperties {
        self.properties
    }

    /// Descriptors are capabilities: each carries the exact characteristic and session that
    /// discovered it, so a read or write cannot address a replacement topology.
    pub fn descriptors(&self) -> Vec<Arc<SupervisedGattDescriptor>> {
        self.descriptors.clone()
    }

    pub fn begin_read(&self) -> ForeignReadBeginResult {
        let Some(supervisor) = self.binding.connection.supervisor() else {
            return ForeignReadBeginResult::failed(unavailable());
        };
        let binding = &self.binding;
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.connection.context.clone(),
            |supervisor, context| {
                supervisor.begin_read_characteristic(
                    context,
                    binding.connection.connection,
                    binding.session,
                    self.id,
                )
            },
        ) {
            Ok(operation) => ForeignReadBeginResult {
                read: Some(Arc::new(SupervisedRead { operation })),
                failure: None,
            },
            Err(error) => ForeignReadBeginResult::failed(error.into()),
        }
    }

    /// `value` is bounded by Rust's ATT write limit; rejection comes back as this call's failure,
    /// not as a native submission.
    pub fn begin_write(&self, value: Vec<u8>, write_type: WriteType) -> ForeignWriteBeginResult {
        let Some(supervisor) = self.binding.connection.supervisor() else {
            return ForeignWriteBeginResult::failed(unavailable());
        };
        let binding = &self.binding;
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.connection.context.clone(),
            |supervisor, context| {
                supervisor.begin_write_characteristic(
                    context,
                    binding.connection.connection,
                    binding.session,
                    self.id,
                    value,
                    write_type,
                )
            },
        ) {
            Ok(operation) => ForeignWriteBeginResult {
                write: Some(Arc::new(SupervisedWrite { operation })),
                failure: None,
            },
            Err(error) => ForeignWriteBeginResult::failed(error.into()),
        }
    }
}

#[uniffi::export]
impl SupervisedGattDescriptor {
    pub fn uuid(&self) -> BluetoothUuid {
        self.uuid.clone()
    }

    pub fn begin_read(&self) -> ForeignDescriptorReadBeginResult {
        let Some(supervisor) = self.binding.connection.supervisor() else {
            return ForeignDescriptorReadBeginResult::failed(unavailable());
        };
        let binding = &self.binding;
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.connection.context.clone(),
            |supervisor, context| {
                supervisor.begin_read_descriptor(
                    context,
                    binding.connection.connection,
                    binding.session,
                    self.characteristic_id,
                    self.id,
                )
            },
        ) {
            Ok(operation) => ForeignDescriptorReadBeginResult {
                read: Some(Arc::new(SupervisedDescriptorRead { operation })),
                failure: None,
            },
            Err(error) => ForeignDescriptorReadBeginResult::failed(error.into()),
        }
    }

    /// Descriptor writes always expect an ATT response; there is no write type.
    pub fn begin_write(&self, value: Vec<u8>) -> ForeignDescriptorWriteBeginResult {
        let Some(supervisor) = self.binding.connection.supervisor() else {
            return ForeignDescriptorWriteBeginResult::failed(unavailable());
        };
        let binding = &self.binding;
        match supervisor.submit_operation::<_, SupervisorOperationError>(
            binding.connection.context.clone(),
            |supervisor, context| {
                supervisor.begin_write_descriptor(
                    context,
                    binding.connection.connection,
                    binding.session,
                    self.characteristic_id,
                    self.id,
                    value,
                )
            },
        ) {
            Ok(operation) => ForeignDescriptorWriteBeginResult {
                write: Some(Arc::new(SupervisedDescriptorWrite { operation })),
                failure: None,
            },
            Err(error) => ForeignDescriptorWriteBeginResult::failed(error.into()),
        }
    }
}

#[uniffi::export]
impl SupervisedDescriptorRead {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignDescriptorReadValueResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignDescriptorReadValueResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(value) => ForeignDescriptorReadValueResult {
                value: Some(value),
                failure: None,
            },
            Err(error) => ForeignDescriptorReadValueResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

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
impl SupervisedDescriptorWrite {
    /// Absence of failure is BLeng's completion receipt, not a read-back of the peer's value.
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
impl SupervisedRead {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignReadValueResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignReadValueResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(value) => ForeignReadValueResult {
                value: Some(value),
                failure: None,
            },
            Err(error) => ForeignReadValueResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

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
impl SupervisedWrite {
    /// Absence of failure is BLeng's completion receipt, not a read-back of the peer's value.
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
impl SupervisedConnectionPriority {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignOperationCommandResult::from_failure(Some(*failure)),
        };
        ForeignOperationCommandResult::from_failure(
            self.operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

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
impl SupervisedBond {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignBondValueResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignBondValueResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(state) => ForeignBondValueResult {
                state: Some(state),
                failure: None,
            },
            Err(error) => ForeignBondValueResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_failure(
            self.operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        )
    }

    /// Cancel-safe progress pull. The terminal bond receipt still comes from `wait_result`; this
    /// lane only reports the platform states that lead there.
    pub async fn next_state(&self, timeout_ms: f64) -> ForeignBondProgress {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignBondProgress::failed(*failure),
        };
        let progress = Arc::clone(&self.progress);
        match self
            .operation
            .observe_with_timeout(timeout_ms, async move { progress.next_state().await })
            .await
        {
            Ok(Ok(BondProgressStep::State { state, ended })) => ForeignBondProgress {
                state: Some(state),
                ended,
                failure: None,
            },
            Ok(Ok(BondProgressStep::End(end))) => ForeignBondProgress {
                state: None,
                ended: true,
                failure: bond_progress_end(end),
            },
            Ok(Ok(BondProgressStep::Failed(error))) => ForeignBondProgress::failed(ble_failure(
                ForeignOperationFailureDomain::Operation,
                "ble",
                error,
            )),
            Ok(Err(error)) => ForeignBondProgress::failed(bond_progress_failure(error)),
            Err(error) => ForeignBondProgress::failed(bleng_failure(
                ForeignOperationFailureDomain::Observation,
                error,
            )),
        }
    }
}

#[uniffi::export]
impl SupervisedRssi {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignRssiValueResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignRssiValueResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(rssi) => ForeignRssiValueResult {
                rssi: Some(rssi),
                failure: None,
            },
            Err(error) => ForeignRssiValueResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

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
impl SupervisedMtu {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignMtuValueResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignMtuValueResult::failed(*failure),
        };
        match self.operation.wait(timeout_ms).await {
            Ok(info) => ForeignMtuValueResult {
                info: Some(info),
                failure: None,
            },
            Err(error) => ForeignMtuValueResult::failed(bleng_failure(
                ForeignOperationFailureDomain::Operation,
                error,
            )),
        }
    }

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
