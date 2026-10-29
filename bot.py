from __future__ import annotations

import logging
import os
import asyncio
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
import texts
from streaks import (
    RESTORES_PER_MONTH,
    add_progress,
    consume_restore,
    emoji_for,
    format_nickname,
    is_streak_restorable,
    next_milestone,
    reset_streak,
    restores_left,
    strip_prefix,
)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("streak-bot")

voice_joined: dict[tuple[int, int], datetime] = {}

def guild_object() -> discord.Object | None:
    return discord.Object(id=int(GUILD_ID)) if GUILD_ID.isdigit() else None

def ru_command(name: str, **kwargs):
    return bot.tree.command(name=name, description=texts.COMMANDS[name], **kwargs)

class StreakBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await db.init()
        guild = guild_object()
        if guild:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s command(s) to guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global command(s)", len(synced))
        
        credit_voice_loop.start()
        midnight_dimmer_loop.start()
        auto_backup_loop.start()
        unbound_decay_loop.start()

bot = StreakBot()

def today():
    return discord.utils.utcnow().date()

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def voice_key(member: discord.Member) -> tuple[int, int]:
    return member.guild.id, member.id

def in_voice(state: discord.VoiceState) -> bool:
    return state.channel is not None

def base_display_name(member: discord.Member) -> str:
    return strip_prefix(member.nick or member.display_name or member.name)

async def try_set_nickname(member: discord.Member, nick: str | None) -> bool:
    if member.guild.owner_id == member.id:
        return False
    try:
        await member.edit(nick=nick, reason="Обновление стрика")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

async def apply_nickname(member: discord.Member, streak, visible: bool) -> bool:
    base = base_display_name(member)
    now_date = today()
    
    if not visible:
        current = member.nick
        if current and strip_prefix(current) != current:
            return await try_set_nickname(member, base)
        return True
        
    if streak.is_frozen:
        desired = format_nickname(base, streak.count, state="frozen")
    elif streak.count <= 0:
        if is_streak_restorable(streak, now_date):
            desired = format_nickname(base, streak.last_lost_count, state="lost")
        else:
            desired = base
    else:
        is_done_today = streak.last_active == now_date
        state = "active" if is_done_today else "unlit"
        desired = format_nickname(base, streak.count, state=state)
        
    if member.nick == desired:
        return True
    return await try_set_nickname(member, desired)

async def check_streak_rewards(member: discord.Member, streak):
    if streak.count <= 0:
        return
    roles_dict = await db.get_all_streak_roles(member.guild.id)
    if not roles_dict:
        return
        
    for days, role_ids in roles_dict.items():
        if streak.count == days:
            for role_id in role_ids:
                role = member.guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Награда за стрик {days} дней")
                    except discord.Forbidden:
                        log.warning(f"Не удалось выдать роль {role_id} пользователю {member.id} (нет прав)")

async def record_progress(member: discord.Member, *, messages: int = 0, voice_seconds: int = 0) -> None:
    settings = await db.get_guild_settings(member.guild.id)
    streak = await db.get_streak(member.guild.id, member.id)
    
    old_last_active = streak.last_active
    streak, changed = add_progress(
        streak, today(), settings.message_goal, settings.voice_goal_seconds,
        messages=messages, voice_seconds=voice_seconds,
    )
    await db.save_streak(streak)
    
    if changed and streak.nickname_visible:
        await apply_nickname(member, streak, True)
    elif not changed and old_last_active != today() and streak.nickname_visible:
        await apply_nickname(member, streak, True)
        
    if changed:
        await check_streak_rewards(member, streak)

def track_voice_join(member: discord.Member) -> None:
    voice_joined.setdefault(voice_key(member), utcnow())

async def credit_voice(member: discord.Member) -> None:
    key = voice_key(member)
    joined = voice_joined.get(key)
    if joined is None:
        return
    now = utcnow()
    elapsed = int((now - joined).total_seconds())
    voice_joined[key] = now
    if elapsed > 0:
        await record_progress(member, voice_seconds=elapsed)

async def stop_voice(member: discord.Member) -> None:
    await credit_voice(member)
    voice_joined.pop(voice_key(member), None)

def progress_lines(streak, msg_goal: int, voice_goal: int) -> str:
    if streak.progress_date != today():
        messages, voice = 0, 0
    else:
        messages, voice = streak.messages_today, streak.voice_seconds_today
    done = streak.last_active == today()
    
    lines = [texts.TODAY_PROGRESS.format(messages=messages, message_goal=msg_goal, voice=texts.format_voice(voice), voice_goal=texts.format_voice(voice_goal))]
    if done:
        lines.append(texts.TODAY_DONE)
    return "\n".join(lines)

def streak_text(streak, msg_goal: int, voice_goal: int) -> str:
    count = streak.count
    now_date = today()
    if count <= 0:
        if is_streak_restorable(streak, now_date):
            body = texts.STREAK_FROZEN.format(count=streak.last_lost_count)
        else:
            body = texts.NO_STREAK
    else:
        lines = [texts.STREAK_COUNT.format(emoji=emoji_for(count), count=count, flames=texts.flames_word(count))]
        nxt = next_milestone(count)
        if nxt:
            lines.append(texts.NEXT_EMOJI.format(emoji=nxt[1], count=nxt[0]))
        else:
            lines.append(texts.MAX_EMOJI)
        body = "\n".join(lines)
    return f"{body}\n{progress_lines(streak, msg_goal, voice_goal)}"

def guild_only_response():
    return texts.GUILD_ONLY

@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    now = utcnow()
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            for member in channel.members:
                if not member.bot:
                    voice_joined.setdefault((guild.id, member.id), now)
        for channel in guild.stage_channels:
            for member in channel.members:
                if not member.bot:
                    voice_joined.setdefault((guild.id, member.id), now)

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return
    member = message.author
    if not isinstance(member, discord.Member):
        member = message.guild.get_member(message.author.id)
        if member is None:
            return
    await record_progress(member, messages=1)
    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return
    was_in, now_in = in_voice(before), in_voice(after)
    if not was_in and now_in:
        track_voice_join(member)
        return
    if was_in and not now_in:
        await stop_voice(member)
        return
    if was_in and now_in:
        track_voice_join(member)

@tasks.loop(seconds=20)
async def credit_voice_loop() -> None:
    for guild_id, user_id in list(voice_joined.keys()):
        guild = bot.get_guild(guild_id)
        if guild is None:
            voice_joined.pop((guild_id, user_id), None)
            continue
        member = guild.get_member(user_id)
        if member is None or member.voice is None or member.voice.channel is None:
            voice_joined.pop((guild_id, user_id), None)
            continue
        await credit_voice(member)

@credit_voice_loop.before_loop
async def before_credit_voice_loop() -> None:
    await bot.wait_until_ready()

@tasks.loop(minutes=5)
async def midnight_dimmer_loop() -> None:
    now_date = today()
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot or guild.owner_id == member.id:
                continue
            streak = await db.get_streak(guild.id, member.id)
            if streak.nickname_visible and not streak.is_frozen:
                if streak.count > 0 and streak.last_active != now_date:
                    if member.nick and not member.nick.startswith("🔘"):
                        await apply_nickname(member, streak, True)
                        await asyncio.sleep(5)
                elif streak.count <= 0 and streak.last_lost_count > 0 and not is_streak_restorable(streak, now_date):
                    if member.nick and member.nick.startswith("❌"):
                        await apply_nickname(member, streak, True)
                        await asyncio.sleep(5)

@midnight_dimmer_loop.before_loop
async def before_midnight_dimmer_loop() -> None:
    await bot.wait_until_ready()

@tasks.loop(minutes=30)
async def unbound_decay_loop() -> None:
    now_date = today()
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM streaks WHERE is_frozen = 1") as cursor:
            rows = await cursor.fetchall()
            
        for row in rows:
            streak = db.streak_from_row(row)
            if streak.progress_date != now_date:
                streak.count -= 1
                streak.progress_date = now_date
                
                if streak.count <= 0:
                    streak.is_frozen = False
                    streak.count = 0
                    
                await db.save_streak(streak)
                
                guild = bot.get_guild(streak.guild_id)
                if guild:
                    member = guild.get_member(streak.user_id)
                    if member and streak.nickname_visible:
                        await apply_nickname(member, streak, True)
                        await asyncio.sleep(5)

@unbound_decay_loop.before_loop
async def before_unbound_decay_loop() -> None:
    await bot.wait_until_ready()

@tasks.loop(hours=24)
async def auto_backup_loop() -> None:
    await asyncio.sleep(60)
    DATA_DIR = os.getenv("DATA_DIR", ".")
    db_path = Path(DATA_DIR) / "streaks.db"
    backup_dir = Path(DATA_DIR) / "backups"
    
    backup_dir.mkdir(exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    backup_path = backup_dir / f"streaks_backup_{today_str}.db"

    try:
        shutil.copy2(db_path, backup_path)
        log.info(f"✅ Автобэкап базы данных создан: {backup_path.name}")
        for file in backup_dir.glob("streaks_backup_*.db"):
            file_age_days = (datetime.now() - datetime.fromtimestamp(file.stat().st_mtime)).days
            if file_age_days > 7:
                file.unlink()
                log.info(f"🗑️ Удален старый бэкап: {file.name}")
    except Exception as e:
        log.error(f"❌ Ошибка при создании автобэкапа: {e}")

@auto_backup_loop.before_loop
async def before_auto_backup_loop() -> None:
    await bot.wait_until_ready()

class UnboundView(discord.ui.View):
    def __init__(self, streak: db.Streak, member: discord.Member):
        super().__init__(timeout=None)
        self.streak = streak
        self.member = member
        self.update_buttons()

    def update_buttons(self):
        self.toggle_button.disabled = (self.streak.count <= 1 and not self.streak.is_frozen)
        if self.streak.is_frozen:
            self.toggle_button.label = "🔥 Разжечь (Вернуться к обычному стрику)"
            self.toggle_button.style = discord.ButtonStyle.danger
        else:
            self.toggle_button.label = "❄️ Заморозить (Утечка -1 в день)"
            self.toggle_button.style = discord.ButtonStyle.primary

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="❄️ Управление заморозкой",
            color=discord.Color.blue() if self.streak.is_frozen else discord.Color.orange()
        )
        if self.streak.is_frozen:
            embed.description = (
                f"Ваш стрик **заморожен**. Обычная активность не добавляет огоньки.\n"
                f"Каждый день (по UTC) сгорает **1 огонек**.\n\n"
                f"Текущий запас: **{emoji_for(self.streak.count)} {self.streak.count}**\n"
                f"Завтра останется: **{self.streak.count - 1 if self.streak.count > 0 else 0}**"
            )
        else:
            embed.description = (
                f"Ваш стрик: **{emoji_for(self.streak.count)} {self.streak.count}**\n\n"
                f"**Заморозка** позволит вам не потерять серию, если вы уезжаете или не можете писать.\n"
                f"⚠️ Но каждый день будет сгорать по **1 огоньку**.\n"
                f"Как только огоньки закончатся, серия сгорит."
            )
        return embed

    @discord.ui.button(label="Загрузка...", style=discord.ButtonStyle.primary)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("Это не ваша заморозка.", ephemeral=True)
            return

        self.streak.is_frozen = not self.streak.is_frozen
        if self.streak.is_frozen:
            self.streak.progress_date = today()
        else:
            self.streak.last_active = today()
            
        await db.save_streak(self.streak)
        await apply_nickname(self.member, self.streak, self.streak.nickname_visible)
        
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

@ru_command("streakunbound")
async def unbound_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
        
    member = interaction.user
    streak = await db.get_streak(interaction.guild.id, member.id)
    
    if streak.count <= 0:
        await interaction.response.send_message("У вас нет активного стрика, который можно заморозить.", ephemeral=True)
        return
        
    view = UnboundView(streak, member)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

class LeaderboardView(discord.ui.View):
    def __init__(self, entries: list[db.Streak], guild: discord.Guild, author_id: int):
        super().__init__(timeout=120)
        self.entries = entries
        self.guild = guild
        self.author_id = author_id
        self.page = 0
        self.per_page = 10
        self.max_page = max(0, (len(entries) - 1) // self.per_page)
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.page == 0)
        self.next_button.disabled = (self.page >= self.max_page)

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]

        lines = []
        for i, streak in enumerate(page_entries, start=start + 1):
            member = self.guild.get_member(streak.user_id)
            if member:
                clean_name = strip_prefix(member.display_name)
                name = f"**{clean_name}**"
            else:
                name = f"`{streak.user_id}`"
                
            frozen_icon = " ❄️" if streak.is_frozen else ""
            lines.append(f"**{i}.** {emoji_for(streak.count)} **{streak.count}**{frozen_icon} — {name}")

        embed = discord.Embed(
            title=texts.LEADERBOARD_TITLE,
            description="\n".join(lines),
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"{texts.LEADERBOARD_FOOTER} | Страница {self.page + 1} из {self.max_page + 1}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Вы не можете листать чужой лидерборд.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Вперед", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

@ru_command("leaderboard")
async def leaderboard_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    await interaction.response.defer()
    
    entries = await db.list_active_streaks(interaction.guild.id, today(), limit=1000)
    
    if not entries:
        await interaction.followup.send(texts.LEADERBOARD_EMPTY)
        return

    view = LeaderboardView(entries, interaction.guild, interaction.user.id)
    await interaction.followup.send(embed=view.build_embed(), view=view)

@ru_command("streak")
@app_commands.describe(user=texts.PARAMS["streak_user"])
async def streak_command(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    target = user or interaction.user
    settings = await db.get_guild_settings(interaction.guild.id)
    streak = await db.get_streak(interaction.guild.id, target.id)
    emoji = emoji_for(streak.count) if streak.count else "🔥"
    
    if target.id == interaction.user.id:
        body = streak_text(streak, settings.message_goal, settings.voice_goal_seconds)
    elif streak.count <= 0:
        body = texts.OTHER_NO_STREAK.format(mention=target.mention)
    else:
        body = texts.OTHER_STREAK.format(mention=target.mention, emoji=emoji, count=streak.count)
        if isinstance(target, discord.Member) and target.id:
            body = f"{body}\n{progress_lines(streak, settings.message_goal, settings.voice_goal_seconds)}"
    await interaction.response.send_message(body, ephemeral=True)

@ru_command("hidestreak")
async def hidestreak_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    member = interaction.user
    streak = await db.get_streak(interaction.guild.id, member.id)
    streak.nickname_visible = False
    await db.save_streak(streak)
    updated = await apply_nickname(member, streak, False)
    if updated:
        await interaction.response.send_message(texts.HIDDEN, ephemeral=True)
        return
    await interaction.response.send_message(texts.NICK_PERMISSION, ephemeral=True)

@ru_command("showstreak")
async def showstreak_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    member = interaction.user
    streak = await db.get_streak(interaction.guild.id, member.id)
    streak.nickname_visible = True
    await db.save_streak(streak)
    if streak.count <= 0 and not is_streak_restorable(streak, today()):
        await interaction.response.send_message(texts.SHOWN_EMPTY, ephemeral=True)
        return
    updated = await apply_nickname(member, streak, True)
    if updated:
        await interaction.response.send_message(texts.SHOWN.format(emoji=emoji_for(streak.count), count=streak.count), ephemeral=True)
        return
    await interaction.response.send_message(f"{streak_text(streak, 3, 180)}\n{texts.NICK_PERMISSION}", ephemeral=True)

@ru_command("setstreak")
@app_commands.describe(user=texts.PARAMS["set_user"], flames=texts.PARAMS["set_flames"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setstreak_command(interaction: discord.Interaction, user: discord.Member, flames: app_commands.Range[int, 0, 10000]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    streak = await db.get_streak(interaction.guild.id, user.id)
    if flames == 0:
        reset_streak(streak, today())
        body = texts.SET_CLEARED.format(mention=user.mention)
    else:
        streak.count = int(flames)
        streak.last_active = today()
        streak.progress_date = today()
        streak.lost_date = None
        streak.last_lost_count = 0
        streak.is_frozen = False
        settings = await db.get_guild_settings(interaction.guild.id)
        streak.messages_today = settings.message_goal
        streak.voice_seconds_today = 0
        body = texts.SET_VALUE.format(mention=user.mention, emoji=emoji_for(streak.count), count=streak.count)
    await db.save_streak(streak)
    nick_ok = await apply_nickname(user, streak, streak.nickname_visible)
    if not nick_ok:
        body += f"\n{texts.NICK_PERMISSION}"
    await interaction.response.send_message(body)

async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = texts.ADMIN_PERMISSION
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return
    raise error

setstreak_command.error(admin_command_error)

@ru_command("resetstreak")
@app_commands.describe(user=texts.PARAMS["reset_user"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def resetstreak_command(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    target = user or interaction.user
    streak = await db.get_streak(interaction.guild.id, target.id)
    reset_streak(streak, today())
    await db.save_streak(streak)
    await apply_nickname(target, streak, streak.nickname_visible)
    if target.id == interaction.user.id:
        body = texts.RESET_SELF
    else:
        body = texts.RESET_OTHER.format(mention=target.mention)
    await interaction.response.send_message(body, ephemeral=True)

resetstreak_command.error(admin_command_error)

@ru_command("restorestreak")
async def restorestreak_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    member = interaction.user
    streak = await db.get_streak(interaction.guild.id, member.id)
    now_date = today()
    if streak.count > 0 and streak.last_active == now_date:
        await interaction.response.send_message(texts.RESTORE_NOTHING, ephemeral=True)
        return
    if streak.last_lost_count <= 0 or not streak.lost_date:
        await interaction.response.send_message(texts.RESTORE_NOTHING, ephemeral=True)
        return
    days_since_lost = (now_date - streak.lost_date).days
    if days_since_lost > 1:
        streak.last_lost_count = 0
        streak.lost_date = None
        await db.save_streak(streak)
        await apply_nickname(member, streak, streak.nickname_visible)
        await interaction.response.send_message(texts.RESTORE_TOO_LATE, ephemeral=True)
        return
    left = restores_left(streak, now_date)
    if left <= 0:
        streak.last_lost_count = 0
        streak.lost_date = None
        await db.save_streak(streak)
        await apply_nickname(member, streak, streak.nickname_visible)
        await interaction.response.send_message(texts.RESTORE_NO_RESTORES_LEFT, ephemeral=True)
        return
    consume_restore(streak, now_date)
    streak.count = streak.last_lost_count
    streak.last_lost_count = 0
    streak.lost_date = None
    streak.last_active = now_date
    streak.progress_date = now_date
    settings = await db.get_guild_settings(interaction.guild.id)
    streak.messages_today = settings.message_goal
    streak.voice_seconds_today = 0
    await db.save_streak(streak)
    nick_ok = await apply_nickname(member, streak, streak.nickname_visible)
    body = texts.RESTORE_USED.format(emoji=emoji_for(streak.count), count=streak.count, flames=texts.flames_word(streak.count))
    body += "\n" + texts.RESTORE_LIMIT_INFO.format(left=restores_left(streak, now_date), total=RESTORES_PER_MONTH)
    if not nick_ok:
        body += f"\n{texts.NICK_PERMISSION}"
    await interaction.response.send_message(body, ephemeral=True)

@ru_command("setmsggoal")
@app_commands.describe(amount=texts.PARAMS["set_msg_amount"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setmsggoal_command(interaction: discord.Interaction, amount: app_commands.Range[int, 0, 1000]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    settings = await db.get_guild_settings(interaction.guild.id)
    if amount == 0:
        settings.message_goal = 3
        body = texts.SET_MSG_GOAL_RESET
    else:
        settings.message_goal = int(amount)
        body = texts.SET_MSG_GOAL_SUCCESS.format(goal=amount)
    await db.save_guild_settings(settings)
    await interaction.response.send_message(body)

setmsggoal_command.error(admin_command_error)

@ru_command("setvoicegoal")
@app_commands.describe(minutes=texts.PARAMS["set_voice_minutes"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setvoicegoal_command(interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 1440]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    settings = await db.get_guild_settings(interaction.guild.id)
    if minutes == 0:
        settings.voice_goal_seconds = 180
        body = texts.SET_VOICE_GOAL_RESET
    else:
        settings.voice_goal_seconds = int(minutes * 60)
        body = texts.SET_VOICE_GOAL_SUCCESS.format(goal=texts.format_goal_time(int(minutes * 60)))
    await db.save_guild_settings(settings)
    await interaction.response.send_message(body)

setvoicegoal_command.error(admin_command_error)

@ru_command("testunlit")
@app_commands.describe(user=texts.PARAMS["test_user"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def testunlit_command(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    target = user or interaction.user
    streak = await db.get_streak(interaction.guild.id, target.id)
    if streak.count <= 0:
        await interaction.response.send_message(texts.TEST_UNLIT_EMPTY.format(mention=target.mention), ephemeral=True)
        return
    streak.last_active = today() - timedelta(days=1)
    await db.save_streak(streak)
    await apply_nickname(target, streak, streak.nickname_visible)
    await interaction.response.send_message(texts.TEST_UNLIT_SUCCESS.format(mention=target.mention), ephemeral=True)

testunlit_command.error(admin_command_error)

@ru_command("setstreakrole")
@app_commands.describe(days=texts.PARAMS["role_days"], role=texts.PARAMS["role_mention"])
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setstreakrole_command(interaction: discord.Interaction, days: app_commands.Range[int, 1, 10000], role: discord.Role) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    await db.save_streak_role(interaction.guild.id, int(days), role.id)
    await interaction.response.send_message(texts.STREAK_ROLE_ADDED.format(days=days, role=role.mention), ephemeral=True)

setstreakrole_command.error(admin_command_error)

@ru_command("delstreakrole")
@app_commands.describe(days=texts.PARAMS["role_days"], role=texts.PARAMS.get("role_mention", "Оставьте пустым, чтобы удалить ВСЕ роли за этот день"))
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def delstreakrole_command(interaction: discord.Interaction, days: app_commands.Range[int, 1, 10000], role: discord.Role | None = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    role_id = role.id if role else None
    await db.delete_streak_role(interaction.guild.id, int(days), role_id)
    if role:
        body = f"🗑️ Награда {role.mention} за **{days}** огоньков удалена."
    else:
        body = f"🗑️ Все награды за **{days}** огоньков удалены."
    await interaction.response.send_message(body, ephemeral=True)

delstreakrole_command.error(admin_command_error)

@ru_command("roleslist")
async def roleslist_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(guild_only_response(), ephemeral=True)
        return
    roles_dict = await db.get_all_streak_roles(interaction.guild.id)
    if not roles_dict:
        await interaction.response.send_message(texts.STREAK_ROLE_LIST_EMPTY, ephemeral=True)
        return
    lines = []
    for days, role_ids in sorted(roles_dict.items()):
        days_roles = []
        for rid in role_ids:
            r = interaction.guild.get_role(rid)
            days_roles.append(r.mention if r else "`удалена`")
        lines.append(f"🔥 **{days}** дней — {', '.join(days_roles)}")
    body = "🎭 **Награды за стрики:**\n" + "\n".join(lines)
    await interaction.response.send_message(body, ephemeral=True)

def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажите DISCORD_TOKEN в файле .env (см. .env.example).")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
