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


def test_holdem_response_schema_requires_response_and_allows_ignored_metadata() -> None:
    schema = _json("contracts/protocol_response.schema.json")
    assert schema["type"] == "object"
    assert schema["required"] == ["response"]
    assert schema["additionalProperties"] is True
    assert set(schema["properties"]) == {"response"}
    assert schema["properties"]["response"]["type"] == "integer"
    assert schema["properties"]["response"]["minimum"] == -2


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


def test_player_wiki_is_quickstart_first_and_has_no_comparison_prose() -> None:
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
    assert "botzone" not in prose_without_required_signal.casefold()
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
