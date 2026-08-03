# 协议 JSON Schema

本目录为 **德州扑克（holdem）紧凑行协议** 的 JSON Schema，供校验与对照 [wiki/PROTOCOL.md](../wiki/PROTOCOL.md)。

| 文件 | 用途 |
|------|------|
| `protocol_request.schema.json` | 平台 → Bot 决策请求（`t":"act"` 等字段） |
| `protocol_response.schema.json` | Bot → 平台响应（`a` ∈ `f`/`c`/`k`/`r`/`all`，可选 `x`） |

**范围**：当前 schema **仅覆盖 holdem** 动作协议；五子棋 / 点格棋走 `t":"mv"` 行协议，见 wiki 对应页，不在本目录。

**解析宽容度**：服务端 `games/holdem/protocol.py` 的解析除短码（`f`/`c`/`k`/`r`/`all`）外，也接受**完整动作名**（`fold` / `call` / `check` / `raise` / `allin`）。上传 Bot 仍推荐使用短码，与 schema 一致。
