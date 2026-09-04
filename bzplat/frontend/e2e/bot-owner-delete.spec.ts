import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = {
  id: 77,
  username: 'bot_delete_tester',
  email: 'bot_delete_tester@example.test',
  role: 'user',
  display_name: 'Bot 删除测试用户',
  is_active: 1,
}

interface MockBot {
  id: number
  name: string
  display_name: string
  game_id: string
  is_active: number
  is_ranked: number
  runnable: boolean
  current_version: number
  runtime_mode: string
}

async function mockApp(page: Page, initialBots?: MockBot[]) {
  const unexpectedBackendRequests: string[] = []
  const forbiddenMainRequests: string[] = []
  const deleteCalls: number[] = []
  let releaseDelete!: () => void
  const deleteGate = new Promise<void>((resolve) => { releaseDelete = resolve })
  let markDeleteStarted!: () => void
  const deleteStarted = new Promise<void>((resolve) => { markDeleteStarted = resolve })
  let mineReads = 0
  const mineReadPages: number[] = []
  let bots: MockBot[] = initialBots || [{
    id: 701,
    name: 'delete_me',
    display_name: '准备删除的 Bot',
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 0,
    runnable: true,
    current_version: 3,
    runtime_mode: 'traditional',
  }, {
    id: 702,
    name: 'stopped_bot',
    display_name: '保留的停用 Bot',
    game_id: 'gomoku',
    is_active: 0,
    is_ranked: 0,
    runnable: true,
    current_version: 2,
    runtime_mode: 'longrunning',
  }]

  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.port === '50380') forbiddenMainRequests.push(`${request.method()} ${url.pathname}`)
  })
  await page.route('https://fonts.googleapis.com/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/css', body: '' })
  })
  // Register the fallback first: Playwright evaluates the newest matching route
  // first, so the feature routes below remain authoritative.
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      expect(request.headers().authorization).toBeUndefined()
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ user: USER }),
      })
      return
    }
    if (url.pathname === '/api/notifications/unread-count') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
      return
    }
    if (url.pathname === '/api/local-ai/agents') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' })
      return
    }
    if (url.pathname === '/api/site/info') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      return
    }
    unexpectedBackendRequests.push(`${request.method()} ${url.pathname}${url.search}`)
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.route('**/api/bots/mine?**', async (route) => {
    mineReads += 1
    const url = new URL(route.request().url())
    const requestedPage = Number(url.searchParams.get('page') || 1)
    const perPage = Number(url.searchParams.get('per_page') || 20)
    const start = (requestedPage - 1) * perPage
    const visibleBots = bots.slice(start, start + perPage)
    mineReadPages.push(requestedPage)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: visibleBots, page: requestedPage, per_page: perPage, total: bots.length }),
    })
  })
  await page.route('**/api/bots/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const match = url.pathname.match(/^\/api\/bots\/(\d+)$/)
    if (request.method() !== 'DELETE' || !match) {
      await route.fallback()
      return
    }
    const botId = Number(match[1])
    deleteCalls.push(botId)
    markDeleteStarted()
    await deleteGate
    bots = bots.filter((bot) => bot.id !== botId)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, bot_id: botId, is_deleted: true }),
    })
  })

  return {
    deleteCalls,
    deleteStarted,
    forbiddenMainRequests,
    mineReadPages,
    releaseDelete,
    unexpectedBackendRequests,
    mineReads: () => mineReads,
  }
}

function mockBot(id: number, displayName: string): MockBot {
  return {
    id,
    name: `bot_${id}`,
    display_name: displayName,
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 0,
    runnable: true,
    current_version: 1,
    runtime_mode: 'traditional',
  }
}

test('owner delete removes the Bot while a reversibly stopped Bot stays manageable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)

  await page.goto('/#/my-bots')
  await expect(page.getByRole('link', { name: '准备删除的 Bot', exact: true })).toBeVisible()
  const stoppedRow = page.getByRole('link', { name: '保留的停用 Bot', exact: true })
    .locator('xpath=ancestor::li[1]')
  await expect(stoppedRow.getByText('已停用', { exact: true })).toBeVisible()
  await expect(stoppedRow.getByRole('button', { name: '启用', exact: true })).toBeEnabled()

  const deletedRow = page.getByRole('link', { name: '准备删除的 Bot', exact: true })
    .locator('xpath=ancestor::li[1]')
  await deletedRow.getByRole('button', { name: '管理 准备删除的 Bot', exact: true }).click()
  await page.getByRole('menuitem', { name: '删除 Bot', exact: true }).click()

  const dialog = page.getByRole('dialog', { name: '删除 Bot' })
  await expect(dialog).toContainText('从“我的 Bot”管理列表移除')
  await expect(dialog).toContainText('不能恢复，也不能用同名重新创建')
  await expect(dialog).toContainText('历史对局、评分、版本和 Bot 名称会继续保留')
  for (const buttonName of ['取消', '从管理列表删除']) {
    const button = dialog.getByRole('button', { name: buttonName, exact: true })
    // Radix opens the dialog with a brief zoom-in animation. Measure the
    // settled hit target instead of mistaking an in-flight transform for its
    // layout size on Chromium/Firefox/WebKit.
    await expect.poll(async () => (
      (await button.boundingBox())?.height ?? 0
    )).toBeGreaterThanOrEqual(44)
  }

  const deleted = page.waitForResponse((response) => (
    response.request().method() === 'DELETE'
    && new URL(response.url()).pathname === '/api/bots/701'
  ))
  await dialog.getByRole('button', { name: '从管理列表删除', exact: true }).click()
  await network.deleteStarted

  const deletingButton = deletedRow.getByRole('button', { name: '准备删除的 Bot 删除中', exact: true })
  await expect(deletedRow.getByRole('status')).toHaveText('删除中…')
  await expect(deletingButton).toBeDisabled()
  await expect(deletingButton).toHaveAttribute('aria-busy', 'true')
  expect(network.deleteCalls).toEqual([701])

  network.releaseDelete()
  expect((await deleted).status()).toBe(200)

  await expect(page.getByText('准备删除的 Bot 已从管理列表删除', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '准备删除的 Bot', exact: true })).toHaveCount(0)
  await expect(stoppedRow).toBeVisible()
  await expect(stoppedRow.getByText('已停用', { exact: true })).toBeVisible()
  await expect(stoppedRow.getByRole('button', { name: '启用', exact: true })).toBeEnabled()
  await expect.poll(network.mineReads).toBeGreaterThanOrEqual(2)
  expect(network.deleteCalls).toEqual([701])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('deleting the only Bot on page two returns to page one without reloading the empty page', async ({ page }) => {
  const firstPageBots = Array.from({ length: 20 }, (_, index) => (
    mockBot(800 + index, `第一页 Bot ${index + 1}`)
  ))
  const pageTwoBot = mockBot(899, '第二页唯一 Bot')
  const network = await mockApp(page, [...firstPageBots, pageTwoBot])
  const monitor = monitorBrowser(page)

  await page.goto('/#/my-bots')
  await page.getByRole('button', { name: '第 2 页', exact: true }).click()
  await expect(page.getByText('第 2 页', { exact: true })).toBeVisible()
  const targetRow = page.getByRole('link', { name: pageTwoBot.display_name, exact: true })
    .locator('xpath=ancestor::li[1]')
  await targetRow.getByRole('button', { name: `管理 ${pageTwoBot.display_name}`, exact: true }).click()
  await page.getByRole('menuitem', { name: '删除 Bot', exact: true }).click()

  const readsBeforeDelete = network.mineReadPages.length
  const deleted = page.waitForResponse((response) => (
    response.request().method() === 'DELETE'
    && new URL(response.url()).pathname === `/api/bots/${pageTwoBot.id}`
  ))
  await page.getByRole('dialog', { name: '删除 Bot' })
    .getByRole('button', { name: '从管理列表删除', exact: true })
    .click()
  await network.deleteStarted
  network.releaseDelete()
  expect((await deleted).status()).toBe(200)

  await expect(page.getByText('第 1 页', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '第一页 Bot 1', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: pageTwoBot.display_name, exact: true })).toHaveCount(0)
  await expect.poll(() => network.mineReadPages.length).toBeGreaterThan(readsBeforeDelete)
  expect(network.mineReadPages.slice(readsBeforeDelete)).toEqual([1])
  expect(network.deleteCalls).toEqual([pageTwoBot.id])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})
