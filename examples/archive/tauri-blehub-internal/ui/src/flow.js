// Example-owned orchestration, not a BleHub runtime or a conformance harness.
import { CHARACTERISTIC_UUID, NOTIFICATION_UUID, SERVICE_UUID } from "./fixtureProfile.js";

export { CHARACTERISTIC_UUID, NOTIFICATION_UUID, SERVICE_UUID } from "./fixtureProfile.js";

export const initialSteps = [
  "initialize", "scan_start", "scan_stop", "connect", "gatt_discovery", "read",
  "write_with_response", "write_without_response", "notifications_enable", "rssi",
  "notifications_disable", "disconnect",
];

const CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb";

// Shared across component remounts; never round a wire u64 through a JS Number.
let nextId = 1n;
export function allocateId() {
  if (nextId > 18446744073709551615n) throw new Error("example ID space exhausted; restart the host");
  return String(nextId++);
}

export function errorMessage(error) {
  if (error && typeof error === "object" && "category" in error) {
    return `${error.category}: ${error.message}${error.platform_details == null ? "" : ` (${JSON.stringify(error.platform_details)})`}`;
  }
  return String(error);
}

export function adapterReady(data) {
  const authorized = (value) => value === "authorized" || value === "not_required";
  return data?.state === "powered_on"
    && authorized(data.scan_authorization) && authorized(data.connection_authorization);
}

const matches = (data, scope) => Object.entries(scope).every(([key, value]) => data?.[key] === value);
const domain = (kind, scope = {}, state) => (event) => event.kind === kind
  && matches(event.data, scope) && (state === undefined || event.data?.state === state);

export function createCommandRunner(api, { timeout = 30_000, onCommand = () => {} } = {}) {
  let pending;
  function cancel(reason = new Error("event listener disposed")) {
    pending?.reject(reason);
  }
  function receive(event) {
    pending?.receive(event);
  }
  async function command(name, request = {}, predicates = [], { scope = {}, initialize = false, evidenceNames = [] } = {}) {
    if (pending) throw new Error("an example command is already running");
    onCommand(name);
    const commandId = allocateId();
    let timer;
    let waiter;
    const events = new Promise((resolve, reject) => {
      let terminal;
      let snapshot;
      const found = new Map();
      const finish = (error) => {
        clearTimeout(timer);
        if (pending === waiter) pending = undefined;
        if (error) reject(error);
        else resolve([terminal, ...(initialize ? [snapshot] : [...found.values()])]);
      };
      waiter = {
        reject: (error) => finish(error),
        receive: (event) => {
          const ownCommand = event.command_id === commandId;
          const unsolicited = event.command_id == null && (
            (initialize && event.kind === "adapter_snapshot_changed")
            || (Object.keys(scope).length > 0 && matches(event.data, scope))
          );
          if (!ownCommand && !unsolicited) return;
          // A domain error is never evidence of success, even if its state looks successful.
          if (event.error != null) {
            finish(new Error(`${name}: ${errorMessage(event.error)}`));
            return;
          }
          if (ownCommand && event.kind === "command_completed") terminal ??= event;
          if (initialize && event.kind === "adapter_snapshot_changed") snapshot = event;
          predicates.forEach((predicate, index) => {
            if (!found.has(index) && predicate(event)) found.set(index, event);
          });
          if (terminal && (initialize ? adapterReady(snapshot?.data) : found.size === predicates.length)) finish();
        },
      };
      pending = waiter;
      timer = setTimeout(() => {
        const missing = [];
        if (!terminal) missing.push("successful command_completed");
        if (initialize) {
          if (!adapterReady(snapshot?.data)) missing.push("powered_on + authorized adapter snapshot");
        } else {
          predicates.forEach((_, index) => {
            if (!found.has(index)) missing.push(evidenceNames[index] ?? `domain predicate ${index + 1}`);
          });
        }
        finish(new Error(`${name}: timed out; command_id=${commandId}; scope=${JSON.stringify(scope)}; missing: ${missing.join(", ")}`));
      }, timeout);
    });
    try {
      // Register first: a demo backend may emit synchronously inside invoke.
      const args = initialize ? { commandId } : { request: { ...request, command_id: commandId } };
      const [, result] = await Promise.all([Promise.resolve().then(() => api.invoke(`plugin:blehub|${name}`, args)), events]);
      return result;
    } catch (error) {
      waiter.reject(error);
      throw error;
    }
  }
  return { command, receive, cancel };
}

export function initializeAdapter(runner) {
  return runner.command("initialize", {}, [], { initialize: true });
}

const sameValue = (actual, expected) => Array.isArray(actual)
  && actual.length === expected.length && actual.every((byte, index) => byte === expected[index]);

export async function runFlow(runner, capabilities, initialization, mark, { readbackDelay = 250 } = {}) {
  mark("initialize", initialization);
  const scan = { scan_session_id: allocateId() };
  const connection = { connection_id: allocateId() };
  const gatt = { ...connection, gatt_session_id: allocateId() };
  const subscriptionId = allocateId();
  const step = (name, request, predicates, scope, evidenceNames) => runner.command(name, request, predicates, { scope, evidenceNames });

  // CoreBluetooth can hide service UUIDs from the advertisement even when the native
  // service-filtered scan matched. Validate the actual profile during GATT discovery.
  const scanned = await step("start_scan", {
    ...scan, service_uuids: [SERVICE_UUID], name_prefix: null, company_id: null,
    allow_duplicates: capabilities.supports_duplicate_scan_results,
  }, [domain("scan_started", scan), (event) => domain("advertisement_observed", scan)(event)
    && typeof event.data.peripheral_id === "string" && event.data.connectable !== false], scan,
      ["scan_started", "matching fixture advertisement_observed"]);
  const advertisement = scanned.find((event) => event.kind === "advertisement_observed");
  mark("scan_start", scanned);
  mark("scan_stop", await step("stop_scan", scan, [domain("scan_stopped", scan)], scan));
  mark("connect", await step("connect", {
    ...connection, peripheral_id: advertisement.data.peripheral_id,
  }, [domain("connection_state_changed", connection, "connected")], connection));

  const discovered = await step("discover_gatt", gatt, [domain("gatt_discovered", gatt)], connection);
  const database = discovered.find((event) => event.kind === "gatt_discovered").data;
  const service = database.services?.find((candidate) => candidate.uuid?.toLowerCase() === SERVICE_UUID);
  const characteristic = service?.characteristics?.find((candidate) => candidate.uuid?.toLowerCase() === CHARACTERISTIC_UUID);
  const notification = service?.characteristics?.find((candidate) => candidate.uuid?.toLowerCase() === NOTIFICATION_UUID);
  if (!characteristic?.readable || !characteristic.writable_with_response || !characteristic.writable_without_response) {
    throw new Error("fixture read/write profile was not present in GATT database");
  }
  if (!notification?.notifiable || !notification.descriptors?.some((descriptor) => descriptor.uuid?.toLowerCase() === CCCD_UUID)) {
    throw new Error("fixture notification profile/CCCD was not present in GATT database");
  }
  mark("gatt_discovery", discovered);
  const io = { ...gatt, characteristic_id: characteristic.id };
  const read = () => step("read_characteristic", io, [domain("characteristic_read", io)], connection);
  mark("read", await read());
  // The active nRF52840 profile exposes one-byte value buffers. Keep this
  // internal RF flow aligned with the canonical profile rather than the
  // archived Android fixture's multi-byte payloads.
  for (const [writeType, value] of [["with_response", [0x10]], ["without_response", [0xab]]]) {
    const written = await step("write_characteristic", { ...io, value, write_type: writeType },
      writeType === "with_response" ? [domain("characteristic_written", io)] : [], connection);
    // Without-response completion is local submission only. Readback is separate
    // fixture-application evidence, after a caller-owned settling interval.
    if (writeType === "without_response") await new Promise((resolve) => setTimeout(resolve, readbackDelay));
    const readback = await read();
    if (!sameValue(readback.find((event) => event.kind === "characteristic_read").data.value, value)) {
      throw new Error(`${writeType}: fixture readback did not match written value`);
    }
    mark(`write_${writeType}`, [...written, ...readback]);
  }
  const subscription = { ...gatt, subscription_id: subscriptionId, characteristic_id: notification.id };
  const enabled = await step("set_notifications", { ...subscription, action: "enable", mode: "platform_default" }, [
    domain("notification_state_changed", subscription, "enabled"),
    domain("notification_received", subscription),
  ], connection);
  const state = enabled.find((event) => event.kind === "notification_state_changed").data;
  if (capabilities.reports_actual_notification_mode && !["notification", "indication"].includes(state.actual_mode)) {
    throw new Error("notification actual_mode was not reported");
  }
  mark("notifications_enable", enabled);
  if (capabilities.supports_read_rssi) {
    mark("rssi", await step("read_rssi", connection, [domain("rssi_read", connection)], connection));
  } else {
    mark("rssi", [], "skipped: capability unavailable");
  }
  mark("notifications_disable", await step("set_notifications", { ...subscription, action: "disable", mode: null }, [
    domain("notification_state_changed", subscription, "disabled"),
  ], connection));
  mark("disconnect", await step("disconnect", connection, [domain("connection_state_changed", connection, "disconnected")], connection));
}
