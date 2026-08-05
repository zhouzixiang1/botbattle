# 通知系统

站内通知 + 可选邮件提醒。对局完成、被关注、赛事阶段变化、被评论等事件会生成通知，顶栏铃铛实时显示未读数。

## 通知类型

| type | 触发场景 | 邮件 pref 字段 |
|------|----------|----------------|
| `match_done` | challenge/table/ladder 对局完成（contest 内部对局不通知） | `email_match_done` |
| `followed` | 被其他用户关注 | `email_followed` |
| `contest` | 赛事阶段变化 | `email_contest` |
| `comment` | Bot/对局被评论 | `email_comment` |

## 前端

- **顶栏铃铛**：未读红点 + 下拉最近 10 条 + 「全部已读」+ 链到通知列表页；定时轮询未读数。
- **通知列表页** `/notifications`：全部/未读筛选、单条已读、全部已读。

## 端点（均需登录）

| 端点 | 说明 |
|------|------|
| `GET /api/notifications?unread_only=&limit=&offset=` | 通知列表 + 未读数 |
| `GET /api/notifications/unread-count` | 未读数（铃铛轮询用） |
| `POST /api/notifications/read` `{id}` | 单条标记已读 |
| `POST /api/notifications/read-all` | 全部已读 |
| `GET /api/notification-prefs` | 通知/邮件偏好 |
| `PUT /api/notification-prefs` | 更新偏好（email_match_done/email_followed/email_contest/email_comment） |

## 邮件提醒

对局完成、被关注、赛事阶段变化、被评论等事件默认生成**站内通知**；如需邮件提醒，可在「个人设置 → 通知偏好」按类型开启（邮件发送失败不影响站内通知）。
