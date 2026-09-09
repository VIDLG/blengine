import { expect, test, vi } from "vitest";
import { adapterReady, createCommandRunner, initializeAdapter, runFlow } from "./flow.js";
import { installTauriMock } from "./tauriMock.js";

const ready = { state: "powered_on", scan_authorization: "authorized", connection_authorization: "not_required" };
const event = (kind, command_id, data = null, error = null) => ({ kind, command_id, data, error, detail: null });

for (const terminalFirst of [false, true]) {
  test(`initialization requires both pieces of evidence (terminal first: ${terminalFirst})`, async () => {
    const api = { invoke: vi.fn(async () => {}) };
    const runner = createCommandRunner(api);
    const result = initializeAdapter(runner);
    await Promise.resolve();
    const id = api.invoke.mock.calls[0][1].commandId;
    const snapshot = event("adapter_snapshot_changed", id, ready);
    const terminal = event("command_completed", id);
    let done = false;
    void result.then(() => { done = true; });
    runner.receive(terminalFirst ? terminal : snapshot);
    await Promise.resolve();
    expect(done).toBe(false);
    runner.receive(terminalFirst ? snapshot : terminal);
    expect(await result).toHaveLength(2);
  });
}

test("terminal success waits for powered_on and both authorizations, using latest snapshot", async () => {
  const api = { invoke: vi.fn(async () => {}) };
  const runner = createCommandRunner(api);
  const result = initializeAdapter(runner);
  await Promise.resolve();
  const id = api.invoke.mock.calls[0][1].commandId;
  runner.receive(event("adapter_snapshot_changed", id, ready));
  runner.receive(event("adapter_snapshot_changed", null, { ...ready, state: "powered_off" }));
  runner.receive(event("command_completed", id));
  let done = false;
  void result.then(() => { done = true; });
  for (const data of [
    { ...ready, scan_authorization: "not_determined" },
    { ...ready, connection_authorization: "denied" },
    { ...ready, state: "unavailable" },
  ]) {
    expect(adapterReady(data)).toBe(false);
    runner.receive(event("adapter_snapshot_changed", null, data));
    await Promise.resolve();
    expect(done).toBe(false);
  }
  runner.receive(event("adapter_snapshot_changed", null, ready));
  expect(await result).toHaveLength(2);
});

for (const kind of ["command_completed", "connection_state_changed"]) {
  test(`${kind} error fails immediately even with successful-looking state and no invoke completion`, async () => {
    const api = { invoke: vi.fn(() => new Promise(() => {})) };
    const runner = createCommandRunner(api);
    const result = runner.command("connect", { connection_id: "77" }, [(e) => e.data?.state === "connected"]);
    const rejected = expect(result).rejects.toThrow("gatt: broken");
    await Promise.resolve();
    const id = api.invoke.mock.calls[0][1].request.command_id;
    runner.receive(event(kind, id, { connection_id: "77", state: "connected" }, { category: "gatt", message: "broken", platform_details: null }));
    await rejected;
  });
}

test("old command and session events cannot satisfy a later invocation; duplicates do not fill missing domains", async () => {
  const api = { invoke: vi.fn(async () => {}) };
  const runner = createCommandRunner(api);
  const start = (scanId) => runner.command("start_scan", { scan_session_id: scanId }, [
    (e) => e.kind === "scan_started" && e.data?.scan_session_id === scanId,
    (e) => e.kind === "advertisement_observed" && e.data?.scan_session_id === scanId,
  ], { scope: { scan_session_id: scanId } });
  const first = start("100");
  await Promise.resolve();
  const firstId = api.invoke.mock.calls[0][1].request.command_id;
  const stale = [event("command_completed", firstId), event("scan_started", firstId, { scan_session_id: "100" }),
    event("advertisement_observed", null, { scan_session_id: "100", peripheral_id: "201" })];
  stale.forEach(runner.receive);
  await first;
  const second = start("101");
  let done = false;
  void second.then(() => { done = true; });
  await Promise.resolve();
  const secondId = api.invoke.mock.calls[1][1].request.command_id;
  expect(secondId).not.toBe(firstId);
  stale.forEach(runner.receive);
  runner.receive(event("command_completed", firstId, null, { category: "gatt", message: "late failure" }));
  runner.receive(event("command_completed", secondId));
  runner.receive(event("scan_started", secondId, { scan_session_id: "101" }));
  runner.receive(event("scan_started", secondId, { scan_session_id: "101" }));
  await Promise.resolve();
  expect(done).toBe(false);
  runner.receive(event("advertisement_observed", null, { scan_session_id: "101", peripheral_id: "202" }));
  expect(await second).toHaveLength(3);
});

for (const terminalFirst of [false, true]) {
  test(`shared Apple flow can repeat with fresh string IDs and hidden service UUIDs (${terminalFirst})`, async () => {
    const mock = installTauriMock({ platform: "apple", terminalFirst, terminalDetail: "display-only diagnostic" });
    const runner = createCommandRunner(mock.api);
    const stop = await mock.api.listen("blehub://event", ({ payload }) => runner.receive(payload));
    try {
      const capabilities = await mock.api.invoke("plugin:blehub|capabilities");
      const initialization = await initializeAdapter(runner);
      for (let cycle = 0; cycle < 2; cycle++) {
        const marks = [];
        await runFlow(runner, capabilities, initialization, (name) => marks.push(name), { readbackDelay: 0 });
        expect(marks).toHaveLength(12);
      }
      const ids = mock.commands.flatMap(({ args }) => args?.commandId ? [args.commandId] : args?.request ? [args.request.command_id] : []);
      expect(new Set(ids).size).toBe(ids.length);
      for (const key of ["scan_session_id", "connection_id", "gatt_session_id", "subscription_id"]) {
        const firstUses = mock.commands.filter(({ command, args }) => args?.request?.[key] && {
          scan_session_id: "start_scan", connection_id: "connect", gatt_session_id: "discover_gatt", subscription_id: "set_notifications",
        }[key] === command.replace("plugin:blehub|", "") && args.request.action !== "disable");
        expect(firstUses).toHaveLength(2);
        expect(firstUses[0].args.request[key]).not.toBe(firstUses[1].args.request[key]);
      }
    } finally { stop(); }
  });
}

test("missing domain events time out and clean up the waiter", async () => {
  const runner = createCommandRunner({ invoke: async () => {} }, { timeout: 10 });
  await expect(initializeAdapter(runner)).rejects.toThrow("timed out");
  const again = initializeAdapter(runner);
  runner.cancel(new Error("cancelled"));
  await expect(again).rejects.toThrow("cancelled");
});
