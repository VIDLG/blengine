import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { compileMacOSOwner, ownsListener } from "./macos-owner.js";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const launcher = path.join(repoRoot, ".work", "tauri-e2e", "macos-owner", "launch-app");
const bundle = path.join(repoRoot, ".work", "cargo-target", "debug", "bundle", "macos", "BleHub Tauri Internal.app");
const isMac = process.platform === "darwin";

async function until(predicate, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await delay(50);
  }
  throw new Error("owner lifecycle assertion timed out");
}

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch (error) {
    if (error.code === "ESRCH") return false;
    throw error;
  }
}

async function port() {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const value = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return value;
}

function observe(child) {
  const state = { child, messages: [], exited: false, code: undefined, stderr: "" };
  let buffer = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    for (let index; (index = buffer.indexOf("\n")) !== -1;) {
      state.messages.push(JSON.parse(buffer.slice(0, index)));
      buffer = buffer.slice(index + 1);
    }
  });
  child.stderr.on("data", (chunk) => { state.stderr += chunk; });
  child.on("exit", (code) => { state.code = code; state.exited = true; });
  child.on("error", (error) => { state.stderr += error.message; state.exited = true; });
  child.stdin.on("error", () => {});
  return state;
}

async function start(t, app = bundle) {
  const state = observe(spawn(launcher, [app, String(await port())], { stdio: ["pipe", "pipe", "pipe"] }));
  t.after(async () => {
    state.child.stdin.end();
    await until(() => state.exited);
  });
  return state;
}

const launched = (state) => state.messages.find((message) => message.event === "launched");

// The Just recipe builds the app; compile this candidate's owner independently
// of E2E success. These tests open windows but never initialize or invoke BLE.
test("macOS LaunchServices owner lifecycle", { skip: !isMac, timeout: 120_000 }, async (t) => {
  assert.ok(existsSync(bundle), "run via just tauri macos-owner-test to build the app");
  compileMacOSOwner(repoRoot);

  await t.test("listener identity rejects another process even on a responding port", async () => {
    const server = net.createServer();
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      assert.equal(ownsListener(process.pid, server.address().port), true);
      assert.equal(ownsListener(1, server.address().port), false);
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  });

  await t.test("normal stop cleans only the owned instance, leaving its neighbor alive", async (t) => {
    const first = await start(t);
    const second = await start(t);
    await until(() => launched(first) && launched(second));
    const firstPid = launched(first).pid;
    const secondPid = launched(second).pid;
    assert.notEqual(firstPid, secondPid);
    second.child.stdin.end("stop\n");
    await until(() => second.exited && !alive(secondPid));
    assert.equal(second.code, 0, second.stderr);
    assert.ok(alive(firstPid));
    first.child.stdin.end();
    await until(() => first.exited && !alive(firstPid));
    assert.equal(first.code, 0, first.stderr);
  });

  await t.test("EOF during pending launch cleans the late returned instance", async (t) => {
    const state = await start(t);
    state.child.stdin.end();
    await until(() => state.exited);
    assert.equal(state.code, 0, state.stderr);
    assert.ok(launched(state), JSON.stringify(state.messages));
    assert.equal(alive(launched(state).pid), false);
  });

  await t.test("NSWorkspace launch failure exits nonzero without an owned app", async (t) => {
    const state = await start(t, path.join(repoRoot, ".work", "tauri-e2e", "absent-owner-test.app"));
    await until(() => state.exited);
    assert.equal(state.code, 1, state.stderr);
    assert.ok(state.messages.some((message) => message.event === "error"));
    assert.equal(launched(state), undefined);
  });

  await t.test("unresponsive app receives bounded forceTerminate on EOF", async (t) => {
    const state = await start(t);
    await until(() => launched(state));
    const pid = launched(state).pid;
    process.kill(pid, "SIGSTOP");
    state.child.stdin.end();
    await until(() => state.exited && !alive(pid));
    assert.equal(state.code, 0, state.stderr);
  });

  await t.test("SIGKILL of Node closes the lease; owner survives to clean the app", async (t) => {
    const script = `
      import { spawn } from 'node:child_process';
      const child = spawn(${JSON.stringify(launcher)}, [${JSON.stringify(bundle)}, ${JSON.stringify(String(await port()))}], {
        stdio: ['pipe', 'pipe', 'pipe'], detached: true,
      });
      console.log(JSON.stringify({ event: 'owner', pid: child.pid }));
      child.stdout.pipe(process.stdout);
      child.stderr.pipe(process.stderr);
    `;
    const parent = observe(spawn(process.execPath, ["--input-type=module", "-e", script], { stdio: ["pipe", "pipe", "pipe"] }));
    t.after(() => { if (!parent.exited) parent.child.kill("SIGKILL"); });
    await until(() => launched(parent));
    const appPid = launched(parent).pid;
    const ownerPid = parent.messages.find((message) => message.event === "owner").pid;
    parent.child.kill("SIGKILL");
    await until(() => parent.exited && !alive(appPid) && !alive(ownerPid));
  });
});
