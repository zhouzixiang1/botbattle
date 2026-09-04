import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'
import { HOLDEM_TEMPLATE_TIME_CONTROL } from './time-control-fixtures'

const CONTEST_ID = 913

type Winner = 0 | 1 | null

function duplicateOutcome(
  winners: Winner[],
  termination: { kind: 'normal' | 'technical'; reason: string; loser: Winner } = {
    kind: 'normal',
    reason: 'completed',
    loser: null,
  },
) {
  return {
    kind: 'duplicate' as const,
    planned_games: 2,
    completed_games: winners.length,
    score: {
      wins_a: winners.filter((winner) => winner === 0).length,
      draws: winners.filter((winner) => winner === null).length,
      wins_b: winners.filter((winner) => winner === 1).length,
    },
    rounds_played: winners.length * 70,
    normalized_delta_a: winners.reduce((sum, winner) => sum + (winner === 0 ? 10 : winner === 1 ? -10 : 0), 0),
    games: winners.map((winner, index) => ({
      index: index + 1,
      winner,
      rounds_played: 70,
      normalized_delta_a: winner === 0 ? 10 : winner === 1 ? -10 : 0,
    })),
    termination,
  }
}

function singleDrawOutcome() {
  return {
    kind: 'single' as const,
    planned_games: 1,
    completed_games: 1,
    score: { wins_a: 0, draws: 1, wins_b: 0 },
    rounds_played: 70,
    normalized_delta_a: 0,
    games: [{ index: 1, winner: null, rounds_played: 70, normalized_delta_a: 0 }],
    termination: { kind: 'normal' as const, reason: 'completed', loser: null },
  }
}

function pairing(
  id: number,
  outcome: ReturnType<typeof duplicateOutcome> | ReturnType<typeof singleDrawOutcome> | null,
) {
  return {
    id,
    bot_a_id: 100 + id,
    bot_b_id: 200 + id,
    bot_a_name: `alpha-${id}`,
    bot_a_display: `Alpha ${id}`,
    bot_b_name: `beta-${id}`,
    bot_b_display: `Beta ${id}`,
    owner_a_name: `owner-alpha-${id}`,
    owner_b_name: `owner-beta-${id}`,
    match_id: `duplicate-${id}`,
    status: 'completed',
    stage_idx: 0,
    stage_key: 'dup_rr',
    round_num: id,
    match_winner: null,
    series_index: 1,
    series_size: 1,
    outcome,
  }
}

function duplicateDetail() {
  const row = (
    entryId: number,
    botName: string,
    points: number,
    wins: number,
    draws: number,
    losses: number,
    rank: number,
  ) => ({
    entry_id: entryId,
    bot_id: 900 + entryId,
    bot_name: botName,
    owner_name: `owner-${entryId}`,
    points,
    wins,
    draws,
    losses,
    byes: 0,
    delta_total: 0,
    group_id: '',
    rank,
    advancement: null,
    counts: { unique_opponents: 3, encounter_groups: 3, match_jobs: 3, scoring_games: 6 },
  })
  const rows = [
    row(1, 'river-one', 12, 3, 3, 0, 1),
    row(2, 'river-two', 9, 2, 3, 1, 2),
    row(3, 'river-three', 6, 1, 3, 2, 3),
    row(4, 'river-four', 3, 0, 3, 3, 4),
  ]
  return {
    contest: {
      id: CONTEST_ID,
      title: '复式独立计分回归赛',
      description: '每个 70 手计分场分别记分',
      status: 'running',
      organizer_id: 88,
      template_id: 'holdem_dup_rr',
      template_name: '德州：复式单循环',
      game_id: 'holdem',
      games_per_pair: 1,
      stages_json: JSON.stringify([{
        key: 'dup_rr',
        type: 'round_robin',
        scoring: 'poker_3_1_0',
        duplicate: true,
        games_per_pair: 1,
        series_scoring: 'independent_scoring_game_points_v1',
      }]),
      current_stage_idx: 0,
      official_results_ready: 0,
    },
    entries: [],
    entries_total: 4,
    entries_page: 1,
    entries_per_page: 20,
    pairings: [
      {
        ...pairing(1, duplicateOutcome([0, 1])),
        // The Match is authoritative when the asynchronous pairing write-back
        // lags. Every contest view must prefer display_status.
        status: 'running',
        display_status: 'completed',
      },
      pairing(2, duplicateOutcome([0, 0])),
      pairing(3, duplicateOutcome([1], { kind: 'technical', reason: 'protocol_error', loser: 0 })),
      pairing(4, null),
      pairing(5, duplicateOutcome([null, null])),
      pairing(6, duplicateOutcome([1, 0])),
    ],
    standings: rows.map(({ entry_id: _entryId, rank: _rank, advancement: _advancement, ...rest }) => rest),
    stage_standings: [{
      stage_idx: 0,
      stage_key: 'dup_rr',
      status: 'running',
      source: 'live',
      completed_pairings: 6,
      total_pairings: 6,
      advancement_final: false,
      counts: {
        encounter_groups: { completed: 6, total: 6 },
        match_jobs: { completed: 6, total: 6 },
        scoring_games: { completed: 11, planned: 12, terminal_unplayed: 1 },
      },
      rows,
    }],
    estimate: { estimated_matches: 6, eta_seconds: 3600 },
    my_entry: null,
    is_organizer: false,
  }
}

async function installContestApi(page: Page, body: ReturnType<typeof duplicateDetail>) {
  const apiPaths: string[] = []
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    apiPaths.push(pathname)
    if (pathname === '/api/auth/me') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"not signed in"}' })
    }
    if (pathname === `/api/contests/${CONTEST_ID}`) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
  return apiPaths
}

async function installOpenDuplicateContestApi(
  page: Page,
  {
    frozenAdvanceCount,
    frozenStagePatch,
    legacyScalarGamesPerPair,
  }: {
    frozenAdvanceCount?: number
    frozenStagePatch?: Record<string, unknown>
    legacyScalarGamesPerPair?: number
  } = {},
) {
  const body = duplicateDetail()
  body.contest.status = 'open'
  if (frozenAdvanceCount != null || frozenStagePatch || legacyScalarGamesPerPair != null) {
    const stages = JSON.parse(body.contest.stages_json) as Array<Record<string, unknown>>
    if (frozenAdvanceCount != null) stages[0]!.advance_count = frozenAdvanceCount
    if (frozenStagePatch) Object.assign(stages[0]!, frozenStagePatch)
    if (legacyScalarGamesPerPair != null) {
      stages[0]!.games_per_pair = legacyScalarGamesPerPair
      delete stages[0]!.series_scoring
      body.contest.games_per_pair = legacyScalarGamesPerPair
    }
    body.contest.stages_json = JSON.stringify(stages)
  }
  if (legacyScalarGamesPerPair == null) {
    Object.assign(body.contest, {
      stage_series_settings: { dup_rr: { games_per_pair: 1 } },
    })
  }
  body.is_organizer = true
  const writes: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (request.method() !== 'GET') writes.push(`${request.method()} ${pathname}`)
    if (pathname === '/api/auth/me') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          user: {
            id: 88,
            username: 'duplicate-organizer',
            display_name: '复式赛事组织者',
            email: 'duplicate-organizer@example.test',
            role: 'organizer',
            email_verified: 1,
          },
        }),
      })
    }
    if (pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (pathname === '/api/contests/templates') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          templates: [{
            id: 'holdem_dup_rr',
            game_id: 'holdem',
            ...HOLDEM_TEMPLATE_TIME_CONTROL,
            stages: [{ key: 'dup_rr', type: 'round_robin', scoring: 'poker_3_1_0', duplicate: true }],
            ...(legacyScalarGamesPerPair == null
              ? { stage_series_configs: [{
                  stage_key: 'dup_rr',
                  label: '单循环',
                  games_per_pair: { default: 1, allowed_values: [1, 2, 4] },
                }] }
              : { games_per_pair_config: { default: 1, min: 1, max: 10 } }),
          }],
        }),
      })
    }
    if (pathname === '/api/bots/mine') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"bots":[]}' })
    }
    if (pathname === `/api/contests/${CONTEST_ID}` && request.method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
  return writes
}

test('duplicate detail separates encounter groups, scoring games, partial technical results and unavailable outcomes', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const apiPaths = await installContestApi(page, duplicateDetail())
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  await expect(main.getByText('复式交锋 · 每组 2 场计分', { exact: true })).toBeVisible()
  await expect(main.getByText('预计 6 组', { exact: true })).toBeVisible()
  await expect(main.getByText('1 组 · 2 场计分', { exact: true })).toBeVisible()
  await expect(main.getByRole('tab', { name: /6\/6 组复式交锋 · 11\/12 场计分/ })).toBeVisible()
  await expect(main.getByText('当前阶段 6/6 个对手系列 · 6/6 组复式交锋 · 11/12 场计分。', { exact: true })).toBeVisible()

  const scheduleTable = main.getByRole('table', { name: '赛事对阵一览表' })
  const authoritativeCompletedRow = scheduleTable.getByRole('row').filter({ hasText: 'Alpha 1' })
  await expect(authoritativeCompletedRow).toContainText('Alpha 1 1胜 · 平 0 · Beta 1 1胜')
  await expect(authoritativeCompletedRow).toContainText('已完成')
  await expect(authoritativeCompletedRow).not.toContainText('进行中')
  await expect(scheduleTable.getByRole('row').filter({ hasText: 'Alpha 2' })).toContainText('Alpha 2 2胜 · 平 0 · Beta 2 0胜')
  const technicalRow = scheduleTable.getByRole('row').filter({ hasText: 'Alpha 3' })
  await expect(technicalRow).toContainText('技术终局 · 已计 1/2 场')
  await expect(technicalRow).toContainText('Alpha 3 技术判负')
  await expect(scheduleTable.getByRole('row').filter({ hasText: 'Alpha 4' })).toContainText('赛果暂不可用')
  await expect(main.getByText('平局', { exact: true })).toHaveCount(0)

  const stagePanel = main
    .getByRole('heading', { name: '阶段排名与晋级', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(stagePanel).toContainText('6/6 个对手系列 · 6/6 组复式交锋 · 11/12 场计分')
  await expect(stagePanel).toContainText('面对 3 位对手 · 3 组复式交锋 · 6 场计分')

  await main.getByRole('tab', { name: /阶段积分/ }).click()
  const firstStanding = main.getByRole('row').filter({ hasText: 'river-one' })
  await expect(firstStanding).toContainText('面对 3 位对手 · 3 组复式交锋 · 6 场计分')
  await expect(firstStanding).toContainText('3 胜 / 3 平 / 0 负 · 轮空 0')

  expect(apiPaths.filter((path) => path.includes('/match/'))).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)

  await page.setViewportSize({ width: 390, height: 844 })
  await main.getByRole('tab', { name: /对阵/ }).click()
  const viewButtons = main.getByRole('group', { name: '对阵视图' }).getByRole('button')
  for (const button of await viewButtons.all()) {
    expect((await button.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  }
  const authoritativeCompletedCard = main.getByTestId('contest-schedule-mobile-card').filter({ hasText: 'Alpha 1' })
  await expect(authoritativeCompletedCard).toContainText('Alpha 1 1胜 · 平 0 · Beta 1 1胜')
  await expect(authoritativeCompletedCard).toContainText('已完成')
  await expect(authoritativeCompletedCard).not.toContainText('进行中')
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('legacy group detail shows backend group ranks without inventing overall ranks', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.template_id = 'legacy_group_drr'
  body.contest.template_name = '历史分组双循环'
  body.contest.stages_json = JSON.stringify([{
    key: 'groups',
    type: 'group_double_round_robin',
    scoring: 'poker_3_1_0',
    group_count: 2,
    advance_per_group: 1,
  }])
  body.stage_standings[0]!.stage_key = 'groups'
  body.stage_standings[0]!.rows = body.stage_standings[0]!.rows.map((row, index) => ({
    ...row,
    group_id: index < 2 ? 'A' : 'B',
    rank: index % 2 + 1,
  }))
  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const panel = page.getByRole('heading', { name: '阶段排名与晋级', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(panel.getByText('A组 · 组内 1', { exact: true })).toBeVisible()
  await expect(panel.getByText('A组 · 组内 2', { exact: true })).toBeVisible()
  await expect(panel.getByText('B组 · 组内 1', { exact: true })).toBeVisible()
  await expect(panel.getByText('B组 · 组内 2', { exact: true })).toBeVisible()
  await expect(panel.getByText('名次不可用', { exact: true })).toHaveCount(0)
  await expect(panel.getByText(/总\d/)).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('ordinary completed single outcome keeps a real draw while a missing outcome stays unavailable', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.template_id = 'holdem_rr'
  body.contest.template_name = '德州：单循环'
  body.contest.games_per_pair = 1
  body.contest.stages_json = JSON.stringify([{
    key: 'rr', type: 'round_robin', scoring: 'poker_3_1_0', games_per_pair: 1,
    series_scoring: 'independent_scoring_game_points_v1',
  }])
  body.pairings = [pairing(1, singleDrawOutcome()), pairing(2, null)]
  body.pairings.forEach((item) => { item.stage_key = 'rr' })
  body.stage_standings[0]!.stage_key = 'rr'
  body.stage_standings[0]!.completed_pairings = 2
  body.stage_standings[0]!.total_pairings = 2
  body.stage_standings[0]!.counts = {
    encounter_groups: { completed: 2, total: 2 },
    match_jobs: { completed: 2, total: 2 },
    scoring_games: { completed: 1, planned: 2, terminal_unplayed: 0 },
  }
  body.stage_standings[0]!.rows = body.stage_standings[0]!.rows.map((row) => ({
    ...row,
    points: 4,
    wins: 1,
    draws: 1,
    losses: 0,
    counts: { unique_opponents: 1, encounter_groups: 1, match_jobs: 2, scoring_games: 2 },
  }))

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  const mobileCards = main.getByTestId('contest-schedule-mobile-card')
  await expect(mobileCards.filter({ hasText: 'Alpha 1' })).toContainText('平局')
  await expect(mobileCards.filter({ hasText: 'Alpha 2' })).toContainText('赛果暂不可用')
  const stagePanel = main
    .getByRole('heading', { name: '阶段排名与晋级', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(stagePanel).toContainText('面对 1 位对手 · 2 条对局记录 · 2 场计分')
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('malformed frozen stage does not guess duplicate, series, scoring, standings, or progress', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.stages_json = JSON.stringify([{
    key: 'dup_rr',
    type: 'round_robin',
    scoring: 'poker_3_1_0',
    duplicate: 'false',
    games_per_pair: 1,
    series_scoring: 'unknown_scoring_contract',
  }])
  body.pairings = [pairing(1, null)]

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  const overview = main.getByTestId('contest-overview')
  await expect(overview.locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toBeVisible()
  await expect(overview.getByText('暂不可用', { exact: true })).toBeVisible()
  await expect(main.getByRole('tab', { name: /赛制配置暂不可用/ })).toBeVisible()
  await expect(main.getByText('赛制配置暂不可用，已停止推断对阵单位与赛果。', { exact: true })).toBeVisible()
  await expect(main.getByText('已停止推断本阶段积分与晋级。', { exact: true })).toBeVisible()
  await expect(main.getByRole('table', { name: '赛事对阵一览表' })).toHaveCount(0)
  await expect(main.getByText(/组复式交锋|场计分/)).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('explicit legacy aggregate marker also rejects fields outside its frozen stage schema', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.stages_json = JSON.stringify([{
    key: 'rr',
    type: 'round_robin',
    scoring: 'poker_3_1_0',
    duplicate: false,
    games_per_pair: 1,
    series_scoring: 'aggregate_match_points_v1',
    // ranking_scope is only valid together with replace_top on a double RR.
    ranking_scope: 4,
  }])
  body.pairings = [pairing(1, singleDrawOutcome())]

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  await expect(main.getByTestId('contest-overview').locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toBeVisible()
  await expect(main.getByText('赛制配置暂不可用，已停止推断对阵单位与赛果。', { exact: true })).toBeVisible()
  await expect(main.getByRole('table', { name: '赛事对阵一览表' })).toHaveCount(0)
  await expect(main.getByText(/旧版系列结算|组复式交锋|场计分/)).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('a valid duplicate Swiss frozen stage keeps its two-game scoring contract readable', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.stages_json = JSON.stringify([{
    key: 'prelim',
    type: 'swiss',
    scoring: 'poker_3_1_0',
    duplicate: true,
    rounds: 3,
    effective_rounds: 3,
    swiss_extra_rounds: 0,
    games_per_pair: 1,
    series_scoring: 'independent_scoring_game_points_v1',
  }])
  body.pairings.forEach((item) => { item.stage_key = 'prelim' })
  body.stage_standings[0]!.stage_key = 'prelim'

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  await expect(main.getByTestId('contest-overview').locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toHaveCount(0)
  await expect(main.getByText('复式交锋 · 每组 2 场计分', { exact: true })).toBeVisible()
  await expect(main.getByText('赛制配置暂不可用，已停止推断对阵单位与赛果。', { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('a marked Swiss stage missing its frozen rounds cannot be recomputed by the detail page', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.stages_json = JSON.stringify([{
    key: 'prelim',
    type: 'swiss',
    scoring: 'poker_3_1_0',
    duplicate: false,
    games_per_pair: 2,
    series_scoring: 'independent_scoring_game_points_v1',
  }])

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  await expect(main.getByTestId('contest-overview').locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toBeVisible()
  await expect(main.getByText('赛制配置暂不可用，已停止推断对阵单位与赛果。', { exact: true })).toBeVisible()
  await expect(main.getByRole('table', { name: '赛事对阵一览表' })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('duplicate publish confirmation freezes encounter groups and their two independent scoring games', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes = await installOpenDuplicateContestApi(page)
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '截止报名并发布排期？' })).toBeVisible()
  await expect(dialog).toContainText(
    '单循环每对选手 1 组复式交锋（2 场计分，每组两场同牌换座、独立计分）',
  )
  await expect(dialog).not.toContainText('每对选手 1 场计分')
  await dialog.getByRole('button', { name: '取消' }).click()

  expect(writes).toEqual([])
  await monitor.expectClean()
})

test('legacy scalar duplicate publish confirmation states the frozen K before safe migration', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes = await installOpenDuplicateContestApi(page, { legacyScalarGamesPerPair: 3 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await expect(page.getByRole('button', { name: '保存设置' })).toHaveCount(0)
  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '截止报名并发布排期？' })).toBeVisible()
  await expect(dialog).toContainText(
    '单循环每对选手 3 组复式交锋（6 场计分，每组两场同牌换座、独立计分）',
  )
  await dialog.getByRole('button', { name: '取消' }).click()

  expect(writes).toEqual([])
  await monitor.expectClean()
})

test('template topology drift disables fairness edits but preserves direct custom-stage publication', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes = await installOpenDuplicateContestApi(page, { frozenAdvanceCount: 4 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await expect(page.getByText('冻结阶段拓扑与内置模板不一致，已停用公平性设置编辑；发布时将保留当前冻结阶段。', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '截止报名·出排期' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '保存设置' })).toHaveCount(0)
  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  await expect(page.getByRole('dialog').getByRole('heading', { name: '截止报名并发布排期？' })).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '取消' }).click()
  expect(writes).toEqual([])
  await monitor.expectClean()
})

test('open contest with a malformed frozen scoring contract cannot publish from the detail page', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes = await installOpenDuplicateContestApi(page, {
    frozenStagePatch: {
      scoring: 'ccgc_2_1_0',
      allow_large_round_robin: 'false',
    },
  })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await expect(page.getByTestId('contest-overview').locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toBeVisible()
  await expect(page.getByRole('button', { name: '截止报名·出排期' })).toBeDisabled()
  await page.getByRole('tab', { name: /对阵/ }).click()
  await expect(page.getByText('赛制配置暂不可用，已停止推断对阵单位与赛果。', { exact: true })).toBeVisible()
  expect(writes).toEqual([])
  await monitor.expectClean()
})

test('ranking scope without replace-top mode is fail-closed before publish', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes = await installOpenDuplicateContestApi(page, {
    frozenStagePatch: {
      type: 'double_round_robin',
      ranking_scope: 8,
    },
  })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await expect(page.getByTestId('contest-overview').locator('[data-slot="badge"]').filter({ hasText: '赛制配置暂不可用' })).toBeVisible()
  await expect(page.getByRole('button', { name: '截止报名·出排期' })).toBeDisabled()
  expect(writes).toEqual([])
  await monitor.expectClean()
})

test('legacy aggregate stage keeps its frozen one-result-per-series wording', async ({ page }) => {
  const body = duplicateDetail()
  body.contest.title = '历史系列结算赛'
  body.contest.template_id = 'holdem_prelim_swiss'
  body.contest.template_name = '德州：历史瑞士轮'
  body.contest.games_per_pair = 2
  body.contest.stages_json = JSON.stringify([{
    key: 'prelim',
    type: 'swiss',
    scoring: 'poker_3_1_0',
    rounds: 0,
    games_per_pair: 2,
    series_scoring: 'aggregate_match_points_v1',
  }])
  body.pairings = [pairing(1, singleDrawOutcome()), pairing(2, singleDrawOutcome())]
  body.pairings.forEach((item, index) => {
    item.stage_key = 'prelim'
    item.series_index = index + 1
    item.series_size = 2
    item.round_num = 1
  })
  const legacyRows = body.stage_standings[0]!.rows.map((row, index) => ({
    ...row,
    points: 1,
    wins: 0,
    draws: 1,
    losses: 0,
    counts: { unique_opponents: 1, encounter_groups: 1, match_jobs: 2, scoring_games: 1 },
    rank: index + 1,
  }))
  body.standings = legacyRows.map(({ entry_id: _entryId, rank: _rank, advancement: _advancement, ...rest }) => rest)
  body.stage_standings[0] = {
    ...body.stage_standings[0]!,
    stage_key: 'prelim',
    completed_pairings: 2,
    total_pairings: 2,
    counts: {
      encounter_groups: { completed: 1, total: 1 },
      match_jobs: { completed: 2, total: 2 },
      scoring_games: { completed: 1, planned: 1, terminal_unplayed: 0 },
    },
    rows: legacyRows,
  }

  const monitor = monitorBrowser(page)
  await installContestApi(page, body)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const main = page.getByRole('main')
  await expect(main.getByText('历史系列对局', { exact: true })).toBeVisible()
  await expect(main.getByText('旧版系列结算', { exact: true })).toBeVisible()
  await expect(main.getByText(/本阶段配置：.*旧版系列结算（冻结历史口径）/)).toBeVisible()
  await expect(main.getByText(/完整系列按冻结规则只结算 1 次胜、平、负/)).toBeVisible()
  const stagePanel = main
    .getByRole('heading', { name: '阶段排名与晋级', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(stagePanel).toContainText('1/1 个对手系列 · 2/2 场历史系列对局 · 1/1 次旧版系列结算')
  await expect(stagePanel).toContainText('面对 1 位对手 · 2 场历史系列对局 · 1 次旧版系列结算')
  await main.getByRole('button', { name: '分组视图', exact: true }).click()
  await expect(main.getByText('本对交锋 2 场历史系列对局', { exact: true })).toHaveCount(2)
  await expect(main.getByRole('link', { name: '查看历史对局' })).toHaveCount(2)

  await main.getByRole('button', { name: '一览表', exact: true }).click()
  const historySchedule = main.getByRole('table', { name: '赛事对阵一览表' })
  await expect(historySchedule).toContainText('本对 2 场历史系列对局')
  await expect(historySchedule).toContainText('旧版系列 第 1/2 场')
  await expect(historySchedule).not.toContainText('已完成 1/1 场计分')
  await expect(historySchedule.getByRole('link', { name: '查看历史对局' })).toHaveCount(2)

  await page.setViewportSize({ width: 390, height: 844 })
  const firstHistoryCard = main.getByTestId('contest-schedule-mobile-card').first()
  await expect(firstHistoryCard).toContainText('本对 2 场历史系列对局 · 旧版系列第 1/2 场')
  await expect(firstHistoryCard).not.toContainText('已完成 1/1 场计分')
  await expect(firstHistoryCard.getByRole('link', { name: '查看历史对局' })).toBeVisible()
  await main.getByRole('tab', { name: /阶段积分/ }).click()
  await expect(main.getByText(/本历史阶段沿用旧版系列结算/)).toBeVisible()
  await expect(main.getByRole('columnheader', { name: '旧版系列战绩 / 轮空' })).toBeVisible()
  await expect(main.getByRole('row').filter({ hasText: 'river-one' })).toContainText('1 次旧版系列结算')
  await expect(main.getByText('1 场计分', { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})
