import { defineConfig, devices } from '@playwright/test';

const port = 8765;
const baseURL = `http://127.0.0.1:${port}`;
const python = process.env.FRAMEART_E2E_PYTHON || 'python';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `${python} -m frameart.cli serve --host 127.0.0.1 --port ${port}`,
    url: `${baseURL}/health/ready`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      ...process.env,
      FRAMEART_AUTH_ENABLED: 'false',
      FRAMEART_DATA_DIR: '.e2e-data',
    },
  },
});
