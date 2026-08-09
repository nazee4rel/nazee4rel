"""Delivery channels for alerts and reports.

The dashboard channel always succeeds, because the alert row *is* the delivery —
it is written before anything is sent, so a misconfigured mail server loses a
notification, never the alert itself.

Email is deliberately plain: `smtplib` from the standard library, run in a
thread so the async workers are not blocked. No new dependency, no HTML
templating engine, and no images or tracking pixels. It is also **off unless
configured**, and an unconfigured channel records `SKIPPED` rather than
`FAILED` — nothing has gone wrong when you simply have not set up SMTP.

Two content rules apply to everything that leaves this process:

* Never include a credential, a token, or an API key. Alert bodies are composed
  from the rule registry and from numbers; nothing from the token store can
  reach them.
* Recipients are stored redacted, so the delivery log does not become a copy of
  the address book.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.alerting import DeliveryChannel, DeliveryStatus

log = get_logger(__name__)

SEND_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    target: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Message:
    subject: str
    text: str


class Channel(Protocol):
    channel: DeliveryChannel

    @property
    def is_configured(self) -> bool: ...

    async def send(self, message: Message) -> DeliveryResult: ...


def redact_address(address: str) -> str:
    """`owner@example.com` -> `o***@example.com`.

    Enough to confirm which recipient was used without the delivery log
    becoming a list of addresses.
    """
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    head = local[0] if local else ""
    return f"{head}***@{domain}"


class DashboardChannel:
    """A no-op sender. The stored row is the delivery."""

    channel = DeliveryChannel.DASHBOARD

    @property
    def is_configured(self) -> bool:
        return True

    async def send(self, message: Message) -> DeliveryResult:
        return DeliveryResult(status=DeliveryStatus.SENT, target="dashboard")


class EmailChannel:
    """SMTP, from the standard library, off unless configured."""

    channel = DeliveryChannel.EMAIL

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.alert_email_from
            and self.settings.alert_email_to
        )

    async def send(self, message: Message) -> DeliveryResult:
        if not self.is_configured:
            return DeliveryResult(
                status=DeliveryStatus.SKIPPED,
                error="SMTP is not configured. Alerts are still recorded in the dashboard.",
            )

        recipient = self.settings.alert_email_to
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._send_sync, message),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                target=redact_address(recipient),
                error=f"The SMTP server did not respond within {SEND_TIMEOUT_SECONDS:.0f}s.",
            )
        except Exception as exc:  # noqa: BLE001 — smtplib raises a wide family
            # The exception text can contain the server banner but never our
            # credentials; smtplib does not echo them back.
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                target=redact_address(recipient),
                error=f"{type(exc).__name__}: {exc}"[:500],
            )

        log.info("alerts.email_sent", target=redact_address(recipient))
        return DeliveryResult(status=DeliveryStatus.SENT, target=redact_address(recipient))

    def _send_sync(self, message: Message) -> None:
        settings = self.settings
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = settings.alert_email_from
        email["To"] = settings.alert_email_to
        email.set_content(message.text)

        if settings.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=SEND_TIMEOUT_SECONDS
            )
        else:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=SEND_TIMEOUT_SECONDS
            )
        with server:
            if settings.smtp_use_starttls and not settings.smtp_use_ssl:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(email)


def build_channels(names: list[str] | None = None) -> list[Channel]:
    """Resolve configured channel names to channel objects.

    The dashboard is always included, whatever the configuration says. An alert
    that exists nowhere is not an alert.
    """
    available: dict[str, Channel] = {
        DeliveryChannel.DASHBOARD.value: DashboardChannel(),
        DeliveryChannel.EMAIL.value: EmailChannel(),
    }
    if not names:
        return [available[DeliveryChannel.DASHBOARD.value]]

    selected = [available[name] for name in names if name in available]
    if not any(c.channel is DeliveryChannel.DASHBOARD for c in selected):
        selected.insert(0, available[DeliveryChannel.DASHBOARD.value])
    return selected
