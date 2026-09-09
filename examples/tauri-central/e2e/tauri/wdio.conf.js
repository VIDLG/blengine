import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const repoRoot = path.resolve(root, "../..");
const appBundle = path.join(
  repoRoot,
  ".work",
  "cargo-target",
  "debug",
  "bundle",
  "macos",
  "BleHub Tauri Central.app",
);
const binary = path.join(appBundle, "Contents", "MacOS", "blehub-tauri-central");

export const config = {
  runner: "local",
  specs: ["./specs/**/*.e2e.js"],
  maxInstances: 1,
  logLevel: "warn",
  services: [["@wdio/tauri-service", {
    appBinaryPath: binary,
    driverProvider: "embedded",
    embeddedPort: 4445,
    startTimeout: 60_000,
    commandTimeout: 30_000,
  }]],
  capabilities: [{
    browserName: "tauri",
    maxInstances: 1,
    "tauri:options": { application: binary },
  }],
  framework: "mocha",
  reporters: ["spec"],
  mochaOpts: { ui: "bdd", timeout: 300_000 },
  onPrepare: () => {
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
    const rust = spawnSync(
      "pixi",
      [
        "run",
        "npm",
        "--prefix",
        "ui",
        "exec",
        "--",
        "tauri",
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
    );
    if (rust.status !== 0) throw new Error("Tauri host bundle build failed");

    // Launch the signed/packaged app through LaunchServices first. This gives
    // macOS TCC the real bundle identity before the embedded provider starts
    // the bundle executable with WDIO's port/environment.
    const launch = spawnSync("open", ["-na", appBundle], {
      cwd: root,
      stdio: "inherit",
      shell: false,
    });
    if (launch.status !== 0) throw new Error("LaunchServices failed to start the macOS app bundle");
    const settle = spawnSync("sleep", ["2"], { stdio: "inherit", shell: false });
    if (settle.status !== 0) throw new Error("Failed while waiting for LaunchServices app startup");
    const quit = spawnSync(
      "osascript",
      ["-e", 'tell application id "dev.blehub.tauri.central" to quit'],
      { cwd: root, stdio: "inherit", shell: false },
    );
    if (quit.status !== 0) throw new Error("Failed to stop the LaunchServices TCC preflight app");
    const released = spawnSync("sleep", ["1"], { stdio: "inherit", shell: false });
    if (released.status !== 0) throw new Error("Failed while waiting for the TCC preflight app to exit");
  },
};
