import { expect, test } from '@playwright/test';

const tinyPng = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAFElEQVR4nGNkYPjPAANMDEgANwcAMdMBB1sLEtoAAAAASUVORK5CYII=',
  'base64',
);

async function mockQuiz(page) {
  let saved = null;
  let failSave = false;
  const pairs = Array.from({length: 12}, (_, index) => ({
    id: 'pair-' + index,
    title: 'Art pairing ' + index,
    prompt: 'Which would you enjoy in your home?',
    left: {
      id: 'left-' + index, title: 'Landscape ' + index, artist: 'Landscape painter',
      image_url: '/static/curation/test-left.webp', source_url: 'https://www.artic.edu/artworks/1',
      license: 'Public domain', tags: ['landscape'],
    },
    right: {
      id: 'right-' + index, title: 'Abstract ' + index, artist: 'Abstract painter',
      image_url: '/static/curation/test-right.webp', source_url: 'https://www.artic.edu/artworks/2',
      license: 'Public domain', tags: ['abstract'],
    },
  }));
  await page.route('**/curation/quiz', async (route) => route.fulfill({json: {version: 1, pairs}}));
  await page.route('**/static/curation/test-*.webp', async (route) => route.fulfill({contentType: 'image/png', body: tinyPng}));
  await page.route('**/curation/profile', async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({json: saved});
    if (failSave) return route.fulfill({status: 503, json: {detail: 'Storage temporarily unavailable'}});
    saved = {
      ...route.request().postDataJSON(), updated_at: '2026-11-12T20:00:00Z',
      derived: {
        preferred_tags: [{tag: 'landscape', score: 4}], avoided_tags: [],
        summary: ['Prepare a 20–30 artwork review collection.', 'Preserve the whole artwork.'],
      },
    };
    return route.fulfill({json: saved});
  });
  return {
    saved: () => saved,
    failSaving: (value) => { failSave = value; },
  };
}

test('visual Art Profile saves, resumes, finishes, exports and edits home details', async ({page}) => {
  const store = await mockQuiz(page);
  await page.goto('/?page=curate');
  await expect(page.getByRole('heading', {name: 'Art for your everyday'})).toBeVisible();
  await page.getByRole('button', {name: 'Build my Art Profile'}).click();
  await page.getByRole('button', {name: 'Choose left: Landscape 0'}).click();
  await expect(page.getByRole('button', {name: 'Choose left: Landscape 0'})).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', {name: 'Next pair'}).click();
  await expect(page.getByText('Pair 2 of 12', {exact: true})).toBeVisible();
  await expect.poll(() => store.saved()?.step).toBe(1);

  await page.reload();
  await page.getByRole('button', {name: 'Continue your quiz'}).click();
  await expect(page.getByText('Pair 2 of 12', {exact: true})).toBeVisible();
  for (let index = 1; index < 12; index += 1) {
    await page.getByRole('button', {name: index < 6 ? 'I like both' : 'Skip this pair', exact: true}).click();
    await page.getByRole('button', {name: index === 11 ? 'About your home' : 'Next pair'}).click();
  }
  await page.getByLabel('Where do you live?').fill('Atlanta, Northern Hemisphere');
  await page.getByLabel('Where do you come from?').fill('Madrid');
  await page.getByLabel('What do the kids enjoy?').fill('Animals and drawing');
  await page.getByLabel('How should artwork fit the Frame TV?').selectOption('preserve');
  await page.getByRole('button', {name: 'Save my Art Profile'}).click();
  await expect(page.getByRole('heading', {name: 'Your household’s Art Profile'})).toBeVisible();
  await expect(page.getByText('Your first chapter is ready.')).toBeVisible();
  await expect(page.getByText('Madrid', {exact: true})).toBeVisible();
  expect(store.saved().status).toBe('complete');
  expect(store.saved().household.fit).toBe('preserve');
  expect(Object.keys(store.saved().choices)).toHaveLength(12);

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', {name: 'Download Art Profile'}).click();
  expect((await downloadPromise).suggestedFilename()).toBe('frameart-art-profile.json');
  await page.getByRole('button', {name: 'Edit home details'}).click();
  await expect(page.getByLabel('Where do you come from?')).toHaveValue('Madrid');
  await page.getByLabel('Where do you come from?').fill('Madrid and Galicia');
  await page.getByRole('button', {name: 'Save my Art Profile'}).click();
  await expect(page.getByText('Madrid and Galicia', {exact: true})).toBeVisible();
});

test('quiz keeps failed saves visible and works with keyboard on mobile', async ({page}) => {
  const store = await mockQuiz(page);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/?page=curate');
  await page.getByRole('button', {name: 'Build my Art Profile'}).click();
  store.failSaving(true);
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('button', {name: 'Choose right: Abstract 0'})).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#art-profile-save-state')).toContainText('Not saved.');
  store.failSaving(false);
  await page.getByRole('button', {name: 'Retry save'}).click();
  await expect(page.locator('#art-profile-save-state')).toHaveText('Saved on this FrameArt server');
  await expect(page.locator('.mobile-nav [data-page="curate"]')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
