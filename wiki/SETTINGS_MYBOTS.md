# 个人设置中心 + MyBots 管理

## 个人设置 `/settings`

顶栏用户名处进入（或用户主页「编辑资料」按钮）。四个 tab：

- **资料**：头像上传（png/jpeg/webp/gif ≤2MB）、显示名（≤64）、简介（≤500）。调 `PUT /api/auth/profile` + `POST /api/auth/avatar`。
- **密码**：修改密码（旧密码 + 新密码 ≥8）。调 `POST /api/auth/change-password`（改后清除所有会话，需重新登录）。
- **通知偏好**：4 个邮件提醒开关（对局完成/被关注/赛事/被评论）。调 `PUT /api/notification-prefs`。
- **我的收藏**：收藏的 Bot 列表（卡片，链接到 Bot 详情）。

## MyBots 管理增强 `/my-bots`

每个 Bot 卡片新增操作：
- **启用/停用**：`POST /api/bots/{id}/active`
- **版本**：打开版本管理对话框（上传新版本 / 查看历史 / 回滚）
- **编辑**：内联表单改 display_name/description（`PATCH /api/bots/{id}`）
- **删除**：软删（is_active=0，`DELETE /api/bots/{id}`）
- Bot 名链接到 Bot 详情页。
- 每张卡片显示当前 **Botzone 运行模式**（`longrunning` / `traditional`）徽章 + 版本号。

### 上传新 Bot

上传时须选择：
- **游戏类型**（holdem / gomoku / pencil）
- **Botzone 运行模式**：
  - **LongRunning（长驻，推荐）**：进程整场不重启；首回合发完整历史信封，Bot 响应后输出 `>>>BOTZONE_REQUEST_KEEP_RUNNING<<<` 握手，之后每回合只发单 request。适合有昂贵初始化（如神经网络）的 Bot。
  - **Traditional（传统）**：每回合发完整历史信封 `{"requests":[...],"responses":[...]}`，Bot 自重放重建状态。适合无状态、易调试的 Bot。

详见 [协议规范](#/wiki?slug=protocol) §1。

### 版本管理（1 Bot 1 游戏，多版本）

一个 Bot 可上传多个版本，随时切换激活（回滚）：

- **上传新版本**：`POST /api/bots/{id}/versions`（带 `runtime_mode` + 文件）。新版本成为当前版本。
- **查看历史**：`GET /api/bots/{id}/versions`（owner / admin 可见；含每版本的 runtime_mode、大小、时间、备注）。
- **回滚到此版本**：`POST /api/bots/{id}/versions/{version}/activate`。不删除其他版本，仅切换 current_version + 镜像（binary_path / runtime_mode / ...）。回滚时该版本的 runtime_mode 一并恢复。

> 版本管理用于迭代 Bot 而不丢失旧版本。赛事对局冻结到报名时的版本（`contest_pairings.bot_a_version_id`），不受上传新版本影响。

> 私有 Bot 功能已下线——所有 Bot 默认且仅处于公开状态，不再有公开/私有切换。

## 后端端点（owner 专用，require_user）

| 端点 | 说明 |
|------|------|
| `PATCH /api/bots/{id}` | owner 改 display_name/description/is_active（受限白名单；非 owner 403） |
| `DELETE /api/bots/{id}` | owner 软删（is_active=0；非 owner 403） |
| `POST /api/bots` | 上传新 Bot（带 `runtime_mode` Form） |
| `POST /api/bots/{id}/versions` | owner 上传新版本（带 `runtime_mode`） |
| `GET /api/bots/{id}/versions` | owner/admin 查版本历史（非 owner 403） |
| `POST /api/bots/{id}/versions/{v}/activate` | owner 回滚到指定版本（恢复该版本 runtime_mode） |

> 资料编辑（profile/avatar/change-password）端点已就绪，设置中心补全前端 UI 并聚合到 /settings。
