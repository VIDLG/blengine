import { expect, test } from "@playwright/test";

const steps = [
  "initialize", "scan_start", "scan_stop", "connect", "gatt_discovery", "read",
  "write_with_response", "write_without_response", "notifications_enable", "rssi",
  "notifications_disable", "disconnect",
].map((name) => ({ name, event_count: 2 }));

test.beforeEach(async ({ page }) => {
  await page.addInitScript(({ scriptedSteps }) => {
    let eventListener;
    let listenerGeneration = 0;
    window.__TAURI__ = {
      core: {
        invoke: async (command) => {
          if (command === "plugin:blehub|run_basic_probe") {
            for (const payload of [
              { kind: "adapter_snapshot_changed", command_id: 1, detail: "state=PoweredOn" },
              { kind: "gatt_discovered", command_id: 5, detail: "services=2" },
              { kind: "notification_received", command_id: null, detail: "value=DE AD BE EF" },
              { kind: "command_completed", command_id: 12, detail: null },
            ]) eventListener?.callback({ payload });
            return { steps: scriptedSteps };
          }
          return undefined;
        },
      },
      event: {
        listen: async (_name, callback) => {
          const generation = ++listenerGeneration;
          eventListener = { callback, generation };
          return () => {
            if (eventListener?.generation === generation) eventListener = undefined;
          };
        },
      },
    };
  }, { scriptedSteps: steps });
});

test("runs the mocked Apple flow headlessly", async ({ page }) => {
  await page.addInitScript(() => {
    let eventListener;
    let listenerGeneration = 0;
    const commands = [];
    const emit = (payload) => setTimeout(() => eventListener?.callback({ payload }), 0);
    const complete = (commandId) => emit({ kind: "command_completed", command_id: commandId, detail: null });
    const ids = { scanSession: 101, connection: 301, gattSession: 401, subscription: 801 };
    const characteristic = {
      id: 42,
      uuid: "fff10000-0000-4000-8000-000000000000",
      readable: true,
      writable_with_response: true,
      writable_without_response: true,
      notifiable: false,
      indicatable: false,
    };
    const notificationCharacteristic = {
      id: 43,
      uuid: "fff20000-0000-4000-8000-000000000000",
      readable: true,
      writable_with_response: false,
      writable_without_response: false,
      notifiable: true,
      indicatable: true,
    };
    window.__TAURI__ = {
      core: {
        invoke: async (command, args) => {
          commands.push({ command, args });
          if (command === "plugin:blehub|backend_mode") return "apple-corebluetooth";
          if (command === "plugin:blehub|initialize") {
            emit({ kind: "adapter_snapshot_changed", command_id: 1, detail: "state=PoweredOn" });
            complete(1);
          } else if (command === "plugin:blehub|start_scan") {
            emit({
              kind: "scan_started",
              command_id: 2,
              data: { scan_session_id: ids.scanSession },
            });
            emit({
              kind: "advertisement_observed",
              command_id: null,
              data: {
                peripheral_id: 201,
                scan_session_id: ids.scanSession,
                advertised_services: ["fff00000-0000-4000-8000-000000000000"],
              },
            });
            complete(2);
          } else if (command === "plugin:blehub|stop_scan") {
            emit({ kind: "scan_stopped", command_id: null, data: { scan_session_id: ids.scanSession } });
            complete(3);
          } else if (command === "plugin:blehub|connect") {
            emit({ kind: "connection_state_changed", command_id: 4, data: { connection_id: ids.connection, state: "Connecting" } });
            emit({ kind: "connection_state_changed", command_id: 4, data: { connection_id: ids.connection, state: "Connected" } });
            complete(4);
          } else if (command === "plugin:blehub|discover_gatt") {
            emit({
              kind: "gatt_discovered",
              command_id: 5,
              data: {
                connection_id: ids.connection,
                gatt_session_id: ids.gattSession,
                services: [{
                  id: 41,
                  uuid: "fff00000-0000-4000-8000-000000000000",
                  primary: true,
                  characteristics: [characteristic, notificationCharacteristic],
                }],
              },
            });
            complete(5);
          } else if (command === "plugin:blehub|read_characteristic") {
            emit({ kind: "characteristic_read", command_id: 6, data: { characteristic_id: characteristic.id, value: [0] } });
            complete(6);
          } else if (command === "plugin:blehub|write_characteristic") {
            if (args.request.command_id === 7) emit({ kind: "characteristic_written", command_id: 7, data: { characteristic_id: characteristic.id } });
            complete(args.request.command_id);
          } else if (command === "plugin:blehub|set_notifications") {
            const commandId = args.request.command_id;
            const enabled = args.request.action === "enable";
            emit({
              kind: "notification_state_changed",
              command_id: commandId,
              data: { subscription_id: ids.subscription, characteristic_id: notificationCharacteristic.id, state: enabled ? "Enabled" : "Disabled" },
            });
            if (enabled) emit({ kind: "notification_received", command_id: null, data: { subscription_id: ids.subscription, characteristic_id: notificationCharacteristic.id, value: [0xde, 0xad, 0xbe, 0xef] } });
            complete(commandId);
          } else if (command === "plugin:blehub|read_rssi") {
            emit({ kind: "rssi_read", command_id: 10, data: { connection_id: ids.connection, rssi: -50 } });
            complete(10);
          } else if (command === "plugin:blehub|disconnect") {
            emit({ kind: "connection_state_changed", command_id: 12, data: { connection_id: ids.connection, state: "Disconnected" } });
            complete(12);
          }
          return undefined;
        },
      },
      event: {
        listen: async (_name, callback) => {
          const generation = ++listenerGeneration;
          eventListener = { callback, generation };
          return () => {
            if (eventListener?.generation === generation) eventListener = undefined;
          };
        },
      },
    };
    window.__BLEHUB_TEST_COMMANDS__ = commands;
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await expect(page.getByTitle("state=PoweredOn")).toBeVisible();
  await expect(page.getByText("CoreBluetooth ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run CoreBluetooth flow" }).click();
  await expect(page.getByText("CoreBluetooth flow passed")).toBeVisible();
  await expect(page.getByText("12/12")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run CoreBluetooth flow again" })).toBeVisible();
  await page.getByRole("button", { name: "Run CoreBluetooth flow again" }).click();
  await expect(page.getByText("CoreBluetooth flow passed")).toBeVisible();
  await expect(page.getByText("12/12")).toBeVisible();
  await page.getByRole("button", { name: /advertisement_observed/ }).click();
  await expect(page.locator("pre").filter({ hasText: '"peripheral_id": 201' })).toBeVisible();
  const flowCommands = [
    "plugin:blehub|initialize",
    "plugin:blehub|start_scan",
    "plugin:blehub|stop_scan",
    "plugin:blehub|connect",
    "plugin:blehub|discover_gatt",
    "plugin:blehub|read_characteristic",
    "plugin:blehub|write_characteristic",
    "plugin:blehub|write_characteristic",
    "plugin:blehub|set_notifications",
    "plugin:blehub|read_rssi",
    "plugin:blehub|set_notifications",
    "plugin:blehub|disconnect",
  ];
  expect(await page.evaluate(() => window.__BLEHUB_TEST_COMMANDS__
    .map(({ command }) => command)
    .filter((command) => command !== "plugin:blehub|backend_mode"))).toEqual([
    ...flowCommands,
    ...flowCommands.slice(1),
  ]);
});

test("runs the basic flow and renders projected events", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  await page.getByRole("button", { name: "Run basic flow" }).click();
  await expect(page.getByText("basic flow passed")).toBeVisible();
  await expect(page.getByText("12/12")).toBeVisible();
  await expect(page.getByText("notification_received")).toBeVisible();
  await expect(page.locator("main")).toHaveScreenshot("basic-flow.png");
});

test("keeps the surface usable on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator("main")).toHaveScreenshot("basic-flow-mobile.png");
});
