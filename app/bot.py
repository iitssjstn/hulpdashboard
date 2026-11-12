"""Discord-laag. Draait als asyncio-taak naast het dashboard in hetzelfde proces,
zodat het dashboard de bot direct opnieuw kan laten verbinden na een tokenwijziging."""
from __future__ import annotations

import asyncio
import logging
import time

import discord

from . import core, db, memory, settings

log = logging.getLogger(__name__)

APPROVE, REJECT = "✅", "❌"
DEFAULT_HELP_CHANNEL = "hulp"
DEFAULT_MEMORY_CHANNEL = "geheugen"
SWEEP_SECONDS = 30 * 60


def _int(value) -> int | None:
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


class HelperClient(discord.Client):
    def __init__(self, manager: "BotManager"):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # privileged: moet aan in de Developer Portal
        intents.guild_reactions = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.manager = manager
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.memory_timers: dict[str, asyncio.Task] = {}
        self.sweeper: asyncio.Task | None = None

    # ----- verbinding -----

    async def on_ready(self) -> None:
        self.manager.set_status("online", f"Ingelogd als {self.user}")
        log.info("Discord-bot online als %s", self.user)
        if self.sweeper is None or self.sweeper.done():
            self.sweeper = asyncio.create_task(self._sweep_loop())

    async def on_resumed(self) -> None:
        self.manager.set_status("online", f"Ingelogd als {self.user}")

    async def on_disconnect(self) -> None:
        if self.manager.status == "online":
            self.manager.set_status("verbinden", "Verbinding met Discord even weg, opnieuw verbinden…")

    async def close(self) -> None:
        if self.sweeper:
            self.sweeper.cancel()
        for t in self.memory_timers.values():
            t.cancel()
        await super().close()

    # ----- kanalen -----

    def _in_guild(self, guild: discord.Guild | None) -> bool:
        gid = _int(settings.get("guild_id"))
        return guild is not None and (gid is None or guild.id == gid)

    def is_help_channel(self, channel) -> bool:
        cid = _int(settings.get("help_channel_id"))
        candidates = [channel]
        parent = getattr(channel, "parent", None)
        if parent is not None:  # draadjes binnen #hulp tellen ook
            candidates.append(parent)
        for ch in candidates:
            if cid is not None and ch.id == cid:
                return True
            if cid is None and getattr(ch, "name", "") == DEFAULT_HELP_CHANNEL:
                return True
        return False

    def memory_channel(self, guild: discord.Guild | None = None):
        cid = _int(settings.get("memory_channel_id"))
        if cid:
            return self.get_channel(cid)
        guilds = [guild] if guild else self.guilds
        gid = _int(settings.get("guild_id"))
        for g in guilds:
            if g and (gid is None or g.id == gid):
                ch = discord.utils.get(g.text_channels, name=DEFAULT_MEMORY_CHANNEL)
                if ch:
                    return ch
        return None

    # ----- berichten -----

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self._in_guild(message.guild):
            return
        if not self.is_help_channel(message.channel):
            return
        lock = self.user_locks.setdefault(message.author.id, asyncio.Lock())
        async with lock:
            try:
                await self._handle(message)
            except Exception:  # noqa: BLE001
                log.exception("Fout bij verwerken van een bericht")
                try:
                    await message.reply(core.friendly_error("other"), mention_author=False)
                except discord.HTTPException:
                    pass

    async def _handle(self, message: discord.Message) -> None:
        text = message.content or ""
        name = message.author.display_name
        outbox = core.Outbox()

        if core.is_command(text):
            reply = core.handle_command(name, text, outbox)
            await self._send(message, reply)
            await self._publish(outbox, message.guild)
            return

        images: list[core.IncomingImage] = []
        for att in message.attachments[: core.MAX_IMAGES]:
            ctype = (att.content_type or "").lower()
            is_img = ctype.startswith("image/") or att.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"))
            if is_img and att.size <= core.MAX_IMAGE_BYTES:
                try:
                    images.append(core.IncomingImage(await att.read(), att.filename))
                except discord.HTTPException:
                    log.warning("Bijlage kon niet worden gedownload")
        if not text.strip() and not images:
            return

        async with message.channel.typing():
            reply = await core.answer_question(
                str(message.author.id), name, str(message.channel.id), text, images)
        await self._send(message, reply)
        if reply.status == "ok":
            self._schedule_memory(str(message.author.id), message.guild)

    async def _send(self, message: discord.Message, reply: core.Reply) -> None:
        owner = _int(settings.get("owner_user_id"))
        allowed = discord.AllowedMentions(
            users=[discord.Object(owner)] if (reply.mention_owner and owner) else False,
            everyone=False, roles=False, replied_user=False)
        for i, chunk in enumerate(core.split_message(reply.text)):
            if i == 0:
                await message.reply(chunk, mention_author=False, allowed_mentions=allowed)
            else:
                await message.channel.send(chunk, allowed_mentions=allowed)

    # ----- geheugen -----

    def _schedule_memory(self, user_id: str, guild) -> None:
        old = self.memory_timers.get(user_id)
        if old and not old.done():
            old.cancel()
        self.memory_timers[user_id] = asyncio.create_task(self._memory_after_idle(user_id, guild))

    async def _memory_after_idle(self, user_id: str, guild) -> None:
        try:
            await asyncio.sleep(settings.get_int("memory_idle_minutes", 1, 240) * 60)
        except asyncio.CancelledError:
            return
        await self._extract(user_id, guild)

    async def _extract(self, user_id: str, guild=None) -> None:
        outbox = core.Outbox()
        try:
            await core.extract_facts_for_user(user_id, outbox)
        except Exception:  # noqa: BLE001
            log.exception("Fout bij geheugen bijwerken")
        await self._publish(outbox, guild)

    async def _sweep_loop(self) -> None:
        """Pak gesprekken op die nog niet op feiten zijn bekeken (bijv. na een herstart)."""
        await asyncio.sleep(60)
        while True:
            for uid in core.users_with_unchecked():
                if uid not in self.memory_timers or self.memory_timers[uid].done():
                    await self._extract(uid)
            await asyncio.sleep(SWEEP_SECONDS)

    async def _publish(self, outbox: core.Outbox, guild=None) -> None:
        if not outbox.events:
            return
        channel = self.memory_channel(guild)
        if channel is None:
            log.warning("Geheugenkanaal niet gevonden; nieuwe feiten staan wel in het dashboard")
            return
        for ev in outbox.events:
            if ev.status == "pending":
                text = (f"🧠 **Nieuw feit gevonden** (wacht op goedkeuring):\n> {ev.text}\n"
                        f"Reageer met {APPROVE} om op te slaan of {REJECT} om af te wijzen, "
                        f"of doe het in het dashboard.")
            elif ev.source == "discord":
                text = f"📝 **{ev.by}** liet onthouden:\n> {ev.text}"
            else:
                text = f"🧠 **Nieuw feit opgeslagen:**\n> {ev.text}"
            try:
                msg = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                if ev.status == "pending":
                    db.execute("UPDATE facts SET discord_message_id=? WHERE id=?", (str(msg.id), ev.fact_id))
                    await msg.add_reaction(APPROVE)
                    await msg.add_reaction(REJECT)
            except discord.HTTPException as exc:
                log.warning("Kon niet posten in het geheugenkanaal: %s", exc)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        owner = _int(settings.get("owner_user_id"))
        if owner is None or payload.user_id != owner:
            return
        emoji = str(payload.emoji)
        if emoji not in (APPROVE, REJECT):
            return
        row = db.query_one("SELECT * FROM facts WHERE discord_message_id=? AND status='pending'",
                           (str(payload.message_id),))
        if row is None:
            return
        new = "approved" if emoji == APPROVE else "rejected"
        memory.set_status(row["id"], new)
        channel = self.get_channel(payload.channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(payload.message_id)
                label = "opgeslagen ✅" if new == "approved" else "afgewezen ❌"
                await msg.edit(content=f"🧠 Feit {label}:\n> {row['text']}")
                await msg.clear_reactions()
            except discord.HTTPException:
                pass


class BotManager:
    """Start, stopt en herstart de Discord-verbinding. Houdt de status bij voor het dashboard."""

    def __init__(self) -> None:
        self.client: HelperClient | None = None
        self.task: asyncio.Task | None = None
        self.status = "niet ingesteld"
        self.detail = "Er is nog geen Discord-token ingesteld."
        self.since = time.time()
        self._lock = asyncio.Lock()

    def set_status(self, status: str, detail: str) -> None:
        self.status, self.detail, self.since = status, detail, time.time()

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def _start(self) -> None:
        token = settings.get_secret("discord_token")
        if not token:
            self.set_status("niet ingesteld", "Er is nog geen Discord-token ingesteld.")
            return
        self.client = HelperClient(self)
        self.set_status("verbinden", "Bezig met verbinden met Discord…")
        self.task = asyncio.create_task(self._run(self.client, token))

    async def _run(self, client: HelperClient, token: str) -> None:
        try:
            await client.start(token, reconnect=True)
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure:
            self.set_status("fout", "Discord weigert het token. Vervang het token bij Instellingen.")
            log.error("Discord-login mislukt: ongeldig token")
        except discord.PrivilegedIntentsRequired:
            self.set_status("fout", "Zet 'Message Content Intent' aan in de Discord Developer Portal "
                                    "(Bot-tabblad) en klik daarna op 'Opnieuw verbinden'.")
            log.error("Discord: Message Content Intent staat uit")
        except Exception as exc:  # noqa: BLE001
            self.set_status("fout", f"Verbinding met Discord mislukt ({type(exc).__name__}).")
            log.exception("Discord-verbinding mislukt")
        else:
            if self.status not in ("fout",):
                self.set_status("offline", "De bot is gestopt.")
        finally:
            if not client.is_closed():
                await client.close()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.client, self.task = None, None
        self.set_status("offline", "De bot is gestopt.")

    async def restart(self) -> None:
        async with self._lock:
            await self._stop()
            await self._start()

    def healthy(self) -> bool:
        if self.status in ("online", "niet ingesteld"):
            return True
        # Even geduld tijdens (her)verbinden.
        return self.status == "verbinden" and time.time() - self.since < 180

    def info(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "since": self.since,
            "latency_ms": round(self.client.latency * 1000) if self.client and self.client.is_ready()
            and self.client.latency == self.client.latency else None,
            "guilds": [g.name for g in self.client.guilds] if self.client and self.client.is_ready() else [],
        }


manager = BotManager()
