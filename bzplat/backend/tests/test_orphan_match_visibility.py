"""孤儿对局可见性测试（对抗审计 PR-I）。

PR #88/#93 孤儿清理把 matches_* 的 bot_a_id/bot_b_id 置 NULL（保行不删），
但读路径原来用 INNER JOIN bots → 孤儿对局被 JOIN 丢弃，从 UI/replay/历史消失。
本测试验证改 LEFT JOIN 后：孤儿对局在 list/get/bracket 都可见。
"""
from __future__ import annotations

from bzplat.backend.store import Store


def _store_with_orphan_match(tmp_path) -> tuple[Store, str]:
    """建库 + 2 bot + 1 对局 + 删 1 bot（触发 SET NULL）→ 对局变孤儿。"""
    s = Store(str(tmp_path / "orphan.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    mid = "m_orphan_test"
    s.create_match(mid, b1["id"], b2["id"], owner_id=u["id"], game_id="holdem")
    # 删 b1 → matches_holdem.bot_a_id 置 NULL（FK ON DELETE SET NULL）
    s.delete_bot(b1["id"])
    return s, mid


def test_list_matches_includes_orphaned(tmp_path):
    """孤儿对局（bot_a_id NULL）应出现在 list_matches（LEFT JOIN 保行）。"""
    s, mid = _store_with_orphan_match(tmp_path)
    listed = s.list_matches(limit=100)
    ids = {m["id"] for m in listed}
    assert mid in ids, f"孤儿对局 {mid} 应在 list_matches 中可见，实际 ids={ids}"
    # count_matches（无 JOIN）应与 list_matches（LEFT JOIN）一致
    total = s.count_matches()
    assert total == len(listed), f"count={total} != list={len(listed)}（孤儿对局被 JOIN 丢弃）"
    s.close()


def test_get_match_detailed_returns_orphaned(tmp_path):
    """孤儿对局详情应可访问（bot_a_name=NULL），不返 None/404。"""
    s, mid = _store_with_orphan_match(tmp_path)
    d = s.get_match_detailed(mid)
    assert d is not None, f"孤儿对局 {mid} 详情应可见（LEFT JOIN），实际 None"
    # bot_a 名为 NULL（已删），bot_b 名保留
    assert d.get("bot_a_name") is None, f"已删 bot 的 bot_a_name 应为 NULL，实际 {d.get('bot_a_name')}"
    assert d.get("bot_b_name") == "botB"
    s.close()


def test_contest_bracket_handles_deleted_bot(tmp_path):
    """对阵图中 bot 被删的 pairing 行应可见（LEFT JOIN），bot 名 NULL。"""
    s = Store(str(tmp_path / "bracket.db"))
    org = s.create_user("org", "o@ex.com", "x")
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    cid = s.create_contest("Cup", organizer_id=org["id"], game_id="holdem")["id"]
    # 建一个 pairing 引用 b1/b2
    with s._tx() as c:
        c.execute(
            "INSERT INTO contest_pairings(contest_id, stage_idx, round_num, bot_a_id, bot_b_id) "
            "VALUES(?,0,1,?,?)",
            (cid, b1["id"], b2["id"]),
        )
    # 删 b1 → pairing.bot_a_id 置 NULL（SET NULL）
    s.delete_bot(b1["id"])
    bracket = s.contest_bracket(cid)
    assert len(bracket) == 1, f"删 bot 的 pairing 应仍可见（LEFT JOIN），实际 {len(bracket)} 条"
    assert bracket[0].get("bot_a_name") is None  # 已删
    assert bracket[0].get("bot_b_name") == "botB"
    s.close()


def test_list_liked_top_includes_orphaned(tmp_path):
    """孤儿对局（bot_a_id NULL）有点赞时应出现在 liked_top（LEFT JOIN 保行）。

    审计发现 list_liked_top_matches 用 INNER JOIN（与 list_matches 的 LEFT JOIN
    契约不一致），删 bot 后孤儿对局从点赞榜消失。修复后应可见。
    """
    s, mid = _store_with_orphan_match(tmp_path)
    # 把孤儿对局标 completed + 加点赞，使其满足 liked_top 的 WHERE
    # （update_match 不支持 likes_count/views_count，直接写表）
    s.update_match(mid, status="completed")
    with s._tx() as c:
        c.execute("UPDATE matches_holdem SET likes_count=3, views_count=10 WHERE id=?", (mid,))
    top = s.list_liked_top_matches(limit=10)
    ids = {m["id"] for m in top}
    assert mid in ids, f"孤儿对局 {mid}（有点赞）应在 liked_top 中可见，实际 ids={ids}"
    # 孤儿方的 bot_a_name 应为 None（LEFT JOIN 不丢弃）
    orphan = next(m for m in top if m["id"] == mid)
    assert orphan.get("bot_a_name") is None, "已删 bot 的 bot_a_name 应为 NULL（LEFT JOIN 保行）"
    assert orphan.get("bot_b_name") == "botB"
    s.close()
