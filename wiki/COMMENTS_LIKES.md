# 评论 + 点赞 + 浏览

对局与 Bot 详情页支持评论与点赞；首页展示「热门对局」点赞榜（对标 Botzone）。

## 功能

- **评论**：在 MatchDetail（`/match/:id`）与 BotDetail（`/bot/:id`）底部评论区发表评论；作者/admin 可删除。评论触发 target owner 的 `comment` 通知。
- **点赞**：评论区头部 ♥ 按钮；点赞/取消点赞（幂等）。对 match 点赞同步 +1 `likes_count`。
- **浏览计数**：打开 MatchDetail 时 POST `/api/matches/{id}/view` 记录浏览（+1 `views_count`）。
- **点赞榜**：首页「🔥 热门对局」展示 `likes_count > 0` 的对局（按点赞数倒序）。

## 后端端点

- `GET /api/comments?target_type=&target_id=`（公开）、`POST /api/comments`（需登录，触发通知）、`DELETE /api/comments/{id}`（作者/admin）。
- `POST /api/likes`、`DELETE /api/likes`、`GET /api/likes/status`（均需登录）。
- `POST /api/matches/{id}/view`（公开，+1 浏览）。
- `GET /api/matches/liked-top?limit=`（公开，点赞榜）。

## 前端

- 对局页 / Bot 详情页底部嵌入评论区 + 点赞按钮。
- 首页「🔥 热门对局」展示点赞榜。
