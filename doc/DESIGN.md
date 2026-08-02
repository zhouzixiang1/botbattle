# 设计文档

> 本文档描述 botbattle 平台的系统架构、模块设计、数据库设计、接口设计与安全设计。

## 1. 系统架构

### 1.1 总体架构

平台采用**前后端分离 + 单进程**架构：后端 FastAPI 提供 REST/SSE/WebSocket 接口并托管前端构建产物，前端 React SPA 通过 HTTP 交互。

```mermaid
graph TB
    subgraph 客户端
        FE[React SPA<br/>shadcn/ui + 暗色]
    end
    subgraph 后端 FastAPI 单进程
        API[REST API<br/>109 路由]
        SSE[SSE 观赛]
        WS[WebSocket 人类对战]
        MW[中间件<br/>限流+安全头]
    end
    subgraph 核心层
        ORCH[编排层<br/>orchestrator]
        ENGINE[裁判引擎<br/>holdem/gomoku/pencil]
        CONTEST[赛制层<br/>templates/stages/manager]
        STORE[数据层<br/>Store + SQLite]
        NOTIFY[通知层]
        AUTO[闲时自动对局<br/>auto_matcher]
    end
    subgraph 沙箱
        DOCKER[Docker<br/>ELF + PE/Wine]
    end
    FE -->|HTTP/SSE/WS| MW
    MW --> API & SSE & WS
    API --> ORCH & CONTEST & STORE & NOTIFY
    ORCH --> ENGINE
    ORCH --> DOCKER
    ORCH --> STORE
    CONTEST --> ORCH
    AUTO --> ORCH
    ENGINE -.->|MatchResult| ORCH
```

### 1.2 运行模型
- **单进程 uvicorn factory**（`main:create_app`），默认 `127.0.0.1:50380`。
- **lifespan** 启动后台 asyncio 闲时自动对局任务（AutoMatchScheduler）。
- **并发控制**：`asyncio.Semaphore(max_concurrent)` 限制 bot 对局槽；人类对战独立 `_human_sem`（默认 4）。
- **限流**：内存滑动窗口 IP 限流（单进程；多 worker 部署需换 Redis）。

## 2. 模块设计（12 层）

### 2.1 模块树与职责

| 层 | 模块 | 职责 |
|----|------|------|
| 接口 | `api_routes.py` | 主 REST（95 路由）：bots/matches/users/search/leaderboard/comments/likes/notifications/contests/admin/wiki/matchpacks |
| 接口 | `auth/routes.py` | 认证 REST（13 路由，prefix `/api/auth`）：注册/登录/验证/重置/profile/avatar |
| 接口 | `main.py` | 应用工厂 + 中间件装配 + StaticFiles 挂载（dist/wiki-assets/avatars）+ lifespan |
| 游戏注册 | `games/` | **全面解耦的单一真相**：base.py（GameSpec 接口 + GameRegistry 单例）+ 每游戏完全自包含子包 games/<game>/（engine.py 裁判 + protocol.py 行协议 + result.py 独立结果 + tiers.py per-game 段位 + cards.py[holdem] + spec.py 装配）。GameSpec 集中声明全部固有属性，通用层经 `registry.get(game_id)` 取 spec 调用其能力，**禁止 if game_id 分支** |
| 兼容转发 | `_compat/` | 向后兼容转发层：把旧 import 路径（`engine.<x>`/`protocol.<x>`）转发到 games/<game>/。engine/ 与 protocol/ 旧文件改为 re-export 自 _compat（一行 shim） |
| 裁判(shim) | `engine/` | 保留仅为兼容旧 import：__init__/game/gomoku/pencil/result/tiers/cards 改为 shim；registry.py 委托 games 注册表 |
| 协议(shim) | `protocol/` | 保留仅为兼容旧 import：json_protocol/board_protocol → re-export 自 _compat |
| 编排 | `matches/` | orchestrator（入队/SSE/评分/人类对战）+ runner（起 Bot 进程，经 games 注册表路由协议）+ auto_matcher（闲时自动） |
| 赛制 | `contests/` | templates（7 内置模板）+ stages（对阵生成）+ manager（阶段状态机）+ validation |
| 沙箱 | `runtime/` | BinaryRunner（docker/wine/local 三模式）+ limits（资源硬顶） |
| 数据 | `store/` | Store 类（SQLite，100+ 方法，含 _migrate 自愈）+ schema.py（常量唯一来源） |
| 认证 | `auth/` | routes + auth_manager + captcha + dependencies（require_user/admin/organizer） |
| 通知 | `notifications/` | NotificationManager（站内通知 + 按 prefs 复用 Mailer 发邮件） |
| 支撑 | `bots/ rating/ mail/ security.py logging_config.py crypto.py cli.py` | Bot 上传分类 / Glicko-2 / SMTP / 安全头+限流 / 日志 / 密码 hash / CLI |

### 2.2 核心解耦契约

```mermaid
graph LR
    G[GameSpec games/] -->|session_factory| E[裁判引擎]
    G -->|protocol| P[行协议]
    E -->|产出| R[RoundResult/MatchResult]
    R -->|winners + deltas| O[编排层 matches]
    R -->|winners + deltas| C[赛制层 contests]
    O -->|只读 winners/deltas| S[评分/通知/XP]
    C -->|只读 winners/deltas| T[积分榜/晋级]
    style G fill:#e3f2fd,stroke:#1565c0
    style R fill:#e8f5e9,stroke:#2e7d32
```

**两层解耦**：

1. **GameSpec 注册表（`games/`，全面解耦的单一真相）**：每款游戏是一个 `GameSpec` 对象，集中声明 `game_id`/`label`/`session_factory`(裁判)/`protocol`(行协议)/`default_match_params`+`validate_match_params`(配置)/`rounds_per_match`+`normalize_earnings`+`eta_for_match`(编排特化)/`tiers`+`tier_for`(per-game 段位)/`judge_params`(裁判参数)/`templates`(赛事模板)/`code_path`+`summary`(元信息)。通用层（编排/赛制/评分/DB）经 `registry.get(game_id)` 取 spec 调用其能力，**禁止 `if game_id == ...` 分支**——所有游戏差异封装在各自 spec。

2. **结果鸭子契约（`RoundResult`/`MatchResult`，独立定义不共享基类）**：裁判产出 `winners`(座位号列表，空=平局) + `deltas`(长 2 零和数组)；`MatchResult` 含 `rounds_played` + `rounds` + `events` + `winner`。**编排层与赛制层只依赖这两个字段，绝不触碰扑克的 pot/board/holes 或棋类的棋盘**——这是赛制代码能通用于三款游戏的根本。`tests/test_result_contract.py` 断言三游戏 result 都满足此契约（防 drift）。

**DRY 边界**：游戏规则（engine/result/tiers 数据/templates）各游戏独立；平台工具（`_board_protocol.py` 行协议序列化、`base.tier_for_in` 段位查表算法）共享——避免字节级重复的维护隐患。

### 2.3 新增一款游戏的成本

通用层**零改动**，仅需：
1. 建 `games/<game>/` 子包：`engine.py`(裁判) + `protocol.py`(行协议，棋类可 re-export `_board_protocol`) + `result.py`(独立结果，满足鸭子契约) + `tiers.py`(段位曲线，调 `base.tier_for_in`) + `templates.py`(赛事模板) + `spec.py`(装配 GameSpec)。
2. `schema.py`：`matches_<game>` 表（FK 用 `ON DELETE SET NULL`）+ 索引；`REGISTERED_ENGINES`/`VALID_GAME_IDS` 各加该项。
3. `games/__init__.py`：`registry.register(SPEC)` 一行（启动断言 schema 与注册表一致）。
4. 前端 `src/games/<game>/index.ts`（GameViewSpec）+ `src/games/index.ts` 注册。
5. **约束**：`games/<game>/` 不得反向 import `engine`/`_compat`（循环依赖，`test_import_cycles.py` 守护）；通用层不得 import 具体游戏模块。

**不再需要**在 `registry.run_session`/`runner._dumps`/`_loads`/`_fail_response`/orchestrator 加 `if game_id==` 分支——这是全面解耦前 6 处分散注册点的彻底消除。

## 3. 数据库设计

SQLite 单文件（默认 `botzone.db`），**27 张表**，约 38 个索引。所有常量（状态码/类型/段位阈值/配置键名）集中在 `store/schema.py`。

### 3.1 核心表（选录）

| 表 | 用途 | 关键列 |
|----|------|--------|
| `users` | 用户 | id/username/email/password_hash/role/display_name/bio/avatar/xp/level/last_active_at |
| `bots` | Bot | owner_id/name/display_name/game_id/os/arch/format/binary_path/current_version/is_public/is_active |
| `matches_holdem` / `matches_gomoku` / `matches_pencil` | 对局（**每游戏一张表**，全面解耦 PR3） | id/bot_a_id/bot_b_id/match_type/status/game_id/winner/n_dots/human_user_id/likes_count/views_count；三表结构一致，游戏专属列在其他游戏中默认 NULL/0 |
| `matches_index` | 对局定位 | id(PK)/game_id——get_match(id) 先查此表定位到哪张 matches_<game> |
| `ratings` | 评分（**per-game**，PK=bot_id+game_id） | bot_id/game_id/rating(1500)/rd(350)/vol/wins/losses/draws/last_played_at |
| `contests` | 赛事 | title/organizer_id/status(draft/open/running/rest/finished/cancelled)/game_id/stages_json/current_stage_idx |
| `contest_pairings` | 对阵 | contest_id/round_num/bot_a_id/bot_b_id/match_id(逻辑外键，无 DB FK)/stage_idx/bracket_slot |
| `contest_stage_results` | 积分 | contest_id/stage_idx/bot_id/points/wins/draws/losses/rank_in_group |

### 3.2 社交/互动表

| 表 | 用途 |
|----|------|
| `rating_history` | 评分变化时序（**per-game**，bot_id+game_id；段位趋势曲线，每 bot×game 截断保留） |
| `follows` | 关注关系（follower_id, followee_id） |
| `favorites` | 收藏 Bot（user_id, bot_id） |
| `comments` | 评论（target_type=match/bot, target_id, user_id, body） |
| `likes` | 点赞（user_id, target_type, target_id） |
| `notifications` | 通知（user_id, type, title, body, link, is_read） |
| `notification_prefs` | 通知邮件偏好（email_match_done/email_followed/email_contest/email_comment） |

### 3.3 支撑表（选录）

| 表 | 用途 |
|----|------|
| `bot_versions` | Bot 版本管理（多版本 + 切换激活） |
| `match_replays` | 对局回放事件存储（events_json） |
| `sessions` | 会话（token, user_id, expires_at，认证核心） |
| `platform_settings` | 所有热配置 KV（运行时/站点/裁判/auto-match） |
| `contest_entries` | 赛事报名（user_id, bot_id, group_id, seed） |
| `pair_stats` | 对手战绩统计（a_wins/a_losses/draws） |

### 3.4 迁移机制
`Store._migrate()` 在每次建连时自愈：为旧库补新增列（game_id/xp/level/bio/avatar/likes_count 等），必要时重建表放宽 CHECK 约束（纳入 rest/ladder/human 等新状态）。**向后兼容，不破坏现有数据**（除对局数据——见下）。

**全面解耦 PR3 的迁移**（matches 拆 per-game 表 + ratings 加 game_id 维度）：
- **对局数据不保留**（用户决策）：检测旧单表 `matches` → 先清 `contest_pairings.match_id`（置 NULL），再 DROP `matches`+`match_replays`；新三表（`matches_holdem/gomoku/pencil`）+ `matches_index` 由 SCHEMA `IF NOT EXISTS` 建。对局可后续跑种子脚本（`scripts/seed_test_accounts.py`）重建。
- **用户/Bot/赛事/评论/评分保留**：`ratings`/`rating_history` 加 `game_id` 列、PK 改 `(bot_id, game_id)`、按 `bots.game_id` 回填（CREATE new→INSERT SELECT JOIN bots→DROP→RENAME）。

## 4. 接口设计

**共 109 个路由装饰器**：api_routes.py 95（含 1 WebSocket + 1 SSE）+ auth/routes.py 13 + main.py 1 健康端点。按权限分四类：

### 4.1 公开端点（无需登录，访客可用）
- 健康：`GET /api/health`
- Bot 浏览：`GET /api/bots/public`、`/api/bots/{id}`、`/profile`、`/matches`、`/opponents`、`/rating-history`
- 用户浏览：`GET /api/users`、`/api/users/{name}/profile`、`/bots`、`/followers`、`/following`
- 对局浏览：`GET /api/matches`、`/matches/liked-top`、`/matches/{id}`
- 排行与元数据：`GET /api/leaderboard`、`/api/tiers`、`/api/levels/info`、`/api/site/info`
- 搜索：`GET /api/search`
- 赛事浏览：`GET /api/contests`、`/api/contests/{id}`、`/bracket`、`/templates`
- Wiki：`GET /api/wiki`
- 数据集列表：`GET /api/matchpacks`

### 4.2 鉴权端点（require_user，登录玩家）
- Bot 管理：`POST /api/bots`（上传）、`/versions`、`/active`、`PATCH/DELETE /api/bots/{id}`
- 对局：`POST /api/matches/challenge`、`/api/matches/human`
- 社交：`POST/DELETE /api/users/{id}/follow`、`/api/bots/{id}/favorite`
- 互动：`POST/DELETE /api/comments`、`/api/likes`、`POST /api/matches/{id}/view`
- 通知：`GET /api/notifications`、`POST /read`、`/read-all`、`GET/PUT /api/notification-prefs`
- 赛事：`POST /api/contests/{id}/register`
- 数据下载：`GET /api/matchpacks/download`（level ≥1 gating）
- 认证：`GET /api/auth/me`、`POST /logout`、`/change-password`、`PUT /profile`、`POST /avatar`

### 4.3 组织者端点（require_organizer 或 admin）
- `POST /api/contests`（创建赛事）
- `POST /api/contests/{id}/{open,start,resume,advance}`（赛事推进，require_organizer）
- 注：`register`/`dispatch` 为 require_user（报名/换 Bot 由登录用户发起）

### 4.4 管理员端点（require_admin）
- 用户管理：`GET /api/admin/users`、`POST /role`、`PATCH/DELETE /api/admin/users/{id}`、`/sessions`
- Bot/对局/赛事管理：`GET /api/admin/{bots,matches,contests}`、`PATCH/DELETE`、`GET /api/admin/contests/{id}/entries`
- 配置：`GET /api/admin/settings/runtime`、`PATCH /api/admin/settings/{runtime,site}`
- 裁判：`GET /api/admin/judges`、`PATCH /api/admin/judges/params`
- 模板：`GET/POST /api/admin/templates`、`PUT/DELETE /{tid}`、`POST /preview`
- 邮件：`GET /api/admin/email/{templates,outbox}`、`PUT /templates/{key}`
- 日志：`GET /api/admin/logs`
- 认证辅助：`POST /api/auth/admin/create-reset-token`（生成密码重置 token）

### 4.5 实时端点
- **SSE** `GET /api/matches/{id}/events`：观赛事件流（先推 snapshot 再增量）。
- **WebSocket** `WS /api/matches/{id}/play`：人类对战落子回传（接收 `{move}`）。

## 5. 前端架构

### 5.1 技术栈与设计系统
- React 19 + Vite 8 + Tailwind CSS v4（CSS-first）+ shadcn/ui（new-york）+ Radix UI + lucide-react（图标，无 emoji）+ recharts（图表）+ next-themes（暗色）。
- **设计 token**：shadcn v4 OKLCH 双主题（`:root` 浅 / `.dark` 暗），emerald 品牌色系，`@theme inline` 桥接到 Tailwind utility。**刻意无紫色无米色**（规避 AI 默认审美）。
- **暗色模式**：next-themes class 策略，浅色默认 + 跟随系统，侧栏底部一键切换。
- **响应式**：sm/md/lg/xl 断点；**lg(1024)+ 桌面侧边栏，<lg 移动端顶栏 + Sheet 汉堡抽屉**；表格窄屏隐藏次要列。
- **代码分割**：React.lazy + Suspense，22 个顶层路由各自独立 chunk；主包 gzip ~115KB，recharts 隔离到 BotDetail chunk。
- **路径别名 `@/` → src/**，禁相对路径。

### 5.2 组件库与页面
- **26 个 shadcn 共享原语**（`src/components/ui/`）：Button/Input/Card/Table/Tabs/Badge/Dialog/Command/Chart/Sheet/Slider 等，是全项目唯一组件抽象层。
- **项目封装**：status.tsx（EmptyState/Loading/ErrorMsg/StatusBadge）、metric-card.tsx、tier-badge.tsx、BrandMark.tsx（平台品牌标识）、AuthShell.tsx（登录/注册/重置/验证的居中壳：品牌头部 + 居中 Card，解决空旷）、use-playback.ts（定速回放/直播缓冲 hook：buffer/stepIdx/playing/speed/定时步进/live-follow，buffer 有 MAX_BUFFER 上限防 OOM）、playback-controls.tsx（播放/暂停/步进/速度档/进度条控件）。
- **全局 Shell**：app-shell.tsx 按登录态分两套 chrome：
  - **已登录**：**lg+ 桌面左侧边栏**（Logo + compact 搜索 + 垂直导航 + 底部用户区/主题/通知）；**<lg 移动端顶栏 + Sheet 抽屉**。
  - **访客（未登录）**：**全断点顶栏**（BrandMark + 公开导航 + 主题切换 + **登录/注册**；窄屏用 Sheet 抽屉放导航与 CTA）。侧栏仅登录后出现，避免访客桌面无入口。
  - **auth 页**（登录/注册/重置/验证）：不显示侧栏，内容占满居中；顶栏保留精简条（品牌 + 主题 + 登录/注册）。
  - nav-config.ts（9 导航项）。GlobalSearch 支持 `compact` 变体适配窄侧栏（铺满宽、截断、无快捷键徽章）。首页 Hero 对访客额外展示注册/登录 CTA。
- **页面壳统一**：PageStub.tsx 作为内容页标题区壳——紧凑标题 + `subtitle`（一行说明）+ `actions`（右侧操作槽：筛选/按钮）；垂直 padding 由全局 `<main>` 统一提供（PageStub 只设水平 padding，避免双倍留白）；auth 页改用 AuthShell（不套 PageStub）。表格统一视觉：表头 `bg-muted/40` + 小写弱化字色，行 hover 高亮。
- **观赛/对战页左右分栏**：MatchDetail / ArenaWatch / HumanPlay `xl:grid-cols-[minmax(0,1fr)_22rem]`（左展示 / 右日志），`lg`(1024-1279) 因侧栏占位自动堆叠（避免侧栏+分栏三列挤压），`xl`(1280)+ 横排；ArenaWatch 走 usePlayback 定速缓冲层（事件入 buffer、可控节奏播放，而非实时直播）。MatchBoard（棋盘/牌桌渲染）对 reduceEvents 结果做 useMemo 缓存，避免定速播放每帧全量归约。
- **页面**：21 个独立页面 + admin/ 10 Tab，覆盖首页/排行榜/Bot 详情/用户主页/搜索/通知/设置/赛事/对局回放/人类对战/数据下载/账号 等。
- **三棋盘可视化**：poker（PokerTable 深绿毡面）/ gomoku（GomokuBoard 木色）/ pencil（PencilBoard），统一经 MatchBoard 分发。

## 6. 安全设计

| 威胁 | 防护措施 |
|------|---------|
| **恶意 Bot** | Docker 硬隔离：`--network=none --memory=512m --cpus=1 --read-only --tmpfs /tmp --cap-drop=ALL --security-opt no-new-privileges --user 65534:65534`；资源硬顶（admin 不可抬高） |
| **接口滥用** | 分级 IP 限流（auth 20/60s、challenge 8/60s、upload 6/60s、captcha 60/60s、其他 120/60s），`BZ_RATE_LIMIT` 可关；按真实公网 IP 分桶（`BZ_TRUST_PROXY=1` 解析 XFF） |
| **暴力破解** | 图形验证码（注册/登录）；登录失败不区分用户名/密码错误 |
| **密码泄露** | 密码 hash 存储（非明文）；重置链接防枚举 |
| **XSS / 点击劫持** | 安全头：X-Content-Type-Options / X-Frame-Options:DENY / Referrer-Policy / Permissions-Policy（可选 HSTS） |
| **会话劫持** | session token，cookie `bz_session`，改密码清会话 |
| **公网暴露** | nginx HTTPS + frp 反代；`BZ_TRUST_PROXY=1` 信任 XFF 取真实 IP（否则限流失效、登录 IP 错误） |

### 6.1 日志与审计（公网加固）

三套独立日志文件（详见 [wiki/SECURITY.md](../wiki/SECURITY.md)）：
- **`logs/app.log`**：业务/系统日志。
- **`logs/access.log`**：HTTP 访问日志（`AccessLogMiddleware`，含真实 IP + 方法 + 路径 + 状态 + 耗时）。
- **`logs/audit.log`**：安全审计日志（`audit_log()` 辅助，敏感操作含 actor+IP+action+result；`result=fail` 升 WARNING）。

埋点：登录成功/失败、注册、验证邮箱、改密、重置密码、登出、Bot 上传/版本、对局创建、人类对战、赛事创建、admin 删用户/bot/赛事、改角色、建重置令牌。管理员可在前端 admin「日志」Tab 切换三文件查看（`/api/admin/logs?file={app|access|audit}`，文件参数白名单防路径穿越）。验证码日志脱敏（SMTP 未配置时不打明文）。

> 返回 [doc/INDEX.md](./INDEX.md)
