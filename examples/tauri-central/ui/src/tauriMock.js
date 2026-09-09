// Browser-only IPC double. Every command emits domain events and its own terminal;
// invoke resolving is dispatch acceptance, never a precomputed flow result.
// Kept self-contained so Playwright can install the same schema via addInitScript.
export function installTauriMock(options = {}) {
  const listeners = new Set();
  const commands = [];
  const emitted = [];
  const terminals = [];
  const listenerResolvers = [];
  let value = [0];
  const platform = options.platform ?? "test";
  const serviceUuid = "fff00000-0000-4000-8000-000000000000";
  const rwId = "18446744073709551611";
  const notifyId = "18446744073709551612";
  const error = { category: "gatt", message: "mock terminal failure", platform_details: { platform, code: "E_TEST", name: null, message: null } };
  const emit = (event) => {
    const payload = { command_id: null, detail: null, data: null, error: null, ...event };
    emitted.push(payload);
    for (const callback of listeners) callback({ payload });
  };
  const api = {
    listen: async (_name, callback) => {
      if (options.holdListener) await new Promise((resolve) => listenerResolvers.push(resolve));
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
    invoke: async (command, args) => {
      commands.push({ command, args });
      const name = command.replace("plugin:blehub|", "");
      if (name === "capabilities") return {
        platform,
        contract_version: 5,
        supports_duplicate_scan_results: true,
        supports_connectable_flag: true,
        supports_read_rssi: options.supportsReadRssi ?? true,
        supports_required_notification: platform !== "apple",
        supports_required_indication: platform !== "apple",
        reports_actual_notification_mode: true,
      };
      if (name === "close") return;
      const request = args?.request ?? {};
      const commandId = args?.commandId ?? request.command_id;
      if (typeof commandId !== "string" || !/^\d+$/.test(commandId)) throw new Error("command ID must be a decimal string");
      for (const [key, id] of Object.entries(request)) {
        if (key.endsWith("_id") && key !== "company_id" && (typeof id !== "string" || !/^\d+$/.test(id))) throw new Error(`${key} must be a decimal string`);
      }
      if (options.rejectCommand === name) throw new Error("mock dispatch rejected");
      const scope = Object.fromEntries(Object.entries(request).filter(([key]) => key.endsWith("_id") && key !== "command_id" && key !== "company_id"));
      const domainEvents = [];
      const domain = (kind, data, id = commandId) => domainEvents.push({ kind, command_id: id, data });
      switch (name) {
        case "initialize":
          domain("adapter_snapshot_changed", {
            state: options.adapterState ?? "powered_on",
            scan_authorization: options.authorization ?? "authorized",
            connection_authorization: options.authorization ?? "not_required",
            name: "mock adapter",
          });
          break;
        case "start_scan":
          domain("scan_started", scope);
          domain("advertisement_observed", {
            ...scope, peripheral_id: "18446744073709551610", local_name: "BleHub mock fixture",
            advertised_services: platform === "apple" ? [] : [serviceUuid],
            manufacturer_data: [{ company_id: 65535, payload: [1, 2, 3] }],
            service_data: [{ service_uuid: serviceUuid, payload: [4, 5] }], rssi: -50, connectable: true,
          }, null);
          break;
        case "stop_scan": domain("scan_stopped", { ...scope, reason: "requested" }); break;
        case "connect":
          domain("connection_state_changed", { ...scope, state: "connecting" });
          domain("connection_state_changed", { ...scope, state: "connected" });
          break;
        case "discover_gatt":
          domain("gatt_discovered", {
            ...scope,
            services: [{
              id: "18446744073709551613", uuid: serviceUuid, primary: true,
              characteristics: [{
                id: rwId, uuid: "fff10000-0000-4000-8000-000000000000", readable: true,
                writable_with_response: true, writable_without_response: true,
                notifiable: false, indicatable: false, descriptors: [],
              }, {
                id: notifyId, uuid: "fff20000-0000-4000-8000-000000000000", readable: true,
                writable_with_response: false, writable_without_response: false,
                notifiable: true, indicatable: true,
                descriptors: [{ id: "18446744073709551614", uuid: "00002902-0000-1000-8000-00805f9b34fb" }],
              }],
            }],
          });
          break;
        case "read_characteristic": domain("characteristic_read", { ...scope, value: [...value] }); break;
        case "write_characteristic":
          if (!options.brokenReadback) value = [...request.value];
          domain("characteristic_written", scope);
          break;
        case "set_notifications": {
          const enabled = request.action === "enable";
          domain("notification_state_changed", { ...scope, state: enabled ? "enabled" : "disabled", actual_mode: enabled ? "notification" : null });
          if (enabled) domain("notification_received", { ...scope, value: [0xde, 0xad, 0xbe, 0xef] }, null);
          break;
        }
        case "read_rssi": domain("rssi_read", { ...scope, rssi: -50 }); break;
        case "disconnect": domain("connection_state_changed", { ...scope, state: "disconnected" }); break;
        default: throw new Error(`unexpected mock command: ${name}`);
      }
      const terminal = {
        kind: "command_completed", command_id: commandId,
        // Deliberately misleading display text exercises detail-independent decisions.
        detail: options.terminalDetail ?? null,
        error: options.failCommand === name ? error : null,
      };
      const complete = () => options.holdTerminalFor === name ? terminals.push(terminal) : emit(terminal);
      if (options.terminalFirst) complete();
      for (const event of domainEvents) {
        if (options.domainError === name) event.error = { ...error, message: "mock domain failure" };
        emit(event);
      }
      if (!options.terminalFirst) complete();
    },
  };
  window.__TAURI__ = { core: { invoke: api.invoke }, event: { listen: api.listen } };
  const mock = {
    api, commands, emitted, emit,
    releaseListener: () => listenerResolvers.splice(0).forEach((resolve) => resolve()),
    releaseTerminals: () => terminals.splice(0).forEach(emit),
  };
  window.__BLEHUB_TEST__ = mock;
  return mock;
}
