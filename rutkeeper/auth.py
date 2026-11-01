"""Accounts for the dashboard: password hashes, session tokens, and lockout."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

SESSION_COOKIE = "tua_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_FAILURES = 8
LOCK_SECONDS = 15 * 60

# scrypt work factor: about 16 MB and well under a second on a Raspberry Pi.
_N, _R, _P, _LEN = 2**14, 8, 1, 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_LEN)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${_b64(salt)}${_b64(_scrypt(password, salt))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = _scrypt(password, _unb64(salt))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked against unknown emails so a wrong address takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_login(password: str, stored: str | None) -> bool:
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Use at least 10 characters."
    if len(password) > 200:
        return "That password is too long."
    return None


def normalize_email(value: str) -> str | None:
    email = value.strip().lower()
    if len(email) < 3 or len(email) > 200:
        return None
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return None
    return email


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def safe_next(value: str | None) -> str:
    """Only same-site paths, so the login page cannot bounce someone to another host."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if value.startswith("/login") or value.startswith("/logout"):
        return "/"
    return value
