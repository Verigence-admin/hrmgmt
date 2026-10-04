"""Outgoing email through SMTP (Gmail with an app password). One attempt per message, no retries:
a failure is reported and HR decides when to send again. The message body is never logged."""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class MailError(RuntimeError):
    """The message could not be sent. `code` is a short, safe reason for HR."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Mailer(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


def body_to_html(body: str) -> str:
    """The plain-text body as simple HTML: escaped, paragraphs kept, links not auto-created."""
    paragraphs = [p for p in body.replace("\r\n", "\n").split("\n\n")]
    return "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs if p.strip()
    )


class SmtpMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_address: str | None = None,
        from_name: str = "Verigence HR",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not host or not user or not password:
            raise ValueError("SMTP host, user and password are required")
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._from = formataddr((from_name, from_address or user))
        self._timeout = timeout_seconds

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        message.add_alternative(body_to_html(body), subtype="html")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(self._user, self._password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            logger.warning("hr_mail_failed", reason="auth")
            raise MailError("MAIL_AUTH_FAILED", "The mail account refused the sign-in") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            logger.warning("hr_mail_failed", reason="recipient")
            raise MailError("MAIL_RECIPIENT_REFUSED", "The address was refused") from exc
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("hr_mail_failed", reason=type(exc).__name__)
            raise MailError("MAIL_UNAVAILABLE", "The mail server could not be reached") from exc
