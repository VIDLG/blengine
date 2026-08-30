use super::{BluetoothUuid, PeripheralId, ScanSessionId};

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ManufacturerData {
    pub company_id: u16,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct ServiceData {
    pub service_uuid: BluetoothUuid,
    pub payload: Vec<u8>,
}

/// A platform-normalized scan observation, not a raw RF packet or canonical
/// peripheral state. Fields may include data merged by the native platform.
#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct AdvertisementObservation {
    pub scan_session_id: ScanSessionId,
    pub peripheral_id: PeripheralId,
    pub local_name: Option<String>,
    pub advertised_services: Vec<BluetoothUuid>,
    pub manufacturer_data: Vec<ManufacturerData>,
    pub service_data: Vec<ServiceData>,
    pub rssi: Option<i16>,
    pub connectable: Option<bool>,
}

impl AdvertisementObservation {
    pub(crate) fn validate(&self) -> Result<(), String> {
        for (index, uuid) in self.advertised_services.iter().enumerate() {
            if !uuid.is_canonical() {
                return Err(format!(
                    "advertised_services[{index}] is not a canonical Bluetooth UUID"
                ));
            }
        }
        for (index, data) in self.service_data.iter().enumerate() {
            if !data.service_uuid.is_canonical() {
                return Err(format!(
                    "service_data[{index}].service_uuid is not a canonical Bluetooth UUID"
                ));
            }
        }
        Ok(())
    }
}
