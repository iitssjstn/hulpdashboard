"""FastAPI-dashboard + Discord-bot in één proces."""
from __future__ import annotations

import hmac
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, auth, crypto, db, discord_check, logsetup, memory, paths, prompt, settings, tickets
from .bot import manager

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TZ = ZoneInfo("Europe/Amsterdam")
PAGE_SIZE = 30
SETUP_STEPS = ["Wachtwoord", "Discord-bot", "OpenRouter", "Server & kanalen", "Over je vader", "Klaar"]

MESSAGES = {
    "gesloten": ("ok", "Ticket afgesloten ✓"),
    "heropend": ("ok", "Ticket weer geopend ✓"),
    "opgeslagen": ("ok", "Opgeslagen ✓"),
    "wachtwoord": ("ok", "Je wachtwoord is gewijzigd ✓ Andere apparaten zijn uitgelogd."),
    "geheim": ("ok", "Opgeslagen ✓ De bot gebruikt het nieuwe geheim direct."),
    "herverbinden": ("ok", "De bot verbindt opnieuw met Discord…"),
    "toegevoegd": ("ok", "Feit toegevoegd ✓"),
    "geweigerd": ("fout", "Niet opgeslagen: leeg, dubbel, of er stond een wachtwoord/bankgegeven in."),
    "verwijderd": ("ok", "Verwijderd ✓"),
    "standaard": ("ok", "De standaardprompt is teruggezet ✓"),
}


# ---------- opstarten ----------

def init_storage() -> None:
    db.init()
    crypto.init()


def announce_setup_code() -> None:
    code = auth.ensure_setup_code()
    if code:
        # Alleen naar stdout (INFO komt niet in de database), zodat hij in 'docker compose logs' staat.
        line = "=" * 60
        log.info("\n%s\n  EERSTE KEER INSTELLEN\n  Open het dashboard en vul deze setupcode in:\n\n"
                 "      %s\n\n  (De code is alleen geldig tot je een wachtwoord hebt gekozen.)\n%s",
                 line, code, line)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logsetup.setup()
    init_storage()
    log.info("Hulpdashboard versie %s gestart", paths.VERSION)
    announce_setup_code()
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso).astimezone(TZ)
    except ValueError:
        return iso
    months = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
    return f"{dt.day} {months[dt.month - 1]} {dt.year} {dt:%H:%M}"


def fmt_epoch(ts: float) -> str:
    return fmt_time(datetime.fromtimestamp(ts, timezone.utc).isoformat())


templates.env.filters["tijd"] = fmt_time
templates.env.filters["epoch"] = fmt_epoch
templates.env.filters["fromjson"] = lambda v: db.get_json(v, [])
templates.env.globals["version"] = paths.VERSION


def fmt_mentions(text: str | None) -> str:
    """Discord-mentions (<@123>) leesbaar maken in het dashboard."""
    owner = str(settings.get("owner_user_id") or "")
    name = (settings.get("profile").get("eigenaar_naam") or "jij").strip() or "jij"
    return re.sub(r"<@!?(\d+)>", lambda m: f"@{name}" if m.group(1) == owner else "@iemand", text or "")


templates.env.filters["mentions"] = fmt_mentions


# ---------- beveiliging ----------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


class NeedLogin(Exception):
    pass


class NeedSetup(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    if not auth.has_password():
        return RedirectResponse("/setup", 303)
    return RedirectResponse("/login", 303)


@app.exception_handler(NeedSetup)
async def _need_setup(request: Request, exc: NeedSetup):
    return RedirectResponse(f"/setup/{settings.get('setup_step') or 2}", 303)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "onbekend"


def same_origin(request: Request) -> bool:
    """Voor formulieren zonder sessie (login, eerste wachtwoord): Origin moet kloppen."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin or origin == "null":
        return origin is None  # oude browsers sturen niets mee; 'null' weigeren
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlparse(origin).netloc == host


def require_session(request: Request):
    sess = auth.get_session(request.cookies.get(auth.SESSION_COOKIE))
    if sess is None:
        raise NeedLogin()
    request.state.csrf = sess["csrf"]
    return sess


def require_ready(request: Request):
    sess = require_session(request)
    if not settings.get("setup_done"):
        raise NeedSetup()
    return sess


async def check_csrf(request: Request, sess) -> None:
    token = request.headers.get("x-csrf-token")
    if token is None:
        form = await request.form()
        token = form.get("csrf")
    if not token or not hmac.compare_digest(str(token), sess["csrf"]):
        raise HTTPException(403, "Ongeldig formulier. Ververs de pagina en probeer opnieuw.")


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def login_response(request: Request, target: str) -> RedirectResponse:
    token = auth.create_session()
    resp = RedirectResponse(target, 303)
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="strict", secure=is_https(request), path="/")
    return resp


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    msg = MESSAGES.get(request.query_params.get("m", ""))
    ctx.setdefault("flash", msg)
    ctx.setdefault("csrf", getattr(request.state, "csrf", ""))
    ctx.setdefault("bot", manager.info())
    if ctx.get("active"):
        ctx.setdefault("waiting", tickets.counts().get("wacht_op_jou", 0))
    return templates.TemplateResponse(request, name, ctx)


# ---------- gezondheid ----------

@app.get("/healthz")
async def healthz():
    ok = manager.healthy()
    return JSONResponse({"web": "ok", "bot": manager.status, "version": paths.VERSION},
                        status_code=200 if ok else 503)


# ---------- inloggen ----------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not auth.has_password():
        return RedirectResponse("/setup", 303)
    return render(request, "login.html", error=None)


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = client_ip(request)
    wait = auth.limiter.check(ip)
    if wait:
        return render(request, "login.html",
                      error=f"Te veel pogingen. Probeer het over {max(1, wait // 60)} minuten opnieuw.")
    if not same_origin(request) or not auth.verify_password(password, settings.get("password_hash")):
        auth.limiter.fail(ip)
        log.warning("Mislukte inlogpoging vanaf %s", ip)
        return render(request, "login.html", error="Dat wachtwoord klopt niet.")
    auth.limiter.success(ip)
    return login_response(request, "/")


@app.post("/logout")
async def logout(request: Request):
    sess = require_session(request)
    await check_csrf(request, sess)
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ---------- setup-wizard ----------

@app.get("/setup", response_class=HTMLResponse)
async def setup_start(request: Request):
    if auth.has_password():
        sess = auth.get_session(request.cookies.get(auth.SESSION_COOKIE))
        if sess is None:
            return RedirectResponse("/login", 303)
        if settings.get("setup_done"):
            return RedirectResponse("/", 303)
        return RedirectResponse(f"/setup/{max(2, settings.get('setup_step') or 2)}", 303)
    return render(request, "setup.html", step=1, steps=SETUP_STEPS, error=None)


@app.post("/setup/1")
async def setup_password(request: Request, code: str = Form(""), password: str = Form(""),
                         confirm: str = Form("")):
    if auth.has_password():
        raise HTTPException(403, "Er is al een wachtwoord ingesteld.")
    if not same_origin(request):
        raise HTTPException(403, "Ongeldig formulier.")
    ip = client_ip(request)
    wait = auth.limiter.check(ip)
    if wait:
        return render(request, "setup.html", step=1, steps=SETUP_STEPS,
                      error=f"Te veel pogingen. Probeer het over {max(1, wait // 60)} minuten opnieuw.")
    if not auth.check_setup_code(code):
        auth.limiter.fail(ip)
        log.warning("Verkeerde setupcode ingevuld vanaf %s", ip)
        return render(request, "setup.html", step=1, steps=SETUP_STEPS,
                      error="Die setupcode klopt niet. Kijk in de logs van de container (zie uitleg).")
    problem = auth.password_problem(password, confirm)
    if problem:
        return render(request, "setup.html", step=1, steps=SETUP_STEPS, error=problem)
    settings.set("password_hash", auth.hash_password(password))
    auth.limiter.success(ip)
    auth.ensure_setup_code()  # code vervalt
    settings.set("setup_step", 2)
    log.info("Dashboardwachtwoord ingesteld")
    return login_response(request, "/setup/2")


@app.get("/setup/{step}", response_class=HTMLResponse)
async def setup_step_page(request: Request, step: int):
    require_session(request)
    if step < 2 or step > len(SETUP_STEPS):
        return RedirectResponse("/setup", 303)
    return render(request, "setup.html", step=step, steps=SETUP_STEPS, error=None, **setup_context())


def setup_context() -> dict:
    return {
        "has_discord": settings.has_secret("discord_token"),
        "has_openrouter": settings.has_secret("openrouter_key"),
        "s": {k: settings.get(k) for k in ("guild_id", "help_channel_id", "memory_channel_id",
                                           "owner_user_id")},
        "profile": settings.get("profile"),
        "fields": settings.PROFILE_FIELDS,
    }


@app.post("/setup/{step}")
async def setup_step_save(request: Request, step: int):
    sess = require_session(request)
    await check_csrf(request, sess)
    form = await request.form()
    skip = form.get("action") == "skip"
    if step == 2 and not skip:
        token = (form.get("secret") or "").strip()
        if token:
            settings.set_secret("discord_token", token)
            await manager.restart()
    elif step == 3 and not skip:
        key = (form.get("secret") or "").strip()
        if key:
            settings.set_secret("openrouter_key", key)
    elif step == 4 and not skip:
        error = save_ids(form)
        if error:
            return render(request, "setup.html", step=4, steps=SETUP_STEPS, error=error, **setup_context())
    elif step == 5 and not skip:
        save_profile(form)
    elif step == 6:
        settings.set("setup_done", True)
        return RedirectResponse("/status", 303)
    nxt = min(step + 1, len(SETUP_STEPS))
    settings.set("setup_step", nxt)
    return RedirectResponse(f"/setup/{nxt}", 303)


ID_FIELDS = {
    "guild_id": "Server-ID",
    "help_channel_id": "Kanaal-ID #hulp",
    "memory_channel_id": "Kanaal-ID #geheugen",
    "owner_user_id": "Jouw user ID",
    "owner_channel_id": "Kanaal-ID meldingen",
}


def save_ids(form) -> str | None:
    values = {}
    for key, label in ID_FIELDS.items():
        v = re.sub(r"\s", "", str(form.get(key) or ""))
        if v and not v.isdigit():
            return f"{label} mag alleen cijfers bevatten (rechtsklik → 'ID kopiëren' in Discord)."
        values[key] = v
    for k, v in values.items():
        settings.set(k, v)
    return None


def save_profile(form) -> None:
    profile = settings.get("profile")
    for key, _, _ in settings.PROFILE_FIELDS:
        if key in form:
            profile[key] = str(form.get(key) or "").strip()[:2000]
    settings.set("profile", profile)


# ---------- pagina's ----------

@app.get("/")
async def home(request: Request):
    require_ready(request)
    return RedirectResponse("/tickets" if tickets.enabled() else "/gesprekken", 303)


@app.get("/gesprekken", response_class=HTMLResponse)
async def gesprekken(request: Request, q: str = "", page: int = 1):
    require_ready(request)
    page = max(1, page)
    where, params = "", []
    if q.strip():
        where = "WHERE question LIKE ? OR answer LIKE ? OR user_name LIKE ? OR model LIKE ?"
        like = f"%{q.strip()}%"
        params = [like, like, like, like]
    total = db.query_one(f"SELECT COUNT(*) AS n FROM conversations {where}", tuple(params))["n"]
    rows = db.query(f"SELECT * FROM conversations {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    tuple(params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(request, "gesprekken.html", rows=rows, q=q, page=page, pages=pages, total=total,
                  active="gesprekken")


# ----- tickets -----

TICKET_FILTERS = {"open": ("open", "wacht_op_jou"), "wacht_op_jou": ("wacht_op_jou",), "opgelost": ("opgelost",),
                  "alle": ("open", "wacht_op_jou", "opgelost")}


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request, status: str = "open", q: str = "", page: int = 1):
    require_ready(request)
    statuses = TICKET_FILTERS.get(status, TICKET_FILTERS["open"])
    page = max(1, page)
    where = f"WHERE t.status IN ({','.join('?' * len(statuses))})"
    params: list = list(statuses)
    if q.strip():
        like = f"%{q.strip()}%"
        where += (" AND (t.title LIKE ? OR t.summary LIKE ? OR EXISTS (SELECT 1 FROM conversations c "
                  "WHERE c.ticket_id=t.id AND (c.question LIKE ? OR c.answer LIKE ?)))")
        params += [like, like, like, like]
    total = db.query_one(f"SELECT COUNT(*) AS n FROM tickets t {where}", tuple(params))["n"]
    rows = db.query(
        f"SELECT t.*, (SELECT COUNT(*) FROM conversations c WHERE c.ticket_id=t.id) AS n_msgs, "
        f"(SELECT question FROM conversations c WHERE c.ticket_id=t.id ORDER BY id LIMIT 1) AS first_q "
        f"FROM tickets t {where} ORDER BY CASE t.status WHEN 'wacht_op_jou' THEN 0 WHEN 'open' THEN 1 ELSE 2 END, "
        f"t.updated_at DESC LIMIT ? OFFSET ?", tuple(params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(request, "tickets.html", rows=rows, status=status if status in TICKET_FILTERS else "open",
                  q=q, page=page, pages=pages, total=total, counts=tickets.counts(), enabled=tickets.enabled(),
                  icons=tickets.STATUS_ICON, labels=tickets.STATUS_LABEL, active="tickets")


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail(request: Request, ticket_id: int):
    require_ready(request)
    t = tickets.get(ticket_id)
    if t is None:
        raise HTTPException(404, "Ticket niet gevonden")
    rows = db.query("SELECT * FROM conversations WHERE ticket_id=? ORDER BY id", (ticket_id,))
    link = (f"https://discord.com/channels/{t['guild_id']}/{t['thread_id']}"
            if t["guild_id"] and t["thread_id"] else None)
    return render(request, "ticket.html", t=t, rows=rows, link=link, icons=tickets.STATUS_ICON,
                  labels=tickets.STATUS_LABEL, active="tickets")


@app.post("/tickets/{ticket_id}/{action}")
async def ticket_action(request: Request, ticket_id: int, action: str):
    sess = require_ready(request)
    await check_csrf(request, sess)
    if tickets.get(ticket_id) is None:
        raise HTTPException(404)
    if action == "sluiten":
        await manager.close_ticket(ticket_id)
        m = "gesloten"
    elif action == "heropenen":
        await manager.reopen_ticket(ticket_id)
        m = "heropend"
    else:
        raise HTTPException(404)
    return RedirectResponse(f"/tickets/{ticket_id}?m={m}", 303)


@app.get("/afbeeldingen/{name}")
async def afbeelding(request: Request, name: str):
    require_session(request)
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", name):
        raise HTTPException(404)
    path = paths.IMAGE_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ----- geheugen -----

@app.get("/geheugen", response_class=HTMLResponse)
async def geheugen(request: Request):
    require_ready(request)
    budget = settings.get_int("memory_budget_chars", 200, 20000)
    approved = memory.approved_facts()
    used = sum(len(r["text"]) + 3 for r in approved)
    rejected = db.query("SELECT * FROM facts WHERE status='rejected' ORDER BY id DESC LIMIT 20")
    return render(request, "geheugen.html", approved=approved, pending=memory.pending_facts(),
                  rejected=rejected, budget=budget, used=used, mode=settings.get("memory_mode"),
                  active="geheugen")


@app.post("/geheugen/toevoegen")
async def geheugen_toevoegen(request: Request, text: str = Form("")):
    sess = require_ready(request)
    await check_csrf(request, sess)
    fid = memory.add_fact(text, "approved", "handmatig")
    return RedirectResponse("/geheugen?m=" + ("toegevoegd" if fid else "geweigerd"), 303)


@app.post("/geheugen/{fact_id}/{action}")
async def geheugen_actie(request: Request, fact_id: int, action: str):
    sess = require_ready(request)
    await check_csrf(request, sess)
    form = await request.form()
    m = "opgeslagen"
    if action == "goedkeuren":
        memory.set_status(fact_id, "approved")
    elif action == "afwijzen":
        memory.set_status(fact_id, "rejected")
    elif action == "verwijderen":
        memory.delete_fact(fact_id)
        m = "verwijderd"
    elif action == "bewerken":
        if not memory.update_text(fact_id, str(form.get("text") or "")):
            m = "geweigerd"
    else:
        raise HTTPException(404)
    return RedirectResponse(f"/geheugen?m={m}", 303)


@app.post("/geheugen-alles-goedkeuren")
async def geheugen_alles(request: Request):
    sess = require_ready(request)
    await check_csrf(request, sess)
    for r in memory.pending_facts():
        memory.set_status(r["id"], "approved")
    return RedirectResponse("/geheugen?m=opgeslagen", 303)


# ----- systeemprompt -----

@app.get("/prompt", response_class=HTMLResponse)
async def prompt_page(request: Request):
    require_ready(request)
    base = settings.get("base_prompt") or prompt.DEFAULT_BASE_PROMPT
    return render(request, "prompt.html", profile=settings.get("profile"), fields=settings.PROFILE_FIELDS,
                  base=base, is_default=not settings.get("base_prompt"),
                  preview=prompt.build_system_prompt(), active="prompt")


@app.post("/prompt")
async def prompt_save(request: Request):
    sess = require_ready(request)
    await check_csrf(request, sess)
    form = await request.form()
    save_profile(form)
    base = str(form.get("base_prompt") or "").replace("\r\n", "\n").strip()
    if base and base != prompt.DEFAULT_BASE_PROMPT.strip():
        settings.set("base_prompt", base[:20000])
    elif base:
        settings.set("base_prompt", None)
    return RedirectResponse("/prompt?m=opgeslagen", 303)


@app.post("/prompt/standaard")
async def prompt_reset(request: Request):
    sess = require_ready(request)
    await check_csrf(request, sess)
    settings.set("base_prompt", None)
    return RedirectResponse("/prompt?m=standaard", 303)


# ----- instellingen -----

@app.get("/instellingen", response_class=HTMLResponse)
async def instellingen(request: Request):
    require_ready(request)
    s = {k: settings.get(k) for k in settings.DEFAULTS if k not in ("password_hash", "profile", "base_prompt")}
    return render(request, "instellingen.html", s=s, active="instellingen", error=request.query_params.get("e"),
                  has_discord=settings.has_secret("discord_token"),
                  has_openrouter=settings.has_secret("openrouter_key"))


@app.post("/instellingen/algemeen")
async def instellingen_algemeen(request: Request):
    sess = require_ready(request)
    await check_csrf(request, sess)
    form = await request.form()
    error = save_ids(form)
    if error:
        return RedirectResponse("/instellingen?e=" + error, 303)
    primary = str(form.get("model_primary") or "").strip()
    if primary:
        settings.set("model_primary", primary[:200])
    fallbacks = [m.strip() for m in re.split(r"[\n,]", str(form.get("model_fallbacks") or "")) if m.strip()]
    settings.set("model_fallbacks", fallbacks[:10])
    for key, lo, hi in (("history_messages", 0, 50), ("history_hours", 1, 720),
                        ("memory_idle_minutes", 1, 240), ("memory_budget_chars", 200, 20000)):
        try:
            settings.set(key, max(lo, min(hi, int(str(form.get(key) or settings.get(key))))))
        except ValueError:
            pass
    if form.get("ticket_mode") in ("uit", "openbaar", "prive"):
        settings.set("ticket_mode", form.get("ticket_mode"))
    try:
        settings.set("ticket_followup_minutes", max(0, min(240, int(str(form.get("ticket_followup_minutes"))))))
    except ValueError:
        pass
    settings.set("ticket_ai_titles", form.get("ticket_ai_titles") == "1")
    if form.get("memory_mode") in ("auto", "review"):
        settings.set("memory_mode", form.get("memory_mode"))
    extra = [w.strip() for w in str(form.get("sensitive_extra") or "").splitlines() if w.strip()]
    settings.set("sensitive_extra", extra[:100])
    return RedirectResponse("/instellingen?m=opgeslagen", 303)


@app.post("/instellingen/geheim/{key}")
async def instellingen_geheim(request: Request, key: str, secret: str = Form("")):
    sess = require_ready(request)
    await check_csrf(request, sess)
    if key not in settings.SECRET_KEYS:
        raise HTTPException(404)
    if not secret.strip():
        return RedirectResponse("/instellingen?e=Vul eerst een nieuwe waarde in.", 303)
    settings.set_secret(key, secret)
    log.warning("Geheim '%s' is vervangen via het dashboard", key)
    if key == "discord_token":
        await manager.restart()
    return RedirectResponse("/instellingen?m=geheim", 303)


@app.post("/instellingen/wachtwoord")
async def instellingen_wachtwoord(request: Request, current: str = Form(""), password: str = Form(""),
                                  confirm: str = Form("")):
    sess = require_ready(request)
    await check_csrf(request, sess)
    ip = client_ip(request)
    if auth.limiter.check(ip):
        return RedirectResponse("/instellingen?e=Te veel pogingen, probeer het later opnieuw.", 303)
    if not auth.verify_password(current, settings.get("password_hash")):
        auth.limiter.fail(ip)
        return RedirectResponse("/instellingen?e=Je huidige wachtwoord klopt niet.", 303)
    problem = auth.password_problem(password, confirm)
    if problem:
        return RedirectResponse("/instellingen?e=" + problem, 303)
    settings.set("password_hash", auth.hash_password(password))
    auth.delete_all_sessions()
    log.warning("Dashboardwachtwoord gewijzigd")
    return login_response(request, "/instellingen?m=wachtwoord")


@app.post("/bot/herverbinden")
async def bot_herverbinden(request: Request):
    sess = require_session(request)
    await check_csrf(request, sess)
    await manager.restart()
    return RedirectResponse("/status?m=herverbinden", 303)


# ----- API voor de Test-knoppen -----

@app.post("/api/test/{key}")
async def api_test(request: Request, key: str):
    sess = require_session(request)
    await check_csrf(request, sess)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    value = str(body.get("value") or "").strip() or settings.get_secret(key) or ""
    if key not in settings.SECRET_KEYS:
        raise HTTPException(404)
    if not value:
        return {"ok": False, "lines": ["Er is nog niets ingevuld om te testen."]}
    if key == "openrouter_key":
        ok, msg = await ai.test_key(value)
        return {"ok": ok, "lines": [msg]}
    channels = {ID_FIELDS[k]: str(body.get(k) or settings.get(k) or "")
                for k in ("help_channel_id", "memory_channel_id")}
    ok, lines, invite = await discord_check.check_token(value, channels)
    return {"ok": ok, "lines": lines, "invite": invite}


@app.post("/api/modellen")
async def api_modellen(request: Request):
    sess = require_session(request)
    await check_csrf(request, sess)
    try:
        models = await ai.list_free_vision_models()
    except Exception as exc:  # noqa: BLE001
        log.warning("Modellijst ophalen mislukt: %s", type(exc).__name__)
        return {"ok": False, "models": [], "error": "Kon de modellijst niet ophalen bij OpenRouter."}
    return {"ok": True, "models": models[:40]}


# ----- status -----

@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    require_ready(request)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    usage = db.query("SELECT COALESCE(model, '(geen)') AS model, status, COUNT(*) AS n FROM conversations "
                     "WHERE created_at >= ? GROUP BY model, status ORDER BY n DESC", (since,))
    last = db.query("SELECT id, created_at, user_name, model, status, error FROM conversations "
                    "ORDER BY id DESC LIMIT 15")
    logs = db.query("SELECT * FROM logs ORDER BY id DESC LIMIT 60")
    counts = {
        "gesprekken": db.query_one("SELECT COUNT(*) AS n FROM conversations")["n"],
        "feiten": db.query_one("SELECT COUNT(*) AS n FROM facts WHERE status='approved'")["n"],
        "wachtend": db.query_one("SELECT COUNT(*) AS n FROM facts WHERE status='pending'")["n"],
    }
    return render(request, "status.html", usage=usage, last=last, logs=logs, counts=counts,
                  models=settings.models(), has_discord=settings.has_secret("discord_token"),
                  has_openrouter=settings.has_secret("openrouter_key"), active="status")
