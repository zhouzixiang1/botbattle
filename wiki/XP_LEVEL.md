# 经验与等级系统

用户通过平台活动获得经验（XP），累积升级（Level）。对标 Botzone 的 level + 活跃度体系，等级可用于功能 gating（如 数据下载需 level ≥ 1）。

## 经验奖励（常量在 `store/schema.py`）

| 活动 | 经验 | 常量 |
|------|------|------|
| 参与一场对局 | 10 | `XP_MATCH_PARTICIPATE` |
| 对局胜利（额外） | 15 | `XP_MATCH_WIN` |
| 赛事报名 | 50 | `XP_CONTEST_PARTICIPATE` |
| 发表评论 | 2 | `XP_COMMENT` |
| 被关注 | 3 | `XP_FOLLOWED` |

> 对局经验仅非 contest 类型发放（contest 内部对局不计）。被关注/评论经验给目标用户（被关注者/评论者）。

## 等级阈值

递增曲线：升到 level N 需累计 `100 × N × (N+1) / 2` 经验。

| Level | 累计 XP |
|-------|---------|
| 0 | 0 |
| 1 | 100 |
| 2 | 300 |
| 3 | 600 |
| 4 | 1000 |
| 5 | 1500 |

`level_for_xp(xp)` 推导当前 level；`xp_for_level(level)` 算阈值。

## 触发点

- **对局完成**（`orchestrator._run_match`）：双方 owner 各加 XP（参与 + 胜者额外），仅非 contest。
- **赛事报名**（`POST /api/contests/{id}/register`）：报名者 +50 XP。
- **评论**（`POST /api/comments`）：评论者 +2 XP。
- **被关注**（`POST /api/users/{id}/follow`）：被关注者 +3 XP。

`store.award_xp(user_id, amount)` 加经验 + 重算 level + 更新 `last_active_at`。

## 数据

users 表加列 `xp`/`level`/`last_active_at`（migration）。`GET /api/auth/me` 与 `GET /api/users/{name}/profile` 返回 xp/level。

## 端点

- `GET /api/levels/info`（公开）：经验奖励值 + 等级阈值表（前端展示进度条用）。

## 功能 gating

`LEVEL_GATE_DOWNLOAD = 1`。

## 前端

用户主页显示 Lv.N 徽章 + 经验进度条。
