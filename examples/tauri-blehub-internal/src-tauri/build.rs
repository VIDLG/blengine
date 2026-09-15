use std::{env, fs, path::PathBuf};

fn role_uuid<'a>(service: &'a serde_json::Value, role: &str) -> &'a str {
    service["characteristics"]
        .as_array()
        .expect("nRF scenario characteristics must be an array")
        .iter()
        .find(|characteristic| characteristic["role"] == role)
        .and_then(|characteristic| characteristic["uuid"].as_str())
        .expect("nRF scenario characteristic role is missing")
}

fn hex_bytes(value: &str) -> String {
    let bytes = (0..value.len())
        .step_by(2)
        .map(|index| {
            u8::from_str_radix(&value[index..index + 2], 16)
                .expect("nRF scenario value must be hexadecimal")
        })
        .map(|byte| format!("0x{byte:02x}"))
        .collect::<Vec<_>>();
    format!("&[{}]", bytes.join(", "))
}

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let profile_path =
        manifest_dir.join("../../../tests/conformance/scenarios/blehub-nrf-indication-v2.json");
    println!("cargo:rerun-if-changed={}", profile_path.display());
    let document: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&profile_path).expect("read active nRF scenario"))
            .expect("parse active nRF scenario");
    let service = &document["services"][0];
    let service_uuid = service["uuid"].as_str().expect("nRF service UUID");
    let read_write_uuid = role_uuid(service, "read-write");
    let notification_uuid = role_uuid(service, "updates");
    let initial_value = service["characteristics"]
        .as_array()
        .expect("nRF scenario characteristics must be an array")
        .iter()
        .find(|characteristic| characteristic["role"] == "read-write")
        .and_then(|characteristic| characteristic["initial_value_hex"].as_str())
        .expect("nRF read-write initial value");
    let notification_value = document["notification_value_hex"]
        .as_str()
        .expect("nRF notification value");
    let generated = format!(
        "pub const FIXTURE_SERVICE_UUID: &str = {service_uuid:?};\n\
pub const FIXTURE_READ_WRITE_UUID: &str = {read_write_uuid:?};\n\
pub const FIXTURE_NOTIFICATION_UUID: &str = {notification_uuid:?};\n\
pub const FIXTURE_INITIAL_VALUE: &[u8] = {};\n\
pub const FIXTURE_NOTIFICATION_VALUE: &[u8] = {};\n",
        hex_bytes(initial_value),
        hex_bytes(notification_value),
    );
    let output = PathBuf::from(env::var_os("OUT_DIR").expect("out dir"));
    fs::write(output.join("fixture_profile.rs"), generated).expect("write generated profile");
    tauri_build::build();
}
