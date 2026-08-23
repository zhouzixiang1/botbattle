import { expect, test, type Locator, type Page } from '@playwright/test'

import { loginThroughUi, monitorBrowser, withCleanup } from './helpers'

const ADMIN = process.env.BZ_E2E_ADMIN || 'qa_admin'
const MIN_TOUCH_TARGET_PX = 44
const RENDERING_EPSILON_PX = 0.01

const ADMIN_VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900, interactive: true },
  { name: 'laptop', width: 1280, height: 720, interactive: false },
  { name: 'mobile', width: 390, height: 844, interactive: false },
] as const

const MAINTENANCE_VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
] as const

async function expectNoRootOverflow(page: Page, label: string) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow, `${label} overflows viewport by ${overflow}px`).toBeLessThanOrEqual(1)
}

async function selectAdminModule(page: Page, label: string) {
  const mobileTrigger = page.getByRole('combobox', { name: '选择管理模块' })
  const viewportWidth = page.viewportSize()?.width ?? 1440
  if (viewportWidth < 1024) {
    await expect(mobileTrigger).toBeVisible()
    await mobileTrigger.click()
    await page.getByRole('option', { name: label, exact: true }).click()
  } else {
    const desktopNavigation = page.getByRole('navigation', { name: '管理控制台模块' })
    await expect(desktopNavigation).toBeVisible()
    await desktopNavigation.getByRole('button', { name: new RegExp(`^${label}`) }).click()
  }
  await expect(page.getByRole('main', { name: label, exact: true })).toBeVisible()
}

async function expectTouchTarget(locator: Locator, label: string) {
  const box = await locator.boundingBox()
  expect(box, `${label} has no rendered box`).not.toBeNull()
  expect(box?.width ?? 0, `${label} width`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX - RENDERING_EPSILON_PX)
  expect(box?.height ?? 0, `${label} height`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX - RENDERING_EPSILON_PX)
}

async function expectPseudoTouchTarget(locator: Locator, label: string) {
  const size = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    const pseudo = getComputedStyle(element, '::before')
    const inset = (value: string) => {
      const parsed = Number.parseFloat(value)
      return Number.isFinite(parsed) ? parsed : 0
    }
    return {
      width: rect.width - inset(pseudo.left) - inset(pseudo.right),
      height: rect.height - inset(pseudo.top) - inset(pseudo.bottom),
    }
  })
  expect(size.width, `${label} width`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX - RENDERING_EPSILON_PX)
  expect(size.height, `${label} height`).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX - RENDERING_EPSILON_PX)
}

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  expect((await response.json() as { qa_instance?: boolean }).qa_instance).toBe(true)
})

for (const viewport of ADMIN_VIEWPORTS) {
  test(`admin loads all seven operational tabs without runtime/network/layout errors (${viewport.name})`, async ({ page }) => {
    test.setTimeout(viewport.interactive ? 150_000 : 90_000)
    await withCleanup(async () => {
      await page.setViewportSize(viewport)
      const monitor = monitorBrowser(page)
      await loginThroughUi(page, ADMIN)
      await page.goto('/#/admin')

      if (viewport.name === 'mobile') {
        const menu = page.getByRole('button', { name: '菜单', exact: true })
        await expectTouchTarget(menu, 'mobile shell menu')
        await expectTouchTarget(page.getByRole('button', { name: '搜索', exact: true }), 'mobile shell search')
        await expectTouchTarget(page.getByRole('link', { name: '账户', exact: true }), 'mobile shell account')
        await expectTouchTarget(page.getByRole('button', { name: /^当前：/ }), 'mobile shell theme')

        await menu.click()
        const mobileNavigation = page.getByRole('dialog')
        await expect(mobileNavigation).toBeVisible()
        await expectTouchTarget(mobileNavigation.getByRole('link', { name: '首页', exact: true }), 'mobile navigation item')
        await expectTouchTarget(mobileNavigation.getByRole('button', { name: '搜索', exact: true }), 'mobile drawer search')
        await expectTouchTarget(mobileNavigation.getByRole('link', { name: '站内信', exact: true }), 'mobile drawer messages')
        await expectTouchTarget(mobileNavigation.getByRole('button', { name: '通知', exact: true }), 'mobile drawer notifications')
        await expectTouchTarget(mobileNavigation.getByRole('button', { name: /^当前：/ }), 'mobile drawer theme')
        await mobileNavigation.getByRole('button', { name: '关闭', exact: true }).click()
        await expect(mobileNavigation).toHaveCount(0)

        await expectTouchTarget(page.getByRole('combobox', { name: '选择管理模块' }), 'mobile admin module selector')
      }

    await expect(page.getByText('平台总览统计', { exact: true })).toBeVisible()
    await expect(page.getByText('最近注册用户', { exact: true })).toBeVisible()
    await expect(page.getByText('对局状态分布', { exact: true })).toBeVisible()
    const runtimeHealth = await page.request.get('/api/health')
    expect(runtimeHealth.status(), await runtimeHealth.text()).toBe(200)
    const runtimeCapacity = (await runtimeHealth.json() as { max_concurrent: number }).max_concurrent
    await expect(page.getByTestId('execution-queue-panel')).toContainText(
      `全站当前对局槽上限 ${runtimeCapacity} 场`,
    )
    await expectNoRootOverflow(page, 'dashboard')

    await selectAdminModule(page, '用户')
    const userSearch = page.getByPlaceholder('搜索用户名/邮箱')
    await expect(userSearch).toBeVisible()
    if (viewport.name === 'mobile') {
      await expectTouchTarget(userSearch, 'mobile admin user search')
      await expectTouchTarget(page.getByRole('button', { name: '删除', exact: true }).first(), 'mobile admin destructive action')
    }
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

    await selectAdminModule(page, 'Bot')
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

    await selectAdminModule(page, '对局')
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

    await selectAdminModule(page, '锦标赛')
    await expect(page.getByText(/共 \d+ 个锦标赛/)).toBeVisible()
    await expectNoRootOverflow(page, 'contests')

    await expect(page.getByRole('button', { name: '赛制模板', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '运行时', exact: true })).toHaveCount(0)
    if (viewport.interactive) {
      const runtimeResponse = await page.request.get('/api/admin/settings/runtime')
      expect(runtimeResponse.status(), await runtimeResponse.text()).toBe(200)
      expect((await runtimeResponse.json() as { source?: string; mutable?: boolean })).toMatchObject({
        source: 'code',
        mutable: false,
      })
      const runtimePatchResponse = await page.request.patch('/api/admin/settings/runtime', {
        data: { max_concurrent_matches: 1 },
      })
      expect(runtimePatchResponse.status()).toBe(404)
      expect((await page.request.get('/api/admin/templates')).status()).toBe(404)
      const publicTemplates = await page.request.get('/api/contests/templates')
      expect(publicTemplates.status(), await publicTemplates.text()).toBe(200)
      expect((await publicTemplates.json() as { source?: string; mutable?: boolean })).toMatchObject({
        source: 'code',
        mutable: false,
      })
    }

    await selectAdminModule(page, '日志')
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

    await selectAdminModule(page, '通信中心')
    const communications = page.getByRole('main', { name: '通信中心', exact: true })
    await expect(communications.getByRole('button', { name: '新建群发', exact: true })).toBeVisible()
    if (viewport.interactive) {
      await communications.getByRole('button', { name: '已发送', exact: true }).click()
      await expect(communications.getByRole('heading', { name: '已发送', exact: true })).toBeVisible()
      await communications.getByRole('button', { name: '群发记录', exact: true }).click()
      await expect(communications.getByRole('heading', { name: '群发记录', exact: true })).toBeVisible()
      await communications.getByRole('button', { name: '问题反馈', exact: true }).click()
      await expect(communications.getByRole('heading', { name: '问题反馈', exact: true })).toBeVisible()
    }
    await expectNoRootOverflow(page, 'communications')

      await monitor.expectClean()
    }, async () => {})
  })
}

test('admin queue switch is a single boolean control and survives polling', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  let enabled = true
  let publicQueueRequests = 0
  const payloads: boolean[] = []
  const snapshot = () => ({
    dispatcher: {
      state: 'running',
      accepting: true,
      auto_enabled: enabled,
      pause_reason: '',
      retry_at: null,
    },
    capacity: {
      match_slots: { used: 0, capacity: 1 },
      sandbox_units: { used: 0, capacity: 2 },
      running_matches: 0,
    },
    active: [],
    queued: [{
      public_id: 'admin-public-7001',
      source: 'auto',
      status: 'queued',
      game_id: 'holdem',
      match_type: 'ladder',
      match_id: null,
      sandbox_units: 2,
      rated: true,
      rating_reason: 'eligible',
      retryable: false,
      cancel_requested: false,
      reason: '',
    }],
    queued_count: 1,
  })
  await page.route('**/api/admin/settings/runtime', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ source: 'code', mutable: false, queue: snapshot() }),
    })
  })
  await page.route('**/api/admin/stats', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        users: 123456789,
        users_active: 98765432,
        users_verified: 87654321,
        bots: 123456789,
        bots_active: 98765432,
        matches: 999999999,
        matches_completed: 888888888,
        matches_aborted: 111111111,
        matches_running: 12345678,
        matches_pending: 9876543,
        contests: 123456,
        contests_running: 98765,
        active_sessions: 123456,
        recent_users: [{
          id: 7001,
          username: `very_long_unbroken_admin_dashboard_username_${'x'.repeat(96)}`,
          email: 'long-user@example.test',
          role: 'administrator_role_with_long_fallback_label',
          created_at: '2026-08-12T23:59:59.123456+08:00',
        }],
      }),
    })
  })
  await page.route('**/api/execution-queue', async (route) => {
    publicQueueRequests += 1
    await route.fulfill({ status: 500, body: 'dashboard must use internal admin snapshot' })
  })
  await page.route('**/api/admin/auto-match', async (route) => {
    expect(route.request().method()).toBe('PUT')
    const body = route.request().postDataJSON() as { enabled?: unknown }
    expect(typeof body.enabled).toBe('boolean')
    expect(Object.keys(body)).toEqual(['enabled'])
    enabled = body.enabled as boolean
    payloads.push(enabled)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(snapshot()),
    })
  })

  await page.goto('/#/admin')
  const panel = page.getByTestId('execution-queue-panel')
  const toggle = page.getByRole('switch', { name: '自动排位生产开关' })
  await expect(panel).toContainText('等待执行')
  await expect(toggle).toBeChecked()
  await expect(page.getByText('最近注册用户', { exact: true })).toBeVisible()
  await expect(page.getByText('对局状态分布', { exact: true })).toBeVisible()
  await expectNoRootOverflow(page, 'admin auto queue enabled')

  await toggle.click()
  await expect(toggle).not.toBeChecked()
  await expect(page.getByText('自动排位生产已关闭，人工与赛事任务不受影响', { exact: true })).toBeVisible()
  await toggle.click()
  await expect(toggle).toBeChecked()
  await expect(page.getByText('自动排位生产已开启', { exact: true })).toBeVisible()
  expect(payloads).toEqual([false, true])
  expect(publicQueueRequests).toBe(0)
  await expectNoRootOverflow(page, 'admin auto queue re-enabled')
  await monitor.expectClean()
})

for (const viewport of MAINTENANCE_VIEWPORTS) {
  test(`admin drains the active match into maintenance and resumes (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const monitor = monitorBrowser(page)
    await loginThroughUi(page, ADMIN)

    let autoEnabled = true
    let maintenance = false
    let phase: 'active' | 'upload' | 'legacy' | 'application' | 'fault' | 'recovering' | 'ready' = 'active'
    let beginAttempts = 0
    let deleteAttempts = 0
    let recoveryAttempts = 0
    let maintenanceReads = 0
    let autoRequests = 0
    let statsReadsDuringMaintenance = 0
    let runtimeReadsDuringMaintenance = 0
    const maintenanceReasons: string[] = []

    const snapshot = () => {
      const active = phase === 'active'
      const faultPaused = phase === 'fault'
      const readinessUnavailable = phase === 'application' || phase === 'recovering'
        ? ['application_recovery']
        : []
      return {
        dispatcher: {
          state: faultPaused ? 'paused' : 'running',
          accepting: !maintenance && !faultPaused,
          auto_enabled: autoEnabled,
          maintenance,
          pause_reason: faultPaused ? 'Docker 容器状态不确定，需要精确清场' : '',
          retry_at: null,
        },
        capacity: {
          match_slots: { used: active ? 1 : 0, capacity: 1 },
          sandbox_units: { used: active ? 2 : 0, capacity: 2 },
          running_matches: active || phase === 'legacy' ? 1 : 0,
        },
        active: active ? [{
          public_id: 'maintenance-active-7001',
          request_id: 'maintenance-active-7001',
          source: 'contest',
          status: 'running',
          game_id: 'holdem',
          match_type: 'contest',
          match_id: 'maintenance-match-7001',
          sandbox_units: 2,
          bot_a_environment: 'platform_high',
          bot_b_environment: 'platform_high',
          rated: false,
          rating_reason: 'contest',
          retryable: false,
          cancel_requested: false,
          reason: '',
        }] : [],
        queued: [{
          public_id: 'maintenance-queued-7002',
          request_id: 'maintenance-queued-7002',
          source: 'manual',
          status: 'queued',
          game_id: 'gomoku',
          match_type: 'manual',
          match_id: null,
          sandbox_units: 2,
          bot_a_environment: 'platform_low',
          bot_b_environment: 'platform_low',
          rated: true,
          rating_reason: 'eligible',
          retryable: false,
          cancel_requested: false,
          reason: '',
        }],
        queued_count: 7,
        maintenance: {
          requested: maintenance,
          ready: maintenance && phase === 'ready',
          reason: maintenance ? '管理员准备部署' : '',
          active_count: active ? 1 : 0,
          uploads_in_flight: phase === 'upload' ? 1 : 0,
          active_local_ai_leases: 0,
          untracked_running_matches: phase === 'legacy' ? 1 : 0,
          docker_launch_state: 'idle',
          owned_execution_tasks: active ? 1 : 0,
          readiness_unavailable: readinessUnavailable,
        },
      }
    }

    await page.route('**/api/admin/stats', async (route) => {
      if (maintenance) statsReadsDuringMaintenance += 1
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          users: 40,
          users_active: 20,
          users_verified: 18,
          bots: 77,
          bots_active: 60,
          matches: 1949,
          matches_completed: 1938,
          matches_aborted: 9,
          matches_running: phase === 'active' || phase === 'legacy' ? 1 : 0,
          matches_pending: 7,
          contests: 8,
          contests_running: 1,
          active_sessions: 12,
          recent_users: [],
        }),
      })
    })
    await page.route('**/api/admin/settings/runtime', async (route) => {
      if (maintenance) runtimeReadsDuringMaintenance += 1
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ source: 'code', mutable: false, queue: snapshot() }),
      })
    })
    await page.route('**/api/admin/auto-match', async (route) => {
      autoRequests += 1
      await route.fulfill({ status: 500, body: 'maintenance must close auto atomically' })
    })
    await page.route('**/api/admin/execution-queue/maintenance', async (route) => {
      if (route.request().method() === 'GET') {
        maintenanceReads += 1
        const runtimeSnapshot = snapshot()
        if (maintenance) {
          if (phase === 'active') phase = 'upload'
          else if (phase === 'upload') phase = 'legacy'
          else if (phase === 'legacy') phase = 'application'
          else if (phase === 'application') phase = 'fault'
          else if (phase === 'recovering') phase = 'ready'
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(runtimeSnapshot),
        })
        return
      }
      if (route.request().method() === 'POST') {
        beginAttempts += 1
        const body = route.request().postDataJSON() as { reason?: unknown }
        expect(Object.keys(body)).toEqual(['reason'])
        expect(body.reason).toBe('管理员准备部署')
        maintenanceReasons.push(String(body.reason))
        maintenance = true
        autoEnabled = false
        const drainingSnapshot = snapshot()
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(drainingSnapshot),
        })
        return
      }
      expect(route.request().method()).toBe('DELETE')
      expect(snapshot().maintenance.ready).toBe(true)
      deleteAttempts += 1
      maintenance = false
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(snapshot()),
      })
    })
    await page.route('**/api/admin/execution-queue/resume', async (route) => {
      expect(route.request().method()).toBe('POST')
      expect(maintenance).toBe(true)
      expect(phase).toBe('fault')
      recoveryAttempts += 1
      phase = 'recovering'
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(snapshot()),
      })
    })

    await page.goto('/#/admin')
    const control = page.getByTestId('deployment-maintenance-control')
    const toggle = page.getByRole('switch', { name: '自动排位生产开关' })
    const prepare = page.getByRole('button', { name: '准备维护', exact: true })
    await expect(control).toContainText('正常调度')
    await expect(control).toContainText('运行 1 · 等待 7')
    await expect(toggle).toBeChecked()
    if (viewport.name === 'mobile') {
      await expectTouchTarget(prepare, 'mobile prepare maintenance')
      await expectPseudoTouchTarget(toggle, 'mobile auto-match switch')
    }
    await expectNoRootOverflow(page, `maintenance ready action ${viewport.name}`)

    await prepare.click()
    const dialog = page.getByRole('dialog', { name: '准备部署维护' })
    await expect(dialog).toContainText('当前对局继续到自然结束')
    const begin = dialog.getByRole('button', { name: '开始排空', exact: true })
    if (viewport.name === 'mobile') {
      await expectTouchTarget(dialog.getByRole('button', { name: '取消', exact: true }), 'mobile maintenance cancel')
      await expectTouchTarget(begin, 'mobile begin maintenance')
    }
    await begin.click()

    await expect(control).toContainText('排空中')
    await expect(control).toContainText('还有 1 场对局正在自然结束')
    await expect(control.getByRole('button', { name: '正在排空…', exact: true })).toBeDisabled()
    await expect(toggle).not.toBeChecked()
    await expect(toggle).toBeDisabled()
    await expect(control).toContainText('还有 1 个上传正在完成检查', { timeout: 8_000 })
    await expect(control).toContainText('还有 1 场遗留对局仍标记为运行中', { timeout: 8_000 })
    await expect(control).toContainText('评分与赛事状态正在恢复，完成前不能停服', { timeout: 8_000 })
    await expect(control).toContainText('排空受阻', { timeout: 8_000 })
    await expect(control).toContainText('Docker 容器状态不确定，需要精确清场')

    const recover = control.getByRole('button', { name: '清场并恢复', exact: true })
    if (viewport.name === 'mobile') await expectTouchTarget(recover, 'mobile recover queue')
    await recover.click()
    const recoverDialog = page.getByRole('dialog', { name: '清场并恢复执行队列' })
    const confirmRecover = recoverDialog.getByRole('button', { name: '清场并恢复', exact: true })
    if (viewport.name === 'mobile') {
      await expectTouchTarget(recoverDialog.getByRole('button', { name: '取消', exact: true }), 'mobile recovery cancel')
      await expectTouchTarget(confirmRecover, 'mobile confirm recovery')
    }
    await confirmRecover.click()
    await expect(page.getByText('运行环境已恢复，继续完成部署排空', { exact: true })).toBeVisible()
    await expect(control).toContainText('评分与赛事状态正在恢复，完成前不能停服')
    await expect(control).toContainText('可安全停服', { timeout: 8_000 })
    await expect(control).toContainText('运行环境已清空 · 7 项等待任务已保留')
    await expect(page.getByTestId('execution-queue-panel')).toContainText(
      '运行环境已排空；等待任务已保留，恢复后会继续执行。',
    )
    expect(autoRequests).toBe(0)
    expect(beginAttempts).toBe(1)
    expect(recoveryAttempts).toBe(1)
    expect(maintenanceReads).toBeGreaterThanOrEqual(6)
    expect(maintenanceReasons.every((reason) => reason === '管理员准备部署')).toBe(true)
    expect(statsReadsDuringMaintenance).toBe(0)
    expect(runtimeReadsDuringMaintenance).toBe(0)
    const readsAtReady = maintenanceReads
    await expect.poll(() => maintenanceReads, { timeout: 4_000 }).toBeGreaterThan(readsAtReady)
    expect(statsReadsDuringMaintenance).toBe(0)
    expect(runtimeReadsDuringMaintenance).toBe(0)
    await expectNoRootOverflow(page, `maintenance active ${viewport.name}`)

    const resume = page.getByRole('button', { name: '恢复调度', exact: true })
    if (viewport.name === 'mobile') await expectTouchTarget(resume, 'mobile resume scheduling')
    await resume.click()
    await expect(control).toContainText('正常调度')
    await expect(control).toContainText('运行 0 · 等待 7')
    await expect(toggle).not.toBeChecked()
    await expect(toggle).toBeEnabled()
    await expect(page.getByText('调度已恢复；自动排位仍保持关闭', { exact: true })).toBeVisible()
    expect(deleteAttempts).toBe(1)
    await expectNoRootOverflow(page, `maintenance resumed ${viewport.name}`)
    await monitor.expectClean()
  })
}

for (const viewport of ADMIN_VIEWPORTS) {
  test(`manual contest schedule stays explicit and scrollable with long text (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const monitor = monitorBrowser(page)
    await loginThroughUi(page, ADMIN)

    const title = `手动排期-${'LONG_UNBROKEN_'.repeat(40)}-🎯`
    const contest = {
      id: 980,
      title,
      organizer_id: 1,
      status: 'draft',
      created_at: '2026-08-09T09:00:00',
      ends_at: null,
      registration_opens_at: null,
      registration_closes_at: null,
      starts_at: null,
      template_id: 'holdem_rr',
      game_id: 'holdem',
    }
    let submitted: Record<string, string | null> | undefined
    await page.route('**/api/admin/contests?page=1&per_page=20', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ contests: [contest], total: 1 }),
      })
    })
    await page.route('**/api/admin/contests/980', async (route) => {
      expect(route.request().method()).toBe('PATCH')
      submitted = route.request().postDataJSON() as Record<string, string | null>
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ contest: { ...contest, ...submitted } }),
      })
    })

    await page.goto('/#/admin?tab=contests')
    const row = page.getByRole('row').filter({ hasText: '手动排期-' })
    await expect(row.getByText('开放报名：手动', { exact: true })).toBeVisible()
    await expect(row.getByText('报名截止：手动', { exact: true })).toBeVisible()
    await expect(row.getByText('比赛开始：手动', { exact: true })).toBeVisible()
    await expectNoRootOverflow(page, `manual schedule row ${viewport.name}`)

    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
    await row.getByRole('button', { name: '编辑时间', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '编辑赛事时间' })
    await expect(dialog.getByLabel('开放报名')).toHaveValue('')
    await expect(dialog.getByLabel('报名截止')).toHaveValue('')
    const autoStart = dialog.getByRole('switch', { name: '按时间自动开赛' })
    await expect(autoStart).not.toBeChecked()
    await expect(dialog.getByText('比赛开始：手动。发布排期后等待组织者点击“开始比赛”。', { exact: true })).toBeVisible()
    await expectNoRootOverflow(page, `manual schedule dialog ${viewport.name}`)

    const scrollMetrics = await dialog.evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }))
    expect(scrollMetrics.scrollHeight).toBeGreaterThanOrEqual(scrollMetrics.clientHeight)
    if (scrollMetrics.scrollHeight > scrollMetrics.clientHeight) {
      await dialog.evaluate((element) => element.scrollTo(0, element.scrollHeight))
      expect(await dialog.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
    }

    await autoStart.click()
    await expect(dialog.getByLabel('比赛开始')).toHaveValue('')
    await expect(dialog.getByText('选择自动开赛后必须填写比赛开始时间', { exact: true })).toBeVisible()
    await expect(dialog.getByRole('button', { name: '保存时间', exact: true })).toBeDisabled()
    await autoStart.click()
    await dialog.getByRole('button', { name: '保存时间', exact: true }).click()
    await expect.poll(() => submitted).toEqual({
      registration_opens_at: null,
      registration_closes_at: null,
      starts_at: null,
    })
    await expect(dialog).toHaveCount(0)
    await monitor.expectClean()
  })
}

test('admin manual schedule persists in the isolated DB and emits an audit record', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let contestId: number | null = null
  await withCleanup(async () => {
    await loginThroughUi(page, ADMIN)
    const title = `PW Admin schedule DB ${Date.now()}`
    const create = await page.request.post('/api/contests', {
      data: {
        title,
        game_id: 'holdem',
        starts_at: '2099-12-01T12:00:00',
      },
    })
    expect(create.status(), await create.text()).toBe(200)
    contestId = ((await create.json()) as { contest: { id: number } }).contest.id

    await page.goto('/#/admin?tab=contests')
    const row = page.getByRole('row').filter({ hasText: title })
    await expect(row).toBeVisible()
    await row.getByRole('button', { name: '编辑时间', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '编辑赛事时间' })
    const autoStart = dialog.getByRole('switch', { name: '按时间自动开赛' })
    await expect(autoStart).toBeChecked()
    await autoStart.click()
    const patchResponse = page.waitForResponse((response) =>
      response.request().method() === 'PATCH' &&
      new URL(response.url()).pathname === `/api/admin/contests/${contestId}`,
    )
    await dialog.getByRole('button', { name: '保存时间', exact: true }).click()
    const saved = await patchResponse
    expect(saved.status(), await saved.text()).toBe(200)
    expect((await saved.json() as { contest: { starts_at: string | null } }).contest.starts_at).toBeNull()

    await page.reload()
    const reloadedRow = page.getByRole('row').filter({ hasText: title })
    await expect(reloadedRow.getByText('比赛开始：手动', { exact: true })).toBeVisible()
    const detail = await page.request.get(`/api/contests/${contestId}`)
    expect(detail.status(), await detail.text()).toBe(200)
    expect((await detail.json() as { contest: { starts_at: string | null } }).contest.starts_at).toBeNull()

    const query = encodeURIComponent(`target=${contestId}`)
    const audit = await page.request.get(`/api/admin/logs?file=audit&limit=300&q=${query}`)
    expect(audit.status(), await audit.text()).toBe(200)
    const lines = (await audit.json() as { lines: string[] }).lines
    expect(lines.some((line) =>
      line.includes('action=admin_patch_contest_fields') &&
      line.includes('result=ok') &&
      line.includes(`target=${contestId}`),
    )).toBe(true)
    await monitor.expectClean()
  }, async () => {
    if (contestId === null) return
    const remove = await page.request.delete(`/api/admin/contests/${contestId}`)
    expect(remove.status(), await remove.text()).toBe(200)
  })
})

test('contest admin exposes only phase-appropriate actions and flags invalid schedules', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  const base = {
    organizer_id: 1,
    created_at: '2099-08-09T09:00:00',
    ends_at: null,
    registration_opens_at: '2099-08-09T10:00:00',
    registration_closes_at: '2099-08-09T11:00:00',
    starts_at: '2099-08-09T12:00:00',
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
      registration_closes_at: '2099-08-09T13:00:00',
    },
    { ...base, id: 907, title: '阶段矩阵-已取消', status: 'cancelled' },
    {
      ...base,
      id: 908,
      title: '阶段矩阵-纯手动',
      status: 'draft',
      registration_opens_at: null,
      registration_closes_at: null,
      starts_at: null,
    },
  ]
  let schedulePatch: Record<string, string | null> | undefined
  let schedulePatchAttempts = 0
  await page.route('**/api/admin/contests?page=1&per_page=20', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contests, total: contests.length }),
    })
  })
  await page.route('**/api/admin/contests/901', async (route) => {
    expect(route.request().method()).toBe('PATCH')
    schedulePatchAttempts += 1
    schedulePatch = route.request().postDataJSON() as Record<string, string | null>
    if (schedulePatchAttempts === 1) {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '模拟事务拒绝：对阵已派发' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contest: { ...contests[0], ...schedulePatch } }),
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
  await expect(row('阶段矩阵-进行中').getByRole('button', { name: /编辑时间|修正时间/ })).toHaveCount(0)
  await expect(row('阶段矩阵-休息').getByRole('button', { name: '进入下一阶段', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-休息').getByRole('button', { name: '恢复性结束', exact: true })).toBeVisible()
  await expect(row('阶段矩阵-休息').getByRole('button', { name: /编辑时间|修正时间/ })).toHaveCount(0)

  const open = row('阶段矩阵-报名')
  await open.getByRole('button', { name: '编辑时间', exact: true }).click()
  const openDialog = page.getByRole('dialog', { name: '编辑赛事时间' })
  await expect(openDialog.locator('#admin-registration-opens-at')).toHaveCount(0)
  await expect(openDialog.getByText('开放报名（已生效，只读）', { exact: true })).toBeVisible()
  await expect(openDialog.locator('#admin-registration-closes-at')).toBeVisible()
  await openDialog.getByRole('button', { name: '取消', exact: true }).click()

  const published = row('阶段矩阵-已发布')
  await published.getByRole('button', { name: '编辑时间', exact: true }).click()
  const publishedDialog = page.getByRole('dialog', { name: '编辑赛事时间' })
  await expect(publishedDialog.locator('#admin-registration-opens-at')).toHaveCount(0)
  await expect(publishedDialog.locator('#admin-registration-closes-at')).toHaveCount(0)
  await expect(publishedDialog.getByText('报名截止（已发布，只读）', { exact: true })).toBeVisible()
  await expect(publishedDialog.getByLabel('比赛开始')).toBeVisible()
  await publishedDialog.getByRole('button', { name: '取消', exact: true }).click()

  const finished = row('阶段矩阵-已完成')
  await expect(finished.getByText('成绩已归档 · 只读', { exact: true })).toBeVisible()
  await expect(finished.getByRole('button', { name: /取消赛事|删除草稿|清理已取消赛事|开始比赛|恢复性结束/ })).toHaveCount(0)
  await expect(finished.getByText('报名截止晚于比赛开始', { exact: true })).toBeVisible()
  await expect(finished.getByRole('button', { name: /编辑时间|修正时间/ })).toHaveCount(0)

  const manual = row('阶段矩阵-纯手动')
  await expect(manual.getByText('开放报名：手动', { exact: true })).toBeVisible()
  await expect(manual.getByText('报名截止：手动', { exact: true })).toBeVisible()
  await expect(manual.getByText('比赛开始：手动', { exact: true })).toBeVisible()
  await manual.getByRole('button', { name: '编辑时间', exact: true }).click()
  const manualDialog = page.getByRole('dialog', { name: '编辑赛事时间' })
  const autoStart = manualDialog.getByRole('switch', { name: '按时间自动开赛' })
  await expect(autoStart).not.toBeChecked()
  await expect(manualDialog.getByText('比赛开始：手动。发布排期后等待组织者点击“开始比赛”。', { exact: true })).toBeVisible()
  await autoStart.click()
  await expect(manualDialog.getByLabel('比赛开始')).toHaveValue('')
  await expect(manualDialog.getByText('选择自动开赛后必须填写比赛开始时间', { exact: true })).toBeVisible()
  await expect(manualDialog.getByRole('button', { name: '保存时间', exact: true })).toBeDisabled()
  await manualDialog.getByRole('button', { name: '取消', exact: true }).click()

  const scheduled = row('阶段矩阵-草稿')
  await scheduled.getByRole('button', { name: '编辑时间', exact: true }).click()
  const scheduledDialog = page.getByRole('dialog', { name: '编辑赛事时间' })
  const scheduledAutoStart = scheduledDialog.getByRole('switch', { name: '按时间自动开赛' })
  await expect(scheduledAutoStart).toBeChecked()
  await scheduledAutoStart.click()
  await scheduledDialog.getByRole('button', { name: '保存时间', exact: true }).click()
  await expect(scheduledDialog.getByText('模拟事务拒绝：对阵已派发', { exact: true })).toBeVisible()
  await expect(scheduledDialog).toBeVisible()
  await scheduledDialog.getByRole('button', { name: '保存时间', exact: true }).click()
  await expect.poll(() => schedulePatch).toEqual({
    registration_opens_at: '2099-08-09T10:00:00',
    registration_closes_at: '2099-08-09T11:00:00',
    starts_at: null,
  })
  expect(schedulePatchAttempts).toBe(2)
  await expect(scheduledDialog).toHaveCount(0)

  const cancelled = row('阶段矩阵-已取消')
  await expect(cancelled.getByText('已取消 · 可清理', { exact: true })).toBeVisible()
  await expect(cancelled.getByRole('button', { name: '清理已取消赛事', exact: true })).toBeVisible()
  await expect(cancelled.getByRole('button', { name: '取消赛事', exact: true })).toHaveCount(0)
  await expect(cancelled.getByRole('button', { name: /编辑时间|修正时间/ })).toHaveCount(0)
  await expect(page.getByText('React 19 · Tailwind v4 · shadcn/ui', { exact: true })).toHaveCount(0)
  await monitor.expectClean([{
    kind: 'http',
    method: 'PATCH',
    status: 400,
    pathname: '/api/admin/contests/901',
  }])
})

test('contest detail changes its primary content and actions with lifecycle stage', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  let lifecycleStatus: 'open' | 'published' = 'open'
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
            source_stage: 1,
            ranking_cohort: 'stage:1',
            points: 3,
            bot_name: 'winner_bot',
            owner_name: 'winner_user',
            awarded: '冠军',
            tiebreaks: {
              points: 3,
              buchholz_cut1: 4,
              sonneborn_berger: 2,
              head_to_head: 1,
              normalized_delta: 100,
              technical_losses: 0,
              seed: 1,
            },
          }, {
            rank: 2,
            entry_id: 1002,
            bot_id: 12,
            user_id: 22,
            source_stage: 1,
            ranking_cohort: 'stage:1',
            points: 3,
            bot_name: 'runner_bot',
            owner_name: 'runner_user',
            awarded: '亚军',
            tiebreaks: {
              points: 3,
              buchholz_cut1: 2,
              sonneborn_berger: 1,
              head_to_head: 0,
              normalized_delta: -100,
              technical_losses: 0,
              seed: 2,
            },
          }, {
            rank: 9,
            entry_id: 1003,
            bot_id: 13,
            user_id: 23,
            source_stage: 0,
            ranking_cohort: 'stage:0',
            points: 3,
            bot_name: 'preliminary_bot',
            owner_name: 'preliminary_user',
            awarded: '',
            tiebreaks: {
              points: 3,
              buchholz_cut1: 99,
              sonneborn_berger: 99,
              head_to_head: 1,
              normalized_delta: 999,
              technical_losses: 0,
              seed: 3,
            },
          }],
        }),
      })
      return
    }
    if (
      url.pathname === '/api/contests/902/publish' &&
      route.request().method() === 'POST'
    ) {
      lifecycleStatus = 'published'
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'published' }),
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
          status: finished ? 'finished' : lifecycleStatus,
          title: finished ? '阶段详情-已完成' : '阶段详情-报名中',
          registration_closes_at: finished ? '2026-08-09T13:00:00' : '2026-08-09T11:00:00',
          official_results_ready: finished ? 1 : 0,
          current_stage_idx: finished ? 1 : 0,
          stages_json: finished
            ? JSON.stringify([
                { key: 'swiss', type: 'swiss', scoring: 'poker_3_1_0' },
                { key: 'final', type: 'double_round_robin', scoring: 'poker_3_1_0', ranking_mode: 'replace_top', ranking_scope: 2 },
              ])
            : commonContest.stages_json,
        },
        entries: finished ? [{ id: 1001, user_id: 21, bot_id: 11, bot_name: 'winner_bot' }] : [],
        pairings: finished ? [{
          id: 2001,
          bot_a_id: 11,
          bot_b_id: 12,
          bot_a_name: 'winner_bot',
          bot_b_name: 'runner_bot',
          owner_a_name: 'winner_user',
          owner_a_display: 'Winner User',
          owner_b_name: 'runner_user',
          owner_b_display: 'Runner User',
          stage_idx: 1,
          status: 'completed',
          match_winner: 0,
        }] : [],
        standings: finished ? [{ bot_id: 11, bot_name: 'winner_bot', points: 3, wins: 1, draws: 0, losses: 0, delta_total: 100 }] : [],
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
  await expect(page.getByText(/对手分 Cut1 4/)).toBeVisible()
  await expect(page.getByText(/对手分 Cut1 2/)).toBeVisible()
  const preliminaryRow = page.getByRole('row').filter({ hasText: 'preliminary_bot' })
  await expect(preliminaryRow.getByText('瑞士轮', { exact: true })).toBeVisible()
  await expect(preliminaryRow.getByText('瑞士轮内名次已确定', { exact: true })).toBeVisible()
  await expect(preliminaryRow.getByText(/对手分 Cut1/)).toHaveCount(0)
  await expect(page.getByText(/时间配置异常：报名截止时间晚于比赛开始时间/)).toBeVisible()
  await expect(page.getByRole('button', { name: /开放报名|截止报名|立即开赛|强制结束赛事/ })).toHaveCount(0)
  const contestTabs = page.getByRole('tablist').first()
  expect(await contestTabs.evaluate((element) => getComputedStyle(element).overflowY)).toBe('hidden')
  await page.getByRole('tab', { name: /对阵/ }).click()
  await expect(page.locator('[data-pairing-result="decided"]:visible').filter({ hasText: 'winner_bot 胜' })).toBeVisible()

  await page.goto('/#/contests/902')
  await expect(page.getByRole('tab', { name: /选手/ })).toHaveAttribute('data-state', 'active')
  await expect(page.getByRole('tab', { name: /对阵|阶段积分|正式名次/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '截止报名·出排期', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '立即开赛', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '截止报名·出排期', exact: true }).click()
  await expect(page.getByRole('tab', { name: /对阵/ })).toHaveAttribute('data-state', 'active')
  await expect(page.getByRole('button', { name: '立即开赛', exact: true })).toBeVisible()
  await monitor.expectClean()
})
