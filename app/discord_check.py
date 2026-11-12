"""Discord-token en kanaal-ID's controleren via de REST-API (voor de Test-knop)."""
from __future__ import annotations

import httpx

API = "https://discord.com/api/v10"
FLAG_MESSAGE_CONTENT = 1 << 18
FLAG_MESSAGE_CONTENT_LIMITED = 1 << 19

# Bekijken, berichten sturen (ook in draadjes), geschiedenis lezen, links insluiten, reacties
# toevoegen, berichten beheren (reacties in #geheugen opruimen), openbare en privé-draadjes maken,
# draadjes beheren (titel/status aanpassen) en kanalen beheren (het meldingskanaal voor jou aanmaken).
PERMISSIONS = (1024 + 2048 + 274877906944 + 65536 + 16384 + 64 + 8192
               + 34359738368 + 68719476736 + 17179869184 + 16)


def invite_url(app_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot&permissions={PERMISSIONS}")


async def check_token(token: str, channel_ids: dict[str, str] | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, list[str], str | None]:
    """Geeft (ok, meldingen, uitnodigingslink) terug."""
    headers = {"Authorization": f"Bot {token.strip()}"}
    lines: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            me = await client.get(f"{API}/users/@me", headers=headers)
            if me.status_code == 401:
                return False, ["Discord kent dit token niet. Kopieer het opnieuw via Developer Portal → Bot "
                               "→ Reset Token."], None
            if me.status_code != 200:
                return False, [f"Discord gaf een onverwachte reactie (HTTP {me.status_code})."], None
            user = me.json()
            lines.append(f"Token werkt ✓ De bot heet {user.get('username')}.")

            app = await client.get(f"{API}/applications/@me", headers=headers)
            invite = None
            ok = True
            if app.status_code == 200:
                data = app.json()
                flags = int(data.get("flags") or 0)
                invite = invite_url(str(data.get("id") or user.get("id")))
                if flags & (FLAG_MESSAGE_CONTENT | FLAG_MESSAGE_CONTENT_LIMITED):
                    lines.append("Message Content Intent staat aan ✓")
                else:
                    ok = False
                    lines.append("⚠ Message Content Intent staat UIT. Zet hem aan: Developer Portal → "
                                 "jouw app → Bot → Privileged Gateway Intents → Message Content Intent.")

            for label, cid in (channel_ids or {}).items():
                cid = (cid or "").strip()
                if not cid:
                    continue
                if not cid.isdigit():
                    ok = False
                    lines.append(f"⚠ {label}: '{cid}' is geen geldig ID (alleen cijfers).")
                    continue
                ch = await client.get(f"{API}/channels/{cid}", headers=headers)
                if ch.status_code == 200:
                    lines.append(f"{label}: #{ch.json().get('name')} gevonden ✓")
                elif ch.status_code in (403, 404):
                    ok = False
                    lines.append(f"⚠ {label}: de bot kan dit kanaal niet zien. Is hij uitgenodigd op de "
                                 f"server en mag hij het kanaal bekijken?")
                else:
                    lines.append(f"{label}: kon niet controleren (HTTP {ch.status_code}).")
            return ok, lines, invite
    except httpx.HTTPError as exc:
        return False, [f"Kon Discord niet bereiken ({type(exc).__name__}). Heeft de server internet?"], None
