"""Message templates (channel-neutral) and how a message is filled in.

A template is plain text with {{placeholders}}; only the listed ones are allowed, so a typo cannot
send a half-filled message. To add a template later, add one entry to DEFAULTS, PLACEHOLDERS and
REQUIRED below. GENERAL is a note HR writes to employees; WELCOME carries a person's sign-in ID
and a temporary password."""

from __future__ import annotations

import re
from typing import Any

GENERAL = "GENERAL"
WELCOME = "WELCOME"
CODES = (GENERAL, WELCOME)

_COMMON = ("name", "company", "sign_in_link", "app_link")
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    GENERAL: _COMMON,
    WELCOME: (*_COMMON, "login_id", "temp_password"),
}
# A welcome message without these two is useless: it would not carry the login.
REQUIRED: dict[str, tuple[str, ...]] = {GENERAL: (), WELCOME: ("login_id", "temp_password")}

DEFAULTS: dict[str, dict[str, str]] = {
    GENERAL: {
        "name": "General message to employees",
        "subject": "A message from {{company}} HR",
        "body": ("Dear {{name}},\n\nWrite your message here.\n\nRegards,\nHR, {{company}}"),
    },
    WELCOME: {
        "name": "Welcome: your Verigence login",
        "subject": "Your Verigence login is ready",
        "body": (
            "Dear {{name}},\n\n"
            "Your Verigence login has been created. You can now sign in and download the "
            "Verigence mobile app. The app can also be used to mark your attendance, in addition "
            "to the process you follow today.\n\n"
            "Sign-in ID: {{login_id}}\n"
            "Temporary password: {{temp_password}}\n\n"
            "1. Sign in here: {{sign_in_link}}\n"
            "2. After signing in, download the mobile app here: {{app_link}}\n\n"
            "Please keep this password private and do not forward this email. If you want a "
            'password of your own, use "Forgot password" on the sign-in page. If you cannot '
            "sign in, reply to this email or speak to HR.\n\n"
            "Regards,\nHR, {{company}}"
        ),
    },
}

_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


class TemplateError(ValueError):
    """The template text is not acceptable. The message is safe to show HR."""


def used_placeholders(text: str) -> set[str]:
    return set(_TOKEN.findall(text))


def validate(code: str, subject: str, body: str) -> None:
    if code not in PLACEHOLDERS:
        raise TemplateError("Unknown template.")
    if not subject.strip() or not body.strip():
        raise TemplateError("The subject and the message cannot be empty.")
    allowed = set(PLACEHOLDERS[code])
    unknown = (used_placeholders(subject) | used_placeholders(body)) - allowed
    if unknown:
        raise TemplateError(
            "These placeholders are not available here: "
            + ", ".join("{{" + u + "}}" for u in sorted(unknown))
            + "."
        )
    if "temp_password" in used_placeholders(subject):
        raise TemplateError("The password cannot be placed in the subject line.")
    missing = [r for r in REQUIRED[code] if r not in used_placeholders(body)]
    if missing:
        raise TemplateError(
            "The message must include " + ", ".join("{{" + m + "}}" for m in missing) + "."
        )


def render(text: str, values: dict[str, Any]) -> str:
    return _TOKEN.sub(lambda m: str(values.get(m.group(1), "")), text)
