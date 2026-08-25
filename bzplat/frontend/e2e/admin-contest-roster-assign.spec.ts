import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const CONTEST_ID = 989_101
const ADMIN = {
  id: 989_001,
  username: 'roster_admin',
  email: 'roster_admin@example.test',
  role: 'admin',
  display_name: '名册管理员',
  is_active: 1,
}
const ORGANIZER = {
  ...ADMIN,
  id: 989_002,
  username: 'roster_organizer',
  email: 'roster_organizer@example.test',
  role: 'organizer',
  display_name: '实名赛事组织者',
}

const EXISTING_USER = {
  id: 989_010,
  username: 'already_entered',
  display_name: '已报名用户',
  is_active: 1,
}
const ALPHA_USER = {
  id: 989_011,
  username: 'roster_alpha',
  display_name: 'Alpha 选手',
  is_active: 1,
}
const BETA_USER = {
  id: 989_012,
  username: 'roster_beta',
  display_name: 'Beta 选手',
  is_active: 1,
}
const INACTIVE_USER = {
  id: 989_013,
  username: 'roster_inactive',
  display_name: '停用账号',
  is_active: 0,
}
const PAGED_USER = {
  id: 989_014,
  username: 'roster_many_bots',
  display_name: '多 Bot 选手',
  is_active: 1,
}
const FILLER_USERS = Array.from({ length: 17 }, (_, index) => ({
  id: 989_030 + index,
  username: `roster_filler_${String(index + 1).padStart(2, '0')}`,
  display_name: `占位选手 ${String(index + 1).padStart(2, '0')}`,
  is_active: 1,
}))

const ALPHA_BOTS = [
  { id: 989_111, owner_id: ALPHA_USER.id, name: 'alpha_one', display_name: 'Alpha 一号', current_version: 1, runnable: true },
  { id: 989_112, owner_id: ALPHA_USER.id, name: 'alpha_two', display_name: 'Alpha 二号', current_version: 2, runnable: true },
]
const BETA_BOTS = [
  { id: 989_121, owner_id: BETA_USER.id, name: 'beta_one', display_name: 'Beta 一号', current_version: 3, runnable: true },
  { id: 989_122, owner_id: BETA_USER.id, name: 'beta_two', display_name: 'Beta 二号', current_version: 4, runnable: true },
]
const PAGED_BOTS = Array.from({ length: 51 }, (_, index) => ({
  id: 989_300 + index,
  owner_id: PAGED_USER.id,
  name: `many_bot_${String(index + 1).padStart(2, '0')}`,
  display_name: `多 Bot ${String(index + 1).padStart(2, '0')}`,
  current_version: 1,
  runnable: true,
}))

function contest(requireRealName = 0) {
  return {
    id: CONTEST_ID,
    title: requireRealName ? '实名名册权限回归' : '管理员精确名册回归',
    description: '验证管理员逐个选择用户与 Bot',
    organizer_id: ORGANIZER.id,
    status: 'draft',
    created_at: '2026-08-25T08:00:00+00:00',
    starts_at: null,
    ends_at: null,
    registration_opens_at: null,
    registration_closes_at: null,
    template_id: 'gomoku_rr',
    template_name: '五子棋单循环',
    game_id: 'gomoku',
    stages_json: JSON.stringify([{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }]),
    current_stage_idx: 0,
    require_real_name: requireRealName,
    showcase_key: null,
  }
}

function existingEntries() {
  return [{
    id: 989_201,
    contest_id: CONTEST_ID,
    user_id: EXISTING_USER.id,
    bot_id: 989_211,
    username: EXISTING_USER.username,
    owner_name: EXISTING_USER.username,
    owner_display: EXISTING_USER.display_name,
    bot_name: 'existing_bot',
    bot_display: '已报名 Bot',
    registered_at: '2026-08-25T08:10:00+00:00',
  }]
}

async function mockBase(page: Page, user = ADMIN) {
  const unexpectedBackendRequests: string[] = []
  const forbiddenMainRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.port === '50380') forbiddenMainRequests.push(`${request.method()} ${request.url()}`)
  })
  await page.route('https://fonts.googleapis.com/**', (route) => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }))
  // Install the catch-all first. Playwright resolves newer matching routes first,
  // so each feature fixture below takes precedence and unknown calls stay visible.
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/notifications/unread-count') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"count":0}' })
      return
    }
    if (url.pathname === '/api/local-ai/agents') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' })
      return
    }
    unexpectedBackendRequests.push(`${route.request().method()} ${url.pathname}${url.search}`)
    await route.fulfill({
      status: 418,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `unmocked roster endpoint: ${url.pathname}` }),
    })
  })
  await page.route('**/api/auth/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ user }),
  }))
  await page.addInitScript((activeUser) => {
    localStorage.setItem('bzplat_token', 'roster-test-token')
    localStorage.setItem('bzplat_user', JSON.stringify(activeUser))
  }, user)
  return { unexpectedBackendRequests, forbiddenMainRequests }
}

async function mockAdminContestList(page: Page) {
  const entryRequests: URL[] = []
  await page.route('**/api/admin/contests?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ contests: [contest()], page: 1, per_page: 20, total: 1 }),
  }))
  await page.route(new RegExp(`/api/admin/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), (route) => {
    entryRequests.push(new URL(route.request().url()))
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ entries: existingEntries() }),
    })
  })
  return { entryRequests }
}

async function openAdminRoster(page: Page) {
  await page.goto('/#/admin?tab=contests')
  await page.getByRole('button', { name: '管理名册', exact: true }).click()
  await expect(page.getByTestId('admin-contest-roster-assign')).toBeVisible()
}

async function mockPickerData(page: Page) {
  const userSearches: URL[] = []
  const botSearches: URL[] = []
  const users = [EXISTING_USER, ALPHA_USER, BETA_USER, INACTIVE_USER, ...FILLER_USERS, PAGED_USER]
  await page.route('**/api/admin/users?*', async (route) => {
    const url = new URL(route.request().url())
    userSearches.push(url)
    const q = (url.searchParams.get('q') || '').toLowerCase()
    const activeOnly = url.searchParams.get('active') === 'true'
    const filtered = users.filter((user) => (
      (!activeOnly || Boolean(user.is_active)) &&
      (!q || user.username.toLowerCase().includes(q) || user.display_name.toLowerCase().includes(q))
    ))
    const page = Number(url.searchParams.get('page') || '1')
    const perPage = Number(url.searchParams.get('per_page') || '20')
    const rows = filtered.slice((page - 1) * perPage, page * perPage)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ users: rows, page, per_page: perPage, total: filtered.length }),
    })
  })
  await page.route('**/api/admin/bots?*', async (route) => {
    const url = new URL(route.request().url())
    botSearches.push(url)
    const ownerId = Number(url.searchParams.get('owner_id'))
    const allBots = ownerId === ALPHA_USER.id
      ? ALPHA_BOTS
      : ownerId === BETA_USER.id
        ? BETA_BOTS
        : ownerId === PAGED_USER.id
          ? PAGED_BOTS
          : []
    const q = (url.searchParams.get('q') || '').toLowerCase()
    const filtered = allBots.filter((bot) => (
      !q || bot.name.toLowerCase().includes(q) || bot.display_name.toLowerCase().includes(q)
    ))
    const page = Number(url.searchParams.get('page') || '1')
    const perPage = Number(url.searchParams.get('per_page') || '50')
    const bots = filtered.slice((page - 1) * perPage, page * perPage)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots, page, per_page: perPage, total: filtered.length }),
    })
  })
  return { userSearches, botSearches }
}

test('admin stages exact user-to-Bot mappings, replaces/removes rows and posts only explicit entries', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const contestRequests = await mockAdminContestList(page)
  const picker = await mockPickerData(page)
  let posted: Record<string, unknown> | null = null
  let postCount = 0
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    postCount += 1
    posted = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ added: 2, skipped: [], total_entries: 3 }),
    })
  })

  await openAdminRoster(page)
  await page.getByRole('button', { name: '选择参赛用户与 Bot', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择参赛用户与 Bot' })
  await expect(dialog).toBeVisible()
  await expect.poll(() => contestRequests.entryRequests.some(
    (url) => url.searchParams.get('identity') === 'false',
  )).toBe(true)

  const search = dialog.getByRole('textbox', { name: '搜索参赛用户' })
  await search.fill('alpha')
  await expect(dialog.getByRole('button', { name: `选择用户 ${ALPHA_USER.username}` })).toBeVisible()
  expect(picker.userSearches.some((url) => url.searchParams.get('q') === 'alpha')).toBe(true)
  expect(picker.userSearches.every((url) => url.searchParams.get('active') === 'true')).toBe(true)
  await expect(dialog.getByRole('button', { name: `选择用户 ${EXISTING_USER.username}` })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: `选择用户 ${INACTIVE_USER.username}` })).toHaveCount(0)

  await dialog.getByRole('button', { name: `选择用户 ${ALPHA_USER.username}` }).click()
  await dialog.getByLabel('选择该用户的 Bot').click()
  await page.getByRole('option', { name: /Alpha 一号/ }).click()
  await dialog.getByRole('button', { name: '加入待指派', exact: true }).click()

  await search.fill('beta')
  await expect(dialog.getByRole('button', { name: `选择用户 ${BETA_USER.username}` })).toBeVisible()
  await dialog.getByRole('button', { name: `选择用户 ${BETA_USER.username}` }).click()
  await dialog.getByLabel('选择该用户的 Bot').click()
  await page.getByRole('option', { name: /Beta 一号/ }).click()
  await dialog.getByRole('button', { name: '加入待指派', exact: true }).click()
  await expect(dialog.getByText('2 人', { exact: true })).toBeVisible()

  await dialog.getByLabel(`更换 ${ALPHA_USER.username} 的 Bot`).click()
  await page.getByRole('option', { name: /Alpha 二号/ }).click()
  await dialog.getByLabel(`移除 ${BETA_USER.username} 的待指派项`).click()
  await expect(dialog.getByText('1 人', { exact: true })).toBeVisible()

  await dialog.getByRole('button', { name: `选择用户 ${BETA_USER.username}` }).click()
  await dialog.getByRole('textbox', { name: '搜索该用户的 Bot' }).fill('beta_two')
  await dialog.getByLabel('选择该用户的 Bot').click()
  await page.getByRole('option', { name: /Beta 二号/ }).click()
  await dialog.getByRole('button', { name: '加入待指派', exact: true }).click()
  await dialog.getByRole('button', { name: '确认指派 2 人', exact: true }).dblclick()

  await expect(dialog).toBeHidden()
  await expect(page.getByText('已指派 2 人', { exact: true })).toBeVisible()
  expect(posted).toEqual({
    entries: [
      { user_id: ALPHA_USER.id, bot_id: ALPHA_BOTS[1].id },
      { user_id: BETA_USER.id, bot_id: BETA_BOTS[1].id },
    ],
  })
  expect(postCount).toBe(1)
  expect(picker.botSearches.length).toBeGreaterThanOrEqual(3)
  expect(picker.botSearches.some((url) => url.searchParams.get('q') === 'beta_two')).toBe(true)
  for (const request of picker.botSearches) {
    expect(request.searchParams.get('active')).toBe('true')
    expect(request.searchParams.get('runnable')).toBe('true')
    expect(request.searchParams.get('game_id')).toBe('gomoku')
    expect(request.searchParams.get('per_page')).toBe('50')
    expect([String(ALPHA_USER.id), String(BETA_USER.id)]).toContain(request.searchParams.get('owner_id'))
  }
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('Bot picker paginates beyond the first 50 runnable Bots', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  await mockAdminContestList(page)
  const picker = await mockPickerData(page)
  let posted: Record<string, unknown> | null = null
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    posted = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ added: 1, skipped: [], total_entries: 2 }),
    })
  })

  await openAdminRoster(page)
  await page.getByRole('button', { name: '选择参赛用户与 Bot', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择参赛用户与 Bot' })
  await expect(dialog.getByText('共 21 个活跃用户', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '下一页用户', exact: true }).click()
  await expect(dialog.getByText('2 / 2', { exact: true }).first()).toBeVisible()
  await dialog.getByRole('button', { name: `选择用户 ${PAGED_USER.username}` }).click()
  await expect(dialog.getByText('共 51 个可用 Bot', { exact: true })).toBeVisible()
  const nextBotPage = dialog.getByRole('button', { name: '下一页', exact: true })
  await nextBotPage.click()
  await expect(nextBotPage.locator('xpath=..').getByText('2 / 2', { exact: true })).toBeVisible()
  await expect(dialog.getByLabel('选择该用户的 Bot')).toContainText('多 Bot 51')
  await dialog.getByRole('button', { name: '加入待指派', exact: true }).click()
  await dialog.getByRole('button', { name: '确认指派 1 人', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(posted).toEqual({
    entries: [{ user_id: PAGED_USER.id, bot_id: PAGED_BOTS[50].id }],
  })
  expect(picker.botSearches.some((url) => url.searchParams.get('page') === '2')).toBe(true)
  expect(picker.userSearches.some((url) => url.searchParams.get('page') === '2')).toBe(true)
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('partial explicit assignment stays open with exact reasons and can be corrected', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const contestRequests = await mockAdminContestList(page)
  await mockPickerData(page)
  const posts: Array<Record<string, unknown>> = []
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    posts.push(route.request().postDataJSON() as Record<string, unknown>)
    const first = posts.length === 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(first
        ? { added: 0, skipped: [`bot ${ALPHA_BOTS[0].id} 当前不可运行，跳过`], total_entries: 1 }
        : { added: 1, skipped: [], total_entries: 2 }),
    })
  })

  await openAdminRoster(page)
  await page.getByRole('button', { name: '选择参赛用户与 Bot', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择参赛用户与 Bot' })
  await expect.poll(() => contestRequests.entryRequests.some(
    (url) => url.searchParams.get('identity') === 'false',
  )).toBe(true)
  await dialog.getByRole('textbox', { name: '搜索参赛用户' }).fill('alpha')
  await dialog.getByRole('button', { name: `选择用户 ${ALPHA_USER.username}` }).click()
  await dialog.getByLabel('选择该用户的 Bot').click()
  await page.getByRole('option', { name: /Alpha 一号/ }).click()
  await dialog.getByRole('button', { name: '加入待指派', exact: true }).click()
  await dialog.getByRole('button', { name: '确认指派 1 人', exact: true }).click()

  await expect(dialog).toBeVisible()
  const alert = dialog.getByRole('alert')
  await expect(alert).toContainText(`bot ${ALPHA_BOTS[0].id} 当前不可运行，跳过`)
  await expect(dialog.getByRole('button', { name: '确认指派 1 人', exact: true })).toBeEnabled()

  await dialog.getByLabel(`更换 ${ALPHA_USER.username} 的 Bot`).click()
  await page.getByRole('option', { name: /Alpha 二号/ }).click()
  await expect(alert).toHaveCount(0)
  await dialog.getByRole('button', { name: '确认指派 1 人', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(posts).toEqual([
    { entries: [{ user_id: ALPHA_USER.id, bot_id: ALPHA_BOTS[0].id }] },
    { entries: [{ user_id: ALPHA_USER.id, bot_id: ALPHA_BOTS[1].id }] },
  ])
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('assign-all remains a secondary confirmed action with its own payload', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  await mockAdminContestList(page)
  let posted: Record<string, unknown> | null = null
  let postCount = 0
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    postCount += 1
    posted = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ added: 7, skipped: [], total_entries: 8 }),
    })
  })

  await openAdminRoster(page)
  const precise = page.getByRole('button', { name: '选择参赛用户与 Bot', exact: true })
  const assignAll = page.getByRole('button', { name: '指派全部可用用户', exact: true })
  await expect(precise).toHaveAttribute('data-variant', 'default')
  await expect(assignAll).toHaveAttribute('data-variant', 'outline')
  await assignAll.dblclick()
  await expect(page.getByRole('button', { name: '准备确认…', exact: true })).toBeDisabled()
  const confirm = page.getByRole('dialog', { name: '指派全部可用用户？' })
  await expect(confirm).toContainText('请只在确实需要全员参赛时使用')
  expect(posted).toBeNull()
  await confirm.getByRole('button', { name: '确认全员指派', exact: true }).click()
  await expect(page.getByText('已指派 7 人', { exact: true })).toBeVisible()
  expect(posted).toEqual({ assign_all: true, game_id: 'gomoku' })
  expect(postCount).toBe(1)
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

async function mockContestDetail(page: Page, requireRealName: number, isOrganizer = true) {
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      contest: contest(requireRealName),
      entries: existingEntries(),
      pairings: [],
      standings: [],
      stage_standings: [],
      entries_page: 1,
      entries_per_page: 20,
      entries_total: 1,
      my_entry: null,
      is_organizer: isOrganizer,
    }),
  }))
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))
  await page.route(new RegExp(`/api/admin/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ entries: existingEntries() }),
  }))
  await page.route('**/api/admin/users?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ users: [ALPHA_USER], page: 1, per_page: 20, total: 1 }),
  }))
}

test('contest detail reuses the admin picker and remains keyboard/touch safe at 390px', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  await mockContestDetail(page, 0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/contests/${CONTEST_ID}`)

  const assign = page.getByTestId('admin-contest-roster-assign')
  await expect(assign).toBeVisible()
  const precise = assign.getByRole('button', { name: '选择参赛用户与 Bot', exact: true })
  const assignAll = assign.getByRole('button', { name: '指派全部可用用户', exact: true })
  expect((await precise.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect((await assignAll.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)

  await precise.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: '选择参赛用户与 Bot' })
  await expect(dialog).toBeVisible()
  expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
  await expect(dialog.getByRole('textbox', { name: '搜索参赛用户' })).toBeFocused()
  await page.keyboard.press('Tab')
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true)
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('real-name organizer keeps the existing no-delegation boundary', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page, ORGANIZER)
  await mockContestDetail(page, 1)
  await page.goto(`/#/contests/${CONTEST_ID}`)

  await expect(page.getByText('实名赛事仅允许选手本人报名，组织者不能代报名或批量指派。')).toBeVisible()
  await expect(page.getByTestId('admin-contest-roster-assign')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '批量指派', exact: true })).toHaveCount(0)
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})
