use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState, BluetoothUuid,
    CharacteristicProperties, ManufacturerData, PeripheralId, ScanSessionId, ServiceData,
};
use uuid::Uuid;
use windows::{
    Devices::{
        Bluetooth::{
            Advertisement::BluetoothLEAdvertisementReceivedEventArgs,
            GenericAttributeProfile::GattCharacteristicProperties,
        },
        Radios::RadioState,
    },
    Storage::Streams::{DataReader, IBuffer},
    core::{GUID, Result},
};

const SERVICE_DATA_16_BIT: u8 = 0x16;
const SERVICE_DATA_32_BIT: u8 = 0x20;
const SERVICE_DATA_128_BIT: u8 = 0x21;

pub(crate) fn map_radio_state(state: RadioState) -> AdapterState {
    match state {
        RadioState::On => AdapterState::PoweredOn,
        RadioState::Off | RadioState::Disabled => AdapterState::PoweredOff,
        _ => AdapterState::Unknown,
    }
}

pub(crate) fn map_adapter_snapshot(state: RadioState, name: Option<String>) -> AdapterSnapshot {
    AdapterSnapshot {
        state: map_radio_state(state),
        scan_authorization: AuthorizationState::NotRequired,
        connection_authorization: AuthorizationState::NotRequired,
        name,
    }
}

pub(crate) fn map_guid(guid: GUID) -> BluetoothUuid {
    BluetoothUuid {
        value: Uuid::from_u128(guid.to_u128()).hyphenated().to_string(),
    }
}

pub(crate) fn map_characteristic_properties(
    properties: GattCharacteristicProperties,
) -> CharacteristicProperties {
    CharacteristicProperties {
        readable: properties.contains(GattCharacteristicProperties::Read),
        writable_with_response: properties.contains(GattCharacteristicProperties::Write),
        writable_without_response: properties
            .contains(GattCharacteristicProperties::WriteWithoutResponse),
        notifiable: properties.contains(GattCharacteristicProperties::Notify),
        indicatable: properties.contains(GattCharacteristicProperties::Indicate),
    }
}

pub(crate) fn read_buffer(buffer: &IBuffer) -> Result<Vec<u8>> {
    let reader = DataReader::FromBuffer(buffer)?;
    let mut bytes = vec![0; reader.UnconsumedBufferLength()? as usize];
    reader.ReadBytes(&mut bytes)?;
    Ok(bytes)
}

pub(crate) fn parse_service_data(data_type: u8, data: &[u8]) -> Option<ServiceData> {
    let (service_uuid, payload) = match data_type {
        SERVICE_DATA_16_BIT if data.len() >= 2 => {
            let value = u16::from_le_bytes(data[..2].try_into().ok()?);
            (
                BluetoothUuid::canonicalize(&format!("{value:04x}"))?,
                &data[2..],
            )
        }
        SERVICE_DATA_32_BIT if data.len() >= 4 => {
            let value = u32::from_le_bytes(data[..4].try_into().ok()?);
            (
                BluetoothUuid::canonicalize(&format!("{value:08x}"))?,
                &data[4..],
            )
        }
        SERVICE_DATA_128_BIT if data.len() >= 16 => {
            let value = u128::from_le_bytes(data[..16].try_into().ok()?);
            (map_guid(GUID::from_u128(value)), &data[16..])
        }
        _ => return None,
    };

    Some(ServiceData {
        service_uuid,
        payload: payload.to_vec(),
    })
}

pub(crate) fn map_advertisement_observation(
    args: &BluetoothLEAdvertisementReceivedEventArgs,
    scan_session_id: ScanSessionId,
    peripheral_id: PeripheralId,
) -> Result<AdvertisementObservation> {
    let advertisement = args.Advertisement()?;

    let local_name = advertisement.LocalName()?.to_string();
    let local_name = (!local_name.is_empty()).then_some(local_name);

    let service_uuids = advertisement.ServiceUuids()?;
    let service_uuid_count = service_uuids.Size()?;
    let mut advertised_services = Vec::with_capacity(service_uuid_count as usize);
    for index in 0..service_uuid_count {
        advertised_services.push(map_guid(service_uuids.GetAt(index)?));
    }

    let winrt_manufacturer_data = advertisement.ManufacturerData()?;
    let manufacturer_data_count = winrt_manufacturer_data.Size()?;
    let mut manufacturer_data = Vec::with_capacity(manufacturer_data_count as usize);
    for index in 0..manufacturer_data_count {
        let data = winrt_manufacturer_data.GetAt(index)?;
        manufacturer_data.push(ManufacturerData {
            company_id: data.CompanyId()?,
            payload: read_buffer(&data.Data()?)?,
        });
    }

    let data_sections = advertisement.DataSections()?;
    let data_section_count = data_sections.Size()?;
    let mut service_data = Vec::new();
    for index in 0..data_section_count {
        let section = data_sections.GetAt(index)?;
        let data_type = section.DataType()?;
        if matches!(
            data_type,
            SERVICE_DATA_16_BIT | SERVICE_DATA_32_BIT | SERVICE_DATA_128_BIT
        ) {
            let bytes = read_buffer(&section.Data()?)?;
            if let Some(data) = parse_service_data(data_type, &bytes) {
                service_data.push(data);
            }
        }
    }

    Ok(AdvertisementObservation {
        scan_session_id,
        peripheral_id,
        local_name,
        advertised_services,
        manufacturer_data,
        service_data,
        rssi: Some(args.RawSignalStrengthInDBm()?),
        connectable: Some(args.IsConnectable()?),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn uuid(value: &str) -> BluetoothUuid {
        BluetoothUuid {
            value: value.to_owned(),
        }
    }

    #[test]
    fn maps_windows_radio_states() {
        assert_eq!(map_radio_state(RadioState::Unknown), AdapterState::Unknown);
        assert_eq!(map_radio_state(RadioState::On), AdapterState::PoweredOn);
        assert_eq!(map_radio_state(RadioState::Off), AdapterState::PoweredOff);
        assert_eq!(
            map_radio_state(RadioState::Disabled),
            AdapterState::PoweredOff
        );
    }

    #[test]
    fn canonicalizes_windows_guid() {
        assert_eq!(
            map_guid(GUID::from_u128(0x00112233_4455_6677_8899_aabbccddeeff)),
            uuid("00112233-4455-6677-8899-aabbccddeeff")
        );
    }

    #[test]
    fn maps_combined_fixture_characteristic_properties() {
        assert_eq!(
            map_characteristic_properties(
                GattCharacteristicProperties::Read
                    | GattCharacteristicProperties::Write
                    | GattCharacteristicProperties::WriteWithoutResponse,
            ),
            CharacteristicProperties {
                readable: true,
                writable_with_response: true,
                writable_without_response: true,
                notifiable: false,
                indicatable: false,
            }
        );
        assert_eq!(
            map_characteristic_properties(
                GattCharacteristicProperties::Read
                    | GattCharacteristicProperties::Notify
                    | GattCharacteristicProperties::Indicate,
            ),
            CharacteristicProperties {
                readable: true,
                writable_with_response: false,
                writable_without_response: false,
                notifiable: true,
                indicatable: true,
            }
        );
    }

    #[test]
    fn ignores_empty_and_unknown_characteristic_property_bits() {
        let expected = CharacteristicProperties {
            readable: false,
            writable_with_response: false,
            writable_without_response: false,
            notifiable: false,
            indicatable: false,
        };

        assert_eq!(
            map_characteristic_properties(GattCharacteristicProperties::None),
            expected
        );
        assert_eq!(
            map_characteristic_properties(GattCharacteristicProperties(1 << 31)),
            expected
        );
    }

    #[test]
    fn parses_16_bit_service_data() {
        assert_eq!(
            parse_service_data(SERVICE_DATA_16_BIT, &[0x0d, 0x18, 0xaa, 0xbb]),
            Some(ServiceData {
                service_uuid: uuid("0000180d-0000-1000-8000-00805f9b34fb"),
                payload: vec![0xaa, 0xbb],
            })
        );
    }

    #[test]
    fn parses_32_bit_service_data() {
        assert_eq!(
            parse_service_data(SERVICE_DATA_32_BIT, &[0x78, 0x56, 0x34, 0x12, 0xcc]),
            Some(ServiceData {
                service_uuid: uuid("12345678-0000-1000-8000-00805f9b34fb"),
                payload: vec![0xcc],
            })
        );
    }

    #[test]
    fn parses_128_bit_service_data() {
        let data = [
            0xff, 0xee, 0xdd, 0xcc, 0xbb, 0xaa, 0x99, 0x88, 0x77, 0x66, 0x55, 0x44, 0x33, 0x22,
            0x11, 0x00, 0xdd, 0xee,
        ];
        assert_eq!(
            parse_service_data(SERVICE_DATA_128_BIT, &data),
            Some(ServiceData {
                service_uuid: uuid("00112233-4455-6677-8899-aabbccddeeff"),
                payload: vec![0xdd, 0xee],
            })
        );
    }

    #[test]
    fn ignores_malformed_or_unrelated_service_data() {
        assert_eq!(parse_service_data(SERVICE_DATA_16_BIT, &[0x0d]), None);
        assert_eq!(
            parse_service_data(SERVICE_DATA_32_BIT, &[0x78, 0x56, 0x34]),
            None
        );
        assert_eq!(parse_service_data(SERVICE_DATA_128_BIT, &[0; 15]), None);
        assert_eq!(parse_service_data(0xff, &[0; 16]), None);
    }
}
