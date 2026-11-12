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


class _Typing:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("typing-start")

    async def __aexit__(self, *a):
        self.log.append("typing-stop")


class _Att:
    def __init__(self, data, filename="foto.png", content_type="image/png"):
        self._data, self.filename, self.content_type, self.size = data, filename, content_type, len(data)

    async def read(self):
        return self._data


def _message(text, events, attachments=()):
    channel = SimpleNamespace(id=1, name="hulp", parent=None, typing=lambda: _Typing(events))

    async def send(content, **kw):
        events.append(("send", content, kw))
    channel.send = send

    async def reply(content, **kw):
        events.append(("reply", content, kw))
    return SimpleNamespace(content=text, author=SimpleNamespace(id=5, display_name="Pa", bot=False),
                           channel=channel, guild=SimpleNamespace(id=9, text_channels=[]), attachments=list(attachments),
                           reply=reply)


def test_handle_message_flow(monkeypatch):
    import io

    from PIL import Image

    from app import ai, db

    sent = []

    async def fake_chat(messages, **kw):
        sent.append(messages)
        return ai.AIResult("Klik rechtsboven op het tandwiel.", "m:free")
    monkeypatch.setattr(ai, "chat", fake_chat)
    settings.set("owner_user_id", "321")

    async def scenario():
        c = bot.HelperClient(bot.BotManager())
        try:
            events = []
            buf = io.BytesIO()
            Image.new("RGB", (50, 50)).save(buf, "PNG")
            await c._handle(_message("Wat is dit?", events, [_Att(buf.getvalue())]))
            assert events[0] == "typing-start" and events[1] == "typing-stop"
            assert events[2][0] == "reply" and "tandwiel" in events[2][1]
            assert sent[-1][-1]["content"][1]["type"] == "image_url"
            assert "5" in c.memory_timers  # geheugen-check ingepland na stilte

            events.clear()
            await c._handle(_message("!onthoud Heeft een Elgato Facecam", events))
            assert "onthoud" in events[0][1]

            events.clear()
            await c._handle(_message("Moet ik deze rekening via iDEAL betalen?", events))
            text, kw = events[-1][1], events[-1][2]
            assert "<@321> kun je hier even naar kijken?" in text
            am = kw["allowed_mentions"]
            assert [u.id for u in am.users] == [321] and am.everyone is False
            assert db.query_one("SELECT COUNT(*) n FROM conversations WHERE status='doorverwezen'")["n"] == 1
        finally:
            await c.close()
    asyncio.run(scenario())
