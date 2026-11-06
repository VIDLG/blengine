//! Product projection of the existing scan operation and delivery, without another lifecycle.

use std::sync::Arc;

use blehub::{
    BleErrorCategory, BleEventError, BleHubError, BluetoothUuid, CommandDispatchError,
    ManufacturerData, PeripheralId, PlatformErrorDetails, ScanOptions, ScanStopReason,
    ScanStopResult, ServiceData,
};
use bleng::{
    BlengBusyReason, BlengContractFailure, BlengError, BlengNotReadyReason, BlengResource,
    StopScanOperation,
};

use super::{ForeignOwnerContext, ScanSessionError, SupervisedScan, Supervisor};
use crate::{ScanDeliveryError, ScanEvent, SupervisorOperationError};

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignScanFailureDomain {
    Input,
    Submission,
    Operation,
    Observation,
    Delivery,
    Lifecycle,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ForeignScanFailure {
    pub domain: ForeignScanFailureDomain,
    pub code: String,
    pub message: String,
    pub platform_details: Option<PlatformErrorDetails>,
}

impl ForeignScanFailure {
    fn new(domain: ForeignScanFailureDomain, code: &str, message: &str) -> Self {
        Self {
            domain,
            code: code.to_owned(),
            message: message.to_owned(),
            platform_details: None,
        }
    }

    fn input(code: &str, message: &str) -> Self {
        Self::new(ForeignScanFailureDomain::Input, code, message)
    }
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignScanOptions {
    pub service_uuids: Vec<String>,
    pub name_prefix: Option<String>,
    pub company_id: Option<f64>,
    pub allow_duplicates: bool,
}

impl ForeignScanOptions {
    fn validate(self) -> Result<ScanOptions, Box<ForeignScanFailure>> {
        let company_id = self
            .company_id
            .map(|value| {
                if !value.is_finite()
                    || value.fract() != 0.0
                    || !(0.0..=u16::MAX as f64).contains(&value)
                {
                    return Err(Box::new(ForeignScanFailure::input(
                        "invalid_company_id",
                        "company_id must be an integer between 0 and 65535",
                    )));
                }
                Ok(value as u16)
            })
            .transpose()?;
        let service_uuids = self
            .service_uuids
            .into_iter()
            .map(|value| {
                BluetoothUuid::canonicalize(&value).ok_or_else(|| {
                    Box::new(ForeignScanFailure::input(
                        "invalid_service_uuid",
                        "service_uuids contains an invalid Bluetooth UUID",
                    ))
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        // Match BleHub's fixed predicate contract, including its UTF-8 byte bound.
        if let Some(prefix) = &self.name_prefix {
            if prefix.is_empty() {
                return Err(Box::new(ForeignScanFailure::input(
                    "empty_name_prefix",
                    "name_prefix must not be empty",
                )));
            }
            if prefix.len() > blehub::MAX_SCAN_NAME_PREFIX_BYTES {
                return Err(Box::new(ForeignScanFailure::input(
                    "name_prefix_too_long",
                    "name_prefix exceeds 248 UTF-8 bytes",
                )));
            }
        }
        Ok(ScanOptions {
            service_uuids,
            name_prefix: self.name_prefix,
            company_id,
            allow_duplicates: self.allow_duplicates,
        })
    }
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignScanCommandResult {
    pub failure: Option<ForeignScanFailure>,
}

impl ForeignScanCommandResult {
    fn from_result(result: Result<(), ScanSessionError>) -> Self {
        Self {
            failure: result.err().map(session_failure),
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanBeginResult {
    pub scan: Option<Arc<SupervisedScan>>,
    pub failure: Option<ForeignScanFailure>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanStopResult {
    pub operation: Option<Arc<SupervisedScanStop>>,
    pub failure: Option<ForeignScanFailure>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanObservation {
    pub advertisement: Option<ForeignScanAdvertisement>,
    pub ended: Option<ForeignScanEnd>,
    pub failure: Option<ForeignScanFailure>,
}

impl ForeignScanObservation {
    fn failed(failure: ForeignScanFailure) -> Self {
        Self {
            advertisement: None,
            ended: None,
            failure: Some(failure),
        }
    }

    fn ended(reason: ForeignScanEndReason, failure: Option<ForeignScanFailure>) -> Self {
        Self {
            advertisement: None,
            ended: Some(ForeignScanEnd { reason, failure }),
            failure: None,
        }
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanAdvertisement {
    pub peripheral: Arc<SupervisedPeripheral>,
    pub local_name: Option<String>,
    pub advertised_services: Vec<String>,
    pub manufacturer_data: Vec<ManufacturerData>,
    pub service_data: Vec<ServiceData>,
    pub rssi: Option<i16>,
    pub connectable: Option<bool>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Enum)]
pub enum ForeignScanEndReason {
    Requested,
    SystemSuspended,
    Failed,
    OwnerClosed,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignScanEnd {
    pub reason: ForeignScanEndReason,
    pub failure: Option<ForeignScanFailure>,
}

/// A fixed owner-scoped capability, not a native handle or a strong owner reference.
#[derive(uniffi::Object)]
pub struct SupervisedPeripheral {
    context: ForeignOwnerContext,
    id: PeripheralId,
}

#[uniffi::export]
impl SupervisedPeripheral {
    pub fn same_peripheral(&self, other: Arc<Self>) -> bool {
        self.context == other.context && self.id == other.id
    }
}

#[derive(uniffi::Object)]
pub struct SupervisedScanStop {
    operation: Arc<StopScanOperation>,
}

#[uniffi::export]
impl SupervisedScanStop {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignScanCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignScanCommandResult {
                    failure: Some(*failure),
                };
            }
        };
        ForeignScanCommandResult {
            failure: self
                .operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignScanFailureDomain::Operation, error)),
        }
    }

    pub async fn cancel_result(&self) -> ForeignScanCommandResult {
        ForeignScanCommandResult {
            failure: self
                .operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignScanFailureDomain::Operation, error)),
        }
    }
}

#[uniffi::export]
impl Supervisor {
    pub fn begin_scan_view(
        self: Arc<Self>,
        context: ForeignOwnerContext,
        options: ForeignScanOptions,
    ) -> ForeignScanBeginResult {
        let handoff = self.submit_operation(context.clone(), |supervisor, context| {
            // Keep the original lock/owner guard through gate, validation and submission.
            // Calling the exported begin_scan here would recursively acquire that lock.
            let _owner = supervisor.operation_owner(context)?;
            let options = options.validate()?;
            supervisor
                .begin_scan(context, options)
                .map_err(ScanSessionError::from)
                .map_err(session_failure)
                .map_err(Box::new)
        });
        match handoff {
            Ok(handoff) => ForeignScanBeginResult {
                scan: Some(SupervisedScan::new(&self, context, handoff)),
                failure: None,
            },
            Err(failure) => ForeignScanBeginResult {
                scan: None,
                failure: Some(*failure),
            },
        }
    }
}

#[uniffi::export]
impl SupervisedScan {
    pub async fn start_result(&self, timeout_ms: f64) -> ForeignScanCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignScanCommandResult {
                    failure: Some(*failure),
                };
            }
        };
        ForeignScanCommandResult::from_result(self.wait_until_started(timeout_ms).await)
    }

    pub async fn cancel_start_result(&self) -> ForeignScanCommandResult {
        ForeignScanCommandResult::from_result(self.cancel_start().await)
    }

    pub async fn next_observation(&self, timeout_ms: f64) -> ForeignScanObservation {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => return ForeignScanObservation::failed(*failure),
        };
        match self.next_event(timeout_ms).await {
            Ok(ScanEvent::Advertisement { advertisement }) => ForeignScanObservation {
                advertisement: Some(ForeignScanAdvertisement {
                    peripheral: Arc::new(SupervisedPeripheral {
                        context: self.source_context().clone(),
                        id: advertisement.peripheral_id,
                    }),
                    local_name: advertisement.local_name,
                    advertised_services: advertisement
                        .advertised_services
                        .into_iter()
                        .map(|uuid| uuid.value)
                        .collect(),
                    manufacturer_data: advertisement.manufacturer_data,
                    service_data: advertisement.service_data,
                    rssi: advertisement.rssi,
                    connectable: advertisement.connectable,
                }),
                ended: None,
                failure: None,
            },
            Ok(ScanEvent::Stopped { result, .. }) => match result {
                ScanStopResult::Ok { reason } => ForeignScanObservation::ended(
                    match reason {
                        ScanStopReason::Requested { .. } => ForeignScanEndReason::Requested,
                        ScanStopReason::SystemSuspended => ForeignScanEndReason::SystemSuspended,
                    },
                    None,
                ),
                ScanStopResult::Err { error } => ForeignScanObservation::ended(
                    ForeignScanEndReason::Failed,
                    Some(ble_failure(
                        ForeignScanFailureDomain::Operation,
                        "ble",
                        error,
                    )),
                ),
            },
            Err(ScanSessionError::Delivery {
                error: ScanDeliveryError::OwnerRetired,
            }) => {
                // Delivery retirement is not a synthesized native ScanStopped receipt.
                ForeignScanObservation::ended(ForeignScanEndReason::OwnerClosed, None)
            }
            Err(error) => ForeignScanObservation::failed(session_failure(error)),
            // ScanDelivery consumes Started internally; never treat one as an advertisement/end.
            Ok(ScanEvent::Started { .. }) => {
                ForeignScanObservation::failed(ForeignScanFailure::new(
                    ForeignScanFailureDomain::Delivery,
                    "delivery.unexpected_started",
                    "unexpected scan start observation",
                ))
            }
        }
    }

    pub fn begin_stop_view(&self) -> ForeignScanStopResult {
        match self.begin_stop() {
            Ok(operation) => ForeignScanStopResult {
                operation: Some(Arc::new(SupervisedScanStop { operation })),
                failure: None,
            },
            Err(error) => ForeignScanStopResult {
                operation: None,
                failure: Some(session_failure(error)),
            },
        }
    }
}

fn normalize_timeout(value: f64) -> Result<u64, Box<ForeignScanFailure>> {
    // u64::MAX rounds up to 2^64 as f64. Reject that exclusive upper bound before casting.
    // Integral values (including zero/over-24h) then use the original u64 validation, before
    // the original operation takes its result slot or installs a pull.
    if !value.is_finite() || value.fract() != 0.0 || value < 0.0 || value >= u64::MAX as f64 {
        return Err(Box::new(ForeignScanFailure::input(
            "invalid_timeout",
            "timeout must be an integer between 1 ms and 24 hours",
        )));
    }
    Ok(value as u64)
}

// Only internal Result errors are boxed; the fixed foreign DTO layout stays unchanged.
impl From<SupervisorOperationError> for Box<ForeignScanFailure> {
    fn from(error: SupervisorOperationError) -> Self {
        Box::new(ForeignScanFailure::from(error))
    }
}

impl From<SupervisorOperationError> for ForeignScanFailure {
    fn from(error: SupervisorOperationError) -> Self {
        let (code, message) = match error {
            SupervisorOperationError::NoActiveSession => (
                "supervisor.no_active_session",
                "supervisor has no active session",
            ),
            SupervisorOperationError::StaleContext => (
                "supervisor.stale_context",
                "operation context does not match the active owner",
            ),
            SupervisorOperationError::RecoveryNotReady => (
                "supervisor.recovery_not_ready",
                "supervisor recovery is not ready for operations",
            ),
            SupervisorOperationError::SupervisorBusy => (
                "supervisor.busy",
                "supervisor operation is already in progress",
            ),
            SupervisorOperationError::SupervisorUnavailable => {
                ("supervisor.unavailable", "supervisor is unavailable")
            }
            SupervisorOperationError::InvalidContext { detail } => {
                return Self::input("supervisor.invalid_context", &detail);
            }
            SupervisorOperationError::Bleng { error } => {
                return bleng_failure(ForeignScanFailureDomain::Submission, error);
            }
        };
        Self::new(ForeignScanFailureDomain::Lifecycle, code, message)
    }
}

fn session_failure(error: ScanSessionError) -> ForeignScanFailure {
    use ForeignScanFailureDomain as Domain;
    let (code, message) = match error {
        ScanSessionError::Submission { error } => return error.into(),
        ScanSessionError::Operation { error } => return bleng_failure(Domain::Operation, error),
        ScanSessionError::Observation { error } => {
            return bleng_failure(Domain::Observation, error);
        }
        ScanSessionError::DeliveryBusy => {
            ("delivery.busy", "another scan delivery is still attached")
        }
        ScanSessionError::StartNotObserved => {
            return ForeignScanFailure::new(
                Domain::Submission,
                "scan.start_not_observed",
                "no scan identity has been confirmed or observed",
            );
        }
        ScanSessionError::Delivery { error } => match error {
            ScanDeliveryError::PullBusy => ("delivery.pull_busy", "another scan pull is pending"),
            ScanDeliveryError::StartFailed => (
                "delivery.start_failed",
                "scan start failed; observation detached without native rollback",
            ),
            ScanDeliveryError::AdvertisementOverflow => (
                "delivery.advertisement_overflow",
                "scan advertisement count budget exhausted",
            ),
            ScanDeliveryError::ByteOverflow => (
                "delivery.byte_overflow",
                "scan advertisement data budget exhausted",
            ),
            ScanDeliveryError::StopDataOverflow => (
                "delivery.stop_data_overflow",
                "scan stop error data budget exhausted",
            ),
            ScanDeliveryError::PendingEventOverflow => (
                "delivery.pending_event_overflow",
                "scan end-before-start staging budget exhausted",
            ),
            ScanDeliveryError::OwnerRetired => {
                ("delivery.owner_retired", "scan source owner retired")
            }
            ScanDeliveryError::Detached => {
                ("delivery.detached", "scan observation endpoint detached")
            }
            ScanDeliveryError::Unavailable => (
                "delivery.unavailable",
                "scan delivery synchronization failed",
            ),
            ScanDeliveryError::ConflictingStart => (
                "delivery.conflicting_start",
                "scan start identity conflicts with its earlier binding",
            ),
        },
    };
    ForeignScanFailure::new(Domain::Delivery, code, message)
}

fn bleng_failure(domain: ForeignScanFailureDomain, error: BlengError) -> ForeignScanFailure {
    let (code, message) = match error {
        BlengError::Closed => ("closed", "BLeng is closed"),
        BlengError::Cancelled => ("cancelled", "BLeng operation was cancelled"),
        BlengError::OperationNotFound { .. } => {
            ("operation_not_found", "BLeng operation was not found")
        }
        BlengError::OperationAlreadyCompleted { .. } => (
            "operation_already_completed",
            "BLeng operation already completed",
        ),
        BlengError::OperationNotCancellable { .. } => (
            "operation_not_cancellable",
            "BLeng operation cannot be cancelled",
        ),
        BlengError::OperationResultConsumed => (
            "operation_result_consumed",
            "BLeng operation result was already consumed",
        ),
        BlengError::Disconnected => ("disconnected", "BLE connection was lost"),
        BlengError::InvalidTimeout => {
            return ForeignScanFailure::input(
                "invalid_timeout",
                "timeout must be between 1 ms and 24 hours",
            );
        }
        BlengError::NotReady { reason } => match reason {
            BlengNotReadyReason::BackendNotAssembled => (
                "not_ready.backend_not_assembled",
                "no backend has been assembled",
            ),
            BlengNotReadyReason::NotInitialized => (
                "not_ready.not_initialized",
                "adapter initialization has not completed",
            ),
            BlengNotReadyReason::ScanNotActive => {
                ("not_ready.scan_not_active", "scan is not active")
            }
            BlengNotReadyReason::ConnectionNotActive => (
                "not_ready.connection_not_active",
                "connection is not active",
            ),
            BlengNotReadyReason::GattNotReady => ("not_ready.gatt_not_ready", "GATT is not ready"),
            BlengNotReadyReason::SubscriptionNotActive => (
                "not_ready.subscription_not_active",
                "subscription is not active",
            ),
        },
        BlengError::Busy { reason } => match reason {
            BlengBusyReason::Scan => ("busy.scan", "scan is busy"),
            BlengBusyReason::Peripheral => ("busy.peripheral", "peripheral is busy"),
            BlengBusyReason::Gatt => ("busy.gatt", "GATT is busy"),
            BlengBusyReason::Operation => ("busy.operation", "a conflicting operation is pending"),
            BlengBusyReason::BackendSlotOccupied => {
                ("busy.backend_slot_occupied", "backend slot is occupied")
            }
        },
        BlengError::ResourceExhausted { resource } => match resource {
            BlengResource::PendingWaiters => (
                "resource_exhausted.pending_waiters",
                "pending waiter capacity exhausted",
            ),
            BlengResource::SubmissionQueue => (
                "resource_exhausted.submission_queue",
                "submission queue capacity exhausted",
            ),
            BlengResource::Identifiers => (
                "resource_exhausted.identifiers",
                "identifier capacity exhausted",
            ),
        },
        BlengError::ContractFailure { reason } => match reason {
            BlengContractFailure::InvalidCommandId => (
                "contract_failure.invalid_command_id",
                "invalid command identity",
            ),
            BlengContractFailure::DuplicateHubOwnership => (
                "contract_failure.duplicate_hub_ownership",
                "duplicate Hub ownership",
            ),
        },
        BlengError::DispatchRejected { error } => return dispatch_failure(domain, error),
        BlengError::Operation { error } => return ble_failure(domain, "bleng.operation", error),
        BlengError::Assembly { error } => return hub_failure(domain, "bleng.assembly", error),
        BlengError::Cleanup { error } => return hub_failure(domain, "bleng.cleanup", error),
        BlengError::Timeout => ("timeout", "BLeng operation timed out"),
        BlengError::TransportFault => ("transport_fault", "BLeng transport fault"),
        BlengError::WorkerStopped => ("worker_stopped", "BLeng worker stopped"),
    };
    ForeignScanFailure::new(domain, &format!("bleng.{code}"), message)
}

fn dispatch_failure(
    domain: ForeignScanFailureDomain,
    error: CommandDispatchError,
) -> ForeignScanFailure {
    let mut platform_details = None;
    let (code, message) = match error {
        CommandDispatchError::Closed => ("closed", "BleHub is closed".to_owned()),
        CommandDispatchError::InvalidRequest { detail } => ("invalid_request", detail),
        CommandDispatchError::InvalidState { detail } => ("invalid_state", detail),
        CommandDispatchError::OperationNotFound { .. } => {
            ("operation_not_found", "operation was not found".to_owned())
        }
        CommandDispatchError::OperationAlreadyCompleted { .. } => (
            "operation_already_completed",
            "operation already completed".to_owned(),
        ),
        CommandDispatchError::OperationNotCancellable { .. } => (
            "operation_not_cancellable",
            "operation cannot be cancelled".to_owned(),
        ),
        CommandDispatchError::ResourceExhausted { detail } => ("resource_exhausted", detail),
        CommandDispatchError::BackendUnavailable {
            detail,
            platform_details: details,
        } => {
            platform_details = details;
            ("backend_unavailable", detail)
        }
        CommandDispatchError::FfiContract { detail } => ("ffi_contract", detail),
    };
    ForeignScanFailure {
        domain,
        code: format!("bleng.dispatch_rejected.{code}"),
        message,
        platform_details,
    }
}

fn hub_failure(
    domain: ForeignScanFailureDomain,
    path: &str,
    error: BleHubError,
) -> ForeignScanFailure {
    let mut platform_details = None;
    let (code, message) = match error {
        BleHubError::BackendAssembly { detail } => ("backend_assembly", detail),
        BleHubError::BackendCleanup {
            detail,
            platform_details: details,
        } => {
            platform_details = details;
            ("backend_cleanup", detail)
        }
        BleHubError::ResourceBusy { detail } => ("resource_busy", detail),
        BleHubError::InvalidConfiguration { detail, .. } => ("invalid_configuration", detail),
        BleHubError::FfiContract { detail } => ("ffi_contract", detail),
    };
    ForeignScanFailure {
        domain,
        code: format!("{path}.{code}"),
        message,
        platform_details,
    }
}

fn ble_failure(
    domain: ForeignScanFailureDomain,
    path: &str,
    error: BleEventError,
) -> ForeignScanFailure {
    let category = match error.category {
        BleErrorCategory::Permission => "permission",
        BleErrorCategory::AdapterUnavailable => "adapter_unavailable",
        BleErrorCategory::InvalidState => "invalid_state",
        BleErrorCategory::NotFound => "not_found",
        BleErrorCategory::Timeout => "timeout",
        BleErrorCategory::Cancelled => "cancelled",
        BleErrorCategory::OperationAborted => "operation_aborted",
        BleErrorCategory::Connection => "connection",
        BleErrorCategory::Gatt => "gatt",
        BleErrorCategory::Protocol => "protocol",
        BleErrorCategory::NotSupported => "not_supported",
        BleErrorCategory::Backend => "backend",
        BleErrorCategory::Internal => "internal",
    };
    ForeignScanFailure {
        domain,
        code: format!("{path}.{category}"),
        message: error.message,
        platform_details: error.platform_details,
    }
}

#[cfg(test)]
#[path = "tests/scan_view.rs"]
mod tests;
