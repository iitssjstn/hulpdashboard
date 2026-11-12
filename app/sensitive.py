"""Herkennen van gevoelige onderwerpen en wegpoetsen van gevoelige gegevens."""
from __future__ import annotations

import re

from . import settings

# Groepen gevoelige onderwerpen (Nederlands + Engels). Bewust ruim: liever één keer
# te vaak de eigenaar erbij halen dan iemand door een betaling of oplichting praten.
TOPIC_PATTERNS: dict[str, list[str]] = {
    "betalen": [
        r"\bbetal\w*", r"\bbetaling\w*", r"\bideal\b", r"\bcredit ?card\w*", r"\bpinpas\w*",
        r"\bbankpas\w*", r"\bbank(?:zaken|app|rekening|gegevens|pas|code)?\b",
        r"\binternet ?bankier\w*", r"\bmobiel bankier\w*", r"\bover(?:maken|boeken|gemaakt)\b",
        r"\btikkie\b", r"\bpaypal\b", r"\bincasso\b", r"\brekeningnummer\w*", r"\biban\b",
        r"\bpayment\w*", r"\bpay\b", r"\bfactuur\b", r"\bvoorschot\b",
    ],
    "wachtwoorden": [
        r"\bwachtwoord\w*", r"\bpassword\w*", r"\bpin ?code\w*", r"\binlog ?code\w*",
        r"\binloggegevens\b", r"\bverificatiecode\w*", r"\bbeveiligingscode\w*", r"\b2fa\b",
        r"\btwee-?staps\w*", r"\bdigid\b", r"\bpasscode\b", r"\bherstelcode\w*",
    ],
    "verdacht": [
        r"\bverdacht\w*", r"\bphishing\b", r"\boplicht\w*", r"\bfraude\w*", r"\bscam\w*",
        r"\bgehackt\b", r"\bgehacked\b", r"\bhacker\w*", r"\bnep[- ]?(?:mail|sms|bericht|site|website)\b",
        r"\bpakketje (?:betalen|tegenhouden)\b", r"\bbelde .{0,30}(?:microsoft|helpdesk|bank)\b",
        r"\buw (?:computer|pc) is (?:besmet|geblokkeerd)\b", r"\bafpersing\b", r"\blosgeld\b",
        r"\bmeekijken op (?:mijn|de) (?:pc|computer)\b", r"\banydesk\b", r"\bteamviewer\b",
    ],
    "opzeggen": [
        r"\bopzeg\w*", r"\bop te zeggen\b", r"\bzeg\w* .{0,20}\bop\b", r"\bstopzetten\b",
        r"\bcancel\w*", r"\bunsubscribe\b", r"\blidmaatschap beëindig\w*",
        r"\babonnement (?:stoppen|beëindigen|afsluiten|afgesloten|aanpassen)\b",
    ],
}

_compiled: dict[str, list[re.Pattern]] = {
    group: [re.compile(p, re.IGNORECASE) for p in pats] for group, pats in TOPIC_PATTERNS.items()
}


def detect(text: str) -> str | None:
    """Geef de naam van de gevonden gevoelige groep terug, of None."""
    if not text:
        return None
    for group, pats in _compiled.items():
        if any(p.search(text) for p in pats):
            return group
    for word in settings.get("sensitive_extra") or []:
        word = (word or "").strip()
        if word and re.search(r"\b" + re.escape(word), text, re.IGNORECASE):
            return "extra"
    return None


# ---------- wegpoetsen ----------

_REDACTIONS = [
    # Nederlandse IBAN (bijv. NL91 ABNA 0417 1643 00)
    re.compile(r"\bNL\d{2}[ ]?[A-Z]{4}(?:[ ]?\d){10}\b", re.IGNORECASE),
    # Overige veelvoorkomende IBANs (vrijwel alleen cijfers na de landcode)
    re.compile(r"\b(?:BE|DE|LU|AT|FR|ES|IT|GB|CH)\d{2}(?:[ ]?[0-9A-Z]{4}){3,7}(?:[ ]?\d{1,3})?\b"),
    # Twitch-streamsleutel
    re.compile(r"\blive_\d+_[A-Za-z0-9]{10,}\b"),
    # Betaalkaartnummers (13-19 cijfers, eventueel met spaties of streepjes)
    re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
]
_AFTER_KEYWORD = re.compile(
    r"(?P<kw>\b(?:wachtwoord|password|wachtwoord is|pincode|pin|inlogcode|code|cvc|cvv)\b"
    r"\s*(?:is|=|:)?\s*)(?P<val>\S+)",
    re.IGNORECASE,
)
REDACTED = "[verwijderd]"


def redact(text: str) -> str:
    """Haal wachtwoorden, pincodes, IBANs en kaartnummers uit tekst."""
    if not text:
        return text
    for pat in _REDACTIONS:
        text = pat.sub(REDACTED, text)

    def _kw(m: re.Match) -> str:
        val = m.group("val")
        # Alleen wegpoetsen als het op een code/wachtwoord lijkt (bevat cijfer of is lang genoeg).
        if any(ch.isdigit() for ch in val) or (len(val) >= 6 and not val.isalpha()):
            return m.group("kw") + REDACTED
        if m.group("kw").strip().lower().startswith(("wachtwoord", "password")) and val.lower() not in (
            "vergeten", "kwijt", "wijzigen", "veranderen", "resetten", "werkt", "niet", "is", "van", "voor",
            "en", "in", "op", "te", "maar", "of",
        ):
            return m.group("kw") + REDACTED
        return m.group(0)

    return _AFTER_KEYWORD.sub(_kw, text)


def contains_sensitive_data(text: str) -> bool:
    return redact(text) != text
