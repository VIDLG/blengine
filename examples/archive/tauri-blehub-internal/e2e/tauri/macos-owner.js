import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, openSync, closeSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const here = path.dirname(fileURLToPath(import.meta.url));

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, resolve);
  });
  let released = false;
  return {
    port: server.address().port,
    release: () => {
      if (released) return Promise.resolve();
      released = true;
      return new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    },
  };
}

// A ready /status alone could belong to a stale process. Require the new app's
// exact PID and IPv4 loopback listener, not a port owner selected for killing.
export function ownsListener(pid, port) {
  const result = spawnSync("/usr/sbin/lsof", [
    "-nP", "-a", "-p", String(pid), "-iTCP:" + port, "-sTCP:LISTEN", "-Fpn",
  ], { encoding: "utf8", timeout: 2_000 });
  if (result.error) throw result.error;
  if (result.status === 1 && !result.stderr.trim()) return false;
  if (result.status !== 0) throw new Error(`lsof failed: ${result.stderr}`);
  const lines = result.stdout.trim().split("\n");
  return lines.includes(`p${pid}`) && lines.includes(`n127.0.0.1:${port}`);
}

export function compileMacOSOwner(repoRoot) {
  const output = path.join(repoRoot, ".work", "tauri-e2e", "macos-owner");
  mkdirSync(output, { recursive: true });
  const launcher = path.join(output, "launch-app");
  const compilation = spawnSync("/usr/bin/xcrun", [
    "swiftc", "-swift-version", "5", "-module-cache-path", path.join(output, "module-cache"),
    path.join(here, "launch-app.swift"), "-o", launcher,
  ], { stdio: "inherit", timeout: 120_000 });
  if (compilation.error || compilation.status !== 0) {
    throw new Error("NSWorkspace owner compilation failed", { cause: compilation.error });
  }
  return { output, launcher };
}

export async function launchMacOSApp({ repoRoot, appBundle }) {
  const { output, launcher } = compileMacOSOwner(repoRoot);
  const reservation = await reservePort();
  let log;
  let child;
  try {
    log = openSync(path.join(output, "owner.log"), "w");
    // This handoff has the usual bind race; ownsListener closes the identity gap
    // before WDIO may create a session. Never connect just because HTTP responds.
    await reservation.release();
    child = spawn(launcher, [appBundle, String(reservation.port)], {
      stdio: ["pipe", "pipe", log], detached: true,
    });
  } finally {
    await reservation.release();
    if (log !== undefined) closeSync(log);
  }
  let pid;
  let failure;
  let exited = false;
  let exitCode;
  let buffer = "";
  let stopping;
  const exit = new Promise((resolve) => {
    child.once("error", (error) => { failure = error; exited = true; resolve(); });
    child.once("close", (code, signal) => {
      exited = true;
      exitCode = code;
      if (code !== 0) failure ??= new Error(`NSWorkspace owner exited: code=${code}, signal=${signal}`);
      resolve();
    });
  });
  child.stdin.on("error", (error) => { failure ??= error; });
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    if (buffer.length > 16_384) {
      failure ??= new Error("NSWorkspace owner exceeded stdout protocol limit");
      child.stdin.destroy();
      return;
    }
    for (let newline; (newline = buffer.indexOf("\n")) !== -1;) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      try {
        const message = JSON.parse(line);
        if (message.event === "launched" && Number.isInteger(message.pid) && message.pid > 0 && !pid) {
          pid = message.pid;
          console.log(`[macOS owner] app PID=${pid}, port=${reservation.port}`);
        } else if (message.event === "error") {
          failure ??= new Error(message.message);
        } else if (message.event === "terminated" && message.pid === pid) {
          console.log(`[macOS owner] confirmed termination PID=${pid}`);
        } else {
          throw new Error(`Unexpected owner message: ${line}`);
        }
      } catch (error) {
        failure ??= error;
        child.stdin.destroy();
      }
    }
  });

  const releaseOnExit = () => child.stdin.destroy();
  const stop = () => stopping ??= (async () => {
    child.stdin.end();
    const deadline = Date.now() + 75_000;
    while (!exited && Date.now() < deadline) await delay(100);
    if (!exited) {
      // Do not kill the owner and abandon its app. Its own bounded cleanup is
      // authoritative; a hung owner must be reported, not called a clean exit.
      child.stdin.destroy();
      throw new Error(`NSWorkspace owner cleanup timed out (owner PID=${child.pid}, app PID=${pid})`);
    }
    await exit;
    process.off("exit", releaseOnExit);
    process.off("SIGINT", interrupt);
    process.off("SIGTERM", terminate);
    if (failure || exitCode !== 0) throw failure ?? new Error("NSWorkspace owner failed");
  })();
  const onSignal = (code) => {
    void stop().catch((error) => console.error(error)).finally(() => process.exit(code));
  };
  const interrupt = () => onSignal(130);
  const terminate = () => onSignal(143);
  process.once("exit", releaseOnExit);
  process.once("SIGINT", interrupt);
  process.once("SIGTERM", terminate);

  try {
    const deadline = Date.now() + 65_000;
    while (Date.now() < deadline) {
      if (failure) throw failure;
      if (exited) throw new Error("NSWorkspace owner exited before server readiness");
      if (pid && ownsListener(pid, reservation.port)) {
        let ready = false;
        try {
          const response = await fetch(`http://127.0.0.1:${reservation.port}/status`, {
            signal: AbortSignal.timeout(2_000),
          });
          ready = response.ok && (await response.json()).value?.ready === true;
        } catch { /* Server/webview may still be starting. */ }
        if (ready && !failure && !exited && ownsListener(pid, reservation.port)) {
          return { port: reservation.port, pid, stop };
        }
      }
      await delay(100);
    }
    throw new Error(`Embedded server not ready on its own PID=${pid}, port=${reservation.port}`);
  } catch (error) {
    try { await stop(); } catch (cleanup) {
      throw new AggregateError([error, cleanup], "macOS launch/readiness failed; cleanup also reported failure");
    }
    throw error;
  }
}
