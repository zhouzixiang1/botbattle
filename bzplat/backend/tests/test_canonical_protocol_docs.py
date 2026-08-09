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


def test_holdem_response_schema_is_closed() -> None:
    schema = _json("contracts/protocol_response.schema.json")
    assert schema["type"] == "object"
    assert schema["required"] == ["response"]
    assert schema["additionalProperties"] is False
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
        "响应对象只允许一个顶层字段 `response`",
        "LongRunning 缺失精确握手不回退",
        "technical_incident_count",
        "has_bot_incidents",
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


def test_player_wiki_has_no_repository_only_instructions() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "wiki").glob("*.md"))
    for internal in (
        "samples/",
        "scripts/",
        "pytest",
        "bzplat/",
        ".worktrees/",
        "AGENTS.md",
        "../doc/",
    ):
        assert internal not in text, f"玩家 Wiki 泄漏仓库开发入口: {internal}"


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
