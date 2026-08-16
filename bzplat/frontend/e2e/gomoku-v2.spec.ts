import { expect, test, type Locator, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const RULESET = 'gomoku_ccgc_2013_v1'

type Point = { x: number; y: number }
type Fixture = {
  humanSeat: 0 | 1
  request: Record<string, unknown>
  events: Array<Record<string, unknown>>
  pending?: boolean
}

function emptyBoard() {
  return Array.from({ length: 15 }, () => Array(15).fill(-1) as number[])
}

function boardWith(stones: Array<Point & { color: 0 | 1 }>) {
  const board = emptyBoard()
  for (const stone of stones) board[stone.x][stone.y] = stone.color
  return board
}

function matchStart() {
  return {
    type: 'match_start', game_id: 'gomoku', size: 15,
    ruleset: RULESET, protocol_version: 2, time_budget_per_side: 900,
  }
}

function request(
  phase: string,
  me: 0 | 1,
  color: 0 | 1,
  seatColors: [0 | 1, 0 | 1],
  board: number[][],
  extra: Record<string, unknown> = {},
) {
  return {
    protocol_version: 2,
    ruleset: RULESET,
    phase,
    me,
    color,
    seat_colors: seatColors,
    board,
    pass_allowed: false,
    ...extra,
  }
}

const opening = {
  type: 'opening', player: 0, opening_code: 'D1', n: 2,
  black1: { x: 7, y: 7 }, white2: { x: 7, y: 8 }, black3: { x: 8, y: 8 },
}
const firstThree = [
  { x: 7, y: 7, color: 0 as const },
  { x: 7, y: 8, color: 1 as const },
  { x: 8, y: 8, color: 0 as const },
]

function optionalStrictModeReplayAbort(id: string) {
  return [
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname: `/api/matches/${id}`,
      errorText: 'net::ERR_ABORTED',
      optional: true,
    },
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname: `/api/matches/${id}`,
      errorText: 'Load request cancelled',
      optional: true,
    },
    {
      kind: 'requestfailed' as const,
      method: 'GET',
      pathname: `/api/matches/${id}`,
      errorText: 'NS_BINDING_ABORTED',
      optional: true,
    },
  ]
}

async function mockAnonymousAuth(page: Page) {
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"未登录"}' })
  })
}

async function clickGrid(canvas: Locator, point: Point) {
  // ResizeObserver updates the CSS box before React's canvas backing-store
  // effect can run. Wait for both coordinate spaces to agree so a route or
  // viewport change cannot shift a click by one grid line.
  await expect.poll(async () => canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement
    const rect = target.getBoundingClientRect()
    const dpr = window.devicePixelRatio || 1
    return Math.abs(target.width / dpr - rect.width) <= 1
      && Math.abs(target.height / dpr - rect.height) <= 1
  })).toBe(true)
  const metrics = await canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement
    const rect = target.getBoundingClientRect()
    const dpr = window.devicePixelRatio || 1
    return {
      cssWidth: rect.width,
      cssHeight: rect.height,
      width: target.width / dpr,
      height: target.height / dpr,
    }
  })
  const header = Math.max(48, metrics.width * 0.065)
  const gutter = Math.max(18, metrics.width * 0.035)
  const cell = Math.max(
    8,
    Math.min(
      (metrics.width - gutter * 2) / 14,
      (metrics.height - header - gutter) / 14,
    ),
  )
  const boardPx = cell * 14
  const ox = (metrics.width - boardPx) / 2
  const oy = header + Math.max(0, (metrics.height - header - gutter - boardPx) / 2)
  await canvas.click({
    position: {
      x: (ox + point.x * cell) / metrics.width * metrics.cssWidth,
      y: (oy + point.y * cell) / metrics.height * metrics.cssHeight,
    },
  })
}

async function mockReplay(
  page: Page,
  id: string,
  events: Array<Record<string, unknown>>,
  reason: string,
  winner: number | null,
) {
  await page.route(`**/api/matches/${id}/replay`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match_id: id, events, event_count: events.length }),
    })
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
          game_id: 'gomoku',
          status: 'completed',
          match_type: 'contest',
          winner,
          reason,
          bot_a: { name: 'opening_side', owner_name: 'alpha' },
          bot_b: { name: 'swap_side', owner_name: 'beta' },
          result: { rounds_played: 7, deltas: winner === 0 ? [1, -1] : [-1, 1] },
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
}

test('competition replay exposes opening, swap, N candidates, retained point and forbidden adjudication', async ({ page }, testInfo) => {
  await mockAnonymousAuth(page)
  const monitor = monitorBrowser(page)
  const id = 'gomoku-v2-competition-replay'
  const finalBoard = boardWith([
    ...firstThree,
    { x: 6, y: 7, color: 1 },
    { x: 6, y: 6, color: 0 },
    { x: 5, y: 7, color: 1 },
    { x: 5, y: 6, color: 0 },
  ])
  const events = [
    matchStart(),
    { type: 'turn', player: 0, color: 0, phase: 'opening_proposal' },
    opening,
    { type: 'turn', player: 1, color: 1, phase: 'swap_choice' },
    { type: 'swap', player: 1, swapped: true, seat_colors: [1, 0] },
    { type: 'turn', player: 0, color: 1, phase: 'white4' },
    { type: 'move', player: 0, color: 1, x: 6, y: 7, phase: 'white4', move_index: 4 },
    { type: 'turn', player: 1, color: 0, phase: 'black5_candidates' },
    {
      type: 'black5_candidates', player: 1, n: 2,
      points: [{ x: 6, y: 6 }, { x: 8, y: 6 }],
    },
    { type: 'turn', player: 0, color: 1, phase: 'black5_select' },
    { type: 'black5_selected', player: 0, index: 0, point: { x: 6, y: 6 } },
    { type: 'move', player: 1, color: 0, x: 6, y: 6, phase: 'black5_select', move_index: 5 },
    { type: 'turn', player: 0, color: 1, phase: 'normal_play', pass_allowed: true },
    { type: 'move', player: 0, color: 1, x: 5, y: 7, phase: 'normal_play', move_index: 6 },
    { type: 'time_used', seat: 0, used: 87.6, remaining: 812.4, budget: 900 },
    { type: 'turn', player: 1, color: 0, phase: 'normal_play', pass_allowed: true },
    { type: 'move', player: 1, color: 0, x: 5, y: 6, phase: 'normal_play', move_index: 7 },
    { type: 'time_used', seat: 1, used: 100.1, remaining: 799.9, budget: 900 },
    { type: 'forbidden', player: 1, color: 0, x: 5, y: 6, forbidden_kind: 'double_three' },
    {
      type: 'match_end', game_id: 'gomoku', ruleset: RULESET, protocol_version: 2,
      winner: 0, reason: 'forbidden_double_three', moves: 7, opening_code: 'D1', n: 2,
      seat_colors: [1, 0], board: finalBoard,
    },
  ]
  await mockReplay(page, id, events, 'forbidden_double_three', 0)

  await page.goto(`/#/match/${id}`)
  const starPoints = await page.evaluate(async () => {
    const module = await import('/src/games/gomoku/canvas.ts')
    return module.gomokuStarPoints(15)
  })
  expect(starPoints).toEqual([
    { x: 7, y: 7 },
    { x: 3, y: 3 },
    { x: 3, y: 11 },
    { x: 11, y: 11 },
    { x: 11, y: 3 },
  ])
  const jump = page.getByRole('button', { name: /直接查看最终结果/ })
  await expect(jump).toBeVisible()
  await jump.click()

  const summary = page.getByTestId('gomoku-replay-summary')
  await expect(summary).toContainText('全国竞赛规则')
  await expect(summary).toContainText('开局 D1 · 2 打')
  await expect(summary).toContainText('已交换棋色')
  await expect(summary).toContainText('座位 1 执白 · 座位 2 执黑')
  await expect(summary).toContainText('候选 2 点 · 保留 #1 (6, 6)')
  await expect(summary).toContainText('三三禁手 · (5, 6)')
  await expect(page.getByTestId('match-result-card')).toContainText('开局提案方 · 当前执白')
  await expect(page.getByTestId('match-result-card')).toContainText('交换决策方 · 当前执黑')
  await expect(page.getByTestId('gomoku-clock-seat-1')).toContainText('13:32')
  await expect(page.getByTestId('gomoku-clock-seat-2')).toContainText('13:19')
  await expect(page.getByTestId('terminal-reason')).toHaveAttribute('data-tone', 'danger')

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(summary).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  const canvasBox = await page.locator('canvas').boundingBox()
  expect(canvasBox).not.toBeNull()
  expect((canvasBox?.width ?? 0) / (canvasBox?.height ?? 1)).toBeCloseTo(1, 1)
  await page.screenshot({ path: testInfo.outputPath('gomoku-v2-mobile.png'), fullPage: true })
  await monitor.expectClean(optionalStrictModeReplayAbort(id))
})

test('legacy Gomoku replay remains readable without v2 fields', async ({ page }) => {
  await mockAnonymousAuth(page)
  const monitor = monitorBrowser(page)
  const id = 'gomoku-v2-legacy-replay'
  const events = [
    { type: 'match_start', game_id: 'gomoku', size: 15 },
    { type: 'turn', player: 0 },
    { type: 'move', player: 0, x: 7, y: 7, move_index: 1 },
    { type: 'turn', player: 1 },
    { type: 'move', player: 1, x: 7, y: 8, move_index: 2 },
    { type: 'match_end', winner: 0, reason: 'five' },
  ]
  await mockReplay(page, id, events, 'five', 0)

  await page.goto(`/#/match/${id}`)
  await expect(page.getByTestId('gomoku-replay-summary')).toContainText('旧版自由五子棋')
  await expect(page.getByTestId('match-result-card')).toContainText('先手 · 黑')
  await expect(page.getByTestId('match-result-card')).toContainText('后手 · 白')
  await expect(page.getByText('共 2 步', { exact: true })).toBeVisible()
  await expect(page.getByRole('img', { name: '五子棋对局画面' })).toBeVisible()
  await monitor.expectClean(optionalStrictModeReplayAbort(id))
})

test('competition clock marks cumulative timeout', async ({ page }) => {
  await mockAnonymousAuth(page)
  const monitor = monitorBrowser(page)
  const id = 'gomoku-v2-clock-timeout'
  const events = [
    matchStart(),
    { type: 'turn', player: 0, color: 0, phase: 'opening_proposal' },
    { type: 'time_out', seat: 0, used: 900, budget: 900 },
    { type: 'match_end', winner: 1, reason: 'timeout' },
  ]
  await mockReplay(page, id, events, 'timeout', 1)

  await page.goto(`/#/match/${id}`)
  const jump = page.getByRole('button', { name: /直接查看最终结果/ })
  if (await jump.isVisible()) await jump.click()
  await expect(page.getByTestId('gomoku-clock-seat-1')).toContainText('0:00')
  await expect(page.getByTestId('gomoku-clock-seat-1')).toContainText('棋钟耗尽')
  await monitor.expectClean(optionalStrictModeReplayAbort(id))
})

test('human v2 surface submits every phase envelope and stays touch-safe at 390px', async ({ page }, testInfo) => {
  await mockAnonymousAuth(page)
  const monitor = monitorBrowser(page)
  const board3 = boardWith(firstThree)
  const board4 = boardWith([...firstThree, { x: 6, y: 7, color: 1 }])
  const board5 = boardWith([...firstThree, { x: 6, y: 7, color: 1 }, { x: 6, y: 6, color: 0 }])
  const board6 = boardWith([...firstThree, { x: 6, y: 7, color: 1 }, { x: 6, y: 6, color: 0 }, { x: 5, y: 7, color: 1 }])
  const symmetricBoard4 = boardWith([
    { x: 7, y: 7, color: 0 }, { x: 7, y: 8, color: 1 },
    { x: 7, y: 9, color: 0 }, { x: 7, y: 6, color: 1 },
  ])
  const fixtures: Record<string, Fixture> = {
    opening: {
      humanSeat: 0,
      request: request('opening_proposal', 0, 0, [0, 1], emptyBoard(), {
        fixed_black1: { x: 7, y: 7 }, n_range: [2, 5],
      }),
      events: [matchStart(), { type: 'turn', player: 0, color: 0, phase: 'opening_proposal' }],
    },
    swap: {
      humanSeat: 1,
      request: request('swap_choice', 1, 1, [0, 1], board3, { n: 2 }),
      events: [matchStart(), opening, { type: 'turn', player: 1, color: 1, phase: 'swap_choice' }],
    },
    white4: {
      humanSeat: 1,
      request: request('white4', 1, 1, [0, 1], board3, { n: 2 }),
      events: [
        matchStart(), opening, { type: 'swap', player: 1, swapped: false, seat_colors: [0, 1] },
        { type: 'turn', player: 1, color: 1, phase: 'white4' },
      ],
    },
    candidates: {
      humanSeat: 1,
      request: request('black5_candidates', 1, 0, [1, 0], board4, { n: 2 }),
      events: [
        matchStart(), opening, { type: 'swap', player: 1, swapped: true, seat_colors: [1, 0] },
        { type: 'move', player: 0, color: 1, x: 6, y: 7, phase: 'white4', move_index: 4 },
        { type: 'turn', player: 1, color: 0, phase: 'black5_candidates' },
      ],
    },
    'candidate-shape': {
      humanSeat: 0,
      request: request('black5_candidates', 0, 0, [0, 1], symmetricBoard4, { n: 2 }),
      events: [
        matchStart(),
        { type: 'turn', player: 0, color: 0, phase: 'black5_candidates' },
      ],
    },
    select: {
      humanSeat: 1,
      request: request('black5_select', 1, 1, [0, 1], board4, {
        n: 2, candidates: [{ x: 6, y: 6 }, { x: 8, y: 6 }],
      }),
      events: [
        matchStart(), opening, { type: 'swap', player: 1, swapped: false, seat_colors: [0, 1] },
        { type: 'move', player: 1, color: 1, x: 6, y: 7, phase: 'white4', move_index: 4 },
        { type: 'black5_candidates', player: 0, n: 2, points: [{ x: 6, y: 6 }, { x: 8, y: 6 }] },
        { type: 'turn', player: 1, color: 1, phase: 'black5_select' },
      ],
    },
    pass: {
      humanSeat: 1,
      request: request('normal_play', 1, 1, [0, 1], board5, { n: 2, pass_allowed: true }),
      events: [
        matchStart(), opening, { type: 'swap', player: 1, swapped: false, seat_colors: [0, 1] },
        { type: 'move', player: 1, color: 1, x: 6, y: 7, phase: 'white4', move_index: 4 },
        { type: 'black5_candidates', player: 0, n: 2, points: [{ x: 6, y: 6 }, { x: 8, y: 6 }] },
        { type: 'black5_selected', player: 1, index: 0, point: { x: 6, y: 6 } },
        { type: 'move', player: 0, color: 0, x: 6, y: 6, phase: 'black5_select', move_index: 5 },
        { type: 'turn', player: 1, color: 1, phase: 'normal_play' },
      ],
    },
    normal: {
      humanSeat: 1,
      request: request('normal_play', 1, 0, [1, 0], board6, { n: 2, pass_allowed: true }),
      events: [
        matchStart(), opening, { type: 'swap', player: 1, swapped: true, seat_colors: [1, 0] },
        { type: 'move', player: 0, color: 1, x: 6, y: 7, phase: 'white4', move_index: 4 },
        { type: 'black5_candidates', player: 1, n: 2, points: [{ x: 6, y: 6 }, { x: 8, y: 6 }] },
        { type: 'black5_selected', player: 0, index: 0, point: { x: 6, y: 6 } },
        { type: 'move', player: 1, color: 0, x: 6, y: 6, phase: 'black5_select', move_index: 5 },
        { type: 'move', player: 0, color: 1, x: 5, y: 7, phase: 'normal_play', move_index: 6 },
        { type: 'time_used', seat: 1, used: 870, remaining: 30, budget: 900 },
        { type: 'turn', player: 1, color: 0, phase: 'normal_play' },
      ],
    },
    closed: {
      humanSeat: 0,
      request: request('opening_proposal', 0, 0, [0, 1], emptyBoard(), {
        fixed_black1: { x: 7, y: 7 }, n_range: [2, 5],
      }),
      pending: false,
      events: [
        matchStart(),
        { type: 'turn', player: 0, color: 0, phase: 'opening_proposal' },
        {
          type: 'your_turn', player: 0,
          request: request('opening_proposal', 0, 0, [0, 1], emptyBoard(), {
            fixed_black1: { x: 7, y: 7 }, n_range: [2, 5],
          }),
        },
        { type: 'time_used', seat: 0, used: 1, remaining: 899, budget: 900 },
        opening,
      ],
    },
  }
  const sent = new Map<string, Array<Record<string, unknown>>>()

  await page.routeWebSocket(
    (url) => /\/api\/matches\/gomoku-v2-human-[a-z0-9-]+\/play$/.test(url.pathname),
    (socket) => {
      const id = new URL(socket.url()).pathname.split('/').at(-2) || ''
      const fixtureKey = id.replace('gomoku-v2-human-', '')
      const fixture = fixtures[fixtureKey]
      socket.onMessage((message) => {
        const messages = sent.get(fixtureKey) ?? []
        messages.push(JSON.parse(String(message)) as Record<string, unknown>)
        sent.set(fixtureKey, messages)
      })
      setTimeout(() => socket.send(JSON.stringify({
        type: 'snapshot',
        match: {
          id,
          game_id: 'gomoku',
          status: 'running',
          match_type: 'human',
          human_seat: fixture.humanSeat,
          bot_a: { name: 'opening_bot', owner_name: 'alpha' },
          bot_b: fixture.humanSeat === 1
            ? { owner_name: 'human_player', is_human: true }
            : { name: 'swap_bot', owner_name: 'beta' },
        },
        events: fixture.pending === false
          ? fixture.events
          : [
              ...fixture.events,
              { type: 'your_turn', player: fixture.humanSeat, request: fixture.request },
            ],
      })), 0)
    },
  )

  await page.setViewportSize({ width: 390, height: 844 })

  await page.goto('/#/play/gomoku-v2-human-opening')
  await expect(page.getByTestId('gomoku-human-phase')).toHaveText('指定开局')
  let canvas = page.getByRole('button', { name: /五子棋对局画面/ })
  await clickGrid(canvas, { x: 7, y: 8 })
  await clickGrid(canvas, { x: 8, y: 8 })
  await page.getByTestId('gomoku-opening-controls').getByRole('button', { name: '3', exact: true }).click()
  const surfaceButtons = page.getByTestId('gomoku-human-surface').getByRole('button')
  const buttonSizes = await surfaceButtons.evaluateAll((buttons) => buttons.map((button) => {
    const rect = button.getBoundingClientRect()
    return { width: rect.width, height: rect.height, label: button.getAttribute('aria-label') || button.textContent }
  }))
  for (const size of buttonSizes) {
    expect(size.height, `${size.label} touch height`).toBeGreaterThanOrEqual(44)
    expect(size.width, `${size.label} touch width`).toBeGreaterThanOrEqual(44)
  }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await page.getByTestId('gomoku-human-surface').screenshot({
    path: testInfo.outputPath('gomoku-v2-human-mobile.png'),
  })
  await page.getByTestId('gomoku-submit-opening').click()
  await expect.poll(() => sent.get('opening')?.at(-1)).toEqual({
    response: {
      action: 'opening', white2: { x: 7, y: 8 }, black3: { x: 8, y: 8 }, n: 3,
    },
  })

  await page.goto('/#/play/gomoku-v2-human-swap')
  await page.getByTestId('gomoku-submit-swap').click()
  await expect.poll(() => sent.get('swap')?.at(-1)).toEqual({ response: { action: 'swap', swap: true } })

  await page.goto('/#/play/gomoku-v2-human-white4')
  canvas = page.getByRole('button', { name: /五子棋对局画面/ })
  await clickGrid(canvas, { x: 6, y: 7 })
  await expect.poll(() => sent.get('white4')?.at(-1)).toEqual({ response: { action: 'move', x: 6, y: 7 } })

  await page.goto('/#/play/gomoku-v2-human-candidates')
  canvas = page.getByRole('button', { name: /五子棋对局画面/ })
  await clickGrid(canvas, { x: 6, y: 6 })
  await clickGrid(canvas, { x: 8, y: 6 })
  await page.getByTestId('gomoku-submit-candidates').click()
  await expect.poll(() => sent.get('candidates')?.at(-1)).toEqual({
    response: { action: 'black5_candidates', points: [{ x: 6, y: 6 }, { x: 8, y: 6 }] },
  })

  await page.goto('/#/play/gomoku-v2-human-candidate-shape')
  canvas = page.getByRole('button', { name: /五子棋对局画面/ })
  await clickGrid(canvas, { x: 6, y: 5 })
  await expect(page.getByTestId('gomoku-human-surface')).toContainText('已选 1/2')
  // (8,5) 与 (6,5) 关于 x=7 镜像同形，前端与裁判均拒绝把它加入草稿。
  await clickGrid(canvas, { x: 8, y: 5 })
  await expect(page.getByTestId('gomoku-human-surface')).toContainText('已选 1/2')
  await clickGrid(canvas, { x: 5, y: 5 })
  await page.getByTestId('gomoku-submit-candidates').click()
  await expect.poll(() => sent.get('candidate-shape')?.at(-1)).toEqual({
    response: { action: 'black5_candidates', points: [{ x: 6, y: 5 }, { x: 5, y: 5 }] },
  })

  await page.goto('/#/play/gomoku-v2-human-select')
  await page.getByTestId('gomoku-select-controls').getByRole('button', { name: /保留 #2/ }).click()
  await expect.poll(() => sent.get('select')?.at(-1)).toEqual({ response: { action: 'black5_select', index: 1 } })

  await page.goto('/#/play/gomoku-v2-human-pass')
  await page.getByTestId('gomoku-submit-pass').click()
  await expect.poll(() => sent.get('pass')?.at(-1)).toEqual({ response: { action: 'pass' } })

  await page.goto('/#/play/gomoku-v2-human-normal')
  await expect(page.getByTestId('human-matchup')).toContainText('交换决策方 · 当前执黑')
  await expect(page.getByLabel('人类对战状态')).toContainText('30s')
  canvas = page.getByRole('button', { name: /五子棋对局画面/ })
  await clickGrid(canvas, { x: 7, y: 7 })
  expect(sent.get('normal') ?? []).toHaveLength(0)
  await canvas.focus()
  await canvas.press('ArrowRight')
  await canvas.press('ArrowRight')
  // The cumulative clock rerenders HumanPlay every 500 ms. The selected
  // keyboard point must survive that parent render instead of jumping back to
  // the first legal point before Enter submits it.
  await page.waitForTimeout(700)
  await canvas.press('Enter')
  await expect.poll(() => sent.get('normal')?.at(-1)).toEqual({ response: { action: 'move', x: 1, y: 0 } })

  await page.goto('/#/play/gomoku-v2-human-closed')
  await expect(page.getByLabel('人类对战状态')).toContainText('等待中')
  await expect(page.getByTestId('gomoku-submit-opening')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /五子棋对局画面/ })).toHaveCount(0)

  await monitor.expectClean()
})
