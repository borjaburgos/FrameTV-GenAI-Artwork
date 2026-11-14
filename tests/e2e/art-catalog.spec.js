import {expect, test} from '@playwright/test';

const tinyPng = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGNkYPjPAANMDEgANwcAMdMBB1sLEtoAAAAASUVORK5CYII=',
  'base64',
);

function catalog() {
  return {
    configured: true, status: 'ready', item_count: 2, pilot_limit: 20,
    last_synced_at: 1794580200,
    items: [
      {
        id: 'rs-1', title: 'Water Lilies', date: '1906', medium: 'Oil on canvas',
        artist: {
          id: 'claude-monet', name: 'Claude Monet', birth_year: 1840, death_year: 1926,
          nationality: 'French', bio: 'A French painter associated with Impressionism.',
          source_url: 'https://www.artic.edu/artists/35809/claude-monet',
        },
        movement: 'Impressionism', description: 'A quiet study of the garden pond.',
        source_url: 'https://www.artic.edu/artworks/16568/water-lilies',
        license: 'Public domain (CC0)', license_url: 'https://creativecommons.org/publicdomain/zero/1.0/',
        ai_generated: false, tags: ['garden', 'water'], width: 3840, height: 2160,
        institution: 'Art Institute of Chicago', accession_number: 'TEST-ACCESSION-1',
        work_dimensions: 'Original dimensions from the source catalog',
        crop_notes: 'Approved full-frame 16:9 crop; no matte or padding.',
        thumbnail_url: '/art-catalog/rs-1/thumbnail',
      },
      {
        id: 'rs-2', title: '<img src=x onerror="window.artInjected=true"> Bold Geometry',
        artist: {id: 'ai', name: 'AI-created original'}, date: '2026', medium: 'Digital image',
        source_url: 'javascript:window.artInjected=true', license_url: 'data:text/html,unsafe',
        license: 'AI original; provenance retained', ai_generated: true,
        ai_provenance: {tool: 'Image generation', prompt: 'Bold geometric shapes'},
        thumbnail_url: '/art-catalog/rs-2/thumbnail', tags: ['abstract', 'geometry'],
        width: 3840, height: 2160,
      },
    ],
  };
}

async function mockCatalog(page, initial = catalog()) {
  let current = initial;
  let failSync = false;
  let syncCalls = 0;
  await page.route('**/art-catalog', (route) => route.fulfill({json: current}));
  await page.route('**/art-catalog/*/thumbnail', (route) => route.fulfill({contentType: 'image/png', body: tinyPng}));
  await page.route('**/art-catalog/sync', (route) => {
    syncCalls += 1;
    return failSync ? route.fulfill({status: 502, json: {detail: 'ResourceSpace is temporarily unavailable.'}}) : route.fulfill({json: current});
  });
  return {
    setCurrent(value) { current = value; },
    setFailSync(value) { failSync = value; },
    syncCalls: () => syncCalls,
  };
}

test('curated library searches artists, shows verified metadata and keeps unsafe content inert', async ({page}) => {
  const requests = [];
  page.on('request', (request) => requests.push({url: request.url(), method: request.method()}));
  await mockCatalog(page);
  await page.goto('/?page=library');
  const root = page.locator('#art-catalog-root');
  await expect(root.getByText('Local catalog · 2 artworks', {exact: false})).toBeVisible();
  await expect(root.locator('.art-catalog-card')).toHaveCount(2);
  await root.getByLabel('Search curated artwork').fill('Monet');
  await expect(root.locator('.art-catalog-card')).toHaveCount(1);
  await root.getByRole('button', {name: 'Artwork details: Water Lilies', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: 'Water Lilies'});
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('Oil on canvas', {exact: true})).toBeVisible();
  await expect(dialog.getByText('1840–1926', {exact: true})).toBeVisible();
  await expect(dialog.getByText('A French painter associated with Impressionism.')).toBeVisible();
  await expect(dialog.locator('dt').filter({hasText: 'Collection / source'})).toBeVisible();
  await expect(dialog.getByText('Art Institute of Chicago', {exact: true})).toBeVisible();
  await expect(dialog.locator('dt').filter({hasText: 'Accession number'})).toBeVisible();
  await expect(dialog.getByText('TEST-ACCESSION-1', {exact: true})).toBeVisible();
  await expect(dialog.locator('dt').filter({hasText: 'Original artwork dimensions'})).toBeVisible();
  await expect(dialog.getByText('Original dimensions from the source catalog', {exact: true})).toBeVisible();
  await expect(dialog.locator('dt').filter({hasText: 'TV image dimensions'})).toBeVisible();
  await expect(dialog.getByText('3840 × 2160 px', {exact: true})).toBeVisible();
  await expect(dialog.getByText('TV crop', {exact: true})).toBeVisible();
  await expect(dialog.getByText('Approved full-frame 16:9 crop; no matte or padding.', {exact: true})).toBeVisible();
  await expect(dialog.getByRole('link', {name: 'Artwork source'})).toHaveAttribute('href', 'https://www.artic.edu/artworks/16568/water-lilies');
  await expect(dialog.getByRole('link', {name: 'Artist reference'})).toHaveAttribute('rel', 'noopener noreferrer');
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
  await root.getByLabel('Search curated artwork').fill('geometry');
  await expect(root.locator('.art-catalog-card')).toHaveCount(1);
  await expect(root.getByText('AI-created original', {exact: true})).toBeVisible();
  await root.locator('.art-catalog-card button').click();
  const aiDialog = page.locator('#art-catalog-dialog');
  await expect(aiDialog.getByRole('heading', {name: catalog().items[1].title, exact: true})).toBeVisible();
  await expect(aiDialog.getByText('Creator & provenance', {exact: true})).toBeVisible();
  await expect(aiDialog.locator('a')).toHaveCount(0);
  await expect(aiDialog.locator('[onerror]')).toHaveCount(0);
  expect(await page.evaluate(() => window.artInjected)).toBeUndefined();
  expect(requests.some(({url, method}) => method !== 'GET' &&
    /\/tv\/|\/jobs\/[^/]+\/apply/.test(url))).toBe(false);
});

test('failed metadata refresh preserves cached cards and always releases the button', async ({page}) => {
  const store = await mockCatalog(page);
  await page.goto('/?page=library');
  const root = page.locator('#art-catalog-root');
  const button = root.getByRole('button', {name: 'Refresh metadata'});
  await expect(root.locator('.art-catalog-card')).toHaveCount(2);
  store.setFailSync(true);
  await button.click();
  await expect(root.getByRole('alert')).toHaveText('ResourceSpace is temporarily unavailable.');
  await expect(root.getByRole('status')).toContainText('Your cached artwork is still available');
  await expect(root.locator('.art-catalog-card')).toHaveCount(2);
  await expect(button).toBeEnabled();
  await expect(root).toHaveAttribute('aria-busy', 'false');
  store.setFailSync(false);
  await button.click();
  await expect(root.getByRole('alert')).toBeHidden();
  await expect(button).toBeEnabled();
  expect(store.syncCalls()).toBe(2);
});

test('unconfigured catalog explains setup without interrupting the existing artwork library', async ({page}) => {
  await mockCatalog(page, {
    configured: false, status: 'not_configured', items: [], item_count: 0,
    last_synced_at: null, pilot_limit: 20,
  });
  await page.goto('/?page=library');
  const root = page.locator('#art-catalog-root');
  await expect(root.getByRole('status')).toContainText('Not connected');
  await expect(root.getByText('ResourceSpace is not connected yet.', {exact: false})).toBeVisible();
  await expect(root.getByRole('button', {name: 'Refresh metadata'})).toBeDisabled();
  await expect(page.getByRole('heading', {name: 'Artwork Library', exact: true})).toBeVisible();
  await expect(page.locator('#btn-refresh-gallery')).toBeEnabled();
});

test('stale catalog and artwork details remain usable without horizontal overflow on mobile', async ({page}, testInfo) => {
  const snapshot = catalog();
  snapshot.status = 'stale';
  snapshot.error = 'ResourceSpace is temporarily unavailable.';
  await mockCatalog(page, snapshot);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/?page=library');
  const root = page.locator('#art-catalog-root');
  await expect(root.getByRole('status')).toContainText('Showing the last successfully cached catalog');
  await root.getByRole('button', {name: 'Artwork details: Water Lilies', exact: true}).click();
  await expect(page.getByRole('dialog', {name: 'Water Lilies'})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  expect(await page.locator('#art-catalog-dialog').evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(false);
  await page.screenshot({path: testInfo.outputPath('mobile-artwork-details.png')});
  await page.locator('#art-catalog-dialog').evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect(page.getByRole('button', {name: 'Close artwork details'})).toBeInViewport({ratio: 1});
  await page.getByRole('button', {name: 'Close artwork details'}).click();
  await expect(page.locator('#art-catalog-dialog')).not.toBeVisible();
});
