import test, { expect, type Page } from '@playwright/test'

import { loginThroughUi, monitorBrowser } from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'

/** 走 Python 在线编辑模式填好上传表单（无需真实文件即可触发提交链）。 */
async function fillUploadForm(page: Page, name: string) {
  const form = page.locator('form').filter({ has: page.locator('#upload-name') })
  await page.locator('#upload-name').fill(name)
  await form.getByRole('combobox').nth(1).click()
  await page.getByRole('option', { name: 'Python 源码', exact: true }).click()
  await page.locator('[data-testid="upload-mode-editor"]').click()
  await page.locator('#upload-source').fill('print("cooldown probe")\n')
  return form
}

// 生产实证：POST /api/bots 63% 失败（400×212 + 429×44），用户在失败后立即
// 连发重试撞限流。这里钉住冷却门的三段行为——限流错误可见、按钮禁用并
// 显示倒计时、Retry-After 到期后自动恢复可提交。
test('upload rejected by rate limit enters a visible cooldown, then self-releases', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  let intercepted = 0
  await page.route('**/api/bots', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    intercepted += 1
    await route.fulfill({
      status: 429,
      contentType: 'application/json',
      headers: { 'retry-after': '3' },
      body: JSON.stringify({
        detail: '请求过于频繁,请稍后再试',
        code: 'rate_limit_exceeded',
      }),
    })
  })

  const form = await fillUploadForm(page, `a_cool_${Date.now().toString(36)}`)
  await form.getByRole('button', { name: '上传', exact: true }).click()

  // 服务端错误物化为可读提示；冷却文案与倒计时出现。
  await expect(page.getByText('请求过于频繁,请稍后再试').first()).toBeVisible()
  const cooldown = page.getByTestId('upload-cooldown')
  await expect(cooldown).toBeVisible()
  await expect(cooldown).toContainText(/请在 [1-3] 秒后重试/)

  // 冷却期内提交按钮禁用（按钮文案切换为倒计时形态）。
  const coolingButton = form.getByRole('button', { name: /秒后可重试/ })
  await expect(coolingButton).toBeDisabled()

  // Retry-After: 3 到期后自动解除：倒计时消失、按钮恢复「上传」且可点。
  await expect(page.getByTestId('upload-cooldown')).toBeHidden({ timeout: 6_000 })
  const submit = form.getByRole('button', { name: '上传', exact: true })
  await expect(submit).toBeEnabled()
  // 客户端从不代替用户自动重发：整个用例只发出一次 POST。
  expect(intercepted).toBe(1)

  await monitor.expectClean([
    { kind: 'http', method: 'POST', status: 429, pathname: '/api/bots' },
  ])
})

// 400 是用户可就地修正的错误（改名/换文件即可），不得进入冷却。
test('upload rejected with a fixable 400 keeps the submit button available', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  await page.route('**/api/bots', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: { code: 'duplicate_name', message: '名称已被占用' },
      }),
    })
  })

  const form = await fillUploadForm(page, `a_dup_${Date.now().toString(36)}`)
  await form.getByRole('button', { name: '上传', exact: true }).click()

  await expect(page.getByText('名称已被占用').first()).toBeVisible()
  await expect(page.getByTestId('upload-cooldown')).toHaveCount(0)
  await expect(form.getByRole('button', { name: '上传', exact: true })).toBeEnabled()

  await monitor.expectClean([
    { kind: 'http', method: 'POST', status: 400, pathname: '/api/bots' },
  ])
})

// local-ai agents 轮询迁移到 useSingleFlightPolling 后：前台仍按 5s 周期
// 静默刷新；页面隐藏（后台标签页）自动暂停，恢复可见立即补一次刷新。
test('local agent status polling keeps refreshing and pauses while the tab is hidden', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, USER)

  let agentsRequests = 0
  await page.route('**/api/local-ai/agents', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback()
      return
    }
    agentsRequests += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [] }),
    })
  })

  await page.goto('/#/challenge')
  await expect(page.getByTestId('challenge-form')).toBeVisible()

  // 初次加载 + 至少一个 5s 轮询周期：轮询迁移后刷新能力不变。
  await expect.poll(() => agentsRequests, { timeout: 9_000 }).toBeGreaterThanOrEqual(2)

  // 切到后台标签页：两个轮询周期（>10s）内不得发出新请求。
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  const hiddenCount = agentsRequests
  await page.waitForTimeout(6_500)
  expect(agentsRequests).toBe(hiddenCount)

  // 恢复可见：立即补一次刷新，随后周期轮询继续。
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => agentsRequests, { timeout: 5_000 }).toBeGreaterThan(hiddenCount)

  await monitor.expectClean()
})
