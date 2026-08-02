# Canvas+GSAP 视觉重写设计文档（照搬 botzone）

> 日期：2026-08-02 · 分支：`feat/canvas-gsap-visual-rewrite`
> 目标：把三游戏棋盘（holdem/gomoku/pencil）从 DOM 渲染重写为 **canvas + GSAP 时间线动画**，全面复刻 [botzone](https://botzone.org.cn/match/6a6edeaa27e7bf01db0d85e5) 的视觉与动效。**同时修复"牌 T"显示异常**（已先行修复）。

## 0. 附带修复：牌 T 异常（已完成，本分支首提交）

**根因**：后端紧凑协议用 `T` 表示 10（`cards.py` `RANK_CHARS="23456789TJQKA"`），前端 `parseCard` 直接显示 `T` 不归一化为 `10`。
**修复**：`PlayingCard.tsx` `parseCard` 两条路径（`"Td"` 与 `"<suit,rank>"`）均 `T → 10`。已 build 通过。
> 注：此修复独立于 canvas 重写；若 canvas 重写用 Poker.JS 自带牌面（Poker.JS 的 point 表已含 `"10"`），则 DOM `PlayingCard` 仅人类对战 DOM 按钮回退用。**保留此修复**。

## 1. botzone 视觉方案分析（已爬取，作为复刻基准）

爬取 `TexasHoldem2p.html` + `poker.min.js` + `matchview.js` 得出 botzone 的渲染架构：

| 层 | 技术 | 职责 |
|---|---|---|
| **牌面** | [Poker.JS](https://github.com/Tairraos/Poker.JS)（canvas 矢量路径绘制） | `ctx.drawPokerCard(x,y,size,suit,point)` 画牌面；`ctx.drawPokerBack(...)` 画牌背。point 表 = `["2".."9","10","J","Q","K","A"]`（**含 "10"，非 T**）。红桃/方片用 `#a22`，黑桃/梅花用 `#000`。 |
| **动画** | GSAP（`TweenMax` / `TimelineMax`） | 每个状态变化生成一个 `tl.to(animdata, duration, {t:0→1, onUpdate: ()=>drawData(data, animdata.t)})`，逐帧用插值参数 `t` 重绘整个场景。 |
| **场景** | 单个 `<canvas>`（900×600） | `drawData(data, t)` 每帧清屏重绘：椭圆牌桌 → 两座位(头像/名/筹码) → 手牌(翻牌动画用 `scale(2|t-0.5|,1)`) → 公共牌(发牌过渡) → 动作浮字(`fillText` 带阴影+上浮淡出) → 底池/筹码 → 结算覆盖层。 |
| **交互** | DOM 按钮覆盖在 canvas 上 | `#button-container` 绝对定位盖在 canvas 上，人类对战用。 |

**关键动效清单（要复刻的）：**
- **发牌翻面**：手牌/公共牌新出现时，用 `scale(2*|t-0.5|, 1)` 做 X 轴翻转（t<0.5 画牌背，t≥0.5 画牌面）。
- **动作浮字**：`fillText("跟注 Call")`，黄色 `rgba(255,238,88, 1-ty)`，`shadowBlur=10`，上浮 `ytrans=-20-10*ty`（ty=t^4，加速淡出）。
- **筹码/底池数字**：随 `t` 插值变化（`pot`/`round_bet` 平滑过渡）。
- **结算覆盖**：半透明黑底 + 黄字 `😆赢得 X` / `😭输掉 X`，`t` 控制淡入淡出。
- **座位高亮**：轮到谁 `ctx.fillText("👉")` + 行动指示。
- **棋类**（botzone 通用 matchview）：棋子/边落子时缩放/淡入进入。

## 2. 技术选型（已确认）

| 项 | 选择 | 理由 |
|---|---|---|
| 动画库 | **`gsap` npm 包**（3.x，当前版） | 2025-04 起 [GSAP 100% 免费商用](https://www.npmjs.com/package/gsap)；比 botzone 的 1.20.4(2018) 新、维护好、TypeScript 类型齐全。**不照搬 botzone 旧版 js 文件**，走 npm。 |
| 牌面 | **Poker.JS 矢量**（本地 vendor） | botzone 同款；GitHub `Tairraos/Poker.JS` 已 404，用从 botzone 爬到的 `poker.min.js`（10KB）放本地 `bzplat/frontend/src/lib/pokerjs/`，加 license 注释。point 表自带 `"10"`。 |
| 渲染 | **单 `<canvas>` per game** | 复刻 botzone；每游戏一个 canvas 场景绘制器。 |
| 桥接 | **React 持有 events，canvas 订阅 events 差分驱动 GSAP timeline** | React 负责 state/events（SSE/WS/回放），canvas 负责绘制；中间一个 `useCanvasRenderer` hook 把"events 增量"翻译成"GSAP timeline 动画帧"。 |
| 人类对战 | **canvas + DOM 按钮覆盖**（同 botzone） | 一套扑克渲染三场景（观赛/回放/人类）通用。 |

## 3. 架构（canvas↔React 桥接契约）

```
React (events[])  ──→  useCanvasGameRenderer(gameId, events, canvasRef)
                          │
                          ├─ 维护 "已绘制状态" lastScene（上一帧的归一化场景）
                          ├─ diff(events 增量) → 生成 SceneDelta
                          └─ gsap.timeline().to(animdata, dur, {t:0→1, onUpdate: draw(scene, t)})
                                  │
                                  └─ draw(ctx, scene, t):  每帧清屏重绘整个场景
                                       ├─ PokerCanvas.draw(ctx, scene, t)   // 椭圆桌/座位/手牌翻面/公共牌/筹码/动作浮字/结算
                                       ├─ GomokuCanvas.draw(ctx, scene, t)  // 棋盘网格/棋子缩放进入/最后一手标记
                                       └─ PencilCanvas.draw(ctx, scene, t)  // 点阵/边连线绘制/格归属填充
```

**核心契约**：每个游戏的 canvas 渲染器实现统一接口：
```ts
interface GameCanvasRenderer<S> {
  toScene(events: RawEvent[]): S          // events → 归一化场景（纯函数，可复用现有 reducer 的 vm）
  diff(prev: S, next: S): SceneDelta       // 场景差分（哪些是"新"的需动画）
  draw(ctx: CanvasRenderingContext2D, prev: S, next: S, t: number, opts: DrawOpts): void  // 帧绘制
}
```
- `toScene` **复用现有 reducer**（`useMatchState`/`useGomokuState`/`usePencilState` 已有 vm）——不重写归约逻辑，只重写绘制。
- `diff` 驱动动画：手牌/公共牌/棋子/边的新增 → 触发翻面/缩放 timeline。
- `draw` 用插值 `t`（0→1）在 prev↔next 间绘制。

**组件结构**：
- `src/components/GameCanvas.tsx`（新）：通用 `<canvas>` + `useCanvasGameRenderer`，按 `gameId` 经注册表取 renderer。
- `src/games/<game>/canvas.tsx`（新，每游戏一个）：实现 `GameCanvasRenderer`。
- `src/games/<game>/index.ts`：`GameViewSpec` 增 `CanvasRenderer` 字段（与现有 `Board` 并存；`MatchBoard` 改为优先用 canvas，DOM Board 保留作回退/人类对战按钮宿主）。
- `src/lib/pokerjs/`（新 vendor）：Poker.JS 本地副本 + TS 声明。

## 4. 各游戏绘制设计（复刻 botzone）

### 4.1 Holdem（`PokerCanvas`）— 直接照搬 botzone `drawData`
- **画布** 900×600（响应式缩放，max-width）。
- **椭圆牌桌**：两段椭圆拼接（`ellipse(W/2±L, H/2, R, R)`），绿色填充。
- **两座位**（上=座1/BB，下=座0/SB）：头像（Bot/用户头像 URL，无则首字母圆）+ 名字 + "累积赢得 X" + "本轮剩余 Y"。轮到谁画 `👉`。
- **手牌**：`drawCards`，新牌用 `scale(2|t-0.5|,1)` 翻面（t<0.5 牌背，≥0.5 用 Poker.JS `drawPokerCard`）。摊牌/结算时翻面。
- **公共牌**：5 槽，新发的牌同样翻面过渡；用 `prev_public_cards` vs `public_cards` 决定哪些是新的。
- **动作浮字**：`"跟注 Call"` / `"加注 Raise X"` / `"弃牌 Fold"` / `"过牌 Check"` / `"全押 AllIn"`，黄字+黑阴影，`ytrans=-20-10*t^4` 上浮，`alpha=1-t^4` 淡出。
- **下注/底池**：座位旁"本轮已下注 X"，桌中央"底池 X"，数字随 t 插值。
- **结算覆盖**：每手结束半透明黑底 + `😆赢得 X` / `😭输掉 X` / `😐不赚不亏`，t 淡入淡出；最终局 `胜者 X`。
- **座位名**（你之前要的需求）：canvas 内已含名字（复用）。

### 4.2 Gomoku（`GomokuCanvas`）— botzone 通用 matchview 风格
- 木色棋盘 + 网格线（同现有 DOM 视觉，转 canvas 绘制）。
- **棋子落子动画**：新棋子从 `r=0` 缩放到 `r=cell*0.38` + 淡入（`t: 0→1`，ease-out），黑/白填充。
- **最后一手标记**：中心彩色小圆（黑棋橙色/白棋红色），脉冲动画。
- **胜利连线**（五连）：高亮 5 子 + 连线绘制动画。
- 座标/步数信息 canvas 顶部文本。

### 4.3 Pencil（`PencilCanvas`）
- 点阵棋盘：圆点 + 边（未占灰色细线 / 已占按玩家红蓝粗线）。
- **边连线动画**：新占边从端点到端点"画线"过渡（`t: 0→1` 沿线长度），颜色按玩家。
- **格归属填充**：闭合格淡红/淡蓝填充，`t: 0→1` 透明度淡入。
- **得分**：格内画首字母/数字，缩放进入。

## 5. 改动清单

**新增**
- `bzplat/frontend/src/lib/pokerjs/poker.min.js`（vendor，botzone 爬取，加 license 注释）+ `pokerjs.d.ts`（TS 声明：`drawPokerCard/Back`）。
- `bzplat/frontend/package.json`：`+gsap`（npm）。
- `bzplat/frontend/src/components/GameCanvas.tsx`：通用 canvas + `useCanvasGameRenderer` hook + GSAP timeline 驱动。
- `bzplat/frontend/src/games/base.ts`：`GameViewSpec` 增 `CanvasRenderer?: GameCanvasRenderer`。
- `bzplat/frontend/src/games/holdem/canvas.tsx`、`gomoku/canvas.tsx`、`pencil/canvas.tsx`：各游戏 renderer。

**修改**
- `bzplat/frontend/src/components/MatchBoard.tsx`：优先渲染 `<GameCanvas>`（有 CanvasRenderer 时），否则回退现有 DOM `<Board>`。
- `bzplat/frontend/src/pages/HumanPlay.tsx`：holdem 用 `<GameCanvas>` 渲染牌桌，DOM 操作按钮（Fold/Call 等）绝对定位覆盖（同 botzone）。
- `bzplat/frontend/src/components/poker/PlayingCard.tsx`：T→10 已修（保留；canvas 路径用 Poker.JS 不受影响，DOM 仅回退用）。

**删除/废弃**
- 切 canvas 后**直接删除**现有 DOM 棋盘组件（`PokerTable.tsx`/`GomokuBoard.tsx`/`PencilBoard.tsx`）及 `PlayingCard.tsx`（canvas 路径用 Poker.JS，不再需要 DOM 牌）。每游戏 PR 删对应 DOM 组件；`GameViewSpec.Board` 字段替换为 `CanvasRenderer`。**不留回退**（用户确认）。

**文档**
- `wiki/MATCH.md`：观赛视觉说明（canvas 动画）。
- `doc/DESIGN.md` §前端：canvas 渲染层 + 注册表 CanvasRenderer 字段。
- `doc/DEVELOPMENT.md`：新增 `gsap` 依赖。

## 6. 测试与验证

- **无前端测试框架**（无 vitest/jest）→ 用 `npm run build` + 截图验证（`scripts/screenshot_verify.py`）零回归。
- **关键验证点**：
  - 三游戏 canvas 正常渲染（观赛/回放/人类对战三场景）。
  - 发牌翻面/棋子缩放/边连线动画流畅。
  - "10" 正确显示（不再 T）。
  - 人类对战按钮可点（DOM 覆盖层不挡 canvas 交互）。
  - 响应式（移动端 canvas 缩放）。
- **回归**：`pytest`（后端未动，应 345 pass）。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| canvas↔React 状态同步复杂（SSE 高频事件 vs GSAP timeline 排队） | timeline 用 `insertPoint` 串行排队（同 botzone）；events 缓冲，动画跑完再消费下一帧。 |
| Poker.JS 无 TS 类型 / 旧 API | 手写 `.d.ts` 声明 `drawPokerCard/Back`；封装薄 wrapper。 |
| GSAP 与 React StrictMode 双调用 | GSAP timeline 在 useEffect cleanup `tl.kill()`；严格管理生命周期。 |
| 移动端 canvas 尺寸 | `canvas` 用 CSS `width:100%`，内部坐标系固定 900×600，靠 `ctx.scale` 适配 devicePixelRatio。 |
| 视觉与 botzone 不完全一致 | 以 botzone 实际页面截图为对照基准（用户给的 match id），逐步比对。 |
| 工作量大（三游戏 + 桥接） | 分 PR：PR1=基建(GameCanvas+gsap+Poker.JS+holdem canvas)；PR2=gomoku canvas；PR3=pencil canvas。每 PR 独立可验证。 |

## 8. 分阶段交付（建议拆 3 个 PR）

- **PR-A**：基建 + Holdem canvas（含"牌 T"修复、gsap 引入、Poker.JS vendor、GameCanvas 框架、PokerCanvas 复刻 botzone drawData）。最高价值，单独可用。
- **PR-B**：Gomoku canvas（棋子缩放/连线）。
- **PR-C**：Pencil canvas（边连线/格填充）。
- 每 PR：build + 截图验证 + 同步文档。

## 9. 非目标（本次不做）

- 实时/回放统一页面（主任务，待视觉完成后继续，已有独立设计文档）。
- 自研物理引擎/3D（YAGNI，canvas 2D 够用）。
- 音效（botzone 无，YAGNI）。
