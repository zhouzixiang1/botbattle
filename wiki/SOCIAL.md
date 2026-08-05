# 社交：关注用户 + 收藏 Bot

## 关注用户

在用户主页（`/user/:name`）点击「+ 关注 / 已关注」按钮。被关注者会收到一条 `followed` 通知。

**展示**：用户主页显示「关注 N / 粉丝 N」。

## 收藏 Bot

在 Bot 详情页（`/bot/:id`）点击「☆ 收藏 / ★ 已收藏（N）」按钮。收藏数实时显示。

## 后端端点

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `POST /api/users/{id}/follow` | require_user | 关注（不能关注自己；重复幂等；触发 followed 通知） |
| `DELETE /api/users/{id}/follow` | require_user | 取关 |
| `GET /api/users/{id}/follow-status` | require_user | 是否关注 + follower/following 数 |
| `GET /api/users/{id}/followers` | 公开 | 粉丝列表 |
| `GET /api/users/{id}/following` | 公开 | 关注列表 |
| `POST /api/bots/{id}/favorite` | require_user | 收藏 Bot（重复幂等） |
| `DELETE /api/bots/{id}/favorite` | require_user | 取消收藏 |
| `GET /api/bots/{id}/favorite-status` | require_user | 是否收藏 + 收藏数 |
| `GET /api/auth/me/favorites` | require_user | 我的收藏列表 |

关注其他用户后，对方会收到一条 `followed` 通知。
