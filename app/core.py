"""Bot-logica zonder Discord: vragen beantwoorden, commando's, geheugen.

Zo is alles te testen zonder echte tokens.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

from . import ai, db, memory, paths, prompt, sensitive, settings

log = logging.getLogger(__name__)

REFER_MARKER = "[[DOORVERWIJZEN]]"
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_MAX_SIDE = 1600


@dataclass
class Reply:
    text: str
    status: str = "ok"  # ok | doorverwezen | fout | commando
    mention_owner: bool = False
    conversation_id: int | None = None
    topic: str | None = None  # bij een gevoelig onderwerp: welke groep


@dataclass
class IncomingImage:
    data: bytes
    filename: str = "afbeelding"


@dataclass
class SavedImage:
    filename: str
    data_url: str


@dataclass
class MemoryEvent:
    fact_id: int
    text: str
    status: str
    source: str
    by: str = ""


@dataclass
class Outbox:
    """Meldingen voor het #geheugen-kanaal, die de Discord-laag verstuurt."""
    events: list[MemoryEvent] = field(default_factory=list)


# ---------- teksten ----------

def owner_name() -> str:
    return (settings.get("profile").get("eigenaar_naam") or "").strip()


def owner_tag() -> str:
    uid = str(settings.get("owner_user_id") or "").strip()
    if uid.isdigit():
        return f"<@{uid}>"
    return owner_name() or "je zoon/dochter"


def referral_line() -> str:
    return f"{owner_tag()} kun je hier even naar kijken?"


def referral_reply() -> str:
    return (
        "Goed dat je het even vraagt! Hier ben ik voorzichtig mee, want het gaat om geld, "
        "inloggegevens of iets wat niet helemaal te vertrouwen is. Klik voorlopig nergens op "
        "en vul niets in.\n\n" + referral_line()
    )


FRIENDLY_ERRORS = {
    "no_key": "Ik ben nog niet helemaal klaar om te helpen. {owner} moet nog iets instellen. 🙏",
    "bad_key": "Ik kan nu even niet nadenken, er klopt iets niet met mijn instellingen. "
               "{owner} kan dat oplossen. 🙏",
    "all_failed": "Sorry, het lukt me nu even niet om te antwoorden. Het is erg druk. "
                  "Probeer het over een paar minuten nog een keer. 🙏",
    "other": "Sorry, er ging iets mis aan mijn kant. Probeer het straks nog een keer. 🙏",
}


def friendly_error(kind: str) -> str:
    return FRIENDLY_ERRORS.get(kind, FRIENDLY_ERRORS["other"]).format(owner=owner_name() or "Je zoon/dochter")


# ---------- afbeeldingen ----------

def _process_image(raw: bytes) -> tuple[bytes, str]:
    """Verklein, draai goed en verwijder EXIF (bijv. GPS uit foto's). Geeft JPEG terug."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode == "L":
            img = img.convert("RGB")
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def save_images(images: list[IncomingImage]) -> list[SavedImage]:
    saved: list[SavedImage] = []
    paths.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    for im in images[:MAX_IMAGES]:
        if len(im.data) > MAX_IMAGE_BYTES:
            continue
        try:
            jpeg, mime = _process_image(im.data)
        except Exception as exc:  # noqa: BLE001 - kapotte of onbekende afbeelding
            log.warning("Afbeelding kon niet worden gelezen: %s", type(exc).__name__)
            continue
        name = f"{uuid.uuid4().hex}.jpg"
        (paths.IMAGE_DIR / name).write_bytes(jpeg)
        saved.append(SavedImage(name, f"data:{mime};base64," + base64.b64encode(jpeg).decode()))
    return saved


# ---------- gespreksgeschiedenis ----------

def history_messages(user_id: str, ticket_id: int | None = None) -> list[dict]:
    """Eerdere berichten als context. Met een ticket: dat hele gesprek (tot 30), anders per gebruiker."""
    if ticket_id is not None:
        rows = db.query(
            "SELECT question, answer, images FROM conversations "
            "WHERE ticket_id=? AND status IN ('ok','doorverwezen') ORDER BY id DESC LIMIT 30",
            (ticket_id,),
        )
    else:
        n = settings.get_int("history_messages", 0, 50)
        hours = settings.get_int("history_hours", 1, 24 * 30)
        if n == 0:
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = db.query(
            "SELECT question, answer, images FROM conversations "
            "WHERE user_id=? AND status IN ('ok','doorverwezen') AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, since, n),
        )
    out: list[dict] = []
    for r in reversed(rows):
        q = r["question"]
        if db.get_json(r["images"], []):
            q = (q + "\n[stuurde een afbeelding]").strip()
        out.append({"role": "user", "content": q or "[stuurde een afbeelding]"})
        if r["answer"]:
            out.append({"role": "assistant", "content": r["answer"]})
    return out


# ---------- vragen beantwoorden ----------

def _clean_question(text: str) -> str:
    text = re.sub(r"<@!?\d+>", "", text or "")
    return text.strip()


def _store(user_id: str, user_name: str, channel_id: str | None, question: str, answer: str,
           images: list[str], model: str | None, status: str, error: str | None = None,
           memory_checked: int = 0, ticket_id: int | None = None) -> int:
    return db.insert(
        "INSERT INTO conversations (created_at, user_id, user_name, channel_id, question, answer, images, "
        "model, status, error, memory_checked, ticket_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (db.now(), user_id, user_name, channel_id, question, answer, json.dumps(images), model,
         status, error, memory_checked, ticket_id),
    )


async def answer_question(user_id: str, user_name: str, channel_id: str | None, text: str,
                          images: list[IncomingImage] | None = None, ticket_id: int | None = None) -> Reply:
    raw = _clean_question(text)
    question = sensitive.redact(raw)
    images = images or []

    # Gevoelig onderwerp in de tekst: niet naar het model, direct eigenaar erbij halen.
    topic = sensitive.detect(raw)
    if topic:
        answer = referral_reply()
        saved = await asyncio.to_thread(save_images, images) if images else []
        cid = _store(user_id, user_name, channel_id, question, answer, [s.filename for s in saved],
                     None, "doorverwezen", f"gevoelig onderwerp: {topic}", memory_checked=1, ticket_id=ticket_id)
        return Reply(answer, "doorverwezen", True, cid, topic=topic)

    if not question and not images:
        return Reply("Hoi! Waar kan ik je mee helpen? Je mag ook een foto of schermafbeelding sturen.",
                     "commando")

    saved = await asyncio.to_thread(save_images, images) if images else []
    if images and not saved and not question:
        return Reply("Ik kon die afbeelding helaas niet openen. Kun je hem nog een keer sturen, "
                     "of een schermafbeelding maken?", "commando")

    content: list[dict] | str
    if saved:
        content = [{"type": "text", "text": question or "Wat zie je hier, en wat moet ik doen?"}]
        content += [{"type": "image_url", "image_url": {"url": s.data_url}} for s in saved]
    else:
        content = question

    messages = [{"role": "system", "content": prompt.build_system_prompt()}]
    messages += history_messages(user_id, ticket_id)
    messages.append({"role": "user", "content": content})

    try:
        result = await ai.chat(messages)
    except ai.AIError as exc:
        log.error("AI-fout (%s): %s | %s", exc.kind, exc.detail, " ; ".join(exc.attempts)[:1500])
        answer = friendly_error(exc.kind)
        cid = _store(user_id, user_name, channel_id, question, answer, [s.filename for s in saved],
                     None, "fout", f"{exc.detail}: {' ; '.join(exc.attempts)}"[:2000], memory_checked=1, ticket_id=ticket_id)
        return Reply(answer, "fout", False, cid)
    except Exception as exc:  # noqa: BLE001 - nooit een stacktrace naar vader
        log.exception("Onverwachte fout bij beantwoorden")
        answer = friendly_error("other")
        cid = _store(user_id, user_name, channel_id, question, answer, [s.filename for s in saved],
                     None, "fout", f"{type(exc).__name__}: {exc}"[:2000], memory_checked=1, ticket_id=ticket_id)
        return Reply(answer, "fout", False, cid)

    if result.attempts:
        log.warning("Fallback gebruikt, antwoord van %s. Mislukt: %s", result.model,
                    " ; ".join(result.attempts)[:1500])

    answer = sensitive.redact(result.text)
    status, mention = "ok", False
    if REFER_MARKER in answer or "[[DOORVERWIJZEN]" in answer:
        answer = answer.replace(REFER_MARKER, "").replace("[[DOORVERWIJZEN]", "").strip()
        answer = f"{answer}\n\n{referral_line()}".strip()
        status, mention = "doorverwezen", True
    cid = _store(user_id, user_name, channel_id, question, answer, [s.filename for s in saved],
                 result.model, status, memory_checked=1 if status == "doorverwezen" else 0, ticket_id=ticket_id)
    return Reply(answer, status, mention, cid)


# ---------- commando's ----------

COMMANDS = ("!onthoud", "!vergeet", "!geheugen", "!hulp")


def is_command(text: str) -> bool:
    first = (text or "").strip().split(maxsplit=1)
    return bool(first) and first[0].lower() in COMMANDS


def handle_command(user_name: str, text: str, outbox: Outbox) -> Reply:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "!hulp":
        return Reply(
            "Zo werk ik:\n"
            "• Stel gewoon je vraag hier, of stuur een foto of schermafbeelding.\n"
            "• `!onthoud <tekst>`: ik onthoud iets voor je\n"
            "• `!geheugen`: laat zien wat ik onthouden heb\n"
            "• `!vergeet <nummer of tekst>`: ik vergeet iets",
            "commando",
        )

    if cmd == "!onthoud":
        if not arg:
            return Reply("Wat moet ik onthouden? Typ bijvoorbeeld: `!onthoud Mijn tv is een Samsung`",
                         "commando")
        if sensitive.contains_sensitive_data(arg):
            return Reply("Dat bewaar ik liever niet: er lijkt een wachtwoord, code of bankgegeven in "
                         "te staan. Zulke dingen onthoud ik nooit. 🔒", "commando")
        fact_id = memory.add_fact(arg, "approved", "discord")
        if fact_id is None:
            return Reply("Dat wist ik al, of het was te kort om te onthouden. 🙂", "commando")
        outbox.events.append(MemoryEvent(fact_id, memory.clean_fact(arg), "approved", "discord", user_name))
        return Reply(f"Goed, dat onthoud ik: _{memory.clean_fact(arg)}_ ✅", "commando")

    if cmd == "!vergeet":
        if not arg:
            return Reply("Wat moet ik vergeten? Typ `!geheugen` om de nummers te zien, en dan "
                         "bijvoorbeeld `!vergeet 3`.", "commando")
        result, rows = memory.forget(arg)
        if result == "deleted":
            return Reply(f"Vergeten: _{rows[0]['text']}_ 🗑️", "commando")
        if result == "multiple":
            nums = {r["id"]: i + 1 for i, r in enumerate(memory.approved_facts())}
            listing = "\n".join(f"{nums[r['id']]}. {r['text']}" for r in rows[:10])
            return Reply("Ik vond meerdere dingen. Welke bedoel je? Typ `!vergeet` met het nummer:\n"
                         + listing, "commando")
        return Reply("Dat kon ik niet vinden in mijn geheugen. Typ `!geheugen` om alles te zien.",
                     "commando")

    # !geheugen
    rows = memory.approved_facts()
    pending = len(memory.pending_facts())
    if not rows:
        text_out = "Ik heb nog niets onthouden."
    else:
        text_out = "Dit heb ik onthouden:\n" + "\n".join(f"{i + 1}. {r['text']}" for i, r in enumerate(rows))
    if pending:
        text_out += f"\n\n({pending} nieuwe feiten wachten nog op goedkeuring.)"
    return Reply(text_out, "commando")


# ---------- automatisch geheugen ----------

async def extract_facts_for_user(user_id: str, outbox: Outbox) -> list[int]:
    """Vraag het model of er nieuwe blijvende feiten in de nog niet bekeken gesprekken zaten."""
    rows = db.query(
        "SELECT * FROM conversations WHERE user_id=? AND memory_checked=0 AND status='ok' "
        "ORDER BY id DESC LIMIT 12",
        (user_id,),
    )
    if not rows:
        return []
    rows = list(reversed(rows))
    ids = [r["id"] for r in rows]

    def mark_checked() -> None:
        db.execute(
            f"UPDATE conversations SET memory_checked=1 WHERE user_id=? AND memory_checked=0 "
            f"AND id <= ?", (user_id, max(ids)),
        )

    try:
        result = await ai.chat(memory.build_extract_messages(rows), max_tokens=400, temperature=0.1)
    except ai.AIError as exc:
        log.warning("Geheugen bijwerken lukte niet (%s): %s", exc.kind, exc.detail)
        if exc.kind != "all_failed":
            mark_checked()
        return []  # bij drukte later opnieuw proberen
    mark_checked()

    mode = settings.get("memory_mode")
    status = "approved" if mode == "auto" else "pending"
    new_ids = []
    for text in memory.parse_facts_json(result.text):
        if sensitive.detect(text) in ("wachtwoorden", "betalen"):
            continue
        fid = memory.add_fact(text, status, "auto", conversation_id=ids[-1])
        if fid:
            new_ids.append(fid)
            outbox.events.append(MemoryEvent(fid, memory.clean_fact(text), status, "auto"))
    return new_ids


def users_with_unchecked() -> list[str]:
    return [r["user_id"] for r in db.query(
        "SELECT DISTINCT user_id FROM conversations WHERE memory_checked=0 AND status='ok'")]


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Discord staat maximaal 2000 tekens per bericht toe."""
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks or [""]
