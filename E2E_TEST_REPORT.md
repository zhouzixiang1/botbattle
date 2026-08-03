# 三视角端到端 GUI 测试报告

> **测试对象**：botbattle 多游戏 Bot 竞赛平台
> **测试日期**：2026-08-03
> **测试方式**：IAB 浏览器 GUI（访客 + 登录态三角色）+ API 黑盒 + 静态代码审查
> **测试环境**：worktree 隔离运行时栈（后端 50381 + 前端 5173，独立 db，不污染线上 50380）
> **测试账号**：tester1(user) / e2e_organizer(organizer) / e2e_admin(admin)
> **截图**：38 张，存于 `/tmp/gui_e2e_shots/`，命名 `<视角>_<页面>.png`

---

## 测试覆盖总结

| 视角 | 登录 | 核心功能验证 | 排版截图 | 边界守卫 |
|---|---|---|---|---|
| 访客 | — | 公开页可访问 | 9 张 | ✅ 需登录页有软提示 |
| 玩家 tester1 | ✅ | 挑战/我的Bot/回放/设置/下载 | 9 张 | ✅ /admin 被挡 |
| 组织者 e2e_organizer | ✅ | 建赛事成功/赛事详情运营 | 5 张 | ✅ /admin Tab 被挡、能建赛事 |
| 管理员 e2e_admin | ✅ | 10 Tab 全部可访问 | 11 张 | ✅ 全权限 |

**登录机制验证**：`BZ_SKIP_CAPTCHA=1` 环境变量开关 + CUA 坐标点击登录按钮，三角色全部成功登录（见 PR）。

---

## 问题清单（按严重度排序）

### 🔴 主要（建议尽快修）

#### P1-1 【功能 bug】Search 页切换游戏筛选不刷新结果
- **文件**：`bzplat/frontend/src/pages/Search.tsx:69-84`
- **现象**：搜索 effect 的 `useEffect` 依赖数组是 `[q, type]`，**漏了 `gameId`**。用户在「Bot/对局」Tab 切换游戏筛选时，URL 的 `game_id` 更新了，但搜索结果**不刷新**，必须再点一次搜索。
- **复现**：`#/search?q=test&type=bots` → 切换游戏下拉 → 结果不变。
- **修复**：把 `gameId` 加入依赖：`}, [q, type, gameId])`。
- **严重度**：主要（真实功能缺陷，用户可感知）。

#### P1-2 【一致性】时间格式全站不统一，且暴露 UTC 时区标记
- **文件**：`Home.tsx:160`、`History.tsx:126`、`Contests.tsx:259`、`admin/UsersTab`（注册时间）等直接显示原始 ISO `2026-08-03T23:44:37`；而 `BotDetail.tsx:323`、`Search.tsx:236`、`Comments.tsx:142` 做了 `slice(0,16).replace('T',' ')` 规整。
- **现象（GUI 实测确认）**：admin 用户 Tab 注册时间显示 `2026-08-03T23:44:37`（带 T，未转本地时区）；首页/历史页对局时间同样暴露原始 ISO。
- **影响**：同一字段两种格式；`T`/`Z` 对普通用户不友好；未转本地时区。
- **修复**：封装统一的 `fmtTime(iso)` 工具函数全站调用（建议 `new Date(iso).toLocaleString()` 或统一去 T/Z）。

#### P1-3 【排版】MatchViewer 手导航器对长对局渲染过多按钮，移动端溢出
- **文件**：`bzplat/frontend/src/pages/MatchViewer.tsx:330-337`
- **现象（GUI 实测确认）**：70 手对局渲染了 70 个 `size-7` 按钮；500 手对局会生成数十行按钮，移动端占据数屏高度。
- **修复**：手数 > N 时折叠为「输入手号跳转」或横向 ScrollArea。

#### P1-4 【UX】首页进度列硬编码兜底 70，误导非 70 手对局
- **文件**：`bzplat/frontend/src/pages/Home.tsx:198`
- **现象**：`${m.hands_played ?? 0}/${m.total_hands ?? 70}`——`total_hands` 缺失时默认 70。但赛事可配 50 手，后端若返回 null 会显示成 `12/70`，误导用户。
- **GUI 关联发现**：测试中发起 `hands:10` 的挑战，实际跑 70 手（后端默认覆盖？需核实 match_config 是否生效）。
- **修复**：`total_hands` 缺失时显示 `12 手` 而非 `12/70`。

### 🟡 次要

#### P2-1 【UX】首页导航标签与页面大标题不一致
- **文件**：`nav-config.ts:26`（label「首页」）vs `Home.tsx:59`（title「最新对局」）
- **现象（GUI 实测确认）**：点「首页」看到大标题「最新对局」，认知错位。
- **修复**：Home.tsx title 改「首页」，或导航改「广场」。

#### P2-2 【疑似 bug】admin 运行时 Tab 出现 "Not Found" 文本
- **现象（GUI 实测发现）**：admin → 运行时 Tab，「最大并发对局」字段附近显示 `Not Found`。
- **可能原因**：某个运行时配置接口 404，或 CPU 核心数读取失败。
- **待核实**：需查 RuntimeTab.tsx 哪个字段触发了 Not Found（可能是 `cpu` 核心数读取接口）。
- **严重度**：次要（功能未完全失效，但显示异常）。

#### P2-3 【排版】MatchViewer 顶栏信息条窄屏挤压
- **文件**：`MatchViewer.tsx:244-277`
- **现象（GUI 实测确认）**：matchId/游戏/类型/状态/手数/胜者/跳最新按钮堆在一行，窄屏换行 4-5 行；`手数：70` 与 `/70` 分行。
- **修复**：拆成「状态行」+「对阵行」；`total_hands` 缺失时不显示 `/`。

#### P2-4 【UX】DataDownload 对未登录用户文案「需 Lv.1」有歧义
- **文件**：`DataDownload.tsx`
- **现象**：访客（未登录）看到下载列「需 Lv.1」锁，但访客根本没登录，应明确提示「登录后下载」。
- **修复**：未登录时显示「登录后下载」+ 跳 `/login` 链接。

#### P2-5 【UX】BotDetail 对 0 场对局的新 Bot 显示误导性段位
- **文件**：`BotDetail.tsx`
- **现象**：新上传 Bot（0 场、rating=1500）会基于 1500 显示「进阶/熟练」段位，但该 Bot 从未对战。
- **修复**：0 场对局 Bot 标「未定级」或不显示段位。

#### P2-6 【UX】MatchViewer 返回按钮固定指首页，丢失浏览上下文
- **文件**：`MatchViewer.tsx:394`（`<Link to="/">返回</Link>`）
- **现象**：从排行榜/Bot详情/历史页进入对局详情，「返回」一律回首页，丢失来源页。
- **修复**：用 `useNavigate(-1)` 或路由 state 记录来源。

### 🟢 UX 细节

#### P3-1 首页「热门对局」加载失败静默吞错 + 无骨架导致布局跳动
- `Home.tsx:242`（`.catch(()=>{})`）+ `:244`（空数组返回 null）→ 加载中区域突然出现造成跳动。建议加 skeleton。

#### P3-2 筛选器标签文案不统一
- Home/Leaderboard/Contests 写「游戏」，DataDownload 写「筛选游戏」，History 多一个「状态」。建议统一。

#### P3-3 UserProfile 经验进度条百分比用 `xp % 100` 猜算，逻辑可疑
- `UserProfile.tsx:177`，建议用后端返回的「当前段 xp / 升级所需」直接算。

#### P3-4 StatusBadge 对未知状态显示原始英文 key
- `status.tsx:106-111`，兜底应显示「未知状态」。

#### P3-5 Wiki 移动端侧栏导航占纵向空间多
- `Wiki.tsx:95`，建议移动端用 Select/Sheet 抽屉。

---

## 测试中验证「正常」的功能（无问题）

- ✅ 三角色登录（后端 + GUI 均正常）
- ✅ Bot 上传 / 列表 / CRUD
- ✅ 发起挑战 → 对局完成 → 回放
- ✅ 收藏 Bot、修改资料
- ✅ 组织者创建赛事（POST /api/contests 成功）
- ✅ 赛事详情页排版（阶段 Tabs、操作按钮、配置展示）
- ✅ 管理员 10 Tab 全部可访问
- ✅ 角色边界守卫（玩家/组织者进 /admin 被正确挡住，文案清晰）
- ✅ 对局完成通知（NotificationBell 显示未读数）
- ✅ Glicko-2 排行榜（段位/Rating/战绩展示）

---

## 附：IAB 自动化测试障碍（非平台 bug，记录备查）

- **现象**：Playwright `click()` 登录按钮会冻结 IAB webview 渲染通道。
- **根因**：登录按钮被 Radix `Tooltip` 的 `TooltipTrigger asChild` 包裹（`CaptchaField.tsx:54-69`），Playwright actionability 探测与 Tooltip 事件监听冲突。
- **影响**：仅 IAB 自动化场景；**手动浏览器登录完全正常**（用户确认）。
- **绕过**：用 `tab.cua.click({x,y})` 坐标点击（不走 actionability）+ `BZ_SKIP_CAPTCHA=1` 跳验证码。
