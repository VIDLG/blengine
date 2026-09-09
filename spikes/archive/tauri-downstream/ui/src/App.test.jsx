import { beforeEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";
import { Theme } from "@astryxdesign/core";
import { neutralTheme } from "@astryxdesign/theme-neutral";
import App from "./App.jsx";

const steps = [
  "initialize", "scan_start", "scan_stop", "connect", "gatt_discovery", "read",
  "write_with_response", "write_without_response", "notifications_enable", "rssi",
  "notifications_disable", "disconnect",
].map((name) => ({ name, event_count: 2 }));

function installTauriMock({ reject = false, backendMode } = {}) {
  let listener;
  let listenerGeneration = 0;
  const invoke = vi.fn(async (command) => {
    if (reject) throw new Error("probe failed");
    if (command === "plugin:blehub|backend_mode") return backendMode;
    if (command === "plugin:blehub|run_basic_probe") {
      for (const event of [
        { kind: "adapter_snapshot_changed", command_id: 1, detail: "state=PoweredOn" },
        { kind: "gatt_discovered", command_id: 5, detail: "services=2" },
        { kind: "notification_received", command_id: null, detail: "value=DE AD BE EF" },
        { kind: "command_completed", command_id: 12, detail: null },
      ]) listener?.callback({ payload: event });
      return { steps };
    }
    return undefined;
  });
  const listen = vi.fn(async (_name, callback) => {
    const generation = ++listenerGeneration;
    listener = { callback, generation };
    return () => {
      if (listener?.generation === generation) listener = undefined;
    };
  });
  window.__TAURI__ = { core: { invoke }, event: { listen } };
  return { invoke, listen };
}

function renderApp() {
  return render(
    <Theme theme={neutralTheme}>
      <App />
    </Theme>,
  );
}

beforeEach(() => {
  delete window.__TAURI__;
});

test("renders Astryx surface and runs the complete basic flow", async () => {
  const mock = installTauriMock();
  const screen = await renderApp();

  await expect.element(screen.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  await screen.getByRole("button", { name: "Run basic flow" }).click();
  await expect.element(screen.getByText("basic flow passed")).toBeVisible();
  await expect.element(screen.getByText("12/12")).toBeVisible();
  await expect.element(screen.getByText("notification_received")).toBeVisible();
  expect(mock.invoke).toHaveBeenCalledWith("plugin:blehub|run_basic_probe");
});

test("uses the real Apple initialization command for the Apple backend path", async () => {
  const mock = installTauriMock({ backendMode: "apple-corebluetooth" });
  const screen = await renderApp();

  await expect.element(screen.getByRole("button", { name: "Initialize CoreBluetooth" })).toBeVisible();
  await screen.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await expect.element(screen.getByText("initialize dispatched; waiting for adapter state")).toBeVisible();
  expect(mock.invoke).toHaveBeenCalledWith("plugin:blehub|initialize", { commandId: 1 });
  expect(mock.invoke).not.toHaveBeenCalledWith("plugin:blehub|run_basic_probe");
});

test("renders a failed command without losing the visible control surface", async () => {
  installTauriMock({ reject: true });
  const screen = await renderApp();

  await screen.getByRole("button", { name: "Run basic flow" }).click();
  await expect.element(screen.getByText("failed: Error: probe failed")).toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Dispose hub" })).toBeVisible();
});
