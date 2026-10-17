import { expect, test } from "@playwright/test";
import { login } from "./helpers";

test.beforeEach(async ({ page }) => {
  await login(page);
});

test("settings pages load and runtime settings persist", async ({ page }) => {
  await page.goto("/settings/safety");
  const select = page.getByLabel("Scope strictness");
  await select.selectOption("strict");
  await expect(select).toHaveValue("strict");
  await page.reload();
  await expect(page.getByLabel("Scope strictness")).toHaveValue("strict");
  for (const [path, heading] of [
    ["models", "ROLE ROUTING"],
    ["persona", "PERSONA"],
    ["tools", "TOOLS"],
    ["files", "FILES"],
    ["redteam", "RED TEAM"],
    ["appearance", "APPEARANCE"],
    ["access", "SIGNED-IN DEVICES"],
  ] as const) {
    await page.goto(`/settings/${path}`);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
});

test("API keys never reach the page", async ({ page }) => {
  await page.goto("/settings/models");
  await expect(page.locator("body")).not.toContainText(/sk-[a-z]+-/);
  await expect(page.getByText("anthropic key")).toBeVisible();
});

test("a wrong passphrase is refused with a plain message", async ({ browser }) => {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto("/");
  await page.getByLabel("Operator passphrase").fill("wrong-wrong-wrong");
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page.getByRole("alert")).toContainText(/passphrase/i);
  await ctx.close();
});
