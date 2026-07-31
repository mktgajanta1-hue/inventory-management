"""
MediaTrack auth — stdlib only (no new dependencies).

Passwords : PBKDF2-HMAC-SHA256, 200k iterations, per-user salt.
Sessions  : HMAC-signed tokens in an HttpOnly cookie. The signing secret comes
            from the SECRET_KEY environment variable. If it is not set (local
            dev / packaged .exe), a key is generated once and stored next to the
            application so sessions survive restarts.

            In production SECRET_KEY must be set: containers have no persistent
            filesystem, so a generated key would change on every deploy and log
            everyone out.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path


def data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


SECRET_FILE = data_dir() / "secret.key"


def get_secret() -> bytes:
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    # Fallback for local dev and the packaged .exe.
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(os.urandom(32))
    return SECRET_FILE.read_bytes()


def hash_pw(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex(), dk.hex()


def verify_pw(password: str, salt_hex: str, hash_hex: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(dk.hex(), hash_hex)


def make_token(user_id: int, version: int = 1, days: int = 30) -> str:
    payload = json.dumps(
        {"u": user_id, "v": version, "e": int(time.time()) + days * 86400}
    ).encode()
    sig = hmac.new(get_secret(), payload, "sha256").digest()
    return (
        base64.urlsafe_b64encode(payload).decode()
        + "."
        + base64.urlsafe_b64encode(sig).decode()
    )


def read_token(token: str) -> tuple[int, int] | None:
    """Returns (user_id, token_version) or None."""
    try:
        p64, s64 = token.split(".")
        payload = base64.urlsafe_b64decode(p64)
        sig = base64.urlsafe_b64decode(s64)
        expected = hmac.new(get_secret(), payload, "sha256").digest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if data.get("e", 0) < time.time():
            return None
        return int(data["u"]), int(data.get("v", 1))
    except Exception:
        return None
