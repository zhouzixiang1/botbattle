"""Store 层单测。"""
from __future__ import annotations

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CODE_VERIFY,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
)


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "botzone.db"))


def test_create_and_get_user(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", hash_password("password1"))
    assert u["username"] == "alice"
    assert u["email_verified"] == 0
    assert s.get_user(u["id"])["email"] == "a@ex.com"
    assert s.get_user_by_username("alice")["id"] == u["id"]
    assert s.get_user_by_email("a@ex.com")["id"] == u["id"]


def test_update_and_list_users(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("bob", "b@ex.com", hash_password("password1"))
    s.update_user(u["id"], display_name="Bob", email_verified=1)
    got = s.get_user(u["id"])
    assert got["display_name"] == "Bob"
    assert got["email_verified"] == 1
    assert len(s.list_users()) >= 1


def test_sessions(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("carol", "c@ex.com", hash_password("password1"))
    s.add_session("tok1", u["id"], "2099-01-01T00:00:00", ip_addr="1.2.3.4")
    sess = s.get_session("tok1")
    assert sess["user_id"] == u["id"]
    assert sess["ip_addr"] == "1.2.3.4"
    assert s.delete_session("tok1")
    assert s.get_session("tok1") is None
    s.add_session("tok2", u["id"], "2099-01-01T00:00:00")
    s.add_session("tok3", u["id"], "2099-01-01T00:00:00")
    assert s.delete_sessions_for_user(u["id"]) == 2


def test_email_codes_and_templates(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("dave", "d@ex.com", hash_password("password1"))
    s.add_email_code(u["id"], CODE_VERIFY, "123456", "2099-01-01T00:00:00")
    code = s.get_latest_email_code(u["id"], CODE_VERIFY)
    assert code["code"] == "123456"
    s.mark_email_code_used(code["id"])
    assert s.get_latest_email_code(u["id"], CODE_VERIFY) is None

    tpl = s.get_template(TPL_VERIFY_EMAIL)
    assert tpl is not None
    assert "验证码" in tpl["subject"] or "code" in tpl["body_text"].lower() or "{{code}}" in tpl["body_text"]
    keys = {t["key"] for t in s.list_templates()}
    assert TPL_VERIFY_EMAIL in keys
    assert TPL_WELCOME in keys
    s.update_template(
        TPL_VERIFY_EMAIL,
        subject="new subj",
        body_html="<p>x</p>",
        body_text="x",
    )
    assert s.get_template(TPL_VERIFY_EMAIL)["subject"] == "new subj"
    s.add_outbox("d@ex.com", "subj", template_key=TPL_VERIFY_EMAIL)


def test_password_resets(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("erin", "e@ex.com", hash_password("password1"))
    s.add_password_reset("rtok", u["id"], "2099-01-01T00:00:00")
    assert s.get_password_reset("rtok")["user_id"] == u["id"]
    s.mark_password_reset_used("rtok")
    assert s.get_password_reset("rtok") is None


def test_bots_versions_ratings(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("frank", "f@ex.com", hash_password("password1"))
    bot = s.create_bot(owner_id=u["id"], name="mybot", format="elf", os="linux")
    assert bot["name"] == "mybot"
    assert s.get_bot_by_owner_name(u["id"], "mybot")["id"] == bot["id"]

    ver = s.add_bot_version(
        bot["id"],
        binary_path="/tmp/bot.bin",
        checksum="abc",
        size_bytes=10,
        os="linux",
        arch="x86_64",
        format="elf",
    )
    assert ver["version"] == 1
    assert s.get_bot(bot["id"])["current_version"] == 1
    assert len(s.list_bot_versions(bot["id"])) == 1

    rating = s.ensure_rating(bot["id"])
    assert rating["rating"] == 1500.0
    s.update_rating_row(bot["id"], rating=1600.0, wins=1, matches_played=1)
    assert s.get_rating(bot["id"])["rating"] == 1600.0
    board = s.list_leaderboard(10)
    assert any(r["bot_id"] == bot["id"] for r in board)

    s.update_bot(bot["id"], display_name="My Bot")
    assert s.get_bot(bot["id"])["display_name"] == "My Bot"
    assert len(s.list_bots(owner_id=u["id"])) == 1
    assert s.delete_bot(bot["id"])


def test_matches_replays_pair_stats(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("gina", "g@ex.com", hash_password("password1"))
    a = s.create_bot(owner_id=u["id"], name="bot_a")
    b = s.create_bot(owner_id=u["id"], name="bot_b")
    m = s.create_match("m1", a["id"], b["id"], owner_id=u["id"])
    assert m["status"] == "pending"
    s.update_match("m1", status="completed", winner=0, earnings_a=100)
    assert s.get_match("m1")["winner"] == 0
    listed = s.list_matches(limit=10, offset=0, owner_id=u["id"])
    assert len(listed) == 1
    listed2 = s.list_matches(limit=10, offset=0, bot_id=a["id"], status="completed")
    assert len(listed2) == 1

    s.upsert_replay("m1", events_json='[{"t":1}]', hands_json="[]")
    assert '"t": 1' in s.get_replay("m1")["events_json"] or '"t":1' in s.get_replay("m1")["events_json"]
    s.upsert_pair_stats(a["id"], b["id"], 1.5, 0.1, 2.0, 3)

    # count_matches：与 list_matches 语义对齐（status / game_id 过滤）
    assert s.count_matches() == 1
    assert s.count_matches(status="completed") == 1
    assert s.count_matches(status="running") == 0
    # create_match 默认 game_id=holdem
    assert s.count_matches(game_id="holdem") == 1
    assert s.count_matches(game_id="gomoku") == 0
    assert s.count_matches(status="completed", game_id="holdem") == 1
    assert s.count_matches(status="completed", game_id="gomoku") == 0


def test_recover_orphan_matches(tmp_path):
    """服务重启后清理孤儿 running 对局：标 aborted，返回受影响数。"""
    s = _store(tmp_path)
    u = s.create_user("gina", "g@ex.com", hash_password("password1"))
    a = s.create_bot(owner_id=u["id"], name="bot_a")
    b = s.create_bot(owner_id=u["id"], name="bot_b")
    # 3 场：pending / running / completed
    s.create_match("m_pending", a["id"], b["id"], owner_id=u["id"])
    s.create_match("m_running", a["id"], b["id"], owner_id=u["id"])
    s.create_match("m_done", a["id"], b["id"], owner_id=u["id"])
    s.update_match("m_running", status="running")
    s.update_match("m_done", status="completed", winner=0)

    # 重启清理：只有 running 被标 aborted
    recovered = s.recover_orphan_matches()
    assert recovered == 1
    assert s.get_match("m_running")["status"] == "aborted"
    assert s.get_match("m_running")["reason"] == "orphan_after_restart"
    assert s.get_match("m_running")["ended_at"] is not None
    # 其它状态不动
    assert s.get_match("m_pending")["status"] == "pending"
    assert s.get_match("m_done")["status"] == "completed"

    # 幂等：再清理一次返回 0（已无 running）
    assert s.recover_orphan_matches() == 0


def test_contests_entries_pairings(tmp_path):
    s = _store(tmp_path)
    org = s.create_user("org1", "o@ex.com", hash_password("password1"), role="organizer")
    u = s.create_user("player", "p@ex.com", hash_password("password1"))
    bot = s.create_bot(owner_id=u["id"], name="entrybot")
    c = s.create_contest("Cup", org["id"], description="d")
    assert c["status"] == "draft"
    s.update_contest(c["id"], status="open")
    assert s.get_contest(c["id"])["status"] == "open"
    assert len(s.list_contests(status="open")) == 1

    entry = s.add_entry(c["id"], u["id"], bot["id"])
    assert entry["bot_id"] == bot["id"]
    assert s.get_entry(c["id"], u["id"])["id"] == entry["id"]
    assert len(s.list_entries(c["id"])) == 1

    bot2 = s.create_bot(owner_id=org["id"], name="opp")
    pairing = s.add_pairing(c["id"], bot["id"], bot2["id"], round_num=1)
    s.update_pairing(pairing["id"], status="done", match_id=None)
    assert len(s.list_pairings(c["id"])) == 1
