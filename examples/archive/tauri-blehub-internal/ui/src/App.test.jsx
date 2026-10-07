import { beforeEach, expect, test } from "vitest";
import { render } from "vitest-browser-react";
import { Theme } from "@astryxdesign/core";
import { neutralTheme } from "@astryxdesign/theme-neutral";
import App from "./App.jsx";
import { installTauriMock } from "./tauriMock.js";

function renderApp() {
  return render(<Theme theme={neutralTheme}><App /></Theme>);
}

beforeEach(() => { delete window.__TAURI__; });

const calls = (mock, name) => mock.commands.filter(({ command }) => command === `plugin:blehub|${name}`);

test("runs demo through initialize and ordinary commands with real mock readback", async () => {
  const mock = installTauriMock();
  const screen = await renderApp();
  await expect.element(screen.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  await screen.getByRole("button", { name: "Run basic flow" }).click();
  await expect.element(screen.getByText("basic flow passed", { exact: true })).toBeVisible();
  await expect.element(screen.getByText("12/12")).toBeVisible();
  await expect.element(screen.getByText("notification_received", { exact: true })).toBeVisible();
  expect(mock.commands[1].command).toBe("plugin:blehub|initialize");
  expect(calls(mock, "read_characteristic")).toHaveLength(3);
  expect(calls(mock, "disconnect")).toHaveLength(1);
  expect(calls(mock, "initialize")[0].args.commandId).toMatch(/^\d+$/);
  expect(calls(mock, "read_characteristic")[0].args.request.characteristic_id).toBe("18446744073709551611");
});

test("keeps actions disabled until listener registration and capabilities finish", async () => {
  const mock = installTauriMock({ holdListener: true });
  const screen = await renderApp();
  await expect.element(screen.getByRole("button", { name: "Run basic flow" })).toBeDisabled();
  await expect.element(screen.getByRole("button", { name: "Dispose hub" })).toBeDisabled();
  expect(mock.commands).toHaveLength(0);
  mock.releaseListener();
  await expect.element(screen.getByRole("button", { name: "Run basic flow" })).toBeEnabled();
});

test("Apple snapshot alone is not ready; terminal completes initialization", async () => {
  const mock = installTauriMock({ platform: "apple", holdTerminalFor: "initialize" });
  const screen = await renderApp();
  await screen.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await expect.element(screen.getByRole("button", { name: "Running..." })).toBeDisabled();
  await expect.element(screen.getByText("adapter_snapshot_changed", { exact: true })).toBeVisible();
  await expect.element(screen.getByText("CoreBluetooth ready", { exact: true })).not.toBeInTheDocument();
  mock.releaseTerminals();
  await expect.element(screen.getByText("CoreBluetooth ready", { exact: true })).toBeVisible();
  await screen.getByRole("button", { name: "Run CoreBluetooth flow" }).click();
  await expect.element(screen.getByText("CoreBluetooth flow passed", { exact: true })).toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Run CoreBluetooth flow again" })).toBeEnabled();
  expect(calls(mock, "initialize")).toHaveLength(1);
});

for (const options of [
  { failCommand: "initialize" },
  { failCommand: "connect" },
  { domainError: "connect" },
  { rejectCommand: "start_scan" },
  { brokenReadback: true },
]) {
  test(`fails visibly without continuing: ${JSON.stringify(options)}`, async () => {
    const mock = installTauriMock(options);
    const screen = await renderApp();
    await screen.getByRole("button", { name: "Run basic flow" }).click();
    await expect.element(screen.getByText(/^flow failed:/)).toBeVisible();
    await expect.element(screen.getByText("basic flow passed", { exact: true })).not.toBeInTheDocument();
    await expect.element(screen.getByRole("button", { name: "Run basic flow" })).toBeDisabled();
    expect(calls(mock, "set_notifications")).toHaveLength(0);
    await screen.getByRole("button", { name: "Dispose hub" }).click();
    await expect.element(screen.getByText("disposed", { exact: true })).toBeVisible();
  });
}

test("dispose cancels a pending initialize and late terminal cannot make it ready", async () => {
  const mock = installTauriMock({ platform: "apple", holdTerminalFor: "initialize" });
  const screen = await renderApp();
  await screen.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await screen.getByRole("button", { name: "Dispose hub" }).click();
  mock.releaseTerminals();
  await expect.element(screen.getByText("disposed", { exact: true })).toBeVisible();
  await expect.element(screen.getByText("CoreBluetooth ready", { exact: true })).not.toBeInTheDocument();
});
