"""Tickets: elk probleem een eigen draadje. Logica zonder Discord (testbaar)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from . import ai, db, sensitive, settings

log = logging.getLogger(__name__)

STATUS_ICON = {"open": "🟡", "wacht_op_jou": "🙋", "opgelost": "✅"}
STATUS_LABEL = {"open": "Open", "wacht_op_jou": "Wacht op jou", "opgelost": "Opgelost"}
TOPIC_LABEL = {
    "betalen": "betalen of bankzaken",
    "wachtwoorden": "wachtwoorden of inloggegevens",
    "verdacht": "een verdacht bericht of telefoontje",
    "opzeggen": "een abonnement opzeggen",
    "extra": "een onderwerp uit jouw lijst met gevoelige woorden",
}
MAX_TITLE = 60


def enabled() -> bool:
    return settings.get("ticket_mode") in ("openbaar", "prive")


def private() -> bool:
    return settings.get("ticket_mode") == "prive"


# ---------- opslag ----------

def create(user_id: str, user_name: str, guild_id: str | None, thread_id: str | None, title: str) -> int:
    ts = db.now()
    return db.insert(
        "INSERT INTO tickets (created_at, updated_at, user_id, user_name, guild_id, thread_id, title, status) "
        "VALUES (?,?,?,?,?,?,?, 'open')",
        (ts, ts, user_id, user_name, guild_id, thread_id, clean_title(title)),
    )


def get(ticket_id: int):
    return db.query_one("SELECT * FROM tickets WHERE id=?", (ticket_id,))


def by_thread(thread_id: str | int | None):
    if thread_id is None:
        return None
    return db.query_one("SELECT * FROM tickets WHERE thread_id=?", (str(thread_id),))


def by_escalation_message(message_id: str | int):
    return db.query_one("SELECT * FROM tickets WHERE escalation_message_id=?", (str(message_id),))


def touch(ticket_id: int) -> None:
    db.execute("UPDATE tickets SET updated_at=? WHERE id=?", (db.now(), ticket_id))


def set_status(ticket_id: int, status: str) -> None:
    assert status in STATUS_ICON
    closed = db.now() if status == "opgelost" else None
    db.execute("UPDATE tickets SET status=?, updated_at=?, closed_at=? WHERE id=?",
               (status, db.now(), closed, ticket_id))


def set_title(ticket_id: int, title: str) -> None:
    db.execute("UPDATE tickets SET title=? WHERE id=?", (clean_title(title), ticket_id))


def set_escalation(ticket_id: int, reason: str, summary: str, channel_id: str | None,
                   message_id: str | None) -> None:
    db.execute(
        "UPDATE tickets SET escalation_reason=?, summary=?, escalation_channel_id=?, escalation_message_id=?, "
        "updated_at=? WHERE id=?",
        (reason, summary, channel_id, message_id, db.now(), ticket_id),
    )


def followup_ticket(user_id: str):
    """Open ticket van deze gebruiker met recente activiteit: een nieuw bericht in #hulp hoort daar vermoedelijk bij."""
    minutes = settings.get_int("ticket_followup_minutes", 0, 240)
    if minutes == 0:
        return None
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return db.query_one(
        "SELECT * FROM tickets WHERE user_id=? AND status IN ('open','wacht_op_jou') AND updated_at >= ? "
        "AND thread_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (user_id, since),
    )


def conversation_rows(ticket_id: int, limit: int = 30) -> list:
    rows = db.query("SELECT * FROM conversations WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (ticket_id, limit))
    return list(reversed(rows))


def counts() -> dict:
    out = {k: 0 for k in STATUS_ICON}
    for r in db.query("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


# ---------- titels ----------

def clean_title(title: str) -> str:
    title = re.sub(r"<@!?\d+>", "", title or "")
    title = re.sub(r"[\r\n`*_~#>|]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip().strip('"“”\'.').strip()
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1].rsplit(" ", 1)[0] + "…"
    return title or "Hulpvraag"


def fallback_title(question: str, topic: str | None = None, has_image: bool = False) -> str:
    if topic:
        return f"Vraag over {TOPIC_LABEL.get(topic, 'iets gevoeligs')}"
    question = sensitive.redact(question or "")
    if not question.strip():
        return "Vraag met een afbeelding" if has_image else "Hulpvraag"
    return clean_title(question)


def thread_name(title: str, status: str) -> str:
    return f"{STATUS_ICON.get(status, '🟡')} {clean_title(title)}"[:100]


TITLE_PROMPT = (
    "Bedenk een korte, duidelijke Nederlandse titel (maximaal 6 woorden) voor deze hulpvraag, zoals een "
    "onderwerpregel. Noem het apparaat of programma als dat duidelijk is. Antwoord ALLEEN met de titel, "
    "zonder aanhalingstekens. Voorbeelden: 'Tv geeft geen geluid', 'OBS: camera blijft zwart', "
    "'Netflix inloggen op de tv'."
)


async def ai_title(question: str, has_image: bool = False) -> str | None:
    question = sensitive.redact(question or "").strip()
    if not question:
        return None
    text = question + ("\n(Er zat ook een afbeelding bij.)" if has_image else "")
    try:
        result = await ai.chat([{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": text}],
                               max_tokens=30, temperature=0.2)
    except ai.AIError as exc:
        log.info("Titel bedenken lukte niet (%s)", exc.kind)
        return None
    title = clean_title(result.text.splitlines()[0] if result.text else "")
    return None if title == "Hulpvraag" else title


# ---------- samenvatting voor de eigenaar ----------

SUMMARY_PROMPT = """\
Je schrijft een korte melding voor de zoon/dochter van een niet-technische gebruiker. De helper-bot \
heeft hem/haar erbij gehaald. Vat het gesprek hieronder samen in het Nederlands, in maximaal 3 korte \
zinnen, zodat hij/zij het gesprek niet hoeft terug te lezen:
1. Wat is het probleem (apparaat/programma, wat er misgaat)?
2. Wat heeft de bot al voorgesteld of is al geprobeerd?
3. Waarom is hulp van een mens nodig / wat moet er nog gebeuren?
Geen opsommingstekens, geen inleiding. Noem nooit wachtwoorden, codes of bankgegevens."""


def reason_text(kind: str, topic: str | None = None) -> str:
    if kind == "gevoelig":
        return f"Gevoelig onderwerp: {TOPIC_LABEL.get(topic or '', 'iets gevoeligs')}"
    if kind == "zelf":
        return "Hij vroeg zelf om jouw hulp"
    return "De bot vond dit iets voor jou (gevoelig of te lastig)"


def _quote(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", sensitive.redact(text or "")).strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text


def simple_summary(rows: list, kind: str, topic: str | None, user_name: str) -> str:
    """Samenvatting zonder AI (voor gevoelige onderwerpen, of als het model niet antwoordt)."""
    last_q = next((r["question"] for r in reversed(rows) if r["question"]), "")
    parts = []
    if kind == "gevoelig":
        parts.append(f"{user_name} heeft een vraag over {TOPIC_LABEL.get(topic or '', 'iets gevoeligs')}. "
                     f"De bot heeft hem gezegd nergens op te klikken en niets in te vullen.")
    if last_q:
        parts.append(f"Zijn laatste bericht: “{_quote(last_q)}”")
    earlier = [r for r in rows if r["status"] == "ok"]
    if earlier and kind != "gevoelig":
        parts.append(f"De bot gaf al {len(earlier)} antwoord(en) in dit gesprek.")
    return "\n".join(parts) or "Geen details beschikbaar."


async def build_summary(rows: list, kind: str, topic: str | None, user_name: str) -> str:
    """Samenvatting van het gesprek. Gevoelige onderwerpen gaan bewust niet naar het model."""
    if kind == "gevoelig" or not rows:
        return simple_summary(rows, kind, topic, user_name)
    convo = []
    for r in rows[-12:]:
        if r["status"] == "doorverwezen" and r["error"] and "gevoelig" in (r["error"] or ""):
            continue  # gevoelige berichten niet meesturen
        q = sensitive.redact(r["question"] or "")
        if q:
            convo.append(f"{user_name}: {q}")
        if r["answer"]:
            convo.append(f"Bot: {sensitive.redact(r['answer'])}")
    if not convo:
        return simple_summary(rows, kind, topic, user_name)
    try:
        result = await ai.chat([{"role": "system", "content": SUMMARY_PROMPT},
                                {"role": "user", "content": "\n".join(convo)[-6000:]}],
                               max_tokens=220, temperature=0.2)
        text = sensitive.redact(result.text.strip())
        return text[:900] if text else simple_summary(rows, kind, topic, user_name)
    except ai.AIError as exc:
        log.info("Samenvatting maken lukte niet (%s), eenvoudige samenvatting gebruikt", exc.kind)
        return simple_summary(rows, kind, topic, user_name)
