"""向后兼容转发层（全面解耦 PR4）。

全面解耦后，引擎/协议/结果/段位已物理迁入 ``games/<game>/`` 包。本包集中提供
旧 import 路径（``bzplat.backend.engine.<x>`` / ``bzplat.backend.protocol.<x>``）
的转发 shim，让现存代码（含测试）无需改动即可继续工作。

**设计**：转发逻辑集中在本包，games/ 包本身不含兼容逻辑（干净）。
``engine/`` 与 ``protocol/`` 下的旧文件改为一行 re-export 自本包。
后续可整体删除 ``_compat/``（清理由后续可选 PR）。
"""
