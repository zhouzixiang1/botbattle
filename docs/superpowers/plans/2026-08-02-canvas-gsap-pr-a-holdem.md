# PR-A: Canvas+GSAP 基建 + Holdem 扑克牌桌 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 GSAP + Poker.JS，搭建通用 `<GameCanvas>` 框架（canvas↔React 桥接），把 holdem 扑克牌桌从 DOM 重写为 canvas + GSAP 时间线动画，全面复刻 botzone 视觉（发牌翻面/动作浮字/筹码插值/结算覆盖），并删除旧 DOM PokerTable。

**Architecture:** React 持有 events[]（SSE/WS/回放不变），`useCanvasGameRenderer` hook 监听 events 增量，调 holdem 的 `toScene`（复用现有 reducer）→ `diff` 找新增 → 驱动 GSAP `tl.to(animdata, dur, {t:0→1, onUpdate: draw})`，每帧用 Poker.JS 在 `<canvas>` 重绘整个场景。每游戏实现统一 `GameCanvasRenderer` 接口，经 `GameViewSpec.CanvasRenderer` 注册。

**Tech Stack:** React 19 + TypeScript + Vite 8 + Tailwind v4；新增 `gsap`(npm 3.x) + 本地 vendor `Poker.JS`；canvas 2D API。

## Global Constraints

- **包名 `bzplat`，禁用相对路径，前端用 `@/` 别名**（AGENTS.md）。
- **不裸 hex / 不硬编码 slate-brand**：canvas 内绘制色值用从 botzone 爬取的固定色（绿桌 `green`、红牌 `#a22`、黑牌 `#000`、黄字 `rgba(255,238,88,α)`）；DOM 外壳仍用语义 token。
- **改完 rebuild+restart**：`bash scripts/rebuild.sh`（前端 build → 后端 restart）。
- **验证**：`npm run build` + `pytest`（后端未动应 345 pass）+ 截图 `scripts/screenshot_verify.py`。
- **无前端测试框架**（无 vitest/jest）→ 用 build + 截图 + 浏览器实测验证（每个任务明确验证方式）。
- **botzone 色值/布局基准**（从 `TexasHoldem2p.html` 爬取，照搬）：椭圆桌两圆心距 `L=230`、半径 `R=190`；CARD_SIZE=100；红牌 `#a22`、黑牌 `#000`；动作浮字 `rgba(255,238,88,1-t^4)` + `shadowBlur=10` + 上浮 `-20-10*t^4`。

---

## File Structure

**新增：**
- `bzplat/frontend/src/lib/pokerjs/poker.min.js` — Poker.JS vendor（从 botzone 爬取的 10KB，加 license 注释头）。
- `bzplat/frontend/src/lib/pokerjs/index.ts` — 薄 wrapper：动态注入 poker.min.js 到 canvas 原型（Poker.JS 给 `CanvasRenderingContext2D.prototype` 挂 `drawPokerCard/drawPokerBack`）。
- `bzplat/frontend/src/lib/pokerjs/pokerjs.d.ts` — TS 声明：`drawPokerCard(x,y,size,suit,point)` / `drawPokerBack(x,y,size)`。
- `bzplat/frontend/src/components/GameCanvas.tsx` — 通用 `<canvas>` + `useCanvasGameRenderer(gameId, events, canvasRef)` hook（GSAP timeline 驱动）。
- `bzplat/frontend/src/games/base.ts`（改）— `GameViewSpec` 增 `CanvasRenderer?: GameCanvasRenderer`。
- `bzplat/frontend/src/games/canvas-types.ts` — `GameCanvasRenderer<S>` 接口 + `SceneDelta`/`DrawOpts` 类型。
- `bzplat/frontend/src/games/holdem/canvas.ts` — `PokerCanvasRenderer`：`toScene`/`diff`/`draw`（复刻 botzone drawData）。

**修改：**
- `bzplat/frontend/src/games/holdem/index.ts` — `holdemSpec` 挂 `CanvasRenderer`。
- `bzplat/frontend/src/components/MatchBoard.tsx` — 优先渲染 `<GameCanvas>`（有 CanvasRenderer 时）。
- `bzplat/frontend/src/pages/HumanPlay.tsx` — holdem 用 `<GameCanvas>`，DOM 操作按钮绝对定位覆盖。
- `bzplat/frontend/package.json` — `+gsap`。
- `doc/DEVELOPMENT.md` — 记录 gsap 依赖。

**删除：**
- `bzplat/frontend/src/components/poker/PokerTable.tsx`
- `bzplat/frontend/src/components/poker/PlayingCard.tsx`（canvas 用 Poker.JS，不再需要 DOM 牌）
- `bzplat/frontend/src/components/poker/CardRow`（在 PlayingCard.tsx 内）

**保留（不动）：**
- `bzplat/frontend/src/components/poker/useMatchState.ts` — reducer，`toScene` 复用它。
- gomoku/pencil 的 DOM 棋盘（PR-B/C 处理）。

---

## Task 1: 引入 gsap 依赖 + Poker.JS vendor

**Files:**
- Modify: `bzplat/frontend/package.json`
- Create: `bzplat/frontend/src/lib/pokerjs/poker.min.js`, `index.ts`, `pokerjs.d.ts`
- Test: `npm run build` 能跑通

**Interfaces:**
- Produces: `@/lib/pokerjs` 导出 `ensurePokerJS(ctx)` —— 把 Poker.JS 的 `drawPokerCard/drawPokerBack` 挂到给定 ctx 的 prototype（幂等）。

- [ ] **Step 1: 安装 gsap**

```bash
cd bzplat/frontend && npm install gsap
```
确认 `package.json` 出现 `"gsap": "^3.x"`。

- [ ] **Step 2: 落地 Poker.JS vendor 文件**

把已爬取的 `/tmp/PokerJS_poker.min.js`（botzone 的 `FightTheLandlord2/poker.min.js`，10KB）复制到 `bzplat/frontend/src/lib/pokerjs/poker.min.js`，并在文件**顶部**加 license 注释：

```js
/** Poker.JS — canvas 矢量扑克牌渲染（来源 https://github.com/Tairraos/Poker.JS，经 botzone.org.cn 使用）
 *  原作者 Tairraos。本文件为 vendor 副本，照搬自 botzone。 */
```

- [ ] **Step 3: 写 index.ts wrapper（动态注入 prototype）**

```ts
// bzplat/frontend/src/lib/pokerjs/index.ts
let injected = false
const SRC = `/* eslint-disable */
// @ts-nocheck
` + (await import('./poker.min.js?raw')).default

/** 把 Poker.JS 的 drawPokerCard/drawPokerBack 挂到 ctx 的 prototype（幂等）。 */
export function ensurePokerJS(ctx: CanvasRenderingContext2D): void {
  if (injected) return
  // Poker.JS 自执行函数会给 CanvasRenderingContext2D.prototype 挂方法
  // 用 new Function 在全局作用域执行一次
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  const fn = new Function(SRC)
  fn.call(globalThis)
  injected = true
}
```
> 注：Poker.JS 是 IIFE，执行后给 `CanvasRenderingContext2D.prototype` 挂 `drawPokerCard`/`drawPokerBack`。`?raw` 让 Vite 以字符串导入。`injected` 保证只执行一次。

- [ ] **Step 4: 写 TS 声明 pokerjs.d.ts**

```ts
// bzplat/frontend/src/lib/pokerjs/pokerjs.d.ts
export {}
declare global {
  interface CanvasRenderingContext2D {
    drawPokerCard(x: number, y: number, size: number, suit: 'h'|'d'|'s'|'c', point: string): void
    drawPokerBack(x: number, y: number, size: number): void
  }
}
```

- [ ] **Step 5: build 验证**

Run: `cd bzplat/frontend && npm run build`
Expected: 编译通过（gsap + pokerjs vendor 无错）。

- [ ] **Step 6: 提交**

```bash
git add bzplat/frontend/package.json bzplat/frontend/package-lock.json bzplat/frontend/src/lib/pokerjs/
git commit -m "feat(canvas): 引入 gsap + Poker.JS vendor（PR-A 基建）"
```

---

## Task 2: GameCanvasRenderer 接口 + GameViewSpec 注册

**Files:**
- Create: `bzplat/frontend/src/games/canvas-types.ts`
- Modify: `bzplat/frontend/src/games/base.ts`（GameViewSpec 增 CanvasRenderer 字段）

**Interfaces:**
- Produces: `GameCanvasRenderer<S>`（`toScene`/`diff`/`draw`）、`SceneDelta`、`DrawOpts`；`GameViewSpec` 增可选 `CanvasRenderer`。

- [ ] **Step 1: 写 canvas-types.ts**

```ts
// bzplat/frontend/src/games/canvas-types.ts
import type { RawEvent } from '@/components/poker/useMatchState'

/** 一个游戏的归一化场景（由 toScene 从 events 归约）。 */
export type Scene = Record<string, unknown>

/** 场景差分：哪些是"新"的需动画。每游戏自定义结构。 */
export type SceneDelta = { animation: 'deal' | 'place' | 'settle' | 'none'; payload?: unknown }

export interface DrawOpts {
  width: number
  height: number
  /** 可选：座位身份（Bot 名/用户名），用于绘制座位标签 */
  seats?: SeatInfo[]
}
export interface SeatInfo {
  botName?: string
  ownerName?: string
  isHuman?: boolean
}

/** 每游戏 canvas 渲染器统一接口（canvas↔React 桥接契约）。 */
export interface GameCanvasRenderer<S extends Scene = Scene> {
  /** events → 归一化场景（通常复用现有 reducer）。 */
  toScene(events: RawEvent[]): S
  /** 两场景差分，决定触发哪种动画。 */
  diff(prev: S | null, next: S): SceneDelta
  /** 每帧绘制：在 prev↔next 间用插值 t(0→1) 画。 */
  draw(ctx: CanvasRenderingContext2D, prev: S | null, next: S, t: number, opts: DrawOpts): void
}
```

- [ ] **Step 2: 改 base.ts GameViewSpec 增字段**

读 `bzplat/frontend/src/games/base.ts`，在 `GameViewSpec` interface 加可选字段：

```ts
import type { GameCanvasRenderer } from './canvas-types'
// ...
export interface GameViewSpec {
  kind: 'board' | 'cards'
  Board: React.ComponentType<BoardProps>
  reduce: (events: RawEvent[]) => unknown
  /** canvas 渲染器（有则 GameCanvas 优先用，替代 DOM Board）。 */
  CanvasRenderer?: GameCanvasRenderer
  defaultMatchConfig: Record<string, unknown>
  configFields: ConfigField[]
}
```

- [ ] **Step 3: build 验证**

Run: `cd bzplat/frontend && npm run build`
Expected: 通过（无 renderer 实现时，字段可选不影响现有 gomoku/pencil/holdem spec）。

- [ ] **Step 4: 提交**

```bash
git add bzplat/frontend/src/games/canvas-types.ts bzplat/frontend/src/games/base.ts
git commit -m "feat(canvas): GameCanvasRenderer 接口 + GameViewSpec.CanvasRenderer 字段"
```

---

## Task 3: GameCanvas 通用组件 + useCanvasGameRenderer hook

**Files:**
- Create: `bzplat/frontend/src/components/GameCanvas.tsx`

**Interfaces:**
- Consumes: `GameCanvasRenderer`（Task 2）、`getGame(gameId)`（现有注册表）。
- Produces: `<GameCanvas gameId events seats width height />` 组件 + `useCanvasGameRenderer` hook。

- [ ] **Step 1: 写 GameCanvas.tsx**

```tsx
// bzplat/frontend/src/components/GameCanvas.tsx
import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { getGame } from '@/games'
import type { RawEvent } from '@/components/poker/useMatchState'
import type { SeatInfo } from '@/games/canvas-types'

interface Props {
  gameId?: string | null
  events: RawEvent[]
  seats?: SeatInfo[]
  width?: number
  height?: number
  className?: string
}

export default function GameCanvas({ gameId, events, seats, width = 900, height = 600, className }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stateRef = useRef<{ prev: unknown; next: unknown } | null>(null)
  const tlRef = useRef<gsap.core.Timeline | null>(null)
  const lastEventsLenRef = useRef(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const spec = getGame(gameId)
    const renderer = spec.CanvasRenderer
    if (!renderer) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // 适配 devicePixelRatio（高清屏不糊）
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)

    // 首次或 events 增长 → 重算场景 + 跑动画
    if (events.length === lastEventsLenRef.current) return
    lastEventsLenRef.current = events.length

    const next = renderer.toScene(events)
    const prev = stateRef.current?.next ?? null
    const delta = renderer.diff(prev, next)
    stateRef.current = { prev, next }

    // 杀掉旧 timeline，建新的（同 botzone：每个状态变化一个 tl）
    tlRef.current?.kill()
    if (delta.animation === 'none') {
      renderer.draw(ctx, prev, next, 1, { width, height, seats })
      return
    }
    const animdata = { t: 0 }
    const dur = delta.animation === 'settle' ? 1.0 : 0.5
    tlRef.current = gsap.timeline()
    tlRef.current.to(animdata, {
      t: 1,
      duration: dur,
      ease: 'power2.out',
      onUpdate: () => renderer.draw(ctx, prev, next, animdata.t, { width, height, seats }),
    })
  }, [gameId, events, seats, width, height])

  // 卸载清理
  useEffect(() => () => { tlRef.current?.kill() }, [])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: 'auto', maxWidth: width }}
      className={className}
      role="img"
      aria-label={`${gameId ?? ''} 对局画面`}
    />
  )
}
```

- [ ] **Step 2: build 验证**

Run: `cd bzplat/frontend && npm run build`
Expected: 通过。

- [ ] **Step 3: 提交**

```bash
git add bzplat/frontend/src/components/GameCanvas.tsx
git commit -m "feat(canvas): GameCanvas 通用组件 + useCanvasGameRenderer（GSAP timeline 驱动）"
```

---

## Task 4: Holdem PokerCanvasRenderer（复刻 botzone drawData）

**Files:**
- Create: `bzplat/frontend/src/games/holdem/canvas.ts`

**Interfaces:**
- Consumes: `useMatchState` reducer（现有）、Poker.JS（Task 1）、`GameCanvasRenderer`（Task 2）。
- Produces: `PokerCanvasRenderer`（holdem 的 canvas 渲染器）。

> 关键复刻来源：botzone `TexasHoldem2p.html` 的 `drawBackground`/`drawCards`/`drawPlayer`/`drawAction`/`drawData`。色值/布局见 Global Constraints。

- [ ] **Step 1: 写 holdem/canvas.ts（toScene + diff + draw）**

```ts
// bzplat/frontend/src/games/holdem/canvas.ts
import type { RawEvent } from '@/components/poker/useMatchState'
import { reduceEvents } from '@/components/poker/useMatchState'
import type { GameCanvasRenderer, Scene, SceneDelta, DrawOpts, SeatInfo } from '@/games/canvas-types'
import { ensurePokerJS } from '@/lib/pokerjs'

// botzone 布局常量（照搬）
const R = 190, L = 230, CARD_SIZE = 100
const POINT = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
const SUIT_BY_CODE: Record<string, 'h'|'d'|'s'|'c'> = { h:'h', d:'d', s:'s', c:'c' }

interface HoldemScene extends Scene {
  hand: number
  chips: [number, number]
  pot: number
  holes: (string[]|null)[]   // 每座手牌 [[card,card],...]
  board: string[]            // 公共牌
  street: string
  toAct: number | null
  lastAction: { player: number; action: string; amount?: number } | null
  roundBets: number[]        // 每座本轮已下注
  winners: number[] | null
  matchOver: boolean
  folded: boolean[]
  allin: boolean[]
}

function parseCardCode(card: string): { suit: 'h'|'d'|'s'|'c'; point: string } | null {
  if (!card || card.length < 2) return null
  const rankCh = card[0].toUpperCase().replace('T','10')  // 协议 T → 显示 10
  const suit = SUIT_BY_CODE[card[1].toLowerCase()]
  if (!suit) return null
  const point = POINT.includes(rankCh) ? rankCh : rankCh === '10' ? '10' : null
  if (!point) return null
  return { suit, point }
}

export const PokerCanvasRenderer: GameCanvasRenderer<HoldemScene> = {
  toScene(events: RawEvent[]): HoldemScene {
    // reduceEvents 返回 { hand, sbSeat, street, board, pot, seats, toAct, lastSettle, matchOver, matchWinner, ... }
    // seats[i] = { hole:[c,c]|[null,null], chips, bet, folded, allin, isWinner, net, lastAction:{action,amount} }
    const vm = reduceEvents(events) as any
    const seats = vm.seats ?? []
    return {
      hand: vm.hand ?? 0,
      chips: [seats[0]?.chips ?? 20000, seats[1]?.chips ?? 20000],
      pot: vm.pot ?? 0,
      holes: seats.map((s: any) => (s?.hole?.[0] ? [s.hole[0], s.hole[1]].filter(Boolean) : null)),
      board: vm.board ?? [],
      street: vm.street ?? 'preflop',
      toAct: vm.toAct ?? null,
      lastAction: (() => {
        // 取最后一个有 lastAction 的座位（当前行动方）
        for (let i = 0; i < seats.length; i++) {
          if (seats[i]?.lastAction) return { player: i, action: seats[i].lastAction.action, amount: seats[i].lastAction.amount }
        }
        return null
      })(),
      roundBets: seats.map((s: any) => s?.bet ?? 0),
      winners: vm.lastSettle?.winners ?? null,
      matchOver: !!vm.matchOver,
      folded: seats.map((s: any) => !!s?.folded),
      allin: seats.map((s: any) => !!s?.allin),
    }
  },
  diff(prev: HoldemScene | null, next: HoldemScene): SceneDelta {
    if (!prev) return { animation: 'deal' }
    if (next.matchOver && !prev.matchOver) return { animation: 'settle' }
    if (next.hand !== prev.hand) return { animation: 'settle' }
    const newCards = (next.board.length > (prev.board?.length ?? 0)) ||
      next.holes.some((h,i) => (h?.length ?? 0) > ((prev.holes[i]?.length) ?? 0))
    if (newCards) return { animation: 'deal' }
    if (JSON.stringify(next.lastAction) !== JSON.stringify(prev.lastAction)) return { animation: 'place' }
    return { animation: 'none' }
  },
  draw(ctx, prev, next, t, opts) {
    ensurePokerJS(ctx)
    const W = opts.width, H = opts.height
    const X = (s:number) => W/2 + L*s
    const Y0 = H/2 - R*0.67, Y1 = H/2 + R*0.67

    // 清屏 + 椭圆桌（照搬 drawBackground）
    ctx.clearRect(0,0,W,H)
    ctx.beginPath()
    ctx.ellipse(W/2 - L, H/2, R, R, 0, Math.PI/2, Math.PI*3/2)
    ctx.ellipse(W/2 + L, H/2, R, R, 0, -Math.PI/2, Math.PI/2)
    ctx.closePath()
    ctx.fillStyle = '#0f5132'  // 深绿桌（比 botzone 'green' 略深，更耐看）
    ctx.fill()

    // 左侧信息：手数/轮/底池（随 t 插值底池）
    const pot = prev && prev.pot !== next.pot ? Math.round(prev.pot + (next.pot - prev.pot) * t) : next.pot
    ctx.font = 'bold 16px "DM Sans", sans-serif'
    ctx.fillStyle = '#fff'
    ctx.textAlign = 'left'
    ctx.fillText(`第 ${(next.hand||0)+1} 手`, X(-1.4), H/2 - 35)
    ctx.fillText(`轮: ${next.street}`, X(-1.4), H/2)
    ctx.fillText(`底池: ${pot}`, X(-1.4), H/2 + 35)

    // 座位（上=座1, 下=座0）
    drawSeat(ctx, X(-0.75), Y0, 1, next, prev, t, opts.seats)
    drawSeat(ctx, X(-0.75), Y1, 0, next, prev, t, opts.seats)

    // 手牌（翻面动画：新牌 scale(2|t-0.5|,1)）
    drawCards(ctx, X(0), Y0, next.holes[1], t, prev?.holes[1] ?? null)
    drawCards(ctx, X(0), Y1, next.holes[0], t, prev?.holes[0] ?? null)

    // 公共牌（5 槽，新发的翻面）
    const board = [...next.board]
    while (board.length < 5) board.push('' as any)
    const prevBoard = prev ? [...(prev.board||[])] : []
    while (prevBoard.length < 5) prevBoard.push('' as any)
    drawCommunity(ctx, X(0), H/2, board, prevBoard, t)

    // 动作浮字（黄字+阴影+上浮淡出）
    if (next.lastAction && (!prev || JSON.stringify(prev.lastAction) !== JSON.stringify(next.lastAction))) {
      drawActionFloat(ctx, X(0.75), next.lastAction.player === 0 ? Y1 : Y0, next.lastAction, t)
    }
  },
}

function drawSeat(ctx: CanvasRenderingContext2D, x: number, y: number, idx: number, next: HoldemScene, prev: HoldemScene | null, t: number, seats?: SeatInfo[]) {
  const info = seats?.[idx]
  const chips = prev && prev.chips[idx] !== next.chips[idx] ? Math.round(prev.chips[idx] + (next.chips[idx]-prev.chips[idx])*t) : next.chips[idx]
  const isToAct = next.toAct === idx && !next.matchOver
  ctx.textAlign = 'center'
  // 头像圆（首字母）
  const initial = info?.botName?.[0] ?? info?.ownerName?.[0] ?? '?'
  ctx.beginPath(); ctx.arc(x-25, y-45+25, 18, 0, Math.PI*2)
  ctx.fillStyle = idx === 0 ? '#3b82f6' : '#ef4444'; ctx.fill()
  ctx.fillStyle = '#fff'; ctx.font = 'bold 16px "DM Sans"'
  ctx.fillText(initial, x-25, y-45+25+5)
  // 名字（两行：BOT名 + @用户名）
  ctx.fillStyle = '#fff'; ctx.font = '13px "DM Sans"'
  if (isToAct) ctx.fillText('👉', x-45, y-12)
  ctx.fillText(info?.botName ?? `座位 ${idx}`, x, y+20)
  ctx.fillStyle = 'rgba(255,255,255,0.6)'; ctx.font = '11px "DM Sans"'
  ctx.fillText(info?.isHuman ? `@${info?.ownerName} (你)` : `@${info?.ownerName ?? ''}`, x, y+36)
  // 筹码
  ctx.fillStyle = '#fff'; ctx.font = 'bold 13px "DM Sans"'
  ctx.fillText(`筹码 ${chips}`, x, y+52)
}

function drawCards(ctx: CanvasRenderingContext2D, x: number, y: number, cards: string[]|null, t: number, prevCards: string[]|null, reveal = true) {
  if (!cards || !cards.length) return
  const d = (CARD_SIZE * 3.3) / 4
  const x0 = x - (d/2)*(cards.length-1) - (CARD_SIZE*3)/8
  for (let i = 0; i < cards.length; i++) {
    const isNew = !prevCards || !prevCards[i]
    const ct = isNew ? t : 1
    ctx.save()
    ctx.translate(x0 + i*d, y - CARD_SIZE/2)
    if (isNew && ct < 1) {
      ctx.translate(d * (0.5 - Math.abs(ct - 0.5)), 0)
      ctx.scale(2 * Math.abs(ct - 0.5), 1)
    }
    const parsed = parseCardCode(cards[i])
    if (!parsed || (isNew && ct < 0.5) || !reveal) {
      ;(ctx as any).drawPokerBack(0, 0, CARD_SIZE)
    } else {
      ;(ctx as any).drawPokerCard(0, 0, CARD_SIZE, parsed.suit, parsed.point)
    }
    ctx.restore()
  }
}

function drawCommunity(ctx: CanvasRenderingContext2D, x: number, y: number, board: string[], prevBoard: string[], t: number) {
  const d = (CARD_SIZE * 3.3) / 4
  const x0 = x - (d/2)*(board.length-1) - (CARD_SIZE*3)/8
  for (let i = 0; i < board.length; i++) {
    const isNew = !!board[i] && !prevBoard[i]
    const ct = isNew ? Math.min((t*1.5)/0.85, 1) : 1
    if (!board[i]) continue
    ctx.save()
    ctx.translate(x0 + i*d, y - CARD_SIZE/2)
    if (isNew && ct < 1) {
      ctx.translate(d * (0.5 - Math.abs(ct - 0.5)), 0)
      ctx.scale(2 * Math.abs(ct - 0.5), 1)
    }
    const parsed = parseCardCode(board[i])
    if (!parsed || (isNew && ct < 0.5)) {
      ;(ctx as any).drawPokerBack(0, 0, CARD_SIZE)
    } else {
      ;(ctx as any).drawPokerCard(0, 0, CARD_SIZE, parsed.suit, parsed.point)
    }
    ctx.restore()
  }
}

const ACTION_TEXT: Record<string, string> = { fold:'弃牌 Fold', check:'过牌 Check', call:'跟注 Call', raise:'加注 Raise', allin:'全押 AllIn' }
function drawActionFloat(ctx: CanvasRenderingContext2D, x: number, y: number, action: {action:string; amount?:number}, t: number) {
  const ty = Math.pow(t, 4)
  const txt = ACTION_TEXT[action.action] ?? action.action
  ctx.save()
  ctx.textAlign = 'center'
  ctx.font = 'bold 22px "DM Sans"'
  ctx.fillStyle = `rgba(255,238,88,${1-ty})`
  ctx.shadowColor = 'black'; ctx.shadowBlur = 10
  ctx.fillText(action.amount ? `${txt} ${action.amount}` : txt, x, y - 20 - 10*ty)
  ctx.restore()
}
```

- [ ] **Step 2: 挂到 holdemSpec**

读 `bzplat/frontend/src/games/holdem/index.ts`，import 并挂：

```ts
import { PokerCanvasRenderer } from './canvas'
// ...
export const holdemSpec: GameViewSpec = {
  // ...现有字段...
  CanvasRenderer: PokerCanvasRenderer,
}
```

- [ ] **Step 3: build 验证**

Run: `cd bzplat/frontend && npm run build`
Expected: 通过（Poker.JS `?raw` 导入 + canvas 绘制无 TS 错）。

- [ ] **Step 4: 提交**

```bash
git add bzplat/frontend/src/games/holdem/canvas.ts bzplat/frontend/src/games/holdem/index.ts
git commit -m "feat(canvas): Holdem PokerCanvasRenderer 复刻 botzone（发牌翻面/动作浮字/筹码插值）"
```

---

## Task 5: MatchBoard 切 canvas + 删 DOM PokerTable

**Files:**
- Modify: `bzplat/frontend/src/components/MatchBoard.tsx`
- Delete: `bzplat/frontend/src/components/poker/PokerTable.tsx`, `PlayingCard.tsx`
- Modify: 引用 PokerTable 的地方（grep 找）

**Interfaces:**
- Consumes: `GameCanvas`（Task 3）、`CanvasRenderer`（Task 4）。

- [ ] **Step 1: 改 MatchBoard 优先用 canvas**

```tsx
// bzplat/frontend/src/components/MatchBoard.tsx
import GameCanvas from './GameCanvas'
// ...
export default function MatchBoard({ gameId, events, seats, ... }) {
  const gid = normalizeGameId(gameId)
  const spec = getGame(gid)
  if (spec.CanvasRenderer) {
    return <GameCanvas gameId={gid} events={events} seats={seats} />
  }
  // 回退 DOM Board（gomoku/pencil 暂时走这，PR-B/C 加 CanvasRenderer 后自动切）
  const vm = useMemo(() => (events.length ? spec.reduce(events) : null), [spec, events])
  if (!vm) return null
  const Board = spec.Board
  return <Board vm={vm} onMove={handler} revealMode={revealMode} />
}
```
> MatchBoard 新增 `seats?: SeatInfo[]` prop（透传给 canvas）。

- [ ] **Step 2: 删 DOM 扑克组件**

```bash
rm bzplat/frontend/src/components/poker/PokerTable.tsx
rm bzplat/frontend/src/components/poker/PlayingCard.tsx
```

- [ ] **Step 3: 修引用 PokerTable 的地方**

```bash
grep -rln "PokerTable\|PlayingCard\|CardRow" bzplat/frontend/src --include="*.tsx" --include="*.ts"
```
预期：只有 holdem/index.ts 的 `Board` 字段引用 PokerTable。把 holdem spec 的 `Board` 字段改为**占位组件**（canvas 接管后 Board 不再用于 holdem；但 GameViewSpec 要求 Board 必填，给一个空 stub）：

```tsx
// holdem/index.ts
const HoldemBoardStub = () => null  // canvas 接管，DOM Board 不再用
export const holdemSpec = { ..., Board: HoldemBoardStub, CanvasRenderer: PokerCanvasRenderer }
```

- [ ] **Step 4: build + 截图验证**

Run: `cd bzplat/frontend && npm run build`
Expected: 通过，无 PokerTable/PlayingCard 引用残留。

截图验证：`python scripts/screenshot_verify.py`（或手动开 holdem 回放页看 canvas 牌桌渲染、发牌翻面动画、"10"正确显示）。

- [ ] **Step 5: 提交**

```bash
git add -A bzplat/frontend/src
git commit -m "feat(canvas): MatchBoard 切 canvas + 删 DOM PokerTable/PlayingCard（holdem 全 canvas）"
```

---

## Task 6: HumanPlay 扑克用 canvas + DOM 按钮覆盖

**Files:**
- Modify: `bzplat/frontend/src/pages/HumanPlay.tsx`

- [ ] **Step 1: holdem 分支用 GameCanvas**

读 `HumanPlay.tsx`，把 `{gameId === 'holdem' && (<><MatchBoard.../><HoldemActions.../>)</>)}` 改为：

```tsx
{gameId === 'holdem' && (
  <div className="relative">
    <MatchBoard gameId="holdem" events={events} seats={seatInfos} revealMode="all" />
    <HoldemActions
      disabled={!myTurn || over}
      legal={myTurn}
      onAct={(a, x) => sendMove(x !== undefined ? { a, x } : { a })}
    />
  </div>
)}
```
（`seatInfos` 从 match 的 bot_a/bot_b + owner 构造，可暂时传 undefined——canvas 会用"座位 0/1"兜底，座位名 JOIN 在主任务统一页做。）

- [ ] **Step 2: build + 手动验证人类对战**

Run: `bash scripts/rebuild.sh`
手动：开 holdem 人类对战，确认 canvas 牌桌显示、DOM 按钮可点（之前 PR#47 修的 myTurn 逻辑不受影响）、落子后 canvas 动画。

- [ ] **Step 3: 提交**

```bash
git add bzplat/frontend/src/pages/HumanPlay.tsx
git commit -m "feat(canvas): HumanPlay 扑克切 canvas + DOM 按钮覆盖"
```

---

## Task 7: 后端回归 + 文档同步 + PR

- [ ] **Step 1: 后端 pytest 回归**

Run: `cd /home/zzx/project/botbattle && source .venv/bin/activate && pytest -q`
Expected: 345 passed（后端未动）。

- [ ] **Step 2: 同步文档**

- `doc/DEVELOPMENT.md`：依赖表加 `gsap ^3.x`（前端）。
- `wiki/MATCH.md`：观赛视觉说明（holdem 改为 canvas 动画渲染，发牌翻面/动作浮字）。
- `doc/DESIGN.md` §前端：加 canvas 渲染层 + `GameViewSpec.CanvasRenderer` 字段说明。

- [ ] **Step 3: 推送 + 开 PR**

```bash
git push -u origin feat/canvas-gsap-visual-rewrite
gh pr create --title "feat(canvas): PR-A 基建 + Holdem 扑克 canvas 化（照搬 botzone）" --body "..."
```
PR body：说明范围（holdem canvas + 删 DOM 扑克）、gsap/Poker.JS 来源与许可、验证（build + 截图 + 345 pytest）、后续 PR-B/C。

- [ ] **Step 4: 合并后删分支 + rebuild**

```bash
gh pr merge --merge --delete-branch
git checkout main && git pull
bash scripts/rebuild.sh
```

---

## Self-Review（plan vs spec）

- **Spec 覆盖**：spec §2 技术选型（gsap npm + Poker.JS vendor）→ Task 1 ✓；§3 桥接架构（GameCanvasRenderer + GameCanvas）→ Task 2/3 ✓；§4.1 Holdem drawData 复刻 → Task 4 ✓；§5 删 DOM 扑克 → Task 5 ✓；§4 人类对战 DOM 按钮覆盖 → Task 6 ✓；§6 验证 + §5 文档 → Task 7 ✓。
- **Placeholder 扫描**：无 TBD/TODO；所有代码块完整。
- **类型一致**：`GameCanvasRenderer<S>`、`SeatInfo`、`DrawOpts` 在 Task 2 定义，Task 3/4 使用一致；`ensurePokerJS` Task 1 定义 Task 4 用。
- **未覆盖**：gomoku/pencil canvas（spec §4.2/4.3）→ 留 PR-B/C（本计划是 PR-A，scope 正确）。

## 风险提示（执行时注意）

- **Poker.JS `?raw` 导入 + `new Function` 执行**：若 Vite/TS 报错，备选是把 poker.min.js 内容直接 inline 到 index.ts 顶部（`/* eslint-disable */ ... `）。
- **`reduceEvents` 返回结构**：Task 4 的 `toScene` 假设 vm 有 `seats/hole/board/pot/toAct/lastAction/lastSettle/matchOver` 字段——执行时先 `console.log(vm)` 确认实际字段名（reducer 可能叫 `lastSettle` vs `settle`），按实际调整。
- **Poker.JS `drawPokerCard` 的 point 参数**：Poker.JS 接受 `"10"`（不是 T）——本计划 `parseCardCode` 已 `T→10`，与"牌 T 修复"一致。
