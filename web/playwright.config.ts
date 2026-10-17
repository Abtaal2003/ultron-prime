import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

const PORTS = [8788, 8789];
// Locally, point PW_CHROMIUM at a Chromium binary (needed in sandboxed containers). In CI the
// browser installed by `playwright install` is used.
const local = process.env.PW_CHROMIUM ?? "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const launchOptions = existsSync(local) ? { executablePath: local, args: ["--no-sandbox"] } : {};

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { trace: "retain-on-failure" },
  // One demo server (own port, own home) per project, so state never leaks between them.
  webServer: PORTS.map((port) => ({
    command: ".venv/bin/python -m tests.e2e.demo_server",
    cwd: "..",
    url: `http://localhost:${port}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 90_000,
    env: { E2E_PORT: String(port), E2E_HOME: `/tmp/ultron-e2e-${port}` },
  })),
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 }, launchOptions, baseURL: "http://localhost:8788" } },
    { name: "phone", use: { ...devices["Pixel 5"], launchOptions, baseURL: "http://localhost:8789" } },
  ],
});
