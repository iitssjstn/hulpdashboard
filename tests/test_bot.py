import asyncio
from types import SimpleNamespace

import discord

from app import bot, settings


def test_manager_without_token():
    m = bot.BotManager()
    asyncio.run(m.start())
    assert m.status == "niet ingesteld" and m.healthy()


def test_manager_bad_token_and_intents(monkeypatch):
    settings.set_secret("discord_token", "fake")

    async def scenario(exc):
        async def fake_start(self, token, reconnect=True):
            raise exc
        monkeypatch.setattr(bot.HelperClient, "start", fake_start)
        m = bot.BotManager()
        await m.start()
        await asyncio.sleep(0.05)
        status = (m.status, m.detail, m.healthy())
        await m.stop()
        return status

    st, detail, healthy = asyncio.run(scenario(discord.LoginFailure("nope")))
    assert st == "fout" and "token" in detail and not healthy
    st, detail, _ = asyncio.run(scenario(discord.PrivilegedIntentsRequired(None)))
    assert st == "fout" and "Message Content Intent" in detail


def test_restart_uses_new_token(monkeypatch):
    seen = []

    async def fake_start(self, token, reconnect=True):
        seen.append(token)
        self.manager.set_status("online", "ok")
        await asyncio.sleep(3600)
    monkeypatch.setattr(bot.HelperClient, "start", fake_start)

    async def scenario():
        settings.set_secret("discord_token", "oud")
        m = bot.BotManager()
        await m.start()
        await asyncio.sleep(0.05)
        settings.set_secret("discord_token", "nieuw")
        await m.restart()
        await asyncio.sleep(0.05)
        healthy = m.healthy()
        await m.stop()
        return healthy

    assert asyncio.run(scenario())
    assert seen == ["oud", "nieuw"]


def _channel(cid, name, parent=None):
    return SimpleNamespace(id=cid, name=name, parent=parent)


def test_help_channel_matching():
    async def scenario():
        c = bot.HelperClient(bot.BotManager())
        try:
            # Zonder ID: kanaal met de naam #hulp (en draadjes daarin).
            assert c.is_help_channel(_channel(1, "hulp"))
            assert c.is_help_channel(_channel(2, "draadje", parent=_channel(1, "hulp")))
            assert not c.is_help_channel(_channel(3, "algemeen"))
            settings.set("help_channel_id", "3")
            assert c.is_help_channel(_channel(3, "algemeen"))
            assert not c.is_help_channel(_channel(1, "hulp"))
            settings.set("guild_id", "50")
            assert c._in_guild(SimpleNamespace(id=50)) and not c._in_guild(SimpleNamespace(id=51))
            assert not c._in_guild(None)  # privéberichten negeren
        finally:
            await c.close()
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Nep-Discord: genoeg om de berichtenflow, tickets, knoppen en meldingen te testen.
# ---------------------------------------------------------------------------
import io
import itertools

import pytest
from PIL import Image

from app import ai, db, tickets

_ids = itertools.count(1000)


class _Typing:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append(("typing",))

    async def __aexit__(self, *a):
        pass


class FakeSent:
    def __init__(self, channel, content, kw):
        self.id = next(_ids)
        self.channel, self.content, self.kw = channel, content, kw
        self.embeds = [kw["embed"]] if kw.get("embed") else []
        self.view = kw.get("view")

    async def edit(self, **kw):
        self.channel.log.append(("edit-msg", self.id, kw))
        if "embed" in kw:
            self.embeds = [kw["embed"]]
        self.view = kw.get("view", self.view)


class FakeChannel:
    def __init__(self, cid, name, log, guild=None, category=None):
        self.id, self.name, self.log, self.guild, self.category = cid, name, log, guild, category
        self.parent = None
        self.sent: list[FakeSent] = []
        self.jump_url = f"https://discord.com/channels/9/{cid}"
        self.mention = f"<#{cid}>"

    def typing(self):
        return _Typing(self.log)

    async def send(self, content=None, **kw):
        m = FakeSent(self, content, kw)
        self.sent.append(m)
        self.log.append(("send", self.id, content, kw))
        return m

    async def fetch_message(self, mid):
        return next(m for m in self.sent if m.id == mid)

    async def create_thread(self, name, **kw):
        t = FakeThread(next(_ids), name, self.log, self)
        self.log.append(("create-private-thread", name, kw))
        return t


class FakeThread(discord.Thread):
    def __init__(self, tid, name, log, parent):  # noqa: D401 - geen super().__init__
        self._fake = {"id": tid, "name": name, "parent": parent}
        self.log = log
        self.sent: list[FakeSent] = []
        self.users = []
        self.archived_flag = False

    id = property(lambda self: self._fake["id"])
    name = property(lambda self: self._fake["name"])
    parent = property(lambda self: self._fake["parent"])
    guild = property(lambda self: self._fake["parent"].guild)
    jump_url = property(lambda self: f"https://discord.com/channels/9/{self.id}")
    mention = property(lambda self: f"<#{self.id}>")

    def typing(self):
        return _Typing(self.log)

    async def send(self, content=None, **kw):
        m = FakeSent(self, content, kw)
        self.sent.append(m)
        self.log.append(("send", self.id, content, kw))
        return m

    async def edit(self, **kw):
        if "name" in kw:
            self._fake["name"] = kw["name"]
        if "archived" in kw:
            self.archived_flag = kw["archived"]
        self.log.append(("edit-thread", self.id, kw))

    async def add_user(self, user):
        self.users.append(user.id)


class FakeGuild:
    def __init__(self, log):
        self.id = 9
        self.log = log
        self.default_role = discord.Object(9)
        self.me = discord.Object(1)
        self.text_channels = []
        self.created = []

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    async def fetch_member(self, uid):
        return discord.Object(uid)

    async def create_text_channel(self, name, **kw):
        ch = FakeChannel(next(_ids), name, self.log, self, kw.get("category"))
        self.created.append((ch, kw))
        self.text_channels.append(ch)
        return ch


class World:
    """Een nep-server met #hulp en een gebruiker 'Pa'."""

    def __init__(self, client):
        self.log = []
        self.guild = FakeGuild(self.log)
        self.help = FakeChannel(1, "hulp", self.log, self.guild, category="Cat")
        self.guild.text_channels.append(self.help)
        self.client = client
        self.channels = {self.help.id: self.help}
        client.get_channel = lambda cid: self.channels.get(cid) or self.guild.get_channel(cid)

        async def fetch_channel(cid):
            ch = client.get_channel(cid)
            if ch is None:
                raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "weg")
            return ch
        client.fetch_channel = fetch_channel

    def message(self, text, channel=None, attachments=(), author_id=5):
        channel = channel or self.help
        world = self

        async def reply(content, **kw):
            world.log.append(("reply", channel.id, content, kw))

        async def create_thread(name, **kw):
            t = FakeThread(next(_ids), name, world.log, channel)
            world.channels[t.id] = t
            world.log.append(("create-thread", name, kw))
            return t

        return SimpleNamespace(
            content=text, author=SimpleNamespace(id=author_id, display_name="Pa", bot=False),
            channel=channel, guild=self.guild, attachments=list(attachments), reply=reply,
            create_thread=create_thread, jump_url=f"https://discord.com/channels/9/{channel.id}/1")

    def threads(self):
        return [c for c in self.channels.values() if isinstance(c, FakeThread)]


class _Att:
    def __init__(self, data, filename="foto.png", content_type="image/png"):
        self._data, self.filename, self.content_type, self.size = data, filename, content_type, len(data)

    async def read(self):
        return self._data


@pytest.fixture
def fake_ai(monkeypatch):
    sent = []

    async def fake_chat(messages, **kw):
        sent.append(messages)
        system = messages[0]["content"]
        if system == tickets.TITLE_PROMPT:
            return ai.AIResult("Tv geeft geen geluid", "m:free")
        if system == tickets.SUMMARY_PROMPT:
            return ai.AIResult("Pa's tv geeft geen geluid. De bot stelde de soundbar voor. Hij wil hulp.", "m:free")
        return ai.AIResult(fake_chat.reply, "m:free")

    fake_chat.reply = "Klik rechtsboven op het tandwiel."
    monkeypatch.setattr(ai, "chat", fake_chat)
    return fake_chat, sent


async def _drain(client):
    for _ in range(5):
        if client.background:
            await asyncio.gather(*list(client.background), return_exceptions=True)
        await asyncio.sleep(0)


def run_world(scenario):
    async def wrapper():
        c = bot.HelperClient(bot.BotManager())
        try:
            await scenario(c, World(c))
        finally:
            await _drain(c)
            await c.close()
    asyncio.run(wrapper())


def test_tickets_off_old_flow(fake_ai):
    _, sent = fake_ai
    settings.set("ticket_mode", "uit")

    async def scenario(c, w):
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, "PNG")
        await c._handle(w.message("Wat is dit?", attachments=[_Att(buf.getvalue())]))
        assert w.log[0] == ("typing",)
        assert w.log[1][0] == "reply" and "tandwiel" in w.log[1][2]
        assert "view" not in w.log[1][3]  # geen ticketknoppen zonder tickets
        assert sent[-1][-1]["content"][1]["type"] == "image_url"
        assert not w.threads()
    run_world(scenario)


def test_new_question_creates_thread_with_ai_title(fake_ai):
    settings.set("owner_user_id", "321")
    settings.set("profile", {"eigenaar_naam": "Sjors"})

    async def scenario(c, w):
        await c._handle(w.message("Mijn tv doet het niet, er komt geen geluid uit"))
        await _drain(c)
        (thread,) = w.threads()
        assert thread.name == "🟡 Tv geeft geen geluid"  # eerst eerste woorden, daarna AI-titel
        answer = thread.sent[-1]
        assert "tandwiel" in answer.content
        labels = [i.label for i in answer.view.children]
        assert labels == ["Opgelost", "Vraag Sjors"]
        t = tickets.by_thread(thread.id)
        assert t["title"] == "Tv geeft geen geluid" and t["status"] == "open"
        assert db.query_one("SELECT ticket_id FROM conversations")["ticket_id"] == t["id"]
        assert "5" in c.memory_timers
    run_world(scenario)


def test_history_is_per_ticket(fake_ai):
    _, sent = fake_ai
    settings.set("ticket_followup_minutes", 0)  # elk bericht in #hulp = nieuw ticket

    async def scenario(c, w):
        await c._handle(w.message("Netflix doet raar"))
        await c._handle(w.message("OBS camera zwart"))
        t1, t2 = w.threads()
        await c._handle(w.message("Hij hapert nog steeds", channel=t1))
        user_msgs = [m["content"] for m in sent[-1] if m["role"] == "user"]
        assert user_msgs == ["Netflix doet raar", "Hij hapert nog steeds"]  # geen OBS-vraag erbij
    run_world(scenario)


def test_followup_in_main_channel_goes_to_open_ticket(fake_ai):
    async def scenario(c, w):
        await c._handle(w.message("Mijn tv doet het niet"))
        await c._handle(w.message("Ik heb hem al uit en aan gezet"))
        (thread,) = w.threads()  # geen tweede ticket
        pointer = [e for e in w.log if e[0] == "reply" and "Ik ga verder" in e[2]]
        assert pointer and thread.mention in pointer[0][2]
        assert thread.sent[-1].content.startswith("> **Pa:** Ik heb hem al uit en aan gezet")
    run_world(scenario)


def test_solve_button_closes_and_reopens(fake_ai):
    async def scenario(c, w):
        await c._handle(w.message("Mijn tv doet het niet"))
        (thread,) = w.threads()
        responses = []
        inter = SimpleNamespace(
            channel_id=thread.id, channel=thread, guild=w.guild, user=SimpleNamespace(id=5),
            response=SimpleNamespace(send_message=lambda *a, **k: _rec(responses, a, k)))
        await c.on_ticket_button(inter, "opgelost")
        await _drain(c)
        t = tickets.by_thread(thread.id)
        assert t["status"] == "opgelost" and t["closed_at"]
        assert thread.name.startswith("✅") and thread.archived_flag
        assert "Fijn dat het gelukt is" in responses[0][0][0]

        # Iemand anders mag niet afsluiten
        inter.user = SimpleNamespace(id=999)
        await c.on_ticket_button(inter, "opgelost")
        assert responses[-1][1].get("ephemeral")

        # Nieuw bericht in het draadje: ticket gaat weer open
        await c._handle(w.message("Het doet het toch weer niet", channel=thread))
        await _drain(c)
        assert tickets.by_thread(thread.id)["status"] == "open" and thread.name.startswith("🟡")
    run_world(scenario)


async def _rec(store, a, k):
    store.append((a, k))


def test_sensitive_escalation_creates_owner_channel_with_summary(fake_ai):
    _, sent = fake_ai
    settings.set("owner_user_id", "321")
    settings.set("profile", {"eigenaar_naam": "Sjors"})

    async def scenario(c, w):
        await c._handle(w.message("Ik kreeg een sms dat ik mijn pakketje moet betalen via iDEAL"))
        (thread,) = w.threads()
        # Gevoelig: geen AI-titel en geen AI-samenvatting
        assert not any(m[0]["content"] in (tickets.TITLE_PROMPT, tickets.SUMMARY_PROMPT) for m in sent)
        assert "betalen" in thread.name

        # Meldingskanaal aangemaakt, alleen zichtbaar voor eigenaar en bot
        (owner_ch, kw) = w.guild.created[0]
        assert owner_ch.name == "voor-sjors" and kw["category"] == "Cat"
        ow = kw["overwrites"]
        assert ow[w.guild.default_role].view_channel is False
        assert settings.get("owner_channel_id") == str(owner_ch.id)

        melding = owner_ch.sent[-1]
        assert melding.content == "<@321>"
        emb = melding.embeds[0]
        assert "Pa heeft je hulp nodig" in emb.title
        assert "betalen" in emb.description and "nergens op te klikken" in emb.description
        assert any(f.name == "Onderwerp" for f in emb.fields)
        link = [i for i in melding.view.children if i.url]
        assert link and link[0].url.endswith(str(thread.id))

        # In het draadje wordt de eigenaar genoemd maar niet dubbel gepingd
        answer = thread.sent[-1]
        assert "<@321> kun je hier even naar kijken?" in answer.content
        assert answer.kw["allowed_mentions"].users is False
        assert 321 in thread.users
        t = tickets.by_thread(thread.id)
        assert t["status"] == "wacht_op_jou" and t["escalation_message_id"] == str(melding.id)
        await _drain(c)
        assert thread.name.startswith("🙋")

        # Tweede escalatie: kanaal wordt hergebruikt
        await c._handle(w.message("En nog een vraag over mijn bankpas", channel=thread))
        assert len(w.guild.created) == 1
    run_world(scenario)


def test_ask_button_escalates_with_ai_summary_and_done_closes(fake_ai):
    settings.set("owner_user_id", "321")

    async def scenario(c, w):
        await c._handle(w.message("Mijn tv geeft geen geluid"))
        (thread,) = w.threads()
        responses = []
        inter = SimpleNamespace(
            channel_id=thread.id, channel=thread, guild=w.guild, user=SimpleNamespace(id=5),
            response=SimpleNamespace(send_message=lambda *a, **k: _rec(responses, a, k)))
        await c.on_ticket_button(inter, "vraag")
        owner_ch = w.guild.created[0][0]
        emb = owner_ch.sent[-1].embeds[0]
        assert emb.description.startswith("Pa's tv geeft geen geluid")  # AI-samenvatting
        assert any("zelf" in f.value for f in emb.fields)
        assert tickets.by_thread(thread.id)["status"] == "wacht_op_jou"

        # Eigenaar klikt op 'Afgehandeld'
        melding = owner_ch.sent[-1]
        edits = []
        inter2 = SimpleNamespace(
            user=SimpleNamespace(id=321), message=melding,
            response=SimpleNamespace(edit_message=lambda **k: _rec(edits, (), k),
                                     send_message=lambda *a, **k: _rec(edits, a, k)))
        await c.on_escalation_button(inter2)
        await _drain(c)
        assert edits[0][1]["embed"].title.startswith("✅ Afgehandeld")
        assert [i.label for i in edits[0][1]["view"].children] == ["Open gesprek"]
        assert tickets.by_thread(thread.id)["status"] == "opgelost"
        assert any("heeft dit afgehandeld" in (m.content or "") for m in thread.sent)

        # Niet-eigenaar mag niet afhandelen
        inter2.user = SimpleNamespace(id=5)
        await c.on_escalation_button(inter2)
        assert edits[-1][1].get("ephemeral")
    run_world(scenario)


def test_private_threads(fake_ai):
    settings.set("ticket_mode", "prive")

    async def scenario(c, w):
        await c._handle(w.message("Mijn wifi is traag"))
        created = [e for e in w.log if e[0] == "create-private-thread"]
        assert created and created[0][2]["type"] == discord.ChannelType.private_thread
        pointer = [e for e in w.log if e[0] == "reply" and "apart gesprek" in e[2]]
        assert pointer
    run_world(scenario)


def test_thread_permission_missing_falls_back(fake_ai):
    async def scenario(c, w):
        msg = w.message("Mijn tv doet het niet")

        async def no_threads(name, **kw):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="nee"), "geen rechten")
        msg.create_thread = no_threads
        await c._handle(msg)
        assert any(e[0] == "reply" and "tandwiel" in e[2] for e in w.log)  # gewoon in #hulp geantwoord
    run_world(scenario)


def test_owner_channel_permission_missing_pings_in_thread(fake_ai):
    settings.set("owner_user_id", "321")

    async def scenario(c, w):
        async def no_perm(name, **kw):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="nee"), "geen rechten")
        w.guild.create_text_channel = no_perm
        await c._handle(w.message("Is deze verdachte mail echt?"))
        (thread,) = w.threads()
        answer = thread.sent[-1]
        assert [u.id for u in answer.kw["allowed_mentions"].users] == [321]  # dan wél pingen in het draadje
    run_world(scenario)


def test_commands_still_work_in_main_channel(fake_ai):
    async def scenario(c, w):
        await c._handle(w.message("!onthoud Heeft een Elgato Facecam"))
        assert any(e[0] == "reply" and "onthoud" in e[2] for e in w.log)
        assert not w.threads()
    run_world(scenario)
