import { expect, test } from '@playwright/test';

async function openSettings(page) {
  await page.goto('/');
  await page.locator('.tabs').getByRole('button', { name: 'Settings' }).click();
  await expect(page.locator('#panel-settings')).toBeVisible();
}

test('loads modular frontend assets without browser errors', async ({ page }) => {
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'FrameArt' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Create Artwork' })).toBeVisible();

  const assets = await page.evaluate(() =>
    performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname),
  );
  expect(assets).toContain('/static/app.css');
  expect(assets).toContain('/static/app.js');
  expect(errors).toEqual([]);
});

test('adds and removes an image provider', async ({ page }) => {
  await openSettings(page);
  await page.getByRole('button', { name: 'Add Provider' }).click();
  await page.getByLabel('Provider type').selectOption('ollama');
  await page.getByLabel('Base URL').fill('http://127.0.0.1:11434');
  await page.getByLabel('Primary model').fill('llava');
  await page.getByRole('button', { name: 'Save Provider' }).click();

  const provider = page.locator('#settings-provider-list .settings-item').filter({
    hasText: 'ollama',
  });
  await expect(provider).toContainText('llava');

  page.once('dialog', (dialog) => dialog.accept());
  await provider.getByRole('button', { name: 'Delete' }).click();
  await expect(provider).toHaveCount(0);
});

test('adds and removes a persistent TV profile', async ({ page }) => {
  await openSettings(page);
  await page.getByRole('button', { name: 'Add TV' }).click();
  const dialog = page.getByRole('dialog', { name: 'Add TV' });
  await dialog.getByLabel('Profile ID').fill('e2e_living_room');
  await dialog.getByLabel('Private IPv4 address').fill('192.168.50.25');
  await dialog.getByLabel('Pairing name').fill('FrameArt E2E');
  await dialog.getByRole('button', { name: 'Save TV' }).click();

  const tv = page.locator('#settings-tv-list .settings-item').filter({
    hasText: 'e2e_living_room',
  });
  await expect(tv).toContainText('192.168.50.25:8002');

  page.once('dialog', (dialog) => dialog.accept());
  await tv.getByRole('button', { name: 'Delete' }).click();
  await expect(tv).toHaveCount(0);
});

test('shows discovered TVs and provides a persistent save action', async ({ page }) => {
  await page.route('**/tv/discover?**', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        {
          ip: '192.168.50.30',
          name: 'Living Room Frame',
          model: 'QN55LS03',
          frame_tv: true,
        },
      ]),
    });
  });
  await page.goto('/');
  await page.locator('.tabs').getByRole('button', { name: 'TVs' }).click();
  await page.getByRole('button', { name: 'Scan Network' }).click();

  const tv = page.locator('.tv-card').filter({ hasText: 'Living Room Frame' });
  await expect(tv).toContainText('192.168.50.30');
  await expect(tv.getByRole('button', { name: 'Save' })).toBeVisible();
});

test('runs diagnostics, creates a backup, and exposes mobile navigation', async ({ page }) => {
  await openSettings(page);
  await page.getByRole('button', { name: 'Run Diagnostics' }).click();
  await expect(page.locator('#settings-diagnostics-list')).toContainText('data directory');
  await expect(page.locator('#settings-diagnostics-list')).toContainText('settings store');

  await page.getByRole('button', { name: 'Create Backup' }).click();
  await expect(page.locator('#settings-backup-list')).toContainText('Restore');

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.mobile-nav')).toBeVisible();
  await page.locator('.mobile-nav button[data-page="create"]').click();
  await expect(page.getByRole('heading', { name: 'Create Artwork' })).toBeVisible();
});
