import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { login, newChat, send } from "./helpers";

async function scan(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
  expect(summary).toEqual([]);
}

test("login page has no axe violations", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Operator passphrase")).toBeVisible();
  await scan(page);
});

test("chat screen, a finished turn, and settings have no axe violations", async ({ page }) => {
  await login(page);
  await newChat(page);
  await scan(page);
  await send(page, "please search for python news");
  await expect(page.getByRole("log", { name: "Conversation" })).toContainText("Python shipped a release.");
  await scan(page);
  await page.goto("/settings/memory");
  await expect(page.getByRole("heading", { name: "MEMORY VAULT" })).toBeVisible();
  await scan(page);
  await page.goto("/settings/safety");
  await expect(page.getByRole("heading", { name: "POLICY" })).toBeVisible();
  await scan(page);
});
