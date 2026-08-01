# 对局数据集下载 + 站点配置

## 对局数据集 `/data`

下载已完成对局的数据集（按游戏 × 月份打包，gzip 压缩，每行一条 JSON 对局）。对标 Botzone 的 downloadmatches。

- **列表**：`GET /api/matchpacks`（公开）—— 列出有数据的游戏×月份分组（含对局数）。
- **下载**：`GET /api/matchpacks/download?game_id=&month=`（require_user + **等级 ≥ 1** gating）—— 流式返回 gzip，每行一 JSON（含 id/game/双方 bot/winner/earnings/events 等）。

每条 JSON 格式：
```json
{"id":"...","game_id":"holdem","bot_a":{"id":1,"name":"A"},"bot_b":{...},
 "winner":0,"earnings_a":100,"events":[...]}
```

## 站点配置

站名、公告、about 等可由 admin 配置，前端从公开端点读取。

- **公开读取**：`GET /api/site/info`（无需登录）→ `{name, logo, announcement, about}`。
- **admin 修改**：`PATCH /api/admin/settings/site` `{name, logo, announcement, about}`。

存储在 `platform_settings`（键 `site_name`/`site_logo`/`site_announcement`/`site_about`），main.py 启动时 seed 默认值（站名 "Botbattle"，about "多游戏 Bot 线上对战平台"）。

## 前端

- 新页面 `DataDownload.tsx` + 路由 `/data` + 顶栏「数据」入口：按游戏/月份列表，等级不足显示锁定提示。
- 站点信息端点已就绪，前端可按需读取（如首页公告条、站名）。

## 等级 gating

`LEVEL_GATE_DOWNLOAD = 1`（PR-9）：下载数据集需用户 `level >= 1`（累计 XP ≥ 100）。对标 Botzone「等级 1 以上可用某功能」。
