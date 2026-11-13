import { expect, test } from '@playwright/test';

async function mockFeedback(page) {
  let value = null;
  let status = 'showing';
  let failSave = false;
  const writes = [];
  const tvRequests = [];
  const artwork = {
    key: 'job:family-lake', title: 'A calm family lake', artist: 'Example painter',
    tags: ['landscape', 'calm'], thumbnail_url: null, reference: {job_id: 'family-lake'},
  };
  const ratings = () => value ? [{artwork_key: artwork.key, value, artwork}] : [];
  const summary = () => ({
    total_count: value ? 1 : 0, good_count: value === 'good' ? 1 : 0,
    bad_count: value === 'bad' ? 1 : 0, classified_count: value ? 1 : 0,
    preferred_tags: value === 'good' ? [{tag: 'landscape', score: 3}] : [],
    avoided_tags: value === 'bad' ? [{tag: 'landscape', score: 3}] : [],
    ratings: ratings(),
  });
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && pathname.startsWith('/tv/')) tvRequests.push(pathname);
  });
  await page.route('**/settings/tvs', (route) => route.fulfill({json: {
    tvs: [{profile_id: 'living', name: 'Family Room', ip: '192.168.50.40'}],
  }}));
  await page.route('**/curation/ratings', (route) => {
    if (route.request().method() === 'GET') return route.fulfill({json: {ratings: ratings(), summary: summary()}});
    if (failSave) return route.fulfill({status: 503, json: {detail: 'Rating storage temporarily unavailable'}});
    const payload = route.request().postDataJSON();
    writes.push(payload);
    value = payload.value;
    return route.fulfill({json: {rating: value, artwork, summary: summary()}});
  });
  await page.route('**/curation/now-showing?*', (route) => route.fulfill({json: {
    tv_profile_id: 'living', tv_name: 'Family Room', status,
    artwork: status === 'showing' ? artwork : null,
    rating: status === 'showing' ? value : null,
    message: status === 'watching' ? 'Normal TV viewing is active. Nothing was changed.' : '',
  }}));
  await page.route('**/curation/profile', (route) => route.fulfill({json: {
    schema_version: 1, status: 'draft', step: 0, choices: {}, household: {},
    feedback: summary(), derived: {preferred_tags: summary().preferred_tags, summary: []},
  }}));
  return {
    writes, tvRequests,
    setStatus: (next) => { status = next; },
    setFailSave: (next) => { failSave = next; },
  };
}

test('rates the displayed snapshot, changes and clears votes without controlling the TV', async ({page}) => {
  const mock = await mockFeedback(page);
  await page.goto('/?page=curate');
  const card = page.locator('#art-feedback-curate');
  await page.getByRole('button', {name: 'Build my Art Profile'}).click();
  const quizChoice = page.locator('#art-profile-root [data-art-choice="left"]');
  await quizChoice.click();
  await card.getByRole('button', {name: 'Check current picture'}).click();
  await expect(card.getByRole('heading', {name: 'A calm family lake'})).toBeVisible();
  const votes = card.getByRole('group', {name: 'Rate current artwork'});
  await votes.getByRole('button', {name: 'Good', exact: true}).click();
  await expect(votes.getByRole('button', {name: 'Good', exact: true})).toHaveAttribute('aria-pressed', 'true');
  await expect(card).toContainText('1 good · 0 bad');
  expect(mock.writes[0]).toEqual({reference: {job_id: 'family-lake'}, value: 'good'});
  await expect(quizChoice).toHaveAttribute('aria-pressed', 'true');
  await votes.getByRole('button', {name: 'Good', exact: true}).focus();
  await page.keyboard.press('ArrowRight');
  await expect(quizChoice).toHaveAttribute('aria-pressed', 'true');

  await votes.getByRole('button', {name: 'Bad', exact: true}).click();
  await expect(votes.getByRole('button', {name: 'Bad', exact: true})).toHaveAttribute('aria-pressed', 'true');
  await expect(card).toContainText('0 good · 1 bad');
  await card.getByText('Your recent ratings', {exact: true}).click();
  await card.getByRole('button', {name: 'Good rating for A calm family lake', exact: true}).click();
  await expect(votes.getByRole('button', {name: 'Good', exact: true})).toHaveAttribute('aria-pressed', 'true');
  expect(mock.writes.at(-1)).toEqual({reference: {artwork_key: 'job:family-lake'}, value: 'good'});
  await votes.getByRole('button', {name: 'Clear rating', exact: true}).click();
  await expect(votes.getByRole('button', {name: 'Good', exact: true})).toHaveAttribute('aria-pressed', 'false');
  await expect(votes.getByRole('button', {name: 'Clear rating'})).toBeDisabled();
  expect(mock.writes.at(-1).value).toBeNull();
  expect(mock.tvRequests).toEqual([]);
});

test('rating save failures can retry and active playback offers no voting on mobile', async ({page}) => {
  const mock = await mockFeedback(page);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/?page=tvs');
  const card = page.locator('#art-feedback-tvs');
  await card.getByRole('button', {name: 'Check current picture'}).click();
  mock.setFailSave(true);
  await card.getByRole('button', {name: 'Good', exact: true}).click();
  await expect(card.getByRole('alert')).toContainText('Rating storage temporarily unavailable');
  await expect(card.getByRole('button', {name: 'Good', exact: true})).toBeEnabled();
  mock.setFailSave(false);
  await card.getByRole('button', {name: 'Good', exact: true}).click();
  await expect(card).toContainText('1 good · 0 bad');

  mock.setStatus('watching');
  await card.getByRole('button', {name: 'Refresh current picture'}).click();
  await expect(card).toContainText('Normal TV viewing is active. Nothing was changed.');
  await expect(card.getByRole('group', {name: 'Rate current artwork'})).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  expect(mock.tvRequests).toEqual([]);
});
