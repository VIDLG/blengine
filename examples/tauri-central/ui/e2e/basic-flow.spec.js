import { expect, test } from "@playwright/test";
import { installTauriMock } from "../src/tauriMock.js";

async function open(page, options = {}) {
  await page.addInitScript(installTauriMock, options);
  await page.goto("/");
}

test("runs the mocked Apple flow headlessly with empty advertised services and isolated rerun IDs", async ({ page }) => {
  await open(page, { platform: "apple", terminalFirst: true });
  await page.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await expect(page.getByText("CoreBluetooth ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run CoreBluetooth flow", exact: true }).click();
  await expect(page.getByText("CoreBluetooth flow passed", { exact: true })).toBeVisible();
  await page.evaluate(() => { window.__BLEHUB_OLD_EVENTS__ = [...window.__BLEHUB_TEST__.emitted]; });
  await page.getByRole("button", { name: "Run CoreBluetooth flow again" }).click();
  await page.evaluate(() => window.__BLEHUB_OLD_EVENTS__.forEach(window.__BLEHUB_TEST__.emit));
  await expect(page.getByText("CoreBluetooth flow passed", { exact: true })).toBeVisible();
  await expect(page.getByText("12/12")).toBeVisible();
  const commands = await page.evaluate(() => window.__BLEHUB_TEST__.commands);
  const requests = commands.filter(({ args }) => args?.request).map(({ args }) => args.request);
  const commandIds = requests.map((request) => request.command_id);
  expect(new Set(commandIds).size).toBe(commandIds.length);
  expect(commands.filter(({ command }) => command.endsWith("|initialize"))).toHaveLength(1);
  expect(commands.filter(({ command }) => command.endsWith("|disconnect"))).toHaveLength(2);
  const scans = commands.filter(({ command }) => command.endsWith("|start_scan"));
  expect(scans[0].args.request.scan_session_id).not.toBe(scans[1].args.request.scan_session_id);
  for (const request of requests) {
    for (const [key, value] of Object.entries(request)) if (key.endsWith("_id") && key !== "company_id") expect(value).toMatch(/^\d+$/);
  }
  // Expand preserved wire data rather than deriving state from display-only detail.
  await page.getByRole("button", { name: /advertisement_observed/ }).first().click();
  const advertisement = page.locator("pre").filter({ hasText: '"kind": "advertisement_observed"' }).first();
  await expect(advertisement).toBeVisible();
  await expect(advertisement).toContainText('"peripheral_id": "18446744073709551610"');
  await expect(advertisement).toContainText('"manufacturer_data"');
  await page.getByRole("button", { name: /gatt_discovered/ }).first().click();
  await expect(page.locator("pre").filter({ hasText: '"descriptors"' }).first()).toBeVisible();
});

test("listener and terminal gate visible actions", async ({ page }) => {
  await open(page, { platform: "apple", holdListener: true, holdTerminalFor: "initialize" });
  await expect(page.getByRole("button", { name: "Run basic flow" })).toBeDisabled();
  await page.evaluate(() => window.__BLEHUB_TEST__.releaseListener());
  await page.getByRole("button", { name: "Initialize CoreBluetooth" }).click();
  await expect(page.getByText("adapter_snapshot_changed", { exact: true })).toBeVisible();
  await expect(page.getByText("CoreBluetooth ready", { exact: true })).toHaveCount(0);
  await page.evaluate(() => window.__BLEHUB_TEST__.releaseTerminals());
  await expect(page.getByRole("button", { name: "Run CoreBluetooth flow", exact: true })).toBeEnabled();
});

for (const options of [{ failCommand: "initialize" }, { failCommand: "connect" }, { domainError: "connect" }]) {
  test(`stops at structured failure ${JSON.stringify(options)}`, async ({ page }) => {
    await open(page, options);
    await page.getByRole("button", { name: "Run basic flow" }).click();
    await expect(page.getByText(/^flow failed:/)).toBeVisible();
    await expect(page.getByText("basic flow passed", { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => window.__BLEHUB_TEST__.commands.some(({ command }) => command.endsWith("|discover_gatt")))).toBe(false);
  });
}

test("runs the basic flow and renders projected events", async ({ page }) => {
  await open(page);
  await expect(page.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  await page.getByRole("button", { name: "Run basic flow" }).click();
  await expect(page.getByText("basic flow passed", { exact: true })).toBeVisible();
  await expect(page.getByText("12/12")).toBeVisible();
  await expect(page.getByText("notification_received", { exact: true })).toBeVisible();
  const commands = await page.evaluate(() => window.__BLEHUB_TEST__.commands.map(({ command }) => command.replace("plugin:blehub|", "")));
  expect(commands).toEqual([
    "capabilities", "initialize", "start_scan", "stop_scan", "connect", "discover_gatt", "read_characteristic",
    "write_characteristic", "read_characteristic", "write_characteristic", "read_characteristic",
    "set_notifications", "read_rssi", "set_notifications", "disconnect",
  ]);
  await expect(page.locator("main")).toHaveScreenshot("basic-flow.png");
});

test("keeps the surface usable on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  await expect(page.getByRole("heading", { name: "Central contract lab" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run basic flow" })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.locator("main")).toHaveScreenshot("basic-flow-mobile.png");
});
