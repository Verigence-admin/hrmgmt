from __future__ import annotations

import re

_PAN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL.match(email) or len(email) > 320:
        raise ValueError("Enter a valid email address")
    return email


def clean_pan(value: str) -> str:
    pan = value.strip().upper()
    if not _PAN.match(pan):
        raise ValueError("PAN must be 5 letters, 4 digits, 1 letter (for example ABCDE1234F)")
    return pan


def clean_aadhaar(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) != 12 or len(digits) != len("".join(value.split())):
        raise ValueError("Aadhaar must be exactly 12 digits")
    return digits


def clean_indian_mobile(value: str) -> str:
    """Return the 10 digits of an Indian mobile number, or raise."""
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise ValueError("Enter a valid 10-digit Indian mobile number")
    return digits


def mask_pan(pan: str | None) -> str | None:
    return None if not pan else "XXXXX" + pan[5:]


def mask_aadhaar(aadhaar: str | None) -> str | None:
    return None if not aadhaar else "XXXX XXXX " + aadhaar[-4:]


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")
