"""Versleuteling van geheimen met Fernet (AES-128-CBC + HMAC-SHA256).

De sleutel wordt bij de eerste start automatisch aangemaakt in een apart volume
(/keys/secret.key, chmod 600), los van de database in /data.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import paths

log = logging.getLogger(__name__)
_fernet: Fernet | None = None


class KeyError_(RuntimeError):
    """Sleutelbestand ontbreekt of is onleesbaar."""


def load_or_create_key(key_path: Path | None = None) -> bytes:
    key_path = key_path or paths.KEY_PATH
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_path.parent, 0o700)
    except OSError:
        pass
    if key_path.exists():
        key = key_path.read_bytes().strip()
        try:
            Fernet(key)
        except Exception as exc:  # noqa: BLE001
            raise KeyError_(f"Sleutelbestand {key_path} is ongeldig") from exc
        # Rechten corrigeren als ze ooit te ruim zijn gezet.
        try:
            if key_path.stat().st_mode & 0o077:
                os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key
    key = Fernet.generate_key()
    # O_EXCL: nooit een bestaande sleutel overschrijven.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    log.info("Nieuwe versleutelingssleutel aangemaakt in %s", key_path)
    return key


def init(key_path: Path | None = None) -> None:
    global _fernet
    _fernet = Fernet(load_or_create_key(key_path))


def _f() -> Fernet:
    if _fernet is None:
        init()
    assert _fernet is not None
    return _fernet


def encrypt(plain: str) -> str:
    return _f().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _f().decrypt(token.encode()).decode()
    except InvalidToken:
        log.error("Een opgeslagen geheim kon niet worden ontsleuteld (andere sleutel?)")
        return None
