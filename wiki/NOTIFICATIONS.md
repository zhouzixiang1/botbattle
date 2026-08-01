# 通知系统

站内通知 + 可选邮件提醒。对局完成、被关注、赛事阶段变化、被评论等事件会生成通知，顶栏铃铛实时显示未读数。

## 通知类型

| type | 触发场景 | 邮件 pref 字段 |
|------|----------|----------------|
| `match_done` | challenge/table/ladder 对局完成（contest 内部对局不通知） | `email_match_done` |
| `followed` | 被其他用户关注（PR-4） | `email_followed` |
| `contest` | 赛事阶段变化（PR-5） | `email_contest` |
| `comment` | Bot/对局被评论（PR-7） | `email_comment` |

## 前端

- **顶栏铃铛** `NotificationBell.tsx`：未读红点 + 下拉最近 10 条 + 「全部已读」+ 链到通知列表页；每 30s 轮询未读数。
- **通知列表页** `/notifications`：全部/未读筛选、单条已读、全部已读。

## 后端

**新表**：
- `notifications(id, user_id, type, title, body, link, is_read, created_at)` + 索引 `(user_id, id DESC)`。
- `notification_prefs(user_id, email_match_done, email_followed, email_contest, email_comment)`（每用户一行，默认全 0）。

**NotificationManager**（`notifications/__init__.py`）：
- `notify(user_id, type, title, body, link, send_email=False)`：写站内通知；send_email=True 时按用户 prefs + Mailer 发邮件（邮件失败不阻断通知）。
- `notify_both_owners(bot_a, bot_b, ...)`：对局完成场景，通知双方 Bot owner（同 owner 去重）。
- main.py 注入 `orch.notifier = notifier`；orchestrator 对局完成（非 contest）时自动通知双方 owner。

**端点**（均 require_user）：

| 端点 | 说明 |
|------|------|
| `GET /api/notifications?unread_only=&limit=&offset=` | 通知列表 + unread_count |
| `GET /api/notifications/unread-count` | 未读数（铃铛轮询用） |
| `POST /api/notifications/read` `{id}` | 单条标记已读 |
| `POST /api/notifications/read-all` | 全部已读 |
| `GET /api/notification-prefs` | 通知/邮件偏好 |
| `PUT /api/notification-prefs` | 更新偏好（email_match_done/email_followed/email_contest/email_comment） |

> 注意：`store.update_notification_prefs` 在 `_tx()` 内内联读取结果，**不可递归调用** `get_notification_prefs`（会重入 threading.Lock 死锁）。
