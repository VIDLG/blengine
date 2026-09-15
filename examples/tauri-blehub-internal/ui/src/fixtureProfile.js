// The active Tauri internal RF profile is the canonical conformance scenario.
// Keep UUIDs and profile shape out of the UI orchestration itself.
import profile from "../../../../tests/conformance/scenarios/blehub-nrf-indication-v2.json";

const service = profile.services?.length === 1 ? profile.services[0] : null;
const advertisedService = profile.advertising?.service_uuids?.[0];

if (!service || typeof service.uuid !== "string" || service.uuid !== advertisedService) {
  throw new Error("active nRF scenario must define one advertised service");
}

const characteristicForRole = (role) => {
  const characteristic = service.characteristics?.find((candidate) => candidate.role === role);
  if (!characteristic || typeof characteristic.uuid !== "string") {
    throw new Error(`active nRF scenario is missing characteristic role: ${role}`);
  }
  return characteristic.uuid.toLowerCase();
};

export const FIXTURE_PROFILE_ID = profile.profile_id;
export const SERVICE_UUID = service.uuid.toLowerCase();
export const CHARACTERISTIC_UUID = characteristicForRole("read-write");
export const NOTIFICATION_UUID = characteristicForRole("updates");
export const NOTIFICATION_VALUE_HEX = profile.notification_value_hex;
