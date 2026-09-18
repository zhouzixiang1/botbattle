"""预检崩溃 detail：Bot 自己的 stderr 末尾只进 owner 可见的上传预检路径。

对局失败原因对对手可见，消息本身绝不携带 stderr——隐私边界由
「属性只被预检消费」+「str(err) 不含尾部」双重守护。
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games import preflight_bot
from bzplat.backend.games.base import bot_crash_detail
from bzplat.backend.runtime.binary_runner import BotCrashedError


def test_crash_detail_appends_flattened_stderr_tail():
    err = BotCrashedError("bot abc stdout EOF（进程退出码=255）")
    err.stderr_tail = (
        "[PYI-1:ERROR] Failed to extract torch/lib/libtorch_cpu.so: "
        "decompression resulted in return code -1!\nsecond line\n"
    )
    detail = bot_crash_detail(err)
    assert detail.startswith("Bot 进程异常退出: bot abc stdout EOF（进程退出码=255）")
    assert "Bot stderr 末尾：[PYI-1:ERROR] Failed to extract" in detail
    assert "\n" not in detail  # 换行压平，保证单行可展示
    assert "second line" in detail


def test_crash_detail_without_tail_oversized_or_non_string():
    assert (
        bot_crash_detail(BotCrashedError("bot x 退出码=1"))
        == "Bot 进程异常退出: bot x 退出码=1"
    )
    err = BotCrashedError("bot x 退出码=1")
    err.stderr_tail = "z" * 5000
    bounded = bot_crash_detail(err)
    assert "z" * 300 in bounded
    assert "z" * 301 not in bounded

    weird = BotCrashedError("bot y")
    weird.stderr_tail = 123  # 非字符串一律忽略
    assert bot_crash_detail(weird) == "Bot 进程异常退出: bot y"


def test_crash_message_never_contains_stderr_tail():
    err = BotCrashedError("bot abc stdout EOF（进程退出码=255）")
    err.stderr_tail = "SECRET-TAIL"
    assert "SECRET-TAIL" not in str(err)


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
def test_preflight_crash_detail_carries_owner_stderr(game_id, monkeypatch):
    """三游戏预检崩溃 detail 均带 Bot stderr 末尾（AGHX 排障场景回归）。"""
    from bzplat.backend.games import _botzone_protocol

    async def fake_exchange(*args, **kwargs):
        err = BotCrashedError("bot a7689 stdout EOF（进程退出码=255）")
        err.stderr_tail = "[PYI-1:ERROR] Failed to extract torch/lib/libtorch_cpu.so"
        raise err

    monkeypatch.setattr(_botzone_protocol, "preflight_exchange", fake_exchange)
    ok, detail = asyncio.run(
        preflight_bot(
            game_id,
            "/tmp/preflight-probe-bot",
            object(),
            runtime_mode="traditional",
        )
    )
    assert ok is False
    assert "Bot 进程异常退出" in detail
    assert "Bot stderr 末尾" in detail
    assert "torch/lib/libtorch_cpu.so" in detail
