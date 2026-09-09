import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../../..");
const serviceUuid = "fff00000-0000-4000-8000-000000000000";
const readWriteCharacteristicUuid = "fff10000-0000-4000-8000-000000000000";
const notificationCharacteristicUuid = "fff20000-0000-4000-8000-000000000000";

function runFixture(...args) {
  const result = spawnSync("pixi", ["run", "just", "peripheral", ...args], {
    cwd: repoRoot,
    stdio: "inherit",
    shell: false,
  });
  if (result.status !== 0) throw new Error(`fixture command failed: ${args.join(" ")}`);
}

function runFixtureAllowFailure(...args) {
  return spawnSync("pixi", ["run", "just", "peripheral", ...args], {
    cwd: repoRoot,
    stdio: "inherit",
    shell: false,
  }).status;
}

function runFixtureAsync(...args) {
  return new Promise((resolve, reject) => {
    const child = spawn("pixi", ["run", "just", "peripheral", ...args], {
      cwd: repoRoot,
      stdio: "inherit",
      shell: false,
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`fixture command failed: ${args.join(" ")}`));
      }
    });
  });
}

async function invokeTauri(command, args) {
  const result = await browser.executeAsync((commandName, commandArgs, done) => {
    window.__TAURI__.core.invoke(commandName, commandArgs)
      .then(() => done({ ok: true }))
      .catch((error) => done({ ok: false, error: String(error) }));
  }, command, args);
  const value = result?.value ?? result;
  if (!value?.ok) throw new Error(`Tauri command ${command} failed: ${value?.error ?? "unknown error"}`);
}

async function installEventCollector() {
  const result = await browser.executeAsync((done) => {
    window.__BLEHUB_TAURI_EVENTS__ = [];
    window.__BLEHUB_TAURI_UNSUBSCRIBE__ = undefined;
    window.__TAURI__.event.listen("blehub://event", ({ payload }) => {
      window.__BLEHUB_TAURI_EVENTS__.push(payload);
    }).then((stop) => {
      window.__BLEHUB_TAURI_UNSUBSCRIBE__ = stop;
      done({ ok: true });
    }).catch((error) => done({ ok: false, error: String(error) }));
  });
  const value = result?.value ?? result;
  if (!value?.ok) throw new Error(`Tauri event collector failed: ${value?.error ?? "unknown error"}`);
}

async function collectedEvents() {
  const result = await browser.execute(() => window.__BLEHUB_TAURI_EVENTS__ ?? []);
  return result?.value ?? result;
}

async function waitForCollectedEvent(predicate, timeoutMsg) {
  let observed;
  try {
    await browser.waitUntil(
      async () => {
        const events = await collectedEvents();
        observed = events.find(predicate);
        return observed ?? false;
      },
      { timeout: 60_000, timeoutMsg },
    );
  } catch (error) {
    const events = await collectedEvents();
    const summary = events.map((event) => `${event.kind}:${event.command_id ?? "none"}`).join(",");
    throw new Error(`${timeoutMsg}; observed events=${summary}`, { cause: error });
  }
  return observed;
}

describe("BleHub Tauri UI", () => {
  before(async () => {
    await browser.switchToWindow(await browser.getWindowHandle());
  });
  it("runs the platform-appropriate visible flow", async () => {
    await expect($("h1")).toHaveText("Central contract lab");

    const primaryButton = $("main button");
    await primaryButton.waitForDisplayed();
    await browser.waitUntil(
      async () => ["Run basic flow", "Initialize CoreBluetooth"].includes(await primaryButton.getText()),
      { timeout: 10_000, timeoutMsg: "the backend-specific primary action did not render" },
    );

    const action = await primaryButton.getText();
    await primaryButton.click();
    if (action === "Initialize CoreBluetooth") {
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("state=PoweredOn"),
        { timeout: 30_000, timeoutMsg: "CoreBluetooth did not report PoweredOn" },
      );
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("adapter_snapshot_changed"),
        { timeout: 10_000, timeoutMsg: "adapter snapshot event was not projected" },
      );
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("command_completed"),
        { timeout: 10_000, timeoutMsg: "command completion event was not projected" },
      );
      const flowButton = $("button=Run CoreBluetooth flow");
      await flowButton.waitForDisplayed();
      if (process.env.BLEHUB_TAURI_RF === "1") {
        await flowButton.click();
        await browser.waitUntil(
          async () => (await $("body").getText()).includes("notification_state_changed"),
          { timeout: 60_000, timeoutMsg: "Tauri CoreBluetooth flow did not confirm notification enable" },
        );
        const notification = spawnSync(
          "pixi",
          ["run", "just", "peripheral", "android-notify", "deadbeef"],
          { cwd: repoRoot, stdio: "inherit", shell: false },
        );
        if (notification.status !== 0) throw new Error("Android fixture notification command failed");
        await browser.waitUntil(
          async () => {
            const text = await $("body").getText();
            if (text.includes("flow failed:")) throw new Error(text);
            return text.includes("CoreBluetooth flow passed");
          },
          { timeout: 240_000, timeoutMsg: "Tauri CoreBluetooth RF flow did not complete" },
        );
        await browser.waitUntil(
          async () => (await $("body").getText()).includes("12/12"),
          { timeout: 10_000, timeoutMsg: "Tauri CoreBluetooth flow did not report all steps" },
        );
      }
    } else {
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("basic flow passed"),
        { timeout: 10_000, timeoutMsg: "fake basic flow did not complete" },
      );
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("notification_received"),
        { timeout: 10_000, timeoutMsg: "fake notification event was not projected" },
      );
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("12/12"),
        { timeout: 10_000, timeoutMsg: "fake flow did not report all expected events" },
      );
    }
  });

  it("repeats the clean CoreBluetooth flow across UI sessions", async function () {
    if (process.env.BLEHUB_TAURI_RF !== "1") return;

    for (let cycle = 2; cycle <= 3; cycle += 1) {
      const flowButton = $("button=Run CoreBluetooth flow again");
      await flowButton.waitForDisplayed();
      await flowButton.click();
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("running CoreBluetooth flow: set_notifications"),
        { timeout: 60_000, timeoutMsg: `repeat flow ${cycle} did not reach notification setup` },
      );
      await browser.waitUntil(
        async () => (await $("body").getText()).includes("notification_state_changed"),
        { timeout: 60_000, timeoutMsg: `repeat flow ${cycle} did not enable notification` },
      );
      runFixture("android-notify", "deadbeef");
      await browser.waitUntil(
        async () => {
          const text = await $("body").getText();
          if (text.includes("flow failed:")) throw new Error(text);
          return text.includes("CoreBluetooth flow passed") && text.includes("12/12");
        },
        { timeout: 240_000, timeoutMsg: `repeat flow ${cycle} did not complete` },
      );
    }
  });

  it("sustains repeated scan-connect-GATT-read-disconnect cycles", async function () {
    if (process.env.BLEHUB_TAURI_RF !== "1") return;

    await installEventCollector();
    let activeScanSession;
    let activeConnection;
    try {
      for (let cycle = 0; cycle < 10; cycle += 1) {
        const commandBase = 1_000 + cycle * 10;
        const scanSession = 2_000 + cycle;
        const connection = 3_000 + cycle;
        const gattSession = 4_000 + cycle;
        activeScanSession = scanSession;
        activeConnection = connection;

        await invokeTauri("plugin:blehub|start_scan", {
          request: {
            command_id: commandBase,
            scan_session_id: scanSession,
            service_uuids: [serviceUuid],
            name_prefix: null,
            company_id: null,
            allow_duplicates: true,
          },
        });
        const advertisement = await waitForCollectedEvent(
          (event) => event.kind === "advertisement_observed"
            && event.data?.scan_session_id === scanSession
            && event.data.advertised_services?.includes(serviceUuid),
          `pressure cycle ${cycle + 1} did not observe the Android fixture`,
        );
        await invokeTauri("plugin:blehub|stop_scan", {
          request: { command_id: commandBase + 1, scan_session_id: scanSession },
        });
        await waitForCollectedEvent(
          (event) => event.kind === "command_completed"
            && event.command_id === commandBase + 1
            && event.detail == null,
          `pressure cycle ${cycle + 1} did not stop scanning`,
        );
        activeScanSession = undefined;

        await invokeTauri("plugin:blehub|connect", {
          request: {
            command_id: commandBase + 2,
            connection_id: connection,
            peripheral_id: advertisement.data.peripheral_id,
          },
        });
        await waitForCollectedEvent(
          (event) => event.kind === "connection_state_changed"
            && event.command_id === commandBase + 2
            && event.data?.connection_id === connection
            && event.data.state?.includes("Connected"),
          `pressure cycle ${cycle + 1} did not connect`,
        );
        await invokeTauri("plugin:blehub|discover_gatt", {
          request: {
            command_id: commandBase + 3,
            connection_id: connection,
            gatt_session_id: gattSession,
          },
        });
        const database = await waitForCollectedEvent(
          (event) => event.kind === "gatt_discovered" && event.command_id === commandBase + 3,
          `pressure cycle ${cycle + 1} did not discover GATT`,
        );
        await waitForCollectedEvent(
          (event) => event.kind === "command_completed"
            && event.command_id === commandBase + 3
            && event.detail == null,
          `pressure cycle ${cycle + 1} did not complete GATT discovery`,
        );
        const characteristic = database.data?.services
          ?.flatMap((service) => service.characteristics)
          .find((candidate) => candidate.uuid === readWriteCharacteristicUuid);
        if (!characteristic) throw new Error(`pressure cycle ${cycle + 1} missed the read characteristic`);
        await invokeTauri("plugin:blehub|read_characteristic", {
          request: {
            command_id: commandBase + 4,
            connection_id: connection,
            gatt_session_id: gattSession,
            characteristic_id: characteristic.id,
          },
        });
        await waitForCollectedEvent(
          (event) => event.kind === "characteristic_read" && event.command_id === commandBase + 4,
          `pressure cycle ${cycle + 1} did not read the characteristic`,
        );
        await invokeTauri("plugin:blehub|disconnect", {
          request: { command_id: commandBase + 5, connection_id: connection },
        });
        await waitForCollectedEvent(
          (event) => event.kind === "connection_state_changed"
            && event.command_id === commandBase + 5
            && event.data?.connection_id === connection
            && event.data.state?.includes("Disconnected"),
          `pressure cycle ${cycle + 1} did not disconnect`,
        );
        activeConnection = undefined;
      }
    } finally {
      if (activeScanSession !== undefined) {
        try {
          await invokeTauri("plugin:blehub|stop_scan", {
            request: { command_id: 9_999, scan_session_id: activeScanSession },
          });
        } catch {
          // The backend may already have retired the scan while reporting the failure.
        }
      }
      if (activeConnection !== undefined) {
        try {
          await invokeTauri("plugin:blehub|disconnect", {
            request: { command_id: 9_998, connection_id: activeConnection },
          });
        } catch {
          // The backend may already have retired the connection while reporting the failure.
        }
      }
    }
  });

  it("terminates in-flight GATT discovery on Android radio loss", async function () {
    if (process.env.BLEHUB_TAURI_RF !== "1") return;

    await installEventCollector();
    const fault = {
      scanSession: 501,
      connection: 502,
      gattSession: 503,
      startScanCommand: 51,
      stopScanCommand: 52,
      connectCommand: 53,
      discoverCommand: 54,
    };
    let radioFaultStarted = false;
    let radioFault;
    try {
      await invokeTauri("plugin:blehub|start_scan", {
        request: {
          command_id: fault.startScanCommand,
          scan_session_id: fault.scanSession,
          service_uuids: [serviceUuid],
          name_prefix: null,
          company_id: null,
          allow_duplicates: true,
        },
      });
      const advertisement = await waitForCollectedEvent(
        (event) => event.kind === "advertisement_observed"
          && event.data?.scan_session_id === fault.scanSession
          && event.data.advertised_services?.includes(serviceUuid),
        "in-flight fault scan did not observe the Android fixture",
      );
      await invokeTauri("plugin:blehub|stop_scan", {
        request: {
          command_id: fault.stopScanCommand,
          scan_session_id: fault.scanSession,
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "command_completed"
          && event.command_id === fault.stopScanCommand
          && event.detail == null,
        "in-flight fault scan did not stop",
      );
      await invokeTauri("plugin:blehub|connect", {
        request: {
          command_id: fault.connectCommand,
          connection_id: fault.connection,
          peripheral_id: advertisement.data.peripheral_id,
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "connection_state_changed"
          && event.command_id === fault.connectCommand
          && event.data?.connection_id === fault.connection
          && event.data.state?.includes("Connected"),
        "in-flight fault connection did not become connected",
      );

      await invokeTauri("plugin:blehub|discover_gatt", {
        request: {
          command_id: fault.discoverCommand,
          connection_id: fault.connection,
          gatt_session_id: fault.gattSession,
        },
      });
      radioFaultStarted = true;
      radioFault = runFixtureAsync("android-radio-disable");
      const discoveryFailure = await waitForCollectedEvent(
        (event) => event.kind === "command_completed"
          && event.command_id === fault.discoverCommand
          && event.detail != null,
        "Android radio loss did not terminate in-flight GATT discovery with an error",
      );
      if (!discoveryFailure.detail) {
        throw new Error("in-flight GATT discovery unexpectedly completed without an error");
      }
      await waitForCollectedEvent(
        (event) => {
          const state = event.data?.state ?? "";
          return event.kind === "connection_state_changed"
            && event.command_id == null
            && event.data?.connection_id === fault.connection
            && (state.includes("Disconnected") || state.startsWith("Err"));
        },
        "in-flight Android radio loss did not terminate the connection lifecycle",
      );
      await radioFault;
      radioFault = undefined;
    } finally {
      if (radioFault) await radioFault.catch(() => {});
      if (radioFaultStarted) {
        runFixture("android-radio-enable");
        runFixture("android-resume");
      }
    }
  });

  it("recovers from an active-subscription passive disconnect", async function () {
    if (process.env.BLEHUB_TAURI_RF !== "1") return;

    await installEventCollector();
    const first = {
      scanSession: 201,
      connection: 302,
      gattSession: 402,
      subscription: 802,
    };
    let radioDisabled = false;
    try {
      await invokeTauri("plugin:blehub|start_scan", {
        request: {
          command_id: 21,
          scan_session_id: first.scanSession,
          service_uuids: [serviceUuid],
          name_prefix: null,
          company_id: null,
          allow_duplicates: true,
        },
      });
      const advertisement = await waitForCollectedEvent(
        (event) => event.kind === "advertisement_observed"
          && event.data?.scan_session_id === first.scanSession
          && event.data.advertised_services?.includes(serviceUuid),
        "recovery scan did not observe the Android fixture",
      );
      await invokeTauri("plugin:blehub|stop_scan", {
        request: { command_id: 22, scan_session_id: first.scanSession },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "command_completed" && event.command_id === 22 && event.detail == null,
        "recovery scan did not stop",
      );

      await invokeTauri("plugin:blehub|connect", {
        request: {
          command_id: 23,
          connection_id: first.connection,
          peripheral_id: advertisement.data.peripheral_id,
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "connection_state_changed"
          && event.data?.connection_id === first.connection
          && event.data.state?.includes("Connected"),
        "recovery connection did not become connected",
      );
      await invokeTauri("plugin:blehub|discover_gatt", {
        request: {
          command_id: 24,
          connection_id: first.connection,
          gatt_session_id: first.gattSession,
        },
      });
      const database = await waitForCollectedEvent(
        (event) => event.kind === "gatt_discovered" && event.command_id === 24,
        "recovery GATT discovery did not complete",
      );
      await waitForCollectedEvent(
        (event) => event.kind === "command_completed" && event.command_id === 24 && event.detail == null,
        "recovery GATT command did not complete",
      );
      const notificationCharacteristic = database.data?.services
        ?.flatMap((service) => service.characteristics)
        .find((characteristic) => characteristic.uuid === notificationCharacteristicUuid);
      if (!notificationCharacteristic) throw new Error("recovery notification characteristic was not discovered");

      await invokeTauri("plugin:blehub|set_notifications", {
        request: {
          command_id: 25,
          connection_id: first.connection,
          gatt_session_id: first.gattSession,
          subscription_id: first.subscription,
          characteristic_id: notificationCharacteristic.id,
          action: "enable",
          mode: "platform_default",
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "notification_state_changed"
          && event.command_id === 25
          && event.data?.state?.includes("Enabled"),
        "recovery notification enable did not complete",
      );
      await waitForCollectedEvent(
        (event) => event.kind === "command_completed" && event.command_id === 25 && event.detail == null,
        "recovery notification command did not complete",
      );
      await new Promise((resolve) => setTimeout(resolve, 1_000));

      runFixture("android-radio-disable");
      radioDisabled = true;
      const passiveDisconnect = await waitForCollectedEvent(
        (event) => {
          const state = event.data?.state ?? "";
          // CoreBluetooth may report a native error for an adapter-loss disconnect;
          // both forms terminate the command-less connection lifecycle.
          return event.kind === "connection_state_changed"
            && event.command_id == null
            && event.data?.connection_id === first.connection
            && (state.includes("Disconnected") || state.startsWith("Err"));
        },
        "Tauri did not receive the passive Android disconnect",
      );
      if (passiveDisconnect.data?.connection_id !== first.connection) {
        throw new Error("passive disconnect used the wrong connection identity");
      }
      const eventsAtDisconnect = (await collectedEvents()).length;

      runFixture("android-radio-enable");
      runFixture("android-resume");
      radioDisabled = false;
      runFixtureAllowFailure("android-notify", "deadbeef");
      await new Promise((resolve) => setTimeout(resolve, 2_000));
      const staleNotifications = (await collectedEvents())
        .slice(eventsAtDisconnect)
        .filter((event) => event.kind === "notification_received"
          && event.data?.connection_id === first.connection);
      if (staleNotifications.length !== 0) {
        throw new Error("old connection received a notification after passive disconnect");
      }

      const second = {
        scanSession: 202,
        connection: 303,
        gattSession: 403,
      };
      await invokeTauri("plugin:blehub|start_scan", {
        request: {
          command_id: 26,
          scan_session_id: second.scanSession,
          service_uuids: [serviceUuid],
          name_prefix: null,
          company_id: null,
          allow_duplicates: true,
        },
      });
      const recoveredAdvertisement = await waitForCollectedEvent(
        (event) => event.kind === "advertisement_observed"
          && event.data?.scan_session_id === second.scanSession
          && event.data.advertised_services?.includes(serviceUuid),
        "recovery scan did not observe the restored Android fixture",
      );
      await invokeTauri("plugin:blehub|stop_scan", {
        request: { command_id: 27, scan_session_id: second.scanSession },
      });
      await invokeTauri("plugin:blehub|connect", {
        request: {
          command_id: 28,
          connection_id: second.connection,
          peripheral_id: recoveredAdvertisement.data.peripheral_id,
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "connection_state_changed"
          && event.data?.connection_id === second.connection
          && event.data.state?.includes("Connected"),
        "recovered connection did not become connected",
      );
      await invokeTauri("plugin:blehub|discover_gatt", {
        request: {
          command_id: 29,
          connection_id: second.connection,
          gatt_session_id: second.gattSession,
        },
      });
      const recoveredDatabase = await waitForCollectedEvent(
        (event) => event.kind === "gatt_discovered" && event.command_id === 29,
        "recovered GATT discovery did not complete",
      );
      const readWriteCharacteristic = recoveredDatabase.data?.services
        ?.flatMap((service) => service.characteristics)
        .find((characteristic) => characteristic.uuid === readWriteCharacteristicUuid);
      if (!readWriteCharacteristic) throw new Error("recovered read/write characteristic was not discovered");
      await invokeTauri("plugin:blehub|read_characteristic", {
        request: {
          command_id: 30,
          connection_id: second.connection,
          gatt_session_id: second.gattSession,
          characteristic_id: readWriteCharacteristic.id,
        },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "characteristic_read" && event.command_id === 30,
        "recovered characteristic read did not complete",
      );
      await invokeTauri("plugin:blehub|disconnect", {
        request: { command_id: 31, connection_id: second.connection },
      });
      await waitForCollectedEvent(
        (event) => event.kind === "connection_state_changed"
          && event.command_id === 31
          && event.data?.state?.includes("Disconnected"),
        "recovered logical disconnect did not complete",
      );
    } finally {
      if (radioDisabled) {
        runFixture("android-radio-enable");
      }
      runFixture("android-resume");
    }
  });

  it("disposes the process-owned hub", async () => {
    await $("button=Dispose hub").click();
    await browser.waitUntil(
      async () => (await $("body").getText()).includes("disposed"),
      { timeout: 10_000, timeoutMsg: "hub was not disposed" },
    );
  });
});
