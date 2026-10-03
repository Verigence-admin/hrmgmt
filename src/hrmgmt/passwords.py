from __future__ import annotations

import secrets
import string

_UPPER = string.ascii_uppercase.replace("O", "").replace("I", "")
_LOWER = string.ascii_lowercase.replace("l", "")
_DIGITS = string.digits.replace("0", "").replace("1", "")
_SYMBOLS = "@#%+=!"


def generate_initial_password(length: int = 16) -> str:
    """A random first password: at least one upper, lower, digit and symbol, with look-alike
    characters left out so it can be read out or typed from a message without mistakes.
    Shown to HR once at creation; never stored, logged or written to the audit log."""
    if length < 12:
        raise ValueError("initial password must be at least 12 characters")
    pools = (_UPPER, _LOWER, _DIGITS, _SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
