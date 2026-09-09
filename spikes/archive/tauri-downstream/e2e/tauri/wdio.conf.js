import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const repoRoot = path.resolve(root, "../..");
const binary = path.join(repoRoot, ".work", "cargo-target", "debug", "blehub-tauri-spike");

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
    const result = spawnSync("pixi", ["run", "npm", "run", "build"], {
      cwd: path.join(root, "ui"),
      stdio: "inherit",
      shell: false,
    });
    if (result.status !== 0) throw new Error("React frontend build failed");
    const rust = spawnSync("pixi", ["run", "cargo", "build", "--locked", "--features", "wdio", "--manifest-path", "src-tauri/Cargo.toml"], {
      cwd: root,
      stdio: "inherit",
      shell: false,
    });
    if (rust.status !== 0) throw new Error("Tauri host build failed");
  },
};
