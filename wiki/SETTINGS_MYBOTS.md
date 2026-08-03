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
- **编辑**：内联表单改 display_name/description（`PATCH /api/bots/{id}`）
- **删除**：软删（is_active=0，`DELETE /api/bots/{id}`）
- Bot 名链接到 Bot 详情页。

> 私有 Bot 功能已下线——所有 Bot 默认且仅处于公开状态，不再有公开/私有切换。

## 后端端点（owner 专用，require_user）

| 端点 | 说明 |
|------|------|
| `PATCH /api/bots/{id}` | owner 改 display_name/description/is_active（受限白名单；非 owner 403） |
| `DELETE /api/bots/{id}` | owner 软删（is_active=0；非 owner 403） |

> 资料编辑（profile/avatar/change-password）端点已就绪，设置中心补全前端 UI 并聚合到 /settings。
