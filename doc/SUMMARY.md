# 项目总结报告

> 本文档总结 botbattle 平台的开发成果、里程碑、指标达成、验收交付与遗留事项。

## 1. 项目概况

| 项 | 内容 |
|----|------|
| 项目名 | botbattle（多游戏 Bot 线上对战平台） |
| 定位 | 用户上传二进制 Bot，平台沙箱运行对局，提供观赛/回放/Glicko-2 排行榜/赛事/人类对战/社交 |
| 游戏 | 德州扑克 / 五子棋 / 点格棋（有意的产品边界） |
| 技术栈 | Python 3.12 + FastAPI + SQLite；React 19 + Vite 8 + Tailwind v4 + shadcn/ui |

## 2. 里程碑与开发历程

项目经多个阶段交付。早期 **#1–#27** 奠定功能与文档基线；其后继续合并游戏契约收敛、canvas 观赛、安全日志、赛事修复等（见 §2.4）：

### 2.1 平台功能建设（15 PR：#1–#15）
围绕线上 Bot 对战补齐平台功能：
| PR | 主题 |
|----|------|
| #6 | Bot 详情页（档案/历史/对手战绩/评分曲线） |
| #7 | 用户主页完善 + 全局搜索 |
| #8 | 站内通知 + 邮件提醒 |
| #9 | 关注用户 + 收藏 Bot |
| #10 | 段位称号 + 排名变化趋势 |
| #11 | 赛事对阵图 + 显示 Bot 名 |
| #12 | 对局评论 + 点赞 + 浏览 + 点赞榜 |
| #13 | 个人设置中心 + MyBots 管理增强 |
| #14 | 用户经验 + 等级系统（gating） |
| #15 | 站点配置（同期曾交付的对局数据导出能力已在后续版本下线） |
| 其他 | 大规模压测脚本、pencil 引擎 bug 修复、测试种子 |

### 2.2 前端设计系统重塑（7 PR：#16-#22）
| PR | 主题 |
|----|------|
| #16 (F1) | 地基：shadcn/ui + OKLCH 双主题 token + 暗色骨架 + lucide + `@/`别名 |
| #17 (F2) | 共享 UI 原语库（26 shadcn 组件） |
| #18 (F3) | 全局 Shell + 导航 + 移动端抽屉 + Cmd+K 搜索 |
| #19 (F4) | 核心页（首页/排行榜/Bot详情/用户主页/搜索）+ tiers 清理紫色 |
| #20 (F5) | 对局回放 + 赛事 + 实时观赛（emoji→lucide） |
| #21 (F6) | 账号/管理页 + emoji 全清零 |
| #22 (F7) | 代码分割(主包-62%) + 暗色全覆盖 + 响应式 + a11y |

### 2.3 文档体系 + 体验修复 + 对抗审计（PR #23–#27）
| PR | 主题 |
|----|------|
| #23 | 甲方交付文档体系（doc/ 6 份）+ AGENTS 文档规范 + README 重写 |
| #24 | 人类对战治本（Docker /tmp:exec + BotCrashedError 快速 abort） |
| #25 | 观赛定速缓冲（usePlayback）+ 左右分栏 + SSE 队列扩容 |
| #26 | 前端逐页视觉修复（紧凑标题区 + 表格统一 + auth 品牌壳） |
| #27 | 对抗审计：引擎层传播 BotCrashedError + start_session 泄漏 + 测试盲区 |

### 2.4 后续演进（#27 之后，节选）
| 主题 | 说明 |
|------|------|
| 契约解耦 | `games/` GameSpec 注册表、per-game matches 表；赛制/编排主流程不按游戏名分支，具体适配仍在各游戏包与前端视图 |
| canvas 观赛 | 三游戏 canvas + GSAP；统一 MatchViewer |
| 安全与日志 | access/audit 三文件日志、限流与审计埋点 |
| 赛事修复 | 瑞士/淘汰多轮推进等（赛事压测发现） |
| QA 与恢复加固 | worktree 写隔离、对局/赛事重启对账、评分恰好一次补算、两阶段派发补偿、admin 安全中止与删除、Playwright 真浏览器回归 |
| Pencil 累计棋钟 | GameSpec 固定每方 900 秒，Bot-vs-Bot 与人类双方统一累计；回放/SSE 记录 `time_used`/`time_out`，对局页显示剩余时间与超时状态 |
| 文档对齐 | 交付文档与 wiki 与代码现状一致（本轮） |

## 3. 成果指标

> 下列为文档对齐时点（2026-08）的代码盘点；易变的路由、页面与测试数量只以最终目标
> 提交上的自动化收集和执行结果为准，命令与证据见 `TESTING.md`。

| 指标 | 数值 |
|------|------|
| 后端代码 | `bzplat/backend` 当前 66 个非测试 `.py`（含 `games/`，以 `rg --files` 为准） |
| API 路由 | REST + SSE + WebSocket；精确数量以目标提交的自动化盘点为准 |
| 数据库表 | **30** 张 + **36** 个具名索引（全新初始化结果；per-game 表/索引由 `_migrate` 模板补齐） |
| 游戏架构 | `games/` 注册表 + 3 自包含子包（shim 已删，真实现全在 games/） |
| 前端组件 | 26 个 shadcn 共享原语 |
| 前端页面 | 顶层业务页面均 lazy 分包；精确数量以目标提交的路由盘点为准 |
| 自动化测试 | 后端 pytest + Playwright；最终数量以目标提交重新收集为准 |
| 大规模压测 | 60 用户 × 8 阶段；历史结果仅作参考，本轮发布前须按固定 70/15/N=6 规则重跑 |
| 浏览器验收 | Playwright **4 个 spec / 21 条 collected**；当前目标提交 Chromium 全量 `21 passed`（2.3m）；另有 browser/screenshot 辅助脚本 |
| 合并 PR | 早期 27 个里程碑后继续演进（游戏契约收敛、canvas 重写、安全日志、赛事修复等） |

## 4. 验收交付清单

| 交付物 | 状态 | 位置 |
|--------|------|------|
| 后端源码 | ✅ | `bzplat/backend/` |
| 前端源码 | ✅ | `bzplat/frontend/` |
| 甲方交付文档（6 份） | ✅ | `doc/` |
| 规则/协议文档 | ✅ | `wiki/` |
| 协议 JSON Schema | ✅ | `contracts/` |
| 三游戏样例 Bot | ✅ C / Python 均与唯一现行协议绑定 | `samples/` |
| 测试套件 | ✅ 契约、单元、集成与浏览器套件齐备；最终通过数以目标提交的 `TESTING.md` 证据为准 | `bzplat/backend/tests/` |
| 隔离 API / 冒烟 | ✅ 当前目标提交 API 50 passed / 0 failed、`e2e_smoke.sh` ALL PASSED | `scripts/api_full_test.py`、`scripts/e2e_smoke.sh` |
| 压测脚本 | ✅ 脚本已交付；本轮未将历史基线冒充最终结果 | `scripts/load_test.py` |
| 浏览器验收 | ✅ 4 spec / 21 条 Chromium 全量通过（2.3m；Console/Network/SSE/WS 与三视口） | `bzplat/frontend/e2e/` |
| 部署配置 | ✅ | `deploy/` + `scripts/platform-ctl.sh` |

## 5. 经验教训

| 经验 | 说明 |
|------|------|
| **核心游戏契约的价值** | 公共结果字段与 `GameSpec` 让赛制/编排主流程对三游戏通用；新增游戏仍需注册后端 spec、元数据与前端视图，但主流程不新增游戏名分支 |
| **大规模压测暴露真实 bug** | pencil 引擎 `n_dots=None` 崩溃是压测暴露的生产 bug（非测试环境复现） |
| **shadcn CLI 字面 @ 目录坑** | `npx shadcn add` 生成到字面 `@/` 目录而非 `src/`，需手动迁移 |
| **挑战限流是压测瓶颈** | 同 IP 共享限流使顺序压测极慢，加 `--no-throttle` + `BZ_RATE_LIMIT=0` 解决 |
| **文档随 PR 写易留痕** | wiki 残留 "PR-N" 标注，应在交付前统一清理 |
| **敏感信息管理** | `.env` 不应提交真实 SMTP 密码；文档不回写敏感值 |

## 6. 遗留问题与维护计划

### 6.1 已知遗留
| 项 | 影响 | 建议 |
|----|------|------|
| auto-match scheduler 时序 | 压测环境后台触发受 idle 窗口影响 | 验收模式未触发时硬失败；仅显式诊断参数可降级为 warning，且不得作为验收证据 |
| 多 worker 限流 | 内存限流单进程有效 | 多 worker 部署需换 Redis 共享限流状态 |
| `.env` 敏感信息 | SMTP 明文密码已提交历史 | 建议轮换凭据 + `git filter-branch` 清理历史 + 确认 `.gitignore` |

### 6.2 维护计划
- **新增游戏**：按 `AGENTS.md` 与 `doc/DESIGN.md` §2.3 / `doc/DEVELOPMENT.md` §5.1 接入 GameSpec/前端视图/注册常量，**禁止**赛制与编排主流程新增游戏名分支。
- **文档同步**：改代码时按 `AGENTS.md` 文档规范同步 `doc/` 或 `wiki/`。
- **测试维护**：新增功能/行为变更须在 `bzplat/backend/tests/` 加用例；架构改动同步契约测试；定期跑 `load_test.py`，核心用户流程优先维护 `frontend/e2e/` Playwright 回归。
- **日志监控**：`logs/app.log` + `access.log` + `audit.log` + admin 日志 Tab；Bot 崩溃看 stderr 末尾。

## 7. 结论

botbattle 已形成完整的代码、文档、测试与部署交付面：三游戏核心由 `games/` GameSpec + 结果鸭子契约守护，Pencil 采用 Bot 与人类一致的每方 900 秒累计棋钟，前端具备 shadcn/ui 双主题、canvas/GSAP 观赛与响应式页面，本轮又补齐 QA 写隔离、恢复一致性和真实浏览器自动化。最终验收结论必须来自整合后的目标提交实际执行结果，统一记录在 `TESTING.md`，不以历史手填数字或静态盘点替代运行证据。

> 返回 [doc/INDEX.md](./INDEX.md)
