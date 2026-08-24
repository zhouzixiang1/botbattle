import { expect, test, type Locator, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = {
  id: 42,
  username: 'ranked_bot_tester',
  email: 'ranked_bot_tester@example.test',
  role: 'user',
  display_name: '排行榜测试用户',
  is_active: 1,
}

interface MockBot {
  id: number
  name: string
  display_name: string
  owner_id: number
  owner_name: string
  game_id: string
  is_active: number
  is_ranked: number
  runnable: boolean
  current_version: number
  runtime_mode: string
}

async function mockApp(page: Page) {
  const unexpectedBackendRequests: string[] = []
  const forbiddenMainRequests: string[] = []

  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.port === '50380') forbiddenMainRequests.push(`${request.method()} ${url.pathname}`)
  })
  await page.route('https://fonts.googleapis.com/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/css', body: '' })
  })
  await page.addInitScript((user) => {
    localStorage.setItem('bzplat_token', 'ranked-bot-test-token')
    localStorage.setItem('bzplat_user', JSON.stringify(user))
  }, USER)
  // Feature-specific routes are registered after this fallback. Playwright
  // evaluates the most recently registered matching route first.
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
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
    if (url.pathname === '/api/comments') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"comments":[],"count":0,"total":0}',
      })
      return
    }
    if (url.pathname === '/api/likes/status') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"liked":false,"count":0}',
      })
      return
    }
    unexpectedBackendRequests.push(`${request.method()} ${url.pathname}${url.search}`)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{}',
    })
  })

  return { unexpectedBackendRequests, forbiddenMainRequests }
}

async function expectTouchTarget(locator: Locator, label: string) {
  // DialogContent enters with a short scale animation. Measure the settled
  // interactive geometry, not a transient 95% transform frame.
  await locator.evaluate(async (element) => {
    const dialog = element.closest('[role="dialog"]')
    if (!dialog) return
    await Promise.all(dialog.getAnimations().map((animation) => (
      animation.finished.catch(() => undefined)
    )))
  })
  const box = await locator.boundingBox()
  expect(box, `${label} has no box`).not.toBeNull()
  expect(box!.width, `${label} width`).toBeGreaterThanOrEqual(44)
  expect(box!.height, `${label} height`).toBeGreaterThanOrEqual(44)
}

test('My Bots switches and exits the one ranked Bot per game with accessible confirmation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)
  const calls: Array<{ method: string; botId: number }> = []
  const bots: MockBot[] = [{
    id: 101,
    name: 'holdem_alpha',
    display_name: '德州 Alpha',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 1,
    runnable: true,
    current_version: 3,
    runtime_mode: 'traditional',
  }, {
    id: 102,
    name: 'holdem_beta',
    display_name: '德州 Beta',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 0,
    runnable: true,
    current_version: 2,
    runtime_mode: 'traditional',
  }, {
    id: 103,
    name: 'gomoku_alpha',
    display_name: '五子棋 Alpha',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'gomoku',
    is_active: 1,
    is_ranked: 1,
    runnable: true,
    current_version: 1,
    runtime_mode: 'longrunning',
  }, {
    id: 104,
    name: 'pencil_inactive',
    display_name: '点格棋未启用',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'pencil',
    is_active: 0,
    is_ranked: 0,
    runnable: true,
    current_version: 1,
    runtime_mode: 'traditional',
  }]

  await page.route('**/api/bots/mine?**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots, page: 1, per_page: 20, total: bots.length }),
    })
  })
  await page.route('**/api/bots/*/ranking', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const botId = Number(url.pathname.split('/')[3])
    const selected = bots.find((bot) => bot.id === botId)
    expect(selected).toBeTruthy()
    calls.push({ method: request.method(), botId })
    if (request.method() === 'PUT' && calls.length === 3) {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            code: 'ranking_busy',
            message: '当前排位 Bot 仍有进行中或待结算的计分对局',
          },
        }),
      })
      return
    }
    if (request.method() === 'PUT') {
      for (const bot of bots) {
        if (bot.game_id === selected!.game_id) bot.is_ranked = bot.id === botId ? 1 : 0
      }
    } else if (request.method() === 'DELETE') {
      selected!.is_ranked = 0
    } else {
      throw new Error(`unexpected ranking method: ${request.method()}`)
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        bot: selected,
        cancelled_queued_jobs: request.method() === 'PUT' ? 2 : 1,
      }),
    })
  })

  await page.goto('/#/my-bots')
  const row = (name: string) => page.getByRole('link', { name, exact: true }).locator('xpath=ancestor::li[1]')
  const alpha = row('德州 Alpha')
  const beta = row('德州 Beta')
  const gomoku = row('五子棋 Alpha')
  const inactive = row('点格棋未启用')

  await expect(page.locator('main')).toContainText('切换会取消旧计分排队，进行中或待结算时暂不可操作')
  await expect(alpha.getByText('排行榜 Bot', { exact: true })).toBeVisible()
  await expect(beta.getByText('未参榜', { exact: true })).toBeVisible()
  await expect(gomoku.getByText('排行榜 Bot', { exact: true })).toBeVisible()
  const switchButton = beta.getByRole('button', { name: '派遣参榜', exact: true })
  const exitAlphaButton = alpha.getByRole('button', { name: '退出排名', exact: true })
  const disabledButton = inactive.getByRole('button', { name: '派遣参榜', exact: true })
  await expectTouchTarget(switchButton, 'mobile dispatch action')
  await expectTouchTarget(exitAlphaButton, 'mobile exit action')
  await expectTouchTarget(disabledButton, 'mobile disabled dispatch action')
  await expect(disabledButton).toBeDisabled()
  await expect(inactive).toContainText('先启用此 Bot 才能派遣参榜')

  await switchButton.focus()
  await page.keyboard.press('Enter')
  const switchDialog = page.getByRole('dialog', { name: '切换排行榜 Bot' })
  await expect(switchDialog).toContainText('历史已完成评分保留')
  await expect(switchDialog).toContainText('尚未开始的旧计分排队会取消')
  await expect(switchDialog).toContainText('进行中或待结算的计分对局，暂不能切换')
  await expectTouchTarget(switchDialog.getByRole('button', { name: '取消', exact: true }), 'mobile cancel confirmation')
  await expectTouchTarget(switchDialog.getByRole('button', { name: '确认切换', exact: true }), 'mobile switch confirmation')
  await page.keyboard.press('Escape')
  await expect(switchDialog).toBeHidden()
  await expect(switchButton).toBeFocused()
  expect(calls).toEqual([])

  await switchButton.click()
  await page.getByRole('dialog', { name: '切换排行榜 Bot' })
    .getByRole('button', { name: '确认切换', exact: true })
    .click()
  await expect(beta.getByText('排行榜 Bot', { exact: true })).toBeVisible()
  await expect(alpha.getByText('未参榜', { exact: true })).toBeVisible()
  await expect(gomoku.getByText('排行榜 Bot', { exact: true })).toBeVisible()
  await expect(page.getByText('已将 德州 Beta 派遣到德州扑克排行榜；已取消 2 个旧计分排队', { exact: true })).toBeVisible()
  expect(calls).toEqual([{ method: 'PUT', botId: 102 }])

  await beta.getByRole('button', { name: '退出排名', exact: true }).click()
  const exitDialog = page.getByRole('dialog', { name: '退出排行榜' })
  await expect(exitDialog).toContainText('历史已完成评分与对局记录保留')
  await expect(exitDialog).toContainText('尚未开始的旧计分排队会取消')
  await expect(exitDialog).toContainText('进行中或待结算的计分对局，暂不能退出')
  await exitDialog.getByRole('button', { name: '确认退出', exact: true }).click()
  await expect(beta.getByText('未参榜', { exact: true })).toBeVisible()
  await expect(page.getByText('德州 Beta 已退出德州扑克排行榜；已取消 1 个旧计分排队', { exact: true })).toBeVisible()
  expect(calls).toEqual([
    { method: 'PUT', botId: 102 },
    { method: 'DELETE', botId: 102 },
  ])

  await beta.getByRole('button', { name: '派遣参榜', exact: true }).click()
  await page.getByRole('dialog', { name: '派遣排行榜 Bot' })
    .getByRole('button', { name: '确认派遣', exact: true })
    .click()
  await expect(page.getByText('当前排位 Bot 仍有进行中或待结算的计分对局', { exact: true })).toBeVisible()
  await expect(page.locator('main')).not.toContainText('"code":"ranking_busy"')
  await expect(beta.getByText('未参榜', { exact: true })).toBeVisible()
  expect(calls).toEqual([
    { method: 'PUT', botId: 102 },
    { method: 'DELETE', botId: 102 },
    { method: 'PUT', botId: 102 },
  ])

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean([{
    kind: 'http',
    method: 'PUT',
    status: 409,
    pathname: '/api/bots/102/ranking',
  }])
})

test('Bot detail separates dispatch status from historical ranking eligibility', async ({ page }) => {
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)
  const profile = {
    id: 901,
    name: 'historical_ranked_sample',
    display_name: '历史高分 Bot',
    description: '已退出排行榜但保留历史评分',
    game_id: 'gomoku',
    owner_id: 77,
    owner_name: 'historical_owner',
    is_active: 1,
    is_ranked: 0,
    current_version: 4,
    rating: 1888,
    rd: 55,
    wins: 20,
    losses: 5,
    draws: 2,
    rated_matches: 27,
    unique_opponents: 14,
    confidence_low: 1780,
    confidence_high: 1996,
    rank: 2,
    rank_total: 20,
    percentile: 94.7,
    ranking_min_matches: 10,
    ranking_progress: 1,
    ranking_eligible: true,
    rating_delta: 8,
    recent_delta_30d: 21,
    normal_completion_rate: 1,
    technical_failures: 0,
  }
  await page.route('**/api/bots/901/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const body = path.endsWith('/profile')
      ? { profile }
      : path.endsWith('/matches')
        ? { matches: [], total: 0 }
        : path.endsWith('/opponents')
          ? { opponents: [], page: 1, per_page: 20, total: 0 }
          : path.endsWith('/rating-history')
            ? { history: [] }
            : path.endsWith('/favorite-status')
              ? { favorited: false, favorite_count: 0 }
              : null
    if (body == null) throw new Error(`unexpected Bot detail path: ${path}`)
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })

  await page.goto('/#/bot/901')
  const main = page.locator('main')
  await expect(main.getByText('未参加排行榜', { exact: true }).first()).toBeVisible()
  await expect(main).toContainText('历史评分保留')
  await expect(main).not.toContainText('公开排名 #2')

  Object.assign(profile, {
    is_ranked: 1,
    rated_matches: 4,
    wins: 3,
    losses: 1,
    draws: 0,
    rank: null,
    percentile: null,
    ranking_progress: 0.4,
    ranking_eligible: false,
  })
  await page.reload()
  await expect(main).toContainText('参榜中 · 资格 4/10')
  await expect(main).toContainText('暂未获得公开名次')

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('Leaderboard explains the dispatch rule and labels unrated execution snapshots', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 })
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)
  await page.route('**/api/leaderboard?**', async (route) => {
    const gameId = new URL(route.request().url()).searchParams.get('game_id')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        leaderboard: [],
        game_id: gameId,
        ranking_min_matches: 10,
        summary: { total: 0, eligible: 0, sample: 0, last_rated_at: null },
        page: 1,
        per_page: 50,
        total: 0,
      }),
    })
  })
  await page.route('**/api/execution-queue', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        dispatcher: { state: 'running', accepting: true, auto_enabled: true, pause_reason: '' },
        capacity: {
          match_slots: { used: 1, capacity: 1 },
          sandbox_units: { used: 2, capacity: 2 },
          running_matches: 1,
        },
        active: [{
          public_id: 'unranked-snapshot',
          request_id: 'unranked-snapshot',
          source: 'manual',
          status: 'running',
          game_id: 'holdem',
          match_type: 'manual',
          match_id: 'practice-match',
          sandbox_units: 2,
          rated: false,
          rating_reason: 'ranked_bot_not_selected',
          retryable: false,
          cancel_requested: false,
          reason: '',
        }],
        queued: [],
        queued_count: 0,
      }),
    })
  })

  await page.goto('/#/leaderboard')
  const main = page.locator('main')
  await expect(main).toContainText('每个账号每款游戏最多派遣一个 Bot')
  await expect(main).toContainText('仅展示当前派遣参榜的 Bot')
  await expect(main).toContainText('该游戏暂无已派遣参榜 Bot')
  await expect(page.getByTestId('execution-queue-panel')).toContainText('至少一方未派遣参榜，不计平台排行榜')

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('Challenge keeps ranked and practice Bots selectable and marks both kinds', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)
  const botRequests: URL[] = []
  const bots: MockBot[] = [{
    id: 201,
    name: 'practice_alpha',
    display_name: '练习甲',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 0,
    runnable: true,
    current_version: 1,
    runtime_mode: 'traditional',
  }, {
    id: 202,
    name: 'ranked_alpha',
    display_name: '参榜甲',
    owner_id: USER.id,
    owner_name: USER.username,
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 1,
    runnable: true,
    current_version: 1,
    runtime_mode: 'traditional',
  }, {
    id: 203,
    name: 'ranked_opponent',
    display_name: '参榜对手',
    owner_id: 77,
    owner_name: 'opponent_owner',
    game_id: 'holdem',
    is_active: 1,
    is_ranked: 1,
    runnable: true,
    current_version: 1,
    runtime_mode: 'traditional',
  }]
  await page.route('**/api/bots/public?**', async (route) => {
    const url = new URL(route.request().url())
    botRequests.push(url)
    const ownerId = url.searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, page: 1, per_page: 50, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"versions":[],"current_version":1}',
    })
  })

  await page.goto('/#/challenge')
  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  let picker = page.getByRole('dialog', { name: /^选择我的 Bot/ })
  await expect(picker).toContainText('排行榜 Bot 与练习 Bot 均可挑战')
  const practiceChoice = picker.getByRole('button', { name: /练习甲.*练习 Bot/ })
  const rankedChoice = picker.getByRole('button', { name: /参榜甲.*排行榜 Bot/ })
  await expect(practiceChoice).toBeEnabled()
  await expect(rankedChoice).toBeEnabled()
  await expectTouchTarget(practiceChoice, 'practice Bot picker row')
  await practiceChoice.click()
  await expect(page.locator('main').getByRole('button', { name: /练习甲.*练习 Bot/ })).toBeVisible()

  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  picker = page.getByRole('dialog', { name: /^选择对手 Bot/ })
  const opponentChoice = picker.getByRole('button', { name: /参榜对手.*排行榜 Bot/ })
  await expect(opponentChoice).toBeEnabled()
  await opponentChoice.click()
  await expect(page.locator('main').getByRole('button', { name: /参榜对手.*排行榜 Bot/ })).toBeVisible()
  expect(botRequests.length).toBeGreaterThanOrEqual(2)
  expect(botRequests.every((url) => !url.searchParams.has('is_ranked'))).toBe(true)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('Match viewer exposes the frozen unranked reason instead of a generic label', async ({ page }) => {
  const network = await mockApp(page)
  const monitor = monitorBrowser(page)
  const matchId = 'ranked-bot-not-selected-match'
  const detailCancellations: string[] = []
  page.on('requestfailed', (request) => {
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === `/api/matches/${matchId}`) {
      detailCancellations.push(request.failure()?.errorText || '')
    }
  })
  const match = {
    id: matchId,
    game_id: 'gomoku',
    status: 'completed',
    reason: 'five',
    winner: 0,
    match_type: 'challenge',
    rated: false,
    rating_reason: 'ranked_bot_not_selected',
    rating_settled: false,
    bot_a: { id: 301, name: 'practice_a', display_name: '练习 A', owner_name: 'owner_a' },
    bot_b: { id: 302, name: 'ranked_b', display_name: '参榜 B', owner_name: 'owner_b' },
    result: { rounds_played: 9, deltas: [1, -1], normalized_delta: 1 },
  }
  await page.route(`**/api/matches/${matchId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${matchId}/replay`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match_id: matchId,
        events: [{ type: 'match_end', winner: 0, reason: 'five' }],
        event_count: 1,
        updated_at: '2026-08-18T12:00:00Z',
      }),
    })
  })
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match }),
    })
  })

  await page.goto(`/#/match/${matchId}`)
  await expect(page.getByTestId('rating-state')).toHaveText('未派遣排行榜 Bot · 不计平台排行榜')

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean(detailCancellations.map((errorText) => ({
    kind: 'requestfailed' as const,
    method: 'GET',
    pathname: `/api/matches/${matchId}`,
    errorText,
  })))
})
