# 协议 JSON Schema

本目录保存平台**唯一现行 Holdem 协议**的机器可读契约，与
[`wiki/PROTOCOL.md`](../wiki/PROTOCOL.md)一致。

| 文件 | 用途 |
|------|------|
| `protocol_request.schema.json` | 固定 70 手、11 字段的 Holdem request payload |
| `protocol_request_envelope.schema.json` | Traditional/首回合完整历史与 LongRunning 后续单 request 信封 |
| `protocol_response.schema.json` | 必须包含 `response` 的 Holdem 响应信封 |

顶层整数和缺少 `response` 的旧 `{"a":...}` 不会通过 schema。可选顶层 `debug` 允许任意
JSON 值，但只有正式 Bot-vs-Bot 对局会在终态后把它作为有界、清洗、鉴权的私有 sidecar；
上传预检丢弃它。`data` / `globaldata` 等其他顶层键始终丢弃。只有 `response` 进入历史、
后续请求、裁判和结果。

当前 JSON Schema 覆盖 Holdem；Gomoku / Pencil 共用相同的严格外层信封，其
`response` payload 为 `{"x":int,"y":int}`，详见协议 Wiki。上传预检与正式首回合
使用同一信封和严格规则。
