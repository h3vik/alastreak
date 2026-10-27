from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import aiosqlite

# Ищем переменную DATA_DIR, которую дает хостинг для постоянного хранения
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = Path(DATA_DIR) / "streaks.db"

_NEW_COLUMNS = (
    ("progress_date", "TEXT"),
    ("messages_today", "INTEGER NOT NULL DEFAULT 0"),
    ("voice_seconds_today", "INTEGER NOT NULL DEFAULT 0"),
    ("restore_uses_month", "TEXT"),
    ("restore_uses_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_lost_count", "INTEGER NOT NULL DEFAULT 0"),
    ("lost_date", "TEXT"),
)

@dataclass
class Streak:
    guild_id: int
    user_id: int
    count: int
    last_active: date | None
    nickname_visible: bool
    progress_date: date | None = None
    messages_today: int = 0
    voice_seconds_today: int = 0
    restore_uses_month: str | None = None
    restore_uses_count: int = 0
    last_lost_count: int = 0
    lost_date: date | None = None

@dataclass
class GuildSettings:
    guild_id: int
    message_goal: int
    voice_goal_seconds: int

async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS streaks (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                last_active TEXT,
                nickname_visible INTEGER NOT NULL DEFAULT 1,
                progress_date TEXT,
                messages_today INTEGER NOT NULL DEFAULT 0,
                voice_seconds_today INTEGER NOT NULL DEFAULT 0,
                restore_uses_month TEXT,
                restore_uses_count INTEGER NOT NULL DEFAULT 0,
                last_lost_count INTEGER NOT NULL DEFAULT 0,
                lost_date TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        async with db.execute("PRAGMA table_info(streaks)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, definition in _NEW_COLUMNS:
            if name not in existing:
                await db.execute(f"ALTER TABLE streaks ADD COLUMN {name} {definition}")
        
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                message_goal INTEGER NOT NULL DEFAULT 3,
                voice_goal_seconds INTEGER NOT NULL DEFAULT 180
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS streak_roles (
                guild_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, days, role_id)
            )
            """
        )
        await db.commit()

def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None

def streak_from_row(row: aiosqlite.Row) -> Streak:
    keys = set(row.keys())
    return Streak(
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        count=row["count"],
        last_active=_parse_date(row["last_active"]),
        nickname_visible=bool(row["nickname_visible"]),
        progress_date=_parse_date(row["progress_date"]) if "progress_date" in keys else None,
        messages_today=int(row["messages_today"] or 0) if "messages_today" in keys else 0,
        voice_seconds_today=int(row["voice_seconds_today"] or 0) if "voice_seconds_today" in keys else 0,
        restore_uses_month=row["restore_uses_month"] if "restore_uses_month" in keys else None,
        restore_uses_count=int(row["restore_uses_count"] or 0) if "restore_uses_count" in keys else 0,
        last_lost_count=int(row["last_lost_count"] or 0) if "last_lost_count" in keys else 0,
        lost_date=_parse_date(row["lost_date"]) if "lost_date" in keys else None,
    )

async def get_streak(guild_id: int, user_id: int) -> Streak:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM streaks WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return Streak(guild_id=guild_id, user_id=user_id, count=0, last_active=None, nickname_visible=True)
    return streak_from_row(row)

async def save_streak(streak: Streak) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO streaks (
                guild_id, user_id, count, last_active, nickname_visible,
                progress_date, messages_today, voice_seconds_today,
                restore_uses_month, restore_uses_count, last_lost_count, lost_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                count = excluded.count,
                last_active = excluded.last_active,
                nickname_visible = excluded.nickname_visible,
                progress_date = excluded.progress_date,
                messages_today = excluded.messages_today,
                voice_seconds_today = excluded.voice_seconds_today,
                restore_uses_month = excluded.restore_uses_month,
                restore_uses_count = excluded.restore_uses_count,
                last_lost_count = excluded.last_lost_count,
                lost_date = excluded.lost_date
            """,
            (
                streak.guild_id, streak.user_id, streak.count,
                streak.last_active.isoformat() if streak.last_active else None,
                int(streak.nickname_visible),
                streak.progress_date.isoformat() if streak.progress_date else None,
                streak.messages_today, streak.voice_seconds_today,
                streak.restore_uses_month, streak.restore_uses_count,
                streak.last_lost_count,
                streak.lost_date.isoformat() if streak.lost_date else None,
            ),
        )
        await db.commit()

async def list_active_streaks(guild_id: int, today: date, limit: int = 15) -> list[Streak]:
    yesterday = today - timedelta(days=1)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM streaks
            WHERE guild_id = ? AND count > 0 AND last_active IN (?, ?)
            ORDER BY count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, today.isoformat(), yesterday.isoformat(), limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [streak_from_row(row) for row in rows]

async def get_guild_settings(guild_id: int) -> GuildSettings:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
            
    if row is None:
        return GuildSettings(guild_id=guild_id, message_goal=3, voice_goal_seconds=180)
    return GuildSettings(guild_id=row["guild_id"], message_goal=row["message_goal"], voice_goal_seconds=row["voice_goal_seconds"])

async def save_guild_settings(settings: GuildSettings) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO guild_settings (guild_id, message_goal, voice_goal_seconds)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                message_goal = excluded.message_goal,
                voice_goal_seconds = excluded.voice_goal_seconds
            """,
            (settings.guild_id, settings.message_goal, settings.voice_goal_seconds),
        )
        await db.commit()

async def get_all_streak_roles(guild_id: int) -> dict[int, list[int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT days, role_id FROM streak_roles WHERE guild_id = ?", (guild_id,)) as cursor:
            rows = await cursor.fetchall()
            result = {}
            for days, role_id in rows:
                if days not in result:
                    result[days] = []
                result[days].append(role_id)
            return result

async def save_streak_role(guild_id: int, days: int, role_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO streak_roles (guild_id, days, role_id) VALUES (?, ?, ?)",
            (guild_id, days, role_id),
        )
        await db.commit()

async def delete_streak_role(guild_id: int, days: int, role_id: int | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if role_id is not None:
            await db.execute("DELETE FROM streak_roles WHERE guild_id = ? AND days = ? AND role_id = ?", (guild_id, days, role_id))
        else:
            await db.execute("DELETE FROM streak_roles WHERE guild_id = ? AND days = ?", (guild_id, days))
        await db.commit()
