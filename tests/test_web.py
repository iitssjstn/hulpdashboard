import re

import pytest
from fastapi.testclient import TestClient

from app import auth, db, settings
from app.main import app

PW = "een-lang-wachtwoord"


@pytest.fixture
def client():
    auth.limiter.__init__()
    auth.setup_code = None
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def do_setup(c, finish=True):
    r = c.post("/setup/1", data={"code": auth.setup_code, "password": PW, "confirm": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/2"
    page = c.get("/setup/2").text
    token = csrf_of(page)
    if finish:
        for step in (2, 3, 4, 5):
            c.post(f"/setup/{step}", data={"csrf": token, "action": "skip"})
        c.post("/setup/6", data={"csrf": token})
    return token


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["bot"] == "niet ingesteld"


def test_first_visit_goes_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    assert "Kies een wachtwoord" in client.get("/setup").text


def test_setup_password_validation(client):
    r = client.post("/setup/1", data={"code": auth.setup_code, "password": "kort", "confirm": "kort"})
    assert "minstens 10" in r.text
    r = client.post("/setup/1", data={"code": auth.setup_code, "password": PW, "confirm": PW + "x"})
    assert "niet hetzelfde" in r.text
    assert not auth.has_password()


def test_setup_password_only_once(client):
    do_setup(client)
    client.cookies.clear()
    r = client.post("/setup/1", data={"code": auth.setup_code, "password": "nog-een-wachtwoord", "confirm": "nog-een-wachtwoord"})
    assert r.status_code == 403
    assert auth.verify_password(PW, settings.get("password_hash"))


def test_password_stored_as_hash(client):
    do_setup(client)
    stored = settings.get("password_hash")
    assert PW not in stored and stored.startswith("scrypt$")


def test_full_wizard_and_pages(client):
    token = do_setup(client, finish=False)
    client.post("/setup/2", data={"csrf": token, "secret": "discord-geheim-123"})
    assert settings.get_secret("discord_token") == "discord-geheim-123"
    client.post("/setup/3", data={"csrf": token, "secret": "sk-or-geheim-456"})
    r = client.post("/setup/4", data={"csrf": token, "guild_id": "abc"})
    assert "alleen cijfers" in r.text
    client.post("/setup/4", data={"csrf": token, "guild_id": "1", "help_channel_id": "2",
                                  "memory_channel_id": "3", "owner_user_id": "4"})
    assert settings.get("owner_user_id") == "4"
    client.post("/setup/5", data={"csrf": token, "naam": "Pa", "tv": "LG", "taal_pc": "Engels"})
    assert settings.get("profile")["tv"] == "LG"
    client.post("/setup/6", data={"csrf": token})
    assert settings.get("setup_done")
    for path in ("/gesprekken", "/geheugen", "/prompt", "/instellingen", "/status"):
        html = client.get(path).text
        assert "discord-geheim-123" not in html and "sk-or-geheim-456" not in html, path
    assert "ingesteld ✓" in client.get("/instellingen").text


def test_csrf_required(client):
    do_setup(client)
    r = client.post("/geheugen/toevoegen", data={"text": "Heeft een tv"})
    assert r.status_code == 403
    r = client.post("/geheugen/toevoegen", data={"text": "Heeft een tv", "csrf": "fout"})
    assert r.status_code == 403


def test_login_and_rate_limit(client):
    do_setup(client)
    client.cookies.clear()
    assert client.get("/gesprekken", follow_redirects=False).headers["location"] == "/login"
    for _ in range(5):
        assert "klopt niet" in client.post("/login", data={"password": "fout"}).text
    r = client.post("/login", data={"password": PW})
    assert "Te veel pogingen" in r.text
    auth.limiter.__init__()
    r = client.post("/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303 and auth.SESSION_COOKIE in r.headers.get("set-cookie", "")


def test_login_rejects_cross_origin(client):
    do_setup(client)
    client.cookies.clear()
    r = client.post("/login", data={"password": PW}, headers={"Origin": "https://evil.example"})
    assert "klopt niet" in r.text


def test_memory_crud(client):
    token = do_setup(client)
    client.post("/geheugen/toevoegen", data={"csrf": token, "text": "Heeft een Philips tv"})
    fid = db.query_one("SELECT id FROM facts")["id"]
    client.post(f"/geheugen/{fid}/bewerken", data={"csrf": token, "text": "Heeft een Philips Ambilight tv"})
    assert db.query_one("SELECT text FROM facts")["text"] == "Heeft een Philips Ambilight tv"
    db.execute("INSERT INTO facts (text,status,source,created_at,updated_at) VALUES ('Nieuw','pending','auto','x','x')")
    assert "Wacht op goedkeuring" in client.get("/geheugen").text
    client.post("/geheugen-alles-goedkeuren", data={"csrf": token})
    assert db.query_one("SELECT COUNT(*) n FROM facts WHERE status='approved'")["n"] == 2
    client.post(f"/geheugen/{fid}/verwijderen", data={"csrf": token})
    assert db.query_one("SELECT COUNT(*) n FROM facts")["n"] == 1


def test_prompt_edit_and_reset(client):
    token = do_setup(client)
    client.post("/prompt", data={"csrf": token, "naam": "Henk", "base_prompt": "Wees kort tegen {{naam}}."})
    html = client.get("/prompt").text
    assert "aangepast" in html and "Wees kort tegen Henk." in html
    client.post("/prompt/standaard", data={"csrf": token})
    assert settings.get("base_prompt") is None


def test_settings_save_and_password_change(client):
    token = do_setup(client)
    client.post("/instellingen/algemeen", data={
        "csrf": token, "model_primary": "x/y:free", "model_fallbacks": "a:free\nb:free",
        "history_messages": "99", "memory_mode": "auto", "sensitive_extra": "belasting\n"})
    assert settings.get("model_primary") == "x/y:free"
    assert settings.get("model_fallbacks") == ["a:free", "b:free"]
    assert settings.get("history_messages") == 50
    assert settings.get("memory_mode") == "auto"

    r = client.post("/instellingen/wachtwoord", data={"csrf": token, "current": "fout", "password": "nieuw-wachtwoord",
                                                      "confirm": "nieuw-wachtwoord"}, follow_redirects=False)
    assert "klopt" in r.headers["location"]
    client.post("/instellingen/wachtwoord", data={"csrf": token, "current": PW, "password": "nieuw-wachtwoord",
                                                  "confirm": "nieuw-wachtwoord"})
    assert auth.verify_password("nieuw-wachtwoord", settings.get("password_hash"))
    assert client.get("/instellingen", follow_redirects=False).status_code == 200  # nieuwe sessie


def test_replace_secret_restarts_bot(client, monkeypatch):
    token = do_setup(client)
    from app import main
    calls = []

    async def fake_restart():
        calls.append(1)
    monkeypatch.setattr(main.manager, "restart", fake_restart)
    client.post("/instellingen/geheim/discord_token", data={"csrf": token, "secret": "nieuw"})
    assert calls and settings.get_secret("discord_token") == "nieuw"
    client.post("/instellingen/geheim/openrouter_key", data={"csrf": token, "secret": "k2"})
    assert len(calls) == 1


def test_api_test_endpoint(client, monkeypatch):
    token = do_setup(client)
    from app import ai, discord_check

    async def fake_key(v):
        return v == "goed", "ok" if v == "goed" else "nee"

    async def fake_token(v, channels):
        return True, ["Token werkt ✓"], "https://discord.com/oauth2/authorize?client_id=1"
    monkeypatch.setattr(ai, "test_key", fake_key)
    monkeypatch.setattr(discord_check, "check_token", fake_token)
    h = {"X-CSRF-Token": token}
    assert client.post("/api/test/openrouter_key", json={"value": "goed"}, headers=h).json()["ok"]
    assert not client.post("/api/test/openrouter_key", json={"value": "slecht"}, headers=h).json()["ok"]
    assert client.post("/api/test/discord_token", json={"value": "t"}, headers=h).json()["invite"]
    assert client.post("/api/test/openrouter_key", json={"value": "x"}).status_code == 403


def test_conversation_search_and_images(client):
    do_setup(client)
    from app import core, paths
    paths.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    (paths.IMAGE_DIR / ("a" * 32 + ".jpg")).write_bytes(b"\xff\xd8jpeg")
    core._store("1", "Pa", "9", "OBS geeft dropped frames", "Verlaag je bitrate", ["a" * 32 + ".jpg"], "m", "ok")
    core._store("1", "Pa", "9", "Tv zwart", "Check de stekker", [], "m", "ok")
    html = client.get("/gesprekken?q=bitrate").text
    assert "dropped frames" in html and "Tv zwart" not in html
    assert client.get("/afbeeldingen/" + "a" * 32 + ".jpg").status_code == 200
    assert client.get("/afbeeldingen/..%2Fhulp.sqlite3").status_code == 404
    client.cookies.clear()
    assert client.get("/afbeeldingen/" + "a" * 32 + ".jpg", follow_redirects=False).status_code == 303


def test_security_headers(client):
    r = client.get("/setup")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


def test_login_accepts_same_origin_header(client):
    # Browsers sturen Origin mee bij een POST; die moet geaccepteerd worden.
    r = client.post("/setup/1", data={"code": auth.setup_code, "password": PW, "confirm": PW},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()
    r = client.post("/login", data={"password": PW}, headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/login", data={"password": PW}, headers={"Origin": "null"})
    assert "klopt niet" in r.text


def test_referrer_policy_keeps_origin(client):
    assert client.get("/setup").headers["referrer-policy"] == "same-origin"


def test_setup_code_required(client):
    code = auth.setup_code
    assert code and len(code) == 14  # XXXX-XXXX-XXXX
    r = client.post("/setup/1", data={"code": "", "password": PW, "confirm": PW})
    assert "setupcode klopt niet" in r.text and not auth.has_password()
    r = client.post("/setup/1", data={"code": "AAAA-BBBB-CCCC", "password": PW, "confirm": PW})
    assert "setupcode klopt niet" in r.text and not auth.has_password()
    # Hoofdletters/streepjes/spaties maken niet uit.
    r = client.post("/setup/1", data={"code": " " + code.replace("-", "").lower() + " ", "password": PW,
                                      "confirm": PW}, follow_redirects=False)
    assert r.status_code == 303 and auth.has_password()
    assert auth.setup_code is None  # code is vervallen


def test_setup_code_guessing_is_rate_limited(client):
    for _ in range(5):
        client.post("/setup/1", data={"code": "fout", "password": PW, "confirm": PW})
    r = client.post("/setup/1", data={"code": auth.setup_code, "password": PW, "confirm": PW})
    assert "Te veel pogingen" in r.text and not auth.has_password()


def test_setup_code_not_in_database_or_page(client):
    code = auth.setup_code
    assert code not in client.get("/setup").text
    assert db.query_one("SELECT COUNT(*) n FROM logs WHERE message LIKE ?", (f"%{code}%",))["n"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM settings WHERE value LIKE ?", (f"%{code}%",))["n"] == 0


def test_no_setup_code_when_password_exists(client):
    do_setup(client)
    assert auth.ensure_setup_code() is None
