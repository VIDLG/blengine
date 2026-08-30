use std::collections::HashSet;

use super::{
    BluetoothUuid, ConnectionId, GattCharacteristicId, GattDescriptorId, GattServiceId,
    GattSessionId,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, uniffi::Record)]
pub struct CharacteristicProperties {
    pub readable: bool,
    pub writable_with_response: bool,
    pub writable_without_response: bool,
    pub notifiable: bool,
    pub indicatable: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct GattDatabase {
    pub connection_id: ConnectionId,
    pub gatt_session_id: GattSessionId,
    pub services: Vec<GattService>,
}

impl GattDatabase {
    pub(crate) fn validate(&self) -> Result<(), String> {
        let mut service_ids = HashSet::new();
        let mut characteristic_ids = HashSet::new();
        let mut descriptor_ids = HashSet::new();

        for (service_index, service) in self.services.iter().enumerate() {
            if !service_ids.insert(service.id) {
                return Err(format!(
                    "services[{service_index}].id duplicates another GATT service ID"
                ));
            }
            if !service.uuid.is_canonical() {
                return Err(format!(
                    "services[{service_index}].uuid is not a canonical Bluetooth UUID"
                ));
            }
            for (characteristic_index, characteristic) in service.characteristics.iter().enumerate()
            {
                if !characteristic_ids.insert(characteristic.id) {
                    return Err(format!(
                        "services[{service_index}].characteristics[{characteristic_index}].id duplicates another GATT characteristic ID"
                    ));
                }
                if !characteristic.uuid.is_canonical() {
                    return Err(format!(
                        "services[{service_index}].characteristics[{characteristic_index}].uuid is not a canonical Bluetooth UUID"
                    ));
                }
                for (descriptor_index, descriptor) in characteristic.descriptors.iter().enumerate()
                {
                    if !descriptor_ids.insert(descriptor.id) {
                        return Err(format!(
                            "services[{service_index}].characteristics[{characteristic_index}].descriptors[{descriptor_index}].id duplicates another GATT descriptor ID"
                        ));
                    }
                    if !descriptor.uuid.is_canonical() {
                        return Err(format!(
                            "services[{service_index}].characteristics[{characteristic_index}].descriptors[{descriptor_index}].uuid is not a canonical Bluetooth UUID"
                        ));
                    }
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct GattService {
    pub id: GattServiceId,
    pub uuid: BluetoothUuid,
    pub primary: Option<bool>,
    pub characteristics: Vec<GattCharacteristic>,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct GattCharacteristic {
    pub id: GattCharacteristicId,
    pub uuid: BluetoothUuid,
    pub properties: CharacteristicProperties,
    pub descriptors: Vec<GattDescriptor>,
}

#[derive(Clone, Debug, PartialEq, Eq, uniffi::Record)]
pub struct GattDescriptor {
    pub id: GattDescriptorId,
    pub uuid: BluetoothUuid,
}
