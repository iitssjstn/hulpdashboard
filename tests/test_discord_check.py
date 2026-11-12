import asyncio

import httpx

from app import discord_check


def test_check_token_reports_intent_and_channels():
    def handler(req: httpx.Request):
        p = req.url.path
        if p.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "1", "username": "Hulpbot"})
        if p.endswith("/applications/@me"):
            return httpx.Response(200, json={"id": "42", "flags": 0})
        if p.endswith("/channels/5"):
            return httpx.Response(200, json={"name": "hulp"})
        return httpx.Response(404, json={})

    ok, lines, invite = asyncio.run(discord_check.check_token(
        "t", {"#hulp": "5", "#geheugen": "6"}, transport=httpx.MockTransport(handler)))
    text = "\n".join(lines)
    assert not ok and "Hulpbot" in text and "Message Content Intent staat UIT" in text
    assert "#hulp gevonden" in text and "kan dit kanaal niet zien" in text
    assert "client_id=42" in invite


def test_check_token_invalid():
    t = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
    ok, lines, _ = asyncio.run(discord_check.check_token("t", transport=t))
    assert not ok and "kent dit token niet" in lines[0]
