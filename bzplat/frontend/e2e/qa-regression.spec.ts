import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type Response,
} from '@playwright/test'

import {
  loginThroughUi,
  monitorBrowser,
  runCleanupTasks,
  versionRow,
  withCleanup,
} from './helpers'

const USER = process.env.BZ_E2E_USER || 'tester1'
const OTHER_USER = process.env.BZ_E2E_OTHER_USER || 'tester2'
const ORGANIZER = process.env.BZ_E2E_ORGANIZER || 'qa_organizer'
const ADMIN = process.env.BZ_E2E_ADMIN || 'qa_admin'
const HOLDEM_SAMPLE = fileURLToPath(
  new URL('../../../samples/callbot_linux_amd64', import.meta.url),
)
const PREFLIGHT_FAILURE_SAMPLE = process.env.BZ_E2E_BAD_BOT || '/usr/bin/true'

/**
 * 生产回放 20260809205002-ede64ea8 的可审计局面检查点。
 *
 * 前 69 手保留真实赢家、筹码差、底池与终局原因；第 70 手保留数据库中的完整事件
 * （含四条街、12 次动作、真实公共牌和 -2850/+2850 整场终局）。这不是随机 filler，
 * 既能稳定覆盖 70 手统计，也避免在测试源码复制 648 条与布局无关的重复发牌动作。
 */
const HOLDEM_0809_FIRST_69_SETTLES = [
  [[1], -100, 200, 'fold'], [[1], -400, 800, 'fold'], [[1], -50, 100, 'fold'], [[0], 50, 100, 'fold'],
  [[0], 100, 200, 'fold'], [[1], -300, 600, 'showdown'], [[1], -50, 100, 'fold'], [[1], -100, 200, 'fold'],
  [[1], -50, 100, 'fold'], [[0], 400, 800, 'showdown'], [[0], 400, 800, 'showdown'], [[0], 50, 100, 'fold'],
  [[1], -50, 100, 'fold'], [[0], 50, 100, 'fold'], [[0], 100, 200, 'fold'], [[0], 50, 100, 'fold'],
  [[1], -50, 100, 'fold'], [[1], -300, 600, 'showdown'], [[1], -50, 100, 'fold'], [[0], 50, 100, 'fold'],
  [[1], -50, 100, 'fold'], [[0], 50, 100, 'fold'], [[1], -600, 1200, 'showdown'], [[0], 500, 1000, 'showdown'],
  [[1], -50, 100, 'fold'], [[0], 400, 800, 'showdown'], [[1], -50, 100, 'fold'], [[1], -100, 200, 'fold'],
  [[0], 100, 200, 'fold'], [[1], -200, 400, 'fold'], [[1], -300, 600, 'showdown'], [[1], -200, 400, 'fold'],
  [[1], -400, 800, 'showdown'], [[0], 50, 100, 'fold'], [[1], -50, 100, 'fold'], [[1], -200, 400, 'showdown'],
  [[1], -100, 200, 'fold'], [[0], 500, 1000, 'showdown'], [[1], -50, 100, 'fold'], [[0], 50, 100, 'fold'],
  [[1], -400, 800, 'showdown'], [[0], 200, 400, 'showdown'], [[0], 100, 200, 'fold'], [[1], -100, 200, 'fold'],
  [[1], -200, 400, 'showdown'], [[0], 50, 100, 'fold'], [[0], 100, 200, 'fold'], [[1], -300, 600, 'showdown'],
  [[0], 500, 1000, 'showdown'], [[1], -200, 400, 'fold'], [[0], 200, 400, 'showdown'], [[0], 50, 100, 'fold'],
  [[1], -50, 100, 'fold'], [[1], -400, 800, 'showdown'], [[0, 1], 0, 200, 'showdown'], [[0], 50, 100, 'fold'],
  [[1], -50, 100, 'fold'], [[1], -400, 800, 'showdown'], [[1], -100, 200, 'fold'], [[0], 50, 100, 'fold'],
  [[1], -300, 600, 'fold'], [[0], 50, 100, 'fold'], [[0], 100, 200, 'fold'], [[0], 300, 600, 'showdown'],
  [[1], -50, 100, 'fold'], [[1], -100, 200, 'fold'], [[0], 100, 200, 'fold'], [[1], -300, 600, 'showdown'],
  [[1], -300, 600, 'fold'],
] as const

function holdemProductionReplay0809(): Array<Record<string, unknown>> {
  const events: Array<Record<string, unknown>> = [
    { type: 'match_start', game_id: 'holdem', num_hands: 70 },
  ]
  let net = 0
  HOLDEM_0809_FIRST_69_SETTLES.forEach(([winners, delta, pot, reason], hand) => {
    const sb = hand % 2
    net += delta
    events.push(
      {
        type: 'hand_start', hand, sb, bb: 1 - sb,
        chips: sb === 0 ? [19_950, 19_900] : [19_900, 19_950],
      },
      {
        type: 'settle', hand, winners: [...winners], deltas: [delta, -delta],
        chips: [20_000 + delta, 20_000 - delta], net: [net, -net], pot, reason,
      },
    )
  })
  events.push(
    { type: 'hand_start', hand: 69, sb: 1, bb: 0, chips: [19_900, 19_950] },
    { type: 'deal_hole', hand: 69, holes: [['6h', '5h'], ['Kh', 'Ts']] },
    { type: 'action', hand: 69, player: 1, action: 'call', amount: 50 },
    { type: 'action', hand: 69, player: 0, action: 'raise', amount: 200 },
    { type: 'action', hand: 69, player: 1, action: 'call', amount: 100 },
    { type: 'deal_board', hand: 69, street: 'flop', board: ['Tc', 'Ac', '6s'], dealt: ['Tc', 'Ac', '6s'] },
    { type: 'action', hand: 69, player: 0, action: 'raise', amount: 100 },
    { type: 'action', hand: 69, player: 1, action: 'call', amount: 100 },
    { type: 'deal_board', hand: 69, street: 'turn', board: ['Tc', 'Ac', '6s', '6d'], dealt: ['6d'] },
    { type: 'action', hand: 69, player: 0, action: 'check', amount: 0 },
    { type: 'action', hand: 69, player: 1, action: 'raise', amount: 100 },
    { type: 'action', hand: 69, player: 0, action: 'raise', amount: 200 },
    { type: 'action', hand: 69, player: 1, action: 'call', amount: 100 },
    { type: 'deal_board', hand: 69, street: 'river', board: ['Tc', 'Ac', '6s', '6d', 'As'], dealt: ['As'] },
    { type: 'action', hand: 69, player: 0, action: 'check', amount: 0 },
    { type: 'action', hand: 69, player: 1, action: 'raise', amount: 100 },
    { type: 'action', hand: 69, player: 0, action: 'fold', amount: 0 },
    {
      type: 'settle', hand: 69, winners: [1], deltas: [-500, 500], chips: [19_500, 20_500],
      net: [-2850, 2850], pot: 1000, board: ['Tc', 'Ac', '6s', '6d', 'As'], reason: 'fold',
    },
    { type: 'match_end', winner: 1, reason: 'completed', deltas: [-2850, 2850] },
  )
  return events
}

/** Holdem 复式赛：两局各 70 手，第二局引擎座位与物理 Bot 对调。 */
function holdemDuplicateReplayFixture(): Array<Record<string, unknown>> {
  const events: Array<Record<string, unknown>> = []
  for (const leg of [0, 1] as const) {
    events.push({ type: 'match_start', game_id: 'holdem', num_hands: 70, leg })
    for (let hand = 0; hand < 70; hand += 1) {
      const sb = hand % 2
      events.push({
        type: 'hand_start', hand, sb, bb: 1 - sb,
        chips: sb === 0 ? [19_950, 19_900] : [19_900, 19_950], leg,
      })
      // 第二局最后一手：引擎座位 1 弃牌，换座后应展示为物理座位 1。
      if (leg === 1 && hand === 69) {
        events.push({ type: 'action', hand, player: 1, action: 'fold', amount: 0, leg })
      }
      events.push({
        type: 'settle', hand, winners: [0], deltas: [100, -100],
        chips: [20_100, 19_900], pot: 200, reason: hand === 69 && leg === 1 ? 'fold' : 'showdown', leg,
      })
    }
  }
  events.push({ type: 'match_end', winner: null, reason: 'completed', deltas: [0, 0] })
  return events
}

async function routeStructuredReplay(
  page: Page,
  matchId: string,
  events: Array<Record<string, unknown>>,
) {
  await page.route(`**/api/matches/${matchId}/replay`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match_id: matchId,
        events,
        event_count: events.length,
        updated_at: '2026-08-11T00:00:00',
      }),
    })
  })
}

async function createDisposableBot(
  page: Page,
  name: string,
  runtimeMode: 'traditional' | 'longrunning' = 'traditional',
) {
  const response = await page.request.post('/api/bots', {
    multipart: {
      name,
      display_name: name,
      description: 'Disposable Playwright entity; hard-deleted in test cleanup',
      upload_note: 'initial disposable version',
      game_id: 'holdem',
      runtime_mode: runtimeMode,
      file: {
        name: 'callbot_linux_amd64',
        mimeType: 'application/octet-stream',
        buffer: await readFile(HOLDEM_SAMPLE),
      },
    },
  })
  const text = await response.text()
  const data = JSON.parse(text) as {
    bot?: { id?: number; name?: string; runtime_mode?: string }
  }
  expect(response.status(), text).toBe(200)
  expect(data.bot?.id).toBeGreaterThan(0)
  expect(data.bot?.name).toBe(name)
  expect(data.bot?.runtime_mode).toBe(runtimeMode)
  return data.bot as { id: number; name: string }
}

async function hardDeleteBots(
  browser: Browser,
  baseURL: string,
  botIds: readonly number[],
) {
  if (!botIds.length) return
  const context = await browser.newContext({ baseURL, viewport: { width: 1280, height: 720 } })
  const page = await context.newPage()
  try {
    await loginThroughUi(page, ADMIN)
    await runCleanupTasks([...new Set(botIds)].reverse().map((botId) => ({
      label: `hard-delete Bot ${botId}`,
      run: async () => {
        const remove = await page.request.delete(`/api/admin/bots/${botId}`)
        expect(remove.status(), await remove.text()).toBe(200)
        const verify = await page.request.get(`/api/bots/${botId}`)
        expect(verify.status(), await verify.text()).toBe(404)
      },
    })))
  } finally {
    await context.close()
  }
}

/**
 * There is deliberately no public/admin match-delete API: match history is an
 * audit record. Cleanup therefore fail-closes only this captured ID to an
 * authoritative terminal state, aborting it through the supported admin route
 * when an assertion exits early while its runner is still active.
 */
async function ensureMatchTerminal(
  browser: Browser,
  baseURL: string,
  request: APIRequestContext,
  matchId: string,
) {
  const initial = await request.get(`/api/matches/${matchId}`)
  expect(initial.status(), await initial.text()).toBe(200)
  const initialBody = await initial.json() as { match: { status: string } }
  if (initialBody.match.status === 'pending' || initialBody.match.status === 'running') {
    const context = await browser.newContext({ baseURL, viewport: { width: 1280, height: 720 } })
    const adminPage = await context.newPage()
    try {
      await loginThroughUi(adminPage, ADMIN)
      const abort = await adminPage.request.patch(`/api/admin/matches/${matchId}`, {
        data: { status: 'aborted' },
      })
      expect(abort.status(), await abort.text()).toBe(200)
    } finally {
      await context.close()
    }
  } else {
    expect(['completed', 'aborted']).toContain(initialBody.match.status)
  }
  const verify = await request.get(`/api/matches/${matchId}`)
  expect(verify.status(), await verify.text()).toBe(200)
  const finalBody = await verify.json() as { match: { status: string } }
  expect(['completed', 'aborted']).toContain(finalBody.match.status)
}

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'laptop', width: 1280, height: 720 },
  { name: 'mobile', width: 390, height: 844 },
] as const

/**
 * 线上人机局 20260810140318-a8752705 在第 11 条合法边后的权威棋盘前缀。
 * 旧 pick 把下一次格心点击映射为 (5,5)，裁判随后以 illegal_move 结束。
 */
const PENCIL_HUMAN_INCIDENT_MOVES = [
  [0, 3, 8],
  [1, 4, 7],
  [0, 1, 8],
  [1, 6, 7],
  [0, 10, 5],
  [1, 8, 7],
  [0, 5, 8],
  [1, 8, 5],
  [0, 4, 1],
  [1, 6, 5],
  [0, 1, 2],
] as const

function pencilHumanIncidentPrefix() {
  return [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    ...PENCIL_HUMAN_INCIDENT_MOVES.map(([player, x, y], index) => ({
      type: 'move', player, x, y, scored: false, scores: [0, 0],
      move_index: index + 1, closed_boxes: [],
    })),
    { type: 'turn', player: 1, pass_: 0, last: { x: 1, y: 2 }, scores: [0, 0] },
    {
      type: 'your_turn', player: 1,
      request: { x: 1, y: 2, pass: 0, me: 1, scores: [0, 0] },
    },
  ]
}

type PencilReplayMove = readonly [
  player: 0 | 1,
  x: number,
  y: number,
  score0: number,
  score1: number,
  closedBoxes: readonly (readonly [x: number, y: number])[],
]

/**
 * 生产对局 20260810143624-4149d6a3 的 54 步权威轨迹。坐标、逐步比分和闭格
 * 均从隔离主库副本提取；turn/time_used/pass 信封在测试中按裁判规则确定性重建，
 * 因此保持原对局 206 个公开事件，同时不依赖会变化的生产数据库。
 */
const PENCIL_REPLAY_143624_MOVES = [
  [0, 8, 3, 0, 0, []], [1, 3, 2, 0, 0, []], [0, 3, 8, 0, 0, []],
  [1, 1, 10, 0, 0, []], [0, 6, 3, 0, 0, []], [1, 2, 1, 0, 0, []],
  [0, 9, 4, 0, 0, []], [1, 5, 10, 0, 0, []], [0, 2, 9, 0, 0, []],
  [1, 0, 7, 0, 0, []], [0, 7, 6, 0, 0, []], [1, 9, 6, 0, 0, []],
  [0, 0, 3, 0, 0, []], [1, 7, 4, 0, 0, []], [0, 6, 1, 0, 0, []],
  [1, 3, 6, 0, 0, []], [0, 1, 8, 0, 0, []], [1, 1, 2, 0, 0, []],
  [0, 2, 5, 0, 0, []], [1, 1, 6, 0, 0, []], [0, 10, 9, 0, 0, []],
  [1, 4, 1, 0, 0, []], [0, 2, 3, 0, 0, []], [1, 0, 9, 0, 1, [[1, 9]]],
  [1, 7, 8, 0, 1, []], [0, 10, 3, 0, 1, []], [1, 1, 0, 0, 1, []],
  [0, 4, 9, 0, 1, []], [1, 5, 0, 0, 1, []], [0, 3, 4, 0, 1, []],
  [1, 7, 0, 0, 1, []], [0, 1, 4, 1, 1, [[1, 3]]], [0, 6, 5, 1, 1, []],
  [1, 5, 6, 1, 1, []], [0, 0, 1, 2, 1, [[1, 1]]], [0, 9, 10, 2, 1, []],
  [1, 9, 8, 2, 1, []], [0, 10, 1, 2, 1, []], [1, 3, 0, 2, 2, [[3, 1]]],
  [1, 7, 2, 2, 3, [[7, 3]]], [1, 8, 9, 2, 4, [[9, 9]]], [1, 6, 7, 2, 4, []],
  [0, 8, 1, 3, 4, [[7, 1]]], [0, 5, 2, 4, 4, [[5, 1]]], [0, 7, 10, 4, 4, []],
  [1, 8, 7, 4, 5, [[7, 7]]], [1, 3, 10, 4, 6, [[3, 9]]], [1, 4, 7, 4, 6, []],
  [0, 9, 0, 4, 6, []], [1, 9, 2, 4, 8, [[9, 1], [9, 3]]],
  [1, 6, 9, 4, 9, [[7, 9]]], [1, 4, 3, 4, 10, [[3, 3]]],
  [1, 5, 4, 4, 11, [[5, 3]]], [1, 5, 8, 4, 13, [[5, 7], [5, 9]]],
] as const satisfies readonly PencilReplayMove[]

function pencilProductionReplay143624() {
  const events: Record<string, unknown>[] = [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
  ]
  let decision = 0
  let previousScores: [number, number] = [0, 0]
  PENCIL_REPLAY_143624_MOVES.forEach(([player, x, y, score0, score1, closedBoxes], moveIndex) => {
    decision += 1
    events.push(
      { type: 'turn', player, pass_: 0, scores: [...previousScores] },
      { type: 'time_used', seat: player, used: decision / 10, remaining: 900 - decision / 10, budget: 900 },
      {
        type: 'move', player, x, y, scored: closedBoxes.length > 0,
        scores: [score0, score1], move_index: moveIndex + 1,
        closed_boxes: closedBoxes.map(([boxX, boxY]) => ({ x: boxX, y: boxY, owner: player })),
      },
    )
    previousScores = [score0, score1]
    if (closedBoxes.length > 0 && score0 < 13 && score1 < 13) {
      const passer = player === 0 ? 1 : 0
      decision += 1
      events.push(
        { type: 'turn', player: passer, pass_: 1, last: { x, y }, scores: [score0, score1] },
        { type: 'time_used', seat: passer, used: decision / 10, remaining: 900 - decision / 10, budget: 900 },
        { type: 'pass', player: passer, scores: [score0, score1] },
      )
    }
  })
  events.push({ type: 'match_end', winner: 1, reason: 'majority', deltas: [-9, 9] })
  return events
}

async function installControlledEventSource(page: Page) {
  await page.addInitScript(() => {
    type WireEvent = Record<string, unknown>
    type Controller = {
      emit: (event: WireEvent) => boolean
      disconnect: () => boolean
      stream: (events: WireEvent[], intervalMs: number) => boolean
      sent: () => number
    }
    type ControlledWindow = typeof window & {
      __matchSseController?: Controller
      __matchSseStreamSent?: number
    }
    class ControlledEventSource {
      static current: ControlledEventSource | null = null
      onmessage: ((event: MessageEvent<string>) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      closed = false

      constructor(_url: string | URL) {
        ControlledEventSource.current = this
      }

      close() {
        this.closed = true
      }
    }
    const controlledWindow = window as ControlledWindow
    const controller: Controller = {
      emit(event) {
        const source = ControlledEventSource.current
        if (!source || source.closed || !source.onmessage) return false
        source.onmessage(new MessageEvent('message', { data: JSON.stringify(event) }))
        return true
      },
      disconnect() {
        const source = ControlledEventSource.current
        if (!source || source.closed || !source.onerror) return false
        source.onerror(new Event('error'))
        return true
      },
      stream(events, intervalMs) {
        const source = ControlledEventSource.current
        if (!source || source.closed || !source.onmessage) return false
        controlledWindow.__matchSseStreamSent = 0
        let index = 0
        const timer = window.setInterval(() => {
          if (index >= events.length) {
            window.clearInterval(timer)
            return
          }
          controller.emit(events[index])
          index += 1
          controlledWindow.__matchSseStreamSent = index
          if (index >= events.length) window.clearInterval(timer)
        }, intervalMs)
        return true
      },
      sent() {
        return controlledWindow.__matchSseStreamSent ?? 0
      },
    }
    Object.defineProperty(window, 'EventSource', {
      configurable: true,
      value: ControlledEventSource,
    })
    controlledWindow.__matchSseController = controller
  })

  const invoke = <T>(method: 'emit' | 'disconnect' | 'stream' | 'sent', arg?: T) => page.evaluate(
    ({ method, arg }) => {
      type Controller = {
        emit: (event: Record<string, unknown>) => boolean
        disconnect: () => boolean
        stream: (events: Record<string, unknown>[], intervalMs: number) => boolean
        sent: () => number
      }
      const controller = (window as typeof window & {
        __matchSseController?: Controller
      }).__matchSseController
      if (!controller) return false
      if (method === 'emit') return controller.emit((arg ?? {}) as Record<string, unknown>)
      if (method === 'disconnect') return controller.disconnect()
      if (method === 'stream') {
        const stream = arg as { events: Record<string, unknown>[]; intervalMs: number }
        return controller.stream(stream.events, stream.intervalMs)
      }
      return controller.sent()
    },
    { method, arg },
  )

  return {
    emit: (event: Record<string, unknown>) => invoke('emit', event),
    disconnect: () => invoke('disconnect'),
    stream: (events: Record<string, unknown>[], intervalMs: number) => (
      invoke('stream', { events, intervalMs })
    ),
    sent: () => invoke('sent'),
  }
}

test.beforeAll(async ({ request }) => {
  const response = await request.get('/api/health')
  expect(response.status(), await response.text()).toBe(200)
  const health = await response.json() as { qa_instance?: boolean; db?: unknown }
  expect(
    health.qa_instance,
    'Refusing browser writes: start the isolated backend with BZ_QA_INSTANCE=1',
  ).toBe(true)
  expect(health).not.toHaveProperty('db')
})

for (const viewport of VIEWPORTS) {
  test(`guest navigation has no severe layout or runtime error (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    const monitor = monitorBrowser(page)
    const routes = [
      { path: '/', heading: '首页', evidence: '多游戏 Bot 竞赛平台' },
      { path: '/leaderboard', heading: '排行榜', evidence: '每款游戏独立使用 Glicko-2 数值评分' },
      { path: '/history', heading: '对局历史', evidence: '全部对局记录，可按状态与游戏筛选' },
      { path: '/contests', heading: '锦标赛', evidence: '组织者发布锦标赛' },
      { path: '/wiki', heading: 'Wiki', evidence: '协议规范、Bot 开发指南' },
      { path: '/judges', heading: '裁判', evidence: '公开可审计' },
      { path: '/challenge', heading: '发起挑战', evidence: '请先' },
      { path: '/my-bots', heading: '我的 Bot', evidence: '请先' },
      { path: '/notifications', heading: '通知', evidence: '请先登录' },
      { path: '/settings', heading: '设置', evidence: '请先登录' },
      { path: '/admin', heading: '管理端', evidence: '请先' },
    ]

    for (const route of routes) {
      await page.goto(`/#${route.path}`)
      const main = page.locator('main')
      await expect(main.getByRole('heading', { name: route.heading, exact: true })).toBeVisible()
      await expect(main).toContainText(route.evidence)
      await expect(page.locator('body')).not.toContainText('Application error')
      if (route.path === '/') {
        await expect(main.getByRole('columnheader', { name: '进度', exact: true })).toHaveCount(0)
        if (viewport.name === 'mobile') {
          await expect(main.locator('table')).toBeHidden()
        }
      }
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      )
      expect(overflow, `${route.path} overflows viewport by ${overflow}px`).toBeLessThanOrEqual(1)
      const gutter = await page.evaluate(() => {
        const main = document.querySelector('main')
        const heading = main?.querySelector('h1')
        if (!(main instanceof HTMLElement) || !(heading instanceof HTMLElement)) return null
        const mainRect = main.getBoundingClientRect()
        const headingRect = heading.getBoundingClientRect()
        return headingRect.left - mainRect.left - Number.parseFloat(getComputedStyle(main).paddingLeft)
      })
      expect(gutter, `${route.path} has a nested page gutter`).not.toBeNull()
      expect(Math.abs(gutter ?? 0), `${route.path} has a nested page gutter`).toBeLessThanOrEqual(1)
      await monitor.settle()
    }

    if (viewport.name === 'mobile') {
      await page.getByRole('button', { name: '菜单' }).click()
      await expect(page.getByRole('link', { name: '排行榜', exact: true })).toBeVisible()
      await page.getByRole('link', { name: '排行榜', exact: true }).click()
      await expect(page).toHaveURL(/#\/leaderboard$/)
      await monitor.settle()
    }
    await monitor.expectClean()
  })
}

test('browser-native validation matches backend phone and Bot-name contracts', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.goto('/#/register')
  const phone = page.locator('#reg-phone')
  await phone.fill('abc')
  expect(await phone.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(false)
  await phone.fill('13800138000')
  expect(await phone.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(true)

  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')
  const name = page.locator('#upload-name')
  for (const invalid of ['a', '1bot', 'a-b']) {
    await name.fill(invalid)
    expect(
      await name.evaluate((input: HTMLInputElement) => input.checkValidity()),
      `${invalid} must be rejected before upload`,
    ).toBe(false)
  }
  await name.fill(`a${'x'.repeat(32)}`)
  expect(await name.inputValue()).toHaveLength(32) // maxLength blocks the 33rd character
  expect(await name.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(true)
  await name.fill('ab')
  expect(await name.evaluate((input: HTMLInputElement) => input.checkValidity())).toBe(true)
  await monitor.expectClean()
})

test('notification preference sends one boolean field and survives a refresh', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let initial = false
  let changed = false

  await loginThroughUi(page, USER)
  await withCleanup(async () => {
    const initialResponsePromise = page.waitForResponse((response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === '/api/notification-prefs',
    )
    await page.goto('/#/settings')
    const initialResponse = await initialResponsePromise
    const initialBody = await initialResponse.json() as {
      prefs: Record<string, unknown>
    }
    expect(Object.values(initialBody.prefs).every((value) => typeof value === 'boolean')).toBe(true)

    await page.getByRole('tab', { name: '通知偏好', exact: true }).click()
    const matchDone = page.getByRole('switch', { name: '对局完成邮件提醒', exact: true })
    initial = await matchDone.isChecked()

    const requestPromise = page.waitForRequest((request) =>
      request.method() === 'PUT' &&
      new URL(request.url()).pathname === '/api/notification-prefs',
    )
    const responsePromise = page.waitForResponse((response) =>
      response.request().method() === 'PUT' &&
      new URL(response.url()).pathname === '/api/notification-prefs',
    )
    await matchDone.click()
    changed = true
    const updateRequest = await requestPromise
    expect(updateRequest.postDataJSON()).toEqual({ email_match_done: !initial })
    const updateResponse = await responsePromise
    expect(updateResponse.status(), await updateResponse.text()).toBe(200)
    expect((await updateResponse.json() as { prefs: { email_match_done: unknown } })
      .prefs.email_match_done).toBe(!initial)

    const refreshedResponsePromise = page.waitForResponse((response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === '/api/notification-prefs',
    )
    await page.reload()
    const refreshedResponse = await refreshedResponsePromise
    expect((await refreshedResponse.json() as { prefs: { email_match_done: unknown } })
      .prefs.email_match_done).toBe(!initial)
    await page.getByRole('tab', { name: '通知偏好', exact: true }).click()
    await expect(matchDone).toBeChecked({ checked: !initial })
  }, async () => {
    if (!changed) return
    await page.goto('/#/settings')
    await page.getByRole('tab', { name: '通知偏好', exact: true }).click()
    const matchDone = page.getByRole('switch', { name: '对局完成邮件提醒', exact: true })
    if (await matchDone.isChecked() !== initial) {
      const restored = page.waitForResponse((response) =>
        response.request().method() === 'PUT' &&
        new URL(response.url()).pathname === '/api/notification-prefs',
      )
      await matchDone.click()
      expect((await restored).status()).toBe(200)
    }
  })
  await monitor.expectClean()
})

test('notification preference ignores a stale load and serializes rapid changes', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let releaseInitialGet!: () => void
  let releaseFirstPut!: () => void
  const initialGetGate = new Promise<void>((resolve) => { releaseInitialGet = resolve })
  const firstPutGate = new Promise<void>((resolve) => { releaseFirstPut = resolve })
  const putValues: boolean[] = []
  let getCount = 0
  let serverValue = false

  await loginThroughUi(page, USER)
  await page.route('**/api/notification-prefs', async (route) => {
    const request = route.request()
    if (request.method() === 'GET') {
      getCount += 1
      if (getCount === 1) await initialGetGate
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          prefs: {
            email_match_done: serverValue,
            email_followed: false,
            email_contest: false,
            email_comment: false,
          },
        }),
      })
      return
    }
    const body = request.postDataJSON() as { email_match_done: boolean }
    putValues.push(body.email_match_done)
    if (putValues.length === 1) await firstPutGate
    serverValue = body.email_match_done
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        prefs: {
          email_match_done: serverValue,
          email_followed: false,
          email_contest: false,
          email_comment: false,
        },
      }),
    })
  })

  await page.goto('/#/settings')
  await page.getByRole('tab', { name: '通知偏好', exact: true }).click()
  const matchDone = page.getByRole('switch', { name: '对局完成邮件提醒', exact: true })
  await expect(matchDone).not.toBeChecked()

  await matchDone.click()
  await expect(matchDone).toBeChecked()
  await expect.poll(() => putValues).toEqual([true])
  releaseInitialGet()
  await expect(matchDone).toBeChecked()

  // The second click updates the UI immediately but must not overtake the first
  // write on the wire; otherwise a late first response can win in the database.
  await matchDone.click()
  await expect(matchDone).not.toBeChecked()
  await expect.poll(() => putValues).toEqual([true])
  releaseFirstPut()
  await expect.poll(() => putValues).toEqual([true, false])
  await expect(matchDone).not.toBeChecked()

  await page.reload()
  await page.getByRole('tab', { name: '通知偏好', exact: true }).click()
  await expect(matchDone).not.toBeChecked()
  expect(serverValue).toBe(false)
  await monitor.expectClean()
})

test('upload rejects a Windows PE before creating a Bot', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const longBotLabel = `超长展示名-${'x'.repeat(120)}`
  await loginThroughUi(page, USER)
  await page.goto('/#/my-bots')

  const uniqueName = `pe_reject_${Date.now().toString(36)}`
  const pe = Buffer.alloc(0x80)
  pe.write('MZ', 0, 'ascii')
  pe.writeUInt32LE(0x40, 0x3c)
  pe.write('PE\0\0', 0x40, 'binary')
  pe.writeUInt16LE(0x8664, 0x44)

  await page.locator('#upload-name').fill(uniqueName)
  await page.locator('#upload-file').setInputFiles({
    name: 'windows-bot.exe',
    mimeType: 'application/octet-stream',
    buffer: pe,
  })
  const rejected = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === '/api/bots'
  ))
  await page.locator('form').filter({ has: page.locator('#upload-name') })
    .getByRole('button', { name: '上传', exact: true })
    .click()
  const response = await rejected
  expect(response.status(), await response.text()).toBe(400)
  await expect(page.locator('main')).toContainText('仅支持 Linux x86_64 ELF64')
  await expect(page.getByRole('link', { name: uniqueName, exact: true })).toHaveCount(0)

  await page.route('**/api/bots/mine?**', async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      bots: [{
        id: 987654321,
        name: 'historical_windows_bot',
        display_name: '历史 Windows Bot',
        game_id: 'holdem',
        format: 'pe',
        os: 'windows',
        arch: 'amd64',
        current_version: 2,
        runtime_mode: 'traditional',
        is_active: 0,
        runnable: false,
        unsupported_reason: '仅支持 Linux x86_64 ELF64（小端）',
      }, {
        id: 987654322,
        name: 'long_layout_bot',
        display_name: longBotLabel,
        description: `不可分割简介-${'d'.repeat(160)}`,
        game_id: 'holdem',
        format: `unknown-${'f'.repeat(100)}`,
        os: 'linux',
        arch: 'amd64',
        current_version: 1,
        runtime_mode: 'traditional',
        is_active: 0,
        runnable: false,
        unsupported_reason: `不可分割原因-${'r'.repeat(160)}`,
      }],
      page: 1,
      per_page: 20,
      total: 2,
    }),
  }))
  await page.reload()
  const historicalRow = page.getByRole('link', { name: '历史 Windows Bot', exact: true })
    .locator('xpath=ancestor::li[1]')
  await expect(historicalRow.getByText('不可运行', { exact: true })).toBeVisible()
  await expect(historicalRow.getByRole('button', { name: '不可启用', exact: true })).toBeDisabled()
  await expect(historicalRow).toContainText('仅支持 Linux x86_64 ELF64')
  const longRow = page.getByRole('link', { name: longBotLabel, exact: true })
    .locator('xpath=ancestor::li[1]')
  await expect(longRow).toBeVisible()
  await page.setViewportSize({ width: 320, height: 844 })
  await longRow.getByRole('button', { name: '编辑', exact: true }).click()
  await expect(longRow.getByLabel('显示名', { exact: true })).toBeVisible()
  await expect(longRow.getByLabel('简介', { exact: true })).toBeVisible()
  const editDescriptionBox = await longRow.getByLabel('简介', { exact: true }).boundingBox()
  expect(editDescriptionBox?.width ?? 999).toBeLessThanOrEqual(230)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await monitor.expectClean([{
    kind: 'http',
    method: 'POST',
    status: 400,
    pathname: '/api/bots',
  }])
})

test('contest game switching cannot submit a stale or mismatched template', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ORGANIZER)

  let markGomokuRequested!: () => void
  const gomokuRequested = new Promise<void>((resolve) => { markGomokuRequested = resolve })
  let releaseGomokuResponse!: () => void
  const gomokuResponseGate = new Promise<void>((resolve) => { releaseGomokuResponse = resolve })

  await page.route('**/api/contests/templates?game=*', async (route) => {
    const game = new URL(route.request().url()).searchParams.get('game')
    if (game === 'gomoku') {
      markGomokuRequested()
      await gomokuResponseGate
      // AbortController may already have cancelled this deliberately stale request.
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          templates: [{ id: 'gomoku_stale', name: '不应回填的五子棋模板', game_id: 'gomoku' }],
        }),
      }).catch(() => undefined)
      return
    }
    const templates = game === 'pencil'
      ? [
          // A malformed server response must not make a cross-game template submittable.
          { id: 'gomoku_leak', name: '错误混入模板', game_id: 'gomoku' },
          { id: 'pencil_race_safe', name: '点格棋竞态模板', game_id: 'pencil' },
        ]
      : [{ id: 'holdem_race_safe', name: '德州初始模板', game_id: 'holdem' }]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ templates }),
    })
  })

  let submitted: Record<string, unknown> | undefined
  await page.route('**/api/contests', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    submitted = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contest: { id: 987_654_322, ...submitted } }),
    })
  })

  await page.goto('/#/contests')
  const form = page.locator('form')
  const gameSelect = form.getByRole('combobox').nth(0)
  const templateSelect = form.getByRole('combobox').nth(1)
  const createButton = form.getByRole('button', { name: '创建比赛', exact: true })
  await expect(templateSelect).toContainText('德州初始模板')

  await gameSelect.click()
  await page.getByRole('option', { name: '五子棋', exact: true }).last().click()
  await gomokuRequested
  await expect(templateSelect).toContainText('模板加载中…')
  await expect(createButton).toBeDisabled()

  // 在旧响应仍悬空时再次切换；后返回的 gomoku 结果不得覆盖 pencil。
  await gameSelect.click()
  await page.getByRole('option', { name: '点格棋', exact: true }).last().click()
  await expect(templateSelect).toContainText('点格棋竞态模板')
  await expect(templateSelect).not.toContainText('错误混入模板')
  await expect(createButton).toBeEnabled()
  releaseGomokuResponse()
  await page.waitForTimeout(150)
  await expect(templateSelect).toContainText('点格棋竞态模板')

  await page.locator('#contest-title').fill('模板竞态回归')
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === '/api/contests'
  ))
  await createButton.click()
  expect((await createResponse).status()).toBe(200)
  expect(submitted).toMatchObject({
    game_id: 'pencil',
    template_id: 'pencil_race_safe',
  })
  await expect(page.getByText('赛事创建成功', { exact: true })).toBeVisible()

  await monitor.expectClean([{
    kind: 'requestfailed',
    method: 'GET',
    pathname: '/api/contests/templates',
    search: '?game=gomoku',
    errorText: 'net::ERR_ABORTED',
  }])
})

test('contest recovery finish trusts terminal matches when pairing status is stale', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ADMIN)
  const contestId = 987_654_321
  let contestStatus = 'running'
  let finishRequests = 0
  let releaseFinishResponse!: () => void
  const finishResponseGate = new Promise<void>((resolve) => {
    releaseFinishResponse = resolve
  })

  await page.route(`**/api/contests/${contestId}?**`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        contest: {
          id: contestId,
          title: 'QA force-finish guard',
          status: contestStatus,
          organizer_id: -1,
          game_id: 'gomoku',
          current_stage_idx: 0,
          stages_json: JSON.stringify([{ key: 'final', type: 'round_robin' }]),
        },
        entries: [],
        pairings: [{
          id: 1,
          bot_a_id: 1,
          bot_b_id: 2,
          bot_a_name: 'force_finish_a',
          bot_b_name: 'force_finish_b',
          owner_a_name: 'force_owner_a',
          owner_a_display: '强制结束甲方',
          owner_b_name: 'force_owner_b',
          owner_b_display: '强制结束乙方',
          // The pairing projection is stale, while its associated match is
          // already terminal. The finish endpoint is authoritative here.
          status: 'running',
          match_id: 'completed-match-1',
          stage_idx: 0,
          round_num: 1,
        }],
        standings: [],
        entries_total: 0,
        my_entry: null,
      }),
    })
  })
  await page.route(`**/api/contests/${contestId}/finish`, async (route) => {
    expect(route.request().method()).toBe('POST')
    finishRequests += 1
    await finishResponseGate
    contestStatus = 'finished'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contest: { id: contestId, status: 'finished' } }),
    })
  })
  await page.route(`**/api/contests/${contestId}/official-results`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [] }),
    })
  })

  await page.goto(`/#/contests/${contestId}`)
  const finish = page.getByRole('button', { name: '强制结束赛事', exact: true })
  await expect(finish).toBeEnabled()
  await finish.locator('xpath=..').hover()
  await expect(page.getByRole('tooltip')).toContainText('由后端核验关联对局终态')

  await finish.click()
  const finishRequest = page.waitForRequest((request) => (
    request.method() === 'POST' && request.url().endsWith(`/api/contests/${contestId}/finish`)
  ))
  await page.getByRole('dialog').getByRole('button', { name: '确认结束', exact: true }).click()
  await finishRequest
  await expect(finish).toBeDisabled()
  expect(finishRequests).toBe(1)
  releaseFinishResponse()
  await expect(page.getByRole('main').getByText('已结束', { exact: true })).toBeVisible()
  expect(finishRequests).toBe(1)
  await monitor.expectClean()
})

test('contest detail ignores a stale response after navigating to another contest', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await loginThroughUi(page, ORGANIZER)
  const organizerId = await page.evaluate(() => {
    const raw = localStorage.getItem('bzplat_user')
    return raw ? Number((JSON.parse(raw) as { id?: number }).id) : 0
  })
  expect(organizerId).toBeGreaterThan(0)

  const slowContestId = 987_654_310
  const targetContestId = 987_654_311
  let staleFinishRequests = 0
  let releaseSlow!: () => void
  const slowGate = new Promise<void>((resolve) => { releaseSlow = resolve })
  let observeSlow!: () => void
  const slowObserved = new Promise<void>((resolve) => { observeSlow = resolve })
  const detailBody = (id: number, title: string, status: string) => ({
    contest: {
      id,
      title,
      status,
      organizer_id: organizerId,
      game_id: 'gomoku',
      current_stage_idx: 0,
      stages_json: JSON.stringify([{ key: 'main', type: 'round_robin' }]),
    },
    entries: [],
    pairings: [],
    standings: [],
    entries_total: 0,
    my_entry: null,
  })

  await page.route(
    new RegExp(`/api/contests/${slowContestId}\\?entries_page=1&entries_per_page=20$`),
    async (route) => {
      observeSlow()
      await slowGate
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(detailBody(slowContestId, 'stale contest A', 'running')),
      })
    },
  )
  await page.route(
    new RegExp(`/api/contests/${targetContestId}\\?entries_page=1&entries_per_page=20$`),
    async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(detailBody(targetContestId, 'target contest B', 'running')),
    }),
  )
  await page.route(`**/api/contests/${slowContestId}/finish`, async (route) => {
    staleFinishRequests += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ contest: { id: slowContestId, status: 'finished' } }),
    })
  })

  await page.goto(`/#/contests/${slowContestId}`)
  await slowObserved
  await page.goto(`/#/contests/${targetContestId}`)
  await expect(page.getByRole('heading', { name: 'target contest B', exact: true })).toBeVisible()
  releaseSlow()
  await page.waitForTimeout(250)
  await expect(page).toHaveURL(new RegExp(`/#/contests/${targetContestId}$`))
  await expect(page.getByRole('heading', { name: 'target contest B', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'stale contest A', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开放报名', exact: true })).toHaveCount(0)

  // A non-blocking destructive confirmation is hook state, so it survives a
  // reused route component unless the id transition explicitly cancels it.
  // It must neither reappear on B nor retain an async closure that can POST A.
  await page.goto(`/#/contests/${slowContestId}`)
  await expect(page.getByRole('heading', { name: 'stale contest A', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '强制结束赛事', exact: true }).click()
  const staleConfirm = page.getByRole('dialog').filter({ hasText: '强制结束赛事？' })
  await expect(staleConfirm).toBeVisible()
  await page.goto(`/#/contests/${targetContestId}`)
  await expect(page.getByRole('heading', { name: 'target contest B', exact: true })).toBeVisible()
  // On a regression, click the resurfaced dialog through the mocked endpoint so
  // the assertion below proves the stale continuation really is fenced off.
  if (await staleConfirm.isVisible().catch(() => false)) {
    await staleConfirm.getByRole('button', { name: '确认结束', exact: true }).click()
  }
  await expect(staleConfirm).toHaveCount(0)
  await expect(page.getByRole('button', { name: '强制结束赛事', exact: true })).toBeEnabled()
  expect(staleFinishRequests).toBe(0)
  await monitor.expectClean()
})

async function chooseBot(page: Page, trigger: Locator, query: string, mineOnly: boolean) {
  await trigger.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  if (mineOnly) {
    await expect(dialog.getByRole('button', { name: '全部 Bot', exact: true })).toHaveCount(0)
  }
  const input = dialog.getByPlaceholder(
    mineOnly ? '搜索我的 Bot 名称…' : '搜索 Bot 名称…',
  )
  await input.fill(query)
  await dialog.locator('li').filter({ hasText: query }).getByRole('button').click()
}

/**
 * Challenge/human creation is a durable 202 request, not a synchronous match.
 * Poll the owner-scoped opaque id until capacity admission binds a public match.
 */
async function waitForAcceptedExecutionMatch(
  page: Page,
  acceptedResponse: Response,
  expectedSource: 'manual' | 'human',
  timeout = 45_000,
): Promise<string> {
  const acceptedText = await acceptedResponse.text()
  expect(acceptedResponse.status(), acceptedText).toBe(202)
  const accepted = JSON.parse(acceptedText) as {
    public_id?: string
    request?: { source?: string }
  }
  expect(accepted.public_id).toMatch(/^req_[A-Za-z0-9_-]{24}$/)
  expect(accepted.request?.source).toBe(expectedSource)

  let matchId = ''
  await expect.poll(async () => {
    const detail = await page.request.get(
      `/api/execution-requests/${encodeURIComponent(accepted.public_id!)}`,
    )
    const text = await detail.text()
    if (detail.status() !== 200) return `http:${detail.status()}:${text}`
    const snapshot = JSON.parse(text) as {
      public_id?: string
      request?: { match_id?: string | null; source?: string; status?: string }
    }
    if (snapshot.public_id !== accepted.public_id) return 'public-id-mismatch'
    if (snapshot.request?.source !== expectedSource) return 'source-mismatch'
    matchId = snapshot.request?.match_id || ''
    return matchId ? `match:${matchId}` : `status:${snapshot.request?.status || 'unknown'}`
  }, {
    timeout,
    intervals: [100, 250, 500, 1_000],
    message: `execution request ${accepted.public_id} did not receive a match`,
  }).toMatch(/^match:/)
  return matchId
}

/** 把 Pencil 交错坐标转换成当前响应式 canvas 内的 CSS 点击位置。 */
async function pencilCanvasPoint(canvas: Locator, x: number, y: number) {
  return canvas.evaluate(async (element, coordinate) => {
    const module = await import('/src/games/pencil/canvas.ts')
    const rect = element.getBoundingClientRect()
    const layout = module.pencilCanvasLayout(rect.width, rect.height, 11)
    return { x: layout.cx(coordinate.x), y: layout.cy(coordinate.y) }
  }, { x, y })
}

test('challenge is single-submit, reaches its terminal viewer, and Cmd+K aggregates results', async ({
  page,
  browser,
  baseURL,
  request,
}) => {
  expect(baseURL).toBeTruthy()
  const createdBotIds: number[] = []
  let matchId: string | null = null
  await withCleanup(async () => {
    const monitor = monitorBrowser(page)
    await loginThroughUi(page, USER)
    const suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
    // This case verifies the challenge/UI lifecycle rather than Traditional's
    // per-decision container startup. The sample implements the canonical
    // KEEP_RUNNING handshake, so LongRunning keeps the real 70-hand match
    // comfortably inside the browser-test budget without weakening assertions.
    const disposable = await createDisposableBot(page, `pwch_${suffix}`, 'longrunning')
    createdBotIds.push(disposable.id)
    await page.goto('/#/challenge')

    await chooseBot(
      page,
      page.getByRole('button', { name: '选择我的 Bot', exact: true }),
      disposable.name,
      true,
    )
    await chooseBot(
      page,
      page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }),
      disposable.name,
      false,
    )

    let challengePosts = 0
    page.on('request', (browserRequest) => {
      if (
        browserRequest.method() === 'POST' &&
        new URL(browserRequest.url()).pathname === '/api/matches/challenge'
      ) challengePosts += 1
    })
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/matches/challenge',
    )
    await page.getByRole('button', { name: '开始对局', exact: true }).dblclick()
    const response = await responsePromise
    matchId = await waitForAcceptedExecutionMatch(page, response, 'manual')
    expect(matchId).toBeTruthy()
    await expect(page).toHaveURL(/\/#\/match\//)
    expect(challengePosts).toBe(1)

    await expect.poll(async () => {
      const detail = await page.request.get(`/api/matches/${matchId}`)
      const body = await detail.json() as {
        match?: { status?: string; reason?: string; result?: { rounds_played?: number } }
      }
      return [
        body.match?.status,
        body.match?.reason,
        Number(body.match?.result?.rounds_played ?? 0),
      ]
    }, { timeout: 45_000 }).toEqual(['completed', 'completed', 70])
    await expect(page.getByText('已完成', { exact: true })).toBeVisible({ timeout: 45_000 })
    await expect(page.locator('main')).not.toContainText('座0')

    await page.keyboard.press('Control+K')
    const searchDialogs = page.getByRole('dialog')
    await expect(searchDialogs).toHaveCount(1)
    const search = searchDialogs.getByPlaceholder('搜索 Bot、用户、对局…')
    await search.fill(disposable.name)
    await expect(searchDialogs.getByText('Bot', { exact: true })).toBeVisible()
    await expect(searchDialogs.getByText('对局', { exact: true })).toBeVisible()
    await monitor.expectClean()
  }, async () => {
    const tasks: Array<{ label: string; run: () => Promise<void> }> = []
    if (matchId) {
      const createdMatchId = matchId
      tasks.push({
        label: `settle challenge ${createdMatchId}`,
        run: () => ensureMatchTerminal(browser, baseURL!, request, createdMatchId),
      })
    }
    tasks.push({
      label: 'delete disposable challenge Bots',
      run: () => hardDeleteBots(browser, baseURL!, createdBotIds),
    })
    await runCleanupTasks(tasks)
  })
})

test('terminal SSE snapshot switches a raced live page to replay without reconnecting', async ({ page, request }) => {
  const completedResponse = await request.get('/api/matches?status=completed&limit=1')
  expect(completedResponse.status(), await completedResponse.text()).toBe(200)
  const completed = await completedResponse.json() as { matches?: Array<{ id: string }> }
  const matchId = completed.matches?.[0]?.id
  expect(matchId).toBeTruthy()

  // Reproduce the precise race: the initial detail probe still says `running`, while
  // subscribe() observes the already completed row and sends one terminal snapshot.
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    const response = await route.fetch()
    const body = await response.json() as { match: { status?: string } }
    body.match.status = 'running'
    await route.fulfill({ response, json: body })
  })

  let eventRequests = 0
  page.on('request', (browserRequest) => {
    if (new URL(browserRequest.url()).pathname === `/api/matches/${matchId}/events`) {
      eventRequests += 1
    }
  })
  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  // Native EventSource reconnect delay is normally about three seconds. Stay past
  // that window so a server-side terminal snapshot regression cannot false-pass.
  await page.waitForTimeout(4_000)
  expect(eventRequests).toBe(1)
  await monitor.expectClean()
})

test('canonical terminal deltas drive MatchViewer and HumanPlay', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const viewerId = 'mock-canonical-terminal-viewer'
  const humanId = 'mock-canonical-terminal-human'

  // Register WebSocket interception before the first navigation. Vite creates
  // its HMR socket on that navigation; Playwright must install page-level WS
  // routing before any socket exists for subsequent business sockets to route.
  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${humanId}/play`,
    (socket) => {
      setTimeout(() => {
        socket.send(JSON.stringify({
          type: 'snapshot',
          match: {
            id: humanId,
            game_id: 'holdem',
            status: 'running',
            match_type: 'human',
            human_seat: 1,
            bot_a: { name: 'canonical_bot', owner_name: 'alpha' },
            bot_b: { owner_name: 'human_player', is_human: true },
            result: { rounds_played: 0, deltas: [0, 0] },
          },
          events: [{ type: 'match_start', game_id: 'holdem', num_hands: 1 }],
        }))
      }, 0)
      setTimeout(() => {
        socket.send(JSON.stringify({
          type: 'match_end',
          winner: 0,
          reason: 'completed',
          deltas: [23, -23],
        }))
      }, 30)
    },
  )

  await page.route(`**/api/matches/${viewerId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${viewerId}/events`, async (route) => {
    const stream = [
      { type: 'match_start', game_id: 'holdem', num_hands: 1 },
      { type: 'match_end', winner: 0, reason: 'completed', deltas: [37, -37] },
    ].map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream })
  })
  await page.route(`**/api/matches/${viewerId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: viewerId,
          game_id: 'holdem',
          status: 'running',
          match_type: 'challenge',
          bot_a: { name: 'canonical_a', owner_name: 'alpha' },
          bot_b: { name: 'canonical_b', owner_name: 'beta' },
          result: { rounds_played: 0, deltas: [0, 0] },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ comments: [], count: 0, total: 0 }),
    })
  })

  await page.goto(`/#/match/${viewerId}`)
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await expect(page.getByText('canonical_a @alpha', { exact: true }).first()).toBeVisible()
  // The viewer intentionally parks on the event before terminal. Step once to
  // prove the game reducer consumes canonical `deltas`, not retired aliases.
  await page.getByRole('button', { name: '下一个事件', exact: true }).click()
  await expect(page.getByTestId('holdem-seat-state-1')).toContainText('+37')
  await expect(page.getByTestId('holdem-seat-state-2')).toContainText('-37')

  await page.goto(`/#/play/${humanId}`)
  await expect(page.getByText(/对局结束 · 胜者：canonical_bot @alpha/)).toBeVisible()
  await expect(page.getByText('累计 +23 / -23', { exact: true })).toBeVisible()
  await monitor.expectClean()
})

test('Holdem production replay uses empty space for a responsive current-position dashboard', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = '20260809205002-ede64ea8'
  const events = holdemProductionReplay0809()
  expect(events.filter((event) => event.type === 'hand_start')).toHaveLength(70)
  expect(events.filter((event) => event.type === 'settle')).toHaveLength(70)
  expect(events.at(-1)).toEqual({ type: 'match_end', winner: 1, reason: 'completed', deltas: [-2850, 2850] })

  await routeStructuredReplay(page, matchId, events)
  await page.route(`**/api/matches/${matchId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${matchId}`, async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      match: {
        id: matchId,
        game_id: 'holdem',
        status: 'completed',
        winner: 1,
        reason: 'completed',
        match_type: 'challenge',
        bot_a_id: 3,
        bot_b_id: 4,
        bot_a: { id: 3, name: 'admin', owner_name: 'zzx' },
        bot_b: { id: 4, name: 'mybot01', display_name: '测试Bot01', owner_name: 'tester01' },
        result: { rounds_played: 70, deltas: [-2850, 2850], normalized_delta: -28.5 },
      },
    }),
  }))
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))

  await page.setViewportSize({ width: 1536, height: 900 })
  await page.goto(`/#/match/${matchId}`)
  const overview = page.getByTestId('holdem-position-overview')
  const canvas = page.getByRole('img', { name: 'holdem 对局画面' })
  const timeline = page.getByTestId('match-timeline')
  await page.getByRole('button', { name: /跳到结局/ }).click()
  // 回退到终局前的 settle：座位状态必须是本手结果，
  // 不能在赢家卡上写“等待行动”。fold 瞬时帧则必须等待结算。
  await page.getByRole('button', { name: '上一个事件', exact: true }).click()
  await expect(page.getByTestId('holdem-seat-state-1')).toContainText('本手结束')
  await expect(page.getByTestId('holdem-seat-state-2')).toContainText('本手获胜')
  await page.getByRole('button', { name: '上一个事件', exact: true }).click()
  await expect(overview).toContainText('等待本手结算')
  await expect(overview).not.toContainText('座位 2 行动')
  await page.getByRole('button', { name: '下一个事件', exact: true }).click()
  await page.getByRole('button', { name: '下一个事件', exact: true }).click()
  await expect(overview).toContainText('当前手 70 / 70')
  await expect(overview).toContainText('已结算 70 手')
  await expect(overview).toContainText('剩余 0 手')
  await expect(overview).toContainText('本手底池')
  await expect(overview).toContainText('1,000')
  await expect(overview).toContainText('最近动作')
  await expect(overview).toContainText('座位 1 · 弃牌')
  await expect(overview).toContainText('胜手 座1 29 · 座2 40 · 平分 1')
  await expect(page.getByTestId('holdem-seat-state-1')).toContainText('19,500')
  await expect(page.getByTestId('holdem-seat-state-1')).toContainText('-2,850')
  await expect(page.getByTestId('holdem-seat-state-2')).toContainText('20,500')
  await expect(page.getByTestId('holdem-seat-state-2')).toContainText('+2,850')
  await expect(overview.getByLabel('第 70 手，座位 1 -500')).toBeVisible()

  for (const viewport of [
    { width: 2560, height: 1080 },
    { width: 1920, height: 1080 },
    { width: 1760, height: 900 },
  ]) {
    await page.setViewportSize(viewport)
    await page.evaluate(() => window.scrollTo(0, 0))
    const hudBox = await overview.boundingBox()
    const canvasBox = await canvas.boundingBox()
    const timelineBox = await timeline.boundingBox()
    expect(hudBox).not.toBeNull()
    expect(canvasBox).not.toBeNull()
    expect(timelineBox).not.toBeNull()
    expect(hudBox?.x ?? 9999).toBeLessThan(canvasBox?.x ?? 0)
    expect(canvasBox?.x ?? 9999).toBeLessThan(timelineBox?.x ?? 0)
    // 两个信息栏按内容自然收口，小于一行的差异不用伪造空高度补齐。
    expect(Math.abs((hudBox?.height ?? 0) - (timelineBox?.height ?? 0))).toBeLessThanOrEqual(24)
    expect((canvasBox?.width ?? 0) / (canvasBox?.height ?? 1)).toBeCloseTo(16 / 9, 1)
    expect((timelineBox?.y ?? 0) + (timelineBox?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  }

  for (const viewport of [
    { width: 1600, height: 900 },
    { width: 1536, height: 900 },
    { width: 1366, height: 768 },
    { width: 1280, height: 800 },
  ]) {
    await page.setViewportSize(viewport)
    await page.evaluate(() => window.scrollTo(0, 0))
    const hudBox = await overview.boundingBox()
    const canvasBox = await canvas.boundingBox()
    const timelineBox = await timeline.boundingBox()
    expect((hudBox?.y ?? 9999) + (hudBox?.height ?? 0)).toBeLessThanOrEqual((canvasBox?.y ?? 0) + 1)
    expect(timelineBox?.x ?? 0).toBeGreaterThan((canvasBox?.x ?? 0) + (canvasBox?.width ?? 0))
    expect((timelineBox?.y ?? 0) + (timelineBox?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  }

  for (const viewport of [
    { width: 1024, height: 768 },
    { width: 390, height: 844 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewportSize(viewport)
    await page.evaluate(() => window.scrollTo(0, 0))
    await expect(timeline.getByRole('button', { name: '展开', exact: true })).toBeVisible()
    const hudBox = await overview.boundingBox()
    const canvasBox = await canvas.boundingBox()
    const timelineBox = await timeline.boundingBox()
    expect((hudBox?.y ?? 9999) + (hudBox?.height ?? 0)).toBeLessThanOrEqual((canvasBox?.y ?? 0) + 1)
    expect(timelineBox?.y ?? 0).toBeGreaterThan((canvasBox?.y ?? 0) + (canvasBox?.height ?? 0))
    expect((canvasBox?.width ?? 0) / (canvasBox?.height ?? 1)).toBeCloseTo(16 / 9, 1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  }

  await page.setViewportSize({ width: 1366, height: 768 })
  await page.evaluate(() => window.scrollTo(0, 360))
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(100)
  await expect.poll(async () => (await timeline.boundingBox())?.y ?? 999).toBeLessThanOrEqual(30)
  await page.evaluate(() => window.scrollTo(0, 0))

  await page.setViewportSize({ width: 1600, height: 900 })
  await timeline.getByRole('button', { name: '折叠', exact: true }).click()
  const collapsedHud = await overview.boundingBox()
  const collapsedCanvas = await canvas.boundingBox()
  const collapsedTimeline = await timeline.boundingBox()
  expect(collapsedHud?.x ?? 9999).toBeLessThan(collapsedCanvas?.x ?? 0)
  expect(collapsedTimeline?.y ?? 0).toBeGreaterThan((collapsedCanvas?.y ?? 0) + (collapsedCanvas?.height ?? 0))
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('Holdem duplicate replay keeps 140-hand progress and physical Bot seats truthful', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-holdem-duplicate-position-dashboard'
  const events = holdemDuplicateReplayFixture()
  expect(events.filter((event) => event.type === 'hand_start')).toHaveLength(140)
  expect(events.filter((event) => event.type === 'settle')).toHaveLength(140)

  await routeStructuredReplay(page, matchId, events)
  await page.route(`**/api/matches/${matchId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${matchId}`, async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      match: {
        id: matchId,
        game_id: 'holdem',
        status: 'completed',
        winner: null,
        reason: 'completed',
        match_type: 'contest',
        bot_a: { id: 31, name: 'physical_alpha', owner_name: 'alpha' },
        bot_b: { id: 32, name: 'physical_beta', owner_name: 'beta' },
        result: {
          rounds_played: 140,
          deltas: [0, 0],
          normalized_delta: 0,
          legs: [
            { winner: 0, deltas: [7000, -7000] },
            { winner: 1, deltas: [-7000, 7000] },
          ],
        },
      },
    }),
  }))
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))

  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto(`/#/match/${matchId}`)
  await page.getByRole('button', { name: /跳到结局/ }).click()
  const overview = page.getByTestId('holdem-position-overview')
  await expect(page.getByText('复式赛按分局计分', { exact: true })).toBeVisible()
  await expect(page.getByText('第 140/140 手', { exact: true })).toBeVisible()
  await expect(overview).toContainText('第 2/2 局 · 当前手 70 / 70')
  await expect(overview).toContainText('总计已结算 140 手')
  await expect(overview).toContainText('剩余 0 手')
  await expect(overview).toContainText('座位 1 · 弃牌')
  await expect(overview).toContainText('胜手 座1 70 · 座2 70')
  await expect(overview.getByRole('progressbar', { name: '已完成手数' })).toHaveAttribute('aria-valuemax', '140')
  await expect(overview.getByRole('progressbar', { name: '已完成手数' })).toHaveAttribute('aria-valuenow', '140')
  await expect(overview.getByLabel('第 2 局第 70 手，座位 1 -100')).toBeVisible()

  await expect(page.locator('main')).toContainText('第 2 局 · 座1 · 弃牌')
  await expect(page.locator('main')).toContainText('结束 · 无单一整场胜者 · 正常结束')
  await expect(page.locator('main')).not.toContainText('结束 · 平局')
  await page.getByRole('combobox', { name: '跳转手' }).click()
  const secondLegFirstHand = page.getByRole('option', { name: '第 2 局 · 第 1 手', exact: true })
  await expect(secondLegFirstHand).toBeVisible()
  await secondLegFirstHand.click()
  await page.getByRole('button', { name: '上一个事件', exact: true }).click()
  await expect(overview).toContainText('第 2/2 局 · 等待发牌 · 共 140 手')
  await expect(overview).toContainText('总计已结算 70 手')
  await expect(overview).toContainText('剩余 70 手')
  await expect(overview).toContainText('等待首个动作')
  await expect(overview).toContainText('尚未发牌')
  await expect(overview).not.toContainText('翻牌前')
  await expect(overview).not.toContainText('小盲')
  await expect(overview).not.toContainText('大盲')
  await expect(overview).not.toContainText('等待行动')
  await expect(overview).not.toContainText('当前手 70')
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('human Holdem reuses the public-position HUD without exposing hole-card text', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-holdem-human-responsive-density'
  const productionEvents = holdemProductionReplay0809()
  // 停在真实第 70 手河牌、最后一次加注之后，尚未弃牌和结算。
  const liveEvents = productionEvents.slice(0, -3)

  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${matchId}/play`,
    (socket) => {
      setTimeout(() => socket.send(JSON.stringify({
        type: 'snapshot',
        match: {
          id: matchId,
          game_id: 'holdem',
          status: 'running',
          match_type: 'human',
          human_seat: 1,
          bot_a: { name: 'admin', owner_name: 'zzx' },
          bot_b: { owner_name: 'tester01', is_human: true },
          result: { rounds_played: 69, deltas: [-2350, 2350] },
        },
        events: liveEvents,
      })), 0)
    },
  )

  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto(`/#/play/${matchId}`)
  const overview = page.getByTestId('holdem-position-overview')
  const canvas = page.getByRole('img', { name: 'holdem 对局画面' })
  const eventLog = page.getByTestId('human-event-log')
  await expect(overview).toContainText('当前手 70 / 70')
  await expect(overview).toContainText('河牌')
  await expect(overview).toContainText('座位 2 · 加注至 100')
  await expect(overview).not.toContainText(/6h|5h|Kh|Ts/)

  let hudBox = await overview.boundingBox()
  let canvasBox = await canvas.boundingBox()
  let logBox = await eventLog.boundingBox()
  expect(hudBox?.x ?? 9999).toBeLessThan(canvasBox?.x ?? 0)
  expect(canvasBox?.x ?? 9999).toBeLessThan(logBox?.x ?? 0)

  for (const viewport of [
    { width: 1600, height: 900 },
    { width: 1536, height: 900 },
    { width: 1366, height: 768 },
  ]) {
    await page.setViewportSize(viewport)
    hudBox = await overview.boundingBox()
    canvasBox = await canvas.boundingBox()
    logBox = await eventLog.boundingBox()
    expect((hudBox?.y ?? 9999) + (hudBox?.height ?? 0)).toBeLessThanOrEqual((canvasBox?.y ?? 0) + 1)
    expect(logBox?.x ?? 0).toBeGreaterThan((canvasBox?.x ?? 0) + (canvasBox?.width ?? 0))
  }

  for (const viewport of [
    { width: 390, height: 844 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewportSize(viewport)
    hudBox = await overview.boundingBox()
    canvasBox = await canvas.boundingBox()
    logBox = await eventLog.boundingBox()
    expect((hudBox?.y ?? 9999) + (hudBox?.height ?? 0)).toBeLessThanOrEqual((canvasBox?.y ?? 0) + 1)
    expect(logBox?.y ?? 0).toBeGreaterThan((canvasBox?.y ?? 0) + (canvasBox?.height ?? 0))
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  }
  await monitor.expectClean()
})

test('MatchViewer distinguishes rating eligibility from settlement state', async ({ page }) => {
  const fixtures = [
    {
      id: 'mock-rating-policy-neutral',
      rated: false,
      ratingReason: 'same_owner',
      ratingSettled: true,
      status: 'completed',
      label: '同所有者调试 · 不计天梯',
    },
    {
      id: 'mock-rating-policy-expected',
      rated: true,
      ratingReason: 'eligible',
      ratingSettled: false,
      status: 'pending',
      label: '预计计分',
    },
    {
      id: 'mock-rating-policy-waiting',
      rated: true,
      ratingReason: 'eligible',
      ratingSettled: false,
      status: 'completed',
      label: '待结算',
    },
    {
      id: 'mock-rating-policy-settled',
      rated: true,
      ratingReason: 'eligible',
      ratingSettled: true,
      status: 'completed',
      label: '已计分',
    },
    {
      id: 'mock-rating-policy-aborted',
      rated: true,
      ratingReason: 'eligible',
      ratingSettled: false,
      status: 'aborted',
      label: '已中止未计分',
    },
  ] as const
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))
  let liveReplayRequests = 0
  for (const fixture of fixtures) {
    const replayEvents = fixture.status === 'aborted'
      ? [{ type: 'error', reason: 'platform_error' }]
      : [{ type: 'match_end', winner: 0, reason: 'five' }]
    if (fixture.status === 'pending') {
      await page.route(`**/api/matches/${fixture.id}/replay`, async (route) => {
        liveReplayRequests += 1
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: '{"detail":"live match must not fetch replay"}',
        })
      })
    } else {
      await routeStructuredReplay(page, fixture.id, replayEvents)
    }
    await page.route(`**/api/matches/${fixture.id}/view`, async (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: '{"ok":true}',
    }))
    if (fixture.status === 'pending') {
      await page.route(`**/api/matches/${fixture.id}/events`, async (route) => route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `data: ${JSON.stringify({ type: 'snapshot', match: { status: 'pending' }, events: [] })}\n\n`,
      }))
    }
    await page.route(`**/api/matches/${fixture.id}`, async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: fixture.id,
          game_id: 'gomoku',
          status: fixture.status,
          reason: fixture.status === 'aborted' ? 'platform_error' : 'five',
          winner: fixture.status === 'aborted' ? null : 0,
          match_type: 'challenge',
          rated: fixture.rated,
          rating_reason: fixture.ratingReason,
          rating_settled: fixture.ratingSettled,
          bot_a: { name: 'policy_alpha', owner_name: 'owner-a' },
          bot_b: { name: 'policy_beta', owner_name: 'owner-b' },
          result: { rounds_played: 9, deltas: [1, -1], normalized_delta: 1 },
        },
      }),
    }))
  }

  const monitor = monitorBrowser(page)
  for (const fixture of fixtures) {
    await page.goto(`/#/match/${fixture.id}`)
    await expect(page.getByTestId('rating-state')).toHaveText(fixture.label)
  }
  expect(liveReplayRequests).toBe(0)
  await monitor.expectClean()
})

test('MatchViewer gates replay behind metadata and keeps metadata on replay failure', async ({ page }) => {
  const matchId = 'mock-replay-metadata-gate-failure'
  let replayRequests = 0
  let releaseMetadata!: () => void
  let observeMetadata!: () => void
  const metadataGate = new Promise<void>((resolve) => { releaseMetadata = resolve })
  const metadataObserved = new Promise<void>((resolve) => { observeMetadata = resolve })

  await page.route(`**/api/matches/${matchId}/replay`, async (route) => {
    replayRequests += 1
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '回放暂时不可用' }),
    })
  })
  await page.route(`**/api/matches/${matchId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    observeMetadata()
    await metadataGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: matchId,
          game_id: 'holdem',
          status: 'completed',
          winner: 0,
          reason: 'completed',
          match_type: 'challenge',
          bot_a: { name: 'metadata_alpha', owner_name: 'alpha' },
          bot_b: { name: 'metadata_beta', owner_name: 'beta' },
          result: { rounds_played: 1, deltas: [10, -10] },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))

  await page.goto(`/#/match/${matchId}`)
  await metadataObserved
  expect(replayRequests).toBe(0)
  releaseMetadata()
  await expect(page.getByText('metadata_alpha', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('metadata_beta', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('回放暂时不可用', { exact: true })).toBeVisible()
  expect(replayRequests).toBe(1)
})

test('MatchViewer replays live history sequentially and stays compact across viewports', async ({ page }) => {
  const matchId = 'mock-live-cursor-layout'
  const initialEvents = [
    { type: 'match_start', game_id: 'holdem', num_hands: 70 },
    { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [20000, 20000] },
    { type: 'deal_hole', hand: 0, holes: [['Ah', 'Kd'], ['Qs', 'Jc']] },
    { type: 'action', player: 0, action: 'call', amount: 50 },
  ]
  const reconnectedEvents = [
    ...initialEvents,
    { type: 'settle', hand: 0, winners: [0], deltas: [100, -100], pot: 200, reason: 'showdown' },
    { type: 'hand_start', hand: 1, sb: 1, bb: 0, chips: [20000, 20000] },
    { type: 'deal_hole', hand: 1, holes: [['9h', '9d'], ['8s', '8c']] },
    { type: 'action', player: 1, action: 'raise', amount: 200 },
  ]
  const runningMatch = {
    id: matchId,
    game_id: 'holdem',
    status: 'running',
    match_type: 'challenge',
    bot_a: { name: 'live_alpha', owner_name: 'alpha' },
    bot_b: { name: 'live_beta', owner_name: 'beta' },
    result: { rounds_played: 0, deltas: [0, 0] },
  }
  let replayRequests = 0

  const sse = await installControlledEventSource(page)

  await page.route(`**/api/matches/${matchId}/replay`, async (route) => {
    replayRequests += 1
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"live must not fetch replay"}' })
  })

  await page.route(`**/api/matches/${matchId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  // Hold the initial detail response until navigation has settled so the test
  // can observe the first rendered event before the real playback timer moves.
  let releaseMatchResponse!: () => void
  const matchResponseGate = new Promise<void>((resolve) => {
    releaseMatchResponse = resolve
  })
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await matchResponseGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: runningMatch,
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}' })
  })

  await page.setViewportSize({ width: 1440, height: 720 })
  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  releaseMatchResponse()
  expect(await sse.disconnect()).toBe(true)
  await expect(page.getByText('连接中', { exact: true })).toBeVisible()
  await expect(page.getByText('加载中…', { exact: true })).toHaveCount(0)
  expect(await sse.emit({ type: 'snapshot', match: runningMatch, events: initialEvents })).toBe(true)
  await expect(page.getByText('事件 1/4', { exact: true })).toBeVisible()
  expect(replayRequests).toBe(0)
  await expect(page.getByText('第 1/70 手', { exact: true })).toBeVisible()

  // 即使自动播放已经追到当前尾部，后续事件也必须先增加分母，再按速度推进。
  await page.getByRole('combobox').filter({ hasText: '1x' }).click()
  await page.getByRole('option', { name: '0.5x', exact: true }).click()
  await expect(page.getByText('事件 4/4 · 直播', { exact: true })).toBeVisible({ timeout: 6_000 })
  expect(await sse.emit(reconnectedEvents[4])).toBe(true)
  await expect(page.getByText('事件 4/5', { exact: true })).toBeVisible()
  await expect(page.getByText('事件 5/5 · 直播', { exact: true })).toBeVisible({ timeout: 2_500 })
  await page.getByRole('button', { name: '暂停', exact: true }).click()

  expect(await sse.disconnect()).toBe(true)
  await expect(page.getByText('连接中', { exact: true })).toBeVisible()
  expect(await sse.emit({
    type: 'snapshot',
    match: {
      id: matchId,
      game_id: 'holdem',
      status: 'running',
      match_type: 'challenge',
      bot_a: { name: 'live_alpha', owner_name: 'alpha' },
      bot_b: { name: 'live_beta', owner_name: 'beta' },
      result: { rounds_played: 0, deltas: [0, 0] },
    },
    events: reconnectedEvents,
  })).toBe(true)
  await expect(page.getByText('直播中', { exact: true })).toBeVisible()
  await expect(page.getByText('事件 5/8', { exact: true })).toBeVisible()
  await expect(page.getByText('第 1/70 手', { exact: true })).toBeVisible()

  expect(await sse.emit({
    type: 'match_end', winner: 0, reason: 'completed', deltas: [100, -100],
  })).toBe(true)
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await expect(page.getByText('事件 5/9', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /已结束 · 剩余 4 个事件 · 跳到结局/ })).toBeVisible()
  await page.waitForTimeout(850)
  await expect(page.getByText('事件 5/9', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '播放', exact: true }).click()
  await expect(page.getByText('事件 6/9', { exact: true })).toBeVisible({ timeout: 2_500 })
  await expect(page.getByText('事件 7/9', { exact: true })).toBeVisible({ timeout: 2_500 })
  await expect(page.getByText('事件 8/9', { exact: true })).toBeVisible({ timeout: 2_500 })
  await expect(page.getByText('事件 9/9', { exact: true })).toBeVisible({ timeout: 2_500 })
  await expect(page.getByText('第 2/70 手', { exact: true })).toBeVisible()

  // 终态到尾后可从事件 1 重播，也可显式跳到结局；两者都不隐式跳转。
  await page.getByRole('button', { name: '播放', exact: true }).click()
  await expect(page.getByText('事件 1/9', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '暂停', exact: true }).click()
  await page.getByRole('button', { name: /已结束 · 剩余 8 个事件 · 跳到结局/ }).click()
  await expect(page.getByText('事件 9/9', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '上一个事件', exact: true }).click()
  await expect(page.getByText('事件 8/9', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /已结束 · 剩余 1 个事件 · 跳到结局/ }).click()
  await expect(page.getByText('事件 9/9', { exact: true })).toBeVisible()

  const canvas = page.getByRole('img', { name: 'holdem 对局画面' })
  const canvasBox = await canvas.boundingBox()
  const timelineBox = await page.getByTestId('match-timeline').boundingBox()
  const resultCardBox = await page.getByTestId('match-result-card').boundingBox()
  const commentsCardBox = await page.getByTestId('comments-card').boundingBox()
  expect(canvasBox?.width ?? 0).toBeGreaterThanOrEqual(780)
  expect((canvasBox?.width ?? 0) / (canvasBox?.height ?? 1)).toBeCloseTo(16 / 9, 1)
  expect(timelineBox?.width ?? 0).toBeGreaterThanOrEqual(270)
  expect(timelineBox?.width ?? 0).toBeLessThanOrEqual(310)
  expect(resultCardBox?.height ?? 999).toBeLessThanOrEqual(110)
  expect(commentsCardBox?.height ?? 999).toBeLessThanOrEqual(140)

  await page.evaluate(() => window.scrollTo(0, 360))
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(100)
  await expect.poll(async () => (await page.getByTestId('match-timeline').boundingBox())?.y ?? -1)
    .toBeGreaterThanOrEqual(20)
  expect((await page.getByTestId('match-timeline').boundingBox())?.y ?? 999).toBeLessThanOrEqual(30)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.evaluate(() => window.scrollTo(0, 0))
  const mobileCanvasBox = await canvas.boundingBox()
  expect(mobileCanvasBox?.width ?? 0).toBeGreaterThanOrEqual(356)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await monitor.expectClean()
})

test('private Bot debug stays folded, safe, bounded on mobile, and hidden when unauthorized', async ({ page }) => {
  await loginThroughUi(page, USER)
  const monitor = monitorBrowser(page)
  const allowedId = 'mock-private-debug-allowed'
  const deniedId = 'mock-private-debug-denied'
  const staleId = 'mock-private-debug-stale'
  const longToken = `LONG_${'A'.repeat(3_950)}`
  const replay = [
    { type: 'match_start', game_id: 'holdem', num_hands: 70 },
    { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [20000, 20000] },
    { type: 'match_end', winner: 0, reason: 'completed', deltas: [100, -100] },
  ]

  const detail = (id: string, allowed: boolean) => ({
    match: {
      id,
      game_id: 'holdem',
      status: 'completed',
      match_type: 'challenge',
      winner: 0,
      reason: 'completed',
      can_view_debug: allowed,
      bot_a: { name: 'debug_alpha', owner_name: 'alpha' },
      bot_b: { name: 'debug_beta', owner_name: 'beta' },
      result: { rounds_played: 1, deltas: [100, -100], normalized_delta: 1 },
    },
  })

  for (const id of [allowedId, deniedId, staleId]) {
    await routeStructuredReplay(page, id, replay)
    await page.route(`**/api/matches/${id}/view`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    })
  }
  await page.route(`**/api/matches/${allowedId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(detail(allowedId, true)),
    })
  })
  let deniedDetailRequests = 0
  let releaseDeniedDetail!: () => void
  const deniedDetailGate = new Promise<void>((resolve) => { releaseDeniedDetail = resolve })
  await page.route(`**/api/matches/${deniedId}`, async (route) => {
    deniedDetailRequests += 1
    await deniedDetailGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(detail(deniedId, false)),
    })
  })
  await page.route(`**/api/matches/${staleId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(detail(staleId, true)),
    })
  })
  let allowedDebugRequests = 0
  await page.route(`**/api/matches/${allowedId}/debug`, async (route) => {
    allowedDebugRequests += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match_id: allowedId,
        entries: [
          {
            seat: 0,
            turn: 1,
            leg: null,
            debug: `${longToken}<img src=x onerror="window.__debugXss=true"> https://evil.test/`,
          },
          { seat: 1, turn: 2, leg: 1, debug: { branch: 'defend', score: 0.73 } },
        ],
        entry_count: 2,
        total_bytes: 4_096,
        dropped_count: 3,
        updated_at: '2026-08-10T12:00:00',
      }),
    })
  })
  let deniedDebugRequests = 0
  await page.route(`**/api/matches/${deniedId}/debug`, async (route) => {
    deniedDebugRequests += 1
    await route.fulfill({ status: 403, contentType: 'application/json', body: '{"detail":"denied"}' })
  })
  let staleDebugRequests = 0
  let releaseStaleDebug!: () => void
  const staleDebugGate = new Promise<void>((resolve) => { releaseStaleDebug = resolve })
  await page.route(`**/api/matches/${staleId}/debug`, async (route) => {
    staleDebugRequests += 1
    await staleDebugGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match_id: staleId,
        entries: [{ seat: 0, turn: 9, leg: null, debug: 'STALE_PRIVATE_CONTENT' }],
        entry_count: 1,
        total_bytes: 21,
        dropped_count: 0,
        updated_at: '2026-08-10T12:00:00',
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}' })
  })
  await page.route('**/api/likes/status?target_type=match&target_id=*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"liked":false}' })
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/match/${allowedId}`)
  const panel = page.getByTestId('bot-debug-panel')
  await expect(panel).toBeVisible()
  const longDebug = panel.locator('pre').filter({ hasText: 'LONG_AAAAA' })
  await expect(longDebug).not.toBeVisible()
  await page.getByRole('button', { name: '展开 Bot 调试信息', exact: true }).click()
  await expect(longDebug).toBeVisible()
  await expect(longDebug).toContainText(longToken.slice(0, 64))
  await expect(panel).toContainText('有 3 条内容因安全或容量上限未保存')
  expect(allowedDebugRequests).toBe(1)
  expect(await panel.locator('a').count()).toBe(0)
  expect(await panel.locator('img').count()).toBe(0)
  expect(await page.evaluate(() => Boolean((window as Window & { __debugXss?: boolean }).__debugXss))).toBe(false)

  await page.getByRole('tab', { name: /座位 2/ }).click()
  await expect(panel.getByText('"branch": "defend"', { exact: false })).toBeVisible()
  await page.getByRole('tab', { name: /座位 1/ }).click()

  await page.setViewportSize({ width: 390, height: 844 })
  await panel.scrollIntoViewIfNeeded()
  const panelBox = await panel.boundingBox()
  expect(panelBox?.width ?? 999).toBeLessThanOrEqual(390)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth))
    .toBeLessThanOrEqual(1)
  await page.evaluate(() => window.scrollBy(0, 220))
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth))
    .toBeLessThanOrEqual(1)

  // 上一局的私有响应在路由切换后才返回，不得渲染到新对局。
  await page.goto(`/#/match/${staleId}`)
  await expect.poll(() => staleDebugRequests).toBe(1)
  await page.goto(`/#/match/${deniedId}`)
  await expect.poll(() => deniedDetailRequests).toBe(1)
  // 新路由的权限详情尚未返回时，旧对局 panel 也必须同步从 DOM 消失。
  await expect(page.getByTestId('bot-debug-panel')).toHaveCount(0)
  releaseStaleDebug()
  await expect(page.getByText('STALE_PRIVATE_CONTENT', { exact: true })).toHaveCount(0)
  releaseDeniedDetail()
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await expect(page.getByTestId('bot-debug-panel')).toHaveCount(0)
  await expect(page.getByText('STALE_PRIVATE_CONTENT', { exact: true })).toHaveCount(0)
  await page.waitForTimeout(150)
  expect(deniedDebugRequests).toBe(0)
  await monitor.expectClean([{
    kind: 'requestfailed',
    method: 'GET',
    pathname: `/api/matches/${allowedId}`,
    errorText: 'net::ERR_ABORTED',
  }])
})

test('MatchViewer playback clock cannot be starved by continuous SSE traffic', async ({ page }) => {
  const matchId = 'mock-live-continuous-clock'
  const initialEvents = [
    { type: 'match_start', game_id: 'holdem', num_hands: 70 },
    { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
  ]
  const streamEvents = Array.from({ length: 40 }, (_, index) => ({
    type: 'action',
    player: index % 2,
    action: 'check',
    amount: 0,
  }))
  const runningMatch = {
    id: matchId,
    game_id: 'holdem',
    status: 'running',
    match_type: 'challenge',
    bot_a: { name: 'clock_alpha', owner_name: 'alpha' },
    bot_b: { name: 'clock_beta', owner_name: 'beta' },
    result: { rounds_played: 0, deltas: [0, 0] },
  }
  const sse = await installControlledEventSource(page)
  let releaseMatchResponse!: () => void
  const matchResponseGate = new Promise<void>((resolve) => {
    releaseMatchResponse = resolve
  })

  await page.route(`**/api/matches/${matchId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await matchResponseGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: runningMatch,
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))

  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  releaseMatchResponse()
  expect(await sse.emit({ type: 'snapshot', match: runningMatch, events: initialEvents })).toBe(true)
  const position = page.getByTestId('playback-position')
  await expect(position).toHaveText('事件 1/2')
  expect(await sse.stream(streamEvents, 50)).toBe(true)
  await expect.poll(async () => Number(await sse.sent()), { timeout: 2_000 })
    .toBeGreaterThanOrEqual(18)
  const duringStream = Number((await position.textContent())?.match(/事件 (\d+)\//)?.[1] ?? 0)
  expect(Number(await sse.sent())).toBeLessThan(40)
  expect(duringStream, 'playback cursor must advance while SSE still changes total').toBeGreaterThan(1)
  await expect.poll(async () => Number(await sse.sent()), { timeout: 3_000 })
    .toBe(40)
  await monitor.expectClean()
})

test('MatchViewer preserves more than 4000 events across reconnect snapshots', async ({ page }) => {
  const matchId = 'mock-live-long-prefix'
  const initialEvents = [
    { type: 'match_start', game_id: 'holdem', num_hands: 70 },
    ...Array.from({ length: 4_100 }, (_, index) => ({
      type: 'action', player: index % 2, action: 'check', amount: 0,
    })),
  ]
  const grownEvents = [
    ...initialEvents,
    ...Array.from({ length: 250 }, (_, index) => ({
      type: 'action', player: index % 2, action: 'check', amount: 0,
    })),
  ]
  const sse = await installControlledEventSource(page)
  let releaseMatchResponse!: () => void
  const matchResponseGate = new Promise<void>((resolve) => {
    releaseMatchResponse = resolve
  })
  const runningMatch = {
    id: matchId,
    game_id: 'holdem',
    status: 'running',
    match_type: 'challenge',
    bot_a: { name: 'long_alpha', owner_name: 'alpha' },
    bot_b: { name: 'long_beta', owner_name: 'beta' },
    result: { rounds_played: 0, deltas: [0, 0] },
  }

  await page.route(`**/api/matches/${matchId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await matchResponseGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match: runningMatch }),
    })
  })
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))

  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  releaseMatchResponse()
  await expect(page.getByText('直播中', { exact: true })).toBeVisible()
  expect(await sse.emit({ type: 'snapshot', match: runningMatch, events: initialEvents })).toBe(true)
  const position = page.getByTestId('playback-position')
  await expect(position).toHaveText('事件 1/4101')
  await page.getByRole('button', { name: '暂停', exact: true }).click()
  const cursorBeforeReconnect = Number((await position.textContent())?.match(/事件 (\d+)\//)?.[1] ?? 0)
  expect(await sse.disconnect()).toBe(true)
  await expect(page.getByText('连接中', { exact: true })).toBeVisible()
  expect(await sse.emit({ type: 'snapshot', match: runningMatch, events: grownEvents })).toBe(true)
  await expect(position).toHaveText(`事件 ${cursorBeforeReconnect}/4351`)
  await expect(page.getByText('动作时序 (1/4351)', { exact: true })).toBeVisible()
  await monitor.expectClean()
})

test('Holdem aborts before hand start do not claim hand 1 of 70', async ({ page }) => {
  const fixtures = [
    { id: 'mock-zero-hand-admin-abort', reason: 'admin_aborted' },
    { id: 'mock-zero-hand-platform-abort', reason: 'platform_error' },
  ] as const
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}',
  }))
  for (const fixture of fixtures) {
    await routeStructuredReplay(page, fixture.id, [
      { type: 'match_start', game_id: 'holdem', num_hands: 70 },
      { type: 'error', reason: fixture.reason },
    ])
    await page.route(`**/api/matches/${fixture.id}/view`, async (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: '{"ok":true}',
    }))
    await page.route(`**/api/matches/${fixture.id}`, async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: fixture.id,
          game_id: 'holdem',
          status: 'aborted',
          reason: fixture.reason,
          match_type: 'challenge',
          bot_a: { name: 'abort_alpha', owner_name: 'alpha' },
          bot_b: { name: 'abort_beta', owner_name: 'beta' },
          result: { rounds_played: 0, deltas: [0, 0] },
        },
      }),
    }))
  }

  const monitor = monitorBrowser(page)
  for (const fixture of fixtures) {
    await page.goto(`/#/match/${fixture.id}`)
    await expect(page.getByText('已中止', { exact: true })).toBeVisible()
    await expect(page.getByText(/\/70 手/)).toHaveCount(0)
    await page.getByRole('button', { name: '下一个事件', exact: true }).click()
    await expect(page.getByText(/\/70 手/)).toHaveCount(0)
    await expect(page.getByTestId('holdem-position-overview')).toContainText('未完成任何一手')
    await expect(page.getByTestId('holdem-position-overview')).not.toContainText('当前手 1')
  }
  await monitor.expectClean()
})

test('terminal reason presentation keeps normal adjudication neutral and faults dangerous', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const viewerFixtures = [
    {
      id: 'mock-reason-five', gameId: 'gomoku', reason: 'five',
      label: '连成五子', tone: 'neutral', winner: 0,
      events: [{ type: 'match_end', winner: 0, reason: 'five' }],
    },
    {
      id: 'mock-reason-protocol', gameId: 'gomoku', reason: 'protocol_error',
      label: 'Bot 响应协议错误', tone: 'danger', winner: 1,
      events: [{ type: 'match_end', winner: 1, reason: 'protocol_error' }],
    },
    {
      id: 'mock-reason-legacy', gameId: 'gomoku', reason: 'legacy_internal_reason',
      label: '已完成', tone: 'neutral', winner: 0,
      events: [{ type: 'match_end', winner: 0, reason: 'legacy_internal_reason' }],
    },
    {
      id: 'mock-reason-pencil-illegal', gameId: 'pencil', reason: 'illegal',
      label: '非法连边', tone: 'danger', winner: 1,
      events: [
        { type: 'match_start', n_dots: 6, size: 11 },
        { type: 'time_used', seat: 0, used: 1.2, remaining: 898.8, budget: 900 },
        { type: 'illegal', player: 0, why: 'pass' },
        { type: 'match_end', winner: 1, reason: 'illegal', scores: [0, 0] },
      ],
    },
  ] as const
  const humanFixtures = {
    'mock-human-score': {
      gameId: 'pencil', reason: 'score', label: '按最终得分判定', tone: 'neutral', winner: 1,
    },
    'mock-human-illegal': {
      gameId: 'pencil', reason: 'illegal', label: '非法连边', tone: 'danger', winner: 0,
    },
  } as const

  // WebSocket interception must be installed before the first Vite navigation.
  await page.routeWebSocket(
    (url) => Object.hasOwn(humanFixtures, url.pathname.split('/').at(-2) || ''),
    (socket) => {
      const id = new URL(socket.url()).pathname.split('/').at(-2) as keyof typeof humanFixtures
      const fixture = humanFixtures[id]
      setTimeout(() => socket.send(JSON.stringify({
        type: 'snapshot',
        match: {
          id,
          game_id: fixture.gameId,
          status: 'completed',
          reason: fixture.reason,
          match_type: 'human',
          human_seat: 1,
          winner: fixture.winner,
          bot_a: { name: 'reason_bot', owner_name: 'alpha' },
          bot_b: { owner_name: 'human_player', is_human: true },
          result: { rounds_played: 1, deltas: fixture.winner === 0 ? [1, -1] : [-1, 1] },
        },
        events: [
          ...(id === 'mock-human-illegal' ? [
            { type: 'time_used', seat: 0, used: 2, remaining: 898, budget: 900 },
            { type: 'illegal', player: 0, why: 'illegal_move' },
          ] : []),
          {
            type: 'match_end',
            winner: fixture.winner,
            reason: fixture.reason,
            scores: fixture.gameId === 'pencil' ? [4, 9] : undefined,
          },
        ],
      })), 0)
    },
  )

  for (const fixture of viewerFixtures) {
    await routeStructuredReplay(page, fixture.id, fixture.events)
    await page.route(`**/api/matches/${fixture.id}/view`, async (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: '{"ok":true}',
    }))
    await page.route(`**/api/matches/${fixture.id}`, async (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: fixture.id,
          game_id: fixture.gameId,
          status: 'completed',
          reason: fixture.reason,
          winner: fixture.winner,
          match_type: 'challenge',
          bot_a: { name: 'reason_a', owner_name: 'alpha' },
          bot_b: { name: 'reason_b', owner_name: 'beta' },
          result: {
            rounds_played: 1,
            deltas: fixture.winner === 0 ? [1, -1] : [-1, 1],
          },
        },
      }),
    }))
  }
  await page.route('**/api/comments?*', async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ comments: [], count: 0, total: 0 }),
  }))

  for (const fixture of viewerFixtures) {
    await page.goto(`/#/match/${fixture.id}`)
    const reason = page.getByTestId('terminal-reason')
    await expect(reason).toHaveText(fixture.label)
    await expect(reason).toHaveAttribute('data-tone', fixture.tone)
    await expect(page.locator('main')).not.toContainText('legacy_internal_reason')
    if (fixture.reason === 'five') {
      // The generic timeline must retain the game's richer match_end description.
      await expect(page.getByText('结束 · 座1获胜 · 连成五子', { exact: true })).toBeVisible()
    }
    if (fixture.id === 'mock-reason-pencil-illegal') {
      for (let index = 1; index < fixture.events.length; index += 1) {
        await page.getByRole('button', { name: '下一个事件', exact: true }).click()
      }
      await expect(page.getByText('座1 · 已用 1.2 秒 · 剩余 898.8 秒', { exact: true })).toBeVisible()
      await expect(page.getByText('座1 · 错误让行', { exact: true })).toBeVisible()
      await expect(page.getByText('illegal', { exact: true })).toHaveCount(0)
      await expect(page.getByText('time_used', { exact: true })).toHaveCount(0)
    }
  }

  // Active rows historically carried the storage default reason='completed'.
  // Status is authoritative: a running match must not claim normal completion.
  const runningViewerId = 'mock-reason-running-default'
  const runningMatch = {
    id: runningViewerId,
    game_id: 'gomoku',
    status: 'running',
    reason: 'completed',
    winner: null,
    match_type: 'challenge',
    bot_a: { name: 'running_a', owner_name: 'alpha' },
    bot_b: { name: 'running_b', owner_name: 'beta' },
    result: { rounds_played: 0, deltas: [0, 0] },
  }
  await page.route(`**/api/matches/${runningViewerId}/view`, async (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"ok":true}',
  }))
  await page.route(`**/api/matches/${runningViewerId}/events`, async (route) => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    headers: { 'cache-control': 'no-cache' },
    body: `data: ${JSON.stringify({ type: 'snapshot', match: runningMatch, events: [] })}\n\n`,
  }))
  await page.route(`**/api/matches/${runningViewerId}`, async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ match: runningMatch }),
  }))
  await page.goto(`/#/match/${runningViewerId}`)
  await expect(page.getByText('对局进行中', { exact: true })).toBeVisible()
  await expect(page.getByTestId('terminal-reason')).toHaveCount(0)
  await expect(page.locator('main')).not.toContainText('正常结束')

  for (const [id, fixture] of Object.entries(humanFixtures)) {
    // Stay in the same SPA document: reloading the Vite document tears down its
    // routed sockets before the second mocked human channel can deliver a snapshot.
    await page.evaluate((matchId) => { window.location.hash = `#/play/${matchId}` }, id)
    await expect(page).toHaveURL(new RegExp(`#\\/play\\/${id}$`))
    const reason = page.getByTestId('terminal-reason')
    await expect(reason).toHaveText(`（${fixture.label}）`)
    await expect(reason).toHaveAttribute('data-tone', fixture.tone)
    if (id === 'mock-human-illegal') {
      await expect(page.getByText('座1 · 非法连边', { exact: true })).toBeVisible()
      await expect(page.getByText('座1 · 已用 2 秒 · 剩余 898 秒', { exact: true })).toBeVisible()
      await expect(page.getByText('illegal', { exact: true })).toHaveCount(0)
      await expect(page.getByText('time_used', { exact: true })).toHaveCount(0)
    }
  }

  await loginThroughUi(page, ADMIN)
  await page.route('**/api/matches?*', async (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      matches: [
        {
          id: 'admin-normal-majority', game_id: 'pencil', status: 'completed',
          reason: 'majority', match_type: 'contest', winner: 0,
          bot_a_id: 1, bot_b_id: 2,
          bot_a: { id: 1, name: 'normal_a', owner_name: 'normal_owner_a', owner_display: 'Normal Owner A', is_human: false },
          bot_b: { id: 2, name: 'normal_b', owner_name: 'normal_owner_b', owner_display: 'Normal Owner B', is_human: false },
          result: { rounds_played: 12, deltas: [1, -1] }, created_at: '2026-08-09T12:00:00Z',
          contest_id: 1,
        },
        {
          id: 'admin-danger-platform', game_id: 'holdem', status: 'aborted',
          reason: 'platform_error', match_type: 'challenge', winner: null,
          bot_a_id: 3, bot_b_id: 4,
          bot_a: { id: 3, name: 'fault_a', owner_name: 'fault_owner_a', owner_display: 'Fault Owner A', is_human: false },
          bot_b: { id: 4, name: 'fault_b', owner_name: 'fault_owner_b', owner_display: 'Fault Owner B', is_human: false },
          result: { rounds_played: 0, deltas: [0, 0] }, created_at: '2026-08-09T12:01:00Z',
          contest_id: null,
        },
        {
          id: 'admin-running-default', game_id: 'gomoku', status: 'running',
          reason: 'completed', match_type: 'challenge', winner: null,
          bot_a_id: 5, bot_b_id: 6,
          bot_a: { id: 5, name: 'running_default_a', owner_name: 'running_owner_a', owner_display: 'Running Owner A', is_human: false },
          bot_b: { id: 6, name: 'running_default_b', owner_name: 'running_owner_b', owner_display: 'Running Owner B', is_human: false },
          result: { rounds_played: 4, deltas: [0, 0] }, created_at: '2026-08-09T12:02:00Z',
          contest_id: null,
        },
      ],
      total: 3,
    }),
  }))
  await page.goto('/#/admin?tab=matches')
  await expect(page.getByTestId('terminal-reason').filter({ hasText: '已取得过半格子' }))
    .toHaveAttribute('data-tone', 'neutral')
  await expect(page.getByTestId('terminal-reason').filter({ hasText: '平台运行异常' }))
    .toHaveAttribute('data-tone', 'danger')
  const runningRow = page.getByRole('row').filter({ hasText: 'running_default_a' })
  await expect(runningRow).toContainText('Running Owner A · @running_owner_a')
  await expect(runningRow).toContainText('Running Owner B · @running_owner_b')
  await expect(runningRow.locator('[data-match-nature="challenge"]')).toHaveText('用户挑战')
  await expect(runningRow.getByTestId('terminal-reason')).toHaveCount(0)
  await expect(runningRow).not.toContainText('正常结束')
  await monitor.expectClean()
})

test('Pencil clock initializes the untouched seat and renders a first-event timeout', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"comments":[],"count":0,"total":0}',
    })
  })

  const openMockClockMatch = async (
    matchId: string,
    events: Array<Record<string, unknown>>,
  ) => {
    await page.route(`**/api/matches/${matchId}/view`, async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    })
    await page.route(`**/api/matches/${matchId}/events`, async (route) => {
      const stream = [
        ...events,
        { type: 'match_end', winner: 1, reason: 'completed', deltas: [-1, 1] },
      ]
        .map((event) => `data: ${JSON.stringify(event)}\n\n`)
        .join('')
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream })
    })
    await page.route(`**/api/matches/${matchId}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          match: {
            id: matchId,
            game_id: 'pencil',
            status: 'running',
            match_type: 'challenge',
            bot_a_id: 41,
            bot_b_id: 42,
            bot_a: { id: 41, name: 'clock_red', display_name: 'Clock Red', owner_name: 'clock_owner_red', owner_display: 'Clock Owner Red', is_human: false },
            bot_b: { id: 42, name: 'clock_blue', display_name: 'Clock Blue', owner_name: 'clock_owner_blue', owner_display: 'Clock Owner Blue', is_human: false },
          },
        }),
      })
    })

    await page.goto(`/#/match/${matchId}`)
    await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  }

  await openMockClockMatch('qa-clock-first-used', [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    { type: 'time_used', seat: 0, used: 1, remaining: 899, budget: 900 },
  ])
  await expect(page.getByText('14:59', { exact: true })).toBeVisible()
  // Seat 1 has not acted yet: its clock must start at the event's full budget,
  // never at the reducer's tuple default of zero.
  await expect(page.getByText('15:00', { exact: true })).toBeVisible()

  await openMockClockMatch('qa-clock-first-timeout', [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    { type: 'time_out', seat: 0, used: 900, budget: 900 },
  ])
  const timeoutBadge = page.getByText('超时', { exact: true })
  await expect(timeoutBadge).toBeVisible()
  // The clock and badge share one row, so assert their text together;
  // an exact `getByText('0:00')` cannot match an element whose full text is
  // `0:00超时`. Whitespace is layout-only, while both values and their order stay exact.
  await expect(timeoutBadge.locator('..')).toHaveText(/^\s*0:00\s*超时\s*$/)
  await expect(page.getByText('15:00', { exact: true })).toBeVisible()
  await monitor.expectClean()
})

test('Pencil canvas geometry and hit testing match the interleaved judge coordinates', async ({ page }) => {
  await page.goto('/')
  const geometry = await page.evaluate(async (events) => {
    const module = await import('/src/games/pencil/canvas.ts')
    const layout = module.pencilCanvasLayout(660, 660, 11)
    const horizontal = module.pencilEdgeSegment(1, 0, 11, layout)
    const vertical = module.pencilEdgeSegment(0, 1, 11, layout)
    const box = module.pencilBoxRect(5, 5, layout)
    const scene = module.PencilCanvasRenderer.toScene(events)
    const opts = { width: 660, height: 660 }
    const pick = (x: number, y: number) => module.PencilCanvasRenderer.pick?.(
      layout.cx(x), layout.cy(y), scene, opts,
    ) ?? null
    return {
      horizontal,
      vertical,
      box,
      point0: { x: layout.cx(0), y: layout.cy(0) },
      point2: { x: layout.cx(2), y: layout.cy(2) },
      legal: pick(5, 4),
      subpixelTopEdge: module.PencilCanvasRenderer.pick?.(
        layout.cx(1), layout.cy(0) - 0.75, scene, opts,
      ) ?? null,
      productionBoxClick: pick(5, 5),
      dot: pick(4, 4),
      occupied: pick(3, 8),
      outside: module.PencilCanvasRenderer.pick?.(
        layout.ox - 1, layout.oy, scene, opts,
      ) ?? null,
    }
  }, pencilHumanIncidentPrefix())

  expect(geometry.horizontal).not.toBeNull()
  expect(geometry.horizontal).toMatchObject({ horizontal: true })
  expect(geometry.horizontal?.x1).toBeCloseTo(geometry.point0.x)
  expect(geometry.horizontal?.x2).toBeCloseTo(geometry.point2.x)
  expect(geometry.horizontal?.y1).toBeCloseTo(geometry.point0.y)
  expect(geometry.horizontal?.y2).toBeCloseTo(geometry.point0.y)

  expect(geometry.vertical).not.toBeNull()
  expect(geometry.vertical).toMatchObject({ horizontal: false })
  expect(geometry.vertical?.x1).toBeCloseTo(geometry.point0.x)
  expect(geometry.vertical?.x2).toBeCloseTo(geometry.point0.x)
  expect(geometry.vertical?.y1).toBeCloseTo(geometry.point0.y)
  expect(geometry.vertical?.y2).toBeCloseTo(geometry.point2.y)

  expect(geometry.box).not.toBeNull()
  expect(geometry.box?.width).toBeCloseTo((geometry.point2.x - geometry.point0.x))
  expect(geometry.box?.height).toBeCloseTo((geometry.point2.y - geometry.point0.y))
  expect(geometry.legal).toEqual({ x: 5, y: 4 })
  expect(geometry.subpixelTopEdge).toEqual({ x: 1, y: 0 })
  expect(geometry.productionBoxClick).toBeNull()
  expect(geometry.dot).toBeNull()
  expect(geometry.occupied).toBeNull()
  expect(geometry.outside).toBeNull()
})

test('Pencil human canvas rejects the production box-center click and stays square at all target widths', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-pencil-production-human-pick'
  const sentActions: Array<Record<string, unknown>> = []

  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${matchId}/play`,
    (socket) => {
      socket.onMessage((message) => {
        sentActions.push(JSON.parse(String(message)) as Record<string, unknown>)
      })
      setTimeout(() => {
        socket.send(JSON.stringify({
          type: 'snapshot',
          match: {
            id: matchId,
            game_id: 'pencil',
            status: 'running',
            match_type: 'human',
            human_seat: 1,
            bot_a: { name: 'pencil_reference', owner_name: 'tester1' },
            bot_b: { owner_name: 'tester2', is_human: true },
            result: { rounds_played: 11, deltas: [0, 0] },
          },
          events: pencilHumanIncidentPrefix(),
        }))
      }, 0)
    },
  )

  await page.setViewportSize({ width: 1312, height: 700 })
  await page.goto(`/#/play/${matchId}`)
  await expect(page.getByText('轮到你连边', { exact: true })).toBeVisible()
  const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
  const eventLog = page.getByTestId('human-event-log')
  const overview = page.getByTestId('pencil-position-overview')
  await expect(canvas).toBeVisible()
  await expect(overview).toBeVisible()

  for (const viewport of [
    { width: 2560, height: 1080 },
    { width: 1920, height: 1080 },
    { width: 2048, height: 1024 },
    { width: 2048, height: 1152 },
    { width: 1600, height: 900 },
    { width: 1536, height: 1080 },
    { width: 1366, height: 768 },
    { width: 1312, height: 700 },
    { width: 1024, height: 768 },
    { width: 844, height: 390 },
    { width: 390, height: 700 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewportSize(viewport)
    const bounds = await canvas.boundingBox()
    expect(bounds).not.toBeNull()
    expect(Math.abs((bounds?.width ?? 0) - (bounds?.height ?? 0))).toBeLessThanOrEqual(1)
    if (viewport.width >= 1536) {
      const logBounds = await eventLog.boundingBox()
      const overviewBounds = await overview.boundingBox()
      expect(logBounds).not.toBeNull()
      expect(overviewBounds).not.toBeNull()
      const boardTrackWidth = await page.getByTestId('human-canvas-layout').evaluate((element) => {
        const columns = getComputedStyle(element).gridTemplateColumns
          .split(/\s+/)
          .map((value) => Number.parseFloat(value))
        return columns[1] ?? 0
      })
      // 三栏的实际中轨已经扣除 shell、gutter、HUD、日志栏及系统 gap；棋盘应优先
      // 用满该语义轨道，同时仍受 52rem 和首屏可用高度上限约束。
      const expectedMax = Math.min(832, viewport.height - 256, boardTrackWidth)
      expect(bounds?.width ?? 9999).toBeLessThanOrEqual(expectedMax + 1)
      expect(bounds?.width ?? 0).toBeGreaterThanOrEqual(expectedMax - 2)
      expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
      expect((overviewBounds?.x ?? 9999) + (overviewBounds?.width ?? 0)).toBeLessThan(bounds?.x ?? 0)
      expect(overviewBounds?.height ?? 9999).toBeLessThanOrEqual((bounds?.height ?? 0) + 1)
      const containment = await overview.evaluate((element) => ({
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }))
      expect(containment.scrollHeight).toBeLessThanOrEqual(containment.clientHeight + 1)
      expect(logBounds?.x ?? 0).toBeGreaterThan((bounds?.x ?? 0) + (bounds?.width ?? 0))
      expect((logBounds?.y ?? 0) + (logBounds?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
    } else if (viewport.width >= 1280) {
      const logBounds = await eventLog.boundingBox()
      const overviewBounds = await overview.boundingBox()
      expect(logBounds).not.toBeNull()
      expect(overviewBounds).not.toBeNull()
      expect(Math.abs((overviewBounds?.x ?? 0) - (bounds?.x ?? 0))).toBeLessThanOrEqual(1)
      expect((overviewBounds?.y ?? 0) + (overviewBounds?.height ?? 0)).toBeLessThanOrEqual(bounds?.y ?? 0)
      expect(logBounds?.x ?? 0).toBeGreaterThan((bounds?.x ?? 0) + (bounds?.width ?? 0))
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${viewport.width}px human view overflow`).toBeLessThanOrEqual(1)
  }

  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
  await page.evaluate(() => window.scrollTo(0, 0))

  // 线上事故的旧输出是格心 (5,5)。格心、点、已占边、棋盘外四类误点
  // 现在都只提示，不产生任何 WebSocket 帧。
  const outsideBoard = await canvas.evaluate(async (element) => {
    const module = await import('/src/games/pencil/canvas.ts')
    const rect = element.getBoundingClientRect()
    const layout = module.pencilCanvasLayout(rect.width, rect.height, 11)
    return { x: Math.max(1, layout.ox - 4), y: layout.oy + layout.boardPx / 2 }
  })
  for (const position of [
    await pencilCanvasPoint(canvas, 5, 5), // 格心
    await pencilCanvasPoint(canvas, 4, 4), // 点
    await pencilCanvasPoint(canvas, 3, 8), // 事故前缀中已占边
    outsideBoard, // canvas 内、棋盘边框外
  ]) {
    await canvas.click({ position })
    await expect(page.getByText(/请选择一条尚未占用的边/)).toBeVisible()
    await expect(canvas).toHaveAttribute('data-pick-state', 'invalid')
  }
  await page.waitForTimeout(150)
  expect(sentActions).toHaveLength(0)

  // 相邻合法水平边 (5,4) 必须准确封装为唯一 response 信封。
  const legalEdge = await pencilCanvasPoint(canvas, 5, 4)
  await canvas.click({ position: legalEdge })
  await expect.poll(() => sentActions.length).toBe(1)
  expect(sentActions[0]).toEqual({ response: { x: 5, y: 4 } })
  await monitor.expectClean()
})

test('Pencil human pass request disables the board and submits the only legal pass envelope', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-pencil-human-pass'
  const sentActions: Array<Record<string, unknown>> = []
  const passEvents = [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    { type: 'move', player: 0, x: 0, y: 1, scored: false, scores: [0, 0], move_index: 1 },
    { type: 'move', player: 1, x: 4, y: 1, scored: false, scores: [0, 0], move_index: 2 },
    { type: 'move', player: 0, x: 1, y: 0, scored: false, scores: [0, 0], move_index: 3 },
    { type: 'move', player: 1, x: 6, y: 1, scored: false, scores: [0, 0], move_index: 4 },
    { type: 'move', player: 0, x: 1, y: 2, scored: false, scores: [0, 0], move_index: 5 },
    { type: 'move', player: 1, x: 8, y: 1, scored: false, scores: [0, 0], move_index: 6 },
    {
      type: 'move', player: 0, x: 2, y: 1, scored: true, scores: [1, 0], move_index: 7,
      closed_boxes: [{ x: 1, y: 1, owner: 0 }],
    },
    { type: 'turn', player: 1, pass_: 1, last: { x: 2, y: 1 }, scores: [1, 0] },
    {
      type: 'your_turn', player: 1,
      request: { x: 2, y: 1, pass: 1, me: 1, scores: [1, 0] },
    },
  ]

  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${matchId}/play`,
    (socket) => {
      socket.onMessage((message) => {
        sentActions.push(JSON.parse(String(message)) as Record<string, unknown>)
      })
      setTimeout(() => {
        socket.send(JSON.stringify({
          type: 'snapshot',
          match: {
            id: matchId,
            game_id: 'pencil',
            status: 'running',
            match_type: 'human',
            human_seat: 1,
            bot_a: { name: 'pencil_reference', owner_name: 'tester1' },
            bot_b: { owner_name: 'tester2', is_human: true },
          },
          events: passEvents,
        }))
      }, 0)
    },
  )

  await page.goto(`/#/play/${matchId}`)
  await expect(page.getByText('轮到你确认让行', { exact: true })).toBeVisible()
  const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
  await expect(canvas).toHaveAttribute('data-pick-state', 'inactive')
  const edge = await pencilCanvasPoint(canvas, 5, 4)
  await canvas.click({ position: edge })
  await page.waitForTimeout(150)
  expect(sentActions).toHaveLength(0)

  const passAction = page.getByTestId('pencil-pass-action')
  await expect(passAction).toContainText('对手围成了格，将继续连边')
  await page.getByRole('button', { name: '确认让行', exact: true }).click()
  await expect.poll(() => sentActions.length).toBe(1)
  expect(sentActions[0]).toEqual({ response: { x: -1, y: -1 } })
  await monitor.expectClean()
})

test('Pencil human canvas exposes legal edges to keyboard and screen-reader users', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-pencil-keyboard-action'
  const sentActions: Array<Record<string, unknown>> = []

  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${matchId}/play`,
    (socket) => {
      socket.onMessage((message) => {
        sentActions.push(JSON.parse(String(message)) as Record<string, unknown>)
      })
      setTimeout(() => socket.send(JSON.stringify({
        type: 'snapshot',
        match: {
          id: matchId,
          game_id: 'pencil',
          status: 'running',
          match_type: 'human',
          human_seat: 1,
          bot_a: { name: 'pencil_reference', owner_name: 'tester1' },
          bot_b: { owner_name: 'tester2', is_human: true },
        },
        events: [
          { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
          { type: 'turn', player: 1, pass_: 0, last: { x: -1, y: -1 }, scores: [0, 0] },
          {
            type: 'your_turn', player: 1,
            request: { x: -1, y: -1, pass: 0, me: 1, scores: [0, 0] },
          },
        ],
      })), 0)
    },
  )

  await page.goto(`/#/play/${matchId}`)
  const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
  await expect(canvas).toHaveAttribute('role', 'button')
  await expect(canvas).toHaveAttribute('tabindex', '0')
  await canvas.focus()
  await canvas.press('ArrowRight')
  await expect(canvas).toHaveAttribute('aria-label', /当前位置 \(0,1\)/)
  await canvas.press('Enter')
  await expect.poll(() => sentActions.length).toBe(1)
  expect(sentActions[0]).toEqual({ response: { x: 0, y: 1 } })
  await monitor.expectClean()
})

test('Pencil human canvas replaces an equal-length snapshot and finishes animation through parent rerenders', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-pencil-equal-length-snapshot'
  const sentActions: Array<Record<string, unknown>> = []
  let sendServerEvent: (event: Record<string, unknown>) => void = () => {
    throw new Error('Pencil WebSocket is not connected')
  }
  const match = {
    id: matchId,
    game_id: 'pencil',
    status: 'running',
    match_type: 'human',
    human_seat: 1,
    bot_a: { name: 'pencil_reference', owner_name: 'tester1' },
    bot_b: { owner_name: 'tester2', is_human: true },
  }
  const eventsWithEdge = (x: number, y: number) => [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    { type: 'move', player: 0, x, y, scored: false, scores: [0, 0], move_index: 1 },
    { type: 'turn', player: 1, pass_: 0, last: { x, y }, scores: [0, 0] },
    {
      type: 'your_turn', player: 1,
      request: { x, y, pass: 0, me: 1, scores: [0, 0] },
    },
  ]

  await page.routeWebSocket(
    (url) => url.pathname === `/api/matches/${matchId}/play`,
    (socket) => {
      sendServerEvent = (event) => socket.send(JSON.stringify(event))
      socket.onMessage((message) => {
        sentActions.push(JSON.parse(String(message)) as Record<string, unknown>)
      })
      setTimeout(() => socket.send(JSON.stringify({
        type: 'snapshot',
        match,
        // 初始快照尚未轮到真人；随后 live your_turn 会启动 500ms 倒计时重渲染。
        events: eventsWithEdge(1, 0).slice(0, 3),
      })), 0)
    },
  )

  await page.goto(`/#/play/${matchId}`)
  const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
  await expect(canvas).toBeVisible()
  sendServerEvent(eventsWithEdge(1, 0)[3])
  await expect(page.getByText('轮到你连边', { exact: false })).toBeVisible()

  // 同长度权威 snapshot 必须重算 scene，不能只凭 length 继续使用旧棋盘。
  await page.waitForTimeout(300)
  sendServerEvent({ type: 'snapshot', match, events: eventsWithEdge(5, 4) })
  // snapshot 已把 (5,4) 标为占用、释放旧 (1,0)。若宿主仍持旧 scene，
  // 第一次会显示可点并错误发送，第二次则会显示不可点。
  const newlyOccupied = await pencilCanvasPoint(canvas, 5, 4)
  await canvas.click({ position: newlyOccupied })
  await page.waitForTimeout(150)
  expect(sentActions).toHaveLength(0)
  const newlyFree = await pencilCanvasPoint(canvas, 1, 0)
  await canvas.hover({ position: newlyFree })
  await expect(canvas).toHaveAttribute('data-pick-state', 'valid')

  // 下一次倒计时 tick 会落在这段 0.5s 动画中；父 render 不得 cleanup/freeze timeline。
  sendServerEvent({
    type: 'move', player: 1, x: 1, y: 0, scored: false, scores: [0, 0], move_index: 2,
  })
  await expect(canvas).toHaveAttribute('data-animation-state', 'running')
  await expect(canvas).toHaveAttribute('data-animation-state', 'settled', { timeout: 2_000 })
  await monitor.expectClean()
})

test('Pencil replay gives the square board priority while the timeline remains usable during page scroll', async ({ page }) => {
  const monitor = monitorBrowser(page)
  // 线上 Safari 对局 20260810143624-4149d6a3：此前宽屏方形棋盘按剩余横向
  // 空间放大到 1200px+，首屏只能看到上半局面；完整时序又把右栏撑出视口。
  // fixture 使用上方从该对局提取的完整54步轨迹，并确定性重建原206个公开事件。
  const matchId = '20260810143624-4149d6a3'
  const events = pencilProductionReplay143624()
  expect(events).toHaveLength(206)
  const replayMoves = events.filter((event) => event.type === 'move')
  const replayPasses = events.filter((event) => event.type === 'pass')
  expect(replayMoves).toHaveLength(54)
  expect(replayPasses).toHaveLength(14)
  const seenEdges = new Set<string>()
  let verifiedScores: [number, number] = [0, 0]
  let verifiedBoxes = 0
  replayMoves.forEach((event, index) => {
    const player = Number(event.player)
    const x = Number(event.x)
    const y = Number(event.y)
    const scores = event.scores as [number, number]
    const closedBoxes = event.closed_boxes as Array<{ x: number; y: number; owner: number }>
    expect((x + y) % 2, `move ${index + 1} must target an edge`).toBe(1)
    expect(seenEdges.has(`${x},${y}`), `move ${index + 1} must be unique`).toBe(false)
    seenEdges.add(`${x},${y}`)
    const gained = scores[player] - verifiedScores[player]
    expect(gained, `move ${index + 1} score delta`).toBe(closedBoxes.length)
    expect(scores[1 - player], `move ${index + 1} opponent score`).toBe(verifiedScores[1 - player])
    expect(closedBoxes.every((box) => box.owner === player), `move ${index + 1} box owner`).toBe(true)
    verifiedBoxes += closedBoxes.length
    verifiedScores = [...scores]
  })
  expect(verifiedScores).toEqual([4, 13])
  expect(verifiedBoxes).toBe(17)
  const firstScoringMoveIndex = events.findIndex((event) => event.type === 'move' && event.move_index === 24)
  expect(firstScoringMoveIndex).toBeGreaterThan(1)
  expect(events[firstScoringMoveIndex - 2]).toMatchObject({ type: 'turn', scores: [0, 0] })
  expect(events[firstScoringMoveIndex]).toMatchObject({ type: 'move', scores: [0, 1] })
  await routeStructuredReplay(page, matchId, events)
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
          game_id: 'pencil',
          status: 'completed',
          match_type: 'challenge',
          winner: 1,
          reason: 'majority',
          bot_a: { name: 'admin_pencil', owner_name: 'admin' },
          bot_b: { name: 'tester11_pencil', owner_name: 'tester11' },
          result: { rounds_played: 54, deltas: [-9, 9], normalized_delta: -9 },
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

  // 附件为 2048×1152 Safari 窗口；Chromium 布局回归使用同宽高，并用
  // 1024px 高度模拟浏览器 chrome 进一步压缩后的网页可用区域。
  await page.setViewportSize({ width: 2048, height: 1024 })
  await page.goto(`/#/match/${matchId}`)
  const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
  const timeline = page.getByTestId('match-timeline')
  const overview = page.getByTestId('pencil-position-overview')
  await expect(canvas).toBeVisible()
  await expect(timeline).toBeVisible()
  await expect(overview).toBeVisible()
  await page.getByRole('button', { name: /跳到结局/ }).click()

  await expect(overview).toContainText('54/60')
  await expect(overview).toContainText('6')
  await expect(overview).toContainText('17/25')
  await expect(overview).toContainText('8')
  await expect(page.getByTestId('pencil-last-edge')).toHaveText('(5, 8)')
  await expect(page.getByTestId('pencil-box-map')).toBeVisible()
  await expect(page.getByTestId('pencil-box-map').locator('[data-owner]')).toHaveCount(25)
  await expect(page.getByTestId('pencil-box-map').locator('[data-owner="0"]')).toHaveCount(4)
  await expect(page.getByTestId('pencil-box-map').locator('[data-owner="1"]')).toHaveCount(13)
  await expect(page.getByTestId('pencil-edge-composition')).toHaveAttribute(
    'aria-label',
    '连边构成：红方 24 条，蓝方 30 条，未连 6 条',
  )

  for (const viewport of [
    { width: 2560, height: 1080 },
    { width: 1920, height: 1080 },
    { width: 2048, height: 1024 },
    { width: 2048, height: 1152 },
    { width: 1600, height: 900 },
    { width: 1536, height: 1080 },
  ]) {
    await page.setViewportSize(viewport)
    const desktopCanvas = await canvas.boundingBox()
    const desktopTimeline = await timeline.boundingBox()
    const overviewBounds = await overview.boundingBox()
    expect(desktopCanvas).not.toBeNull()
    expect(desktopTimeline).not.toBeNull()
    expect(overviewBounds).not.toBeNull()
    expect(Math.abs((desktopCanvas?.width ?? 0) - (desktopCanvas?.height ?? 0))).toBeLessThanOrEqual(1)
    // 三栏宽屏同时受视口高度和主区剩余宽度约束。1536×1080 是宽度瓶颈，
    // 不能再按高度公式强行要求 824px 棋盘，否则会重新挤压信息轨或时序栏。
    const expectedMax = Math.min(832, viewport.height - 256, viewport.width - 888)
    expect(desktopCanvas?.width ?? 9999).toBeLessThanOrEqual(expectedMax + 1)
    expect(desktopCanvas?.width ?? 0).toBeGreaterThanOrEqual(expectedMax - 2)
    expect((desktopCanvas?.y ?? 0) + (desktopCanvas?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
    expect((overviewBounds?.x ?? 9999) + (overviewBounds?.width ?? 0)).toBeLessThan(desktopCanvas?.x ?? 0)
    expect(overviewBounds?.height ?? 9999).toBeLessThanOrEqual((desktopCanvas?.height ?? 0) + 1)
    const overviewContainment = await overview.evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      overflowY: getComputedStyle(element).overflowY,
    }))
    expect(overviewContainment.scrollHeight).toBeLessThanOrEqual(overviewContainment.clientHeight + 1)
    expect(overviewContainment.overflowY).not.toBe('visible')
    expect(desktopTimeline?.x ?? 0).toBeGreaterThan((desktopCanvas?.x ?? 0) + (desktopCanvas?.width ?? 0))
    expect((desktopTimeline?.y ?? 0) + (desktopTimeline?.height ?? 0)).toBeLessThanOrEqual(viewport.height + 1)
    const timelineScroll = await timeline.locator('div.overflow-y-auto').evaluate((element) => ({
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }))
    expect(timelineScroll.scrollHeight).toBeGreaterThan(timelineScroll.clientHeight)
  }

  // 用户主动折叠宽屏时序后不得继续保留一条空右轨；概览与棋盘应并排占用主区，
  // 折叠标题移到下一行，展开后恢复三栏。
  await page.setViewportSize({ width: 1600, height: 900 })
  await timeline.getByRole('button', { name: '折叠', exact: true }).click()
  const collapsedCanvas = await canvas.boundingBox()
  const collapsedOverview = await overview.boundingBox()
  const collapsedTimeline = await timeline.boundingBox()
  expect(collapsedCanvas).not.toBeNull()
  expect(collapsedOverview).not.toBeNull()
  expect(collapsedTimeline).not.toBeNull()
  expect((collapsedOverview?.x ?? 9999) + (collapsedOverview?.width ?? 0)).toBeLessThan(collapsedCanvas?.x ?? 0)
  expect(collapsedTimeline?.y ?? 0).toBeGreaterThan(Math.max(
    (collapsedOverview?.y ?? 0) + (collapsedOverview?.height ?? 0),
    (collapsedCanvas?.y ?? 0) + (collapsedCanvas?.height ?? 0),
  ))
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1)
  await timeline.getByRole('button', { name: '展开', exact: true }).click()

  for (const viewport of [
    { width: 1366, height: 768 },
    { width: 1312, height: 700 },
  ]) {
    await page.setViewportSize(viewport)
    const desktopCanvas = await canvas.boundingBox()
    const desktopTimeline = await timeline.boundingBox()
    const desktopOverview = await overview.boundingBox()
    expect(desktopCanvas).not.toBeNull()
    expect(desktopTimeline).not.toBeNull()
    expect(desktopOverview).not.toBeNull()
    expect(Math.abs((desktopCanvas?.width ?? 0) - (desktopCanvas?.height ?? 0))).toBeLessThanOrEqual(1)
    expect(desktopCanvas?.width ?? 0).toBeGreaterThanOrEqual(400)
    expect(desktopCanvas?.width ?? 9999).toBeLessThanOrEqual(520)
    expect(Math.abs((desktopOverview?.x ?? 0) - (desktopCanvas?.x ?? 0))).toBeLessThanOrEqual(1)
    expect((desktopOverview?.y ?? 0) + (desktopOverview?.height ?? 0)).toBeLessThanOrEqual(desktopCanvas?.y ?? 0)
    expect(desktopTimeline?.x ?? 0).toBeGreaterThan((desktopCanvas?.x ?? 0) + (desktopCanvas?.width ?? 0))
  }

  await page.setViewportSize({ width: 1312, height: 700 })
  await page.evaluate(() => window.scrollTo(0, 360))
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(100)
  const stickyTimeline = await timeline.boundingBox()
  expect(stickyTimeline?.y ?? 999).toBeLessThanOrEqual(40)
  await page.evaluate(() => window.scrollTo(0, 0))

  for (const viewport of [
    { width: 1024, height: 768 },
    { width: 844, height: 390 },
  ]) {
    await page.setViewportSize(viewport)
    const boardBounds = await canvas.boundingBox()
    const overviewBounds = await overview.boundingBox()
    const timelineBounds = await timeline.boundingBox()
    expect(boardBounds).not.toBeNull()
    expect(overviewBounds).not.toBeNull()
    expect(timelineBounds).not.toBeNull()
    expect(Math.abs((boardBounds?.width ?? 0) - (boardBounds?.height ?? 0))).toBeLessThanOrEqual(1)
    expect(boardBounds?.width ?? 9999).toBeLessThanOrEqual(viewport.height - 96 + 1)
    expect(boardBounds?.width ?? 0).toBeGreaterThanOrEqual(280)
    expect((overviewBounds?.x ?? 9999) + (overviewBounds?.width ?? 0)).toBeLessThan(boardBounds?.x ?? 0)
    expect(timelineBounds?.y ?? 0).toBeGreaterThan(Math.max(
      (overviewBounds?.y ?? 0) + (overviewBounds?.height ?? 0),
      (boardBounds?.y ?? 0) + (boardBounds?.height ?? 0),
    ))
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${viewport.width}x${viewport.height} replay overflow`).toBeLessThanOrEqual(1)
  }

  for (const viewport of [
    { width: 390, height: 700 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewportSize(viewport)
    const seatOneScore = page.getByTestId('pencil-seat-score-1')
    await expect(seatOneScore).toContainText('座位 1 · 红')
    await expect(seatOneScore).toContainText('4')
    await expect(seatOneScore).not.toContainText('pencil_reference')
    await expect(timeline.getByRole('button', { name: '展开', exact: true })).toBeVisible()
    const bounds = await canvas.boundingBox()
    expect(bounds).not.toBeNull()
    expect(Math.abs((bounds?.width ?? 0) - (bounds?.height ?? 0))).toBeLessThanOrEqual(1)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow, `${viewport.width}px replay overflow`).toBeLessThanOrEqual(1)
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
    await page.evaluate(() => window.scrollTo(0, 0))
  }
  await monitor.expectClean()
})

test('MatchViewer reconnects transient SSE, localizes terminal errors, and warns for abnormal reasons', async ({ page }) => {
  const matchId = 'mock-reconnect-terminal-error'
  const runningMatch = {
    id: matchId,
    game_id: 'holdem',
    status: 'running',
    reason: '',
    match_type: 'challenge',
    winner: null,
    bot_a: { name: 'Reconnect A', owner_name: 'alpha' },
    bot_b: { name: 'Reconnect B', owner_name: 'beta' },
    result: { rounds_played: 0, deltas: [0, 0] },
  }
  await page.route(`**/api/matches/${matchId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{"comments":[],"count":0,"total":0}',
    })
  })
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ match: runningMatch }),
    })
  })
  let eventRequests = 0
  await page.route(`**/api/matches/${matchId}/events`, async (route) => {
    eventRequests += 1
    if (eventRequests === 1) {
      // A finite non-terminal stream reproduces a proxy/network interruption.
      // Native EventSource must be allowed to reconnect instead of being closed
      // permanently by the page's onerror callback.
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'data: {"type":"match_start","game_id":"holdem"}\n\n',
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      // A diagnostic-looking extra field must never become the public reason.
      // In particular, "completed" must not turn an aborted match into
      // the contradictory label "正常结束".
      body: 'data: {"type":"error","message":"completed"}\n\n',
    })
  })
  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  await expect(page.getByText('已中止', { exact: true })).toBeVisible()
  await expect(page.locator('main')).toContainText('平台运行异常')
  await expect(page.locator('main')).not.toContainText('正常结束')
  // The terminal event is retained behind the pinned cursor. Advancing exposes
  // the same canonical public description, never the private diagnostic text.
  await page.getByRole('button', { name: '下一个事件', exact: true }).click()
  await expect(page.getByText('平台运行异常', { exact: true })).toHaveCount(2)
  expect(eventRequests).toBe(2)
  // The second stream delivered an explicit terminal error and must close. Stay
  // beyond Chromium's retry window so terminal reconnects cannot false-pass.
  await page.waitForTimeout(4_250)
  expect(eventRequests).toBe(2)

  // A scored technical loss is completed, but its non-generic reason remains a
  // user-visible warning after a direct refresh (not only during live SSE).
  await page.unroute(`**/api/matches/${matchId}`)
  await page.unroute(`**/api/matches/${matchId}/events`)
  await routeStructuredReplay(page, matchId, [
    { type: 'match_end', winner: 1, reason: 'technical_loss', deltas: [-1, 1] },
  ])
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          ...runningMatch,
          status: 'completed',
          reason: 'technical_loss',
          winner: 1,
          technical_loss: 1,
          result: { rounds_played: 0, deltas: [-1, 1] },
        },
      }),
    })
  })
  await page.reload()
  await expect(page.getByText('已完成', { exact: true })).toBeVisible()
  await expect(page.locator('main')).toContainText('Bot 技术判负')
  await monitor.expectClean()
})

test('Holdem replay state includes blinds and treats all-in amount as raise-to', async ({ page }) => {
  await page.goto('/')
  const states = await page.evaluate(async () => {
    const module = await import('/src/games/holdem/reducer.ts')
    const canvasModule = await import('/src/games/holdem/canvas.ts')
    const allin = module.reduceHoldemEvents([
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'action', hand: 0, player: 0, action: 'call', amount: 50 },
      { type: 'action', hand: 0, player: 1, action: 'allin', amount: 20000 },
    ])
    const runout = module.reduceHoldemEvents([
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'action', hand: 0, player: 0, action: 'allin', amount: 20000 },
      { type: 'action', hand: 0, player: 1, action: 'call', amount: 19900 },
      { type: 'deal_board', hand: 0, street: 'flop', board: ['Ah', 'Kd', 'Qc'] },
    ])
    const folded = module.reduceHoldemEvents([
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'action', hand: 0, player: 0, action: 'fold', amount: 0 },
    ])
    const openingCallEvents = [
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'action', hand: 0, player: 0, action: 'call', amount: 50 },
    ]
    const openingCall = module.reduceHoldemEvents(openingCallEvents)
    const preflopCheck = module.reduceHoldemEvents([
      ...openingCallEvents,
      { type: 'action', hand: 0, player: 1, action: 'check', amount: 0 },
    ])
    const postflopCheck = module.reduceHoldemEvents([
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'deal_board', hand: 0, street: 'flop', board: ['Ah', 'Kd', 'Qc'] },
      { type: 'action', hand: 0, player: 1, action: 'check', amount: 0 },
      { type: 'action', hand: 0, player: 0, action: 'check', amount: 0 },
    ])
    const postflopCall = module.reduceHoldemEvents([
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'deal_board', hand: 0, street: 'flop', board: ['Ah', 'Kd', 'Qc'] },
      { type: 'action', hand: 0, player: 1, action: 'raise', amount: 100 },
      { type: 'action', hand: 0, player: 0, action: 'call', amount: 100 },
    ])
    const duplicate = module.reduceHoldemEvents([
      { type: 'match_start', game_id: 'holdem', num_hands: 1, leg: 0 },
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900], leg: 0 },
      { type: 'action', hand: 0, player: 0, action: 'fold', amount: 0, leg: 0 },
      { type: 'settle', hand: 0, winners: [1], deltas: [-50, 50], chips: [19950, 20050], pot: 100, reason: 'fold', leg: 0 },
      { type: 'match_start', game_id: 'holdem', num_hands: 1, leg: 1 },
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900], leg: 1 },
      { type: 'action', hand: 0, player: 1, action: 'fold', amount: 0, leg: 1 },
      { type: 'settle', hand: 0, winners: [0], deltas: [50, -50], chips: [20050, 19950], pot: 100, reason: 'fold', leg: 1 },
      { type: 'match_end', winner: null, reason: 'completed', deltas: [-100, 100] },
    ])
    const abortedEvents = [
      { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
      { type: 'settle', hand: 0, winners: [0], deltas: [100, -100], chips: [20100, 19900], pot: 200, reason: 'showdown' },
      { type: 'error', reason: 'platform_error' },
    ]
    return {
      allin,
      runout,
      folded,
      openingCall,
      preflopCheck,
      postflopCheck,
      postflopCall,
      duplicate,
      aborted: module.reduceHoldemEvents(abortedEvents),
      abortedScene: canvasModule.PokerCanvasRenderer.toScene(abortedEvents),
    }
  })
  expect(states.allin.pot).toBe(20100)
  expect(states.allin.seats.map((seat) => seat.bet)).toEqual([100, 20000])
  expect(states.allin.seats.map((seat) => seat.chips)).toEqual([19900, 0])
  expect(states.allin.seats[1].allin).toBe(true)
  expect(states.runout.toAct).toBeNull()
  expect(states.folded.toAct).toBeNull()
  expect(states.openingCall.toAct).toBe(1)
  expect(states.preflopCheck.toAct).toBeNull()
  expect(states.postflopCheck.toAct).toBeNull()
  expect(states.postflopCall.toAct).toBeNull()
  expect(states.duplicate).toMatchObject({
    isDuplicate: true,
    leg: 1,
    totalLegs: 2,
    totalHands: 70,
    handsStarted: 2,
    completedHands: 2,
    sbSeat: 1,
    matchWinner: null,
    status: 'match_end',
  })
  expect(states.duplicate.seats.map((seat) => seat.chips)).toEqual([19950, 20050])
  expect(states.duplicate.seats.map((seat) => seat.net)).toEqual([-100, 100])
  expect(states.duplicate.lastSettle?.winners).toEqual([1])
  expect(states.aborted.status).toBe('error')
  expect(states.aborted.matchWinner).toBeNull()
  expect(states.abortedScene.terminalStatus).toBe('error')
  expect(states.abortedScene.matchWinner).toBeNull()
})

test('Pencil replay reconstructs the judge score for an illegal terminal', async ({ page }) => {
  await page.goto('/')
  const turnStates = await page.evaluate(async () => {
    const module = await import('/src/games/pencil/reducer.ts')
    const start = { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] }
    const turn = { type: 'turn', player: 0, pass_: 0, scores: [0, 0] }
    const scoredMove = {
      type: 'move', player: 0, x: 1, y: 0, scored: true, scores: [1, 0],
      move_index: 1, closed_boxes: [{ x: 1, y: 1, owner: 0 }],
    }
    const forcedPass = { type: 'turn', player: 1, pass_: 1, scores: [1, 0] }
    const pass = { type: 'pass', player: 1, scores: [1, 0] }
    const nextTurn = { type: 'turn', player: 0, pass_: 0, scores: [1, 0] }
    const timeUsed = { type: 'time_used', seat: 0, used: 0.2, remaining: 899.8, budget: 900 }
    const timeOut = { type: 'time_out', seat: 0, used: 900, budget: 900 }
    const illegal = { type: 'illegal', player: 0, move: { x: -1, y: -1 }, why: 'illegal_move' }
    const incident = { type: 'technical_incident', seat: 0, code: 'missing_response', error: '响应异常' }
    return [
      module.reducePencilEvents([start, turn]),
      module.reducePencilEvents([start, turn, scoredMove]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass, pass]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass, pass, nextTurn]),
      module.reducePencilEvents([start, turn, timeUsed]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass, timeOut]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass, illegal]),
      module.reducePencilEvents([start, turn, scoredMove, forcedPass, incident]),
    ].map(({ toAct, extraTurn, mustPass }) => ({ toAct, extraTurn, mustPass }))
  })
  expect(turnStates).toEqual([
    { toAct: 0, extraTurn: false, mustPass: false },
    { toAct: null, extraTurn: true, mustPass: false },
    { toAct: 1, extraTurn: true, mustPass: true },
    { toAct: null, extraTurn: false, mustPass: false },
    { toAct: 0, extraTurn: false, mustPass: false },
    { toAct: null, extraTurn: false, mustPass: false },
    { toAct: null, extraTurn: false, mustPass: false },
    { toAct: null, extraTurn: false, mustPass: false },
    { toAct: null, extraTurn: false, mustPass: false },
  ])

  const events = [
    { type: 'match_start', game_id: 'pencil', n_dots: 6, size: 11, scores: [0, 0] },
    { type: 'illegal', player: 0, move: { x: -1, y: -1 }, why: 'illegal_move' },
    { type: 'match_end', winner: 1, reason: 'illegal', deltas: [-2, 2] },
  ]
  const state = await page.evaluate(async (fixture) => {
    const module = await import('/src/games/pencil/reducer.ts')
    return module.reducePencilEvents(fixture)
  }, events)
  expect(state.scores).toEqual([0, 2])
  expect(state.winner).toBe(1)
  expect(state.reason).toBe('illegal')

  const matchId = 'mock-pencil-illegal-position-summary'
  await routeStructuredReplay(page, matchId, events)
  await page.route(`**/api/matches/${matchId}/view`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
  })
  await page.route(`**/api/matches/${matchId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match: {
          id: matchId, game_id: 'pencil', status: 'completed', match_type: 'challenge',
          winner: 1, reason: 'illegal',
          bot_a: { name: 'illegal_a', owner_name: 'alpha' },
          bot_b: { name: 'legal_b', owner_name: 'beta' },
          result: { rounds_played: 0, deltas: [-2, 2], normalized_delta: -2 },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}' })
  })
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto(`/#/match/${matchId}`)
  await page.getByRole('button', { name: /跳到结局/ }).click()
  const overview = page.getByTestId('pencil-position-overview')
  await expect(overview).toContainText('0/25')
  await expect(overview).toContainText('25')
  await expect(overview).not.toContainText('2/25')
  await expect(overview).toContainText('蓝方经裁判判定获胜')
  await expect(overview).toContainText('终止前红 0 格 · 蓝 0 格 · 25 格未决')
  await expect(page.getByTestId('pencil-box-map').locator('[data-owner="0"], [data-owner="1"]')).toHaveCount(0)
})

test('MatchViewer presents a zero-hand protocol loss as a terminal incident', async ({ page }) => {
  const matchId = 'mock-zero-hand-protocol-loss'
  const events = [
    { type: 'hand_start', hand: 0, sb: 0, bb: 1, chips: [19950, 19900] },
    { type: 'deal_hole', hand: 0, holes: [['3d', 'Th'], ['Qs', '4c']] },
    {
      type: 'technical_incident', seat: 0, code: 'missing_response',
      error: 'Bot 响应缺少必填 response 字段', reason: 'protocol_error', turn: 1,
    },
    { type: 'match_end', winner: 1, reason: 'protocol_error', deltas: [-1, 1] },
  ]
  await routeStructuredReplay(page, matchId, events)
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
          game_id: 'holdem',
          status: 'completed',
          match_type: 'challenge',
          winner: 1,
          reason: 'protocol_error',
          technical_loss: 1,
          bot_a_id: 3,
          bot_b_id: 2,
          bot_a: { name: 'admin', owner_name: 'zzx' },
          bot_b: { name: 'zxx02', owner_name: 'zhouzixiang' },
          result: {
            rounds_played: 0,
            deltas: [-1, 1],
            technical_incidents_by_seat: { 0: 1, 1: 0 },
            technical_incident_samples: [{
              seat: 0, code: 'missing_response',
              error: 'Bot 响应缺少必填 response 字段', reason: 'protocol_error', turn: 1,
            }],
          },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}' })
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  const incident = page.getByRole('alert').filter({ hasText: 'Bot 技术判负' })
  await expect(incident).toContainText('admin @zzx（座位 1）')
  await expect(incident).toContainText('zxx02 @zhouzixiang（座位 2）获胜')
  await expect(incident).toContainText('missing_response')
  await expect(incident).toContainText('第 1 次决策')
  await expect(incident).toContainText('Bot 响应缺少必填 response 字段')
  await expect(page.locator('main')).not.toContainText('落后')
  await expect(page.getByText(/\/70 手/)).toHaveCount(0)
  await expect(page.getByRole('button', { name: '播放', exact: true })).toHaveCount(0)
  await expect(page.getByText('手导航（点击跳转）', { exact: true })).toHaveCount(0)
  await expect(page.getByText('结束 · 座2获胜 · Bot 响应协议错误', { exact: true })).toBeVisible()
  const canvasBox = await page.locator('canvas').boundingBox()
  expect(canvasBox).not.toBeNull()
  expect((canvasBox?.width ?? 0) / (canvasBox?.height ?? 1)).toBeCloseTo(16 / 9, 1)

  await page.setViewportSize({ width: 390, height: 844 })
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
  await expect(page.getByText('admin', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('zxx02', { exact: true }).first()).toBeVisible()
  await monitor.expectClean()
})

test('MatchViewer keeps chess history playable after a mid-game technical loss', async ({ page }) => {
  const matchId = 'mock-gomoku-midgame-protocol-loss'
  const events = [
    { type: 'match_start', game_id: 'gomoku', size: 15 },
    { type: 'move', player: 0, x: 7, y: 7, move_index: 1 },
    { type: 'move', player: 1, x: 7, y: 8, move_index: 2 },
    {
      type: 'technical_incident', seat: 0, code: 'missing_response',
      error: 'Bot 响应缺少必填 response 字段', reason: 'protocol_error', turn: 3,
    },
    { type: 'match_end', winner: 1, reason: 'protocol_error', deltas: [-1, 1] },
  ]
  await routeStructuredReplay(page, matchId, events)
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
          game_id: 'gomoku',
          status: 'completed',
          match_type: 'challenge',
          winner: 1,
          reason: 'protocol_error',
          technical_loss: 1,
          bot_a: { name: 'black_bot', owner_name: 'alpha' },
          bot_b: { name: 'white_bot', owner_name: 'beta' },
          // 历史通用字段对棋类为 0；已走步数必须从 replay reducer 取。
          result: { rounds_played: 0, deltas: [-1, 1] },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"comments":[],"count":0,"total":0}' })
  })

  const monitor = monitorBrowser(page)
  await page.goto(`/#/match/${matchId}`)
  await expect(page.getByText('共 2 步', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '播放', exact: true })).toBeVisible()
  await expect(page.locator('main')).toContainText('动作时序')
  await expect(page.getByRole('alert')).toContainText('missing_response')
  await monitor.expectClean()
})

async function activateVersion(page: Page, manager: Locator, botId: number, version: number) {
  const row = versionRow(manager, version)
  await row.getByRole('button', { name: '回滚', exact: true }).click()
  const confirmation = page.getByRole('dialog').filter({ hasText: `回滚到 v${version}?` })
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === `/api/bots/${botId}/versions/${version}/activate`,
  )
  await confirmation.getByRole('button', { name: '确认', exact: true }).click()
  const response = await responsePromise
  expect(response.status(), await response.text()).toBe(200)
  await expect(row.getByText('当前', { exact: true })).toBeVisible()
}

test('version dialog ignores stale Bot responses and repeated rollback stays correct', async ({
  page,
  browser,
  baseURL,
}) => {
  expect(baseURL).toBeTruthy()
  const createdBotIds: number[] = []
  await withCleanup(async () => {
    const monitor = monitorBrowser(page)
    await loginThroughUi(page, USER)
    const suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
    const primaryBot = await createDisposableBot(page, `pwv_a_${suffix}`)
    createdBotIds.push(primaryBot.id)
    const slowBot = await createDisposableBot(page, `pwv_b_${suffix}`)
    createdBotIds.push(slowBot.id)
    await page.goto('/#/my-bots')

    const botLink = page.getByRole('link', { name: primaryBot.name, exact: true })
    const botRow = botLink.locator('xpath=ancestor::li[1]')
    const botId = primaryBot.id
    await expect(botRow.getByText(`#${botId}`, { exact: true })).toBeVisible()

  // Reuse the dialog A→B while A's response is held back. A late response used
  // to replace B's version rows and could activate A's version number on B.
  const slowBotRow = page
    .getByRole('link', { name: slowBot.name, exact: true })
    .locator('xpath=ancestor::li[1]')
  const slowBotId = slowBot.id
  await expect(slowBotRow.getByText(`#${slowBotId}`, { exact: true })).toBeVisible()
  let releaseSlow!: () => void
  const slowGate = new Promise<void>((resolve) => { releaseSlow = resolve })
  let observeSlow!: () => void
  const slowObserved = new Promise<void>((resolve) => { observeSlow = resolve })
  const slowPattern = `**/api/bots/${slowBotId}/versions`
  await page.route(slowPattern, async (route) => {
    observeSlow()
    await slowGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        current_version: 999,
        versions: [{
          id: 999999,
          version: 999,
          binary_path: '/qa/stale-response',
          upload_note: 'must never appear in the next Bot dialog',
          size_bytes: 1,
          os: 'linux',
          arch: 'amd64',
          format: 'elf',
          runtime_mode: 'traditional',
          uploaded_at: '2026-01-01T00:00:00Z',
        }],
      }),
    })
  })
  await slowBotRow.getByRole('button', { name: '版本', exact: true }).click()
  await slowObserved
  await expect(page.getByRole('dialog').filter({ hasText: `版本管理 · ${slowBot.name}` })).toBeVisible()
  await page.keyboard.press('Escape')

  await botRow.getByRole('button', { name: '版本', exact: true }).click()
  const manager = page.getByRole('dialog').filter({ hasText: `版本管理 · ${primaryBot.name}` })
  await expect(manager.getByText('版本历史', { exact: true })).toBeVisible()
  await expect(manager.getByRole('combobox').filter({ hasText: 'Traditional（默认）' })).toBeVisible()
  await expect(manager.getByText(/每个决策点重启进程并发送完整历史信封/)).toBeVisible()
  await expect(manager.getByText(/^v\d+$/).first()).toBeVisible()
  releaseSlow()
  await page.waitForTimeout(200)
  await expect(manager.getByText('v999', { exact: true })).toHaveCount(0)
  await page.unroute(slowPattern)

  // Replacing a valid selection with an oversized file must clear React state as
  // well as the native input; otherwise the next submit silently uploads the old
  // binary even though the user just selected a different file.
  const versionFile = manager.locator('#ver-file')
  await versionFile.setInputFiles(HOLDEM_SAMPLE)
  await expect(manager.getByText('callbot_linux_amd64', { exact: true })).toBeVisible()
  await versionFile.evaluate((element) => {
    const input = element as HTMLInputElement
    const file = new File(['oversized'], 'too-large.bin', { type: 'application/octet-stream' })
    Object.defineProperty(file, 'size', { value: 50 * 1024 * 1024 + 1 })
    const transfer = new DataTransfer()
    transfer.items.add(file)
    input.files = transfer.files
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await expect(manager.getByText('未选择文件', { exact: true })).toBeVisible()

  // Hold an upload on A, reuse the dialog for B, then start B's upload. A's late
  // failure must not surface in B or clear B's busy lock (which would permit a
  // duplicate submit). Both writes are mocked, so this regression adds no DB row.
  let releaseStaleUpload!: () => void
  const staleUploadGate = new Promise<void>((resolve) => { releaseStaleUpload = resolve })
  let observeStaleUpload!: () => void
  const staleUploadObserved = new Promise<void>((resolve) => { observeStaleUpload = resolve })
  let releaseCurrentUpload!: () => void
  const currentUploadGate = new Promise<void>((resolve) => { releaseCurrentUpload = resolve })
  let observeCurrentUpload!: () => void
  const currentUploadObserved = new Promise<void>((resolve) => { observeCurrentUpload = resolve })
  const currentPattern = `**/api/bots/${botId}/versions`
  await page.route(slowPattern, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    observeStaleUpload()
    await staleUploadGate
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'stale A upload failure' }),
    })
  })
  await page.route(currentPattern, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    observeCurrentUpload()
    await currentUploadGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bot: { id: botId } }),
    })
  })

  await page.keyboard.press('Escape')
  await slowBotRow.getByRole('button', { name: '版本', exact: true }).click()
  const staleManager = page.getByRole('dialog').filter({ hasText: `版本管理 · ${slowBot.name}` })
  await expect(staleManager.getByText(/^v\d+$/).first()).toBeVisible()
  await staleManager.locator('#ver-file').setInputFiles(HOLDEM_SAMPLE)
  await staleManager.getByRole('button', { name: '上传新版本', exact: true }).click()
  await staleUploadObserved
  await page.keyboard.press('Escape')

  await botRow.getByRole('button', { name: '版本', exact: true }).click()
  await expect(manager.getByText(/^v\d+$/).first()).toBeVisible()
  await manager.locator('#ver-file').setInputFiles(HOLDEM_SAMPLE)
  await manager.getByRole('button', { name: '上传新版本', exact: true }).click()
  await currentUploadObserved
  await expect(manager.getByRole('button', { name: '处理中…', exact: true })).toBeDisabled()

  const staleFailureResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/bots/${slowBotId}/versions`
  ))
  releaseStaleUpload()
  expect((await staleFailureResponse).status()).toBe(400)
  await expect(manager).not.toContainText('stale A upload failure')
  await expect(manager.getByRole('button', { name: '处理中…', exact: true })).toBeDisabled()

  const currentSuccessResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/bots/${botId}/versions`
  ))
  releaseCurrentUpload()
  expect((await currentSuccessResponse).status()).toBe(200)
  await expect(manager.getByRole('button', { name: '上传新版本', exact: true })).toBeEnabled()
  await page.unroute(slowPattern)
  await page.unroute(currentPattern)

  const originalRow = manager.getByText('当前', { exact: true }).locator('xpath=ancestor::li[1]')
  const originalVersion = Number((await originalRow.getByText(/^v\d+$/).textContent())?.slice(1))
  expect(originalVersion).toBeGreaterThan(0)

  // A real LongRunning upload must pass the backend's strict first-envelope +
  // KEEP_RUNNING preflight; this is intentionally not mocked.
  await manager.getByRole('combobox').filter({ hasText: 'Traditional（默认）' }).click()
  await page.getByRole('option', { name: 'LongRunning（严格长驻）', exact: true }).click()
  await expect(manager.getByText(/首回合响应后必须输出 KEEP_RUNNING 握手/)).toBeVisible()
  await manager.locator('#ver-note').fill('Playwright rollback regression')
  // The preceding mocked success clears React state but intentionally does not
  // change the browser-owned file input value. Clear it so selecting the same
  // fixture emits a real change event for the existing upload regression below.
  await manager.locator('#ver-file').setInputFiles([])
  await manager.locator('#ver-file').setInputFiles(
    HOLDEM_SAMPLE,
  )
  const uploadResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === `/api/bots/${botId}/versions`,
  )
  await manager.getByRole('button', { name: '上传新版本', exact: true }).click()
  const uploaded = await uploadResponse
  expect(uploaded.status(), await uploaded.text()).toBe(200)

  const newestCurrent = manager.getByText('当前', { exact: true }).locator('xpath=ancestor::li[1]')
  await expect.poll(async () => {
    const text = await newestCurrent.getByText(/^v\d+$/).textContent()
    return Number(text?.slice(1))
  }).toBeGreaterThan(originalVersion)
  const newVersion = Number((await newestCurrent.getByText(/^v\d+$/).textContent())?.slice(1))
  expect(newVersion).toBeGreaterThan(originalVersion)
  await expect(newestCurrent).toContainText('longrunning')

  // The old implementation compared against a stale prop. The final click in this
  // sequence was visibly enabled but silently returned without a network request.
  await activateVersion(page, manager, botId, originalVersion)
  await activateVersion(page, manager, botId, newVersion)
  await activateVersion(page, manager, botId, originalVersion) // restore test state

  // Exercise the same context fence for rollback's catch/finally: a late failed
  // rollback on B must not unlock an upload already running in reused dialog A.
  let releaseStaleRollback!: () => void
  const staleRollbackGate = new Promise<void>((resolve) => { releaseStaleRollback = resolve })
  let observeStaleRollback!: () => void
  const staleRollbackObserved = new Promise<void>((resolve) => { observeStaleRollback = resolve })
  const staleRollbackPattern = `**/api/bots/${botId}/versions/${newVersion}/activate`
  await page.route(staleRollbackPattern, async (route) => {
    observeStaleRollback()
    await staleRollbackGate
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'stale B rollback failure' }),
    })
  })
  await versionRow(manager, newVersion).getByRole('button', { name: '回滚', exact: true }).click()
  await page.getByRole('dialog')
    .filter({ hasText: `回滚到 v${newVersion}?` })
    .getByRole('button', { name: '确认', exact: true })
    .click()
  await staleRollbackObserved
  // Close the manager explicitly: an Escape sent while the nested confirmation
  // is finishing its exit animation can be consumed by that already-confirmed
  // dialog and leave the manager modal blocking the underlying Bot row.
  await manager.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(manager).not.toBeVisible()

  let releaseNextUpload!: () => void
  const nextUploadGate = new Promise<void>((resolve) => { releaseNextUpload = resolve })
  let observeNextUpload!: () => void
  const nextUploadObserved = new Promise<void>((resolve) => { observeNextUpload = resolve })
  await page.route(slowPattern, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    observeNextUpload()
    await nextUploadGate
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ bot: { id: slowBotId } }),
    })
  })
  await slowBotRow.getByRole('button', { name: '版本', exact: true }).click()
  await expect(staleManager.getByText(/^v\d+$/).first()).toBeVisible()
  await staleManager.locator('#ver-file').setInputFiles(HOLDEM_SAMPLE)
  await staleManager.getByRole('button', { name: '上传新版本', exact: true }).click()
  await nextUploadObserved
  await expect(staleManager.getByRole('button', { name: '处理中…', exact: true })).toBeDisabled()

  const staleRollbackResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/bots/${botId}/versions/${newVersion}/activate`
  ))
  releaseStaleRollback()
  expect((await staleRollbackResponse).status()).toBe(500)
  await expect(staleManager).not.toContainText('stale B rollback failure')
  await expect(staleManager.getByRole('button', { name: '处理中…', exact: true })).toBeDisabled()

  const nextUploadResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/bots/${slowBotId}/versions`
  ))
  releaseNextUpload()
  expect((await nextUploadResponse).status()).toBe(200)
  await expect(staleManager.getByRole('button', { name: '上传新版本', exact: true })).toBeEnabled()
  await page.unroute(staleRollbackPattern)
  await page.unroute(slowPattern)

  await page.keyboard.press('Escape')
  await botRow.getByRole('button', { name: '版本', exact: true }).click()
  await expect(versionRow(manager, originalVersion).getByText('当前', { exact: true })).toBeVisible()

  // A runnable ELF that exits immediately passes binary classification but fails
  // protocol preflight. The failed upload must not silently activate the greatest
  // historical version when the user had deliberately rolled back to an older one.
  await manager.locator('#ver-note').fill('intentional preflight failure')
  await manager.locator('#ver-file').setInputFiles(PREFLIGHT_FAILURE_SAMPLE)
  const failedUploadPromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === `/api/bots/${botId}/versions`,
  )
  await manager.getByRole('button', { name: '上传新版本', exact: true }).click()
  const failedUpload = await failedUploadPromise
  expect(failedUpload.status(), await failedUpload.text()).toBe(400)
  await expect(manager.getByText(/Bot 预检失败/)).toBeVisible()
  await expect(versionRow(manager, originalVersion).getByText('当前', { exact: true })).toBeVisible()

  await monitor.expectClean([
    {
      kind: 'http',
      method: 'POST',
      status: 400,
      pathname: `/api/bots/${slowBotId}/versions`,
    },
    {
      kind: 'http',
      method: 'POST',
      status: 400,
      pathname: `/api/bots/${botId}/versions`,
    },
    {
      kind: 'http',
      method: 'POST',
      status: 500,
      pathname: `/api/bots/${botId}/versions/${newVersion}/activate`,
    },
  ])
  }, async () => {
    await hardDeleteBots(browser, baseURL!, createdBotIds)
  })
})

test('organizer has no dead admin navigation while admin owner links use usernames', async ({ browser, baseURL }) => {
  expect(baseURL).toBeTruthy()
  const organizerContext = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } })
  const organizerPage = await organizerContext.newPage()
  const organizerMonitor = monitorBrowser(organizerPage)
  await loginThroughUi(organizerPage, ORGANIZER)
  await organizerPage.goto('/#/')
  await expect(organizerPage.getByRole('link', { name: '管理端', exact: true })).toHaveCount(0)
  await organizerPage.goto('/#/admin')
  await expect(organizerPage.getByText('仅管理员可访问管理端。', { exact: false })).toBeVisible()
  await organizerMonitor.expectClean()
  await organizerContext.close()

  const adminContext = await browser.newContext({ baseURL, viewport: { width: 1440, height: 900 } })
  const adminPage = await adminContext.newPage()
  const adminMonitor = monitorBrowser(adminPage)
  await loginThroughUi(adminPage, ADMIN)
  await adminPage.goto('/#/admin')
  await adminPage.getByRole('button', { name: 'Bot', exact: true }).click()
  const botQuery = `${USER}_holdem`
  const filteredBotsPromise = adminPage.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET' &&
      url.pathname === '/api/admin/bots' &&
      url.search === `?page=1&per_page=20&q=${encodeURIComponent(botQuery)}`
  })
  await adminPage.getByRole('textbox', { name: '搜索 Bot 名称', exact: true }).fill(botQuery)
  const filteredBotsResponse = await filteredBotsPromise
  expect(filteredBotsResponse.status(), await filteredBotsResponse.text()).toBe(200)
  const filteredBots = await filteredBotsResponse.json() as {
    bots: Array<{ name: string; display_name?: string; owner_name?: string }>
  }
  expect(filteredBots.bots.length).toBeGreaterThan(0)
  expect(filteredBots.bots.every((bot) =>
    `${bot.name} ${bot.display_name || ''}`.toLowerCase().includes(botQuery.toLowerCase()),
  )).toBe(true)
  const owner = adminPage.locator(`a[href="#/user/${USER}"]`).first()
  await expect(owner).toHaveAttribute('href', `#/user/${USER}`)
  await expect(adminPage.getByRole('table').locator('tbody tr')).toHaveCount(filteredBots.bots.length)
  await adminMonitor.expectClean()
  await adminContext.close()
})

test('admin abort cancels a live human match and cannot be overwritten by the runner', async ({
  page,
  browser,
  baseURL,
  request,
}) => {
  expect(baseURL).toBeTruthy()
  let matchId: string | null = null
  let adminContext: BrowserContext | null = null
  await withCleanup(async () => {
    const humanMonitor = monitorBrowser(page)
    await loginThroughUi(page, OTHER_USER)
    await page.goto('/#/challenge')
  await page.getByRole('button', { name: '我亲自上场', exact: true }).click()
  await chooseBot(
    page,
    page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }),
    `${OTHER_USER}_holdem`,
    false,
  )
  const startResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/matches/human',
  )
  await page.getByRole('button', { name: '开始人类对战', exact: true }).click()
  const startResponse = await startResponsePromise
  const createdMatchId = await waitForAcceptedExecutionMatch(page, startResponse, 'human')
  matchId = createdMatchId
  await expect(page.getByRole('button', { name: '弃牌', exact: true })).toBeEnabled({
    timeout: 20_000,
  })

  adminContext = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
  })
  const adminPage = await adminContext.newPage()
  const adminMonitor = monitorBrowser(adminPage)
  await loginThroughUi(adminPage, ADMIN)
  await adminPage.goto('/#/admin')
  await adminPage.getByRole('button', { name: '对局记录', exact: true }).click()
  const runningResponsePromise = adminPage.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET' &&
      url.pathname === '/api/matches' &&
      url.search === '?status=running&limit=20&offset=0'
  })
  await adminPage.getByRole('combobox').filter({ hasText: '全部状态' }).click()
  await adminPage.getByRole('option', { name: '进行中', exact: true }).click()
  const runningResponse = await runningResponsePromise
  expect(runningResponse.status(), await runningResponse.text()).toBe(200)
  const runningMatches = await runningResponse.json() as {
    matches: Array<{ id: string; status: string }>
  }
  expect(runningMatches.matches.every((match) => match.status === 'running')).toBe(true)
  expect(runningMatches.matches.some((match) => match.id === createdMatchId)).toBe(true)
  const matchRow = adminPage
    .getByText(`${createdMatchId.slice(0, 16)}…`, { exact: true })
    .locator('xpath=ancestor::tr[1]')
  await expect(matchRow).toBeVisible()

  const abortResponsePromise = adminPage.waitForResponse(
    (response) =>
      response.request().method() === 'PATCH' &&
      new URL(response.url()).pathname === `/api/admin/matches/${createdMatchId}`,
  )
  await matchRow.getByRole('button', { name: '中止', exact: true }).click()
  const confirmation = adminPage.getByRole('dialog').filter({ hasText: createdMatchId })
  await confirmation.getByRole('button', { name: '中止', exact: true }).click()
  const abortResponse = await abortResponsePromise
  expect(abortResponse.status(), await abortResponse.text()).toBe(200)

  await expect(page.getByText(/对局结束/)).toBeVisible()
  await expect(page.locator('main')).toContainText('管理员中止')
  await expect(page.locator('main')).not.toContainText('admin-abort')
  await expect.poll(async () => {
    const response = await request.get(`/api/matches/${createdMatchId}`)
    const body = await response.json() as { match?: { status?: string } }
    return body.match?.status
  }).toBe('aborted')
  // Stay past the local runner cancellation/cleanup turn; the old direct DB patch
  // briefly showed aborted and was then overwritten to completed with ratings.
  await page.waitForTimeout(750)
  const finalResponse = await request.get(`/api/matches/${createdMatchId}`)
  expect((await finalResponse.json() as { match: { status: string } }).match.status).toBe('aborted')

  await humanMonitor.expectClean()
  await adminMonitor.expectClean()
  await adminContext.close()
  adminContext = null
  }, async () => {
    const tasks: Array<{ label: string; run: () => Promise<void> }> = []
    if (matchId) {
      const createdMatchId = matchId
      tasks.push({
        label: `settle human match ${createdMatchId}`,
        run: () => ensureMatchTerminal(browser, baseURL!, request, createdMatchId),
      })
    }
    if (adminContext) {
      const context = adminContext
      tasks.push({ label: 'close human-abort admin context', run: () => context.close() })
    }
    await runCleanupTasks(tasks)
  })
})

test('unknown match game is an explicit unsupported state, never a Holdem replay', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-unsupported-game'
  const replayEvents = [
    { type: 'match_start' },
    { type: 'match_end', winner: 0, reason: 'completed', deltas: [1, -1] },
  ]
  let replayRequests = 0
  await page.route(`**/api/matches/${matchId}/replay`, async (route) => {
    replayRequests += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        match_id: matchId,
        events: replayEvents,
        event_count: replayEvents.length,
        updated_at: null,
      }),
    })
  })
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
          game_id: 'future_chess',
          status: 'completed',
          winner: 0,
          result: { rounds_played: 1, deltas: [1, -1] },
        },
      }),
    })
  })
  await page.route('**/api/comments?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ comments: [], count: 0, total: 0 }),
    })
  })

  await page.goto(`/#/match/${matchId}`)
  await expect(page.getByText('不支持的游戏（future_chess）').first()).toBeVisible()
  await expect(page.getByText('回放不可用：不支持的游戏（future_chess）')).toBeVisible()
  await expect(page.getByRole('img', { name: /holdem 对局画面/ })).toHaveCount(0)
  expect(replayRequests).toBe(0)
  await monitor.expectClean()
})

test('Gomoku human canvas serializes its canonical response envelope', async ({ page }) => {
  const monitor = monitorBrowser(page)
  const matchId = 'mock-gomoku-human-action'
  let sentAction: Record<string, unknown> | null = null

  await page.routeWebSocket((url) => url.pathname === `/api/matches/${matchId}/play`, (socket) => {
    socket.onMessage((message) => {
      sentAction = JSON.parse(String(message)) as Record<string, unknown>
    })
    setTimeout(() => {
      socket.send(JSON.stringify({
        type: 'snapshot',
        match: {
          id: matchId,
          game_id: 'gomoku',
          status: 'running',
          match_type: 'human',
          human_seat: 1,
          bot_a: { name: 'mock_bot', owner_name: 'tester1' },
          bot_b: { owner_name: 'tester2', is_human: true },
        },
        events: [
          { type: 'match_start', size: 15 },
          { type: 'turn', player: 1 },
          { type: 'your_turn', player: 1, request: { x: -1, y: -1, me: 1 } },
        ],
      }))
    }, 0)
  })

  await page.goto(`/#/play/${matchId}`)
  await expect(page.getByText('轮到你落子')).toBeVisible()
  const canvas = page.getByRole('img', { name: 'gomoku 对局画面' })
  await expect(canvas).toBeVisible()
  await canvas.click({ position: { x: 300, y: 240 } })
  await expect.poll(() => sentAction).not.toBeNull()
  expect(Object.keys(sentAction!)).toEqual(['response'])
  expect(Object.keys(sentAction!.response as Record<string, unknown>).sort()).toEqual(['x', 'y'])
  expect(Number.isInteger((sentAction!.response as Record<string, unknown>).x)).toBe(true)
  expect(Number.isInteger((sentAction!.response as Record<string, unknown>).y)).toBe(true)
  await monitor.expectClean()
})

test('real Pencil human play accepts several canvas-picked edges without illegal_move', async ({
  page,
  browser,
  baseURL,
  request,
}) => {
  test.setTimeout(150_000)
  expect(baseURL).toBeTruthy()
  let matchId: string | null = null

  await withCleanup(async () => {
    const monitor = monitorBrowser(page)
    const received: Array<Record<string, unknown>> = []
    const sent: Array<Record<string, unknown>> = []
    await loginThroughUi(page, OTHER_USER)
    await page.goto('/#/challenge')

    const gameSelect = page.getByRole('combobox').first()
    await gameSelect.click()
    await page.getByRole('option', { name: '点格棋', exact: true }).click()
    await page.getByRole('button', { name: '我亲自上场', exact: true }).click()
    await chooseBot(
      page,
      page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }),
      `${OTHER_USER}_pencil`,
      false,
    )

    page.on('websocket', (socket) => {
      if (!/^\/api\/matches\/[^/]+\/play$/.test(new URL(socket.url()).pathname)) return
      socket.on('framereceived', (frame) => {
        const event = JSON.parse(String(frame.payload)) as Record<string, unknown>
        if (event.type === 'snapshot' && Array.isArray(event.events)) {
          // snapshot 是完整权威前缀，必须替换本地镜像；追加会把重连历史重复
          // 计算成新 move/your_turn，让未被裁判接受的动作产生假阳性。
          received.splice(0, received.length, ...event.events as Array<Record<string, unknown>>)
        } else {
          received.push(event)
        }
      })
      socket.on('framesent', (frame) => {
        sent.push(JSON.parse(String(frame.payload)) as Record<string, unknown>)
      })
    })

    const startResponsePromise = page.waitForResponse(
      (response) => response.request().method() === 'POST'
        && new URL(response.url()).pathname === '/api/matches/human',
    )
    await page.getByRole('button', { name: '开始人类对战', exact: true }).click()
    const startResponse = await startResponsePromise
    matchId = await waitForAcceptedExecutionMatch(page, startResponse, 'human')

    const canvas = page.locator('canvas[aria-label^="pencil 对局画面"]')
    await expect(canvas).toBeVisible({ timeout: 20_000 })

    const waitForEdgeTurn = async () => {
      const passButton = page.getByRole('button', { name: '确认让行', exact: true })
      for (let passCount = 0; passCount < 25; passCount++) {
        await expect.poll(async () => (
          await page.getByText('轮到你连边', { exact: false }).isVisible()
          || (await passButton.isVisible() && await passButton.isEnabled())
        ), { timeout: 20_000 }).toBe(true)
        if (await page.getByText('轮到你连边', { exact: false }).isVisible()) return
        const sentBeforePass = sent.length
        await passButton.click()
        await expect.poll(() => sent.length).toBe(sentBeforePass + 1)
        expect(sent.at(-1)).toEqual({ response: { x: -1, y: -1 } })
      }
      throw new Error('Pencil Bot produced more than 25 consecutive pass turns')
    }

    // Pick interior edges only. The authoritative received move list is refreshed
    // before each click, so the real Bot can never race us onto the same edge.
    for (let turn = 0; turn < 3; turn++) {
      await waitForEdgeTurn()
      await expect(canvas).not.toHaveAttribute('data-pick-state', 'inactive', { timeout: 20_000 })
      const occupied = new Set(received
        .filter((event) => event.type === 'move')
        .map((event) => `${Number(event.x)},${Number(event.y)}`))
      let coordinate: { x: number; y: number } | null = null
      for (let y = 1; y < 10 && !coordinate; y++) {
        for (let x = 1; x < 10; x++) {
          if ((x + y) % 2 !== 1 || occupied.has(`${x},${y}`)) continue
          coordinate = { x, y }
          break
        }
      }
      expect(coordinate).not.toBeNull()
      const sentBefore = sent.length
      const humanTurnsBefore = received.filter(
        (event) => event.type === 'your_turn' && Number(event.player) === 1,
      ).length
      const position = await pencilCanvasPoint(canvas, coordinate!.x, coordinate!.y)
      await canvas.click({ position })
      await expect.poll(() => sent.length).toBe(sentBefore + 1)
      expect(sent.at(-1)).toEqual({ response: coordinate })
      await expect.poll(() => received.some(
        (event) => event.type === 'move'
          && Number(event.player) === 1
          && Number(event.x) === coordinate!.x
          && Number(event.y) === coordinate!.y,
      ), { timeout: 20_000 }).toBe(true)
      expect(received.some(
        (event) => event.type === 'illegal' && Number(event.player) === 1,
      )).toBe(false)
      if (turn < 2) {
        await expect.poll(() => received.filter(
          (event) => event.type === 'your_turn' && Number(event.player) === 1,
        ).length, { timeout: 20_000 }).toBeGreaterThan(humanTurnsBefore)
      }
    }

    expect(received.filter(
      (event) => event.type === 'move' && Number(event.player) === 1,
    ).length).toBeGreaterThanOrEqual(3)
    await ensureMatchTerminal(browser, baseURL!, request, matchId)
    await expect(page.getByText(/对局结束/)).toBeVisible({ timeout: 20_000 })
    await monitor.expectClean()
  }, async () => {
    if (matchId) await ensureMatchTerminal(browser, baseURL!, request, matchId)
  })
})

test('human Holdem uses one WebSocket per load, sends legal protocol, and finishes', async ({
  page,
  request,
  browser,
  baseURL,
}) => {
  test.setTimeout(150_000)
  expect(baseURL).toBeTruthy()
  let matchId: string | null = null
  await withCleanup(async () => {
    const monitor = monitorBrowser(page)
    await loginThroughUi(page, OTHER_USER)
    await page.goto('/#/challenge')
  await page.getByRole('button', { name: '我亲自上场', exact: true }).click()
  await chooseBot(
    page,
    page.getByRole('button', { name: '选择 Bot（搜索 / 我的 / 按用户）', exact: true }),
    `${OTHER_USER}_holdem`,
    false,
  )
  await expect(page.getByText('人类对战使用该 Bot 的当前激活版本')).toBeVisible()
  await expect(page.getByRole('combobox')).toHaveCount(1) // only game; no ignored version selector

  // Switching back keeps an owned seat-1 Bot and must lazily populate its history;
  // selecting it first in human mode previously left the version cache empty.
  await page.getByRole('button', { name: '选 Bot', exact: true }).click()
  await expect(page.getByRole('combobox')).toHaveCount(2)
  await page.getByRole('combobox').nth(1).click()
  await expect(page.getByRole('option', { name: /v\d+/ }).first()).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '我亲自上场', exact: true }).click()
  await expect(page.getByRole('combobox')).toHaveCount(1)

  const sockets: Array<{ sent: string[]; received: string[]; closed: boolean }> = []
  page.on('websocket', (socket) => {
    // Vite opens its own `/?token=...` HMR socket on every reload. Count only the
    // human-play business channel, otherwise one reload looks like two app sockets.
    if (!/^\/api\/matches\/[^/]+\/play$/.test(new URL(socket.url()).pathname)) return
    const record = { sent: [] as string[], received: [] as string[], closed: false }
    sockets.push(record)
    socket.on('framesent', (event) => record.sent.push(String(event.payload)))
    socket.on('framereceived', (event) => record.received.push(String(event.payload)))
    socket.on('close', () => { record.closed = true })
  })
  const startResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/matches/human',
  )
  await page.getByRole('button', { name: '开始人类对战', exact: true }).click()
  const startedResponse = await startResponse
  const createdMatchId = await waitForAcceptedExecutionMatch(page, startedResponse, 'human')
  matchId = createdMatchId
  await expect(page).toHaveURL(/\/#\/play\//)

  const fold = page.getByRole('button', { name: '弃牌', exact: true })
  const sentFoldCount = () => sockets
    .flatMap((socket) => socket.sent)
    .filter((frame) => frame.includes('"response":-1')).length
  const receivedActionCount = () => sockets
    .flatMap((socket) => socket.received)
    .filter((frame) => frame.includes('"type":"action"')).length
  const sendOneFold = async () => {
    await expect(fold).toBeEnabled({ timeout: 20_000 })
    const sentBefore = sentFoldCount()
    const actionsBefore = receivedActionCount()
    await fold.click()
    await expect.poll(sentFoldCount).toBe(sentBefore + 1)
    // Wait until the engine has consumed this turn. Merely waiting for disabled is
    // racy with a local fast Bot: the next your_turn can re-enable before assertion.
    await expect.poll(receivedActionCount).toBeGreaterThan(actionsBefore)
    expect(sentFoldCount()).toBe(sentBefore + 1)
  }
  const sendRapidDoubleFold = async () => {
    await expect(fold).toBeEnabled({ timeout: 20_000 })
    const sentBefore = sentFoldCount()
    const actionsBefore = receivedActionCount()
    // Two native clicks in one browser task reproduce the same-render race: React
    // state alone has not necessarily committed before the second handler runs.
    await fold.evaluate((element) => {
      const button = element as HTMLButtonElement
      button.click()
      button.click()
    })
    await expect.poll(sentFoldCount).toBe(sentBefore + 1)
    await expect.poll(receivedActionCount).toBeGreaterThan(actionsBefore)
    // A duplicate frame can be rejected only after the accepted frame advances
    // the engine, so assert again after the authoritative action arrives.
    expect(sentFoldCount()).toBe(sentBefore + 1)
  }
  await expect(fold).toBeEnabled({ timeout: 20_000 })
  await sendRapidDoubleFold()
  await page.reload()
  await expect(fold).toBeEnabled({ timeout: 20_000 })

  let folds = 1
  const deadline = Date.now() + 70_000
  while (Date.now() < deadline && !(await page.getByText(/对局结束/).isVisible())) {
    if (await fold.isEnabled()) {
      await sendOneFold()
      folds += 1
    } else {
      await page.waitForTimeout(25)
    }
  }
  await expect(page.getByText(/对局结束/)).toBeVisible()
  expect(folds).toBeGreaterThan(1)
  expect(sockets).toHaveLength(2)
  await expect.poll(() => sockets[1]?.closed).toBe(true)
  for (const socket of sockets) {
    expect(socket.received.filter((frame) => frame.includes('"type":"snapshot"'))).toHaveLength(1)
  }
  expect(sockets.flatMap((socket) => socket.sent).some((frame) => frame.includes('"response":-1'))).toBe(true)
  await expect(page.locator('main')).not.toContainText('座0')

  // The UI terminal message is not sufficient evidence: verify the authoritative
  // persisted row reached completed before testing refresh restoration.
  await expect.poll(async () => {
    const response = await request.get(`/api/matches/${createdMatchId}`)
    if (response.status() !== 200) return `http-${response.status()}`
    return (await response.json() as { match?: { status?: string } }).match?.status
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe('completed')

  // A completed match must restore its terminal state from the snapshot and close
  // cleanly instead of showing "waiting" forever with a leaked subscription.
  await page.reload()
  await expect(page.getByText(/对局结束/)).toBeVisible()
  expect(sockets).toHaveLength(3)
  await expect.poll(() => sockets[2]?.closed).toBe(true)
  expect(sockets[2].received.filter((frame) => frame.includes('"type":"snapshot"'))).toHaveLength(1)
  await monitor.expectClean()
  }, async () => {
    if (matchId) {
      await ensureMatchTerminal(browser, baseURL!, request, matchId)
    }
  })
})
