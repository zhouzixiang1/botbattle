# 五子棋赛事演示 Bot

这组三档 Bot 只用于 `seed_contest_showcase.py` 生成长期只读演示赛事。它们通过真实裁判运行，
不会修改或伪造结果：

- `tactical`：先干扰可见的连续计划，再构造自己的五连；
- `steady`：执行固定的连续进攻计划，不主动防守；
- `foundation`：按固定离散序列合法落子。

三档均不读取时间、不使用随机数。12 个演示 Bot 固定分配为 1–4 tactical、5–8 steady、
9–12 foundation，配合内置蛇形分组后每组各一档，双循环积分稳定为 8/4/0。四组
有意复用同一强/中/弱矩阵；这不是 12 种自然棋力，也不用于正式竞技或排行榜。

运行 `../build_sample.sh` 会从同一源码和 `PROFILE=1/2/3` 构建三个 canonical Linux x86_64
ELF。提交二进制更新时必须同步 `showcase_seed.py` 中的 SHA-256 清单并跑严格 seed 验收。
manifest 策略版本为 `gomoku-showcase-matrix-v2`；已有 partial 图不得混入新版本，必须先用
官方命令 rollback，再从空白展示 namespace 重新 seed。
