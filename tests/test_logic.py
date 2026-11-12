import asyncio
import io
import json

import httpx
import pytest
from PIL import Image

from app import ai, core, db, memory, prompt, sensitive, settings


def run(coro):
    return asyncio.run(coro)


# ---------- gevoelig ----------

@pytest.mark.parametrize("text", [
    "Ik krijg een sms dat ik mijn pakketje moet betalen",
    "Hoe verander ik mijn wachtwoord van Netflix?",
    "Is deze mail van de bank echt?",
    "Hoe zeg ik Videoland op?",
    "Ik wil mijn abonnement opzeggen",
    "Er belde iemand van Microsoft die wil meekijken op mijn pc",
])
def test_detect_sensitive(text):
    assert sensitive.detect(text)


@pytest.mark.parametrize("text", [
    "Mijn tv heeft geen geluid",
    "Hoe voeg ik een camera toe in OBS?",
    "Hoe start ik een raid op Twitch?",
    "StreamElements alert werkt niet bij een nieuwe volger",
])
def test_not_sensitive(text):
    assert sensitive.detect(text) is None


def test_extra_sensitive_words():
    settings.set("sensitive_extra", ["belastingdienst"])
    assert sensitive.detect("Brief van de Belastingdienst") == "extra"


def test_redact():
    assert "0417164300" not in sensitive.redact("Mijn iban is NL91ABNA0417164300")
    assert "NL91 ABNA" not in sensitive.redact("rekening NL91 ABNA 0417 1643 00 graag")
    assert "Geheim123" not in sensitive.redact("mijn wachtwoord is Geheim123")
    assert "4321" not in sensitive.redact("pincode: 4321")
    assert "live_123456789_abcdefghijklmnop" not in sensitive.redact("key live_123456789_abcdefghijklmnop")
    # Niets wegpoetsen wat geen gevoelige gegevens zijn:
    for ok in ["Samsung QE55Q80AATXXN", "wachtwoord vergeten van mijn mail", "OBS 30.2 met 6000 kbps",
               "LG OLED55C3"]:
        assert sensitive.redact(ok) == ok


# ---------- geheugen ----------

def test_facts_budget_and_forget():
    for i in range(30):
        memory.add_fact(f"Feit nummer {i} over een apparaat met een lange beschrijving", "approved", "test")
    text = memory.facts_for_prompt(400)
    assert len(text) < 520
    assert "Feit nummer 29" in text and "weggelaten" in text

    res, rows = memory.forget("1")
    assert res == "deleted" and rows[0]["text"].startswith("Feit nummer 0 ")
    res, rows = memory.forget("Feit nummer 1")  # matcht 1, 10..19
    assert res == "multiple"
    res, _ = memory.forget("nummer 29 over")
    assert res == "deleted"


def test_add_fact_rejects_sensitive_and_duplicates():
    assert memory.add_fact("Zijn wachtwoord is Welkom123!", "approved", "t") is None
    assert memory.add_fact("Heeft een Samsung tv", "approved", "t")
    assert memory.add_fact("heeft een samsung tv.", "approved", "t") is None


def test_parse_facts_json():
    assert memory.parse_facts_json('bla {"feiten": ["Heeft een LG tv", 5]} bla') == ["Heeft een LG tv"]
    assert memory.parse_facts_json("geen json") == []
    assert memory.parse_facts_json('{"feiten": []}') == []


# ---------- prompt ----------

def test_system_prompt_uses_profile_and_language():
    settings.set("profile", {"naam": "Henk", "taal_pc": "Engels", "tv": "Philips 55OLED"})
    memory.add_fact("Gebruikt Chrome", "approved", "t")
    memory.add_fact("Wacht nog", "pending", "t")
    p = prompt.build_system_prompt()
    assert "Henk" in p and "Engels" in p and "Philips 55OLED" in p
    assert "Gebruikt Chrome" in p and "Wacht nog" not in p
    assert "{{" not in p
    assert "(onbekend" in p  # lege velden: vragen in plaats van gokken


# ---------- AI met fallback ----------

def _transport(responses):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body["model"])
        status, data = responses[body["model"]]
        return httpx.Response(status, json=data)

    return httpx.MockTransport(handler), calls


def _ok(text, model):
    return {"model": model, "choices": [{"message": {"content": text}}]}


def test_ai_fallback_on_rate_limit_and_empty():
    t, calls = _transport({
        "a": (429, {"error": {"message": "rate limited"}}),
        "b": (200, _ok("", "b")),
        "c": (200, _ok("Hallo!", "c")),
    })
    r = run(ai.chat([{"role": "user", "content": "hoi"}], models=["a", "b", "c"], api_key="k", transport=t))
    assert r.text == "Hallo!" and r.model == "c" and calls == ["a", "b", "c"] and len(r.attempts) == 2


def test_ai_bad_key_stops():
    t, calls = _transport({"a": (401, {"error": {"message": "no"}}), "b": (200, _ok("x", "b"))})
    with pytest.raises(ai.AIError) as exc:
        run(ai.chat([], models=["a", "b"], api_key="k", transport=t))
    assert exc.value.kind == "bad_key" and calls == ["a"]


def test_ai_all_failed():
    t, _ = _transport({"a": (503, {}), "b": (200, {"error": {"message": "provider error", "code": 502}})})
    with pytest.raises(ai.AIError) as exc:
        run(ai.chat([], models=["a", "b"], api_key="k", transport=t))
    assert exc.value.kind == "all_failed"


def test_ai_no_key():
    with pytest.raises(ai.AIError) as exc:
        run(ai.chat([]))
    assert exc.value.kind == "no_key"


def test_list_free_vision_models():
    data = {"data": [
        {"id": "x/vision:free", "architecture": {"input_modalities": ["text", "image"]}, "created": 2},
        {"id": "x/text:free", "architecture": {"input_modalities": ["text"]}},
        {"id": "x/paid", "pricing": {"prompt": "0.1", "completion": "0.1"},
         "architecture": {"input_modalities": ["text", "image"]}},
    ]}
    t = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    assert [m["id"] for m in run(ai.list_free_vision_models(transport=t))] == ["x/vision:free"]


def test_test_key():
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"label": "bot", "is_free_tier": True}}))
    ok, msg = run(ai.test_key("k", transport=t))
    assert ok and "werkt" in msg
    t = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    ok, msg = run(ai.test_key("k", transport=t))
    assert not ok


# ---------- vragen beantwoorden ----------

@pytest.fixture
def fake_ai(monkeypatch):
    calls = []

    async def fake_chat(messages, **kw):
        calls.append(messages)
        return ai.AIResult(text=fake_chat.reply, model="test/model:free")

    fake_chat.reply = "1. Druk op de knop."
    monkeypatch.setattr(ai, "chat", fake_chat)
    return fake_chat, calls


def test_sensitive_question_refers_without_ai(fake_ai):
    _, calls = fake_ai
    settings.set("owner_user_id", "12345")
    r = run(core.answer_question("1", "Pa", "9", "Ik moet mijn pakketje betalen via deze link"))
    assert r.status == "doorverwezen" and r.mention_owner and "<@12345>" in r.text
    assert "kun je hier even naar kijken?" in r.text
    assert calls == []


def test_normal_question_with_history(fake_ai):
    fake, calls = fake_ai
    run(core.answer_question("1", "Pa", "9", "Mijn tv doet het niet"))
    fake.reply = "Welke tv heb je?"
    r = run(core.answer_question("1", "Pa", "9", "Hij blijft zwart"))
    assert r.status == "ok"
    msgs = calls[-1]
    assert msgs[0]["role"] == "system"
    assert {"role": "user", "content": "Mijn tv doet het niet"} in msgs
    assert msgs[-1] == {"role": "user", "content": "Hij blijft zwart"}
    row = db.query_one("SELECT * FROM conversations ORDER BY id DESC")
    assert row["model"] == "test/model:free" and row["answer"] == "Welke tv heb je?"


def test_history_limit(fake_ai):
    _, calls = fake_ai
    settings.set("history_messages", 2)
    for i in range(5):
        run(core.answer_question("1", "Pa", "9", f"vraag {i}"))
    user_msgs = [m for m in calls[-1] if m["role"] == "user"]
    assert [m["content"] for m in user_msgs] == ["vraag 2", "vraag 3", "vraag 4"]


def test_model_marker_triggers_referral(fake_ai):
    fake, _ = fake_ai
    fake.reply = "Klik nergens op.\n[[DOORVERWIJZEN]]"
    settings.set("owner_user_id", "777")
    r = run(core.answer_question("1", "Pa", "9", "Klopt deze schermafbeelding?"))
    assert r.status == "doorverwezen" and "[[" not in r.text and "<@777>" in r.text


def test_redacts_before_sending(fake_ai):
    _, calls = fake_ai
    run(core.answer_question("1", "Pa", "9", "de code: 9876 van de tv-app werkt niet"))
    sent = calls[-1][-1]["content"]
    assert "9876" not in sent
    assert "9876" not in db.query_one("SELECT question FROM conversations")["question"]


def test_ai_error_gives_friendly_message(monkeypatch):
    async def boom(*a, **k):
        raise ai.AIError("all_failed", "x", ["a: 429"])
    monkeypatch.setattr(ai, "chat", boom)
    r = run(core.answer_question("1", "Pa", "9", "Hoe zet ik mijn camera aan?"))
    assert r.status == "fout" and "Traceback" not in r.text and "Sorry" in r.text

    async def crash(*a, **k):
        raise ZeroDivisionError("kapot")
    monkeypatch.setattr(ai, "chat", crash)
    r = run(core.answer_question("1", "Pa", "9", "nog een vraag"))
    assert r.status == "fout" and "ZeroDivision" not in r.text


def test_image_is_processed_and_sent(fake_ai):
    _, calls = fake_ai
    buf = io.BytesIO()
    Image.new("RGBA", (3000, 2000), (255, 0, 0, 128)).save(buf, "PNG")
    r = run(core.answer_question("1", "Pa", "9", "", [core.IncomingImage(buf.getvalue(), "s.png")]))
    assert r.status == "ok"
    content = calls[-1][-1]["content"]
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"].startswith("data:image/jpeg")
    name = json.loads(db.query_one("SELECT images FROM conversations")["images"])[0]
    from app import paths
    with Image.open(paths.IMAGE_DIR / name) as im:
        assert max(im.size) <= core.IMAGE_MAX_SIDE


def test_broken_image_only(fake_ai):
    r = run(core.answer_question("1", "Pa", "9", "", [core.IncomingImage(b"geen plaatje")]))
    assert "niet openen" in r.text


# ---------- commando's ----------

def test_commands():
    out = core.Outbox()
    r = core.handle_command("Pa", "!onthoud Mijn tv is een Samsung", out)
    assert "onthoud" in r.text and len(out.events) == 1
    r = core.handle_command("Pa", "!onthoud mijn wachtwoord is Geheim123", out)
    assert "liever niet" in r.text
    r = core.handle_command("Pa", "!geheugen", out)
    assert "1. Mijn tv is een Samsung" in r.text
    r = core.handle_command("Pa", "!vergeet 1", out)
    assert "Vergeten" in r.text
    assert "nog niets" in core.handle_command("Pa", "!geheugen", out).text
    assert core.is_command("!Geheugen") and not core.is_command("hoi !geheugen")


def test_extract_facts_pending_and_auto(monkeypatch):
    async def fake_chat(messages, **kw):
        return ai.AIResult('{"feiten": ["Heeft een LG OLED tv", "Pincode is 1234"]}', "m")
    monkeypatch.setattr(ai, "chat", fake_chat)
    core._store("1", "Pa", "9", "Mijn LG OLED doet raar", "Probeer...", [], "m", "ok")
    out = core.Outbox()
    ids = run(core.extract_facts_for_user("1", out))
    assert len(ids) == 1 and out.events[0].status == "pending"
    assert db.query_one("SELECT memory_checked FROM conversations")["memory_checked"] == 1
    assert run(core.extract_facts_for_user("1", core.Outbox())) == []  # al bekeken

    settings.set("memory_mode", "auto")
    core._store("1", "Pa", "9", "Ik gebruik nu Firefox", "Top", [], "m", "ok")

    async def fake_chat2(messages, **kw):
        return ai.AIResult('{"feiten": ["Gebruikt Firefox"]}', "m")
    monkeypatch.setattr(ai, "chat", fake_chat2)
    out = core.Outbox()
    run(core.extract_facts_for_user("1", out))
    assert out.events[0].status == "approved"


def test_split_message():
    text = ("abc " * 1000).strip()
    parts = core.split_message(text)
    assert all(len(p) <= 1900 for p in parts) and " ".join(parts) == text
