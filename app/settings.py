"""Instellingen (JSON in SQLite) en versleutelde geheimen.

Alles wordt bij elk gebruik live uit de database gelezen, zodat wijzigingen in
het dashboard direct werken zonder herstart.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from . import crypto, db

DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_FALLBACKS = [
    "google/gemma-4-26b-a4b-it:free",
    "openrouter/free",
]

PROFILE_FIELDS: list[tuple[str, str, str]] = [
    # (sleutel, label, hulptekst)
    ("naam", "Naam van je vader", "Zo spreekt de bot hem aan, bijv. Pa of Henk"),
    ("eigenaar_naam", "Jouw naam", "Wordt gebruikt als de bot jou erbij haalt"),
    ("aanspreekvorm", "Aanspreekvorm", "je of u"),
    ("besturingssysteem", "Besturingssysteem", "Bijv. Windows 11 of macOS Sonoma"),
    ("taal_pc", "Taal van zijn pc", "Nederlands of Engels; bepaalt de menunamen"),
    ("browser", "Browser", "Bijv. Chrome, Edge, Firefox, Safari"),
    ("tv", "Tv (merk/model)", "Bijv. Samsung QE55Q80 of LG OLED55C3"),
    ("streamingdiensten", "Streamingdiensten", "Bijv. Netflix, Videoland, NPO Start, Ziggo"),
    ("wifi_router", "Wifi / router", "Bijv. KPN Box 14, Ziggo SmartWifi"),
    ("twitch_kanaal", "Twitch-kanaal", "Naam van zijn kanaal, affiliate ja/nee"),
    ("obs", "OBS-scenes en bronnen", "Welke scenes, welke bronnen (camera, game, browser)"),
    ("streamelements", "StreamElements-commando's en overlays", "Bijv. !discord, alerts-overlay"),
    ("microfoon", "Microfoon", "Merk/model, USB of via audio-interface"),
    ("camera", "Camera", "Merk/model"),
    ("overig", "Overige notities", "Alles wat verder handig is om te weten"),
]

DEFAULT_PROFILE = {key: "" for key, _, _ in PROFILE_FIELDS}
DEFAULT_PROFILE.update({"aanspreekvorm": "je", "taal_pc": "Nederlands"})

DEFAULTS: dict[str, Any] = {
    "setup_done": False,
    "setup_step": 1,
    "password_hash": "",
    "guild_id": "",
    "help_channel_id": "",
    "memory_channel_id": "",
    "owner_user_id": "",
    "model_primary": DEFAULT_MODEL,
    "model_fallbacks": DEFAULT_FALLBACKS,
    "history_messages": 10,
    "history_hours": 24,
    "memory_mode": "review",  # 'auto' of 'review'
    "memory_idle_minutes": 10,
    "memory_budget_chars": 2500,
    "sensitive_extra": [],
    "ticket_mode": "openbaar",  # 'uit', 'openbaar' (draadje) of 'prive' (privé-draadje)
    "ticket_followup_minutes": 15,  # bericht in #hulp binnen deze tijd = vervolg op open ticket
    "ticket_ai_titles": True,
    "owner_channel_id": "",  # privékanaal voor meldingen aan de eigenaar (leeg = zelf aanmaken)
    "profile": DEFAULT_PROFILE,
    "base_prompt": None,  # None = standaardprompt uit prompt.py
}

SECRET_KEYS = ("discord_token", "openrouter_key")


def get(key: str) -> Any:
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    default = copy.deepcopy(DEFAULTS.get(key))
    if row is None:
        return default
    value = db.get_json(row["value"], default)
    if key == "profile" and isinstance(value, dict):
        merged = copy.deepcopy(DEFAULT_PROFILE)
        merged.update({k: v for k, v in value.items() if k in merged})
        return merged
    return value


def set(key: str, value: Any) -> None:  # noqa: A001 - bewust kort
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_int(key: str, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(get(key))))
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def models() -> list[str]:
    """Primair model plus fallbacks, zonder dubbelen."""
    out: list[str] = []
    for m in [get("model_primary"), *(get("model_fallbacks") or [])]:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out or [DEFAULT_MODEL]


# ---------- geheimen ----------

def set_secret(key: str, value: str) -> None:
    assert key in SECRET_KEYS
    db.execute(
        "INSERT INTO secrets (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, crypto.encrypt(value.strip()), db.now()),
    )


def get_secret(key: str) -> str | None:
    row = db.query_one("SELECT value FROM secrets WHERE key=?", (key,))
    if row is None:
        return None
    return crypto.decrypt(row["value"])


def has_secret(key: str) -> bool:
    return db.query_one("SELECT 1 FROM secrets WHERE key=?", (key,)) is not None


def delete_secret(key: str) -> None:
    db.execute("DELETE FROM secrets WHERE key=?", (key,))
