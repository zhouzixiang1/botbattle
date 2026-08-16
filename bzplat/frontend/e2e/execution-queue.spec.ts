import { expect, test, type Locator, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const USER = {
  id: 42,
  username: 'queue_tester',
  email: 'queue_tester@example.test',
  role: 'user',
  display_name: 'Queue Tester',
  is_active: 1,
}

const MIN_MOBILE_TOUCH_PX = 44

async function expectMobileTouchTargets(root: Locator, label: string) {
  const undersized = await root
    .locator('button, a[href], input, textarea, label[for="upload-file"]:has(input[type="file"])')
    .evaluateAll((elements, minimum) => elements.flatMap((element) => {
      if (element instanceof HTMLInputElement && element.type === 'file') return []
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      if (
        rect.width === 0
        || rect.height === 0
        || style.display === 'none'
        || style.visibility === 'hidden'
      ) return []
      if (rect.width >= minimum && rect.height >= minimum) return []
      return [{
        tag: element.tagName.toLowerCase(),
        name: element.getAttribute('aria-label') || element.textContent?.trim().slice(0, 40) || '',
        width: Math.round(rect.width * 100) / 100,
        height: Math.round(rect.height * 100) / 100,
      }]
    }), MIN_MOBILE_TOUCH_PX)
  expect(undersized, `${label} contains touch targets smaller than 44px`).toEqual([])
}

function job(overrides: Record<string, unknown> = {}) {
  return {
    public_id: 'public-job-1',
    source: 'human',
    status: 'running',
    game_id: 'holdem',
    match_type: 'human',
    match_id: 'human-public-match',
    sandbox_units: 1,
    rated: false,
    rating_reason: 'human',
    retryable: false,
    cancel_requested: false,
    reason: '',
    created_at: '2026-08-11T10:00:00Z',
    ...overrides,
  }
}

function queueSnapshot(includeHostResources = false) {
  return {
    dispatcher: {
      state: 'starting',
      accepting: false,
      auto_enabled: true,
      pause_reason: '',
      retry_at: null,
    },
    capacity: {
      match_slots: { used: 1, capacity: 1 },
      sandbox_units: { used: 1, capacity: 2 },
      ...(includeHostResources ? {
        host_cpu_millis: { used: 3_000, capacity: 8_000 },
        host_memory_mb: { used: 2_560, capacity: 8_192 },
      } : {}),
      running_matches: 1,
    },
    active: [{
      ...job(),
      // A defensive UI projection must never render unexpected private fields.
      binary_path: '/private/bot_uploads/secret-bot',
      checksum: 'TOP-SECRET-CHECKSUM',
      owner_name: 'PRIVATE-OWNER-NAME',
    }],
    queued: [job({
      public_id: 'public-job-2',
      source: 'manual',
      status: 'queued',
      game_id: 'pencil',
      match_type: 'manual',
      match_id: null,
      sandbox_units: 2,
      rating_reason: 'same_owner',
      capacity_blocked_code: 'host_resources_insufficient',
      capacity_blocked_reason: '该对局需要 4 核 CPU 和 4 GiB 内存；当前主机资源不足，请求会保留排队且不会降档',
    })],
    queued_count: 1,
  }
}

function requestSnapshot(
  status: 'queued' | 'interrupted' | 'cancelled',
  overrides: Record<string, unknown> = {},
  publicId = 'challenge-public-request',
) {
  return {
    public_id: publicId,
    request: job({
      public_id: publicId,
      source: 'manual',
      status,
      match_type: 'manual',
      match_id: null,
      sandbox_units: 2,
      retryable: status === 'interrupted',
      rating_reason: 'eligible',
      reason: status === 'interrupted' ? '平台恢复后需要重新排队' : '',
      ...overrides,
    }),
    ahead_jobs: status === 'queued' ? 2 : 0,
    ahead_sandbox_units: status === 'queued' ? 4 : 0,
    capacity: {
      match_slots: { used: 1, capacity: 1 },
      sandbox_units: { used: 2, capacity: 2 },
      running_matches: 1,
    },
    eta: {
      min_seconds: 10,
      max_seconds: 30,
      dynamic: true as const,
      note: '按当前容量动态估算',
    },
  }
}

async function mockBase(
  page: Page,
  loggedIn: boolean,
  activeUser: Record<string, unknown> = USER,
) {
  const unexpectedBackendRequests: string[] = []
  const forbiddenMainRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.port === '50380') forbiddenMainRequests.push(`${request.method()} ${request.url()}`)
  })
  // Keep the matrix deterministic and offline-safe; system fonts are sufficient
  // for these behavior/layout assertions.
  await page.route('https://fonts.googleapis.com/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/css', body: '' })
  })
  // Install the fallback first. Playwright evaluates later routes first, so the
  // feature-specific routes registered by each test take precedence.
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/notifications/unread-count') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
      return
    }
    if (path === '/api/local-ai/agents') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' })
      return
    }
    unexpectedBackendRequests.push(`${route.request().method()} ${path}`)
    await route.fulfill({
      status: 418,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `unmocked test endpoint: ${path}` }),
    })
  })
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill(loggedIn ? {
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ user: activeUser }),
    } : {
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '未登录' }),
    })
  })
  if (loggedIn) {
    await page.addInitScript((user) => {
      localStorage.setItem('bzplat_token', 'queue-test-token')
      localStorage.setItem('bzplat_user', JSON.stringify(user))
    }, activeUser)
  }
  return { unexpectedBackendRequests, forbiddenMainRequests }
}

test('admin seat one picker exposes all public runnable Bots without an owner filter', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const admin = {
    ...USER,
    id: 99,
    username: 'queue_admin',
    email: 'queue_admin@example.test',
    role: 'admin',
  }
  const network = await mockBase(page, true, admin)
  const requests: URL[] = []
  const foreign = {
    id: 501,
    name: 'foreign-seat-one',
    display_name: 'Foreign Seat One',
    owner_id: 77,
    owner_name: 'foreign_owner',
    game_id: 'holdem',
    is_active: 1,
    runnable: true,
  }
  await page.route('**/api/bots/public?**', async (route) => {
    requests.push(new URL(route.request().url()))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: [foreign], total: 1 }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        current_version: 1,
        versions: [{ id: 9001, version: 1, runnable: true }],
      }),
    })
  })

  await page.goto('/#/challenge')
  const seatOneTrigger = page.getByRole('button', {
    name: '选择全站可用 Bot（管理员）',
    exact: true,
  })
  await expect(seatOneTrigger).toBeVisible()
  await seatOneTrigger.click()
  const picker = page.getByRole('dialog', { name: /选择对手/ })
  await expect(picker.getByRole('button', { name: /Foreign Seat One/ })).toBeVisible()
  expect(requests.some((url) => !url.searchParams.has('owner_id'))).toBe(true)
  await picker.getByRole('button', { name: /Foreign Seat One/ }).click()
  await expect(page.locator('main')).toContainText('Foreign Seat One')
  await expect(page.locator('main')).not.toContainText('玩家 1只能使用自己的 Bot')

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('two online local Bots create one unrated practice request without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)
  const agents = [{
    public_id: 'lai_alpha',
    bot_id: 701,
    label: '笔记本 A',
    game_id: 'holdem',
    status: 'active',
    is_online: true,
    is_busy: false,
    is_available: true,
    bot_active: true,
    last_seen_at: '2026-08-13T10:00:00Z',
    bot_name: 'local_alpha',
    bot_display_name: 'Local Alpha',
  }, {
    public_id: 'lai_beta',
    bot_id: 702,
    label: '笔记本 B',
    game_id: 'holdem',
    status: 'active',
    is_online: true,
    is_busy: false,
    is_available: true,
    bot_active: true,
    last_seen_at: '2026-08-13T10:00:00Z',
    bot_name: 'local_beta',
    bot_display_name: 'Local Beta',
  }]
  await page.route('**/api/local-ai/agents', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: agents }),
    })
  })

  let posted: Record<string, unknown> | null = null
  await page.route('**/api/matches/challenge', async (route) => {
    posted = route.request().postDataJSON() as Record<string, unknown>
    const publicId = String(posted.request_id)
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('cancelled', {
        rated: false,
        rating_reason: 'remote_local',
        sandbox_units: 0,
        bot_a_environment: 'remote_local',
        bot_b_environment: 'remote_local',
      }, publicId)),
    })
  })
  await page.route('**/api/execution-requests/*', async (route) => {
    const publicId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop() || '')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('cancelled', {
        rated: false,
        rating_reason: 'remote_local',
        sandbox_units: 0,
        bot_a_environment: 'remote_local',
        bot_b_environment: 'remote_local',
      }, publicId)),
    })
  })

  await page.goto('/#/challenge')
  const challengeForm = page.getByTestId('challenge-form')
  await expect(challengeForm).toBeVisible()
  for (const [seat, agentName] of [[1, /Local Alpha/], [2, /Local Beta/]] as const) {
    await page.getByLabel(`玩家 ${seat}运行位置`).click()
    await page.getByRole('option', { name: '本地 Bot（我的电脑）', exact: true }).click()
    await page.getByLabel(`玩家 ${seat}本地 Bot 连接`).click()
    await page.getByRole('option', { name: agentName }).click()
  }
  await expectMobileTouchTargets(challengeForm, 'mobile Challenge form')
  await expect(page.getByText('本地 Bot 练习局，不计平台排行榜。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '开始对局', exact: true }).click()
  await expect(page.getByTestId('execution-request-card')).toContainText('本地 Bot 练习，不计平台排行榜')
  expect(posted).toMatchObject({
    my_bot_id: 701,
    opponent_bot_id: 702,
    my_environment: 'remote_local',
    opponent_environment: 'remote_local',
    my_local_agent_id: 'lai_alpha',
    opponent_local_agent_id: 'lai_beta',
  })
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('local Bot token is shown once and the saved agent list never exposes it', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)
  const bot = {
    id: 801,
    name: 'local_identity',
    display_name: 'Local Identity',
    game_id: 'holdem',
    is_active: 1,
    runnable: true,
    current_version: 1,
  }
  await page.route('**/api/bots/mine?**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ bots: [bot], total: 1 }) })
  })
  const agent = {
    public_id: 'lai_created',
    bot_id: bot.id,
    label: '宿舍电脑',
    game_id: 'holdem',
    status: 'active',
    is_online: false,
    is_busy: false,
    is_available: false,
    bot_active: true,
    last_seen_at: null,
    bot_name: bot.name,
    bot_display_name: bot.display_name,
  }
  const revokedAgent = {
    ...agent,
    public_id: 'lai_revoked_old',
    label: '不要显示的旧电脑',
    status: 'revoked',
    bot_display_name: '不要显示的旧 Bot',
  }
  let items: typeof agent[] = [revokedAgent]
  await page.route('**/api/local-ai/agents', async (route) => {
    if (route.request().method() === 'POST') {
      items = [revokedAgent, agent]
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          agent,
          token: 'bzlai_once_only_secret',
          connection_url: '/api/local-ai/connect',
        }),
      })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items }) })
  })

  await page.goto('/#/my-bots')
  const myBotsPage = page.locator('[data-page-layout="account-my-bots"]')
  const region = page.getByTestId('local-bot-connections')
  await expect(region).toBeVisible()
  await expect(region.getByRole('link', { name: '接入说明', exact: true })).toHaveAttribute('href', '#/wiki?slug=local-ai')
  await expect(region.getByText('对局中显示为', { exact: true })).toBeVisible()
  await expect(region).toContainText('本机程序可运行尚未上传的新代码')
  await expect(region.getByText('不要显示的旧电脑', { exact: true })).toHaveCount(0)
  await expect(region.getByRole('link', { name: '下载连接器', exact: true }))
    .toHaveAttribute('href', '/api/local-ai/client')
  await expect(region.getByRole('link', { name: '下载连接器', exact: true }))
    .toHaveAttribute('download', 'local_ai_client.py')
  await expect(region.getByRole('combobox', { name: '对局中显示为', exact: true }))
    .toContainText('Local Identity')
  await expectMobileTouchTargets(myBotsPage, 'mobile My Bots page')
  await expect(region.getByLabel('连接名称')).toHaveAttribute('maxlength', '32')
  await region.getByLabel('连接名称').fill('宿舍电脑')
  await expect(region.getByRole('button', { name: '建立连接', exact: true })).toBeEnabled()
  await region.getByRole('button', { name: '建立连接', exact: true }).click()
  const secret = region.getByTestId('local-agent-secret')
  await expect(secret).toContainText('bzlai_once_only_secret')
  await expect(secret).toContainText('令牌只显示这一次')
  await expect(secret.getByTestId('local-agent-command')).not.toContainText('bzlai_once_only_secret')
  await expect(secret.getByTestId('local-agent-command')).toContainText('--url')
  await expect(secret.getByTestId('local-agent-command')).toContainText('--command')
  await secret.getByRole('button', { name: '我已保存', exact: true }).click()
  await expect(page.getByText('bzlai_once_only_secret', { exact: true })).toHaveCount(0)
  await expect(region).toContainText('宿舍电脑')
  await expectMobileTouchTargets(myBotsPage, 'mobile My Bots page after creating a connection')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('admin reviews and revokes paginated local Bot connections without credential exposure', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const admin = {
    ...USER,
    id: 99,
    username: 'queue_admin',
    email: 'queue_admin@example.test',
    role: 'admin',
  }
  const network = await mockBase(page, true, admin)
  const agent = {
    public_id: 'lai_admin_visible',
    bot_id: 901,
    label: '实验室工作站',
    game_id: 'gomoku',
    status: 'active',
    bot_active: true,
    is_online: true,
    is_busy: false,
    is_available: true,
    unavailable_reason: '',
    last_seen_at: '2026-08-13T10:00:00Z',
    bot_name: 'gomoku_internal_name',
    bot_display_name: '校赛五子棋',
    owner_id: 77,
    owner_name: 'participant_77',
    owner_display_name: '参赛同学 77',
    token_hint: 'must-not-render',
    token: 'must-never-exist',
  }
  let revoked = false
  let listRequest: { pathname: string; page: string | null; perPage: string | null } | null = null
  await page.route('**/api/admin/local-ai/agents?**', async (route) => {
    const url = new URL(route.request().url())
    listRequest = {
      pathname: url.pathname,
      page: url.searchParams.get('page'),
      perPage: url.searchParams.get('per_page'),
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: revoked ? [] : [agent],
        page: 1,
        per_page: 20,
        total: revoked ? 0 : 1,
      }),
    })
  })
  await page.route('**/api/admin/local-ai/agents/lai_admin_visible', async (route) => {
    expect(route.request().method()).toBe('DELETE')
    revoked = true
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"ok":true}',
    })
  })

  await page.goto('/#/admin?tab=local-ai')
  const region = page.getByTestId('admin-local-ai-connections')
  await expect(region).toContainText('实验室工作站')
  await expect(region).toContainText('校赛五子棋')
  await expect(region).not.toContainText('gomoku_internal_name')
  await expect(region).toContainText('参赛同学 77')
  await expect(region).toContainText('可用')
  await expect(page.getByText('must-not-render')).toHaveCount(0)
  await expect(page.getByText('must-never-exist')).toHaveCount(0)
  expect(listRequest).toEqual({
    pathname: '/api/admin/local-ai/agents',
    page: '1',
    perPage: '20',
  })

  await region.getByRole('button', { name: '撤销', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('进行中的本地 Bot 决策将按技术故障处理')
  await dialog.getByRole('button', { name: '撤销连接', exact: true }).click()
  await expect(region).toContainText('当前没有本地 Bot 连接')

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('challenge names both sides by the selected game instead of generic seats', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)

  await page.goto('/#/challenge')
  const formBox = await page.getByTestId('challenge-form').boundingBox()
  const mainBox = await page.locator('main').boundingBox()
  expect(formBox).not.toBeNull()
  expect(mainBox).not.toBeNull()
  expect(formBox?.width ?? 0).toBeGreaterThanOrEqual(900)
  expect((formBox?.width ?? 0) / (mainBox?.width ?? 1)).toBeGreaterThanOrEqual(0.75)
  await expect(page.getByText('玩家 1', { exact: true })).toBeVisible()
  await expect(page.getByText('玩家 2', { exact: true })).toBeVisible()
  await expect(page.getByText(/先手 \/ 黑/)).toHaveCount(0)

  const gameSelect = page.getByRole('combobox').first()
  await gameSelect.click()
  await page.getByRole('option', { name: '五子棋', exact: true }).click()
  await expect(page.getByText('开局提案方', { exact: true })).toBeVisible()
  await expect(page.getByText('交换决策方', { exact: true })).toBeVisible()
  await expect(page.getByText('黑方', { exact: true })).toHaveCount(0)
  await expect(page.getByText('白方', { exact: true })).toHaveCount(0)

  await gameSelect.click()
  await page.getByRole('option', { name: '点格棋', exact: true }).click()
  await expect(page.getByText('红方', { exact: true })).toBeVisible()
  await expect(page.getByText('蓝方', { exact: true })).toBeVisible()

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('admin queue shows compact host capacity while remaining mobile-safe', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const monitor = monitorBrowser(page)
  const admin = {
    ...USER,
    id: 99,
    username: 'queue_admin',
    email: 'queue_admin@example.test',
    role: 'admin',
  }
  const network = await mockBase(page, true, admin)
  await page.route('**/api/admin/stats', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        users: 8,
        users_active: 7,
        users_verified: 6,
        bots: 12,
        bots_active: 9,
        matches: 40,
        matches_completed: 38,
        matches_aborted: 1,
        matches_running: 1,
        matches_pending: 0,
        contests: 2,
        contests_running: 1,
        active_sessions: 3,
        recent_users: [],
      }),
    })
  })
  await page.route('**/api/admin/settings/runtime', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ queue: queueSnapshot(true) }),
    })
  })

  await page.goto('/#/admin')
  const panel = page.getByTestId('execution-queue-panel')
  await expect(panel.locator('[data-testid="host-cpu-capacity"]:visible')).toContainText('3 核 / 8 核')
  await expect(panel.locator('[data-testid="host-memory-capacity"]:visible')).toContainText('2560 MiB / 8 GiB')
  const capacityRows = await panel.locator('dl[aria-label="执行容量"]:visible > div').evaluateAll((items) => (
    items.map((item) => Math.round(item.getBoundingClientRect().top))
  ))
  expect(new Set(capacityRows).size).toBe(1)

  await page.setViewportSize({ width: 390, height: 844 })
  const summary = panel.locator('summary')
  await expect(summary).toBeVisible()
  await summary.click()
  await expect(panel.locator('[data-testid="host-cpu-capacity"]:visible')).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('202 challenge request survives refresh, retries, and cancels exactly once', async ({ page }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)

  const bots = [
    { id: 101, name: 'alpha', display_name: 'Alpha Bot', owner_id: USER.id, owner_name: USER.username },
    { id: 102, name: 'beta', display_name: 'Beta Bot', owner_id: 77, owner_name: 'opponent' },
  ]
  await page.route('**/api/bots/public?**', async (route) => {
    const ownerId = new URL(route.request().url()).searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [], current_version: 1 }),
    })
  })

  const blockedRequest = {
    capacity_blocked_code: 'host_resources_insufficient',
    capacity_blocked_reason: '当前主机资源不足，请求会保留排队且不会降档',
  }
  let current = requestSnapshot('queued', blockedRequest)
  let challengePosts = 0
  let retries = 0
  let deletes = 0
  let delayNextGet = false
  let acceptedId = ''
  await page.route('**/api/matches/challenge', async (route) => {
    expect(route.request().method()).toBe('POST')
    challengePosts += 1
    const body = route.request().postDataJSON() as {
      request_id?: string
      my_environment?: string
      opponent_environment?: string
      my_local_agent_id?: string | null
      opponent_local_agent_id?: string | null
    }
    expect(body.request_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
    expect(body).toMatchObject({
      my_environment: 'platform_low',
      opponent_environment: 'platform_low',
      my_local_agent_id: null,
      opponent_local_agent_id: null,
    })
    acceptedId = body.request_id!
    current = requestSnapshot('queued', blockedRequest, acceptedId)
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify(current),
    })
  })
  await page.route('**/api/execution-requests/**', async (route) => {
    const url = new URL(route.request().url())
    const method = route.request().method()
    if (url.pathname.endsWith('/retry')) {
      expect(method).toBe('POST')
      retries += 1
      current = requestSnapshot('queued', {}, current.public_id)
    } else if (method === 'DELETE') {
      deletes += 1
      current = requestSnapshot('queued', {
        status: 'running',
        match_id: 'cancel-race-match',
        cancel_requested: true,
      }, current.public_id)
    } else {
      expect(method).toBe('GET')
      if (delayNextGet) {
        delayNextGet = false
        await page.waitForTimeout(250)
      }
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(current),
    })
  })

  await page.goto('/#/challenge')
  const botToggle = page.getByRole('button', { name: '选 Bot', exact: true })
  const humanToggle = page.getByRole('button', { name: '我亲自上场', exact: true })
  await expect(botToggle).toHaveAttribute('aria-pressed', 'true')
  await expect(humanToggle).toHaveAttribute('aria-pressed', 'false')
  for (const control of [botToggle, humanToggle]) {
    const box = await control.boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }

  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Alpha Bot/ }).click()
  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Beta Bot/ }).click()
  await page.getByRole('button', { name: '开始对局', exact: true }).click()

  const card = page.getByTestId('execution-request-card')
  await expect(card).toContainText('排队中')
  await expect(card).toContainText('当前主机资源不足，请求会保留排队且不会降档')
  await expect(card).not.toContainText('动态预计等待')
  expect(challengePosts).toBe(1)
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(acceptedId)

  current = requestSnapshot('interrupted', {}, acceptedId)
  delayNextGet = true
  await page.reload()
  await expect(page.getByTestId('execution-request-recovery')).toBeVisible()
  await expect(card).toContainText('已中断')
  await card.getByRole('button', { name: '重新排队', exact: true }).click()
  await expect(card).toContainText('排队中')
  expect(retries).toBe(1)

  await card.getByRole('button', { name: '取消排队', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认取消', exact: true }).click()
  const cancelling = card.getByRole('button', { name: '正在取消…', exact: true })
  await expect(cancelling).toBeDisabled()
  expect(deletes).toBe(1)
  await expect(page).toHaveURL(/\/#\/challenge$/)
  await expect(card).toContainText('取消请求已记录')
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(acceptedId)

  current = requestSnapshot('cancelled', {}, acceptedId)
  await expect(card).toContainText('请求已取消')
  await expect(card.getByRole('button', { name: '取消排队', exact: true })).toHaveCount(0)
  await expect(card.getByRole('button', { name: '发起新请求', exact: true })).toBeVisible()
  expect(deletes).toBe(1)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean()
})

test('lost POST response recovers the committed request by its pre-persisted id', async ({ page }) => {
  test.setTimeout(60_000)
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)
  const bots = [
    { id: 201, name: 'loss-alpha', display_name: 'Loss Alpha', owner_id: USER.id, owner_name: USER.username },
    { id: 202, name: 'loss-beta', display_name: 'Loss Beta', owner_id: 77, owner_name: 'opponent' },
  ]
  await page.route('**/api/bots/public?**', async (route) => {
    const ownerId = new URL(route.request().url()).searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [], current_version: 1 }),
    })
  })

  let committedId = ''
  let challengePosts = 0
  let detailGets = 0
  await page.route('**/api/matches/challenge', async (route) => {
    challengePosts += 1
    const body = route.request().postDataJSON() as { request_id?: string }
    expect(body.request_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
    committedId = body.request_id!
    // Model a reverse proxy losing the successful upstream response after the
    // durable request has committed. The browser sees an ambiguous 5xx only.
    await route.fulfill({
      status: 504,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'upstream response lost after commit' }),
    })
  })
  await page.route('**/api/execution-requests/**', async (route) => {
    expect(route.request().method()).toBe('GET')
    const publicId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-1) || '')
    expect(publicId).toBe(committedId)
    detailGets += 1
    if (detailGets === 1) {
      // The first visibility read may race the commit/replica boundary; it must
      // not make the client discard the already-persisted id.
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'not visible yet' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('queued', {}, committedId)),
    })
  })

  await page.goto('/#/challenge')
  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Loss Alpha/ }).click()
  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Loss Beta/ }).click()
  await page.getByRole('button', { name: '开始对局', exact: true }).click()

  const recovery = page.getByTestId('execution-request-recovery')
  const alert = recovery.locator('[role="alert"][tabindex="-1"]')
  await expect(recovery.getByRole('alert')).toHaveCount(1)
  await expect(alert).toContainText('正在确认受理状态')
  await expect(alert).toBeFocused()
  await expect(page.getByTestId('execution-request-card')).toContainText('排队中', { timeout: 12_000 })
  expect(challengePosts).toBe(1)
  expect(detailGets).toBeGreaterThanOrEqual(2)
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  )?.[1])).toBe(committedId)
  await page.waitForTimeout(500)
  expect(challengePosts).toBe(1)

  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean([
    {
      kind: 'http',
      method: 'POST',
      status: 504,
      pathname: '/api/matches/challenge',
    },
    {
      kind: 'http',
      method: 'GET',
      status: 404,
      pathname: `/api/execution-requests/${committedId}`,
    },
  ])
})

test('a pre-POST visibility read cannot invalidate an accepted challenge', async ({ page }) => {
  test.setTimeout(60_000)
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, true)
  const bots = [
    { id: 301, name: 'race-alpha', display_name: 'Race Alpha', owner_id: USER.id, owner_name: USER.username },
    { id: 302, name: 'race-beta', display_name: 'Race Beta', owner_id: 77, owner_name: 'opponent' },
  ]
  await page.route('**/api/bots/public?**', async (route) => {
    const ownerId = new URL(route.request().url()).searchParams.get('owner_id')
    const rows = ownerId ? bots.filter((bot) => String(bot.owner_id) === ownerId) : bots
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots: rows, total: rows.length }),
    })
  })
  await page.route('**/api/bots/*/versions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ versions: [], current_version: 1 }),
    })
  })

  let acceptedId = ''
  let detailGets = 0
  const matchId = 'accepted-after-visibility-race'
  let markFirstGetStarted!: () => void
  const firstGetStarted = new Promise<void>((resolve) => { markFirstGetStarted = resolve })
  let releaseFirstGet!: () => void
  const firstGetMayReturn = new Promise<void>((resolve) => { releaseFirstGet = resolve })
  let releaseMatchGet!: () => void
  const matchGetMayReturn = new Promise<void>((resolve) => { releaseMatchGet = resolve })

  // Navigation itself is the assertion under test. Keep the destination
  // deterministic so its unrelated detail/social effects do not hit fallback
  // mocks after the challenge has proved the recovery contract.
  await page.route(`**/api/matches/${matchId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: matchId,
          game_id: 'future-test-game',
          status: 'running',
          match_type: 'manual',
          bot_a: { name: 'race-alpha', owner_name: USER.username },
          bot_b: { name: 'race-beta', owner_name: 'opponent' },
          result: { rounds_played: 0, deltas: [0, 0] },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"comments":[],"count":0,"total":0}',
    })
  })
  await page.route('**/api/likes/status?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"liked":false,"count":0}',
    })
  })

  await page.route('**/api/matches/challenge', async (route) => {
    const body = route.request().postDataJSON() as { request_id?: string }
    expect(body.request_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
    acceptedId = body.request_id!
    // The client persists the id before awaiting this POST. Hold the 202 until
    // that persistence has already started the first owner visibility read.
    await firstGetStarted
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('queued', {}, acceptedId)),
    })
  })
  await page.route('**/api/execution-requests/**', async (route) => {
    expect(route.request().method()).toBe('GET')
    const publicId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-1) || '')
    expect(publicId).toBe(acceptedId)
    detailGets += 1
    if (detailGets === 1) {
      markFirstGetStarted()
      await firstGetMayReturn
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'not visible yet' }),
      })
      return
    }
    await matchGetMayReturn
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(requestSnapshot('queued', {
        status: 'running',
        match_id: matchId,
      }, acceptedId)),
    })
  })

  await page.goto('/#/challenge')
  await page.getByRole('button', { name: '选择我的 Bot', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Race Alpha/ }).click()
  await page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Race Beta/ }).click()

  const postResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === '/api/matches/challenge' && response.status() === 202
  ))
  await page.getByRole('button', { name: '开始对局', exact: true }).click()
  await postResponse
  await expect(page.getByTestId('execution-request-card')).toContainText('排队中')

  // Deliver the older GET only after the page has accepted the newer POST
  // snapshot. It is a visibility race, not evidence that the request vanished.
  releaseFirstGet()
  await expect(page.getByRole('alert')).toContainText('正在确认受理状态')
  await expect(page.getByRole('alert')).not.toContainText('已失效或不属于当前账号')
  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 404,
    pathname: `/api/execution-requests/${acceptedId}`,
  }])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])

  releaseMatchGet()
  await expect.poll(() => detailGets).toBe(2)
  await expect(page).toHaveURL(new RegExp(`/#/match/${matchId}$`), { timeout: 12_000 })
  expect(detailGets).toBe(2)
  expect(await page.evaluate(() => Object.entries(sessionStorage).find(
    ([key]) => key.startsWith('bzplat.challenge.execution.'),
  ))).toBeUndefined()
})

test('public queue keeps stale data private and recovers from slow/error/offline at 390px', async ({ page, context }) => {
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, false)

  await page.route('**/api/leaderboard?**', async (route) => {
    const gameId = new URL(route.request().url()).searchParams.get('game_id') || 'holdem'
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

  let calls = 0
  let inFlight = 0
  let maxInFlight = 0
  await page.route('**/api/execution-queue', async (route) => {
    calls += 1
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    try {
      if (calls === 2) {
        await page.waitForTimeout(3_500)
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'temporary queue outage' }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(queueSnapshot()),
      })
    } finally {
      inFlight -= 1
    }
  })

  await page.goto('/#/leaderboard')
  const panel = page.getByTestId('execution-queue-panel')
  await expect(panel).toBeVisible()
  const details = panel.locator('details')
  await expect(details).not.toHaveAttribute('open', '')
  const summary = panel.locator('summary')
  expect((await summary.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  await summary.click()
  await expect(panel).toContainText('正在启动')
  const timestamp = panel.getByText(/^上次更新：/)
  await expect(timestamp).toBeVisible()
  expect(await timestamp.evaluate((element) => element.closest('[aria-live]'))).toBeNull()
  expect((await panel.locator('[aria-live]').allTextContents()).join(' ')).not.toContain('上次更新')
  await expect(panel.getByRole('link', { name: '进入观赛' })).toHaveAttribute(
    'href',
    '#/match/human-public-match',
  )
  await expect(panel).toContainText('人机对战，不计平台排行榜')
  await expect(panel).toContainText('同一所有者，不计平台排行榜')
  await expect(panel).toContainText('当前主机资源不足，请求会保留排队且不会降档')
  await expect(panel.getByTestId('host-cpu-capacity')).toHaveCount(0)
  await expect(panel.getByTestId('host-memory-capacity')).toHaveCount(0)
  await expect(page.getByText('/private/bot_uploads/secret-bot')).toHaveCount(0)
  await expect(page.getByText('TOP-SECRET-CHECKSUM')).toHaveCount(0)
  await expect(page.getByText('PRIVATE-OWNER-NAME')).toHaveCount(0)

  await expect(panel.getByText('temporary queue outage')).toBeVisible({ timeout: 12_000 })
  await expect(panel).toContainText('以下保留上次成功获取的数据')
  await expect(panel.getByRole('link', { name: '进入观赛' })).toBeVisible()
  expect(maxInFlight).toBe(1)

  await panel.getByRole('button', { name: '立即重试', exact: true }).click()
  await expect(panel.getByText('temporary queue outage')).toHaveCount(0)
  await context.setOffline(true)
  await expect(panel).toContainText('当前离线')
  await context.setOffline(false)
  await expect(panel.getByText('当前离线')).toHaveCount(0)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  expect(calls).toBeGreaterThanOrEqual(3)
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 503,
    pathname: '/api/execution-queue',
  }])
})
