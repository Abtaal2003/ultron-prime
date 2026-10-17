import { expect, test } from "@playwright/test";
import { login, newChat, send } from "./helpers";

test.skip(({ viewport }) => (viewport?.width ?? 1280) >= 640, "phone layout only");

test.beforeEach(async ({ page }) => {
  await login(page);
  await newChat(page);
});

test("single column with a fixed input bar, a drawer for chats, and a sheet for panels", async ({ page }) => {
  await expect(page.getByRole("complementary")).toHaveCount(0); // no desktop side panes
  const composer = page.getByRole("form", { name: "Message composer" });
  const box = await composer.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height + 1);
  await page.getByRole("button", { name: "Open chats" }).click();
  await expect(page.getByRole("dialog", { name: "CHATS" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Workshop" }).click();
  await expect(page.getByRole("dialog", { name: "PANELS" })).toBeVisible();
  await expect(page.getByRole("tabpanel")).toBeVisible();
});

test("touch targets are at least 44 px and inputs use 16 px text", async ({ page }) => {
  const small = await page.evaluate(() => {
    const bad: string[] = [];
    document.querySelectorAll<HTMLElement>("button, a.btn, [role=tab], summary").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.height < 43.5 || r.width < 43.5)) bad.push(`${el.tagName} ${el.textContent?.trim().slice(0, 20)} ${Math.round(r.width)}x${Math.round(r.height)}`);
    });
    return bad;
  });
  expect(small).toEqual([]);
  const font = await page.getByLabel("Message", { exact: true }).evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  expect(font).toBeGreaterThanOrEqual(16);
});

test("long tool output and code scroll inside their own box, not the page", async ({ page }) => {
  await send(page, "please search for python news");
  await expect(page.getByRole("log", { name: "Conversation" })).toContainText("Python shipped a release.");
  await page.getByRole("button", { name: "details" }).first().click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("the app shell loads from the service worker while offline", async ({ page, context }) => {
  await page.reload();
  await page.waitForFunction(async () => (await navigator.serviceWorker.getRegistration()) !== undefined);
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await page.reload(); // let the worker take control
  await context.setOffline(true);
  await page.reload();
  await expect(page.locator("#root")).not.toBeEmpty();
  await context.setOffline(false);
});
