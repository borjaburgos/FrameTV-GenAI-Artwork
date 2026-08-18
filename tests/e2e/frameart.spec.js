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

test('identifies an existing physical TV and intentionally renames it', async ({ page, request }) => {
  await request.post('/settings/tvs', {
    data: {
      profile_id: 'e2e_existing_tv',
      ip: '192.168.50.26',
      port: 8002,
      client_name: 'Existing TV',
      ssl: true,
    },
  });

  try {
    await openSettings(page);
    await page.getByRole('button', { name: 'Add TV' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add TV' });
    await dialog.getByLabel('Profile ID').fill('e2e_renamed_tv');
    await dialog.getByLabel('Private IPv4 address').fill('192.168.50.26');
    page.once('dialog', (confirmation) => confirmation.accept());
    await dialog.getByRole('button', { name: 'Save TV' }).click();

    await expect(page.locator('#settings-tv-list')).toContainText('e2e_renamed_tv');
    await expect(page.locator('#settings-tv-list')).not.toContainText('e2e_existing_tv');
  } finally {
    await request.delete('/settings/tvs/e2e_existing_tv');
    await request.delete('/settings/tvs/e2e_renamed_tv');
  }
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

test('library actions safely handle hostile prompt text', async ({ page }) => {
  const cases = [
    ['e2e-prompt-apostrophe', "child's drawing"],
    ['e2e-prompt-backslash', 'path \\ through woods'],
    ['e2e-prompt-quote', 'the "blue" room'],
    ['e2e-prompt-linebreak', 'first line\nsecond line'],
    ['e2e-prompt-html', '<b>test</b>'],
  ];
  const jobDirs = [];
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));

  for (const [jobId, prompt] of cases) {
    const jobDir = path.join(
      process.cwd(), '.e2e-data', 'artifacts', '2026', '01', '03', jobId,
    );
    jobDirs.push(jobDir);
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
      JSON.stringify({ job_id: jobId, prompt_original: prompt, provider: 'openai' }),
    );
  }

  try {
    await page.goto('/');
    await page.locator('.tabs').getByRole('button', { name: 'Library' }).click();

    for (const [jobId, prompt] of cases) {
      await page.locator(`[data-upload-job-id="${jobId}"]`).click();
      expect(await page.locator('#upload-job-id').evaluate((node) => node.textContent)).toBe(prompt);
      await page.locator('#btn-upload-cancel').click();

      await page.locator(`[data-remix-job-id="${jobId}"]`).click();
      expect(
        await page.locator('#remix-source-label').evaluate((node) => node.textContent),
      ).toBe('Library · ' + prompt);
      await page.locator('#btn-remix-cancel').click();
    }

    expect(errors).toEqual([]);
  } finally {
    await Promise.all(jobDirs.map((jobDir) => rm(jobDir, { recursive: true, force: true })));
  }
});

test('TV gallery distinguishes loaded, missing, and unavailable thumbnails', async ({ page, request }) => {
  await request.post('/settings/tvs', {
    data: {
      profile_id: 'e2e_thumbnail_tv',
      ip: '192.168.50.29',
      port: 8002,
      client_name: 'FrameArt E2E',
      ssl: true,
    },
  });
  await page.route('**/tv/art?**', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        { content_id: 'MY_LOADED', is_favourite: false, local_job_id: null },
        { content_id: 'MY_MISSING', is_favourite: false, local_job_id: null },
        { content_id: 'MY_BUSY', is_favourite: false, local_job_id: null },
      ]),
    });
  });
  await page.route('**/tv/art/thumbnails/warm', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        cached: [],
        warmed: ['MY_LOADED', 'MY_BUSY'],
        missing: ['MY_MISSING'],
      }),
    });
  });
  await page.route('**/tv/art/thumbnail?**', async (route) => {
    const contentId = new URL(route.request().url()).searchParams.get('content_id');
    if (contentId === 'MY_LOADED') {
      await route.fulfill({
        contentType: 'image/png',
        body: Buffer.from(
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC',
          'base64',
        ),
      });
    } else if (contentId === 'MY_MISSING') {
      await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    } else {
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
    }
  });

  try {
    await page.goto('/');
    await page.locator('.tabs').getByRole('button', { name: 'TVs' }).click();
    await page.locator('#tv-art-select').selectOption('192.168.50.29');
    await page.getByRole('button', { name: 'Load Art' }).click();

    const loaded = page.locator('.tv-art-item').filter({ hasText: 'MY_LOADED' });
    const missing = page.locator('.tv-art-item').filter({ hasText: 'MY_MISSING' });
    const busy = page.locator('.tv-art-item').filter({ hasText: 'MY_BUSY' });
    await expect(loaded.locator('.art-thumb-wrap')).toHaveAttribute('data-thumbnail-state', 'loaded');
    await expect(missing.locator('.art-thumb-fallback')).toHaveText('No thumbnail available');
    await expect(busy.locator('.art-thumb-fallback')).toHaveText('TV unavailable · Retry');
    await expect(busy.locator('.art-thumb-fallback')).toBeEnabled();
  } finally {
    await request.delete('/settings/tvs/e2e_thumbnail_tv');
  }
});

test('creates TV groups, playlists, and durable schedules', async ({ page, request }) => {
  const jobId = 'e2e-automation-art';
  const jobDir = path.join(process.cwd(), '.e2e-data', 'artifacts', '2026', '01', '02', jobId);
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
    JSON.stringify({ job_id: jobId, prompt_original: 'Automation landscape' }),
  );
  await request.post('/settings/tvs', {
    data: {
      profile_id: 'e2e_automation_tv',
      ip: '192.168.50.26',
      port: 8002,
      client_name: 'FrameArt E2E',
      ssl: true,
    },
  });

  try {
    await page.goto('/');
    await page.locator('.tabs').getByRole('button', { name: 'Automations' }).click();
    await expect(page.locator('#panel-automations')).toBeVisible();
    await expect(page.locator('#automation-integration-status')).toContainText('Running');

    await page.getByLabel('Group name').fill('E2E Group');
    await page.locator('#automation-group-tvs input[value="e2e_automation_tv"]').check();
    await page.getByRole('button', { name: 'Create Group' }).click();
    await expect(page.locator('#automation-group-list')).toContainText('E2E Group');

    await page.getByLabel('Playlist name').fill('E2E Playlist');
    await page.getByLabel('Artwork').selectOption(jobId);
    await page.getByRole('button', { name: 'Create Playlist' }).click();
    await expect(page.locator('#automation-playlist-list')).toContainText('E2E Playlist');

    await page.getByLabel('Schedule name').fill('E2E Schedule');
    await page.getByRole('button', { name: 'Create Schedule' }).click();
    const schedule = page.locator('#automation-schedule-list .settings-item').filter({
      hasText: 'E2E Schedule',
    });
    await expect(schedule).toContainText('E2E Playlist');
    await schedule.getByRole('button', { name: 'Pause' }).click();
    await expect(schedule.getByRole('button', { name: 'Resume' })).toBeVisible();
    await schedule.getByRole('button', { name: 'Delete' }).click();
    await expect(schedule).toHaveCount(0);

    await page.locator('#automation-playlist-list .settings-item')
      .filter({ hasText: 'E2E Playlist' }).getByRole('button', { name: 'Delete' }).click();
    await page.locator('#automation-group-list .settings-item')
      .filter({ hasText: 'E2E Group' }).getByRole('button', { name: 'Delete' }).click();
  } finally {
    await request.delete('/settings/tvs/e2e_automation_tv');
    await rm(jobDir, { recursive: true, force: true });
  }
});
