"""Geheugen: blijvende feiten over vader en zijn spullen."""
from __future__ import annotations

import json
import logging
import re

from . import db, sensitive

log = logging.getLogger(__name__)

MAX_FACT_LEN = 300


def clean_fact(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip().strip("-•* ").strip()
    return text[:MAX_FACT_LEN]


def approved_facts() -> list:
    return db.query("SELECT * FROM facts WHERE status='approved' ORDER BY id")


def pending_facts() -> list:
    return db.query("SELECT * FROM facts WHERE status='pending' ORDER BY id")


def facts_for_prompt(budget_chars: int) -> str:
    """Compacte lijst voor de prompt. Te lang? Dan de nieuwste feiten houden en afkappen."""
    rows = approved_facts()
    if not rows:
        return ""
    lines: list[str] = []
    used = 0
    for row in reversed(rows):  # nieuwste eerst kiezen
        line = f"- {row['text']}"
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    skipped = len(rows) - len(lines)
    if skipped:
        lines.append(f"(nog {skipped} oudere feiten weggelaten om het kort te houden)")
    return "\n".join(lines)


def is_duplicate(text: str) -> bool:
    norm = _norm(text)
    for row in db.query("SELECT text FROM facts WHERE status IN ('approved','pending')"):
        other = _norm(row["text"])
        if norm == other or (len(norm) > 15 and (norm in other or other in norm)):
            return True
    return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ ]", "", text.lower()).strip()


def add_fact(text: str, status: str, source: str, conversation_id: int | None = None) -> int | None:
    """Voeg een feit toe. Weigert lege, dubbele of gevoelige feiten (geeft dan None)."""
    text = clean_fact(text)
    if len(text) < 3:
        return None
    if sensitive.contains_sensitive_data(text):
        log.info("Feit met gevoelige gegevens geweigerd")
        return None
    if is_duplicate(text):
        return None
    ts = db.now()
    return db.insert(
        "INSERT INTO facts (text, status, source, created_at, updated_at, conversation_id) "
        "VALUES (?,?,?,?,?,?)",
        (text, status, source, ts, ts, conversation_id),
    )


def set_status(fact_id: int, status: str) -> None:
    db.execute("UPDATE facts SET status=?, updated_at=? WHERE id=?", (status, db.now(), fact_id))


def update_text(fact_id: int, text: str) -> bool:
    text = clean_fact(text)
    if len(text) < 3 or sensitive.contains_sensitive_data(text):
        return False
    db.execute("UPDATE facts SET text=?, updated_at=? WHERE id=?", (text, db.now(), fact_id))
    return True


def delete_fact(fact_id: int) -> None:
    db.execute("DELETE FROM facts WHERE id=?", (fact_id,))


def forget(arg: str) -> tuple[str, list]:
    """Voor !vergeet: nummer (uit !geheugen) of tekst.

    Geeft (resultaat, rijen) terug: 'deleted', 'none' of 'multiple'.
    """
    rows = approved_facts()
    arg = arg.strip()
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(rows):
            delete_fact(rows[idx]["id"])
            return "deleted", [rows[idx]]
        return "none", []
    needle = arg.lower()
    matches = [r for r in rows if needle in r["text"].lower()]
    if len(matches) == 1:
        delete_fact(matches[0]["id"])
        return "deleted", matches
    if not matches:
        return "none", []
    return "multiple", matches


# ---------- automatisch feiten vinden ----------

EXTRACT_PROMPT = """\
Je beheert een kort geheugen over een (niet-technische) gebruiker van een hulpbot.
Hieronder staat een recent gesprek en de feiten die al bekend zijn.

Zat er in het gesprek een NIEUW, BLIJVEND feit over de gebruiker? Alleen dit telt:
- apparaten en programma's die hij heeft (merk, model, versie)
- diensten en abonnementen die hij gebruikt (alleen de naam)
- vaste voorkeuren
- een opgelost probleem en wat de oplossing was (één zin)

Regels:
- Alleen wat hij zelf duidelijk zegt of wat zeker blijkt. NIET gokken.
- Geen hele gesprekken, geen tijdelijke dingen ("hij is nu moe").
- NOOIT wachtwoorden, codes, bankgegevens, adressen, telefoonnummers of andere gevoelige gegevens.
- Niets wat al bij de bekende feiten staat.
- Elk feit is één korte Nederlandse zin, bijv. "Heeft een Samsung QE55Q80 tv."

Antwoord ALLEEN met JSON in deze vorm, zonder uitleg: {"feiten": ["...", "..."]}
Geen nieuwe feiten? Antwoord dan: {"feiten": []}
"""


def parse_facts_json(text: str) -> list[str]:
    if not text:
        return []
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    facts = data.get("feiten") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return []
    return [clean_fact(f) for f in facts if isinstance(f, str) and clean_fact(f)][:5]


def build_extract_messages(conversation_rows: list) -> list[dict]:
    known = "\n".join(f"- {r['text']}" for r in db.query(
        "SELECT text FROM facts WHERE status IN ('approved','pending') ORDER BY id")) or "(nog geen)"
    convo = []
    for r in conversation_rows:
        convo.append(f"Gebruiker: {r['question']}")
        if r["answer"]:
            convo.append(f"Helper: {r['answer']}")
    return [
        {"role": "system", "content": EXTRACT_PROMPT},
        {"role": "user", "content": f"Bekende feiten:\n{known}\n\nGesprek:\n" + "\n".join(convo)},
    ]
