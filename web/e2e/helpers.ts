import { expect, type Page } from "@playwright/test";

export const PASSPHRASE = "e2e-passphrase-123";

export async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Operator passphrase").fill(PASSPHRASE);
  await page.getByRole("button", { name: "Enter" }).click();
  await expect(page.getByRole("heading", { name: /ULTRON PRIME/ })).toBeVisible();
}

/** Create a chat through the UI and land on it. */
export async function newChat(page: Page): Promise<void> {
  const phone = (page.viewportSize()?.width ?? 1280) < 640;
  if (phone || (page.viewportSize()?.width ?? 0) < 1024) await page.getByRole("button", { name: "Open chats" }).click();
  await page.getByRole("button", { name: "+ New chat", exact: true }).click();
  await expect(page).toHaveURL(/\/chat\/chat_/);
  await expect(page.getByLabel("Message", { exact: true })).toBeVisible();
}

export async function send(page: Page, text: string): Promise<void> {
  await page.getByLabel("Message", { exact: true }).fill(text);
  await page.getByRole("button", { name: "Send" }).click();
}
