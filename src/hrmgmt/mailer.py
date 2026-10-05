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
    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        images: dict[str, bytes] | None = None,
    ) -> None: ...


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
        from_name: str = "Verigence",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not host or not user or not password:
            raise ValueError("SMTP host, user and password are required")
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._from = formataddr((from_name, from_address or user))
        self._timeout = timeout_seconds

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        images: dict[str, bytes] | None = None,
    ) -> None:
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        message.add_alternative(html_body if html_body else body_to_html(body), subtype="html")
        if html_body and images:
            # The pictures ride inside the email (cid:), so they show where web pictures are blocked.
            page = message.get_body(("html",))
            for cid, data in images.items():
                page.add_related(data, "image", "png", cid=f"<{cid}>")  # type: ignore[union-attr]
        stage = "connect"
        try:
            server: smtplib.SMTP
            if self._port == 465:
                # Port 465 is encrypted from the first byte; every other port upgrades with STARTTLS.
                server = smtplib.SMTP_SSL(
                    self._host,
                    self._port,
                    timeout=self._timeout,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            with server:
                if self._port != 465:
                    stage = "starttls"
                    server.starttls(context=ssl.create_default_context())
                stage = "hello"
                server.ehlo()
                stage = "login"
                server.login(self._user, self._password)
                stage = "send"
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            logger.warning("hr_mail_failed", reason="auth")
            raise MailError("MAIL_AUTH_FAILED", "The mail account refused the sign-in") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            logger.warning("hr_mail_failed", reason="recipient")
            raise MailError("MAIL_RECIPIENT_REFUSED", "The address was refused") from exc
        except (smtplib.SMTPException, OSError) as exc:
            # What went wrong and where, so a network block can be told from a mail-account problem.
            # The message text of these errors never carries the password.
            logger.warning(
                "hr_mail_failed",
                reason=type(exc).__name__,
                stage=stage,
                errno=getattr(exc, "errno", None),
                detail=str(exc)[:160],
                host=self._host,
                port=self._port,
            )
            raise MailError("MAIL_UNAVAILABLE", "The mail server could not be reached") from exc
