import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../../..");
const serviceUuid = "fff00000-0000-4000-8000-000000000000";
const readWriteCharacteristicUuid = "fff10000-0000-4000-8000-000000000000";
const notificationCharacteristicUuid = "fff20000-0000-4000-8000-000000000000";
const rf = process.env.BLEHUB_TAURI_RF === "1";
// Native-test commands occupy a separate range from UI-owned commands. Never reuse
// command/session IDs across repeats, soak cycles, cleanup or recovery attempts.
let nextId = 9223372036854775808n;
const id = () => String(nextId++);
let platform;

function runFixture(...args) {
  const result = spawnSync("pixi", ["run", "just", "peripheral", ...args], { cwd: repoRoot, stdio: "inherit", shell: false });
  if (result.status !== 0) throw new Error(`fixture command failed: ${args.join(" ")}`);
}

function runFixtureAllowFailure(...args) {
  return spawnSync("pixi", ["run", "just", "peripheral", ...args], { cwd: repoRoot, stdio: "inherit", shell: false }).status;
}

function runFixtureAsync(...args) {
  return new Promise((resolve, reject) => {
    const child = spawn("pixi", ["run", "just", "peripheral", ...args], { cwd: repoRoot, stdio: "inherit", shell: false });
    child.once("error", reject);
    child.once("exit", (code) => code === 0 ? resolve() : reject(new Error(`fixture command failed: ${args.join(" ")}`)));
  });
}

async function invokeTauri(command, args) {
  const result = await browser.executeAsync((commandName, commandArgs, done) => {
    window.__TAURI__.core.invoke(commandName, commandArgs)
      .then((data) => done({ ok: true, data }))
      .catch((error) => done({ ok: false, error }));
  }, `plugin:blehub|${command}`, args);
  const value = result?.value ?? result;
  if (!value?.ok) throw new Error(`Tauri command ${command} rejected: ${JSON.stringify(value?.error)}`);
  return value.data;
}

async function installEventCollector() {
  const result = await browser.executeAsync((done) => {
    window.__BLEHUB_TAURI_UNSUBSCRIBE__?.();
    window.__BLEHUB_TAURI_EVENTS__ = [];
    window.__TAURI__.event.listen("blehub://event", ({ payload }) => {
      window.__BLEHUB_TAURI_EVENTS__.push(payload);
    }).then((stop) => {
      window.__BLEHUB_TAURI_UNSUBSCRIBE__ = stop;
      done({ ok: true });
    }).catch((error) => done({ ok: false, error }));
  });
  const value = result?.value ?? result;
  if (!value?.ok) throw new Error(`Tauri event collector failed: ${JSON.stringify(value?.error)}`);
}

async function collectedEvents() {
  const result = await browser.execute(() => window.__BLEHUB_TAURI_EVENTS__ ?? []);
  return result?.value ?? result;
}

async function waitForCollectedEvent(predicate, message, { commandId, allowError = false, timeout = 60_000 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const events = await collectedEvents();
    const failure = commandId && events.find((event) => event.command_id === commandId && event.error != null);
    if (failure && !allowError) throw new Error(`${message}: ${JSON.stringify(failure.error)}`);
    const observed = events.find(predicate);
    if (observed) {
      if (observed.error != null && !allowError) throw new Error(`${message}: ${JSON.stringify(observed.error)}`);
      return observed;
    }
    await browser.pause(100);
  }
  throw new Error(`${message}; events=${JSON.stringify(await collectedEvents())}`);
}

async function terminal(commandId, allowError = false) {
  return waitForCollectedEvent((event) => event.kind === "command_completed" && event.command_id === commandId,
    `command ${commandId} did not terminal`, { commandId, allowError });
}

async function command(name, request) {
  const commandId = id();
  await invokeTauri(name, { request: { ...request, command_id: commandId } });
  await terminal(commandId);
  return commandId;
}

const sameScope = (event, scope) => Object.entries(scope).every(([key, value]) => event.data?.[key] === value);
const domain = (kind, scope, state) => (event) => event.kind === kind && sameScope(event, scope)
  && (state === undefined || event.data?.state === state);

async function scan(session) {
  await command("start_scan", {
    scan_session_id: session.scan_session_id, service_uuids: [serviceUuid],
    name_prefix: null, company_id: null, allow_duplicates: true,
  });
  const advertisement = await waitForCollectedEvent(
    domain("advertisement_observed", { scan_session_id: session.scan_session_id }),
    "service-filtered scan did not observe the fixture",
  );
  // Apple may hide advertised UUIDs; the actual service is checked in discovery.
  await command("stop_scan", { scan_session_id: session.scan_session_id });
  return advertisement.data.peripheral_id;
}

async function connect(session, peripheralId) {
  const commandId = await command("connect", { connection_id: session.connection_id, peripheral_id: peripheralId });
  await waitForCollectedEvent(domain("connection_state_changed", { connection_id: session.connection_id }, "connected"),
    "connection did not become connected", { commandId });
}

async function discover(session) {
  const commandId = await command("discover_gatt", { connection_id: session.connection_id, gatt_session_id: session.gatt_session_id });
  const database = await waitForCollectedEvent((event) => event.kind === "gatt_discovered" && event.command_id === commandId,
    "GATT database was not published", { commandId });
  const service = database.data?.services?.find((candidate) => candidate.uuid === serviceUuid);
  if (!service) throw new Error("fixture service was not present in actual GATT database");
  return service.characteristics;
}

async function read(session, characteristic) {
  const commandId = await command("read_characteristic", {
    connection_id: session.connection_id, gatt_session_id: session.gatt_session_id, characteristic_id: characteristic.id,
  });
  return waitForCollectedEvent((event) => event.kind === "characteristic_read" && event.command_id === commandId,
    "characteristic read did not complete", { commandId });
}

async function disconnect(session) {
  const commandId = await command("disconnect", { connection_id: session.connection_id });
  await waitForCollectedEvent((event) => event.command_id === commandId
    && domain("connection_state_changed", { connection_id: session.connection_id }, "disconnected")(event),
  "logical disconnect did not complete", { commandId });
}

const sessionIds = () => ({ scan_session_id: id(), connection_id: id(), gatt_session_id: id() });

async function waitForUI(text, timeout = 60_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const body = await $("body").getText();
    if (body.includes("failed:")) throw new Error(body);
    if (body.includes(text)) return;
    await browser.pause(100);
  }
  throw new Error(`UI did not show ${text}`);
}

async function finishVisibleFlow(eventOffset) {
  if (platform !== "test") {
    const enabled = await waitForCollectedEvent((event) => event.kind === "notification_state_changed"
      && event.data?.state === "enabled" && !eventOffset.has(event.command_id), "UI flow did not enable notifications");
    await terminal(enabled.command_id);
    runFixture("android-notify", "deadbeef");
  }
  await waitForUI(platform === "apple" ? "CoreBluetooth flow passed" : "basic flow passed", 240_000);
  await waitForUI("12/12");
}

describe("BleHub Tauri UI", () => {
  before(async () => {
    await browser.switchToWindow(await browser.getWindowHandle());
    await installEventCollector();
    platform = (await invokeTauri("capabilities")).platform;
    if (rf && platform === "test") throw new Error("RF suite received a demo provider, not native RF evidence");
  });

  it("runs the platform-appropriate visible flow", async function () {
    await expect($("h1")).toHaveText("Central contract lab");
    const primary = $(platform === "apple" ? "button=Initialize CoreBluetooth" : "button=Run basic flow");
    await primary.waitForEnabled({ timeout: 10_000 });
    // Windows now uses the real provider too. Non-RF tests must not silently scan.
    if (platform !== "test" && platform !== "apple" && !rf) return this.skip();
    await primary.click();
    if (platform === "apple") {
      await waitForUI("CoreBluetooth ready");
      const snapshot = await waitForCollectedEvent((event) => event.kind === "adapter_snapshot_changed"
        && event.data?.state === "powered_on"
        && ["authorized", "not_required"].includes(event.data.scan_authorization)
        && ["authorized", "not_required"].includes(event.data.connection_authorization), "adapter is not ready");
      const initialized = await waitForCollectedEvent((event) => event.kind === "command_completed",
        "initialize did not terminal");
      if (snapshot.error || initialized.error) throw new Error("initialization failed");
      const button = $("button=Run CoreBluetooth flow");
      await button.waitForEnabled();
      if (!rf) return;
      await button.click();
    }
    await finishVisibleFlow(new Set());
  });

  it("repeats the clean visible flow with isolated command/session IDs", async function () {
    if (platform !== "test" && !rf) return this.skip();
    for (let cycle = 2; cycle <= 3; cycle++) {
      const before = await collectedEvents();
      const previous = new Set(before.map((event) => event.command_id).filter(Boolean));
      const button = $(platform === "apple" ? "button=Run CoreBluetooth flow again" : "button=Run basic flow");
      await button.waitForEnabled();
      await button.click();
      await waitForCollectedEvent((event) => event.kind === "scan_started" && !previous.has(event.command_id), "repeat did not start a new scan");
      await finishVisibleFlow(previous);
      const events = (await collectedEvents()).slice(before.length);
      const terminals = events.filter((event) => event.kind === "command_completed");
      if (terminals.some((event) => previous.has(event.command_id) || event.error)) throw new Error("reused or failed repeat command");
      if (new Set(terminals.map((event) => event.command_id)).size !== terminals.length) throw new Error("duplicate terminal in repeat");
      const oldScans = new Set(before.filter((event) => event.kind === "scan_started").map((event) => event.data.scan_session_id));
      if (events.some((event) => event.kind === "scan_started" && oldScans.has(event.data.scan_session_id))) throw new Error("reused scan session");
    }
  });

  it("sustains repeated scan-connect-GATT-read-disconnect cycles", async function () {
    if (!rf || platform !== "apple") return this.skip();
    const minutes = Number(process.env.BLEHUB_TAURI_SOAK_MINUTES ?? "0");
    if (!Number.isInteger(minutes) || minutes < 0) throw new Error("BLEHUB_TAURI_SOAK_MINUTES must be a non-negative integer");
    const deadline = minutes > 0 ? Date.now() + minutes * 60_000 : undefined;
    let active;
    let cycle = 0;
    try {
      while (deadline === undefined ? cycle < 10 : Date.now() < deadline) {
        active = sessionIds();
        await connect(active, await scan(active));
        const characteristics = await discover(active);
        const characteristic = characteristics.find((candidate) => candidate.uuid === readWriteCharacteristicUuid);
        if (!characteristic) throw new Error("pressure cycle missed fixture read characteristic");
        await read(active, characteristic);
        await disconnect(active);
        active = undefined;
        cycle++;
      }
      if (cycle === 0) throw new Error("soak completed zero cycles");
    } finally {
      if (active) {
        await command("stop_scan", { scan_session_id: active.scan_session_id }).catch(() => {});
        await command("disconnect", { connection_id: active.connection_id }).catch(() => {});
      }
    }
  });

  it("terminates in-flight GATT discovery on Android radio loss", async function () {
    if (!rf || platform !== "apple") return this.skip();
    const session = sessionIds();
    let radioFault;
    let started = false;
    try {
      await connect(session, await scan(session));
      const commandId = id();
      await invokeTauri("discover_gatt", { request: {
        command_id: commandId, connection_id: session.connection_id, gatt_session_id: session.gatt_session_id,
      } });
      started = true;
      radioFault = runFixtureAsync("android-radio-disable");
      // Attach rejection handling immediately while observing the backend terminal.
      void radioFault.catch(() => {});
      const failure = await terminal(commandId, true);
      if (failure.error == null) throw new Error("discovery completed before the fault; not in-flight failure evidence");
      await waitForCollectedEvent((event) => event.kind === "connection_state_changed" && event.command_id == null
        && event.data?.connection_id === session.connection_id && (event.data.state === "disconnected" || event.error != null),
      "radio loss did not terminate the lifecycle", { allowError: true });
      await radioFault;
      radioFault = undefined;
    } finally {
      if (radioFault) await radioFault.catch(() => {});
      if (started) { runFixture("android-radio-enable"); runFixture("android-resume"); }
    }
  });

  it("recovers from an active-subscription passive disconnect", async function () {
    if (!rf || platform !== "apple") return this.skip();
    const first = sessionIds();
    let radioDisabled = false;
    try {
      await connect(first, await scan(first));
      const characteristics = await discover(first);
      const notification = characteristics.find((candidate) => candidate.uuid === notificationCharacteristicUuid);
      if (!notification?.descriptors?.some((descriptor) => descriptor.uuid === "00002902-0000-1000-8000-00805f9b34fb")) {
        throw new Error("recovery notification characteristic/CCCD was not discovered");
      }
      const commandId = await command("set_notifications", {
        connection_id: first.connection_id, gatt_session_id: first.gatt_session_id,
        subscription_id: id(), characteristic_id: notification.id, action: "enable", mode: "platform_default",
      });
      await waitForCollectedEvent((event) => event.kind === "notification_state_changed" && event.command_id === commandId
        && event.data?.state === "enabled", "notification enable did not complete", { commandId });
      await browser.pause(1_000);
      runFixture("android-radio-disable");
      radioDisabled = true;
      await waitForCollectedEvent((event) => event.kind === "connection_state_changed" && event.command_id == null
        && event.data?.connection_id === first.connection_id && (event.data.state === "disconnected" || event.error != null),
      "passive disconnect was not delivered", { allowError: true });
      const offset = (await collectedEvents()).length;
      runFixture("android-radio-enable");
      runFixture("android-resume");
      radioDisabled = false;
      runFixtureAllowFailure("android-notify", "deadbeef");
      await browser.pause(2_000);
      if ((await collectedEvents()).slice(offset).some((event) => event.kind === "notification_received"
        && event.data?.connection_id === first.connection_id)) throw new Error("notification arrived after passive disconnect");
      const second = sessionIds();
      await connect(second, await scan(second));
      const recovered = (await discover(second)).find((candidate) => candidate.uuid === readWriteCharacteristicUuid);
      if (!recovered) throw new Error("recovered read characteristic was not discovered");
      await read(second, recovered);
      await disconnect(second);
    } finally {
      if (radioDisabled) runFixture("android-radio-enable");
      runFixture("android-resume");
    }
  });

  it("disposes the process-owned hub", async () => {
    await $("button=Dispose hub").click();
    await waitForUI("disposed", 10_000);
  });
});
