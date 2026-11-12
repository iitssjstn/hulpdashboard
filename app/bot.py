"""Discord-laag. Draait als asyncio-taak naast het dashboard in hetzelfde proces,
zodat het dashboard de bot direct opnieuw kan laten verbinden na een tokenwijziging."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import discord

from . import core, db, memory, settings, tickets

log = logging.getLogger(__name__)

APPROVE, REJECT = "✅", "❌"
DEFAULT_HELP_CHANNEL = "hulp"
DEFAULT_MEMORY_CHANNEL = "geheugen"
SWEEP_SECONDS = 30 * 60


def _int(value) -> int | None:
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


def owner_channel_name() -> str:
    name = re.sub(r"[^a-z0-9]+", "-", core.owner_name().lower()).strip("-")
    return f"voor-{name}" if name else "voor-jou"


def ticket_link(ticket) -> str | None:
    if ticket is None or not ticket["thread_id"] or not ticket["guild_id"]:
        return None
    return f"https://discord.com/channels/{ticket['guild_id']}/{ticket['thread_id']}"


def escalation_embed(user_name: str, title: str | None, reason: str, summary: str) -> discord.Embed:
    embed = discord.Embed(title=f"🙋 {user_name} heeft je hulp nodig", description=summary[:4000],
                          colour=discord.Colour.orange(), timestamp=discord.utils.utcnow())
    if title:
        embed.add_field(name="Onderwerp", value=title[:1000], inline=False)
    embed.add_field(name="Waarom jij", value=reason[:1000], inline=False)
    return embed


def mark_embed_done(embed: discord.Embed) -> None:
    embed.colour = discord.Colour.green()
    if embed.title and not embed.title.startswith("✅"):
        embed.title = "✅ Afgehandeld · " + embed.title.removeprefix("🙋 ")


class TicketView(discord.ui.View):
    """Knoppen onder een antwoord in een ticket. Vaste custom_id's: werken ook na een herstart."""

    def __init__(self, ask_label: str = "Vraag om hulp", show_ask: bool = True):
        super().__init__(timeout=None)
        solve = discord.ui.Button(label="Opgelost", emoji="✅", style=discord.ButtonStyle.success,
                                  custom_id="hulp:opgelost")
        solve.callback = self._solve
        self.add_item(solve)
        if show_ask:
            ask = discord.ui.Button(label=ask_label, emoji="🙋", style=discord.ButtonStyle.secondary,
                                    custom_id="hulp:vraag")
            ask.callback = self._ask
            self.add_item(ask)

    async def _solve(self, interaction: discord.Interaction) -> None:
        await interaction.client.on_ticket_button(interaction, "opgelost")

    async def _ask(self, interaction: discord.Interaction) -> None:
        await interaction.client.on_ticket_button(interaction, "vraag")


class EscalationView(discord.ui.View):
    """Knoppen onder een melding in het meldingskanaal van de eigenaar."""

    def __init__(self, url: str | None = None, done: bool = False):
        super().__init__(timeout=None)
        if url:
            self.add_item(discord.ui.Button(label="Open gesprek", emoji="💬", url=url))
        if not done:
            btn = discord.ui.Button(label="Afgehandeld", emoji="✅", style=discord.ButtonStyle.success,
                                    custom_id="hulp:afgehandeld")
            btn.callback = self._done
            self.add_item(btn)

    async def _done(self, interaction: discord.Interaction) -> None:
        await interaction.client.on_escalation_button(interaction)


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
        self.background: set[asyncio.Task] = set()
        self.owner_channel_lock = asyncio.Lock()

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

    async def setup_hook(self) -> None:
        # Knoppen blijven werken na een herstart (vaste custom_id's).
        self.add_view(TicketView())
        self.add_view(EscalationView())

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
        uid = str(message.author.id)
        outbox = core.Outbox()

        if core.is_command(text):
            reply = core.handle_command(name, text, outbox)
            await self._send(message, reply, message.channel)
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

        # ----- bij welk ticket hoort dit bericht? -----
        ticket, target, quote = None, message.channel, False
        if isinstance(message.channel, discord.Thread):
            ticket = tickets.by_thread(message.channel.id)
            if ticket is None and tickets.enabled():
                # Zelf gemaakt draadje in #hulp: daar ook een ticket van maken.
                tid = tickets.create(uid, name, str(message.guild.id), str(message.channel.id),
                                     message.channel.name)
                ticket = tickets.get(tid)
        elif tickets.enabled():
            follow = tickets.followup_ticket(uid)
            thread = await self._get_thread(follow) if follow else None
            if thread is not None:
                ticket, target, quote = follow, thread, True
                await self._safe_reply(message, f"Ik ga verder in ons gesprek hierover: {thread.mention} 👈")
            else:
                ticket, thread, quote = await self._new_ticket(message, text, images)
                if thread is not None:
                    target = thread

        if ticket is not None and ticket["status"] == "opgelost":
            tickets.set_status(ticket["id"], "open")
            ticket = tickets.get(ticket["id"])
            self._bg(self._rename(target, ticket))

        async with target.typing():
            reply = await core.answer_question(
                uid, name, str(target.id), text, images, ticket_id=ticket["id"] if ticket else None)

        owner_ch = None
        if reply.mention_owner:
            owner_ch = await self.owner_channel(message.guild)
        await self._send(message, reply, target, ticket=ticket, quote=text if quote else None,
                         ping_owner=reply.mention_owner and owner_ch is None)
        if ticket is not None:
            tickets.touch(ticket["id"])
        if reply.mention_owner and owner_ch is not None:
            kind = "gevoelig" if reply.topic else "model"
            link = target.jump_url if isinstance(target, discord.Thread) else message.jump_url
            await self.escalate(ticket, owner_ch, kind, reply.topic, name, uid, link, target)
        if reply.status == "ok":
            self._schedule_memory(uid, message.guild)

    async def _safe_reply(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def _send(self, message: discord.Message, reply: core.Reply, target, ticket=None,
                    quote: str | None = None, ping_owner: bool = False) -> None:
        owner = _int(settings.get("owner_user_id"))
        allowed = discord.AllowedMentions(
            users=[discord.Object(owner)] if (ping_owner and owner) else False,
            everyone=False, roles=False, replied_user=False)
        text = reply.text
        if quote:
            short = core.sensitive.redact(quote.strip())
            short = (short[:200] + "…") if len(short) > 200 else short
            text = f"> **{message.author.display_name}:** {short}\n\n{text}" if short else text
        chunks = core.split_message(text)
        view = None
        if ticket is not None and reply.status in ("ok", "doorverwezen", "fout"):
            view = TicketView(ask_label=self._ask_label(), show_ask=owner is not None)
        for i, chunk in enumerate(chunks):
            kwargs = {"allowed_mentions": allowed}
            if view is not None and i == len(chunks) - 1:
                kwargs["view"] = view
            if i == 0 and target.id == message.channel.id:
                await message.reply(chunk, mention_author=False, **kwargs)
            else:
                await target.send(chunk, **kwargs)

    # ----- tickets -----

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    @staticmethod
    def _ask_label() -> str:
        owner = core.owner_name()
        return f"Vraag {owner}"[:80] if owner else "Vraag om hulp"

    async def _new_ticket(self, message: discord.Message, text: str, images: list):
        """Maak een draadje voor een nieuwe vraag. Geeft (ticket, draadje, quote) terug, of (None, None, False)."""
        topic = core.sensitive.detect(text)
        title = tickets.fallback_title(text, topic, bool(images))
        name = tickets.thread_name(title, "open")
        try:
            if tickets.private():
                thread = await message.channel.create_thread(
                    name=name, type=discord.ChannelType.private_thread, invitable=False,
                    auto_archive_duration=1440)
                await thread.add_user(message.author)
                await self._safe_reply(message, f"Ik heb een apart gesprek voor je gemaakt: {thread.mention} 👈 "
                                                f"Klik erop, daar help ik je verder.")
                quote = True
            else:
                thread = await message.create_thread(name=name, auto_archive_duration=1440)
                quote = False
        except discord.HTTPException as exc:
            log.warning("Kon geen draadje maken (heeft de bot het recht 'Draadjes maken'?): %s", exc)
            return None, None, False
        tid = tickets.create(str(message.author.id), message.author.display_name, str(message.guild.id),
                             str(thread.id), title)
        if not topic and settings.get("ticket_ai_titles"):
            self._bg(self._retitle(tid, thread, text, bool(images)))
        return tickets.get(tid), thread, quote

    async def _retitle(self, ticket_id: int, thread, text: str, has_image: bool) -> None:
        title = await tickets.ai_title(text, has_image)
        if title:
            tickets.set_title(ticket_id, title)
            await self._rename(thread, tickets.get(ticket_id))

    async def _rename(self, thread, ticket, archived: bool | None = None) -> None:
        if not isinstance(thread, discord.Thread) or ticket is None:
            return
        name = tickets.thread_name(ticket["title"], ticket["status"])
        kwargs = {}
        if thread.name != name:
            kwargs["name"] = name
        if archived is not None:
            kwargs["archived"] = archived
        if not kwargs:
            return
        try:
            await thread.edit(**kwargs)
        except discord.HTTPException as exc:
            log.warning("Kon het draadje niet bijwerken: %s", exc)

    async def _get_thread(self, ticket):
        if ticket is None or not ticket["thread_id"]:
            return None
        tid = int(ticket["thread_id"])
        ch = self.get_channel(tid)
        if ch is None:
            try:
                ch = await self.fetch_channel(tid)
            except discord.HTTPException:
                return None
        return ch if isinstance(ch, discord.Thread) else None

    async def close_ticket(self, ticket, thread=None, note: str | None = None,
                           edit_escalation: bool = True) -> None:
        tickets.set_status(ticket["id"], "opgelost")
        ticket = tickets.get(ticket["id"])
        thread = thread or await self._get_thread(ticket)
        if thread is not None:
            if note:
                try:
                    await thread.send(note, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            await self._rename(thread, ticket, archived=True)
        if edit_escalation:
            await self._mark_escalation_done(ticket)
        # Opgeloste problemen zijn goede feiten voor het geheugen: nu meteen kijken.
        old = self.memory_timers.pop(ticket["user_id"], None)
        if old and not old.done():
            old.cancel()
        self._bg(self._extract(ticket["user_id"], thread.guild if thread is not None else None))

    async def reopen_ticket(self, ticket) -> None:
        tickets.set_status(ticket["id"], "open")
        ticket = tickets.get(ticket["id"])
        thread = await self._get_thread(ticket)
        if thread is not None:
            await self._rename(thread, ticket, archived=False)

    # ----- melden aan de eigenaar -----

    async def owner_channel(self, guild: discord.Guild | None):
        """Privékanaal voor meldingen aan de eigenaar. Wordt zo nodig aangemaakt."""
        cid = _int(settings.get("owner_channel_id"))
        if cid:
            ch = self.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.fetch_channel(cid)
                except discord.NotFound:
                    ch = None
                    settings.set("owner_channel_id", "")  # verwijderd: opnieuw aanmaken
                except discord.HTTPException:
                    return None
            if ch is not None:
                return ch
        owner = _int(settings.get("owner_user_id"))
        if guild is None or owner is None:
            return None
        async with self.owner_channel_lock:
            cid = _int(settings.get("owner_channel_id"))
            if cid and self.get_channel(cid):
                return self.get_channel(cid)
            try:
                member = await guild.fetch_member(owner)
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    member: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                        send_messages=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                          send_messages=True, embed_links=True),
                }
                help_ch = self._help_channel_in(guild)
                ch = await guild.create_text_channel(
                    owner_channel_name(), overwrites=overwrites,
                    category=getattr(help_ch, "category", None),
                    topic="Meldingen van de hulpbot: hier tag ik je met een samenvatting als je hulp nodig is.",
                    reason="Hulpdashboard: meldingskanaal voor de eigenaar")
            except discord.HTTPException as exc:
                log.warning("Kon het meldingskanaal niet maken (heeft de bot 'Kanalen beheren'?): %s", exc)
                return None
            settings.set("owner_channel_id", str(ch.id))
            log.info("Meldingskanaal #%s aangemaakt", ch.name)
            try:
                await ch.send("👋 Dit is jouw meldingskanaal. Als iemand in #hulp jouw hulp nodig heeft, zet ik "
                              "hier een korte samenvatting met een link naar het gesprek.")
            except discord.HTTPException:
                pass
            return ch

    def _help_channel_in(self, guild: discord.Guild):
        cid = _int(settings.get("help_channel_id"))
        if cid:
            return guild.get_channel(cid)
        return discord.utils.get(guild.text_channels, name=DEFAULT_HELP_CHANNEL)

    async def escalate(self, ticket, owner_ch, kind: str, topic: str | None, user_name: str, user_id: str,
                       link: str, thread=None) -> None:
        if ticket is not None:
            rows = tickets.conversation_rows(ticket["id"])
        else:
            rows = list(reversed(db.query(
                "SELECT * FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 6", (user_id,))))
        summary = await tickets.build_summary(rows, kind, topic, user_name)
        reason = tickets.reason_text(kind, topic)
        embed = escalation_embed(user_name, ticket["title"] if ticket else None, reason, summary)
        owner = _int(settings.get("owner_user_id"))
        view = EscalationView(url=link)
        msg = None
        try:
            msg = await owner_ch.send(f"<@{owner}>" if owner else None, embed=embed, view=view,
                                      allowed_mentions=discord.AllowedMentions(users=True, everyone=False,
                                                                               roles=False))
        except discord.HTTPException as exc:
            log.warning("Kon geen melding sturen in het meldingskanaal: %s", exc)
        if ticket is not None:
            tickets.set_escalation(ticket["id"], reason, summary, str(owner_ch.id),
                                   str(msg.id) if msg else None)
            tickets.set_status(ticket["id"], "wacht_op_jou")
            if isinstance(thread, discord.Thread):
                if owner:
                    try:  # zodat de link ook werkt bij een privé-draadje
                        await thread.add_user(discord.Object(owner))
                    except discord.HTTPException:
                        pass
                self._bg(self._rename(thread, tickets.get(ticket["id"])))

    async def _mark_escalation_done(self, ticket) -> None:
        if not ticket or not ticket["escalation_message_id"] or not ticket["escalation_channel_id"]:
            return
        ch = self.get_channel(int(ticket["escalation_channel_id"]))
        if ch is None:
            return
        try:
            msg = await ch.fetch_message(int(ticket["escalation_message_id"]))
            embed = msg.embeds[0] if msg.embeds else discord.Embed()
            mark_embed_done(embed)
            url = ticket_link(ticket)
            await msg.edit(embed=embed, view=EscalationView(url=url, done=True) if url else None)
        except discord.HTTPException:
            pass

    # ----- knoppen -----

    async def on_ticket_button(self, interaction: discord.Interaction, action: str) -> None:
        ticket = tickets.by_thread(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message("Dit gesprek ken ik niet (meer).", ephemeral=True)
            return
        owner = _int(settings.get("owner_user_id"))
        if interaction.user.id not in (int(ticket["user_id"]), owner):
            await interaction.response.send_message("Alleen degene die de vraag stelde kan dit doen.",
                                                    ephemeral=True)
            return
        if action == "opgelost":
            if ticket["status"] == "opgelost":
                await interaction.response.send_message("Dit gesprek is al afgesloten. ✅", ephemeral=True)
                return
            await interaction.response.send_message(
                "Fijn dat het gelukt is! 🎉 Ik sluit dit gesprek af. Heb je weer een vraag? Typ hem gewoon in "
                "het hulpkanaal.")
            await self.close_ticket(ticket, thread=interaction.channel)
            return
        # 'vraag': eigenaar erbij halen
        if ticket["status"] == "wacht_op_jou":
            await interaction.response.send_message(
                f"{core.owner_name() or 'Je zoon/dochter'} weet het al en kijkt zo snel mogelijk mee. 🙂")
            return
        await interaction.response.send_message(
            f"Goed idee! Ik vraag {core.owner_name() or 'om hulp'} om mee te kijken. 🙋",
            allowed_mentions=discord.AllowedMentions.none())
        owner_ch = await self.owner_channel(interaction.guild)
        thread = interaction.channel
        if owner_ch is None:
            if owner:
                await thread.send(f"<@{owner}> kun je hier even naar kijken?",
                                  allowed_mentions=discord.AllowedMentions(users=[discord.Object(owner)]))
            tickets.set_status(ticket["id"], "wacht_op_jou")
            self._bg(self._rename(thread, tickets.get(ticket["id"])))
            return
        await self.escalate(ticket, owner_ch, "zelf", None, ticket["user_name"], ticket["user_id"],
                            thread.jump_url, thread)

    async def on_escalation_button(self, interaction: discord.Interaction) -> None:
        owner = _int(settings.get("owner_user_id"))
        if owner is not None and interaction.user.id != owner:
            await interaction.response.send_message("Alleen de eigenaar kan dit afhandelen.", ephemeral=True)
            return
        ticket = tickets.by_escalation_message(interaction.message.id)
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        mark_embed_done(embed)
        url = ticket_link(ticket) if ticket else None
        await interaction.response.edit_message(embed=embed,
                                                view=EscalationView(url=url, done=True) if url else None)
        if ticket is not None and ticket["status"] != "opgelost":
            name = core.owner_name() or "Je zoon/dochter"
            await self.close_ticket(ticket, note=f"✅ {name} heeft dit afgehandeld.", edit_escalation=False)

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

    def ready(self) -> bool:
        return self.client is not None and self.client.is_ready()

    async def close_ticket(self, ticket_id: int) -> None:
        ticket = tickets.get(ticket_id)
        if ticket is None:
            return
        if self.ready():
            try:
                name = core.owner_name() or "Je zoon/dochter"
                await self.client.close_ticket(ticket, note=f"✅ {name} heeft dit gesprek afgesloten.")
                return
            except Exception:  # noqa: BLE001
                log.exception("Ticket sluiten in Discord lukte niet")
        tickets.set_status(ticket_id, "opgelost")

    async def reopen_ticket(self, ticket_id: int) -> None:
        ticket = tickets.get(ticket_id)
        if ticket is None:
            return
        if self.ready():
            try:
                await self.client.reopen_ticket(ticket)
                return
            except Exception:  # noqa: BLE001
                log.exception("Ticket heropenen in Discord lukte niet")
        tickets.set_status(ticket_id, "open")

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
