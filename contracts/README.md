# 协议 JSON Schema

本目录保存平台**唯一现行 Holdem 协议**的机器可读契约，与
[`wiki/PROTOCOL.md`](../wiki/PROTOCOL.md)一致。

| 文件 | 用途 |
|------|------|
| `protocol_request.schema.json` | 固定 70 手、11 字段的 Holdem request payload |
| `protocol_request_envelope.schema.json` | Traditional/首回合完整历史与 LongRunning 后续单 request 信封 |
| `protocol_response.schema.json` | 必须包含 `response` 的 Holdem 响应信封 |

顶层整数和缺少 `response` 的旧 `{"a":...}` 不会通过 schema。`debug` / `data` /
`globaldata` 等额外顶层键允许存在，但运行时会丢弃；只有 `response` 进入历史和裁判。

当前 JSON Schema 覆盖 Holdem；Gomoku / Pencil 共用相同的严格外层信封，其
`response` payload 为 `{"x":int,"y":int}`，详见协议 Wiki。上传预检与正式首回合
使用同一信封和严格规则。
