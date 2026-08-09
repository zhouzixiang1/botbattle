# 清理与保留清单

> 只读快照：2026-08-09 22:27（Asia/Shanghai），主目录 HEAD `3079b94`。本清单不授权删除、停进程、改数据库、关 PR 或删分支；每次真正清理前必须重新盘点。

## 1. 绝对保留白名单

| 对象 | 当前证据 | 处理 |
|---|---|---|
| `/home/zzx/project/botbattle/botzone.db` | 24,276,992 bytes；线上 50380 的真相源 | **保留且只读**；不得用测试、迁移或清理脚本写入 |
| `bot_uploads/`、`avatars/`、`logs/`、`.env`、`platform-ctl/` | 线上 DB 的关联产物、凭据和运行状态 | **保留**；不能与缓存一起批量删除 |
| PID 3673944 / `127.0.0.1:50380` | main 线上后端 | **保留**；本轮不启停 |
| `holdem-live-viewer-20260809` | 7 项未提交改动；PID 1917531/1926868 使用 50381/5173 | **保留**；他人开发中，不改、不停、不删 |
| `match-viewer-visual-audit-20260809` | 4 项未提交改动 | **保留**；他人开发中 |
| `qa-matrix-contest-schedule-20260809` | 本任务 worktree | 合并与验证完成前保留；仅本任务自行收尾 |
| 所有来源不明的备份/参考资料 | 见 §3 | 保留到用户明确给出保留期和恢复需求 |
| `/home/zzx/project/pok-arena` 等同级项目 | 独立历史项目，不属于本仓库清理范围 | **禁止触碰** |

## 2. Linked worktree 与分支

当前共有主工作树 + 20 个 linked worktree，`.worktrees/` 总计约 2.2 GB。

| 分类 | 数量/对象 | 结论 |
|---|---|---|
| 脏且可能活跃 | `holdem-live-viewer`、`match-viewer-visual-audit`、本任务 | 必须保留 |
| 干净且 HEAD 等于 main | `gomoku-social-tiers-20260809` | 只能在确认无 agent/进程/未推提交后作为优先清理候选 |
| 干净但提交不是 main 祖先 | 16 个：admin-backend-invariants、authoritative-terminal-event、bot-protocol-integrity、canonical-incident-api、canonical-protocol-docs、canonical-rules-cleanup、docs-admin-truth-audit、frontend-game-contract、frozen-version-failclosed、gamespec-contract-cleanup、holdem-history-qa、incident-contract-unify、qa-full-browser、strict-canonical-protocol、wiki-quickstart-cleanup、wiki-samples-doc-fix | squash 合并会让祖先判断失真；逐项核对对应 PR/patch 和 owner 后才可清，不能因“git status 干净”直接删 |

远端除 `origin/main` 外有 17 个分支：

- 已是 `origin/main` 祖先的 8 个清理候选：`audit/e2e`、`chore/remove-dataset-v2`、`feat/theme-system`、`fix/dead-field-cleanup`、`fix/holdem-settle`、`fix/table-and-residual`、`fix/visual-unify`、`refactor/judge-protocol-split`。
- 不是 `origin/main` 祖先的 9 个必须人工裁决：`audit/full-codebase-review`、`feat/desktop-density-refactor`、`fix/bot-response-compat-and-feedback`、`fix/duplicate-2-leg-independent-scoring`、`fix/full-browser-qa-20260808`、`fix/p1-realname-and-audit`、`fix/p2-residual-and-duplicate`、`fix/pin-game-config`、`fix/traditional-per-turn-restart`。其中带“compat”和旧 Traditional 协议的分支可能与当前唯一严格协议冲突，**不得重新合并，也不得未经 owner 确认就删除**。
- GitHub 当前唯一开放 PR 是 Draft [#144](https://github.com/zhouzixiang1/botbattle/pull/144)。描述仍含旧的 21-test/Wine 语境，和已合并 main 有大范围重叠；应先比较 patch 与后续 PR，再决定关闭或抽取，不可直接合并。

## 3. 主目录未跟踪对象与缓存候选

| 对象 | 大小/状态 | 建议 |
|---|---|---|
| `botzone.db.bak.20260808194007` | 约 11 MB | 数据库备份；先定恢复点/保留期，再决定归档或删除 |
| `botzone.db.bak.pre-pr147-202608092202` | 约 24 MB | PR #147 前备份；至少保留到本轮上线与数据核验完成 |
| `docs/superpowers/...pencil...md` | 2 个未跟踪设计/计划文件 | 确认是否为历史决策记录；若保留应归入 `doc/` 或外部归档，不能长期悬空 |
| `refs/SAU_Game_Platform_2.1.0_r3.rar` 及 `refs/ui-refs/` | `refs/` 约 181 MB | 外部参考资料；确认许可证与后续用途后归档，不纳入产品构建 |
| `.e2e_botzone.db` | 约 264 KB，2026-08-01 | 旧 QA 数据库候选；确认无脚本引用后可删 |
| `browser_shots/` | 约 33 MB | 历史截图；保留最终验收证据，其余按日期归档 |
| `logs/` | 约 41 MB；含 `web.log` 和轮转日志 | 线上证据；按日志保留策略轮转，禁止直接清空当前日志 |
| 主目录 `.pytest_cache`、`__pycache__` | 可再生缓存 | 在确认无进程使用后可清；不要和运行产物混用通配符 |
| 各 worktree 的 `node_modules/dist/test-results/playwright-report/.pytest_cache/__pycache__` | 可再生但分属不同 owner | 随该 worktree 正规移除；不要进入他人 worktree 单独清 |

## 4. 线上演示/压测数据

只读统计显示：36 个用户、76 个 Bot（Holdem 34 / Gomoku 20 / Pencil 22）、1117 条对局索引、2 个赛事；其中 30 个 `tester01`～`tester30` 测试用户。赛事为 `#3 0808 finished` 与 `#5 09 draft`，未发现名为 `0809` 的赛事。

另有明确压测残留：

- `site_announcement = "loadtest 公告"`；
- welcome 模板正文为 `loadtest`；
- 三个邮件模板标题仍使用 `botzone-platform`；
- 大量 tester Bot/对局可能影响排行榜、搜索、首页统计和真实用户观感。

这些是业务数据，不是文件缓存。清理前必须由用户确定：保留哪些演示账号、是否保留 `0808` 历史结果、是否删除 `#5 09` 草稿、排行榜是否重算、关联对局/评分/通知如何处理。当前阶段全部保留。

## 5. 最终清理顺序（获授权后）

1. 重新执行 `git worktree list`、各 worktree `git status`、进程和端口盘点；逐项确认 owner。
2. 对已合并且干净的 worktree：精确停止该 worktree 自己的 PID，再 `git worktree remove <精确路径>`。
3. 仅删除对应本地/远端分支并 `git remote prune origin`；开放 PR 先由 owner 关闭或合并。
4. 数据库备份、截图、日志按书面保留期归档；绝不对 `/home/zzx/project`、仓库根或 `.worktrees/` 做递归通配删除。
5. 演示数据另开数据库变更任务，先在复制库演练并生成影响清单；主库操作需用户再次明确授权。
6. 清理后复核：main 状态、50380、主 DB hash/mtime、端口、剩余 worktree/分支和产品健康。

> 返回 [doc/INDEX.md](./INDEX.md)
