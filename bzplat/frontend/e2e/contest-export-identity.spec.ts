import {
  expect,
  test,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type Download,
  type Locator,
  type Page,
} from '@playwright/test'

import {
  loginThroughUi,
  monitorBrowser,
  runCleanupTasks,
  withCleanup,
  type ExpectedBrowserIssue,
} from './helpers'

const ADMIN = process.env.BZ_E2E_ADMIN || 'qa_admin'
const ORGANIZER = process.env.BZ_E2E_ORGANIZER || 'qa_organizer'
const PLAYER = process.env.BZ_E2E_USER || 'tester1'
const SELF_REGISTER_ONLY_DETAIL = '实名赛事仅允许参赛者本人报名，组织者不可代报名'

const PRIVATE_HEADERS = [
  '报名ID(entry_id)',
  '用户ID(user_id)',
  '用户账号(username)',
  '用户显示名(user_display)',
  'Bot ID(bot_id)',
  'Bot内部名(bot_name)',
  'Bot显示名(bot_display)',
  '实名姓名(real_name)',
  '手机号(phone)',
  '学校(school)',
  '学号(student_id)',
  '实名来源(identity_source)',
  '实名采集时间(identity_captured_at)',
  '实名完整性(identity_completeness)',
  '正式名次(rank)',
  '种子(seed)',
  '分组(group_id)',
  '赛事状态(contest_status)',
  '参赛状态(entry_status)',
  '成绩状态(result_status)',
  '阶段索引(stage_idx)',
  '阶段标识(stage_key)',
  '积分(points)',
  '胜(wins)',
  '平(draws)',
  '负(losses)',
  '净分(delta_total)',
  '奖项(awarded)',
  '报名时间(registered_at)',
] as const

const PUBLIC_HEADERS = [
  'rank',
  'entry_id',
  'bot_name',
  'owner_name',
  'points',
  'buchholz_cut1',
  'sonneborn_berger',
  'awarded',
  'source_stage',
  'ranking_cohort',
] as const

const FIREFOX_SCROLL_LINKED_ADVISORY =
  'This site appears to use a scroll-linked positioning effect.'

interface CsvTable {
  headers: string[]
  rows: Array<Record<string, string>>
}

interface FetchResult {
  status: number
  headers: Record<string, string>
  body: string
}

interface ProfileSnapshot {
  display_name?: string | null
  bio?: string | null
  real_name?: string | null
  phone?: string | null
  school?: string | null
  student_id?: string | null
}

function parseCsv(raw: string): CsvTable {
  const text = raw.replace(/^\uFEFF/, '')
  const matrix: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"'
        index += 1
      } else if (char === '"') {
        quoted = false
      } else {
        field += char
      }
      continue
    }
    if (char === '"') {
      quoted = true
    } else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field.replace(/\r$/, ''))
      matrix.push(row)
      row = []
      field = ''
    } else {
      field += char
    }
  }
  if (field || row.length > 0) {
    row.push(field.replace(/\r$/, ''))
    matrix.push(row)
  }

  const headers = matrix.shift() || []
  return {
    headers,
    rows: matrix
      .filter((values) => values.some((value) => value !== ''))
      .map((values) => Object.fromEntries(headers.map((header, index) => [header, values[index] || '']))),
  }
}

async function readDownloadText(download: Download): Promise<string> {
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(Buffer.from(chunk))
  return Buffer.concat(chunks).toString('utf8')
}

async function browserFetch(page: Page, url: string): Promise<FetchResult> {
  return page.evaluate(async (target) => {
    const response = await fetch(target)
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      body: await response.text(),
    }
  }, url)
}

async function browserPost(page: Page, url: string, data: unknown): Promise<FetchResult> {
  return page.evaluate(async ({ target, payload }) => {
    const response = await fetch(target, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      body: await response.text(),
    }
  }, { target: url, payload: data })
}

async function expectStatus(response: APIResponse, status = 200): Promise<void> {
  expect(response.status(), await response.text()).toBe(status)
}

function expectPrivateCacheHeaders(result: FetchResult): void {
  expect(result.headers['cache-control']).toBe('private, no-store, max-age=0')
  expect(result.headers.pragma).toBe('no-cache')
  expect((result.headers.vary || '').split(',').map((value) => value.trim()).sort()).toEqual([
    'Authorization',
    'Cookie',
  ])
  expect(result.headers['referrer-policy']).toBe('no-referrer')
  expect(result.headers['x-content-type-options']).toBe('nosniff')
}

function expectPrivateHeaders(result: FetchResult, contestId: number): void {
  expect(result.headers['content-type']).toContain('text/csv')
  expect(result.headers['content-disposition']).toBe(
    `attachment; filename="contest-${contestId}-participants-v2.csv"`,
  )
  expectPrivateCacheHeaders(result)
}

function optionalDownloadAborts(pathname: string, search: string): ExpectedBrowserIssue[] {
  return [
    'net::ERR_ABORTED',
    'NS_BINDING_ABORTED',
    'load request cancelled',
  ].map((errorText) => ({
    kind: 'requestfailed' as const,
    method: 'GET',
    pathname,
    search,
    errorText,
    optional: true,
  }))
}

function optionalFirefoxAdvisory(browserName: string): ExpectedBrowserIssue[] {
  return browserName === 'firefox'
    ? [{
        kind: 'console-warning',
        messageIncludes: FIREFOX_SCROLL_LINKED_ADVISORY,
        optional: true,
      }]
    : []
}

async function expectTouchSafeWithoutRootOverflow(page: Page, link: Locator): Promise<void> {
  await page.setViewportSize({ width: 320, height: 568 })
  await expect(link).toBeVisible()
  const bounds = await link.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds?.height || 0).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )).toBeLessThanOrEqual(1)
}

async function focusByKeyboard(page: Page, target: Locator): Promise<void> {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await page.keyboard.press('Tab')
    if (await target.evaluate((element) => document.activeElement === element)) return
  }
  expect(false, 'download link was not reachable through sequential keyboard focus').toBe(true)
}

async function loggedInChild(
  browser: Browser,
  baseURL: string,
  username: string,
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ baseURL, viewport: { width: 1280, height: 720 } })
  const page = await context.newPage()
  await loginThroughUi(page, username)
  return { context, page }
}

async function selfRegisterContestBot(
  page: Page,
  contestId: number,
  botLabel: string,
): Promise<Record<string, unknown>> {
  await page.goto(`/#/contests/${contestId}`)
  const actionRegion = page.getByRole('heading', { name: '赛事操作', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  const selector = actionRegion.getByRole('combobox')
  await expect(selector).toBeVisible()
  await selector.click()
  await page.getByRole('option', { name: botLabel, exact: true }).click()

  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === 'POST' &&
      new URL(response.url()).pathname === `/api/contests/${contestId}/register`,
  )
  await actionRegion.getByRole('button', { name: '报名派遣', exact: true }).click()
  const response = await responsePromise
  expect(response.status(), await response.text()).toBe(200)
  const entry = (await response.json() as { entry: Record<string, unknown> }).entry

  await expect(actionRegion.getByRole('button', { name: '报名派遣', exact: true })).toHaveCount(0)
  await expect(actionRegion.getByRole('button', { name: '确认更换', exact: true })).toBeVisible()
  const rosterRegion = page.getByRole('heading', { name: /报名选手/ })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(rosterRegion).toContainText(PLAYER)
  return entry
}

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  expect((await response.json() as { qa_instance?: boolean }).qa_instance).toBe(true)
})

test('organizer downloads stable identity exports while non-organizers and non-identity events stay private', async ({
  page,
  browser,
  baseURL,
  browserName,
}) => {
  test.setTimeout(120_000)
  expect(baseURL).toBeTruthy()
  let playerContext: BrowserContext | null = null
  let playerPage: Page | null = null
  let organizerContext: BrowserContext | null = null
  let originalProfile: ProfileSnapshot | null = null
  const organizerContestIds: number[] = []
  const adminContestIds: number[] = []

  await withCleanup(async () => {
    const player = await loggedInChild(browser, baseURL!, PLAYER)
    playerContext = player.context
    playerPage = player.page
    const me = await player.page.request.get('/api/auth/me')
    await expectStatus(me)
    const playerUser = (await me.json() as { user: ProfileSnapshot & { id: number } }).user
    originalProfile = playerUser

    const formulaProfile = await player.page.request.put('/api/auth/profile', {
      data: {
        display_name: '+报名时显示名',
        bio: playerUser.bio || '',
        real_name: '=2+3',
        phone: '13800000001',
        school: '@公式学校',
        student_id: '001234',
      },
    })
    await expectStatus(formulaProfile)
    // APIRequestContext shares authentication cookies but cannot update the
    // already-mounted AuthProvider. Reload so the real registration button is
    // gated by the just-saved identity profile in every browser engine.
    await player.page.reload()
    await expect(player.page.getByText('+报名时显示名', { exact: true })).toBeVisible()

    const mine = await player.page.request.get('/api/bots/mine?game_id=holdem&page=1&per_page=100')
    await expectStatus(mine)
    const bot = (await mine.json() as {
      bots: Array<{ id: number; name: string; display_name?: string | null }>
    }).bots.find((item) => item.name === `${PLAYER}_holdem`)
    expect(bot, `${PLAYER} needs its seeded Holdem Bot`).toBeTruthy()

    await loginThroughUi(page, ADMIN)
    const adminMonitor = monitorBrowser(page)
    const playerMonitor = monitorBrowser(player.page)
    const organizer = await loggedInChild(browser, baseURL!, ORGANIZER)
    organizerContext = organizer.context
    for (const requireRealName of [true, false]) {
      const create = await organizer.page.request.post('/api/contests', {
        data: {
          title: `PW 组织者报名门禁 ${browserName} ${requireRealName ? '实名赛' : '非实名赛'} ${Date.now()}`,
          game_id: 'holdem',
          template_id: 'holdem_rr',
          require_real_name: requireRealName,
        },
      })
      await expectStatus(create)
      organizerContestIds.push(
        (await create.json() as { contest: { id: number } }).contest.id,
      )
    }

    const [identityContestId, publicContestId] = organizerContestIds
    const identityPath = `/api/contests/${identityContestId}/export`
    const exportSearch = '?format=csv&schema=2'
    const open = await organizer.page.request.post(`/api/contests/${identityContestId}/open`)
    await expectStatus(open)

    const organizerMonitor = monitorBrowser(organizer.page)
    await organizer.page.goto(`/#/contests/${identityContestId}`)
    const restrictedRoster = organizer.page.getByRole('heading', { name: /报名选手/ })
      .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
    await expect(restrictedRoster).toContainText('实名赛事仅允许选手本人报名')
    await expect(restrictedRoster).toContainText('组织者不能代报名或批量指派')
    await expect(restrictedRoster.getByRole('button', { name: '批量指派', exact: true })).toHaveCount(0)

    const deniedSingle = await browserPost(
      organizer.page,
      `/api/contests/${identityContestId}/entries`,
      { user_id: playerUser.id, bot_id: bot!.id },
    )
    expect(deniedSingle.status).toBe(403)
    expect((JSON.parse(deniedSingle.body) as { detail?: string }).detail).toBe(
      SELF_REGISTER_ONLY_DETAIL,
    )
    const deniedBulk = await browserPost(
      organizer.page,
      `/api/contests/${identityContestId}/entries/bulk`,
      { assign_all: true, game_id: 'holdem' },
    )
    expect(deniedBulk.status).toBe(403)
    expect((JSON.parse(deniedBulk.body) as { detail?: string }).detail).toBe(
      SELF_REGISTER_ONLY_DETAIL,
    )

    const restrictedDetail = await browserFetch(
      organizer.page,
      `/api/contests/${identityContestId}`,
    )
    expect(restrictedDetail.status).toBe(200)
    expectPrivateCacheHeaders(restrictedDetail)
    expect((JSON.parse(restrictedDetail.body) as { entries: unknown[] }).entries).toEqual([])
    const restrictedExport = await browserFetch(
      organizer.page,
      `${identityPath}${exportSearch}`,
    )
    expect(restrictedExport.status).toBe(200)
    expectPrivateHeaders(restrictedExport, identityContestId)
    expect(parseCsv(restrictedExport.body).rows).toEqual([])

    const selfEntry = await selfRegisterContestBot(
      player.page,
      identityContestId,
      bot!.display_name || bot!.name,
    )
    expect(selfEntry.user_id).toBe(playerUser.id)
    expect(selfEntry.bot_id).toBe(bot!.id)

    const detailAtRegistration = await browserFetch(
      organizer.page,
      `/api/contests/${identityContestId}`,
    )
    expect(detailAtRegistration.status).toBe(200)
    expectPrivateCacheHeaders(detailAtRegistration)
    const entryAtRegistration = (
      JSON.parse(detailAtRegistration.body) as { entries: Array<Record<string, unknown>> }
    ).entries.find((entry) => entry.user_id === playerUser.id)
    expect(entryAtRegistration).toBeTruthy()
    expect(entryAtRegistration).toMatchObject({
      id: selfEntry.id,
      bot_id: bot!.id,
      real_name: '=2+3',
      phone: '13800000001',
      school: '@公式学校',
      student_id: '001234',
      identity_source: 'registration_profile',
      identity_complete: 1,
    })
    const identityCapturedAt = String(entryAtRegistration!.identity_captured_at || '')
    expect(identityCapturedAt).not.toBe('')
    expect(identityCapturedAt).toBe(String(entryAtRegistration!.registered_at))

    const exportAtRegistration = await browserFetch(
      organizer.page,
      `${identityPath}${exportSearch}`,
    )
    expect(exportAtRegistration.status).toBe(200)
    expectPrivateHeaders(exportAtRegistration, identityContestId)
    const csvAtRegistration = parseCsv(exportAtRegistration.body)
    expect(csvAtRegistration.headers).toEqual(PRIVATE_HEADERS)
    expect(csvAtRegistration.rows).toHaveLength(1)
    expect(csvAtRegistration.rows[0]).toMatchObject({
      '报名ID(entry_id)': String(selfEntry.id),
      '用户ID(user_id)': String(playerUser.id),
      '用户显示名(user_display)': "'+报名时显示名",
      '实名姓名(real_name)': "'=2+3",
      '手机号(phone)': "'13800000001",
      '学校(school)': "'@公式学校",
      '学号(student_id)': "'001234",
      '实名来源(identity_source)': '报名时资料快照',
      '实名采集时间(identity_captured_at)': identityCapturedAt,
      '实名完整性(identity_completeness)': '完整',
    })

    const changed = await player.page.request.put('/api/auth/profile', {
      data: {
        display_name: '报名后显示名',
        bio: playerUser.bio || '',
        real_name: '报名后姓名',
        phone: '13900000002',
        school: '报名后学校',
        student_id: '990001',
      },
    })
    await expectStatus(changed)

    const detailAfterProfileChange = await browserFetch(
      organizer.page,
      `/api/contests/${identityContestId}`,
    )
    expect(detailAfterProfileChange.status).toBe(200)
    expectPrivateCacheHeaders(detailAfterProfileChange)
    const entryAfterProfileChange = (
      JSON.parse(detailAfterProfileChange.body) as { entries: Array<Record<string, unknown>> }
    ).entries.find((entry) => entry.user_id === playerUser.id)
    expect(entryAfterProfileChange).toMatchObject({
      id: selfEntry.id,
      owner_display: '报名后显示名',
      real_name: '=2+3',
      phone: '13800000001',
      school: '@公式学校',
      student_id: '001234',
      identity_source: 'registration_profile',
      identity_captured_at: identityCapturedAt,
      identity_complete: 1,
    })

    // The organizer is already on this hash route. A same-URL goto is a
    // same-document no-op in Firefox, so explicitly reload the live roster.
    await organizer.page.reload()
    const rosterRegion = organizer.page.getByRole('heading', { name: /报名选手/ })
      .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
    await expect(rosterRegion).toContainText('报名 ID、用户 ID 与 Bot ID')
    await expect(rosterRegion).toContainText('历史报名若无快照会明确标注当前资料回退')
    await expect(rosterRegion).toContainText('组织者不能代报名或批量指派')
    await expect(rosterRegion.getByRole('button', { name: '批量指派', exact: true })).toHaveCount(0)
    await expect(rosterRegion.getByText('报名时资料快照', { exact: true })).toBeVisible()
    const rosterDownload = rosterRegion.getByRole('link', { name: '导出实名报名名单', exact: true })
    await expect(rosterDownload).toHaveAttribute(
      'href',
      `/api/contests/${identityContestId}/export?format=csv&schema=2`,
    )
    await expect(rosterDownload).toHaveAttribute('download', '')
    await expectTouchSafeWithoutRootOverflow(organizer.page, rosterDownload)

    const privateFetch = await browserFetch(organizer.page, `${identityPath}${exportSearch}`)
    expect(privateFetch.status).toBe(200)
    expectPrivateHeaders(privateFetch, identityContestId)
    const privateCsv = parseCsv(privateFetch.body)
    expect(privateCsv.headers).toEqual(PRIVATE_HEADERS)
    expect(privateCsv.rows).toHaveLength(1)
    const privateRow = privateCsv.rows[0]
    expect(privateRow['报名ID(entry_id)']).toMatch(/^\d+$/)
    expect(privateRow['用户ID(user_id)']).toBe(String(playerUser.id))
    expect(privateRow['用户账号(username)']).toBe(PLAYER)
    expect(privateRow['用户显示名(user_display)']).toBe('报名后显示名')
    expect(privateRow['Bot ID(bot_id)']).toBe(String(bot!.id))
    expect(privateRow['Bot内部名(bot_name)']).toBe(bot!.name)
    expect(privateRow['Bot显示名(bot_display)']).toBe(bot!.display_name || bot!.name)
    expect(privateRow['实名姓名(real_name)']).toBe("'=2+3")
    expect(privateRow['手机号(phone)']).toBe("'13800000001")
    expect(privateRow['学校(school)']).toBe("'@公式学校")
    expect(privateRow['学号(student_id)']).toBe("'001234")
    expect(privateRow['实名来源(identity_source)']).toBe('报名时资料快照')
    expect(privateRow['实名完整性(identity_completeness)']).toBe('完整')
    expect(privateRow['实名采集时间(identity_captured_at)']).toBe(identityCapturedAt)
    for (const key of [
      '用户账号(username)',
      '用户显示名(user_display)',
      'Bot内部名(bot_name)',
      'Bot显示名(bot_display)',
      '实名姓名(real_name)',
      '手机号(phone)',
      '学校(school)',
      '学号(student_id)',
      '奖项(awarded)',
    ]) {
      expect(privateRow[key]).not.toMatch(/^[=+\-@]/)
    }

    const rosterDownloadPromise = organizer.page.waitForEvent('download')
    await rosterDownload.click()
    const downloadedRoster = await rosterDownloadPromise
    expect(downloadedRoster.suggestedFilename()).toBe(
      `contest-${identityContestId}-participants-v2.csv`,
    )
    expect(parseCsv(await readDownloadText(downloadedRoster))).toEqual(privateCsv)

    const actualDetailResponse = await organizer.page.request.get(`/api/contests/${identityContestId}`)
    await expectStatus(actualDetailResponse)
    const actualDetail = await actualDetailResponse.json() as Record<string, unknown> & {
      contest: Record<string, unknown>
      entries: Array<Record<string, unknown>>
    }
    const finishedDetailPattern = new RegExp(`/api/contests/${identityContestId}(?:\\?.*)?$`)
    await organizer.page.route(
      finishedDetailPattern,
      async (route) => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...actualDetail,
          contest: {
            ...actualDetail.contest,
            status: 'finished',
            official_results_ready: 1,
          },
        }),
      }),
    )
    await organizer.page.route(`**/api/contests/${identityContestId}/official-results`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"results":[]}' })
    })
    await organizer.page.reload()
    await expect(organizer.page.getByText(/公开成绩 CSV 永不包含报名时实名资料/)).toBeVisible()
    const publicResults = organizer.page.getByRole('link', { name: '导出公开成绩 CSV', exact: true })
    await expect(publicResults).toHaveAttribute(
      'href',
      `/api/contests/${identityContestId}/official-results?format=csv`,
    )
    const organizerResults = organizer.page.getByRole('link', {
      name: '导出组织者成绩明细（含实名报名资料）',
      exact: true,
    })
    await expect(organizerResults).toHaveAttribute(
      'href',
      `/api/contests/${identityContestId}/export?format=csv&schema=2`,
    )
    await expectTouchSafeWithoutRootOverflow(organizer.page, organizerResults)
    await focusByKeyboard(organizer.page, organizerResults)
    await expect(organizerResults).toBeFocused()
    const resultsDownloadPromise = organizer.page.waitForEvent('download')
    await organizer.page.keyboard.press('Enter')
    const downloadedResults = await resultsDownloadPromise
    expect(downloadedResults.suggestedFilename()).toBe(
      `contest-${identityContestId}-participants-v2.csv`,
    )
    expect(parseCsv(await readDownloadText(downloadedResults))).toEqual(privateCsv)

    await organizer.page.unroute(finishedDetailPattern)
    await organizer.page.unroute(`**/api/contests/${identityContestId}/official-results`)
    await organizer.page.goto(`/#/contests/${publicContestId}`)
    const publicRosterRegion = organizer.page.getByRole('heading', { name: /报名选手/ })
      .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
    await expect(publicRosterRegion).not.toContainText('实名')
    await expect(
      publicRosterRegion.getByRole('button', { name: '批量指派', exact: true }),
    ).toBeVisible()
    const allowedSingle = await browserPost(
      organizer.page,
      `/api/contests/${publicContestId}/entries`,
      { user_id: playerUser.id, bot_id: bot!.id },
    )
    expect(allowedSingle.status).toBe(200)
    const publicDetail = await browserFetch(
      organizer.page,
      `/api/contests/${publicContestId}`,
    )
    expect(publicDetail.status).toBe(200)
    const publicEntries = (
      JSON.parse(publicDetail.body) as { entries: Array<Record<string, unknown>> }
    ).entries
    expect(publicEntries).toHaveLength(1)
    expect(publicEntries[0]).not.toHaveProperty('real_name')
    expect(publicEntries[0]).not.toHaveProperty('identity_source')
    const publicRosterDownload = publicRosterRegion.getByRole('link', { name: '导出报名名单', exact: true })
    await expect(publicRosterDownload).toHaveAttribute(
      'href',
      `/api/contests/${publicContestId}/export?format=csv&schema=2`,
    )
    const publicRosterFetch = await browserFetch(
      organizer.page,
      `/api/contests/${publicContestId}/export?format=csv&schema=2`,
    )
    expect(publicRosterFetch.status).toBe(200)
    expectPrivateHeaders(publicRosterFetch, publicContestId)
    const publicRosterCsv = parseCsv(publicRosterFetch.body)
    expect(publicRosterCsv.headers).toEqual(PRIVATE_HEADERS)
    expect(publicRosterCsv.rows).toHaveLength(1)
    for (const key of [
      '实名姓名(real_name)',
      '手机号(phone)',
      '学校(school)',
      '学号(student_id)',
      '实名来源(identity_source)',
      '实名采集时间(identity_captured_at)',
      '实名完整性(identity_completeness)',
    ]) {
      expect(publicRosterCsv.rows[0][key], `${key} must be empty for a public-only event`).toBe('')
    }
    await expectTouchSafeWithoutRootOverflow(organizer.page, publicRosterDownload)

    const createAdminOverride = await page.request.post('/api/contests', {
      data: {
        title: `PW 管理员审计代报名 ${browserName} ${Date.now()}`,
        game_id: 'holdem',
        template_id: 'holdem_rr',
        require_real_name: true,
      },
    })
    await expectStatus(createAdminOverride)
    const adminOverrideContestId = (
      await createAdminOverride.json() as { contest: { id: number } }
    ).contest.id
    adminContestIds.push(adminOverrideContestId)
    const adminOverride = await page.request.post(
      `/api/contests/${adminOverrideContestId}/entries`,
      { data: { user_id: playerUser.id, bot_id: bot!.id } },
    )
    await expectStatus(adminOverride)
    expect(await adminOverride.json()).toEqual({ ok: true })

    const adminOverrideDetail = await browserFetch(
      page,
      `/api/contests/${adminOverrideContestId}`,
    )
    expect(adminOverrideDetail.status).toBe(200)
    expectPrivateCacheHeaders(adminOverrideDetail)
    const adminOverrideNamedEntry = (
      JSON.parse(adminOverrideDetail.body) as { entries: Array<Record<string, unknown>> }
    ).entries.find((entry) => entry.user_id === playerUser.id)
    expect(adminOverrideNamedEntry).toMatchObject({
      user_id: playerUser.id,
      bot_id: bot!.id,
      real_name: '报名后姓名',
      phone: '13900000002',
      school: '报名后学校',
      student_id: '990001',
      identity_source: 'registration_profile',
      identity_complete: 1,
    })
    expect(adminOverrideNamedEntry?.id).not.toBe(selfEntry.id)
    expect(adminOverrideNamedEntry?.identity_captured_at).toBeTruthy()

    await page.goto(`/#/contests/${adminOverrideContestId}`)
    const adminRosterRegion = page.getByRole('heading', { name: /报名选手/ })
      .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
    await expect(adminRosterRegion).toContainText('管理员代报名会写入审计')
    await expect(
      adminRosterRegion.getByRole('button', { name: '批量指派', exact: true }),
    ).toBeVisible()

    await player.page.goto(`/#/contests/${identityContestId}`)
    await expect(player.page.getByRole('link', { name: /导出.*实名/ })).toHaveCount(0)
    const denied = await browserFetch(
      player.page,
      `/api/contests/${identityContestId}/export?format=csv&schema=2`,
    )
    expect(denied.status).toBe(403)
    expectPrivateCacheHeaders(denied)
    expect(denied.body).not.toContain('=2+3')
    expect(denied.body).not.toContain('13800000001')
    await playerMonitor.expectClean([
      {
        kind: 'http',
        method: 'GET',
        status: 403,
        pathname: identityPath,
        search: exportSearch,
      },
      ...optionalFirefoxAdvisory(browserName),
    ])
    await organizerMonitor.expectClean([
      {
        kind: 'http',
        method: 'POST',
        status: 403,
        pathname: `/api/contests/${identityContestId}/entries`,
      },
      {
        kind: 'http',
        method: 'POST',
        status: 403,
        pathname: `/api/contests/${identityContestId}/entries/bulk`,
      },
      ...optionalDownloadAborts(identityPath, exportSearch),
      ...optionalDownloadAborts(identityPath, exportSearch),
      ...optionalFirefoxAdvisory(browserName),
    ])
    await adminMonitor.expectClean(optionalFirefoxAdvisory(browserName))
  }, async () => {
    const tasks: Array<{ label: string; run: () => Promise<void> }> = []
    if (playerPage && originalProfile) {
      const restorePage = playerPage
      const restoreProfile = originalProfile
      tasks.push({
        label: `restore ${PLAYER} profile`,
        run: async () => {
          const response = await restorePage.request.put('/api/auth/profile', {
            data: {
              display_name: restoreProfile.display_name || '',
              bio: restoreProfile.bio || '',
              real_name: restoreProfile.real_name || '',
              phone: restoreProfile.phone || '',
              school: restoreProfile.school || '',
              student_id: restoreProfile.student_id || '',
            },
          })
          await expectStatus(response)
        },
      })
    }
    for (const contestId of [...adminContestIds, ...organizerContestIds].reverse()) {
      tasks.push({
        label: `delete contest ${contestId}`,
        run: async () => {
          const response = await page.request.delete(`/api/admin/contests/${contestId}`)
          await expectStatus(response)
        },
      })
    }
    if (playerContext) {
      const context = playerContext
      tasks.push({
        label: 'close player context',
        run: async () => context.close(),
      })
    }
    if (organizerContext) {
      const context = organizerContext
      tasks.push({
        label: 'close organizer context',
        run: async () => context.close(),
      })
    }
    await runCleanupTasks(tasks)
  })
})

test('public official results download stays public-only and works in the real browser', async ({
  page,
  browser,
  baseURL,
  browserName,
}) => {
  test.setTimeout(90_000)
  expect(baseURL).toBeTruthy()
  const admin = await loggedInChild(browser, baseURL!, ADMIN)
  let contestId: number | null = null
  try {
    const response = await admin.page.request.get('/api/admin/contests?status=finished&page=1&per_page=200')
    await expectStatus(response)
    const finished = (await response.json() as {
      contests: Array<{ id: number; official_results_ready?: number }>
    }).contests.find((contest) => contest.official_results_ready === 1)
    expect(finished, 'isolated fresh QA DB needs one finished contest with official results').toBeTruthy()
    contestId = finished!.id
  } finally {
    await admin.context.close()
  }
  expect(contestId).not.toBeNull()
  const targetContestId = contestId!

  const monitor = monitorBrowser(page)
  await page.goto(`/#/contests/${targetContestId}`)
  const publicDownload = page.getByRole('link', { name: '导出公开成绩 CSV', exact: true })
  await expect(publicDownload).toHaveAttribute(
    'href',
    `/api/contests/${targetContestId}/official-results?format=csv`,
  )
  await expect(page.getByRole('link', { name: /导出组织者成绩明细/ })).toHaveCount(0)
  await expectTouchSafeWithoutRootOverflow(page, publicDownload)

  const publicFetch = await browserFetch(
    page,
    `/api/contests/${targetContestId}/official-results?format=csv`,
  )
  expect(publicFetch.status).toBe(200)
  expect(publicFetch.headers['content-type']).toContain('text/csv')
  expect(publicFetch.headers['content-disposition']).toBe(
    `attachment; filename="contest-${targetContestId}-results.csv"`,
  )
  expect(publicFetch.headers['x-content-type-options']).toBe('nosniff')
  const publicCsv = parseCsv(publicFetch.body)
  expect(publicCsv.headers).toEqual(PUBLIC_HEADERS)
  for (const pii of [
    'real_name',
    'phone',
    'school',
    'student_id',
    'identity_source',
    'identity_captured_at',
  ]) {
    expect(publicCsv.headers).not.toContain(pii)
  }

  const downloadPromise = page.waitForEvent('download')
  await publicDownload.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe(`contest-${targetContestId}-results.csv`)
  expect(parseCsv(await readDownloadText(download))).toEqual(publicCsv)
  await monitor.expectClean(optionalDownloadAborts(
    `/api/contests/${targetContestId}/official-results`,
    '?format=csv',
  ).concat(optionalFirefoxAdvisory(browserName)))
})
