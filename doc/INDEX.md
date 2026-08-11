# 交付文档索引

本目录（`doc/`）存放面向**甲方验收与项目干系人**的工程文档：6 份核心交付文档，另有 6 份现行专项文档和本索引。

## 文档导航

| 文档 | 甲方问题 | 说明 |
|------|---------|------|
| [OVERVIEW.md](./OVERVIEW.md) | 这是什么、能做什么？ | 项目定位、能力一览、技术栈、目录结构、交付物清单 |
| [REQUIREMENTS.md](./REQUIREMENTS.md) | 需求是什么、都满足了吗？ | 功能/非功能需求、用户角色、需求覆盖追溯矩阵 |
| [DESIGN.md](./DESIGN.md) | 怎么设计的？ | 系统架构、模块设计、数据库、接口、前端、安全 |
| [DEVELOPMENT.md](./DEVELOPMENT.md) | 怎么开发、部署？ | 环境搭建、构建运行、编码规范、工作流、扩展、部署运维 |
| [TESTING.md](./TESTING.md) | 测得怎么样？ | 测试策略、范围、执行结果、压测分析、缺陷与结论 |
| [SUMMARY.md](./SUMMARY.md) | 整体交付如何？ | 里程碑、成果指标、验收清单、经验教训、遗留与维护 |

## 专项文档

| 文档 | 说明 |
|------|------|
| [SECURITY.md](./SECURITY.md) | 公网加固运维细节：三文件日志、IP 透传（nginx/frp）、限流、安全响应头、相关环境变量 |
| [LOADTEST.md](./LOADTEST.md) | QA 多角色关键业务链路与多局测试（含隔离 DB-direct 播种，不声称覆盖全部端点） |
| [RUNTIME.md](./RUNTIME.md) | 运维侧运行时：本地 Docker 安全边界、全来源持久执行队列、恢复与评分重建 No-Go |
| [JUDGE_CODE.md](./JUDGE_CODE.md) | 各游戏裁判引擎代码位置、固定规则契约与改动工作流（面向平台开发者） |
| [BROWSER_ACCEPTANCE.md](./BROWSER_ACCEPTANCE.md) | 四角色 × 全页面/操作的浏览器验收矩阵、跨视口与 Console/Network 门槛 |
| [CLEANUP_INVENTORY.md](./CLEANUP_INVENTORY.md) | worktree/分支/备份/缓存/演示数据的只读盘点与保留白名单 |

## doc/ 与 wiki/ 的分工

| 目录 | 受众 | 内容 |
|------|------|------|
| `doc/`（本目录） | 甲方、项目干系人、平台开发者 | 需求、设计、开发、测试、总结等工程交付文档 |
| [`wiki/`](../wiki/) | Bot 玩家、访客 | 游戏规则、对局协议、Bot 开发指南、功能使用说明 |

> 两边互链不复制：工程内容进 `doc/`，协议规则进 `wiki/`。

## 命名约定
- 文件名：`SCREAMING_SNAKE_CASE.md` 英文（可检索，与 wiki 一致）。
- 文档内 H1 标题：中文。
- 新增文档后回填本 INDEX。
