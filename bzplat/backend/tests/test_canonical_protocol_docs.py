"""唯一现行 Bot 通信契约的文档、Schema 与退役产物守护。"""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _wiki_sample(relative: str, marker: str, language: str) -> str:
    text = (ROOT / relative).read_text(encoding="utf-8")
    prefix = f"<!-- SAMPLE:{marker} -->\n```{language}\n"
    assert prefix in text, f"Wiki 缺少完整样例标记: {marker}"
    return text.split(prefix, 1)[1].split("\n```", 1)[0] + "\n"


def test_holdem_response_schema_requires_response_and_declares_private_debug() -> None:
    schema = _json("contracts/protocol_response.schema.json")
    assert schema["type"] == "object"
    assert schema["required"] == ["response"]
    assert schema["additionalProperties"] is True
    assert set(schema["properties"]) == {"response", "debug"}
    assert schema["properties"]["response"]["type"] == "integer"
    assert schema["properties"]["response"]["minimum"] == -2
    # 无 type 约束表示 debug 可取任意 JSON 值；运行时负责私有清洗/限额，
    # additionalProperties 继续允许并丢弃 data/globaldata 等未知 metadata。
    assert set(schema["properties"]["debug"]) == {"description"}


def test_holdem_request_schema_pins_70_and_envelopes_are_closed() -> None:
    payload = _json("contracts/protocol_request.schema.json")
    assert payload["additionalProperties"] is False
    assert payload["properties"]["max_hand"] == {"type": "integer", "const": 70}
    assert payload["properties"]["hand"]["maximum"] == 69

    envelope = _json("contracts/protocol_request_envelope.schema.json")
    assert len(envelope["oneOf"]) == 2
    full, incremental = envelope["oneOf"]
    assert full["required"] == ["requests", "responses"]
    assert full["additionalProperties"] is False
    assert set(full["properties"]) == {"requests", "responses"}
    assert incremental["required"] == ["request"]
    assert incremental["additionalProperties"] is False
    assert set(incremental["properties"]) == {"request"}


def test_normative_docs_do_not_offer_retired_protocols() -> None:
    paths = [
        "README.md",
        "AGENTS.md",
        "wiki/INDEX.md",
        "wiki/PROTOCOL.md",
        "wiki/BOT_DEV.md",
        "wiki/TEXAS.md",
        "wiki/GOMOKU.md",
        "wiki/PENCIL.md",
        "doc/RUNTIME.md",
        "doc/REQUIREMENTS.md",
        "doc/DESIGN.md",
        "contracts/README.md",
    ]
    text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in paths)
    for retired in (
        "兼容回退",
        "默认 50 手",
        "50 小局",
        "默认 1s",
        "约 1s/步",
        "未握手时会在",
        "当前预检只验证",
    ):
        assert retired not in text, f"规范文档重新提供已退役语义: {retired}"

    for required in (
        "响应对象必须包含顶层字段 `response`",
        "LongRunning 缺失精确握手不回退",
        "technical_incident_count",
        "has_technical_incidents",
        "固定 **70 手**",
        "起始筹码固定 20000",
        "小盲固定 50",
        "大盲固定 100",
        "固定 **15×15**",
        "固定 **N=6**",
        "**900 秒",
        "唯一接受的上传产物是 **Linux x86_64 ELF**",
        "python:3.12-bookworm",
        "--platform linux/amd64",
        "ELF 64-bit LSB executable, x86-64",
    ):
        assert required in text, f"规范文档缺少唯一现行契约: {required}"


def test_player_wiki_has_no_repository_or_internal_instructions() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "wiki").glob("*.md"))
    for internal in (
        "samples/",
        "scripts/",
        "pytest",
        "bzplat/",
        ".worktrees/",
        "AGENTS.md",
        "../doc/",
        "/api/",
        "template_id",
        "technical_incident_count",
        "has_technical_incidents",
        "SSE",
        "WebSocket",
        "纯规则源码",
        "协议适配",
        "结果定义",
    ):
        assert internal not in text, f"玩家 Wiki 泄漏工程或内部接口内容: {internal}"


def test_player_wiki_is_quickstart_first_and_scopes_compatibility_guidance() -> None:
    index = (ROOT / "wiki/INDEX.md").read_text(encoding="utf-8")
    protocol = (ROOT / "wiki/PROTOCOL.md").read_text(encoding="utf-8")
    bot_dev = (ROOT / "wiki/BOT_DEV.md").read_text(encoding="utf-8")
    pencil = (ROOT / "wiki/PENCIL.md").read_text(encoding="utf-8")

    for required in (
        "## 第一次上传 Bot",
        '{"requests":[...],"responses":[...]}',
        "Windows、Linux 和 macOS",
        "Docker",
    ):
        assert required in index, f"Wiki 首页缺少快速上手信息: {required}"

    assert '{"requests":[<请求 payload>,...],"responses":[<本 Bot 过去的 response payload>,...]}' in protocol
    assert ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<" in protocol
    assert "不会改发完整历史，也不会切换为 Traditional" in protocol
    assert "8 秒首回合健康检查" in protocol
    assert "Pencil 的正式对局仍按每方 900 秒累计棋钟" in protocol

    for platform in ("## 4. Linux", "## 5. Windows", "## 6. macOS"):
        assert platform in bot_dev
    assert "C：Alpine 静态编译" in bot_dev
    assert "Python：Linux PyInstaller 打包" in bot_dev
    assert "该 8 秒不会计入正式对局" in bot_dev
    assert "不会替代或扣减双方各 900 秒累计棋钟" in pencil

    wiki_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "wiki").glob("*.md")
    )
    prose_without_required_signal = wiki_text.replace(
        ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<", ""
    )
    # Botzone/SAU 只作为 Pencil 预检故障的精确诊断信号出现，不重新提供
    # 第二套协议或泛化成平台对比说明。保留这两处用户可操作的兼容性提示，
    # 同时继续阻止 Wiki 漂回宽泛的 Botzone 比较文案。
    assert "Botzone JSON 首回合协议" in bot_dev
    assert "Botzone JSON 首回合通信" in bot_dev
    assert "旧 SAU 裁判使用的" in bot_dev
    assert "`name?`、`new`、`move`、`take` 等文本命令不是本平台协议" in bot_dev
    assert "Traditional/LongRunning 都不能转换协议" in bot_dev
    prose_without_scoped_diagnostics = prose_without_required_signal
    for allowed in ("Botzone JSON 首回合协议", "Botzone JSON 首回合通信"):
        prose_without_scoped_diagnostics = prose_without_scoped_diagnostics.replace(
            allowed, ""
        )
    assert "botzone" not in prose_without_scoped_diagnostics.casefold()
    assert "对比表" not in wiki_text


def test_wiki_samples_are_the_runner_regression_sources() -> None:
    cases = (
        ("wiki/BOT_DEV.md", "holdem:c", "c", "samples/callbot.c"),
        ("wiki/BOT_DEV.md", "holdem:python", "python", "samples/callbot.py"),
        ("wiki/GOMOKU.md", "gomoku:c", "c", "samples/gomokubot.c"),
        ("wiki/GOMOKU.md", "gomoku:python", "python", "samples/gomokubot.py"),
        ("wiki/PENCIL.md", "pencil:c", "c", "samples/pencilbot.c"),
        ("wiki/PENCIL.md", "pencil:python", "python", "samples/pencilbot.py"),
    )
    for doc, marker, language, source in cases:
        assert _wiki_sample(doc, marker, language) == (ROOT / source).read_text(encoding="utf-8")


def test_player_wiki_explains_contest_formats_as_player_flows() -> None:
    """赛事文档应回答玩家会经历什么，而不只是罗列模板名称。"""
    guide = (ROOT / "wiki/GUIDE.md").read_text(encoding="utf-8")
    for required in (
        "每个 Bot 与其他 Bot 各赛一次",
        "每对 Bot 交手两次",
        "组内按单循环或双循环比赛",
        "同分、尚未交手",
        "每场胜者进入下一轮",
        "休息可换 Bot",
    ):
        assert required in guide, f"平台指南缺少赛制过程: {required}"

    game_docs = {
        "wiki/TEXAS.md": ("瑞士 → 单败", "复式单循环", "Top 8 双循环决赛"),
        "wiki/GOMOKU.md": ("分组双循环 → 单败", "瑞士 → 单败", "课堂双循环"),
        "wiki/PENCIL.md": ("分组双循环 → 单败", "瑞士 → 单败", "900 秒累计棋钟"),
    }
    for relative, required_phrases in game_docs.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for phrase in required_phrases:
            assert phrase in text, f"{relative} 缺少赛事流程: {phrase}"


def test_retired_protocol_mutators_and_binary_are_absent() -> None:
    assert not (ROOT / "samples/callbot_bin").exists()
    assert not (ROOT / "scripts/migrate_bots_to_botzone.py").exists()
    assert not (ROOT / "scripts/e2e_prelim_16.py").exists()
    assert not (ROOT / "bzplat/backend/tests/test_migrate_bots_to_botzone.py").exists()
    assert not (ROOT / "samples/judges").exists()
    assert not (ROOT / "refs/botzone").exists()
    assert not (ROOT / "E2E_TEST_REPORT.md").exists()
    assert not (ROOT / "gui-test-screenshots/REPORT.md").exists()
    assert not (ROOT / "doc/PRELIM_FINAL_PLAN.md").exists()

    build_script = (ROOT / "samples/build_sample.sh").read_text(encoding="utf-8")
    for binary in (
        "callbot_linux_amd64",
        "gomokubot_linux_amd64",
        "pencilbot_linux_amd64",
    ):
        assert binary in build_script
    assert '"ELF 64-bit"' in build_script
    assert '"x86-64"' in build_script
