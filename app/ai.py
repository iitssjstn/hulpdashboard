"""OpenRouter-client met automatische fallback naar het volgende (gratis) model."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from . import settings

log = logging.getLogger(__name__)

API_BASE = "https://openrouter.ai/api/v1"
TIMEOUT = httpx.Timeout(90.0, connect=10.0)
HEADERS = {"X-Title": "Hulpdashboard"}


@dataclass
class AIResult:
    text: str
    model: str
    attempts: list[str] = field(default_factory=list)


class AIError(Exception):
    """Fout die we vriendelijk aan de gebruiker kunnen melden."""

    def __init__(self, kind: str, detail: str, attempts: list[str] | None = None):
        super().__init__(detail)
        self.kind = kind  # 'no_key', 'bad_key', 'all_failed'
        self.detail = detail
        self.attempts = attempts or []


def _extract_text(data: dict) -> str:
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(content, list):  # sommige providers geven delen terug
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


async def chat(
    messages: list[dict],
    models: list[str] | None = None,
    max_tokens: int = 900,
    temperature: float = 0.4,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AIResult:
    api_key = api_key or settings.get_secret("openrouter_key")
    if not api_key:
        raise AIError("no_key", "Er is nog geen OpenRouter-API-key ingesteld")
    models = models or settings.models()
    attempts: list[str] = []

    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        for model in models:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            try:
                resp = await client.post(
                    f"{API_BASE}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}", **HEADERS},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempts.append(f"{model}: verbinding/timeout ({type(exc).__name__})")
                log.warning("Model %s: %s", model, type(exc).__name__)
                continue

            if resp.status_code == 401:
                attempts.append(f"{model}: 401 ongeldige key")
                raise AIError("bad_key", "OpenRouter weigert de API-key (401)", attempts)

            try:
                data = resp.json()
            except ValueError:
                data = {}

            if resp.status_code != 200 or (isinstance(data, dict) and data.get("error")):
                err = data.get("error") if isinstance(data, dict) else None
                msg = err.get("message") if isinstance(err, dict) else str(err or resp.text[:200])
                attempts.append(f"{model}: HTTP {resp.status_code} {msg}"[:300])
                log.warning("Model %s faalde: HTTP %s %s", model, resp.status_code, (msg or "")[:200])
                if resp.status_code == 429:
                    await asyncio.sleep(1)
                continue

            text = _extract_text(data)
            if not text:
                attempts.append(f"{model}: leeg antwoord")
                log.warning("Model %s gaf een leeg antwoord", model)
                continue
            return AIResult(text=text, model=data.get("model") or model, attempts=attempts)

    raise AIError("all_failed", "Geen enkel model gaf antwoord", attempts)


# ---------- hulpfuncties voor het dashboard ----------

async def test_key(api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            resp = await client.get(f"{API_BASE}/key", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return False, f"Kon OpenRouter niet bereiken ({type(exc).__name__}). Heeft de server internet?"
    if resp.status_code == 401:
        return False, "OpenRouter kent deze API-key niet. Controleer of je hem helemaal hebt gekopieerd."
    if resp.status_code != 200:
        return False, f"OpenRouter gaf een onverwachte reactie (HTTP {resp.status_code})."
    info = (resp.json() or {}).get("data", {}) if resp.content else {}
    extra = ""
    if info.get("is_free_tier"):
        extra = " (gratis account: gratis modellen hebben een daglimiet)"
    label = info.get("label") or "zonder naam"
    return True, f"De API-key werkt ✓ (key: {label}){extra}"


async def list_free_vision_models(transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Haal de actuele gratis modellen op die afbeeldingen kunnen lezen."""
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        resp = await client.get(f"{API_BASE}/models")
        resp.raise_for_status()
        data = resp.json().get("data", [])
    out = []
    for m in data:
        pricing = m.get("pricing") or {}
        free = m.get("id", "").endswith(":free") or (
            str(pricing.get("prompt")) in ("0", "0.0") and str(pricing.get("completion")) in ("0", "0.0")
        )
        mods = (m.get("architecture") or {}).get("input_modalities") or []
        if free and "image" in mods:
            out.append({
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "context": m.get("context_length") or 0,
                "created": m.get("created") or 0,
            })
    out.sort(key=lambda x: (-x["created"], x["id"]))
    return out
