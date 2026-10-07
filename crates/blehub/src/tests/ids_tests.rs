use super::*;

#[test]
fn canonicalizes_supported_bluetooth_uuid_forms() {
    let expected = Some(BluetoothUuid {
        value: "0000180d-0000-1000-8000-00805f9b34fb".to_owned(),
    });
    for value in [
        "180D",
        "0000180d",
        "0000180D00001000800000805F9B34FB",
        "0000180D-0000-1000-8000-00805F9B34FB",
    ] {
        assert_eq!(BluetoothUuid::canonicalize(value), expected);
    }
    for value in ["", "0x180D", "180", "not-a-uuid"] {
        assert_eq!(BluetoothUuid::canonicalize(value), None);
    }
}
