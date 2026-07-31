"""Authentication."""

import hashlib
import hmac

from . import db
from .config import Config


class AuthError(Exception):
    pass


def sign(payload: str) -> str:
    return hmac.new(Config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_session_token(token: str) -> dict:
    """Validate a session token of the form ``<user_id>.<signature>``."""
    try:
        user_id, signature = token.rsplit(".", 1)
    except ValueError:
        raise AuthError("malformed token")

    expected = sign(user_id)
    # Constant-time comparison, so an attacker cannot recover the signature
    # one byte at a time by measuring how long the comparison takes.
    if not hmac.compare_digest(expected, signature):
        raise AuthError("bad signature")

    rows = db.query("SELECT id, email, is_admin FROM users WHERE id = ?", (user_id,))
    if not rows:
        raise AuthError("unknown user")
    return dict(rows[0])


def require_user(token: str) -> dict:
    if not token:
        raise AuthError("missing token")
    return verify_session_token(token)
