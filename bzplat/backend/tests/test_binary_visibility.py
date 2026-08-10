"""Canonical executable target visibility and activation gates."""
from __future__ import annotations

import struct
from pathlib import Path

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


SAMPLES = Path(__file__).resolve().parents[3] / "samples"
ELF = SAMPLES / "callbot_linux_amd64"


def _pe_amd64() -> bytes:
    data = bytearray(0x80)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, 0x8664)
    return bytes(data)


def _auth(app, username: str, password: str = "pw123456") -> dict[str, str]:
    _, token = app.state.auth.authenticate(username, password)
    return {"Authorization": f"Bearer {token}"}


def test_blank_legacy_metadata_is_excluded_from_every_public_selector(tmp_path):
    """覆盖旧主库的 elf/空/空 + version unknown/空/空 真实形态。"""
    store = Store(str(tmp_path / "blank-legacy.db"))
    owner = store.create_user("blanklegacy", "blanklegacy@example.com", "hash")
    bot = store.create_bot(
        owner["id"],
        "blank_legacy_bot",
        display_name="Blank Legacy",
        binary_path=str(ELF),
        game_id="holdem",
    )
    store.add_bot_version(bot["id"], binary_path=str(ELF), version=1)
    store.ensure_rating(bot["id"], game_id="holdem")

    store._conn.execute("PRAGMA ignore_check_constraints=ON")
    store._conn.execute(
        "UPDATE bots SET format='elf', os='', arch='', is_active=1 WHERE id=?",
        (bot["id"],),
    )
    store._conn.execute(
        "UPDATE bot_versions SET format='unknown', os='', arch='' WHERE bot_id=?",
        (bot["id"],),
    )
    store._conn.execute("PRAGMA ignore_check_constraints=OFF")
    store._conn.commit()

    assert bot["id"] not in {
        row["id"] for row in store.list_bots(runnable_only=True)
    }
    assert bot["id"] not in {row["id"] for row in store.search_bots("blank")}
    assert bot["id"] not in {
        row["bot_id"]
        for row in store.list_leaderboard(game_id="holdem")["items"]
    }
    assert bot["id"] not in {
        row["bot_id"] for row in store.least_recently_played("holdem")
    }
    store.close()


def test_legacy_pe_is_owner_visible_but_never_public_or_reactivated(tmp_path):
    """Historical rows remain auditable; every current selection path fails closed."""
    app = create_app(db_path=str(tmp_path / "visibility.db"))
    store = app.state.store
    owner = store.create_user(
        "legacyowner", "legacyowner@example.com", hash_password("pw123456")
    )
    other = store.create_user(
        "legacyother", "legacyother@example.com", hash_password("pw123456")
    )
    admin = store.create_user(
        "legacyadmin",
        "legacyadmin@example.com",
        hash_password("pw123456"),
        role="admin",
    )
    for user in (owner, other, admin):
        store.update_user(user["id"], email_verified=1, is_active=1)

    pe_path = tmp_path / "legacy.exe"
    pe_path.write_bytes(_pe_amd64())
    bot = store.create_bot(
        owner["id"],
        "legacy_pe_bot",
        display_name="Legacy PE",
        binary_path=str(ELF),
        game_id="holdem",
    )
    store.add_bot_version(bot["id"], binary_path=str(ELF), version=1)
    store.add_bot_version(bot["id"], binary_path=str(pe_path), version=2)

    # Reproduce a row produced by the retired permissive schema. Opening or
    # reading it must not rewrite history, while all executable paths reject it.
    store._conn.execute("PRAGMA ignore_check_constraints=ON")
    store._conn.execute(
        "UPDATE bot_versions SET format='pe', os='windows', arch='amd64' "
        "WHERE bot_id=? AND version=2",
        (bot["id"],),
    )
    store._conn.execute(
        "UPDATE bots SET current_version=2, binary_path=?, format='pe', "
        "os='windows', arch='amd64', is_active=1 WHERE id=?",
        (str(pe_path), bot["id"]),
    )
    store._conn.execute("PRAGMA ignore_check_constraints=OFF")
    store._conn.commit()
    store.ensure_rating(bot["id"], game_id="holdem")

    # Public ranking and the auto-match scheduler are executable selection
    # surfaces too.  A historical PE must not appear in either, even when its
    # stale row is still active and has a rating.
    assert bot["id"] not in {
        row["bot_id"]
        for row in store.list_leaderboard(game_id="holdem")["items"]
    }
    assert bot["id"] not in {
        row["bot_id"] for row in store.least_recently_played("holdem")
    }

    with TestClient(app) as client:
        owner_headers = _auth(app, "legacyowner")
        other_headers = _auth(app, "legacyother")
        admin_headers = _auth(app, "legacyadmin")

        mine = client.get("/api/bots/mine", headers=owner_headers)
        assert mine.status_code == 200
        own_bot = next(b for b in mine.json()["bots"] if b["id"] == bot["id"])
        assert own_bot["runnable"] is False
        assert "Linux x86_64 ELF64" in own_bot["unsupported_reason"]

        public = client.get("/api/bots/public?game_id=holdem")
        assert public.status_code == 200
        assert bot["id"] not in {b["id"] for b in public.json()["bots"]}

        profile_bots = client.get(f"/api/users/{owner['username']}/bots")
        assert profile_bots.status_code == 200
        assert bot["id"] not in {b["id"] for b in profile_bots.json()["bots"]}

        search = client.get("/api/search?q=legacy_pe&type=bots")
        assert search.status_code == 200
        assert bot["id"] not in {b["id"] for b in search.json()["bots"]}

        owner_versions = client.get(
            f"/api/bots/{bot['id']}/versions", headers=owner_headers
        )
        by_version = {v["version"]: v for v in owner_versions.json()["versions"]}
        assert by_version[1]["runnable"] is True
        assert by_version[2]["runnable"] is False

        public_versions = client.get(
            f"/api/bots/{bot['id']}/versions", headers=other_headers
        )
        assert [v["version"] for v in public_versions.json()["versions"]] == [1]

        admin_versions = client.get(
            f"/api/admin/bots/{bot['id']}/versions", headers=admin_headers
        )
        assert admin_versions.status_code == 200
        assert {
            v["version"]: v["runnable"]
            for v in admin_versions.json()["versions"]
        } == {1: True, 2: False}

        attempts = (
            client.post(
                f"/api/bots/{bot['id']}/active?active=true",
                headers=owner_headers,
            ),
            client.patch(
                f"/api/bots/{bot['id']}",
                json={"is_active": True},
                headers=owner_headers,
            ),
            client.post(
                f"/api/bots/{bot['id']}/versions/2/activate",
                headers=owner_headers,
            ),
            client.patch(
                f"/api/admin/bots/{bot['id']}",
                json={"is_active": True},
                headers=admin_headers,
            ),
        )
        for response in attempts:
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "unsupported_binary"

        assert client.patch(
            f"/api/bots/{bot['id']}",
            json={"is_active": "true"},
            headers=owner_headers,
        ).status_code == 422
        assert client.patch(
            f"/api/admin/bots/{bot['id']}",
            json={"format": "elf"},
            headers=admin_headers,
        ).status_code == 422

    unchanged = store.get_bot(bot["id"])
    assert (unchanged["format"], unchanged["os"], unchanged["arch"]) == (
        "pe", "windows", "amd64"
    )
    assert unchanged["current_version"] == 2
