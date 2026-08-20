import {
  expect,
  test,
  type Download,
  type Page,
} from '@playwright/test'

import { monitorBrowser } from './helpers'

type GameId = 'holdem' | 'gomoku' | 'pencil'
type TerminalStatus = 'completed' | 'aborted'

type MatchFixture = {
  id: string
  gameId: GameId
  status: TerminalStatus | 'running'
  winner: 0 | 1 | null
  reason: string
  events: Array<Record<string, unknown>>
}

const UPDATED_AT = '2026-08-20T12:00:00+08:00'

const terminalFixtures: MatchFixture[] = [
  {
    id: 'match-log-holdem-completed',
    gameId: 'holdem',
    status: 'completed',
    winner: 0,
    reason: 'completed',
    events: [
      { type: 'match_start', game_id: 'holdem', num_hands: 70 },
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19_950, 19_900] },
      { type: 'settle', hand: 0, winners: [0], deltas: [100, -100], pot: 200, reason: 'fold' },
      { type: 'match_end', winner: 0, reason: 'completed', deltas: [100, -100] },
    ],
  },
  {
    id: 'match-log-gomoku-completed',
    gameId: 'gomoku',
    status: 'completed',
    winner: 0,
    reason: 'five',
    events: [
      {
        type: 'match_start', game_id: 'gomoku', size: 15,
        ruleset: 'gomoku_ccgc_2013_v1', protocol_version: 2,
      },
      {
        type: 'opening', player: 0, opening_code: 'D1', n: 2,
        black1: { x: 7, y: 7 }, white2: { x: 7, y: 8 }, black3: { x: 8, y: 8 },
      },
      { type: 'match_end', winner: 0, reason: 'five', deltas: [1, -1] },
    ],
  },
  {
    id: 'match-log-pencil-aborted',
    gameId: 'pencil',
    status: 'aborted',
    winner: null,
    reason: 'platform_error',
    events: [
      { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11 },
      { type: 'error', reason: 'platform_error' },
    ],
  },
]

function matchDetail(fixture: MatchFixture) {
  return {
    id: fixture.id,
    game_id: fixture.gameId,
    status: fixture.status,
    match_type: 'challenge',
    winner: fixture.winner,
    reason: fixture.reason,
    bot_a: { name: `${fixture.gameId}_alpha`, owner_name: 'alpha' },
    bot_b: { name: `${fixture.gameId}_beta`, owner_name: 'beta' },
    result: {
      rounds_played: Math.max(0, fixture.events.length - 2),
      deltas: fixture.winner === 0 ? [1, -1] : fixture.winner === 1 ? [-1, 1] : [0, 0],
      normalized_delta: fixture.winner === null ? 0 : 1,
    },
  }
}

function replayPayload(fixture: MatchFixture) {
  return {
    match_id: fixture.id,
    events: fixture.events,
    event_count: fixture.events.length,
    updated_at: UPDATED_AT,
  }
}

function logPayload(fixture: MatchFixture) {
  return {
    format: 'botbattle.match.log',
    format_version: 1,
    match: matchDetail(fixture),
    replay: replayPayload(fixture),
  }
}

function optionalGetAborts(pathname: string) {
  return [
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname,
      errorText: 'net::ERR_ABORTED',
      optional: true,
    },
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname,
      errorText: 'Load request cancelled',
      optional: true,
    },
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname,
      errorText: 'NS_BINDING_ABORTED',
      optional: true,
    },
  ]
}

function captureExactGetCancellations(page: Page, pathname: string, maxCount = 3) {
  const errors: string[] = []
  page.on('requestfailed', (request) => {
    if (request.method() === 'GET' && new URL(request.url()).pathname === pathname) {
      errors.push(request.failure()?.errorText || '')
    }
  })
  return () => {
    expect(errors.length, `${pathname} cancellation count`).toBeLessThanOrEqual(maxCount)
    for (const errorText of errors) {
      expect(errorText).toMatch(/(?:ERR_ABORTED|NS_BINDING_ABORTED|load request cancelled)/i)
    }
    return errors.map((errorText) => ({
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname,
      errorText,
    }))
  }
}

async function mockAnonymousPage(page: Page) {
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: '{"detail":"未登录"}',
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"comments":[],"count":0,"total":0}',
    })
  })
}

async function mockMatch(
  page: Page,
  fixture: MatchFixture,
  counters: { log: number; record: number },
) {
  const detail = matchDetail(fixture)
  const replay = replayPayload(fixture)
  await page.route(`**/api/matches/${fixture.id}/log`, async (route) => {
    counters.log += 1
    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Content-Disposition': `attachment; filename="botbattle-${fixture.gameId}-${fixture.id}-log.json"`,
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
      },
      body: JSON.stringify(logPayload(fixture)),
    })
  })
  await page.route(`**/api/matches/${fixture.id}/record`, async (route) => {
    counters.record += 1
    await route.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Content-Disposition': `attachment; filename="botbattle-gomoku-${fixture.id}.json"`,
      },
      body: JSON.stringify({ format: 'botbattle.gomoku.record', format_version: 1 }),
    })
  })
  await page.route(`**/api/matches/${fixture.id}/replay`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(replay) })
  })
  await page.route(`**/api/matches/${fixture.id}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${fixture.id}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match: detail }),
    })
  })
}

async function readDownloadJson(download: Download) {
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream) chunks.push(Buffer.from(chunk))
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown
}

for (const fixture of terminalFixtures) {
  test(`terminal ${fixture.gameId} exposes its public match log`, async ({ page, browserName }) => {
    await mockAnonymousPage(page)
    const counters = { log: 0, record: 0 }
    await mockMatch(page, fixture, counters)
    const monitor = monitorBrowser(page)

    await page.goto(`/#/match/${fixture.id}`)
    const logLink = page.getByRole('link', { name: '导出对局日志（JSON）', exact: true })
    await expect(logLink).toBeVisible()
    await expect(logLink).toHaveAttribute('href', `/api/matches/${fixture.id}/log`)
    await expect(logLink).toHaveAttribute('download', '')
    expect(counters.log).toBe(0)

    const recordLink = page.getByRole('link', { name: '导出棋谱（JSON）', exact: true })
    if (fixture.gameId === 'gomoku') {
      await expect(recordLink).toBeVisible()
      await expect(recordLink).toHaveAttribute('href', `/api/matches/${fixture.id}/record`)
      await expect(recordLink).toHaveAttribute('download', '')
    } else {
      await expect(recordLink).toHaveCount(0)
    }
    expect(counters.record).toBe(0)

    await logLink.evaluate((element) => element.removeAttribute('download'))
    const expected = logPayload(fixture)
    if (browserName === 'webkit') {
      const responsePromise = page.waitForResponse((response) => (
        response.request().method() === 'GET'
        && new URL(response.url()).pathname === `/api/matches/${fixture.id}/log`
      ))
      await logLink.click()
      const response = await responsePromise
      expect(response.headers()['content-disposition']).toBe(
        `attachment; filename="botbattle-${fixture.gameId}-${fixture.id}-log.json"`,
      )
      expect(await response.json()).toEqual(expected)
    } else {
      const downloadPromise = page.waitForEvent('download')
      await logLink.click()
      const download = await downloadPromise
      expect(download.suggestedFilename()).toBe(
        `botbattle-${fixture.gameId}-${fixture.id}-log.json`,
      )
      expect(await readDownloadJson(download)).toEqual(expected)
    }
    expect(counters.log).toBe(1)

    await monitor.expectClean([
      ...optionalGetAborts(`/api/matches/${fixture.id}`),
      ...optionalGetAborts(`/api/matches/${fixture.id}/log`),
    ])
  })
}

test('live match hides partial exports and a stale terminal response cannot restore them', async ({ page }) => {
  await mockAnonymousPage(page)
  const terminal = terminalFixtures[1]
  const live: MatchFixture = {
    id: 'match-log-gomoku-live',
    gameId: 'gomoku',
    status: 'running',
    winner: null,
    reason: '',
    events: [
      {
        type: 'match_start', game_id: 'gomoku', size: 15,
        ruleset: 'gomoku_ccgc_2013_v1', protocol_version: 2,
      },
    ],
  }
  const counters = { log: 0, record: 0 }
  let releaseTerminal!: () => void
  let observeTerminal!: () => void
  const terminalGate = new Promise<void>((resolve) => { releaseTerminal = resolve })
  const terminalObserved = new Promise<void>((resolve) => { observeTerminal = resolve })

  await page.route(`**/api/matches/${terminal.id}/log`, async (route) => {
    counters.log += 1
    await route.fulfill({ status: 500, body: 'stale log link must never be requested' })
  })
  await page.route(`**/api/matches/${terminal.id}/record`, async (route) => {
    counters.record += 1
    await route.fulfill({ status: 500, body: 'stale record link must never be requested' })
  })
  await page.route(`**/api/matches/${terminal.id}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${terminal.id}`, async (route) => {
    observeTerminal()
    await terminalGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match: matchDetail(terminal) }),
    })
  })

  await page.route(`**/api/matches/${live.id}/log`, async (route) => {
    counters.log += 1
    await route.fulfill({ status: 409, body: 'live log must not be requested' })
  })
  await page.route(`**/api/matches/${live.id}/record`, async (route) => {
    counters.record += 1
    await route.fulfill({ status: 409, body: 'live record must not be requested' })
  })
  await page.route(`**/api/matches/${live.id}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${live.id}/events`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `data: ${JSON.stringify({ type: 'snapshot', match: matchDetail(live), events: live.events })}\n\n`,
    })
  })
  await page.route(`**/api/matches/${live.id}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match: matchDetail(live) }),
    })
  })

  const expectedTerminalCancellations = captureExactGetCancellations(
    page,
    `/api/matches/${terminal.id}`,
  )
  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${terminal.id}`)
  await terminalObserved
  await page.goto(`/#/match/${live.id}`)
  await expect(page.getByRole('heading', { name: '实时观赛', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '导出对局日志（JSON）', exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: '导出棋谱（JSON）', exact: true })).toHaveCount(0)

  releaseTerminal()
  await page.waitForTimeout(150)
  await expect(page.getByRole('heading', { name: '实时观赛', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '导出对局日志（JSON）', exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: '导出棋谱（JSON）', exact: true })).toHaveCount(0)
  expect(counters).toEqual({ log: 0, record: 0 })

  await monitor.expectClean(expectedTerminalCancellations())
})

test('unknown games fail closed without probing match-log capability', async ({ page }) => {
  await mockAnonymousPage(page)
  const id = 'match-log-unknown-game'
  let logRequests = 0
  let replayRequests = 0
  await page.route(`**/api/matches/${id}/log`, async (route) => {
    logRequests += 1
    await route.fulfill({ status: 500, body: 'unknown game log must not be requested' })
  })
  await page.route(`**/api/matches/${id}/replay`, async (route) => {
    replayRequests += 1
    await route.fulfill({ status: 500, body: 'unknown game replay must not be requested' })
  })
  await page.route(`**/api/matches/${id}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${id}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id,
          game_id: 'future-game',
          status: 'completed',
          match_type: 'challenge',
          winner: 0,
          reason: 'completed',
          bot_a: { name: 'future_alpha', owner_name: 'alpha' },
          bot_b: { name: 'future_beta', owner_name: 'beta' },
          result: { rounds_played: 1, deltas: [1, -1], normalized_delta: 1 },
        },
      }),
    })
  })

  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${id}`)
  await expect(page.getByText('不支持的游戏（future-game）', { exact: false }).first()).toBeVisible()
  await expect(page.getByRole('link', { name: '导出对局日志（JSON）', exact: true })).toHaveCount(0)
  expect({ logRequests, replayRequests }).toEqual({ logRequests: 0, replayRequests: 0 })
  await monitor.expectClean(optionalGetAborts(`/api/matches/${id}`))
})

test('Gomoku export actions are touch-safe, keyboard ordered, and download the exact log contract', async ({ page, browserName }) => {
  await mockAnonymousPage(page)
  const fixture = terminalFixtures[1]
  const counters = { log: 0, record: 0 }
  await mockMatch(page, fixture, counters)
  const monitor = monitorBrowser(page)

  await page.goto(`/#/match/${fixture.id}`)
  const logLink = page.getByRole('link', { name: '导出对局日志（JSON）', exact: true })
  const recordLink = page.getByRole('link', { name: '导出棋谱（JSON）', exact: true })
  await expect(logLink).toBeVisible()
  await expect(recordLink).toBeVisible()

  for (const viewport of [
    { width: 390, height: 844 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewportSize(viewport)
    const logBox = await logLink.boundingBox()
    const recordBox = await recordLink.boundingBox()
    expect(logBox).not.toBeNull()
    expect(recordBox).not.toBeNull()
    expect(logBox?.height ?? 0).toBeGreaterThanOrEqual(44)
    expect(recordBox?.height ?? 0).toBeGreaterThanOrEqual(44)
    if (logBox && recordBox) {
      const horizontalOverlap = Math.min(logBox.x + logBox.width, recordBox.x + recordBox.width)
        - Math.max(logBox.x, recordBox.x)
      const gap = horizontalOverlap > 0
        ? Math.max(recordBox.y - (logBox.y + logBox.height), logBox.y - (recordBox.y + recordBox.height))
        : Math.max(recordBox.x - (logBox.x + logBox.width), logBox.x - (recordBox.x + recordBox.width))
      expect(gap).toBeGreaterThanOrEqual(7)
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${viewport.width}px export actions overflow`).toBeLessThanOrEqual(1)
  }

  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
  let reachedLog = false
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await page.keyboard.press('Tab')
    if (await logLink.evaluate((element) => document.activeElement === element)) {
      reachedLog = true
      break
    }
  }
  expect(reachedLog).toBe(true)
  await expect(logLink).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(recordLink).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(logLink).toBeFocused()

  // Keep the production boolean download attribute assertion above. Removing it
  // only for the mocked response lets Playwright observe the server-authoritative
  // attachment filename and body consistently, matching the existing record test.
  await logLink.evaluate((element) => element.removeAttribute('download'))
  const expected = logPayload(fixture)
  if (browserName === 'webkit') {
    const responsePromise = page.waitForResponse((response) => (
      response.request().method() === 'GET'
      && new URL(response.url()).pathname === `/api/matches/${fixture.id}/log`
    ))
    await page.keyboard.press('Enter')
    const response = await responsePromise
    expect(response.headers()['content-disposition']).toBe(
      `attachment; filename="botbattle-gomoku-${fixture.id}-log.json"`,
    )
    expect(await response.json()).toEqual(expected)
  } else {
    const downloadPromise = page.waitForEvent('download')
    await page.keyboard.press('Enter')
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe(`botbattle-gomoku-${fixture.id}-log.json`)
    expect(await readDownloadJson(download)).toEqual(expected)
  }
  expect(counters).toEqual({ log: 1, record: 0 })

  await monitor.expectClean([
    ...optionalGetAborts(`/api/matches/${fixture.id}`),
    ...optionalGetAborts(`/api/matches/${fixture.id}/log`),
  ])
})
