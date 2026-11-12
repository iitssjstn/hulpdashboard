"""Logging naar stdout, plus waarschuwingen en fouten naar de database (Status-pagina)."""
from __future__ import annotations

import logging
import sys
import threading

from . import db

_local = threading.local()


class _NoVoiceNoise(logging.Filter):
    """discord.py waarschuwt dat spraak niet werkt; de bot gebruikt geen spraak."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


class DBHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_local, "busy", False):
            return
        _local.busy = True
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                # Alleen het fouttype en de melding, geen volledige stacktrace in het dashboard.
                exc = record.exc_info[1]
                msg = f"{msg} ({type(exc).__name__}: {str(exc)[:300]})"
            db.add_log(record.levelname, record.name, msg)
        except Exception:  # noqa: BLE001 - logging mag nooit crashen
            pass
        finally:
            _local.busy = False


def setup() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    out.addFilter(_NoVoiceNoise())
    root.addHandler(out)
    dbh = DBHandler(level=logging.WARNING)
    dbh.addFilter(_NoVoiceNoise())
    root.addHandler(dbh)
    # Rustiger: geen URL's per request van httpx, minder gateway-ruis van discord.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
