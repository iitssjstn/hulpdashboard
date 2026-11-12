"""Eén dashboardgebruiker: wachtwoord-hash (scrypt), sessies, CSRF en login-ratelimiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, settings

SESSION_COOKIE = "hulp_sessie"
SESSION_DAYS = 14
MIN_PASSWORD_LEN = 10

_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


# ---------- wachtwoord ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), maxmem=_SCRYPT["maxmem"], dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def password_problem(password: str, confirm: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Kies een wachtwoord van minstens {MIN_PASSWORD_LEN} tekens."
    if password != confirm:
        return "De twee wachtwoorden zijn niet hetzelfde."
    return None


def has_password() -> bool:
    return bool(settings.get("password_hash"))


# ---------- eenmalige setupcode ----------
# Zolang er nog geen wachtwoord is, mag alleen wie de code uit de containerlogs kent er een kiezen.
# De code staat alleen in het geheugen (niet in de database) en is na elke herstart nieuw.

_SETUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # zonder 0/O en 1/I
setup_code: str | None = None


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def ensure_setup_code() -> str | None:
    """Maak een setupcode aan als er nog geen wachtwoord is (anders None)."""
    global setup_code
    if has_password():
        setup_code = None
        return None
    if setup_code is None:
        raw = "".join(secrets.choice(_SETUP_ALPHABET) for _ in range(12))
        setup_code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    return setup_code


def check_setup_code(code: str) -> bool:
    if setup_code is None:
        return False
    return hmac.compare_digest(normalize_code(code), normalize_code(setup_code))


# ---------- sessies ----------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO sessions (token_hash, csrf, created_at, expires_at) VALUES (?,?,?,?)",
        (_hash_token(token), secrets.token_urlsafe(24), db.now(), expires.isoformat(timespec="seconds")),
    )
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    return token


def get_session(token: str | None):
    if not token:
        return None
    row = db.query_one("SELECT * FROM sessions WHERE token_hash=?", (_hash_token(token),))
    if row is None or row["expires_at"] < db.now():
        return None
    return row


def delete_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def delete_all_sessions() -> None:
    db.execute("DELETE FROM sessions")


# ---------- ratelimiting tegen brute force ----------

class LoginLimiter:
    """Per IP: na 5 fouten binnen 15 min een blokkade die steeds langer wordt.
    Globaal: na 20 fouten binnen 15 min wordt inloggen voor iedereen 15 min geblokkeerd."""

    def __init__(self, per_ip: int = 5, global_max: int = 20, window: int = 15 * 60):
        self.per_ip, self.global_max, self.window = per_ip, global_max, window
        self.fails: dict[str, list[float]] = {}
        self.blocked_until: dict[str, float] = {}
        self.strikes: dict[str, int] = {}
        self.global_fails: list[float] = []
        self.global_blocked_until = 0.0
        self.lock = threading.Lock()

    def check(self, ip: str) -> int:
        """Geeft het aantal seconden dat deze IP nog moet wachten (0 = mag proberen)."""
        now = time.time()
        with self.lock:
            wait = max(self.blocked_until.get(ip, 0) - now, self.global_blocked_until - now, 0)
            return int(wait) + (1 if wait > 0 else 0)

    def fail(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            lst = [t for t in self.fails.get(ip, []) if now - t < self.window] + [now]
            self.fails[ip] = lst
            if len(lst) >= self.per_ip:
                strikes = self.strikes.get(ip, 0) + 1
                self.strikes[ip] = strikes
                self.blocked_until[ip] = now + min(15 * 60 * (2 ** (strikes - 1)), 24 * 3600)
                self.fails[ip] = []
            self.global_fails = [t for t in self.global_fails if now - t < self.window] + [now]
            if len(self.global_fails) >= self.global_max:
                self.global_blocked_until = now + self.window
                self.global_fails = []

    def success(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)
            self.strikes.pop(ip, None)
            self.blocked_until.pop(ip, None)


limiter = LoginLimiter()
