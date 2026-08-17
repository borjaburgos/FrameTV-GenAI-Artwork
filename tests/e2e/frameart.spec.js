import { expect, test } from '@playwright/test';
import { mkdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

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

test('searches, tags, and collects library artwork', async ({ page }) => {
  const jobId = 'e2e-library-art';
  const jobDir = path.join(
    process.cwd(),
    '.e2e-data',
    'artifacts',
    '2026',
    '01',
    '01',
    jobId,
  );
  await mkdir(jobDir, { recursive: true });
  await writeFile(
    path.join(jobDir, 'final.png'),
    Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS8AAAAASUVORK5CYII=',
      'base64',
    ),
  );
  await writeFile(
    path.join(jobDir, 'meta.json'),
    JSON.stringify({ job_id: jobId, prompt_original: 'Blue mountain lake', provider: 'openai' }),
  );

  try {
    await page.goto('/');
    await page.locator('.tabs').getByRole('button', { name: 'Library' }).click();
    const card = page.locator('.gallery-item').filter({ hasText: 'Blue mountain lake' });
    await expect(card).toBeVisible();

    page.once('dialog', (dialog) => dialog.accept('travel, blue'));
    await card.getByRole('button', { name: 'Tags' }).click();
    await expect(card).toContainText('travel');
    await expect(card).toContainText('blue');

    await page.getByPlaceholder('New collection name').fill('E2E Favorites');
    await page.getByRole('button', { name: 'Create Collection' }).click();
    await page.locator('#library-target-collection').selectOption({ label: 'E2E Favorites (0)' });
    await card.locator('.gallery-select-item').check();
    await page.getByRole('button', { name: 'Add to Collection' }).click();
    await expect(card).toContainText('#E2E Favorites');

    await page.getByPlaceholder('Search prompts, providers, tags...').fill('mountain');
    await page.getByRole('button', { name: 'Apply Filters' }).click();
    await expect(card).toBeVisible();

    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: 'Delete Collection' }).click();
    await expect(page.locator('#library-target-collection')).not.toContainText('E2E Favorites');
  } finally {
    await rm(jobDir, { recursive: true, force: true });
  }
});
