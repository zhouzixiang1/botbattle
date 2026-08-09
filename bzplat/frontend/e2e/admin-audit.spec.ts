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

interface JudgesState {
  games: Array<{ params: Array<{ key: string; value: number }> }>
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

function judgeValues(state: JudgesState): Record<string, number> {
  return Object.fromEntries(
    state.games.flatMap((game) => game.params.map((param) => [param.key, param.value])),
  )
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
  { name: 'mobile', width: 390, height: 844, interactive: false },
] as const) {
  test(`admin loads all ten tabs without runtime/network/layout errors (${viewport.name})`, async ({ page }) => {
    let runtimeSnapshot: RuntimeState | null = null
    let judgeSnapshot: Record<string, number> | null = null
    await withCleanup(async () => {
      await page.setViewportSize(viewport)
      const monitor = monitorBrowser(page)
      await loginThroughUi(page, ADMIN)
      if (viewport.interactive) {
        const runtimeResponse = await page.request.get('/api/admin/settings/runtime')
        expect(runtimeResponse.status(), await runtimeResponse.text()).toBe(200)
        runtimeSnapshot = await runtimeResponse.json() as RuntimeState
        const judgesResponse = await page.request.get('/api/admin/judges')
        expect(judgesResponse.status(), await judgesResponse.text()).toBe(200)
        judgeSnapshot = judgeValues(await judgesResponse.json() as JudgesState)
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
      await page.getByRole('combobox').click()
      await page.getByRole('option', { name: 'completed', exact: true }).click()
      const completedMatchesResponse = await completedMatchesPromise
      expect(completedMatchesResponse.status(), await completedMatchesResponse.text()).toBe(200)
      const completedMatches = await completedMatchesResponse.json() as {
        matches: Array<{ status: string }>
      }
      expect(completedMatches.matches.length).toBeGreaterThan(0)
      expect(completedMatches.matches.every((match) => match.status === 'completed')).toBe(true)
      await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(completedMatches.matches.length)
      await expect(page.getByText(/共 \d+ 局/)).toBeVisible()
    }
    await expectNoRootOverflow(page, 'matches')

    await page.getByRole('button', { name: '锦标赛', exact: true }).click()
    await expect(page.getByText(/共 \d+ 个比赛/)).toBeVisible()
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

    await page.getByRole('button', { name: '裁判', exact: true }).click()
    await expect(page.getByRole('button', { name: '保存参数', exact: true })).toBeVisible()
    if (viewport.interactive) {
      const input = page.getByRole('spinbutton').first()
      const original = Number(await input.inputValue())
      const min = Number(await input.getAttribute('min'))
      const max = Number(await input.getAttribute('max'))
      const changed = original < max ? original + 1 : Math.max(min, original - 1)
      await input.fill(String(changed))
      const responsePromise = page.waitForResponse(
        (response) => response.request().method() === 'PATCH' && new URL(response.url()).pathname === '/api/admin/judges/params',
      )
      await page.getByRole('button', { name: '保存参数', exact: true }).click()
      expect((await responsePromise).status()).toBe(200)
      await expect(page.getByText(/已保存并热生效/)).toBeVisible()

    }
    await expectNoRootOverflow(page, 'judges')

    await page.getByRole('button', { name: '日志', exact: true }).click()
    const logSearch = page.getByPlaceholder('关键字 / IP / action')
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
          url.search === '?file=audit&q=login&limit=300'
      })
      await logSearch.fill('login')
      const loginLogResponse = await loginLogPromise
      expect(loginLogResponse.status(), await loginLogResponse.text()).toBe(200)
      const loginLog = await loginLogResponse.json() as { lines: string[] }
      expect(loginLog.lines.length).toBeGreaterThan(0)
      expect(loginLog.lines.every((line) => line.toLowerCase().includes('login'))).toBe(true)
      await expect(page.getByText(`共 ${loginLog.lines.length} 行（末尾 300 条过滤后）`, { exact: true })).toBeVisible()
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
      if (judgeSnapshot) {
        const originalJudges = judgeSnapshot
        tasks.push({
          label: 'restore judge parameters',
          run: async () => {
            const restore = await page.request.patch('/api/admin/judges/params', {
              data: { params: originalJudges },
            })
            expect(restore.status(), await restore.text()).toBe(200)
            const verify = await page.request.get('/api/admin/judges')
            expect(verify.status(), await verify.text()).toBe(200)
            expect(judgeValues(await verify.json() as JudgesState)).toEqual(originalJudges)
          },
        })
      }
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
