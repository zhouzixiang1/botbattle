import { expect, test, type Page } from '@playwright/test'

import { loginThroughUi, monitorBrowser, runCleanupTasks, withCleanup } from './helpers'

const ADMIN = process.env.BZ_E2E_ADMIN || 'qa_admin'

interface RuntimeState {
  action_timeout_sec: number
  max_concurrent_matches: number
  contest_default_rest_minutes: number
  auto_match: {
    enabled: boolean
    interval_sec: number
    min_idle_sec: number
    bot_cooldown: number
    stale_sec: number
    reserve_slots: number
    placement_games: number
    max_per_round: number
    daily_cap: number
  }
}

function runtimePatch(state: RuntimeState) {
  return {
    action_timeout_sec: state.action_timeout_sec,
    max_concurrent_matches: state.max_concurrent_matches,
    contest_default_rest_minutes: state.contest_default_rest_minutes,
    auto_match_enabled: state.auto_match.enabled,
    auto_match_interval_sec: state.auto_match.interval_sec,
    auto_match_min_idle_sec: state.auto_match.min_idle_sec,
    auto_match_bot_cooldown: state.auto_match.bot_cooldown,
    auto_match_stale_sec: state.auto_match.stale_sec,
    auto_match_reserve_slots: state.auto_match.reserve_slots,
    auto_match_placement_games: state.auto_match.placement_games,
    auto_match_max_per_round: state.auto_match.max_per_round,
    auto_match_daily_cap: state.auto_match.daily_cap,
  }
}

async function expectNoRootOverflow(page: Page, label: string) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow, `${label} overflows viewport by ${overflow}px`).toBeLessThanOrEqual(1)
}

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  expect((await response.json() as { qa_instance?: boolean }).qa_instance).toBe(true)
})

for (const viewport of [
  { name: 'desktop', width: 1440, height: 900, interactive: true },
  { name: 'laptop', width: 1280, height: 720, interactive: false },
  { name: 'mobile', width: 390, height: 844, interactive: false },
] as const) {
  test(`admin loads all nine tabs without runtime/network/layout errors (${viewport.name})`, async ({ page }) => {
    test.setTimeout(viewport.interactive ? 150_000 : 90_000)
    let runtimeSnapshot: RuntimeState | null = null
    await withCleanup(async () => {
      await page.setViewportSize(viewport)
      const monitor = monitorBrowser(page)
      await loginThroughUi(page, ADMIN)
      if (viewport.interactive) {
        const runtimeResponse = await page.request.get('/api/admin/settings/runtime')
        expect(runtimeResponse.status(), await runtimeResponse.text()).toBe(200)
        runtimeSnapshot = await runtimeResponse.json() as RuntimeState
      }
      await page.goto('/#/admin')

    await expect(page.getByText('平台总览统计', { exact: true })).toBeVisible()
    await expectNoRootOverflow(page, 'dashboard')

    await page.getByRole('button', { name: '用户', exact: true }).click()
    const userSearch = page.getByPlaceholder('搜索用户名/邮箱')
    await expect(userSearch).toBeVisible()
    if (viewport.interactive) {
      const filteredUsersPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/admin/users' &&
          url.search === `?page=1&per_page=20&q=${encodeURIComponent(ADMIN)}`
      })
      await userSearch.fill(ADMIN)
      const filteredUsersResponse = await filteredUsersPromise
      expect(filteredUsersResponse.status(), await filteredUsersResponse.text()).toBe(200)
      const filteredUsers = await filteredUsersResponse.json() as {
        users: Array<{
          username: string
          email: string
          real_name?: string
          phone?: string
          school?: string
          student_id?: string
        }>
      }
      expect(filteredUsers.users.length).toBeGreaterThan(0)
      expect(filteredUsers.users.every((user) =>
        `${user.username} ${user.email}`.toLowerCase().includes(ADMIN.toLowerCase()),
      )).toBe(true)
      const usersTable = page.getByRole('table')
      await expect(usersTable.locator('tbody tr')).toHaveCount(filteredUsers.users.length)
      await expect(usersTable.getByRole('link', { name: ADMIN, exact: true })).toBeVisible()
      const unnamedUsersPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/admin/users' &&
          url.search === `?page=1&per_page=20&q=${encodeURIComponent(ADMIN)}&real_name=false`
      })
      await page.getByRole('combobox').filter({ hasText: '全部用户' }).click()
      await page.getByRole('option', { name: '未实名', exact: true }).click()
      const unnamedUsersResponse = await unnamedUsersPromise
      expect(unnamedUsersResponse.status(), await unnamedUsersResponse.text()).toBe(200)
      const unnamedUsers = await unnamedUsersResponse.json() as {
        users: Array<{
          username: string
          real_name?: string
          phone?: string
          school?: string
          student_id?: string
        }>
      }
      expect(unnamedUsers.users.length).toBeGreaterThan(0)
      expect(unnamedUsers.users.every((user) =>
        !user.real_name?.trim() ||
        !user.phone?.trim() ||
        !user.school?.trim() ||
        !user.student_id?.trim(),
      )).toBe(true)
      await expect(usersTable.locator('tbody tr')).toHaveCount(unnamedUsers.users.length)
      await expect(usersTable.getByRole('link', { name: ADMIN, exact: true })).toBeVisible()
    }
    await expectNoRootOverflow(page, 'users')

    await page.getByRole('button', { name: 'Bot', exact: true }).click()
    const botSearch = page.getByPlaceholder('搜索 Bot 名称')
    await expect(botSearch).toBeVisible()
    if (viewport.interactive) {
      const botQuery = 'tester1_holdem'
      const filteredBotsPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/admin/bots' &&
          url.search === `?page=1&per_page=20&q=${encodeURIComponent(botQuery)}`
      })
      await botSearch.fill(botQuery)
      const filteredBotsResponse = await filteredBotsPromise
      expect(filteredBotsResponse.status(), await filteredBotsResponse.text()).toBe(200)
      const filteredBots = await filteredBotsResponse.json() as {
        bots: Array<{ name: string; display_name?: string; owner_name?: string }>
      }
      expect(filteredBots.bots.length).toBeGreaterThan(0)
      expect(filteredBots.bots.every((bot) =>
        `${bot.name} ${bot.display_name || ''}`.toLowerCase().includes(botQuery),
      )).toBe(true)
      await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(filteredBots.bots.length)
      await expect(page.locator('a[href="#/user/tester1"]')).toBeVisible()
    }
    await expectNoRootOverflow(page, 'bots')

    await page.getByRole('button', { name: '对局记录', exact: true }).click()
    await expect(page.getByText(/共 \d+ 局/)).toBeVisible()
    if (viewport.interactive) {
      const completedMatchesPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/matches' &&
          url.search === '?status=completed&limit=20&offset=0'
      })
      await page.getByRole('combobox').filter({ hasText: '全部状态' }).click()
      await page.getByRole('option', { name: '已完成', exact: true }).click()
      const completedMatchesResponse = await completedMatchesPromise
      expect(completedMatchesResponse.status(), await completedMatchesResponse.text()).toBe(200)
      const completedMatches = await completedMatchesResponse.json() as {
        matches: Array<{ status: string }>
      }
      expect(completedMatches.matches.length).toBeGreaterThan(0)
      expect(completedMatches.matches.every((match) => match.status === 'completed')).toBe(true)
      await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(completedMatches.matches.length)
      await expect(page.getByText(/共 \d+ 局/)).toBeVisible()

      const abnormalMatchesPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/matches' &&
          url.search === '?status=completed&has_technical_incidents=true&limit=20&offset=0'
      })
      await page.getByRole('combobox').filter({ hasText: '全部诊断结果' }).click()
      await page.getByRole('option', { name: '含 Bot 技术故障', exact: true }).click()
      const abnormalMatchesResponse = await abnormalMatchesPromise
      expect(abnormalMatchesResponse.status(), await abnormalMatchesResponse.text()).toBe(200)
      const abnormalMatches = await abnormalMatchesResponse.json() as {
        matches: Array<{ result?: { technical_incidents_by_seat?: Record<string, number> } }>
      }
      expect(abnormalMatches.matches.length).toBeGreaterThan(0)
      expect(abnormalMatches.matches.every((match) =>
        Object.values(match.result?.technical_incidents_by_seat || {})
          .reduce((sum, value) => sum + Number(value || 0), 0) > 0,
      )).toBe(true)
      await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(abnormalMatches.matches.length)
      await expect(page.getByText(/Bot 技术故障 \d+ 次/).first()).toBeVisible()
    }
    await expectNoRootOverflow(page, 'matches')

    await page.getByRole('button', { name: '锦标赛', exact: true }).click()
    await expect(page.getByText(/共 \d+ 个锦标赛/)).toBeVisible()
    await expectNoRootOverflow(page, 'contests')

    await page.getByRole('button', { name: '赛制模板', exact: true }).click()
    const newTemplate = page.getByRole('button', { name: '+ 新建模板', exact: true })
    await expect(newTemplate).toBeVisible()
    if (viewport.interactive) {
      await newTemplate.click()
      await expect(page.getByRole('heading', { name: '新建模板', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '取消', exact: true }).click()
    }
    await expectNoRootOverflow(page, 'templates')

    await page.getByRole('button', { name: '运行时', exact: true }).click()
    await expect(page.getByRole('heading', { name: '运行时', exact: true })).toBeVisible()
    if (viewport.interactive) {
      const responsePromise = page.waitForResponse(
        (response) => response.request().method() === 'PATCH' && new URL(response.url()).pathname === '/api/admin/settings/runtime',
      )
      await page.getByRole('button', { name: '保存', exact: true }).click()
      expect((await responsePromise).status()).toBe(200)
      await expect(page.getByText('已保存并热更新', { exact: true })).toBeVisible()
    }
    await expectNoRootOverflow(page, 'runtime')

    await page.getByRole('button', { name: '日志', exact: true }).click()
    const logSearch = page.getByPlaceholder('对局 ID / Bot ID / 模块 / IP / 操作')
    await expect(logSearch).toBeVisible()
    if (viewport.interactive) {
      const auditLogPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/admin/logs' &&
          url.search === '?file=audit&limit=300'
      })
      await page.getByRole('button', { name: '审计日志', exact: true }).click()
      const auditLogResponse = await auditLogPromise
      expect(auditLogResponse.status(), await auditLogResponse.text()).toBe(200)
      const loginLogPromise = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return response.request().method() === 'GET' &&
          url.pathname === '/api/admin/logs' &&
          url.search === '?file=audit&limit=300&q=login'
      })
      await logSearch.fill('login')
      const loginLogResponse = await loginLogPromise
      expect(loginLogResponse.status(), await loginLogResponse.text()).toBe(200)
      const loginLog = await loginLogResponse.json() as { lines: string[] }
      expect(loginLog.lines.length).toBeGreaterThan(0)
      expect(loginLog.lines.every((line) => line.toLowerCase().includes('login'))).toBe(true)
      await expect(page.getByText(
        `匹配 ${loginLog.lines.length} 条记录 / ${loginLog.lines.length} 行`,
        { exact: true },
      )).toBeVisible()
    }
    await expectNoRootOverflow(page, 'logs')

    await page.getByRole('button', { name: '邮件', exact: true }).click()
    await expect(page.getByRole('button', { name: '邮件模板', exact: true })).toBeVisible()
    if (viewport.interactive) {
      await page.getByRole('button', { name: /发件箱/ }).click()
      await expect(page.getByText(/无发信记录|收件人/)).toBeVisible()
      await page.getByRole('button', { name: '邮件模板', exact: true }).click()
    }
    await expectNoRootOverflow(page, 'email')

      await monitor.expectClean()
    }, async () => {
      const tasks: Array<{ label: string; run: () => Promise<void> }> = []
      if (runtimeSnapshot) {
        const originalRuntime = runtimeSnapshot
        tasks.push({
          label: 'restore runtime settings',
          run: async () => {
            const restore = await page.request.patch('/api/admin/settings/runtime', {
              data: runtimePatch(originalRuntime),
            })
            expect(restore.status(), await restore.text()).toBe(200)
            const verify = await page.request.get('/api/admin/settings/runtime')
            expect(verify.status(), await verify.text()).toBe(200)
            expect(runtimePatch(await verify.json() as RuntimeState)).toEqual(runtimePatch(originalRuntime))
          },
        })
      }
      await runCleanupTasks(tasks)
    })
  })
}

test('contest admin exposes only phase-appropriate actions and flags invalid schedules', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  const base = {
    organizer_id: 1,
    created_at: '2026-08-09T09:00:00',
    ends_at: null,
    registration_opens_at: '2026-08-09T10:00:00',
    registration_closes_at: '2026-08-09T11:00:00',
    starts_at: '2026-08-09T12:00:00',
    template_id: 'holdem_rr',
    game_id: 'holdem',
  }
  const contests = [
    { ...base, id: 901, title: '阶段矩阵-草稿', status: 'draft' },
    { ...base, id: 902, title: '阶段矩阵-报名', status: 'open' },
    { ...base, id: 903, title: '阶段矩阵-已发布', status: 'published' },
    { ...base, id: 904, title: '阶段矩阵-进行中', status: 'running' },
    { ...base, id: 905, title: '阶段矩阵-休息', status: 'rest' },
    {
      ...base,
      id: 906,
      title: '阶段矩阵-已完成',
      status: 'finished',
      registration_closes_at: '2026-08-09T13:00:00',
    },
    { ...base, id: 907, title: '阶段矩阵-已取消', status: 'cancelled' },
  ]
  await page.route('**/api/admin/contests?page=1&per_page=20', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contests, total: contests.length }),
    })
  })

  await page.goto('/#/admin?tab=contests')
  await expect(page.getByText(`共 ${contests.length} 个锦标赛`, { exact: false })).toBeVisible()

  const row = (title: string) => page.getByRole('row').filter({ hasText: title })
  await expect(row('阶段矩阵-草稿').getByRole('button', { name: '开放报名', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-报名').getByRole('button', { name: '截止报名并发布排期', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-已发布').getByRole('button', { name: '开始比赛', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-进行中').getByRole('button', { name: '恢复性结束', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-进行中').getByRole('button', { name: '取消赛事', exact: true })).toHaveCount(0)
  await expect(row('阶段矩阵-休息').getByRole('button', { name: '进入下一阶段', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-休息').getByRole('button', { name: '恢复性结束', exact: true })).toBeVisible()

  const finished = row('阶段矩阵-已完成')
  await expect(finished.getByText('成绩已归档 · 只读', { exact: true })).toBeVisible()
  await expect(finished.getByRole('button', { name: /取消赛事|删除草稿|开始比赛|恢复性结束/ })).toHaveCount(0)
  await expect(finished.getByText('报名截止晚于比赛开始', { exact: true })).toBeVisible()
  await finished.getByRole('button', { name: '修正时间', exact: true }).click()
  const scheduleDialog = page.getByRole('dialog', { name: '编辑赛事时间' })
  await expect(scheduleDialog.getByText('报名截止晚于比赛开始', { exact: true })).toBeVisible()
  await expect(scheduleDialog.getByRole('button', { name: '保存时间', exact: true })).toBeDisabled()
  await scheduleDialog.getByLabel('报名截止').fill('2026-08-09T11:00')
  await expect(scheduleDialog.getByText('报名截止晚于比赛开始', { exact: true })).toHaveCount(0)
  await expect(scheduleDialog.getByRole('button', { name: '保存时间', exact: true })).toBeEnabled()
  await scheduleDialog.getByRole('button', { name: '取消', exact: true }).click()

  const cancelled = row('阶段矩阵-已取消')
  await expect(cancelled.getByText('已取消 · 可清理', { exact: true })).toBeVisible()
  await expect(cancelled.getByRole('button', { name: '删除草稿', exact: true })).toBeVisible()
  await expect(cancelled.getByRole('button', { name: '取消赛事', exact: true })).toHaveCount(0)
  await expect(page.getByText('React 19 · Tailwind v4 · shadcn/ui', { exact: true })).toHaveCount(0)
  await monitor.expectClean()
})

test('contest detail changes its primary content and actions with lifecycle stage', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  const commonContest = {
    organizer_id: 1,
    title: '阶段详情',
    description: '阶段化展示回归',
    game_id: 'holdem',
    template_id: 'holdem_rr',
    template_name: '德州单循环',
    stages_json: JSON.stringify([{ key: 'rr', type: 'round_robin', scoring: 'poker_3_1_0' }]),
    current_stage_idx: 0,
    registration_opens_at: '2026-08-09T10:00:00',
    starts_at: '2026-08-09T12:00:00',
    require_real_name: 0,
  }

  await page.route('**/api/contests/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/contests/906/official-results') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ready: true,
          results: [{
            rank: 1,
            entry_id: 1001,
            bot_id: 11,
            user_id: 21,
            points: 3,
            bot_name: 'winner_bot',
            owner_name: 'winner_user',
            awarded: '冠军',
          }],
        }),
      })
      return
    }
    const match = url.pathname.match(/^\/api\/contests\/(902|906)$/)
    if (!match) {
      await route.continue()
      return
    }
    const finished = match[1] === '906'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: {
          ...commonContest,
          id: Number(match[1]),
          status: finished ? 'finished' : 'open',
          title: finished ? '阶段详情-已完成' : '阶段详情-报名中',
          registration_closes_at: finished ? '2026-08-09T13:00:00' : '2026-08-09T11:00:00',
          official_results_ready: finished ? 1 : 0,
        },
        entries: finished ? [{ id: 1001, user_id: 21, bot_id: 11, bot_name: 'winner_bot' }] : [],
        pairings: finished ? [{
          id: 2001,
          bot_a_id: 11,
          bot_b_id: 12,
          bot_a_name: 'winner_bot',
          bot_b_name: 'runner_bot',
          stage_idx: 0,
          status: 'completed',
          match_winner: 0,
        }] : [],
        standings: finished ? [{ bot_id: 11, bot_name: 'winner_bot', points: 3, wins: 1, draws: 0, losses: 0, net_chips: 100 }] : [],
        entries_total: finished ? 1 : 0,
        my_entry: null,
      }),
    })
  })
  await page.route('**/api/bots/mine?game_id=holdem', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ bots: [] }) })
  })

  await page.goto('/#/contests/906')
  await expect(page.getByRole('tab', { name: /正式名次/ })).toHaveAttribute('data-state', 'active')
  await expect(page.getByRole('cell', { name: 'winner_bot', exact: true })).toBeVisible()
  await expect(page.getByText('冠军', { exact: true })).toBeVisible()
  await expect(page.getByText(/时间配置异常：报名截止时间晚于比赛开始时间/)).toBeVisible()
  await expect(page.getByRole('button', { name: /开放报名|截止报名|立即开赛|强制结束赛事/ })).toHaveCount(0)

  await page.goto('/#/contests/902')
  await expect(page.getByRole('tab', { name: /选手/ })).toHaveAttribute('data-state', 'active')
  await expect(page.getByRole('tab', { name: /对阵|阶段积分|正式名次/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '截止报名·出排期', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '立即开赛', exact: true })).toHaveCount(0)
  await monitor.expectClean()
})
