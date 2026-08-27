import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = {
  id: 81,
  username: 'series-organizer',
  display_name: '系列赛组织者',
  email: 'series-organizer@example.test',
  role: 'organizer',
  email_verified: 1,
}

const templates = [
  {
    id: 'holdem_dup_rr',
    name: '德州：复式单循环（公平优先，≤12 人）',
    summary: '每对 Bot 使用同一副牌交换座位。',
    game_id: 'holdem',
    recommended: true,
    stages: [{ key: 'dup_rr', type: 'round_robin', duplicate: true }],
    games_per_pair_config: { default: 1, min: 1, max: 10 },
  },
  {
    id: 'holdem_rr',
    name: '德州：单循环（小规模）',
    summary: '每对 Bot 进行独立物理对局。',
    game_id: 'holdem',
    stages: [{ key: 'rr', type: 'round_robin' }],
    games_per_pair_config: { default: 1, min: 1, max: 10 },
  },
  {
    id: 'holdem_prelim_swiss',
    name: '德州：预赛（大规模瑞士快速排名）',
    summary: '瑞士轮按阶段设置交锋场数和额外轮数。',
    game_id: 'holdem',
    stages: [{ key: 'prelim', type: 'swiss' }],
    stage_series_configs: [{
      stage_key: 'prelim',
      label: '瑞士预赛',
      games_per_pair: { default: 2, allowed_values: [1, 2, 4] },
      swiss_extra_rounds: { default: 2, min: 0, max: 4 },
    }],
  },
  {
    id: 'holdem_final_ranked',
    name: '德州：决赛（循环→Top8）',
    summary: '决赛资格循环和 Top8 分别设置。',
    game_id: 'holdem',
    stages: [
      { key: 'qualify', type: 'round_robin' },
      { key: 'final8', type: 'double_round_robin' },
    ],
    stage_series_configs: [
      {
        stage_key: 'qualify',
        label: '决赛资格循环',
        games_per_pair: { default: 2, allowed_values: [1, 2, 4] },
      },
      {
        stage_key: 'final8',
        label: 'Top 8 决胜循环',
        games_per_pair: { default: 4, allowed_values: [2, 4, 6, 8, 10] },
      },
    ],
  },
  {
    id: 'holdem_knockout',
    name: '德州：单败淘汰',
    summary: '不提供系列设置。',
    game_id: 'holdem',
    stages: [{ key: 'knockout', type: 'single_elimination' }],
  },
]

async function mockOrganizerContestApi(page: Page) {
  let createdBody: Record<string, unknown> | null = null
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: USER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      const game = url.searchParams.get('game')
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ templates: game === 'holdem' ? templates : [] }),
      })
    }
    if (url.pathname === '/api/contests' && request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"contests":[],"page":1,"per_page":20,"total":0}',
      })
    }
    if (url.pathname === '/api/contests' && request.method() === 'POST') {
      createdBody = request.postDataJSON() as Record<string, unknown>
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ contest: { id: 9401, ...createdBody } }),
      })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
  return { createdBody: () => createdBody }
}

test('Holdem compatible templates expose 1–10 physical matches and submit games_per_pair', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const captured = await mockOrganizerContestApi(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/contests')

  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  const seriesField = form.getByRole('group').filter({ hasText: '每对选手交手场数' })
  await expect(seriesField).toBeVisible()
  await expect(form.getByRole('combobox', { name: '每对选手交手场数' })).toBeVisible()
  await expect(seriesField).toContainText('1 场复式对局 · 正常完成时 2 局计分')
  await expect(seriesField).toContainText('对局数不是 leg 数')

  const seriesSelect = seriesField.getByRole('combobox')
  expect((await seriesSelect.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await seriesSelect.click()
  await expect(page.getByRole('option')).toHaveCount(10)
  await expect(page.getByRole('option', { name: '1 场', exact: true })).toBeVisible()
  await page.getByRole('option', { name: '4 场', exact: true }).click()
  await expect(seriesField).toContainText('4 场复式对局 · 正常完成时 8 局计分')

  const templateSelect = form.getByRole('combobox').nth(1)
  await templateSelect.click()
  await page.getByRole('option', { name: '德州：单败淘汰', exact: true }).click()
  await expect(form.getByText('每对选手交手场数', { exact: true })).toHaveCount(0)

  await templateSelect.click()
  await page.getByRole('option', { name: '德州：单循环（小规模）', exact: true }).click()
  const plainSeriesField = form.getByRole('group').filter({ hasText: '每对选手交手场数' })
  await expect(plainSeriesField).toContainText('每对选手进行 1 场独立实际对局')
  await expect(plainSeriesField).not.toContainText('leg')
  await expect(plainSeriesField).not.toContainText('正常完成时')

  await templateSelect.click()
  await page.getByRole('option', { name: /德州：复式单循环/ }).click()
  const restoredSeriesField = form.getByRole('group').filter({ hasText: '每对选手交手场数' })
  await expect(restoredSeriesField).toContainText('1 场复式对局 · 正常完成时 2 局计分')
  await restoredSeriesField.getByRole('combobox').click()
  await page.getByRole('option', { name: '4 场', exact: true }).click()

  await page.locator('#contest-title').fill('四场复式邀请赛')
  await form.getByRole('button', { name: '创建赛事', exact: true }).click()
  await expect(page.getByText('赛事创建成功', { exact: true })).toBeVisible()
  expect(captured.createdBody()).toMatchObject({
    title: '四场复式邀请赛',
    game_id: 'holdem',
    template_id: 'holdem_dup_rr',
    games_per_pair: 4,
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('Holdem preliminary and final templates submit independent stage fairness settings', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const captured = await mockOrganizerContestApi(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/contests')

  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  const templateSelect = form.getByRole('combobox').nth(1)
  await templateSelect.click()
  await page.getByRole('option', { name: '德州：预赛（大规模瑞士快速排名）', exact: true }).click()

  const prelim = form.getByRole('group', { name: '瑞士预赛' })
  await expect(prelim).toContainText('参赛人数待定')
  await expect(prelim).toContainText('报名人数确定后')
  await expect(prelim.getByRole('combobox', { name: '每组交锋' })).toHaveText('2 场')
  await expect(prelim.getByRole('combobox', { name: '额外瑞士轮' })).toHaveText('增加 2 轮')
  expect((await prelim.getByRole('combobox', { name: '每组交锋' }).boundingBox())?.height).toBeGreaterThanOrEqual(44)

  await templateSelect.click()
  await page.getByRole('option', { name: '德州：决赛（循环→Top8）', exact: true }).click()
  const qualify = form.getByRole('group', { name: '决赛资格循环' })
  const final8 = form.getByRole('group', { name: 'Top 8 决胜循环' })
  await expect(qualify.getByRole('combobox', { name: '每组交锋' })).toHaveText('2 场')
  await expect(final8.getByRole('combobox', { name: '每组交锋' })).toHaveText('4 场')
  await final8.getByRole('combobox', { name: '每组交锋' }).click()
  await page.getByRole('option', { name: '8 场', exact: true }).click()

  await page.locator('#contest-title').fill('公平决赛')
  await form.getByRole('button', { name: '创建赛事', exact: true }).click()
  await expect(page.getByText('赛事创建成功', { exact: true })).toBeVisible()
  expect(captured.createdBody()).toMatchObject({
    title: '公平决赛',
    game_id: 'holdem',
    template_id: 'holdem_final_ranked',
    stage_series_settings: {
      qualify: { games_per_pair: 2 },
      final8: { games_per_pair: 8 },
    },
  })
  expect(captured.createdBody()).not.toHaveProperty('games_per_pair')
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})
