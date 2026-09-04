import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'
import { GOMOKU_TEMPLATE_TIME_CONTROLS } from './time-control-fixtures'

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
  await page.route('**/api/auth/me', (route) => {
    expect(route.request().headers().authorization).toBeUndefined()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ user }),
    })
  })
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

test('Bot picker ignores a delayed response after the selected user changes', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  await mockAdminContestList(page)
  let releaseAlpha: (() => void) | undefined
  let markAlphaRequested: () => void = () => undefined
  const alphaRequested = new Promise<void>((resolve) => {
    markAlphaRequested = resolve
  })
  await page.route('**/api/admin/bots?*', async (route) => {
    const url = new URL(route.request().url())
    const ownerId = Number(url.searchParams.get('owner_id'))
    if (ownerId === ALPHA_USER.id) {
      markAlphaRequested()
      await new Promise<void>((release) => { releaseAlpha = release })
    }
    const bots = ownerId === ALPHA_USER.id ? ALPHA_BOTS : BETA_BOTS
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bots, page: 1, per_page: 50, total: bots.length }),
    })
  })
  await page.route('**/api/admin/users?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ users: [ALPHA_USER, BETA_USER], page: 1, per_page: 20, total: 2 }),
  }))

  await openAdminRoster(page)
  await page.getByRole('button', { name: '选择参赛用户与 Bot', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择参赛用户与 Bot' })
  await dialog.getByRole('button', { name: `选择用户 ${ALPHA_USER.username}` }).click()
  await alphaRequested
  try {
    await dialog.getByRole('button', { name: `选择用户 ${BETA_USER.username}` }).click()
    // Release Alpha immediately after the click, before waiting for Beta's
    // passive effect/request. chooseUser must invalidate the old generation
    // synchronously in this render-to-effect window.
    releaseAlpha?.()
    await expect(dialog.getByText(
      `已选用户：${BETA_USER.display_name}（@${BETA_USER.username}）`,
      { exact: true },
    )).toBeVisible()
    const botSelect = dialog.getByLabel('选择该用户的 Bot')
    await expect(botSelect).toBeEnabled()
    await botSelect.click()
    await expect(page.getByRole('option', { name: /Beta 一号/ })).toBeVisible()
    await expect(page.getByRole('option', { name: /Alpha 一号/ })).toHaveCount(0)
    await page.keyboard.press('Escape')
    await dialog.getByRole('button', { name: `选择用户 ${BETA_USER.username}` }).click()
    await botSelect.click()
    await expect(page.getByRole('option', { name: /Beta 一号/ })).toBeVisible()
    await expect(page.getByRole('option', { name: /Alpha 一号/ })).toHaveCount(0)
    await page.keyboard.press('Escape')
  } finally {
    releaseAlpha?.()
  }
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
  const triggerBox = await assignAll.boundingBox()
  expect(triggerBox).not.toBeNull()
  const triggerPoint = {
    x: triggerBox!.x + triggerBox!.width / 2,
    y: triggerBox!.y + triggerBox!.height / 2,
  }
  await page.mouse.click(triggerPoint.x, triggerPoint.y)
  await expect(
    page.getByTestId('admin-contest-roster-assign').locator('button').filter({ hasText: '等待确认…' }),
  ).toBeDisabled()
  const confirm = page.getByRole('dialog', { name: '指派全部可用用户？' })
  await expect(confirm).toContainText('请只在确实需要全员参赛时使用')
  await page.waitForTimeout(400)
  expect(await page.evaluate(({ x, y }) => (
    document.elementFromPoint(x, y)?.closest('[data-slot="dialog-overlay"]') != null
  ), triggerPoint)).toBe(true)
  await page.mouse.click(triggerPoint.x, triggerPoint.y)
  await expect(confirm).toBeVisible()
  expect(posted).toBeNull()
  expect(postCount).toBe(0)
  await page.keyboard.press('Escape')
  await expect(confirm).toBeHidden()
  const rawAssignAll = page.getByTestId('admin-contest-roster-assign')
    .locator('button').filter({ hasText: '指派全部可用用户' })
  await expect(rawAssignAll).toBeEnabled()

  await rawAssignAll.click()
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '取消', exact: true }).click()
  await expect(confirm).toBeHidden()
  await expect(rawAssignAll).toBeEnabled()

  await rawAssignAll.click()
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '确认全员指派', exact: true }).click()
  await expect(page.getByText('已指派 7 人', { exact: true })).toBeVisible()
  expect(posted).toEqual({ assign_all: true, game_id: 'gomoku' })
  expect(postCount).toBe(1)
  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('shared confirmations keep default outside-click dismissal', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  await mockAdminContestList(page)
  await page.goto('/#/admin?tab=contests')

  await page.getByRole('button', { name: '删除草稿', exact: true }).click()
  const confirm = page.getByRole('dialog', { name: '删除锦标赛草稿' })
  await expect(confirm).toBeVisible()
  expect(await page.evaluate(() => (
    document.elementFromPoint(5, 5)?.closest('[data-slot="dialog-overlay"]') != null
  ))).toBe(true)
  await page.mouse.click(5, 5)
  await expect(confirm).toBeHidden()

  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

async function mockContestDetail(page: Page, requireRealName: number, isOrganizer = true) {
  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
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

test('contest detail keeps async action refreshes on the current roster page', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const cancelledLightRequests: string[] = []
  page.on('requestfailed', (request) => {
    const url = new URL(request.url())
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/contests/${CONTEST_ID}/entries` &&
      url.search === '?page=2&per_page=20'
    ) cancelledLightRequests.push(request.failure()?.errorText || '')
  })
  const fullDetailPages: number[] = []
  const lightEntryPages: number[] = []
  let resolveAssignStarted!: () => void
  let resolveAssign!: () => void
  let resolveFirstPageTwoStarted!: () => void
  let resolveFirstPageTwo!: () => void
  let resolveFirstPageTwoSettled!: () => void
  let resolveOpenStarted!: () => void
  let resolveOpen!: () => void
  const assignStarted = new Promise<void>((resolve) => { resolveAssignStarted = resolve })
  const assignGate = new Promise<void>((resolve) => { resolveAssign = resolve })
  const firstPageTwoStarted = new Promise<void>((resolve) => { resolveFirstPageTwoStarted = resolve })
  const firstPageTwoGate = new Promise<void>((resolve) => { resolveFirstPageTwo = resolve })
  const firstPageTwoSettled = new Promise<void>((resolve) => { resolveFirstPageTwoSettled = resolve })
  const openStarted = new Promise<void>((resolve) => { resolveOpenStarted = resolve })
  const openGate = new Promise<void>((resolve) => { resolveOpen = resolve })
  let lightPageTwoRequests = 0

  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), async (route) => {
    const url = new URL(route.request().url())
    const requestedPage = Number(url.searchParams.get('entries_page') || '1')
    fullDetailPages.push(requestedPage)
    let entries = [{
      ...existingEntries()[0],
      bot_display: '第 1 页选手',
    }]
    if (requestedPage === 2) {
      entries = [{
        ...existingEntries()[0],
        id: 989_203,
        user_id: 989_021,
        bot_id: 989_221,
        bot_name: 'current_page_two_bot',
        bot_display: '当前第 2 页选手',
      }]
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: contest(0),
        entries,
        pairings: [],
        standings: [],
        stage_standings: [],
        entries_page: requestedPage,
        entries_per_page: 20,
        entries_total: 40,
        my_entry: null,
        is_organizer: true,
      }),
    })
  })
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), async (route) => {
    const url = new URL(route.request().url())
    const requestedPage = Number(url.searchParams.get('page') || '1')
    lightEntryPages.push(requestedPage)
    let entries = [{ ...existingEntries()[0], bot_display: '第 1 页选手' }]
    if (requestedPage === 2) {
      lightPageTwoRequests += 1
      resolveFirstPageTwoStarted()
      await firstPageTwoGate
      entries = [{
        ...existingEntries()[0],
        id: 989_202,
        user_id: 989_020,
        bot_id: 989_220,
        bot_name: 'late_page_two_bot',
        bot_display: '迟到的第 2 页选手',
      }]
    }
    try {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entries, page: requestedPage, per_page: 20, total: 40 }),
      })
    } catch {
      // AbortController may cancel this deliberately stale intercepted request.
    } finally {
      if (requestedPage === 2) resolveFirstPageTwoSettled()
    }
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    resolveAssignStarted()
    await assignGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ added: 1, skipped: [], total_entries: 40 }),
    })
  })
  await page.route(`**/api/contests/${CONTEST_ID}/open`, async (route) => {
    resolveOpenStarted()
    await openGate
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await expect(page.getByText('第 1 页选手', { exact: true })).toBeVisible()

  const assignAll = page.getByTestId('admin-contest-roster-assign')
    .getByRole('button', { name: '指派全部可用用户', exact: true })
  await assignAll.click()
  await page.getByRole('dialog', { name: '指派全部可用用户？' })
    .getByRole('button', { name: '确认全员指派', exact: true })
    .click()
  await assignStarted

  const rosterPagination = page.getByRole('navigation', { name: '分页导航' })
  const pageTwo = rosterPagination.getByRole('button', { name: '第 2 页' })
  await pageTwo.click()
  await firstPageTwoStarted
  expect(fullDetailPages).toEqual([1])
  expect(lightEntryPages).toEqual([2])
  resolveAssign()

  // The child action started on page 1, but its delayed onDone callback must
  // refresh the page selected while the POST was in flight with authoritative
  // full detail. Pagination alone must never repeat that O(n²) projection.
  await expect.poll(() => fullDetailPages.filter((value) => value === 2).length).toBe(1)
  await expect(page.getByText('当前第 2 页选手', { exact: true })).toBeVisible()
  expect(lightPageTwoRequests).toBe(1)

  // A late response from the first page-2 request is from an older generation
  // and cannot replace the action refresh that already won.
  resolveFirstPageTwo()
  await firstPageTwoSettled
  await expect(page.getByText('迟到的第 2 页选手', { exact: true })).toHaveCount(0)

  // Parent-owned actions freeze pagination until their POST and current-page
  // refresh finish, so the visible rows cannot drift underneath a row action.
  await page.getByRole('button', { name: '开放报名', exact: true }).click()
  await openStarted
  await expect(rosterPagination.getByRole('button', { name: '第 1 页' })).toBeDisabled()
  await expect(pageTwo).toBeDisabled()
  expect((await pageTwo.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  resolveOpen()
  await expect(page.getByText('已开放报名', { exact: true })).toBeVisible()
  await expect(page.getByText('当前第 2 页选手', { exact: true })).toBeVisible()
  expect(fullDetailPages.at(-1)).toBe(2)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)

  await expect.poll(() => cancelledLightRequests.length).toBe(1)
  expect(cancelledLightRequests[0]).toMatch(
    /^(?:net::ERR_ABORTED|NS_BINDING_ABORTED|load request cancelled)$/i,
  )
  await monitor.expectClean([{
    kind: 'requestfailed',
    method: 'GET',
    pathname: `/api/contests/${CONTEST_ID}/entries`,
    search: '?page=2&per_page=20',
    errorText: cancelledLightRequests[0],
  }])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('rapid roster pagination uses light reads and aborts a stale page response', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const cancelledLightRequests: string[] = []
  page.on('requestfailed', (request) => {
    const url = new URL(request.url())
    if (
      request.method() === 'GET' &&
      url.pathname === `/api/contests/${CONTEST_ID}/entries` &&
      url.search === '?page=2&per_page=20'
    ) cancelledLightRequests.push(request.failure()?.errorText || '')
  })
  const fullDetailPages: number[] = []
  const lightEntryPages: number[] = []
  let releaseLatePage!: () => void
  let markLatePageStarted!: () => void
  let markLatePageSettled!: () => void
  const latePageGate = new Promise<void>((resolve) => { releaseLatePage = resolve })
  const latePageStarted = new Promise<void>((resolve) => { markLatePageStarted = resolve })
  const latePageSettled = new Promise<void>((resolve) => { markLatePageSettled = resolve })

  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), async (route) => {
    const url = new URL(route.request().url())
    fullDetailPages.push(Number(url.searchParams.get('entries_page') || '1'))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: contest(0),
        entries: [{ ...existingEntries()[0], bot_display: '初始第 1 页选手' }],
        pairings: [],
        standings: [],
        stage_standings: [],
        entries_page: 1,
        entries_per_page: 20,
        entries_total: 40,
        my_entry: null,
        is_organizer: true,
      }),
    })
  })
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), async (route) => {
    const url = new URL(route.request().url())
    const requestedPage = Number(url.searchParams.get('page') || '1')
    lightEntryPages.push(requestedPage)
    if (requestedPage === 2) {
      markLatePageStarted()
      await latePageGate
    }
    const entries = requestedPage === 2
      ? [{ ...existingEntries()[0], id: 989_205, bot_display: '迟到的第 2 页选手' }]
      : [{ ...existingEntries()[0], bot_display: '当前第 1 页选手' }]
    try {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entries, page: requestedPage, per_page: 20, total: 40 }),
      })
    } catch {
      // The stale page-2 request is expected to be aborted by the page-1 load.
    } finally {
      if (requestedPage === 2) markLatePageSettled()
    }
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await expect(page.getByText('初始第 1 页选手', { exact: true })).toBeVisible()
  const rosterPagination = page.getByRole('navigation', { name: '分页导航' })
  await rosterPagination.getByRole('button', { name: '第 2 页' }).click()
  await latePageStarted
  await rosterPagination.getByRole('button', { name: '第 1 页' }).click()
  await expect(page.getByText('当前第 1 页选手', { exact: true })).toBeVisible()

  expect(fullDetailPages).toEqual([1])
  expect(lightEntryPages).toEqual([2, 1])
  releaseLatePage()
  await latePageSettled
  await expect(page.getByText('迟到的第 2 页选手', { exact: true })).toHaveCount(0)
  await expect(page.getByText('当前第 1 页选手', { exact: true })).toBeVisible()

  await expect.poll(() => cancelledLightRequests.length).toBe(1)
  expect(cancelledLightRequests[0]).toMatch(
    /^(?:net::ERR_ABORTED|NS_BINDING_ABORTED|load request cancelled)$/i,
  )
  await monitor.expectClean([{
    kind: 'requestfailed',
    method: 'GET',
    pathname: `/api/contests/${CONTEST_ID}/entries`,
    search: '?page=2&per_page=20',
    errorText: cancelledLightRequests[0],
  }])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('official-results failure survives a successful light roster page load', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  let releaseRosterPage!: () => void
  let markRosterPageStarted!: () => void
  const rosterPageGate = new Promise<void>((resolve) => { releaseRosterPage = resolve })
  const rosterPageStarted = new Promise<void>((resolve) => { markRosterPageStarted = resolve })

  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      contest: { ...contest(0), status: 'finished' },
      entries: [{ ...existingEntries()[0], bot_display: '完赛第 1 页选手' }],
      pairings: [],
      standings: [],
      stage_standings: [],
      entries_page: 1,
      entries_per_page: 20,
      entries_total: 40,
      my_entry: null,
      is_organizer: true,
    }),
  }))
  await page.route(`**/api/contests/${CONTEST_ID}/official-results`, (route) => route.fulfill({
    status: 500,
    contentType: 'application/json',
    body: JSON.stringify({ detail: '正式名次服务暂不可用' }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), async (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('page') || '1')
    if (requestedPage === 2) {
      markRosterPageStarted()
      await rosterPageGate
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        entries: [{
          ...existingEntries()[0],
          id: 989_207,
          bot_display: requestedPage === 2 ? '完赛第 2 页选手' : '完赛第 1 页选手',
        }],
        page: requestedPage,
        per_page: 20,
        total: 40,
      }),
    })
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))

  await page.goto(`/#/contests/${CONTEST_ID}`)
  const officialError = page.getByText('正式名次加载失败：正式名次服务暂不可用', { exact: true })
  await expect(officialError).toBeVisible()
  await page.getByRole('tab', { name: /选手/ }).click()
  await expect(page.getByText('完赛第 1 页选手', { exact: true })).toBeVisible()

  await page.getByRole('navigation', { name: '分页导航' })
    .getByRole('button', { name: '第 2 页' })
    .click()
  await rosterPageStarted
  await expect(officialError).toBeVisible()
  releaseRosterPage()
  await expect(page.getByText('完赛第 2 页选手', { exact: true })).toBeVisible()
  await expect(officialError).toBeVisible()

  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 500,
    pathname: `/api/contests/${CONTEST_ID}/official-results`,
    search: '',
  }])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('a succeeding light roster retry clears its own prior error', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  let pageTwoAttempts = 0
  let releasePageOneRetry!: () => void
  let markPageOneRetryStarted!: () => void
  const pageOneRetryGate = new Promise<void>((resolve) => { releasePageOneRetry = resolve })
  const pageOneRetryStarted = new Promise<void>((resolve) => { markPageOneRetryStarted = resolve })

  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      contest: { ...contest(0), status: 'published' },
      entries: [{ ...existingEntries()[0], bot_display: '初始名册第 1 页选手' }],
      pairings: [],
      standings: [],
      stage_standings: [],
      entries_page: 1,
      entries_per_page: 20,
      entries_total: 40,
      my_entry: null,
      is_organizer: true,
    }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), async (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('page') || '1')
    if (requestedPage === 2) {
      pageTwoAttempts += 1
      if (pageTwoAttempts === 1) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: '名册分页暂不可用' }),
        })
        return
      }
    } else {
      markPageOneRetryStarted()
      await pageOneRetryGate
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        entries: [{
          ...existingEntries()[0],
          id: requestedPage === 2 ? 989_208 : existingEntries()[0].id,
          bot_display: requestedPage === 2 ? '重试成功的第 2 页选手' : '重试成功的第 1 页选手',
        }],
        page: requestedPage,
        per_page: 20,
        total: 40,
      }),
    })
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await page.getByRole('tab', { name: /选手/ }).click()
  await expect(page.getByText('初始名册第 1 页选手', { exact: true })).toBeVisible()
  const rosterPagination = page.getByRole('navigation', { name: '分页导航' })
  await rosterPagination.getByRole('button', { name: '第 2 页' }).click()
  const rosterError = page.getByText('名册分页暂不可用', { exact: true })
  await expect(rosterError).toBeVisible()

  await rosterPagination.getByRole('button', { name: '第 1 页' }).click()
  await pageOneRetryStarted
  await expect(rosterError).toBeVisible()
  releasePageOneRetry()
  await expect(page.getByText('重试成功的第 1 页选手', { exact: true })).toBeVisible()
  await expect(rosterError).toHaveCount(0)

  await rosterPagination.getByRole('button', { name: '第 2 页' }).click()
  await expect(page.getByText('重试成功的第 2 页选手', { exact: true })).toBeVisible()
  await expect(rosterError).toHaveCount(0)
  expect(pageTwoAttempts).toBe(2)

  await monitor.expectClean([{
    kind: 'http',
    method: 'GET',
    status: 500,
    pathname: `/api/contests/${CONTEST_ID}/entries`,
    search: '?page=2&per_page=20',
  }])
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('removing the only row on the last roster page refetches the last valid full-detail page', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const fullDetailPages: number[] = []
  const lightEntryPages: number[] = []
  let deleted = false
  let releaseCorrectedPage!: () => void
  let markCorrectedPageStarted!: () => void
  const correctedPageGate = new Promise<void>((resolve) => { releaseCorrectedPage = resolve })
  const correctedPageStarted = new Promise<void>((resolve) => { markCorrectedPageStarted = resolve })
  const lastEntry = {
    ...existingEntries()[0],
    id: 989_206,
    user_id: 989_026,
    bot_id: 989_226,
    bot_name: 'last_page_only_bot',
    bot_display: '末页唯一选手',
  }

  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), async (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('entries_page') || '1')
    fullDetailPages.push(requestedPage)
    if (deleted && requestedPage === 1) {
      markCorrectedPageStarted()
      await correctedPageGate
    }
    const entries = requestedPage === 2
      ? (deleted ? [] : [lastEntry])
      : [{ ...existingEntries()[0], bot_display: deleted ? '删除后第 1 页选手' : '初始第 1 页选手' }]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: contest(0),
        entries,
        pairings: [],
        standings: [],
        stage_standings: [],
        entries_page: requestedPage,
        entries_per_page: 20,
        entries_total: deleted ? 20 : 21,
        my_entry: null,
        is_organizer: true,
      }),
    })
  })
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('page') || '1')
    lightEntryPages.push(requestedPage)
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        entries: requestedPage === 2 ? [lastEntry] : existingEntries(),
        page: requestedPage,
        per_page: 20,
        total: 21,
      }),
    })
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))
  await page.route(`**/api/contests/${CONTEST_ID}/entries/${lastEntry.user_id}`, async (route) => {
    deleted = true
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await expect(page.getByText('初始第 1 页选手', { exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: '分页导航' })
    .getByRole('button', { name: '第 2 页' })
    .click()
  await expect(page.getByText('末页唯一选手', { exact: true })).toBeVisible()
  expect(fullDetailPages).toEqual([1])
  expect(lightEntryPages).toEqual([2])

  await page.getByRole('button', { name: '移除', exact: true }).click()
  await page.getByRole('dialog', { name: '移除报名选手？' })
    .getByRole('button', { name: '确认移除', exact: true })
    .click()
  await correctedPageStarted

  // Do not first commit the now-empty page 2 while the corrective full read is pending.
  await expect(page.getByText('末页唯一选手', { exact: true })).toBeVisible()
  await expect(page.getByText('暂无报名', { exact: true })).toHaveCount(0)
  releaseCorrectedPage()
  await expect(page.getByText('删除后第 1 页选手', { exact: true })).toBeVisible()
  expect(fullDetailPages).toEqual([1, 2, 1])
  expect(lightEntryPages).toEqual([2])

  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('a light roster response that observes a concurrent page shrink refetches the last valid page', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  const fullDetailPages: number[] = []
  const lightEntryPages: number[] = []
  let releaseCorrectedPage!: () => void
  let markCorrectedPageStarted!: () => void
  const correctedPageGate = new Promise<void>((resolve) => { releaseCorrectedPage = resolve })
  const correctedPageStarted = new Promise<void>((resolve) => { markCorrectedPageStarted = resolve })

  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}(?:\\?.*)?$`), async (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('entries_page') || '1')
    fullDetailPages.push(requestedPage)
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: contest(0),
        entries: [{ ...existingEntries()[0], bot_display: '并发删除前第 1 页选手' }],
        pairings: [],
        standings: [],
        stage_standings: [],
        entries_page: requestedPage,
        entries_per_page: 20,
        entries_total: 21,
        my_entry: null,
        is_organizer: true,
      }),
    })
  })
  await page.route(new RegExp(`/api/contests/${CONTEST_ID}/entries(?:\\?.*)?$`), async (route) => {
    const requestedPage = Number(new URL(route.request().url()).searchParams.get('page') || '1')
    lightEntryPages.push(requestedPage)
    if (requestedPage === 1) {
      markCorrectedPageStarted()
      await correctedPageGate
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        entries: requestedPage === 1
          ? [{ ...existingEntries()[0], bot_display: '并发缩页后的第 1 页选手' }]
          : [],
        page: requestedPage,
        per_page: 20,
        total: 20,
      }),
    })
  })
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await expect(page.getByText('并发删除前第 1 页选手', { exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: '分页导航' })
    .getByRole('button', { name: '第 2 页' })
    .click()
  await correctedPageStarted

  // The stale page remains visible until the replacement page has arrived.
  await expect(page.getByText('并发删除前第 1 页选手', { exact: true })).toBeVisible()
  await expect(page.getByText('暂无报名', { exact: true })).toHaveCount(0)
  releaseCorrectedPage()
  await expect(page.getByText('并发缩页后的第 1 页选手', { exact: true })).toBeVisible()
  expect(fullDetailPages).toEqual([1])
  expect(lightEntryPages).toEqual([2, 1])

  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})

test('a delayed roster callback cannot invalidate the next contest load', async ({ page }) => {
  const nextContestId = CONTEST_ID + 1
  const monitor = monitorBrowser(page)
  const network = await mockBase(page)
  let firstContestLoads = 0
  let resolveAssignStarted!: () => void
  let resolveAssign!: () => void
  let resolveNextLoadStarted!: () => void
  let resolveNextLoad!: () => void
  const assignStarted = new Promise<void>((resolve) => { resolveAssignStarted = resolve })
  const assignGate = new Promise<void>((resolve) => { resolveAssign = resolve })
  const nextLoadStarted = new Promise<void>((resolve) => { resolveNextLoadStarted = resolve })
  const nextLoadGate = new Promise<void>((resolve) => { resolveNextLoad = resolve })

  await page.route('**/api/contests/templates?game=gomoku', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      templates: [{
        id: 'gomoku_rr',
        name: '五子棋单循环',
        game_id: 'gomoku',
        ...GOMOKU_TEMPLATE_TIME_CONTROLS,
        stages: [{ key: 'rr', type: 'round_robin', scoring: 'ccgc_2_1_0' }],
      }],
    }),
  }))
  await page.route(
    new RegExp(`/api/contests/(?:${CONTEST_ID}|${nextContestId})(?:\\?.*)?$`),
    async (route) => {
      const url = new URL(route.request().url())
      const contestId = Number(url.pathname.split('/').at(-1))
      const isNextContest = contestId === nextContestId
      if (isNextContest) {
        resolveNextLoadStarted()
        await nextLoadGate
      } else {
        firstContestLoads += 1
      }
      const requestedPage = Number(url.searchParams.get('entries_page') || '1')
      const detailContest = isNextContest
        ? { ...contest(0), id: nextContestId, title: '切换后的赛事 B' }
        : contest(0)
      const detailEntry = {
        ...existingEntries()[0],
        contest_id: contestId,
        id: isNextContest ? 989_204 : 989_201,
        user_id: isNextContest ? 989_022 : EXISTING_USER.id,
        bot_id: isNextContest ? 989_222 : 989_211,
        bot_name: isNextContest ? 'next_contest_bot' : 'existing_bot',
        bot_display: isNextContest ? '赛事 B 选手' : '赛事 A 选手',
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          contest: detailContest,
          entries: [detailEntry],
          pairings: [],
          standings: [],
          stage_standings: [],
          entries_page: requestedPage,
          entries_per_page: 20,
          entries_total: 1,
          my_entry: null,
          is_organizer: true,
        }),
      })
    },
  )
  await page.route('**/api/bots/mine?*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{"bots":[]}',
  }))
  await page.route(`**/api/admin/contests/${CONTEST_ID}/entries/bulk`, async (route) => {
    resolveAssignStarted()
    await assignGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ added: 1, skipped: [], total_entries: 2 }),
    })
  })

  await page.goto(`/#/contests/${CONTEST_ID}`)
  await expect(page.getByText('赛事 A 选手', { exact: true })).toBeVisible()
  await page.getByTestId('admin-contest-roster-assign')
    .getByRole('button', { name: '指派全部可用用户', exact: true })
    .click()
  await page.getByRole('dialog', { name: '指派全部可用用户？' })
    .getByRole('button', { name: '确认全员指派', exact: true })
    .click()
  await assignStarted

  await page.goto(`/#/contests/${nextContestId}`)
  await nextLoadStarted
  const loadsBeforeOldActionFinished = firstContestLoads
  resolveAssign()
  await expect(page.getByText('已指派 1 人', { exact: true })).toBeVisible()
  expect(firstContestLoads).toBe(loadsBeforeOldActionFinished)

  const nextContestResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === `/api/contests/${nextContestId}`
  })
  resolveNextLoad()
  await nextContestResponse
  await expect(page.getByRole('heading', { name: '切换后的赛事 B', exact: true })).toBeVisible()
  await expect(page.getByText('赛事 B 选手', { exact: true })).toBeVisible()
  await expect(page.getByText('赛事 A 选手', { exact: true })).toHaveCount(0)

  await monitor.expectClean()
  expect(network.unexpectedBackendRequests).toEqual([])
  expect(network.forbiddenMainRequests).toEqual([])
})
