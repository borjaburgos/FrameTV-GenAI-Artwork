import {expect, test} from '@playwright/test';

const tinyPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGNkYPjPAANMDEgANwcAMdMBB1sLEtoAAAAASUVORK5CYII=', 'base64');

async function mockDailyArt(page) {
  const items = Array.from({length: 365}, (_, index) => ({
    id: 'annual-' + (index + 1), title: index === 0 ? '<script>Unsafe title</script>' : 'Artwork ' + (index + 1),
    artist: {name: index === 364 ? 'Last Artist' : 'Artist ' + (index + 1)},
    thumbnail_url: '/art-catalog/annual-' + (index + 1) + '/thumbnail', tags: ['landscape'],
    source_url: 'https://www.artic.edu/artworks/' + (index + 1), license: 'Public domain',
  }));
  let syncStarted = false;
  let syncPolls = 0;
  const writes = [];
  const imports = [];
  const sends = [];
  let failWrites = false;
  let daily = {
    enabled: false, timezone: 'America/New_York', change_time: '00:00', target_type: 'tv', target_id: 'living',
    hold_until: null, holds: [], results: [], pending: false, calendar_year: 2027, calendar_count: 365,
    today: {date: '2027-01-01', artwork_id: 'annual-1', status: 'scheduled'},
    tomorrow: {date: '2027-01-02', artwork_id: 'annual-2', status: 'scheduled'},
  };
  await page.route('**/settings/tvs', (route) => route.fulfill({json: {tvs: [{profile_id: 'living', ip: '192.168.50.20'}]}}));
  await page.route('**/automation/groups', (route) => route.fulfill({json: [{id: 'downstairs', name: 'Downstairs', tv_profile_ids: ['living']}]}));
  await page.route('**/art-catalog', (route) => {
    if (syncStarted) syncPolls += 1;
    return route.fulfill({json: {
      configured: true, status: 'ready', items,
      sync: {status: syncStarted && syncPolls < 2 ? 'running' : 'completed', completed: syncPolls ? 365 : 0, total: 365},
    }});
  });
  await page.route('**/art-catalog/*/thumbnail', (route) => route.fulfill({contentType: 'image/png', body: tinyPng}));
  await page.route('**/art-catalog/sync', (route) => {
    syncStarted = true;
    return route.fulfill({status: 202, json: {id: 'sync-1', status: 'running'}});
  });
  await page.route('**/art-catalog/*/send', (route) => {
    sends.push(route.request().postDataJSON());
    return route.fulfill({status: 202, json: {id: 'delivery-1', status: 'running'}});
  });
  await page.route('**/art-catalog/deliveries/*', (route) => route.fulfill({json: {
    id: 'delivery-1', status: 'completed', results: [{tv: 'living', status: 'deferred', detail: 'TV in use'}],
  }}));
  await page.route('**/modes/daily-art', (route) => {
    if (route.request().method() === 'GET') return route.fulfill({json: daily});
    if (failWrites) return route.fulfill({status: 503, json: {detail: 'Settings temporarily unavailable'}});
    const body = route.request().postDataJSON();
    writes.push(body);
    daily = {...daily, ...body, holds: body.clear_holds
      ? daily.holds.filter((hold) => hold.reason === 'family-album') : daily.holds};
    return route.fulfill({json: daily});
  });
  await page.route('**/modes/daily-art/calendar', (route) => {
    const body = route.request().postDataJSON();
    imports.push(body);
    daily = {...daily, calendar_year: body.year, calendar_count: body.entries.length,
      calendar_years: [2026, 2027], total_calendar_count: 49 + body.entries.length};
    return route.fulfill({json: daily});
  });
  return {writes, imports, sends, failWrites(value) { failWrites = value; }, setDaily(value) { daily = {...daily, ...value}; }};
}

test('annual catalog stays bounded, searches all365, refreshes asynchronously and defers busy-TV sends', async ({page}) => {
  const state = await mockDailyArt(page);
  await page.goto('/?page=library');
  const root = page.locator('#art-catalog-root');
  await expect(root.locator('.art-catalog-card')).toHaveCount(24);
  await root.getByRole('button', {name: 'Show more artworks'}).click();
  await expect(root.locator('.art-catalog-card')).toHaveCount(48);
  await root.getByLabel('Search curated artwork').fill('Last Artist');
  await expect(root.locator('.art-catalog-card')).toHaveCount(1);
  await expect(root).toContainText('Artwork 365');
  await root.getByLabel('Search curated artwork').fill('');
  await root.getByRole('button', {name: 'Refresh metadata'}).click();
  await expect(root.getByRole('button', {name: 'Refresh metadata'})).toBeEnabled();
  await expect(root.getByRole('status')).toContainText('Refreshing in the background');
  await root.getByRole('button', {name: 'Artwork details: <script>Unsafe title</script>', exact: true}).click();
  const dialog = page.locator('#art-catalog-dialog');
  await dialog.getByLabel('TV or TV group').selectOption({label: 'Group · Downstairs'});
  await dialog.getByRole('button', {name: 'Send to TV', exact: true}).click();
  await expect(dialog.locator('#art-catalog-delivery')).toContainText('Skipped safely');
  await expect(dialog.getByRole('button', {name: 'Send to TV', exact: true})).toBeEnabled();
  expect(state.sends).toEqual([{target_type: 'group', target_id: 'downstairs'}]);
  expect(await dialog.locator('script').count()).toBe(0);
  expect(state.writes).toEqual([]);
});

test('Daily Art saves groups, pauses and holds safely, imports year-aware calendar without enabling it', async ({page}) => {
  const state = await mockDailyArt(page);
  await page.goto('/?page=modes');
  const root = page.locator('#daily-art-root');
  await expect(root.getByRole('status')).toContainText('Daily Art is paused.');
  await expect(root).toContainText('2027 calendar · 365 dates assigned');
  expect(state.writes).toEqual([]);
  await root.getByLabel('Daily Art TV or group').selectOption({label: 'Group · Downstairs'});
  await root.getByLabel('Enable daily rotation').check();
  await root.getByRole('button', {name: 'Save Daily Art', exact: true}).click();
  await expect(root.getByRole('status')).toContainText('Daily Art is enabled.');
  expect(state.writes[0]).toEqual({enabled: true, target_type: 'group', target_id: 'downstairs', timezone: 'America/New_York', change_time: '00:00'});
  await root.getByRole('button', {name: 'Hold until tomorrow'}).click();
  await expect.poll(() => state.writes.length).toBe(2);
  expect(Date.parse(state.writes[1].hold_until)).toBeGreaterThan(Date.now());
  await root.getByRole('button', {name: 'Clear manual holds'}).click();
  await expect.poll(() => state.writes.length).toBe(3);
  expect(state.writes[2].clear_holds).toBe(true);
  expect(state.writes[2].hold_until).toBeNull();
  await root.getByRole('button', {name: 'Pause rotation'}).click();
  await expect(root.getByRole('status')).toContainText('Daily Art is paused.');
  const calendar = {year: 2027, timezone: 'America/New_York', entries: [{date: '2027-01-01', artwork_id: 'annual-1', season: 'winter'}]};
  await root.getByLabel('Import calendar JSON').setInputFiles({name: 'calendar.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(calendar))});
  await root.getByRole('button', {name: 'Import calendar', exact: true}).click();
  await expect(root).toContainText('2026 + 2027 calendars · 50 total dates assigned');
  await expect(root.getByLabel('Enable daily rotation')).not.toBeChecked();
  expect(state.imports).toEqual([calendar]);
});

test('Daily Art errors release controls, malformed imports stay local and mobile does not overflow', async ({page}) => {
  const state = await mockDailyArt(page);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/?page=modes');
  const root = page.locator('#daily-art-root');
  await expect(root.getByRole('status')).toContainText('paused');
  state.failWrites(true);
  await root.getByRole('button', {name: 'Save Daily Art', exact: true}).click();
  await expect(root.getByRole('alert')).toHaveText('Settings temporarily unavailable');
  await expect(root.getByRole('button', {name: 'Save Daily Art', exact: true})).toBeEnabled();
  await root.getByLabel('Import calendar JSON').setInputFiles({name: 'too-large.json', mimeType: 'application/json', buffer: Buffer.alloc(513 * 1024, ' ')});
  await root.getByRole('button', {name: 'Import calendar', exact: true}).click();
  await expect(root.getByRole('alert')).toHaveText('The calendar file must be 512 KiB or smaller.');
  await root.getByLabel('Import calendar JSON').setInputFiles({name: 'broken.json', mimeType: 'application/json', buffer: Buffer.from('not-json')});
  await root.getByRole('button', {name: 'Import calendar', exact: true}).click();
  await expect(root.getByRole('alert')).toHaveText('This file is not valid JSON.');
  await expect(root.getByRole('button', {name: 'Import calendar', exact: true})).toBeEnabled();
  expect(state.imports).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});

test('family-album priority stays enabled, explains midnight resume and cannot be cleared as a manual hold', async ({page}) => {
  const state = await mockDailyArt(page);
  state.setDaily({enabled: true, holds: [{tv: 'family-room', reason: 'family-album', date: '2027-01-01', pending: false}]});
  await page.goto('/?page=modes');
  const root = page.locator('#daily-art-root');
  await expect(root.getByRole('status')).toContainText('New family-album photo takes priority until midnight on family-room');
  await expect(root.getByRole('status')).toContainText('Daily Art resumes at the next local midnight');
  await expect(root.getByLabel('Enable daily rotation')).toBeChecked();
  await expect(root.getByRole('button', {name: 'Clear manual holds'})).toBeDisabled();
  expect(state.writes).toEqual([]);

  state.setDaily({holds: [{tv: 'family-room', reason: 'family-album', pending: true, until: null}]});
  await root.getByRole('button', {name: 'Refresh Daily Art'}).click();
  await expect(root.getByRole('status')).toContainText('Family photo waiting for Art Mode on family-room');
  await expect(root.getByRole('status')).toContainText('Daily Art waits until it is shown, then the following midnight.');
  await expect(root.getByRole('button', {name: 'Clear manual holds'})).toBeDisabled();
  expect(state.writes).toEqual([]);

  state.setDaily({holds: [
    {tv: 'family-room', reason: 'family-album', date: '2027-01-01'},
    {tv: 'living', reason: 'manual', date: '2027-01-01'},
  ]});
  await root.getByRole('button', {name: 'Refresh Daily Art'}).click();
  await expect(root.getByRole('button', {name: 'Clear manual holds'})).toBeEnabled();
  await root.getByRole('button', {name: 'Clear manual holds'}).click();
  await expect(root.getByRole('status')).toContainText('New family-album photo takes priority until midnight');
  await expect(root.getByLabel('Enable daily rotation')).toBeChecked();
  await expect(root.getByRole('button', {name: 'Clear manual holds'})).toBeDisabled();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].clear_holds).toBe(true);
});
