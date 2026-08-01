# 用户主页与全局搜索

## 用户主页 `/user/:name`

展示某用户的公开档案与战绩，从排行榜、Bot 详情页（owner）、对局列表等任何出现用户名的地方点击进入。

**内容**：
- 头像（无头像时显示首字母占位）
- 显示名 / @用户名 / 角色徽章（管理员/组织者）
- 简介（bio）
- 注册时间、参与对局总数
- 总战绩卡片：总胜率、胜场、负/平场、Bot 数
- Bot 列表（卡片网格，链接到 Bot 详情页）

查看自己主页时显示「编辑资料」按钮（链接到 `/settings`，完善；profile/avatar 端点 已就绪）。

## 全局搜索 `/search`

顶栏搜索框 + 独立搜索页，三 tab：

- **用户**：按用户名前缀搜索（`/api/users`）
- **Bot**：按 name/display_name 模糊搜索（`/api/search?q=&type=bots`，含 owner 名 + rating）
- **对局**：按 bot 名模糊搜索已完成对局（`/api/search?q=&type=matches`）

## 个人资料编辑（后端端点，

| 端点 | 鉴权 | 说明 |
|------|------|------|
| `PUT /api/auth/profile` | require_user | 更新 display_name（≤64）/ bio（≤500） |
| `POST /api/auth/avatar` | require_user | 上传头像（png/jpeg/webp/gif，≤2MB），存本地 `avatars/<uid>.<ext>` |

头像通过 `/avatars/<file>` 静态访问（StaticFiles 托管）。上传新头像会覆盖旧扩展名文件。

## 用户主页数据端点（公开）

| 端点 | 说明 |
|------|------|
| `GET /api/users/{username}/profile` | 公开档案 + 总战绩聚合（不含 email/password_hash） |
| `GET /api/users/{username}/bots` | 该用户的公开 Bot 列表 |

总战绩 = 该用户所有 Bot 的 ratings SUM(wins/losses/draws/matches_played/net_chips)。

## 数据基础

- `users` 表新增 `bio`/`avatar` 列（migration 幂等）。
- `update_user` 允许 bio/avatar；`_safe_user` 自动透传（剔除 password_hash）。
- store: `user_profile`（聚合）、`aggregate_owner_stats`、`search_bots`（模糊）、`search_matches`（模糊）。
