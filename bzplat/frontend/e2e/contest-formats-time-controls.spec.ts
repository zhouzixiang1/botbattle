import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'
import {
  GAME_TIME_CONTROL_REGISTRY_RESPONSE,
  GOMOKU_TEMPLATE_TIME_CONTROLS,
  HOLDEM_TEMPLATE_TIME_CONTROL,
  PENCIL_TEMPLATE_TIME_CONTROLS,
} from './time-control-fixtures'

const ORGANIZER = {
  id: 912,
  username: 'format-organizer',
  display_name: '赛制组织者',
  email: 'format-organizer@example.test',
  role: 'organizer',
  email_verified: 1,
}

const templatesByGame = {
  holdem: [{
    id: 'holdem_rr',
    name: '德州单循环',
    game_id: 'holdem',
    stages: [{ key: 'rr', type: 'round_robin' }],
    ...HOLDEM_TEMPLATE_TIME_CONTROL,
  }],
  pencil: [{
    id: 'pencil_drr',
    name: '点格棋全员双循环',
    game_id: 'pencil',
    allows_navigation_source_contest: true,
    stages: [{ key: 'league', type: 'double_round_robin' }],
    ...PENCIL_TEMPLATE_TIME_CONTROLS,
  }, {
    id: 'pencil_group_drr',
    name: '点格棋随机均衡分组双循环',
    game_id: 'pencil',
    allows_navigation_source_contest: true,
    stages: [{
      key: 'groups',
      type: 'group_double_round_robin',
      group_count: 2,
      group_assignment: 'secure_random_balanced_v1',
      overall_ranking: 'cross_group_fair_v1',
    }],
    stage_format_configs: [{ stage_key: 'groups', field: 'group_count', min: 2 }],
    ...PENCIL_TEMPLATE_TIME_CONTROLS,
  }],
  gomoku: [{
    id: 'gomoku_seeded_group_drr_final',
    name: '保护种子分组双循环 → 决赛双循环',
    game_id: 'gomoku',
    recommended_min: 22,
    recommended_max: 26,
    participant_range_is_strict: true,
    requires_source_contest: true,
    stages: [{
      key: 'groups',
      type: 'group_double_round_robin',
      group_count: 4,
      advance_per_group: 2,
      group_assignment: 'protected_seed_random_balanced_v1',
      overall_ranking: 'cross_group_fair_v1',
    }, {
      key: 'final',
      type: 'double_round_robin',
      ranking_mode: 'replace_top',
    }],
    time_controls: [{ ...GOMOKU_TEMPLATE_TIME_CONTROLS.time_controls[1], is_default: true }],
    default_time_control_id: 'gomoku_per_side_total_300s_v1',
  }],
} as const

interface ContestApiOptions {
  emptyTemplates?: boolean
  createContest?: (body: Record<string, unknown>) => MockApiResponse | Promise<MockApiResponse>
  sourceCandidates?: (request: {
    gameId: string | null
    query: string
    limit: number
  }) => MockApiResponse | Promise<MockApiResponse>
  contestList?: (request: {
    gameId: string
    page: number
    perPage: number
  }) => MockApiResponse | Promise<MockApiResponse>
}

interface MockApiResponse {
  status?: number
  body: unknown
  onSettled?: () => void
}

async function installContestApi(page: Page, options: ContestApiOptions = {}) {
  let createdBody: Record<string, unknown> | null = null
  const requestedApiUrls: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    requestedApiUrls.push(`${request.method()} ${url.pathname}${url.search}`)
    if (url.pathname === '/api/auth/me') {
      expect(request.headers().authorization).toBeUndefined()
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: ORGANIZER }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/contests/templates') {
      const game = url.searchParams.get('game') as keyof typeof templatesByGame
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ templates: options.emptyTemplates ? [] : templatesByGame[game] || [] }),
      })
    }
    if (url.pathname === '/api/contests/source-candidates' && request.method() === 'GET') {
      const response = options.sourceCandidates
        ? await options.sourceCandidates({
            gameId: url.searchParams.get('game_id'),
            query: url.searchParams.get('query') || '',
            limit: Number(url.searchParams.get('limit')),
          })
        : {
            body: {
              candidates: [
                { id: 101, title: '来源模拟赛一' },
                { id: 102, title: '来源模拟赛二' },
              ],
              has_more: false,
            },
          }
      try {
        return await route.fulfill({
          status: response.status || 200,
          contentType: 'application/json',
          body: JSON.stringify(response.body),
        })
      } finally {
        response.onSettled?.()
      }
    }
    if (url.pathname === '/api/contests' && request.method() === 'GET') {
      const response = options.contestList
        ? await options.contestList({
            gameId: url.searchParams.get('game_id') || '',
            page: Number(url.searchParams.get('page')),
            perPage: Number(url.searchParams.get('per_page')),
          })
        : { body: { contests: [], page: 1, per_page: 20, total: 0 } }
      try {
        return await route.fulfill({
          status: response.status || 200,
          contentType: 'application/json',
          body: JSON.stringify(response.body),
        })
      } finally {
        response.onSettled?.()
      }
    }
    if (url.pathname === '/api/contests' && request.method() === 'POST') {
      createdBody = request.postDataJSON() as Record<string, unknown>
      const response = options.createContest
        ? await options.createContest(createdBody)
        : { body: { contest: { id: 8001, ...createdBody } } }
      try {
        return await route.fulfill({
          status: response.status || 200,
          contentType: 'application/json',
          body: JSON.stringify(response.body),
        })
      } finally {
        response.onSettled?.()
      }
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
  return {
    createdBody: () => createdBody,
    requestedApiUrls: () => [...requestedApiUrls],
  }
}

async function openGomokuCreateForm(page: Page) {
  await page.goto('/#/contests')
  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  await form.getByRole('combobox').first().click()
  await page.getByRole('option', { name: '五子棋', exact: true }).last().click()
  await expect(form.getByText('限 22–26 人', { exact: true })).toBeVisible()
  return form
}

test('empty template response remains stable without an initialization render loop', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const api = await installContestApi(page, { emptyTemplates: true })
  await page.goto('/#/contests')
  await expect(page.getByRole('heading', { name: '锦标赛', exact: true })).toBeVisible()
  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  await expect(form.getByRole('combobox', { name: '赛制' })).toBeDisabled()
  await expect(form.getByRole('button', { name: '创建赛事', exact: true })).toBeDisabled()
  await expect(form.getByText('模板决定对阵覆盖、计分场数与预计耗时。')).toBeVisible()
  expect(api.requestedApiUrls().filter((url) => url === 'GET /api/contests?page=1&per_page=20')).toHaveLength(1)
  await monitor.expectClean()
})

test('Pencil preset submits independent format, time control, and group count at 390px', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  let releaseSourceCandidates: () => void = () => undefined
  const sourceCandidatesGate = new Promise<void>((resolve) => { releaseSourceCandidates = resolve })
  let sourceCandidatesStarted = false
  const api = await installContestApi(page, {
    sourceCandidates: async () => {
      sourceCandidatesStarted = true
      await sourceCandidatesGate
      return {
        body: {
          candidates: [
            { id: 101, title: '来源模拟赛一' },
            { id: 102, title: '来源模拟赛二' },
          ],
          has_more: false,
        },
      }
    },
  })
  await page.goto('/#/contests')
  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  await form.getByRole('combobox').first().click()
  await page.getByRole('option', { name: '点格棋', exact: true }).last().click()
  try {
    await expect.poll(() => sourceCandidatesStarted).toBe(true)
    await form.getByRole('button', { name: '线上预赛 · 分组双循环', exact: true }).click()
    await expect(form.getByRole('combobox', { name: '赛制' })).toContainText('分组')
    await expect(form.getByRole('combobox', { name: '对局时限' })).toContainText('1 秒')
  } finally {
    releaseSourceCandidates()
  }
  await form.getByLabel('分组数量').fill('3')
  await form.getByLabel('标题').fill('点格棋线上分组预赛')
  const navigation = form.getByRole('combobox', { name: '关联赛事（可选）' })
  await expect(navigation).toContainText('不关联其他赛事')
  await navigation.click()
  await page.getByRole('option', { name: '来源模拟赛一 · #101' }).click()
  await expect(form.getByText('不复制名单、成绩或晋级关系。', { exact: false })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  expect((await form.getByRole('combobox', { name: '对局时限' }).boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await form.getByRole('button', { name: '创建赛事', exact: true }).click()
  await expect.poll(() => api.createdBody()).toMatchObject({
    game_id: 'pencil',
    template_id: 'pencil_group_drr',
    time_control_id: 'pencil_per_decision_1s_v1',
    stage_format_settings: { groups: { group_count: 3 } },
    source_contest_id: 101,
  })
  const sourceCandidatesUrl = 'GET /api/contests/source-candidates?game_id=pencil&limit=50'
  expect(api.requestedApiUrls().filter((url) => url === sourceCandidatesUrl)).toEqual([
    sourceCandidatesUrl,
  ])
  await monitor.expectClean()
})

test('Gomoku protected-seed source selector consumes the minimal server-authoritative contract', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const sourceRequests: Array<{ gameId: string | null; query: string; limit: number }> = []
  const api = await installContestApi(page, {
    sourceCandidates: (request) => {
      sourceRequests.push(request)
      return {
        body: {
          candidates: [
            { id: 101, title: '来源模拟赛一' },
            { id: 102, title: '来源模拟赛二' },
          ],
          has_more: false,
        },
      }
    },
  })
  const form = await openGomokuCreateForm(page)
  const submit = form.getByRole('button', { name: '创建赛事', exact: true })
  await expect(submit).toBeDisabled()
  const source = form.getByRole('combobox', { name: '保护种子来源模拟赛' })
  await source.click()
  await expect(page.getByRole('option', { name: '来源模拟赛一 · #101' })).toBeVisible()
  await expect(page.getByRole('option', { name: '来源模拟赛二 · #102' })).toBeVisible()
  await page.getByRole('option', { name: '来源模拟赛一 · #101' }).click()
  expect(sourceRequests).toEqual([{ gameId: 'gomoku', query: '', limit: 50 }])
  expect(api.requestedApiUrls().some((url) => url.includes('/api/contests?') && url.includes('status=finished'))).toBe(false)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('Gomoku protected-seed selector searches one bounded endpoint for an old exact ID', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const sourceRequests: Array<{ gameId: string | null; query: string; limit: number }> = []
  const api = await installContestApi(page, {
    sourceCandidates: (request) => {
      sourceRequests.push(request)
      if (request.query === '') {
        return {
          body: {
            candidates: Array.from({ length: 50 }, (_, index) => ({
              id: 10_000 + index,
              title: `最近正式模拟赛 ${index + 1}`,
            })),
            has_more: true,
          },
        }
      }
      if (request.query === '7') {
        return {
          body: {
            candidates: [{ id: 7, title: '历史正式模拟赛' }],
            has_more: false,
          },
        }
      }
      throw new Error(`unexpected source query ${request.query}`)
    },
  })
  const form = await openGomokuCreateForm(page)
  const source = form.getByRole('combobox', { name: '保护种子来源模拟赛' })
  await expect(source).toBeEnabled()
  await expect(form.getByRole('status')).toContainText('候选超过 50 项')
  await form.getByLabel('搜索保护种子来源').fill('7')
  await expect.poll(() => sourceRequests.map((request) => request.query)).toEqual(['', '7'])
  await source.click()
  await expect(page.getByRole('option', { name: '历史正式模拟赛 · #7' })).toBeVisible()
  await page.getByRole('option', { name: '历史正式模拟赛 · #7' }).click()
  await expect(source).toContainText('历史正式模拟赛')
  expect(sourceRequests).toEqual([
    { gameId: 'gomoku', query: '', limit: 50 },
    { gameId: 'gomoku', query: '7', limit: 50 },
  ])
  expect(api.requestedApiUrls().filter((url) => url.includes('/api/contests/source-candidates'))).toEqual([
    'GET /api/contests/source-candidates?game_id=gomoku&limit=50',
    'GET /api/contests/source-candidates?game_id=gomoku&limit=50&query=7',
  ])
  expect(api.requestedApiUrls().some((url) => url.includes('/api/contests?') && url.includes('status=finished'))).toBe(false)
  await monitor.expectClean()
})

test('Gomoku source search clears a prior selection and disables submit after failure', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await installContestApi(page, {
    sourceCandidates: ({ query }) => query === 'broken'
      ? { status: 503, body: { detail: '来源搜索暂时失败' } }
      : { body: { candidates: [{ id: 101, title: '可用来源模拟赛' }], has_more: false } },
  })
  const form = await openGomokuCreateForm(page)
  const source = form.getByRole('combobox', { name: '保护种子来源模拟赛' })
  await source.click()
  await page.getByRole('option', { name: '可用来源模拟赛 · #101' }).click()
  const submit = form.getByRole('button', { name: '创建赛事', exact: true })
  await expect(submit).toBeEnabled()

  await form.getByLabel('搜索保护种子来源').fill('broken')
  await expect(form.getByText('来源搜索暂时失败', { exact: true })).toBeVisible()
  await expect(source).toBeDisabled()
  await expect(source).toContainText('暂无可用模拟赛')
  await expect(submit).toBeDisabled()
  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 503,
    pathname: '/api/contests/source-candidates',
    search: '?game_id=gomoku&limit=50&query=broken',
  }])
})

test('Gomoku source search keeps the newest result when an older request resolves last', async ({ page }) => {
  const sourceQueries: string[] = []
  let releaseSlow: () => void = () => undefined
  const slowGate = new Promise<void>((resolve) => { releaseSlow = resolve })
  let slowSettled = false
  await installContestApi(page, {
    sourceCandidates: async ({ query }) => {
      sourceQueries.push(query)
      if (query === 'slow') {
        await slowGate
        return {
          body: { candidates: [{ id: 201, title: '过期慢响应' }], has_more: false },
          onSettled: () => { slowSettled = true },
        }
      }
      if (query === 'fast') {
        return {
          body: { candidates: [{ id: 202, title: '当前快速响应' }], has_more: false },
        }
      }
      return { body: { candidates: [{ id: 101, title: '默认来源' }], has_more: false } }
    },
  })
  const form = await openGomokuCreateForm(page)
  const query = form.getByLabel('搜索保护种子来源')
  const source = form.getByRole('combobox', { name: '保护种子来源模拟赛' })
  await expect(source).toBeEnabled()

  await query.fill('slow')
  await expect.poll(() => sourceQueries.includes('slow')).toBe(true)
  await query.fill('fast')
  await expect.poll(() => sourceQueries.includes('fast')).toBe(true)
  await expect(source).toBeEnabled()
  await source.click()
  await expect(page.getByRole('option', { name: '当前快速响应 · #202' })).toBeVisible()
  await expect(page.getByRole('option', { name: '过期慢响应 · #201' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  releaseSlow()
  await expect.poll(() => slowSettled).toBe(true)
  await source.click()
  await expect(page.getByRole('option', { name: '当前快速响应 · #202' })).toBeVisible()
  await expect(page.getByRole('option', { name: '过期慢响应 · #201' })).toHaveCount(0)
})

test('contest list keeps the newest game filter when an older request resolves last', async ({ page }) => {
  const listGames: string[] = []
  let releasePencil: () => void = () => undefined
  const pencilGate = new Promise<void>((resolve) => { releasePencil = resolve })
  let pencilSettled = false
  await installContestApi(page, {
    contestList: async ({ gameId }) => {
      listGames.push(gameId)
      if (gameId === 'pencil') {
        await pencilGate
        return {
          body: {
            contests: [{ id: 301, title: '过期点格棋列表', status: 'open', game_id: 'pencil' }],
            page: 1,
            per_page: 20,
            total: 1,
          },
          onSettled: () => { pencilSettled = true },
        }
      }
      if (gameId === 'gomoku') {
        return {
          body: {
            contests: [{ id: 302, title: '当前五子棋列表', status: 'open', game_id: 'gomoku' }],
            page: 1,
            per_page: 20,
            total: 1,
          },
        }
      }
      return { body: { contests: [], page: 1, per_page: 20, total: 0 } }
    },
  })
  await page.goto('/#/contests')
  await expect(page.getByText('当前条件下暂无赛事')).toBeVisible()
  const filter = page.getByRole('region', { name: '赛事筛选与创建' }).getByRole('combobox')

  await filter.click()
  await page.getByRole('option', { name: '点格棋', exact: true }).click()
  await expect.poll(() => listGames.includes('pencil')).toBe(true)
  await filter.click()
  await page.getByRole('option', { name: '五子棋', exact: true }).click()
  await expect(page.getByText('当前五子棋列表', { exact: true })).toBeVisible()
  await expect(page.getByText('过期点格棋列表', { exact: true })).toHaveCount(0)
  releasePencil()
  await expect.poll(() => pencilSettled).toBe(true)
  await expect(page.getByText('当前五子棋列表', { exact: true })).toBeVisible()
  await expect(page.getByText('过期点格棋列表', { exact: true })).toHaveCount(0)
})

test('contest creation refreshes the current filter and page after a delayed POST', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const listQueries: Array<{ gameId: string; page: number; perPage: number }> = []
  let releaseCreate: () => void = () => undefined
  const createGate = new Promise<void>((resolve) => { releaseCreate = resolve })
  let createStarted = false
  let createSettled = false
  const api = await installContestApi(page, {
    createContest: async (body) => {
      createStarted = true
      await createGate
      return {
        body: { contest: { id: 8001, ...body } },
        onSettled: () => { createSettled = true },
      }
    },
    contestList: (query) => {
      listQueries.push(query)
      const scope = query.gameId || 'all'
      return {
        body: {
          contests: [{
            id: scope === 'pencil' ? 400 + query.page : 300 + query.page,
            title: `${scope}-page-${query.page}`,
            status: 'open',
            game_id: query.gameId || 'holdem',
          }],
          page: query.page,
          per_page: query.perPage,
          total: 45,
        },
      }
    },
  })

  await page.goto('/#/contests')
  await expect(page.getByText('all-page-1', { exact: true })).toBeVisible()
  await page.getByRole('region', { name: '赛事筛选与创建' })
    .getByRole('button', { name: '创建赛事', exact: true })
    .click()
  const form = page.locator('form')
  await form.getByLabel('标题').fill('延迟创建赛事')
  await form.getByRole('button', { name: '创建赛事', exact: true }).click()
  await expect.poll(() => createStarted).toBe(true)

  const filter = page.getByRole('region', { name: '赛事筛选与创建' }).getByRole('combobox')
  await filter.click()
  await page.getByRole('option', { name: '点格棋', exact: true }).click()
  await expect(page.getByText('pencil-page-1', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '第 2 页', exact: true }).click()
  await expect(page.getByText('pencil-page-2', { exact: true })).toBeVisible()

  releaseCreate()
  await expect.poll(() => createSettled).toBe(true)
  await expect.poll(() => listQueries.at(-1)).toEqual({ gameId: 'pencil', page: 2, perPage: 20 })
  await expect(page.getByText('pencil-page-2', { exact: true })).toBeVisible()
  expect(api.requestedApiUrls().filter((url) => url === 'GET /api/contests?page=1&per_page=20')).toHaveLength(1)
  expect(api.requestedApiUrls().filter((url) => url === 'GET /api/contests?game_id=pencil&page=2&per_page=20')).toHaveLength(2)
  await monitor.expectClean()
})

test('challenge shows alternate-unrated and human Bot-only timing at 390px', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  let gamesReads = 0
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/auth/me') {
      expect(route.request().headers().authorization).toBeUndefined()
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: { ...ORGANIZER, role: 'user' } }) })
    }
    if (url.pathname === '/api/notifications/unread-count') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
    }
    if (url.pathname === '/api/local-ai/agents') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' })
    }
    if (url.pathname === '/api/games') {
      gamesReads += 1
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(GAME_TIME_CONTROL_REGISTRY_RESPONSE) })
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"unexpected mock request"}' })
  })
  await page.goto('/#/challenge')
  const form = page.getByTestId('challenge-form')
  await form.getByRole('combobox', { name: '游戏' }).click()
  await page.getByRole('option', { name: '点格棋', exact: true }).click()
  await form.getByRole('combobox', { name: '对局时限' }).click()
  await page.getByRole('option', { name: '每步最多 1 秒 · 练习' }).click()
  await expect(form.getByText('替代时限属于练习模式，本局不计平台排行榜。')).toBeVisible()
  await form.getByRole('button', { name: '我亲自上场', exact: true }).click()
  await expect(form.getByText('非对称练习：所选时限只约束 Bot；你仍使用页面的防挂机时限。')).toBeVisible()
  await expect(form.getByText(/只计 Bot 的完整请求到完整响应/)).toBeVisible()
  expect((await form.getByRole('combobox', { name: '对局时限' }).boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  expect(gamesReads).toBe(1)
  await monitor.expectClean()
})

test('board reducers initialize frozen clocks and reset per-decision turns', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.goto('/')
  const states = await page.evaluate(async () => {
    const [{ reducePencilEvents }, { reduceGomokuEvents }] = await Promise.all([
      import('/src/games/pencil/reducer.ts'),
      import('/src/games/gomoku/reducer.ts'),
    ])
    const pencilStart = {
      type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11,
      time_control: {
        id: 'pencil_per_decision_1s_v1', mode: 'per_decision', seconds: 1, applies_to: 'both_bots',
      },
    }
    const pencilAfterUsed = reducePencilEvents([
      pencilStart,
      { type: 'turn', player: 0, pass_: 0 },
      { type: 'time_used', seat: 0, used: 0.7, remaining: 0.3 },
    ])
    const pencilNextTurn = reducePencilEvents([
      pencilStart,
      { type: 'turn', player: 0, pass_: 0 },
      { type: 'time_used', seat: 0, used: 0.7, remaining: 0.3 },
      { type: 'turn', player: 0, pass_: 0 },
    ])
    const gomoku = reduceGomokuEvents([{
      type: 'match_start', game_id: 'gomoku', size: 15,
      time_control: {
        id: 'gomoku_per_side_total_300s_v1', mode: 'per_side_total', seconds: 300, applies_to: 'both_bots',
      },
    }])
    const malformed = reducePencilEvents([{
      ...pencilStart,
      time_control: { ...pencilStart.time_control, private_seed: 'not-public' },
    }, { type: 'time_used', seat: 0, used: 0.7, remaining: 0.3, budget: 1 }])
    const malformedGomoku = reduceGomokuEvents([{
      type: 'match_start', game_id: 'gomoku', size: 15,
      time_control: {
        id: 'gomoku_per_side_total_300s_v1', mode: 'per_side_total', seconds: 300,
        applies_to: 'both_bots', private_seed: 'not-public',
      },
    }, { type: 'time_used', seat: 0, used: 1, remaining: 299, budget: 300 }])
    const humanPencil = reducePencilEvents([{
      ...pencilStart,
      time_control: { ...pencilStart.time_control, applies_to: 'bot_only' },
    },
    { type: 'turn', player: 0, pass_: 0 },
    { type: 'time_used', seat: 0, used: 0.4, remaining: 0.6, budget: 1 },
    { type: 'turn', player: 1, pass_: 0 }])
    const humanGomoku = reduceGomokuEvents([{
      type: 'match_start', game_id: 'gomoku', size: 15,
      time_control: {
        id: 'gomoku_per_side_total_300s_v1', mode: 'per_side_total', seconds: 300, applies_to: 'bot_only',
      },
    }, { type: 'time_used', seat: 0, used: 1, remaining: 299, budget: 300 }])
    return {
      pencilAfterUsed: {
        used: pencilAfterUsed.timeUsed,
        remaining: pencilAfterUsed.timeRemaining,
      },
      pencilNextTurn: {
        used: pencilNextTurn.timeUsed,
        remaining: pencilNextTurn.timeRemaining,
      },
      gomoku: { budget: gomoku.timeBudget, remaining: gomoku.timeRemaining },
      malformed: malformed.timeRemaining,
      malformedGomoku: { budget: malformedGomoku.timeBudget, remaining: malformedGomoku.timeRemaining },
      humanPencil: { used: humanPencil.timeUsed, remaining: humanPencil.timeRemaining },
      humanGomoku: { budget: humanGomoku.timeBudget, remaining: humanGomoku.timeRemaining },
    }
  })
  expect(states).toEqual({
    pencilAfterUsed: { used: [0.7, 0], remaining: [0.3, 1] },
    pencilNextTurn: { used: [0, 0], remaining: [1, 1] },
    gomoku: { budget: 300, remaining: [300, 300] },
    malformed: null,
    malformedGomoku: { budget: null, remaining: [null, null] },
    humanPencil: { used: [0.4, null], remaining: [0.6, null] },
    humanGomoku: { budget: 300, remaining: [299, null] },
  })
  await monitor.expectClean()
})
