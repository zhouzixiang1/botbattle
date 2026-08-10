"""Legacy notification facade backed by the communications truth model."""
from __future__ import annotations

from typing import Any

from bzplat.backend.communications.service import CommunicationService
from bzplat.backend.store import Store


class NotificationManager:
    """Keep business call sites stable while routing every new write centrally.

    ``mailer`` is accepted only for source compatibility and is never called.  SMTP is
    exclusively owned by the lifespan delivery worker.
    """

    def __init__(
        self,
        store: Store,
        mailer: Any = None,
        *,
        communications: CommunicationService | None = None,
    ) -> None:
        self.store = store
        self.communications = communications or CommunicationService(store)

    def notify(
        self,
        user_id: int,
        *,
        type: str = "",
        title: str = "",
        body: str = "",
        link: str = "",
        send_email: bool = False,
    ) -> dict | None:
        return self.communications.notify_user(
            user_id,
            type=type,
            title=title,
            body=body,
            link=link,
            send_email=send_email,
        )

    def notify_both_owners(
        self,
        bot_a_id: int,
        bot_b_id: int,
        *,
        type: str = "match_done",
        title: str = "",
        body: str = "",
        link: str = "",
        send_email: bool = False,
        exclude_user_ids: set[int] | None = None,
    ) -> None:
        owner_ids: set[int] = set()
        for bot_id in (bot_a_id, bot_b_id):
            bot = self.store.get_bot(bot_id)
            if bot and bot.get("owner_id"):
                owner_ids.add(int(bot["owner_id"]))
        owner_ids.difference_update(exclude_user_ids or set())
        for user_id in owner_ids:
            self.notify(
                user_id,
                type=type,
                title=title,
                body=body,
                link=link,
                send_email=send_email,
            )
