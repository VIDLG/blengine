import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { launchMacOSApp } from "./macos-owner.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const repoRoot = path.resolve(root, "../..");
const targetDir = path.join(repoRoot, ".work", "cargo-target", "debug");
const appBundle = path.join(targetDir, "bundle", "macos", "BleHub Tauri Internal.app");
const binary = process.platform === "darwin"
  ? path.join(appBundle, "Contents", "MacOS", "blehub-tauri-internal")
  : process.platform === "win32"
    ? path.join(targetDir, "blehub-tauri-internal.exe")
    : path.join(targetDir, "blehub-tauri-internal");

const macOS = process.platform === "darwin";
let appOwner;

export const config = {
  runner: "local",
  specs: ["./specs/**/*.e2e.js"],
  maxInstances: 1,
  logLevel: "warn",
  services: macOS ? [] : [["@wdio/tauri-service", { appBinaryPath: binary }]],
  ...(macOS ? { protocol: "http", hostname: "127.0.0.1", path: "/", connectionRetryCount: 0 } : {}),
  capabilities: macOS ? [{
    browserName: "tauri",
    maxInstances: 1,
    "wdio:enforceWebDriverClassic": true,
  }] : [{
    browserName: "tauri",
    maxInstances: 1,
    "tauri:options": { application: binary },
    "wdio:tauriServiceOptions": {
      driverProvider: "embedded",
      embeddedPort: 4445,
      startTimeout: 60_000,
      commandTimeout: 30_000,
      captureBackendLogs: true,
    },
  }],
  framework: "mocha",
  reporters: ["spec"],
  mochaOpts: { ui: "bdd", timeout: 300_000 },
  onPrepare: async (runnerConfig, capabilities) => {
    const demo = process.env.BLEHUB_TAURI_DEMO === "1";
    if (demo && process.env.BLEHUB_TAURI_RF === "1") {
      throw new Error("demo is not RF/conformance evidence; do not combine BLEHUB_TAURI_DEMO and BLEHUB_TAURI_RF");
    }
    const result = spawnSync("pixi", ["run", "npm", "run", "build"], {
      cwd: path.join(root, "ui"),
      stdio: "inherit",
      shell: false,
    });
    if (result.status !== 0) throw new Error("React frontend build failed");
    const featureArgs = demo
      ? ["--no-default-features", "--features", "wdio,demo"]
      : ["--features", "wdio"];
    const rust = process.platform === "darwin"
      ? spawnSync(
        "pixi",
        [
          "run",
          "npm",
          "run",
          "tauri",
          "--",
          "build",
          "--debug",
          "--bundles",
          "app",
          "--config",
          "src-tauri/tauri.conf.json",
          "--",
          ...featureArgs,
        ],
        {
          cwd: root,
          env: { ...process.env, CARGO_TARGET_DIR: path.join(repoRoot, ".work", "cargo-target") },
          stdio: "inherit",
          shell: false,
        },
      )
      : spawnSync(
        "pixi",
        ["run", "rtk", "cargo", "build", "--locked", "--manifest-path", "src-tauri/Cargo.toml", ...featureArgs],
        {
          cwd: root,
          env: { ...process.env, CARGO_TARGET_DIR: path.join(repoRoot, ".work", "cargo-target") },
          stdio: "inherit",
          shell: false,
        },
      );
    if (rust.status !== 0) throw new Error("Tauri host build failed");

    if (macOS) {
      appOwner = await launchMacOSApp({ repoRoot, appBundle });
      runnerConfig.port = appOwner.port;
      for (const capability of capabilities) {
        capability.protocol = "http";
        capability.hostname = "127.0.0.1";
        capability.port = appOwner.port;
        capability.path = "/";
      }
    }
  },
  onComplete: async () => {
    await appOwner?.stop();
  },
};
