"""The designed (HTML) version of the Welcome email: the login, one big button, and the steps with
pictures of the screens. Everything that comes from outside (name, sign-in ID, password, links) is
escaped, so nothing a person's name contains can change the page. The pictures travel inside the
email, so they show even where pictures from the web are blocked."""

from __future__ import annotations

import html
from pathlib import Path

_ASSETS = Path(__file__).parent / "email_assets"

# content id -> file. The id is what the page refers to as cid:<id>.
IMAGES: dict[str, str] = {
    "verigence-signin": "signin.png",
    "verigence-download": "download.png",
    "verigence-forgot-link": "forgot_link.png",
    "verigence-send-code": "send_code.png",
    "verigence-new-password": "new_password.png",
}

_NAVY = "#0b3b6b"
_TEAL = "#0e9f8e"


def inline_images() -> dict[str, bytes]:
    return {cid: (_ASSETS / name).read_bytes() for cid, name in IMAGES.items()}


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _step(number: str, title: str, text: str, image: tuple[str, str] | None = None) -> str:
    picture = ""
    if image:
        cid, alt = image
        picture = (
            '<div style="text-align:center;margin:12px 0 4px">'
            f'<img src="cid:{cid}" alt="{_e(alt)}" width="260" '
            'style="width:260px;max-width:100%;height:auto;border:1px solid #d5dde5;border-radius:10px">'
            "</div>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 18px"><tr>'
        f'<td width="38" valign="top"><div style="width:30px;height:30px;line-height:30px;'
        f'border-radius:15px;background:{_TEAL};color:#fff;text-align:center;font:700 16px Arial,sans-serif">'
        f"{_e(number)}</div></td>"
        '<td valign="top" style="font:16px/1.5 Arial,sans-serif;color:#18212b">'
        f'<div style="font-weight:700;font-size:17px">{_e(title)}</div>'
        f"<div>{text}</div>{picture}</td></tr></table>"
    )


def welcome_html(values: dict[str, str]) -> str:
    name = _e(values.get("name") or "there")
    company = _e(values.get("company") or "Verigence")
    login_id = _e(values.get("login_id"))
    password = _e(values.get("temp_password"))
    link = _e(values.get("app_link"))
    steps = "".join(
        [
            _step(
                "1",
                "Open the link and sign in",
                "Tap the big blue button above. Type your Sign-in ID and password from the box "
                "above, then tap <b>Sign in</b>.",
                ("verigence-signin", "The sign-in screen with the Sign in button marked"),
            ),
            _step(
                "2",
                "Download the app",
                "After you sign in you will see this page. Tap <b>Download APK</b>. Open the file "
                "when it has downloaded. If the phone asks, tap <b>Settings</b>, allow it, then tap "
                "<b>Install</b>. A warning from Android is normal.",
                ("verigence-download", "The download page with the Download APK button marked"),
            ),
            _step(
                "3",
                "Mark your attendance every working day",
                "Open the app and tap <b>My HR</b> at the bottom. When you reach work tap "
                "<b>Check in</b>. When you leave tap <b>Check out</b>. Allow <b>Camera</b> and "
                "<b>Location</b> when the app asks.",
            ),
        ]
    )
    reset = "".join(
        [
            _step(
                "a",
                'Tap "Forgot password?"',
                "It is on the sign-in page, on the right, just below the password box.",
                ("verigence-forgot-link", "The sign-in screen with Forgot password marked"),
            ),
            _step(
                "b",
                "Ask for a code",
                "Type your email address (the same as your Sign-in ID). Tap "
                "<b>Send verification code</b>.",
                (
                    "verigence-send-code",
                    "The Forgot password screen with Send verification code marked",
                ),
            ),
            _step(
                "c",
                "Open your email",
                "Verigence sends you a 6-digit code. If it has not come, look in your Spam folder, "
                "or tap <b>Resend code</b>.",
            ),
            _step(
                "d",
                "Type the code and your new password",
                "Type the 6-digit code. Then type your new password (at least 8 characters) two "
                "times. Tap <b>Reset password</b>.",
                ("verigence-new-password", "The reset screen with Reset password marked"),
            ),
            _step(
                "e",
                "Sign in again",
                'You will see "Password reset complete". Tap <b>Back to sign in</b> and sign in '
                "with your new password.",
            ),
        ]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef3f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f7"><tr><td align="center" style="padding:16px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:12px;overflow:hidden">
<tr><td style="background:{_NAVY};padding:18px 22px;font:700 24px Arial,sans-serif;color:#fff">{company}</td></tr>
<tr><td style="padding:22px;font:16px/1.5 Arial,sans-serif;color:#18212b">
<p style="margin:0 0 14px">Dear {name},</p>
<p style="margin:0 0 18px">Your Verigence login is ready. Please follow the 3 steps below.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f8fb;border:1px solid #d5dde5;border-radius:10px;margin:0 0 18px"><tr><td style="padding:14px 16px;font:16px/1.7 Arial,sans-serif">
<div style="color:#52606d;font-size:13px">Sign-in ID</div>
<div style="font:700 17px Arial,sans-serif;word-break:break-all">{login_id}</div>
<div style="color:#52606d;font-size:13px;margin-top:8px">Temporary password</div>
<div style="font:700 20px 'Courier New',monospace;letter-spacing:1px">{password}</div>
</td></tr></table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 6px"><tr><td align="center">
<a href="{link}" style="display:block;background:#1a56db;color:#fff;text-decoration:none;font:700 18px Arial,sans-serif;padding:16px;border-radius:10px">Open Verigence</a>
</td></tr></table>
<p style="margin:0 0 22px;font-size:13px;color:#52606d;text-align:center;word-break:break-all">If the button does not work, copy this link into your phone's browser:<br>{link}</p>
{steps}
<p style="margin:6px 0 22px;padding:12px 14px;background:#fff6e5;border-radius:8px;font-size:15px">Please do not share your password with anyone.</p>
<div style="border-top:1px solid #d5dde5;padding-top:18px;margin-top:6px">
<div style="font:700 19px Arial,sans-serif;margin-bottom:6px">Want your own password? Or forgot it?</div>
<p style="margin:0 0 14px">Follow these steps. You can do this any time.</p>
{reset}
</div>
<p style="margin:10px 0 0">If you cannot sign in, reply to this email or speak to HR.</p>
<p style="margin:18px 0 0">Thank you,<br>{company}</p>
</td></tr></table>
</td></tr></table>
</body></html>"""
