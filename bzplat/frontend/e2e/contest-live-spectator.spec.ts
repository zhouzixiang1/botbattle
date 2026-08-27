import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

function pairing({
  id,
  status,
  displayStatus,
  matchId,
  seriesIndex,
  winner = null,
  botAId,
  botBId,
  seriesSize = 4,
  isBye = false,
  seriesSummary,
}: {
  id: number
  status: 'pending' | 'running' | 'completed'
  displayStatus: 'pending' | 'queued' | 'running' | 'completed'
  matchId: string | null
  seriesIndex: number
  winner?: number | null
  botAId?: number
  botBId?: number | null
  seriesSize?: number
  isBye?: boolean
  seriesSummary?: {
    series_size: number
    completed_matches: number
    game_points_a: number
    game_points_b: number
    normalized_delta_a: number
    settled: boolean
    standings_points_a: number | null
    standings_points_b: number | null
  }
}) {
  return {
    id,
    round_num: 2,
    bot_a_id: botAId ?? 101 + id,
    bot_b_id: botBId === undefined ? 201 + id : botBId,
    scheduled_at: '2026-08-28T12:00:00+08:00',
    started_at: displayStatus === 'running' || displayStatus === 'completed'
      ? '2026-08-27T12:01:00+08:00'
      : null,
    ended_at: displayStatus === 'completed' ? '2026-08-27T12:08:00+08:00' : null,
    match_id: matchId,
    status,
    display_status: displayStatus,
    stage_idx: 0,
    stage_key: 'rr',
    group_id: null,
    bracket_slot: id,
    series_index: seriesIndex,
    series_size: seriesSize,
    bot_a_name: `alpha-${id}`,
    bot_a_display: `Alpha ${id}`,
    bot_b_name: `beta-${id}`,
    bot_b_display: `Beta ${id}`,
    owner_a_name: `owner-alpha-${id}`,
    owner_a_display: `Alpha Owner ${id}`,
    owner_b_name: `owner-beta-${id}`,
    owner_b_display: `Beta Owner ${id}`,
    match_winner: winner,
    is_bye: isBye,
    bye: isBye,
    series_summary: seriesSummary,
  }
}

function liveSnapshot(overrides: {
  id?: number
  title?: string
  status?: 'published' | 'running' | 'rest' | 'finished' | 'cancelled'
  showcase?: boolean
  immutable?: boolean
  officialResultsReady?: boolean
} = {}) {
  const status = overrides.status ?? 'running'
  return {
    contest: {
      id: overrides.id ?? 42,
      title: overrides.title ?? '秋季德州扑克联赛',
      game_id: 'holdem',
      status,
      showcase: overrides.showcase ?? false,
      immutable: overrides.immutable ?? false,
      official_results_ready: overrides.officialResultsReady ?? false,
      starts_at: '2026-08-27T12:00:00+08:00',
      ends_at: status === 'finished' ? '2026-08-27T14:00:00+08:00' : null,
      rest_ends_at: status === 'rest' ? '2026-08-27T13:00:00+08:00' : null,
    },
    stage: { index: 0, key: 'rr', label: '单循环阶段', type: 'round_robin' },
    series: {
      games_per_pair: 4,
      duplicate: true,
      scoring_legs_per_match: 2,
      scoring_legs_per_pair: 8,
    },
    progress: {
      completed: status === 'finished' ? 12 : 2,
      total: 12,
      running: status === 'running' ? 1 : 0,
      pending: status === 'finished' ? 0 : 9,
    },
    active: status === 'running'
      ? [pairing({ id: 1, status: 'running', displayStatus: 'running', matchId: 'live-table-1', seriesIndex: 2 })]
      : [],
    upcoming: status === 'finished'
      ? []
      : [
          pairing({ id: 2, status: 'running', displayStatus: 'queued', matchId: 'queued-table-2', seriesIndex: 3 }),
          pairing({ id: 3, status: 'pending', displayStatus: 'pending', matchId: null, seriesIndex: 4 }),
        ],
    recent: [
      pairing({ id: 4, status: 'completed', displayStatus: 'completed', matchId: 'recent-match-4', seriesIndex: 1 }),
    ],
    standings: [
      { rank: 1, bot_id: 101, bot_name: 'river-guard', points: 9, wins: 3, draws: 0, losses: 1, byes: 0, delta_total: 42, group_id: 'A' },
      { rank: 1, bot_id: 102, bot_name: 'turn-probe', points: 7, wins: 2, draws: 1, losses: 1, byes: 0, delta_total: 18, group_id: 'B' },
    ],
    updated_at: '2026-08-27T12:10:30+08:00',
    generated_at: '2026-08-27T12:10:32+08:00',
  }
}

async function installLiveApi(
  page: Page,
  snapshot: ReturnType<typeof liveSnapshot>,
  { holdFirst = false }: { holdFirst?: boolean } = {},
) {
  const apiPaths: string[] = []
  let liveReads = 0
  let releaseFirst = () => undefined
  const firstGate = holdFirst
    ? new Promise<void>((resolve) => { releaseFirst = resolve })
    : Promise.resolve()

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    apiPaths.push(pathname)
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === '/api/contests/42/live') {
      liveReads += 1
      if (liveReads === 1) await firstGate
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(snapshot),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{}',
    })
  })

  return {
    apiPaths,
    liveReads: () => liveReads,
    releaseFirst,
  }
}

test('running spectator prioritizes live tables and polls only the live projection', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const api = await installLiveApi(page, liveSnapshot(), { holdFirst: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/contests/42/live')

  await expect(page.getByRole('status', { name: '正在加载赛事直播' })).toBeVisible()
  api.releaseFirst()

  await expect(page.getByRole('heading', { name: '秋季德州扑克联赛' })).toBeVisible()
  await expect(page.getByTestId('contest-live-table')).toHaveCount(1)
  await expect(page.getByText('4 场复式对局 · 正常完成时 8 局计分')).toBeVisible()
  await expect(page.getByText('2 / 12 场 · 17%')).toBeVisible()
  await expect(page.getByText('1 桌进行中 · 9 场待赛')).toBeVisible()
  await expect(page.getByText('已派桌，等待启动')).toBeVisible()
  await expect(page.getByText('第 2/4 场')).toBeVisible()
  await expect(page.getByText('复式对局已裁决')).toBeVisible()
  await expect(page.getByText('平局', { exact: true })).toHaveCount(0)
  await expect(page.getByText('A组 · 1', { exact: true })).toBeVisible()
  await expect(page.getByText('B组 · 1', { exact: true })).toBeVisible()
  await expect(page.getByTestId('contest-live-sync-status')).toHaveText('已自动更新 · 12:10:32')
  await expect(page.getByTestId('contest-live-sync-status')).not.toContainText('2026-08-28')

  const watch = page.getByRole('link', { name: '进入第 1 桌观赛' })
  await expect(watch).toHaveAttribute('href', '#/match/live-table-1')
  expect((await watch.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => {
    const active = document.querySelector('#active-tables-title')
    const standings = document.querySelector('#live-standings-title')
    return Boolean(active && standings && (active.compareDocumentPosition(standings) & Node.DOCUMENT_POSITION_FOLLOWING))
  })).toBe(true)

  await expect.poll(api.liveReads, { timeout: 3_500 }).toBeGreaterThanOrEqual(2)
  expect(api.apiPaths.filter((path) => path.startsWith('/api/contests/')))
    .toEqual(Array(api.liveReads()).fill('/api/contests/42/live'))
  expect(api.apiPaths.filter((path) => path !== '/api/auth/me'))
    .toEqual(Array(api.liveReads()).fill('/api/contests/42/live'))
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('series scorecards aggregate physical games and keep a Swiss bye distinct', async ({ page }) => {
  const pendingSummary = {
    series_size: 2,
    completed_matches: 1,
    game_points_a: 1,
    game_points_b: 0,
    normalized_delta_a: 8.4,
    settled: false,
    standings_points_a: null,
    standings_points_b: null,
  }
  const settledSummary = {
    series_size: 2,
    completed_matches: 2,
    game_points_a: 2,
    game_points_b: 0,
    normalized_delta_a: 15.25,
    settled: true,
    standings_points_a: 3,
    standings_points_b: 0,
  }
  const snapshot = liveSnapshot()
  snapshot.stage = { ...snapshot.stage, key: 'prelim', label: 'prelim', type: 'swiss' }
  snapshot.series = {
    games_per_pair: 2,
    duplicate: false,
    scoring_mode: 'aggregate_match_points_v1',
    conceptual_completed: 1,
    conceptual_total: 3,
  }
  snapshot.active = []
  snapshot.upcoming = [
    pairing({ id: 21, status: 'running', displayStatus: 'queued', matchId: 'series-pending-1', seriesIndex: 1, seriesSize: 2, botAId: 501, botBId: 502, seriesSummary: pendingSummary }),
    pairing({ id: 22, status: 'pending', displayStatus: 'pending', matchId: null, seriesIndex: 2, seriesSize: 2, botAId: 502, botBId: 501, seriesSummary: pendingSummary }),
  ]
  snapshot.recent = [
    pairing({ id: 31, status: 'completed', displayStatus: 'completed', matchId: 'series-done-1', seriesIndex: 1, seriesSize: 2, botAId: 601, botBId: 602, winner: 0, seriesSummary: settledSummary }),
    pairing({ id: 32, status: 'completed', displayStatus: 'completed', matchId: 'series-done-2', seriesIndex: 2, seriesSize: 2, botAId: 602, botBId: 601, winner: 1, seriesSummary: settledSummary }),
    pairing({
      id: 33,
      status: 'completed',
      displayStatus: 'completed',
      matchId: null,
      seriesIndex: 1,
      seriesSize: 1,
      botAId: 603,
      botBId: null,
      isBye: true,
      seriesSummary: { ...settledSummary, series_size: 1, completed_matches: 0, standings_points_a: 3 },
    }),
  ]
  const monitor = monitorBrowser(page)
  await installLiveApi(page, snapshot)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/contests/42/live')

  await expect(page.getByText(/预赛瑞士轮 · 德州扑克/)).toBeVisible()
  await expect(page.getByText('每次对手交锋 2 场 · 已结算 1/3 组', { exact: true })).toBeVisible()
  await expect(page.getByText(/复式对局/)).toHaveCount(0)
  await expect(page.getByText('本轮积分待结算', { exact: true })).toBeVisible()
  await expect(page.getByText(/本轮交锋 2 场 · 已完成 1\/2 · 小分 1–0 · 净胜 \+8\.4BB/)).toBeVisible()
  await expect(page.getByRole('link', { name: '第 1/2 场详情' })).toBeVisible()
  await expect(page.getByText('第 2/2 场待调度', { exact: true })).toBeVisible()
  await expect(page.getByText('3–0 赛事积分', { exact: true })).toBeVisible()
  await expect(page.getByText(/本轮交锋 2 场 · 已完成 2\/2 · 小分 2–0 · 净胜 \+15\.25BB/)).toBeVisible()
  await expect(page.getByText('轮空 · +3 赛事积分', { exact: true })).toBeVisible()
  await expect(page.getByText('本轮没有生成实际对局。', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('immutable running snapshot is dark/mobile readable, keyboard reachable, and never polls', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('theme', 'dark'))
  const monitor = monitorBrowser(page)
  const api = await installLiveApi(page, liveSnapshot({
    status: 'running',
    showcase: true,
    immutable: true,
    officialResultsReady: true,
  }))
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/contests/42/live')

  await expect(page.getByRole('heading', { name: '秋季德州扑克联赛' })).toBeVisible()
  await expect(page.getByText('演示快照', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '演示赛况快照' })).toBeVisible()
  await expect(page.getByTestId('contest-live-sync-status')).toHaveText('快照时间 · 12:10:30')
  await expect(page.locator('span[aria-live="polite"]')).toHaveText('演示赛况快照，内容固定。')
  await expect(page.getByRole('link', { name: '查看演示第 1 桌观赛' })).toBeVisible()
  await expect(page.locator('.animate-ping')).toHaveCount(0)
  const frozen = page.getByRole('button', { name: '赛况已冻结' })
  await expect(frozen).toBeDisabled()
  expect((await frozen.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await expect(page.locator('html')).toHaveClass(/dark/)

  const backLink = page.getByRole('link', { name: '返回赛事详情' })
  for (let index = 0; index < 20 && !(await backLink.evaluate((element) => document.activeElement === element)); index += 1) {
    await page.keyboard.press('Tab')
  }
  await expect(backLink).toBeFocused()

  await page.waitForTimeout(2_200)
  expect(api.liveReads()).toBe(1)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('route generation rejects stale contest data and terminal transition stops polling', async ({ page }) => {
  let releaseContest42 = () => undefined
  const contest42Gate = new Promise<void>((resolve) => { releaseContest42 = resolve })
  let contest42Reads = 0
  let contest43Reads = 0
  const apiPaths: string[] = []

  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    apiPaths.push(pathname)
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === '/api/contests/42/live') {
      contest42Reads += 1
      await contest42Gate
      try {
        return await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(liveSnapshot({ id: 42, title: '旧赛事响应', status: 'running' })),
        })
      } catch {
        return
      }
    }
    if (pathname === '/api/contests/43/live') {
      contest43Reads += 1
      const status = contest43Reads === 1 ? 'running' : 'finished'
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(liveSnapshot({
          id: 43,
          title: '新赛事 43',
          status,
          officialResultsReady: true,
        })),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/#/contests/42/live')
  await expect.poll(() => contest42Reads).toBe(1)
  await page.evaluate(() => { location.hash = '#/contests/43/live' })
  await expect(page.getByRole('heading', { name: '新赛事 43' })).toBeVisible()
  releaseContest42()
  await page.waitForTimeout(150)
  await expect(page.getByRole('heading', { name: '旧赛事响应' })).toHaveCount(0)

  await expect.poll(() => contest43Reads, { timeout: 3_500 }).toBe(2)
  await expect(page.getByText('已结束', { exact: true })).toBeVisible()
  await expect(page.getByTestId('contest-live-sync-status')).toHaveText('最终赛况 · 12:10:32')
  await expect(page.locator('span[aria-live="polite"]')).toHaveText('赛事已结束，赛况已冻结。')
  await page.waitForTimeout(2_200)
  expect(contest43Reads).toBe(2)
  expect(apiPaths.filter((path) => path !== '/api/auth/me'))
    .toEqual(['/api/contests/42/live', '/api/contests/43/live', '/api/contests/43/live'])
})

test('a delayed 404 from the previous contest cannot hide the current live page', async ({ page }) => {
  let releaseOldRequest = () => undefined
  const oldRequestGate = new Promise<void>((resolve) => { releaseOldRequest = resolve })
  let oldReads = 0
  let currentReads = 0

  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === '/api/contests/44/live') {
      oldReads += 1
      await oldRequestGate
      try {
        return await route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not found"}' })
      } catch {
        return
      }
    }
    if (pathname === '/api/contests/45/live') {
      currentReads += 1
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(liveSnapshot({ id: 45, title: '当前赛事 45', status: 'running' })),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/#/contests/44/live')
  await expect.poll(() => oldReads).toBe(1)
  await page.evaluate(() => { location.hash = '#/contests/45/live' })
  await expect(page.getByRole('heading', { name: '当前赛事 45' })).toBeVisible()
  releaseOldRequest()
  await page.waitForTimeout(150)

  expect(currentReads).toBe(1)
  await expect(page.getByRole('heading', { name: '当前赛事 45' })).toBeVisible()
  await expect(page.getByText('赛事不存在或当前不可见')).toHaveCount(0)
})

test('finished contest keeps low-frequency updates until official results are ready', async ({ page }) => {
  await page.clock.install()
  const monitor = monitorBrowser(page)
  let liveReads = 0
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === '/api/contests/42/live') {
      liveReads += 1
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(liveSnapshot({
          status: 'finished',
          officialResultsReady: liveReads >= 2,
        })),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  await page.goto('/#/contests/42/live')
  await expect(page.getByText('成绩正在整理，正式名次稍后公布')).toBeVisible()
  await expect(page.getByRole('button', { name: '刷新赛况' })).toBeEnabled()
  await page.clock.fastForward(10_100)
  await expect.poll(() => liveReads).toBe(2)
  await expect(page.getByText('成绩正在整理，正式名次稍后公布')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '赛况已冻结' })).toBeDisabled()
  await page.clock.fastForward(30_000)
  expect(liveReads).toBe(2)
  await monitor.expectClean()
})

test('initial and later 404 responses stop polling and hide stale contest data', async ({ page }) => {
  await page.clock.install()
  const monitor = monitorBrowser(page)
  let initial404Reads = 0
  let later404Reads = 0
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === '/api/contests/404/live') {
      initial404Reads += 1
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not found"}' })
    }
    if (pathname === '/api/contests/45/live') {
      later404Reads += 1
      if (later404Reads >= 2) {
        return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not found"}' })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(liveSnapshot({ id: 45, title: '稍后变为不可见', status: 'running' })),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/#/contests/404/live')
  await expect(page.getByText('赛事不存在或当前不可见')).toBeVisible()
  await page.clock.fastForward(40_000)
  expect(initial404Reads).toBe(1)

  await page.evaluate(() => { location.hash = '#/contests/45/live' })
  await expect(page.getByRole('heading', { name: '稍后变为不可见' })).toBeVisible()
  await page.clock.fastForward(2_100)
  await expect.poll(() => later404Reads).toBe(2)
  await expect(page.getByText('赛事不存在或当前不可见')).toBeVisible()
  await expect(page.getByRole('heading', { name: '稍后变为不可见' })).toHaveCount(0)
  await page.clock.fastForward(40_000)
  expect(later404Reads).toBe(2)
  await monitor.expectClean([
    { kind: 'http', method: 'GET', status: 404, pathname: '/api/contests/404/live' },
    { kind: 'http', method: 'GET', status: 404, pathname: '/api/contests/45/live' },
  ])
})

test('contest match viewer exposes a stable return link to the spectator page', async ({ page, browserName }) => {
  const matchId = 'contest-return-match'
  const monitor = monitorBrowser(page)
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === `/api/matches/${matchId}/view`) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    }
    if (pathname === `/api/matches/${matchId}`) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          match: {
            id: matchId,
            contest_id: 42,
            game_id: 'holdem',
            status: 'completed',
            match_type: 'contest',
            bot_a_id: 101,
            bot_b_id: 102,
            bot_a_name: 'river-guard',
            bot_b_name: 'turn-probe',
            owner_a_name: 'alpha-owner',
            owner_b_name: 'beta-owner',
            winner: 0,
            rated: false,
            rating_reason: 'contest',
            result: { rounds_played: 1, deltas: [100, -100], normalized_delta: 1 },
          },
        }),
      })
    }
    if (pathname === `/api/matches/${matchId}/replay`) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ match_id: matchId, events: [], event_count: 0 }),
      })
    }
    if (pathname === '/api/comments') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"comments":[],"count":0,"total":0}',
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto(`/#/match/${matchId}`)
  const back = page.getByRole('link', { name: '返回赛事直播' })
  await expect(back).toHaveAttribute('href', '#/contests/42/live')
  expect((await back.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await monitor.expectClean(browserName === 'firefox' ? [] : [{
    kind: 'requestfailed',
    method: 'GET',
    pathname: `/api/matches/${matchId}`,
    errorText: browserName === 'webkit' ? 'Load request cancelled' : 'net::ERR_ABORTED',
  }])
})
