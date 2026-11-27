//! Product projection of the existing scan operation and delivery, without another lifecycle.

use std::sync::Arc;

use blehub::{
    AndroidScanMode, AndroidScanOptions, AndroidScanPhy, BluetoothUuid, CommandDispatchError,
    ManufacturerData, PeripheralId, ScanOptions, ScanStopReason, ScanStopResult, ServiceData,
};
use bleng::StopScanOperation;

use super::failure::{
    ForeignOperationCommandResult, ForeignOperationFailure, ForeignOperationFailureDomain,
    ble_failure, bleng_failure, normalize_timeout,
};
use super::scan_session::ScanSessionError;
use super::{ForeignOwnerContext, SupervisedScan, Supervisor};
use crate::{ScanDeliveryError, ScanEvent};

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignScanOptions {
    pub service_uuids: Vec<String>,
    pub name_prefix: Option<String>,
    pub exact_name: Option<String>,
    pub company_id: Option<f64>,
    pub min_rssi: Option<f64>,
    pub manufacturer_data: Option<ForeignManufacturerDataFilter>,
    pub android: Option<ForeignAndroidScanOptions>,
    pub allow_duplicates: bool,
}

/// Android-only scanner tuning, mirroring [`AndroidScanOptions`].
#[derive(Clone, Copy, Debug, Default, uniffi::Record)]
pub struct ForeignAndroidScanOptions {
    pub scan_mode: Option<AndroidScanMode>,
    pub phy: Option<AndroidScanPhy>,
    pub report_delay_ms: Option<f64>,
    pub legacy: Option<bool>,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignManufacturerDataFilter {
    pub company_id: f64,
    pub data: Vec<u8>,
    pub mask: Option<Vec<u8>>,
}

impl ForeignScanOptions {
    /// Converts the foreign (JS) numbers into the contract's integer widths, then applies the
    /// canonical filter rule.
    ///
    /// Only **representability** is decided here: a value that cannot round-trip through `u16`/
    /// `i16`/`u32` (fractional, non-finite, out of range) is an input error because there is no
    /// contract value to hand over. Every *semantic* rule — UUID spelling, name bounds, mask length,
    /// report-delay cap — belongs to [`ScanOptions::validated`], so this boundary can refuse a bad
    /// filter synchronously without owning a second copy of those rules.
    fn validate(self) -> Result<ScanOptions, Box<ForeignOperationFailure>> {
        let company_id = self
            .company_id
            .map(|value| {
                integral(
                    value,
                    "invalid_company_id",
                    "company_id must be an integer between 0 and 65535",
                    0.0..=u16::MAX as f64,
                )
                .map(|value| value as u16)
            })
            .transpose()?;
        let min_rssi = self
            .min_rssi
            .map(|value| {
                integral(
                    value,
                    "invalid_min_rssi",
                    "min_rssi must be an integer between -32768 and 32767",
                    i16::MIN as f64..=i16::MAX as f64,
                )
                .map(|value| value as i16)
            })
            .transpose()?;
        let manufacturer_data = self
            .manufacturer_data
            .map(
                |filter| -> Result<blehub::ManufacturerDataFilter, Box<ForeignOperationFailure>> {
                    Ok(blehub::ManufacturerDataFilter {
                        company_id: integral(
                            filter.company_id,
                            "invalid_manufacturer_company_id",
                            "manufacturer_data.company_id must be an integer between 0 and 65535",
                            0.0..=u16::MAX as f64,
                        )? as u16,
                        data: filter.data,
                        mask: filter.mask,
                    })
                },
            )
            .transpose()?;
        let android = self
            .android
            .map(
                |android| -> Result<AndroidScanOptions, Box<ForeignOperationFailure>> {
                    let report_delay_ms = android
                        .report_delay_ms
                        .map(|value| {
                            integral(
                                value,
                                "invalid_report_delay",
                                "android.report_delay_ms must be a non-negative integer",
                                0.0..=u32::MAX as f64,
                            )
                            .map(|value| value as u32)
                        })
                        .transpose()?;
                    Ok(AndroidScanOptions {
                        scan_mode: android.scan_mode,
                        phy: android.phy,
                        report_delay_ms,
                        legacy: android.legacy,
                    })
                },
            )
            .transpose()?;
        ScanOptions {
            service_uuids: self
                .service_uuids
                .into_iter()
                .map(|value| BluetoothUuid { value })
                .collect(),
            name_prefix: self.name_prefix,
            exact_name: self.exact_name,
            company_id,
            min_rssi,
            manufacturer_data,
            android,
            allow_duplicates: self.allow_duplicates,
        }
        .validated()
        .map_err(rejected_scan_options)
    }
}

/// One foreign number that must survive the cast into `range` without losing a fraction.
fn integral(
    value: f64,
    code: &'static str,
    message: &'static str,
    range: impl std::ops::RangeBounds<f64>,
) -> Result<f64, Box<ForeignOperationFailure>> {
    if !value.is_finite() || value.fract() != 0.0 || !range.contains(&value) {
        return Err(Box::new(ForeignOperationFailure::input(code, message)));
    }
    Ok(value)
}

/// One caller-facing refusal from the canonical filter rule, keeping its field detail.
fn rejected_scan_options(error: CommandDispatchError) -> Box<ForeignOperationFailure> {
    let detail = match error {
        // The canonical rule refuses a filter only with a caller-facing reason.
        CommandDispatchError::InvalidRequest { detail } => detail,
        other => other.to_string(),
    };
    Box::new(ForeignOperationFailure::input(
        "invalid_scan_options",
        &detail,
    ))
}

impl ForeignOperationCommandResult {
    fn from_scan_result(result: Result<(), ScanSessionError>) -> Self {
        Self::from_failure(result.err().map(session_failure))
    }
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanBeginResult {
    pub scan: Option<Arc<SupervisedScan>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanStopResult {
    pub operation: Option<Arc<SupervisedScanStop>>,
    pub failure: Option<ForeignOperationFailure>,
}

#[derive(Clone, uniffi::Record)]
pub struct ForeignScanObservation {
    pub advertisement: Option<ForeignScanAdvertisement>,
    pub ended: Option<ForeignScanEnd>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignScanObservation {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            advertisement: None,
            ended: None,
            failure: Some(failure),
        }
    }

    fn ended(reason: ForeignScanEndReason, failure: Option<ForeignOperationFailure>) -> Self {
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
    /// This observation endpoint could not keep up; only this lane ended.
    Overflow,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct ForeignScanEnd {
    pub reason: ForeignScanEndReason,
    pub failure: Option<ForeignOperationFailure>,
}

/// A fixed owner-scoped capability, not a native handle or a strong owner reference.
#[derive(uniffi::Object)]
pub struct SupervisedPeripheral {
    pub(in crate::ffi) supervisor: std::sync::Weak<Supervisor>,
    pub(in crate::ffi) context: ForeignOwnerContext,
    pub(in crate::ffi) id: PeripheralId,
    /// Local name from the advertisement that produced this capability.
    pub(in crate::ffi) local_name: Option<String>,
}

#[uniffi::export]
impl SupervisedPeripheral {
    pub fn same_peripheral(&self, other: Arc<Self>) -> bool {
        self.context == other.context && self.id == other.id
    }

    /// Opaque and owner-scoped: stable across advertisements of the same peer, useful only as a
    /// map key. It is not a platform device id and cannot address native state.
    pub fn key(&self) -> String {
        self.id.value.to_string()
    }

    /// Local name observed in the advertisement that produced this capability.
    ///
    /// `None` when the peer advertised no name, and for a capability adopted from an inventory key
    /// before any new observation. This is the peer's self-reported name, never a platform device
    /// name and never a claim about a live link.
    pub fn local_name(&self) -> Option<String> {
        self.local_name.clone()
    }
}

#[derive(uniffi::Object)]
pub struct SupervisedScanStop {
    operation: Arc<StopScanOperation>,
}

#[uniffi::export]
impl SupervisedScanStop {
    pub async fn wait_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignOperationCommandResult {
                    failure: Some(*failure),
                };
            }
        };
        ForeignOperationCommandResult {
            failure: self
                .operation
                .wait(timeout_ms)
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        }
    }

    pub async fn cancel_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult {
            failure: self
                .operation
                .cancel()
                .await
                .err()
                .map(|error| bleng_failure(ForeignOperationFailureDomain::Operation, error)),
        }
    }
}

/// One explicitly adopted inventory key. The key is an owner-scoped opaque identity, not a
/// platform address; a backend still refuses a key it never allocated.
#[derive(Clone, uniffi::Record)]
pub struct ForeignPeripheralAdoptResult {
    pub peripheral: Option<Arc<SupervisedPeripheral>>,
    pub failure: Option<ForeignOperationFailure>,
}

impl ForeignPeripheralAdoptResult {
    fn failed(failure: ForeignOperationFailure) -> Self {
        Self {
            peripheral: None,
            failure: Some(failure),
        }
    }
}

#[uniffi::export]
impl Supervisor {
    /// Adopts one inventory key as a peripheral capability.
    ///
    /// Nothing is connected here: the returned capability submits a normal `connect`, and a backend
    /// that did not allocate the key refuses it. This is how an iOS-restored peer is re-addressed
    /// after a relaunch, without making every inventory record implicitly connectable.
    pub fn adopt_peripheral(
        self: Arc<Self>,
        context: ForeignOwnerContext,
        key: String,
    ) -> ForeignPeripheralAdoptResult {
        let value = match key.parse::<u64>() {
            Ok(value) => value,
            Err(_) => {
                return ForeignPeripheralAdoptResult::failed(ForeignOperationFailure::input(
                    "invalid_peripheral_key",
                    "the inventory key is not a valid owner-scoped identity",
                ));
            }
        };
        ForeignPeripheralAdoptResult {
            peripheral: Some(Arc::new(SupervisedPeripheral {
                supervisor: Arc::downgrade(&self),
                context,
                id: PeripheralId { value },
                local_name: None,
            })),
            failure: None,
        }
    }

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
                .begin_scan_view(context, options)
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
    pub async fn start_result(&self, timeout_ms: f64) -> ForeignOperationCommandResult {
        let timeout_ms = match normalize_timeout(timeout_ms) {
            Ok(timeout_ms) => timeout_ms,
            Err(failure) => {
                return ForeignOperationCommandResult {
                    failure: Some(*failure),
                };
            }
        };
        ForeignOperationCommandResult::from_scan_result(self.wait_until_started(timeout_ms).await)
    }

    pub async fn cancel_start_result(&self) -> ForeignOperationCommandResult {
        ForeignOperationCommandResult::from_scan_result(self.cancel_start().await)
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
                        supervisor: self.source_supervisor(),
                        context: self.source_context().clone(),
                        id: advertisement.peripheral_id,
                        local_name: advertisement.local_name.clone(),
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
                        ForeignOperationFailureDomain::Operation,
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
            // A consumer that cannot keep up ends its own observation lane. The native scan and
            // every other operation keep running; this is not a runtime-wide fault.
            Err(ScanSessionError::Delivery {
                error:
                    ScanDeliveryError::AdvertisementOverflow
                    | ScanDeliveryError::ByteOverflow
                    | ScanDeliveryError::StopDataOverflow
                    | ScanDeliveryError::PendingEventOverflow,
            }) => ForeignScanObservation::ended(ForeignScanEndReason::Overflow, None),
            Err(error) => ForeignScanObservation::failed(session_failure(error)),
            // ScanDelivery consumes Started internally; never treat one as an advertisement/end.
            Ok(ScanEvent::Started { .. }) => {
                ForeignScanObservation::failed(ForeignOperationFailure::new(
                    ForeignOperationFailureDomain::Delivery,
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

fn session_failure(error: ScanSessionError) -> ForeignOperationFailure {
    use ForeignOperationFailureDomain as Domain;
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
            return ForeignOperationFailure::new(
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
    ForeignOperationFailure::new(Domain::Delivery, code, message)
}

#[cfg(test)]
#[path = "tests/scan_view.rs"]
mod tests;
