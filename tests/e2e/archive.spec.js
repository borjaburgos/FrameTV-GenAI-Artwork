import { test, expect } from '@playwright/test';

test('Settings shows archive backlog and refreshes without a NAS request', async ({ page }) => {
  let requests = 0;
  await page.route('**/archive/status', route => {
    requests += 1;
    return route.fulfill({ json: {
      enabled: true, verified: 123, pending: 2, failed: requests === 1 ? 1 : 0,
      last_verified_at: 1794660000, oldest_pending_at: null,
    }});
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.locator('#settings-archive-status')).toContainText('123 verified');
  await expect(page.locator('#settings-archive-status')).toContainText('Archive needs attention');
  await page.locator('#btn-archive-refresh').click();
  await expect(page.locator('#settings-archive-status')).toContainText('Archive enabled');
  await expect(page.locator('#btn-archive-refresh')).toHaveText('Refresh Status');
});
