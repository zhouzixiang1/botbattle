"""Single-process asynchronous delivery and broadcast worker."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
from typing import Any

from bzplat.backend.mail import Mailer

from .repository import CommunicationRepository
from .utils import deterministic_message_id

logger = logging.getLogger(__name__)


def _error_code(exc: BaseException) -> str:
    """Classify without persisting/logging provider text, addresses or payloads."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_auth"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "smtp_timeout"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp_error"
    if isinstance(exc, OSError):
        return "smtp_network"
    if isinstance(exc, RuntimeError):
        return "smtp_not_configured"
    return "smtp_unexpected"


class DeliveryWorker:
    """Lightweight resumable worker for one FastAPI process.

    A database claim prevents concurrent in-process sends.  SMTP has an unavoidable
    crash window after the remote server accepted a message but before ``sent`` was
    committed, so semantics are explicitly at-least-once, bounded by ``max_attempts``.
    A deterministic Message-ID/idempotency key lets cooperative providers deduplicate.
    """

    def __init__(
        self,
        repository: CommunicationRepository,
        mailer: Mailer,
        *,
        interval_sec: float = 1.0,
        broadcast_batch_size: int = 50,
    ) -> None:
        self.repository = repository
        self.mailer = mailer
        self.interval_sec = max(0.05, float(interval_sec))
        self.broadcast_batch_size = max(1, min(int(broadcast_batch_size), 500))

    async def loop(self) -> None:
        while True:
            try:
                recovered = await asyncio.to_thread(self.repository.recover_inflight)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry transient DB startup faults
                logger.error(
                    "communications worker recovery failed code=%s",
                    type(exc).__name__,
                )
                await asyncio.sleep(self.interval_sec)
        if any(recovered.values()):
            logger.warning(
                "communications worker recovered deliveries=%d recipients=%d",
                recovered["deliveries"],
                recovered["broadcast_recipients"],
            )
        while True:
            try:
                work = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the lifecycle worker alive
                logger.error(
                    "communications worker cycle failed code=%s",
                    type(exc).__name__,
                )
                work = 0
            if work == 0:
                await asyncio.sleep(self.interval_sec)

    async def run_once(self) -> int:
        work = 0
        batch = await asyncio.to_thread(
            self.repository.claim_broadcast_batch,
            batch_size=self.broadcast_batch_size,
        )
        if batch:
            broadcast = batch["broadcast"]
            for recipient in batch["recipients"]:
                try:
                    await asyncio.to_thread(
                        self.repository.process_broadcast_recipient,
                        broadcast,
                        recipient,
                    )
                except Exception:  # noqa: BLE001 - isolate one fixed recipient
                    state = await asyncio.to_thread(
                        self.repository.retry_broadcast_recipient,
                        recipient["public_id"],
                        error_code="projection_error",
                    )
                    logger.warning(
                        "broadcast recipient projection failed broadcast=%s recipient=%s state=%s",
                        broadcast["public_id"],
                        recipient["public_id"],
                        state,
                    )
                work += 1
        delivery = await asyncio.to_thread(self.repository.claim_delivery)
        if delivery:
            await self._deliver(delivery)
            work += 1
        return work

    async def _deliver(self, delivery: dict[str, Any]) -> None:
        try:
            content = await asyncio.to_thread(
                self.repository.resolve_delivery_content, delivery
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - template/content boundary is retryable
            status = await asyncio.to_thread(
                self.repository.mark_delivery_failed_or_retry,
                delivery["public_id"],
                error_code="content_unavailable",
            )
            logger.warning(
                "communications delivery content unavailable delivery=%s state=%s",
                delivery["public_id"],
                status,
            )
            return
        if content is None:
            await asyncio.to_thread(
                self.repository.mark_delivery_cancelled, delivery["public_id"]
            )
            return
        subject, body_text, body_html = content
        message_id = deterministic_message_id(str(delivery["idempotency_key"]))
        try:
            if not self.mailer.config.configured:
                raise RuntimeError("smtp_not_configured")
            await asyncio.to_thread(
                self.mailer.send,
                str(delivery["address_snapshot"]),
                subject,
                body_text=body_text,
                body_html=body_html,
                message_id=message_id,
            )
        except asyncio.CancelledError:
            # Leave status=sending; startup recovery returns it to queued.  This is the
            # explicit at-least-once crash/cancellation window.
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            code = _error_code(exc)
            status = await asyncio.to_thread(
                self.repository.mark_delivery_failed_or_retry,
                delivery["public_id"],
                error_code=code,
            )
            logger.warning(
                "communications delivery attempt failed delivery=%s code=%s state=%s",
                delivery["public_id"],
                code,
                status,
            )
            return
        await asyncio.to_thread(
            self.repository.mark_delivery_sent,
            delivery["public_id"],
            provider_message_id=message_id,
        )
