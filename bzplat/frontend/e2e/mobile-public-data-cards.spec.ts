import { expect, test } from '@playwright/test'

import { monitorBrowser } from './helpers'

const BOT_ID = 901
let contestId = 0

test.beforeAll(async ({ request }) => {
  const health = await request.get('/api/health')
  expect(health.status(), await health.text()).toBe(200)
  expect((await health.json() as { qa_instance?: boolean }).qa_instance).toBe(true)

  const contests = await request.get('/api/contests')
  expect(contests.status(), await contests.text()).toBe(200)
  const rows = (await contests.json() as { contests?: Array<{ id: number; status: string }> }).contests || []
  for (const contest of rows.filter((item) => ['published', 'running', 'rest'].includes(item.status))) {
    const detail = await request.get(`/api/contests/${contest.id}`)
    if (!detail.ok()) continue
    const payload = await detail.json() as { pairings?: unknown[] }
    if (payload.pairings?.length) {
      contestId = contest.id
      break
    }
  }
  expect(contestId, 'E2E prerequisite missing: a non-finished contest with pairings').toBeGreaterThan(0)
})

async function mockBotDetail(page: import('@playwright/test').Page) {
  await page.route('**/api/comments?*', async (route) => {
    const url = new URL(route.request().url())
    if (url.searchParams.get('target_type') === 'bot' && url.searchParams.get('target_id') === String(BOT_ID)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"total":0}' })
    }
    return route.continue()
  })
  await page.route(`**/api/bots/${BOT_ID}/**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/profile')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          profile: {
            id: BOT_ID,
            name: 'mobile_alpha',
            display_name: '移动端 Alpha Bot',
            description: '响应式身份卡测试',
            game_id: 'gomoku',
            owner_id: 11,
            owner_name: 'owner_alpha',
            owner_display: 'Alpha 所有者',
            is_active: 1,
            current_version: 3,
            created_at: '2026-08-11T12:00:00+00:00',
            rating: 1600,
            rd: 80,
            wins: 4,
            losses: 2,
            draws: 1,
            rated_matches: 7,
            unique_opponents: 5,
            confidence_low: 1440,
            confidence_high: 1760,
            rank: 3,
            rank_total: 20,
            percentile: 90,
            ranking_min_matches: 5,
            ranking_progress: 1,
            ranking_eligible: true,
            rating_delta: 12,
            recent_delta_30d: 25,
            normal_completion_rate: 1,
            technical_failures: 0,
          },
        }),
      })
    }
    if (url.pathname.endsWith('/opponents')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"opponents":[]}' })
    }
    if (url.pathname.endsWith('/rating-history')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"history":[]}' })
    }
    if (url.pathname.endsWith('/matches')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total: 1,
          matches: [{
            id: 'mobile-card-match',
            status: 'completed',
            game_id: 'gomoku',
            match_type: 'challenge',
            winner: 1,
            bot_a_id: BOT_ID,
            bot_b_id: 902,
            bot_a: { id: BOT_ID, name: 'mobile_alpha', display_name: '移动端 Alpha Bot', owner_name: 'owner_alpha', owner_display: 'Alpha 所有者', is_human: false },
            bot_b: { id: 902, name: 'mobile_beta', display_name: '移动端 Beta Bot', owner_name: 'owner_beta', owner_display: 'Beta 所有者', is_human: false },
            created_at: '2026-08-11T12:30:00+00:00',
          }],
        }),
      })
    }
    return route.continue()
  })
}

test('mobile Bot history shows both identities, owners, nature and result without a wide table', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await mockBotDetail(page)
  await page.goto(`/#/bot/${BOT_ID}`)

  const card = page.getByTestId('bot-match-mobile-card')
  await expect(card).toBeVisible()
  await expect(card.locator('[data-match-participant]')).toHaveCount(2)
  await expect(card).toContainText('移动端 Alpha Bot')
  await expect(card).toContainText('Alpha 所有者')
  await expect(card).toContainText('移动端 Beta Bot')
  await expect(card).toContainText('Beta 所有者')
  await expect(card.locator('[data-match-nature="challenge"]')).toHaveText('用户挑战')
  await expect(card.getByText('负', { exact: true })).toBeVisible()
  const replay = card.getByRole('link', { name: '查看对局回放' })
  expect((await replay.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await expect(page.getByRole('table', { name: 'Bot 对局历史' })).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('desktop Bot history keeps the dense table', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 1024, height: 768 })
  await mockBotDetail(page)
  await page.goto(`/#/bot/${BOT_ID}`)

  await expect(page.getByRole('table', { name: 'Bot 对局历史' })).toBeVisible()
  await expect(page.getByTestId('bot-match-mobile-card')).toBeHidden()
  await monitor.expectClean()
})

test('mobile contest schedule cards keep both participants, round, status and result visible', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${contestId}`)

  const card = page.getByTestId('contest-schedule-mobile-card').first()
  await expect(card).toBeVisible()
  await expect(card.locator('[data-match-participant]')).toHaveCount(2)
  await expect(card).toContainText(/R\d+/)
  const result = card.locator('[data-pairing-result]')
  await expect(result).toContainText(/胜|平局|赛果待定|轮空晋级/)
  const resultText = (await result.textContent()) || ''
  if (resultText.endsWith(' 胜')) {
    const participantTexts = await card.locator('[data-match-participant]').allTextContents()
    expect(participantTexts.some((text) => text.includes(resultText.slice(0, -2)))).toBe(true)
  }
  const matchLink = card.getByRole('link', { name: '查看对局' })
  if (await matchLink.count()) expect((await matchLink.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await expect(page.getByRole('table', { name: '赛事对阵一览表' })).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('feedback uses a Chinese attachment trigger and readable file summary', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/feedback')

  const trigger = page.getByText('选择截图（最多 5 张）', { exact: true })
  await expect(trigger).toBeVisible()
  expect((await trigger.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await page.locator('#feedback-files').setInputFiles({
    name: '回放截图.png',
    mimeType: 'image/png',
    buffer: Buffer.alloc(2048),
  })
  const summary = page.getByTestId('feedback-file-summary')
  await expect(summary).toContainText('回放截图.png')
  await expect(summary).toContainText('2 KiB')
  const nativeInput = page.locator('#feedback-files')
  const nativeBox = await nativeInput.boundingBox()
  expect(nativeBox?.width).toBeLessThanOrEqual(1)
  expect(nativeBox?.height).toBeLessThanOrEqual(1)
  await expect(page.getByText(/Choose Files|No file chosen/)).toHaveCount(0)
  await monitor.expectClean()
})
