import { defineConfig } from '@playwright/test'

const baseURL = process.env.BZ_E2E_BASE_URL || 'http://127.0.0.1:5173'
const parsed = new URL(baseURL)

// 50380 是 AGENTS.md 明确保留给 main + 主库的线上端口。浏览器回归会写业务数据，
// 除非维护者显式确认，否则配置加载阶段即拒绝运行，避免一次误操作污染主库。
if (parsed.port === '50380') {
  throw new Error(
    'Refusing to run Playwright against port 50380. Start the isolated worktree stack and set BZ_E2E_BASE_URL.',
  )
}

export default defineConfig({
  testDir: './e2e',
  outputDir: 'test-results',
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],
  use: {
    baseURL,
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
})
