"""通知管理器：写站内通知 + 按用户偏好可选发邮件。

邮件复用 Mailer（main.py 注入）；落库 email_outbox 记录由调用方/Mailer 负责。
本模块只做「写 notifications 表 + 按 prefs 触发邮件」。
"""
from __future__ import annotations

import logging
from typing import Any

from bzplat.backend.store import Store

logger = logging.getLogger(__name__)

# 通知类型 → prefs 字段映射（决定是否发邮件）
_TYPE_TO_PREF = {
    "match_done": "email_match_done",
    "followed": "email_followed",
    "contest": "email_contest",
    "comment": "email_comment",
}


class NotificationManager:
    def __init__(self, store: Store, mailer: Any = None) -> None:
        self.store = store
        self.mailer = mailer

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
        """写一条站内通知；send_email=True 时按用户 prefs 决定是否发邮件。

        返回新建的 notification dict（用户不存在则 None）。
        """
        user = self.store.get_user(user_id)
        if not user:
            return None
        notif = self.store.add_notification(
            user_id, type=type, title=title, body=body, link=link
        )
        if send_email and self.mailer is not None and self.mailer.config.configured:
            pref_key = _TYPE_TO_PREF.get(type)
            should_email = True
            if pref_key:
                prefs = self.store.get_notification_prefs(user_id)
                should_email = bool(prefs.get(pref_key, 0))
            if should_email:
                try:
                    self.mailer.send(
                        user["email"],
                        f"【通知】{title}" if title else "【通知】",
                        body_text=body or title,
                    )
                except Exception as e:  # noqa: BLE001 - 邮件失败不阻断通知
                    logger.warning("notify email failed user=%s type=%s: %s", user_id, type, e)
        return notif

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
    ) -> None:
        """对局完成等场景：通知双方 Bot 的 owner（去重）。"""
        owner_ids: set[int] = set()
        for bid in (bot_a_id, bot_b_id):
            b = self.store.get_bot(bid)
            if b and b.get("owner_id"):
                owner_ids.add(int(b["owner_id"]))
        for uid in owner_ids:
            self.notify(
                uid, type=type, title=title, body=body, link=link, send_email=send_email
            )
