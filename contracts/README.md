# 协议 JSON Schema

本目录为 **德州扑克（holdem）Botzone 兼容核心协议** 的 JSON Schema，供校验与对照 [wiki/PROTOCOL.md](../wiki/PROTOCOL.md)。

| 文件 | 用途 |
|------|------|
| `protocol_request.schema.json` | 请求负载（Botzone 全名字段 `num_players`/`my_cards`/`history`/...） |
| `protocol_response.schema.json` | 响应信封（`{"response": <裸整数>}`：`-1` fold / `-2` allin / `0` call-check / `>0` raise 额外量） |

**范围**：当前 schema **仅覆盖 holdem** 动作协议；五子棋 / 点格棋已经使用 Botzone 风格信封与 `{x,y}` payload，但尚未在本目录提供 JSON Schema，协议见 wiki 对应页。

**协议**：请求负载字段名与响应动作编码对齐 [TexasHoldem2p](https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p)。响应 `response` 为裸整数；raise 的正整数是「额外下注筹码」(raise delta = 目标总额 − 本街已投)。平台固定 70 手、运行时与可选响应字段限制见 [协议规范](../wiki/PROTOCOL.md)。
