import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const ORGANIZER = {
  id: 91,
  username: 'fairness-organizer',
  display_name: '公平赛组织者',
  email: 'fairness-organizer@example.test',
  role: 'organizer',
  email_verified: 1,
}

const finalConfigs = [
  {
    stage_key: 'qualify',
    label: '决赛全员循环排位',
    games_per_pair: { default: 2, allowed_values: [1, 2, 4] },
  },
  {
    stage_key: 'final8',
    label: 'Top 8 决胜',
    games_per_pair: { default: 4, allowed_values: [2, 4, 6, 8, 10] },
  },
]

const finalTemplateStages = [
  {
    key: 'qualify',
    type: 'round_robin',
    allow_large_round_robin: true,
    advance_count: 8,
    scoring: 'poker_3_1_0',
    rest_after_minutes: 10,
    allow_bot_swap_in_rest: true,
  },
  {
    key: 'final8',
    type: 'double_round_robin',
    ranking_mode: 'replace_top',
    ranking_scope: 8,
    scoring: 'poker_3_1_0',
    rest_after_minutes: 0,
    allow_bot_swap_in_rest: false,
  },
]

const prelimConfig = [{
  stage_key: 'prelim',
  label: '预赛瑞士轮',
  games_per_pair: { default: 2, allowed_values: [1, 2, 4] },
  swiss_extra_rounds: { default: 2, min: 0, max: 4 },
}]

const prelimTemplateStages = [{
  key: 'prelim',
  type: 'swiss',
  rounds: 0,
  scoring: 'poker_3_1_0',
  allow_bot_swap_in_rest: true,
  rest_after_minutes: 0,
}]

function contestDetail(status: 'open' | 'published', final8Games: number) {
  const finalMatches = 28 * final8Games
  return {
    contest: {
      id: 7001,
      title: '年度德州公平决赛',
      description: '资格循环后进入 Top 8。',
      status,
      organizer_id: ORGANIZER.id,
      template_id: 'holdem_final_ranked',
      template_name: '德州：决赛（循环→Top8）',
      game_id: 'holdem',
      current_stage_idx: 0,
      stages_json: JSON.stringify([
        {
          ...finalTemplateStages[0],
          games_per_pair: 2,
          series_scoring: 'independent_scoring_game_points_v1',
        },
        {
          ...finalTemplateStages[1],
          games_per_pair: final8Games,
          series_scoring: 'independent_scoring_game_points_v1',
        },
      ]),
      stage_series_settings: {
        qualify: { games_per_pair: 2 },
        final8: { games_per_pair: final8Games },
      },
      official_results_ready: 0,
    },
    entries: [],
    entries_total: 10,
    pairings: [],
    standings: [],
    stage_standings: [],
    my_entry: null,
    is_organizer: true,
    estimate: {
      estimated_matches: 90 + finalMatches,
      eta_seconds: (90 + finalMatches) * 140,
      stages: [
        {
          stage_key: 'qualify',
          participant_count: 10,
          conceptual_pairings: 45,
          effective_rounds: null,
          games_per_pair: 2,
          estimated_matches: 90,
          estimated_execution_legs: 90,
          eta_seconds: 90 * 140,
        },
        {
          stage_key: 'final8',
          participant_count: 8,
          conceptual_pairings: 28,
          effective_rounds: null,
          games_per_pair: final8Games,
          estimated_matches: finalMatches,
          estimated_execution_legs: finalMatches,
          eta_seconds: finalMatches * 140,
        },
      ],
    },
  }
}

async function installFinalContestApi(page: Page, options: { patchFails?: boolean; omitSettings?: boolean } = {}) {
  let status: 'open' | 'published' = 'open'
  let final8Games = 4
  const writes: Array<{ method: string; pathname: string; body?: Record<string, unknown> }> = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: ORGANIZER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ templates: [{ id: 'holdem_final_ranked', stages: finalTemplateStages, stage_series_configs: finalConfigs }] }),
      })
    }
    if (url.pathname === '/api/bots/mine') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"bots":[]}' })
    }
    if (url.pathname === '/api/contests/7001' && request.method() === 'GET') {
      const detail = contestDetail(status, final8Games)
      if (options.omitSettings) delete (detail.contest as Partial<typeof detail.contest>).stage_series_settings
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail) })
    }
    if (url.pathname === '/api/contests/7001' && request.method() === 'PATCH') {
      const body = request.postDataJSON() as {
        stage_series_settings: { final8: { games_per_pair: number } }
      }
      writes.push({ method: request.method(), pathname: url.pathname, body: body as unknown as Record<string, unknown> })
      if (options.patchFails) {
        return route.fulfill({ status: 409, contentType: 'application/json', body: '{"detail":"设置已被并发修改"}' })
      }
      final8Games = body.stage_series_settings.final8.games_per_pair
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contest: contestDetail(status, final8Games).contest }) })
    }
    if (url.pathname === '/api/contests/7001/publish' && request.method() === 'POST') {
      writes.push({ method: request.method(), pathname: url.pathname })
      status = 'published'
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contest: contestDetail(status, final8Games).contest }) })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
  return { writes }
}

async function installPrelimContestApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: ORGANIZER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ templates: [{ id: 'holdem_prelim_swiss', stages: prelimTemplateStages, stage_series_configs: prelimConfig }] }),
      })
    }
    if (url.pathname === '/api/bots/mine') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"bots":[]}' })
    }
    if (url.pathname === '/api/contests/7002' && request.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          contest: {
            id: 7002,
            title: '德州瑞士预赛',
            status: 'open',
            organizer_id: ORGANIZER.id,
            template_id: 'holdem_prelim_swiss',
            game_id: 'holdem',
            current_stage_idx: 0,
            stages_json: JSON.stringify([{
              ...prelimTemplateStages[0],
              games_per_pair: 2,
              swiss_extra_rounds: 2,
              series_scoring: 'independent_scoring_game_points_v1',
            }]),
            stage_series_settings: { prelim: { games_per_pair: 2, swiss_extra_rounds: 2 } },
          },
          entries: [],
          entries_total: 4,
          pairings: [],
          standings: [],
          stage_standings: [],
          is_organizer: true,
          my_entry: null,
          estimate: {
            estimated_matches: 12,
            eta_seconds: 1_680,
            stages: [{
              stage_key: 'prelim',
              participant_count: 4,
              conceptual_pairings: 6,
              effective_rounds: 3,
              games_per_pair: 2,
              estimated_matches: 12,
              estimated_execution_legs: 12,
              eta_seconds: 1_680,
            }],
          },
        }),
      })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
}

test('final stage settings project scale and are saved before publishing', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('theme', 'dark'))
  const monitor = monitorBrowser(page)
  const api = await installFinalContestApi(page, { omitSettings: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#/contests/7001')

  const fairness = page.locator('[data-slot="data-region"]').filter({ has: page.getByRole('heading', { name: '赛制公平性与规模' }) })
  await expect(fairness).toBeVisible()
  const final8 = fairness.getByRole('group', { name: 'Top 8 决胜' })
  await expect(final8).toContainText('28 组')
  await expect(final8).toContainText('112 场')
  await final8.getByRole('combobox', { name: '每对选手计分场数' }).click()
  await page.getByRole('option', { name: '8 场计分', exact: true }).click()
  await expect(final8).toContainText('224 场')
  await expect(page.getByText('预计 314 场', { exact: true })).toBeVisible()
  await expect(fairness.getByRole('alert')).toContainText('预计超过 8 小时')

  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('决赛全员循环排位每对选手 2 场计分')
  await expect(dialog).toContainText('Top 8 决胜每对选手 8 场计分')
  await expect(dialog).toContainText('预计共 314 场计分')
  await dialog.getByRole('button', { name: '确认发布' }).click()

  await expect(page.getByText('排期已发布', { exact: true }).first()).toBeVisible()
  await expect(fairness.getByText('已冻结', { exact: true })).toBeVisible()
  expect(api.writes.map(({ method, pathname }) => `${method} ${pathname}`)).toEqual([
    'PATCH /api/contests/7001',
    'POST /api/contests/7001/publish',
  ])
  expect(api.writes[0]?.body).toMatchObject({
    stage_series_settings: {
      qualify: { games_per_pair: 2 },
      final8: { games_per_pair: 8 },
    },
  })
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await expect(page.locator('html')).toHaveClass(/dark/)
  await monitor.expectClean()
})

test('Swiss live projection respects the no-repeat cap for small cohorts', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await installPrelimContestApi(page)
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/#/contests/7002')

  const fairness = page.locator('[data-slot="data-region"]').filter({ has: page.getByRole('heading', { name: '赛制公平性与规模' }) })
  const prelim = fairness.getByRole('group', { name: '预赛瑞士轮' })
  await expect(prelim).toContainText('6 组')
  await expect(prelim).toContainText('12 场')
  await expect(prelim).toContainText('3 轮')

  await prelim.getByRole('combobox', { name: '额外瑞士轮' }).click()
  await page.getByRole('option', { name: '不增加', exact: true }).click()
  await expect(prelim).toContainText('4 组')
  await expect(prelim).toContainText('8 场')
  await expect(prelim).toContainText('2 轮')

  await prelim.getByRole('combobox', { name: '额外瑞士轮' }).click()
  await page.getByRole('option', { name: '增加 4 轮', exact: true }).click()
  await expect(prelim).toContainText('6 组')
  await expect(prelim).toContainText('12 场')
  await expect(prelim).toContainText('3 轮')

  await prelim.getByRole('combobox', { name: '每对选手计分场数' }).click()
  await page.getByRole('option', { name: '4 场计分', exact: true }).click()
  await expect(prelim).toContainText('24 场')
  await expect(fairness.getByRole('alert')).toHaveCount(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('publishing stops when the fairness settings PATCH fails', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const api = await installFinalContestApi(page, { patchFails: true })
  await page.goto('/#/contests/7001')

  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认发布' }).click()
  await expect(page.getByText('设置已被并发修改')).toBeVisible()
  expect(api.writes.map(({ method, pathname }) => `${method} ${pathname}`)).toEqual([
    'PATCH /api/contests/7001',
  ])
  await monitor.expectClean([{
    kind: 'http',
    method: 'PATCH',
    status: 409,
    pathname: '/api/contests/7001',
  }])
})

test('built-in id with a custom stage graph publishes without injecting template defaults', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: ORGANIZER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ templates: [{ id: 'holdem_final_ranked', stages: finalTemplateStages, stage_series_configs: finalConfigs }] }),
      })
    }
    if (url.pathname === '/api/bots/mine') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"bots":[]}' })
    }
    if (url.pathname === '/api/contests/7003' && request.method() === 'GET') {
      const detail = contestDetail('open', 4)
      detail.contest.id = 7003
      detail.contest.title = '保留旧自定义阶段'
      detail.contest.stages_json = JSON.stringify([{ key: 'custom', type: 'swiss', rounds: 2 }])
      detail.contest.stage_series_settings = {}
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail) })
    }
    if (url.pathname === '/api/contests/7003/publish' && request.method() === 'POST') {
      writes.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ contest: { id: 7003, status: 'published' } }) })
    }
    if (url.pathname === '/api/contests/7003' && request.method() === 'PATCH') {
      writes.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 400, contentType: 'application/json', body: '{"detail":"must not patch"}' })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })

  await page.goto('/#/contests/7003')
  await expect(page.getByRole('heading', { name: '赛制公平性与规模' })).toHaveCount(0)
  await expect(page.getByText('冻结阶段拓扑与内置模板不一致，已停用公平性设置编辑；发布时将保留当前冻结阶段。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '截止报名·出排期' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认发布' }).click()
  await expect.poll(() => writes).toEqual(['POST /api/contests/7003/publish'])
  await monitor.expectClean()
})

test('template capability failure keeps an open contest from silent default publication', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const writes: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/auth/me') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: ORGANIZER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"temporary unavailable"}' })
    }
    if (url.pathname === '/api/bots/mine') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"bots":[]}' })
    }
    if (url.pathname === '/api/contests/7001' && request.method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(contestDetail('open', 4)) })
    }
    if (request.method() === 'PATCH' || request.method() === 'POST') {
      writes.push(`${request.method()} ${url.pathname}`)
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })

  await page.goto('/#/contests/7001')
  await expect(page.getByText(/公平性配置加载失败/)).toBeVisible()
  await expect(page.getByRole('button', { name: '截止报名·出排期' })).toBeDisabled()
  expect(writes).toEqual([])
  await monitor.expectClean([
    {
      kind: 'http',
      method: 'GET',
      status: 503,
      pathname: '/api/contests/templates',
      search: '?game=holdem',
    },
    {
      // React development StrictMode intentionally remounts effects once.
      // Both failed reads are expected; neither may enable publication.
      kind: 'http',
      method: 'GET',
      status: 503,
      pathname: '/api/contests/templates',
      search: '?game=holdem',
    },
  ])
})
