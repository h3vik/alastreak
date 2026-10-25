from __future__ import annotations

import re
from datetime import date, timedelta

from db import Streak

# Регулярка теперь ищет эмодзи и цифру В КОНЦЕ ника (с пробелом перед ними)
STREAK_SUFFIX_RE = re.compile(r"\s+[🔥⚡💎🏆👑🔘❌]\s*\d+$")

MILESTONES = (
    (200, "👑"),
    (100, "🏆"),
    (50, "💎"),
    (10, "⚡"),
    (1, "🔥"),
)

NICK_LIMIT = 32
RESTORES_PER_MONTH = 2


def emoji_for(count: int) -> str:
    for threshold, emoji in MILESTONES:
        if count >= threshold:
            return emoji
    return "🔥"


def next_milestone(count: int) -> tuple[int, str] | None:
    thresholds = sorted({threshold for threshold, _ in MILESTONES if threshold > 1})
    for threshold in thresholds:
        if count < threshold:
            return threshold, emoji_for(threshold)
    return None


def strip_prefix(name: str) -> str:
    # Теперь функция отрезает суффикс (в конце), а не префикс
    return STREAK_SUFFIX_RE.sub("", name, count=1).strip()


def format_nickname(base_name: str, count: int, state: str = "active") -> str:
    if state == "lost":
        suffix = f" ❌{count}"
    elif state == "unlit":
        suffix = f" 🔘{count}"
    else:
        suffix = f" {emoji_for(count)}{count}"
        
    # Ограничиваем длину ника (в Discord максимум 32 символа)
    remaining = NICK_LIMIT - len(suffix)
    trimmed = base_name[:remaining].rstrip() if remaining > 0 else ""
    return f"{trimmed}{suffix}"


def is_streak_restorable(streak: Streak, today_date: date) -> bool:
    if streak.count > 0 or streak.last_lost_count <= 0 or not streak.lost_date:
        return False
    if restores_left(streak, today_date) <= 0:
        return False
    return (today_date - streak.lost_date).days <= 1


def apply_qualification(streak: Streak, today: date) -> tuple[Streak, bool]:
    if streak.last_active == today:
        return streak, False

    if streak.last_active == today - timedelta(days=1):
        new_count = streak.count + 1
    else:
        new_count = 1

    streak.count = new_count
    streak.last_active = today
    return streak, True


def qualifies_today(streak: Streak, msg_goal: int, voice_goal: int) -> bool:
    return (
        streak.messages_today >= msg_goal
        or streak.voice_seconds_today >= voice_goal
    )


def roll_progress_day(streak: Streak, today: date) -> None:
    if streak.progress_date != today:
        streak.progress_date = today
        streak.messages_today = 0
        streak.voice_seconds_today = 0


def add_progress(
    streak: Streak,
    today: date,
    msg_goal: int,
    voice_goal: int,
    *,
    messages: int = 0,
    voice_seconds: int = 0,
) -> tuple[Streak, bool]:
    roll_progress_day(streak, today)
    streak.messages_today += messages
    streak.voice_seconds_today += voice_seconds
    
    if not qualifies_today(streak, msg_goal, voice_goal):
        return streak, False
        
    if streak.last_active is None:
        return apply_qualification(streak, today)
        
    delta = (today - streak.last_active).days
    if delta > 1:
        if streak.count > 0:
            if restores_left(streak, today) <= 0:
                streak.last_lost_count = 0
                streak.lost_date = None
            else:
                streak.last_lost_count = streak.count
                streak.lost_date = streak.last_active + timedelta(days=1)
        return apply_qualification(streak, today)
        
    return apply_qualification(streak, today)


def reset_streak(streak: Streak, today_date: date) -> Streak:
    if streak.count > 0:
        streak.last_lost_count = streak.count
        streak.lost_date = today_date
    else:
        streak.last_lost_count = 0
        streak.lost_date = None
        
    streak.count = 0
    streak.last_active = None
    streak.progress_date = None
    streak.messages_today = 0
    streak.voice_seconds_today = 0
    return streak


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def restores_left(streak: Streak, today_date: date) -> int:
    if streak.restore_uses_month != month_key(today_date):
        return RESTORES_PER_MONTH
    return max(0, RESTORES_PER_MONTH - streak.restore_uses_count)


def consume_restore(streak: Streak, today_date: date) -> None:
    key = month_key(today_date)
    if streak.restore_uses_month != key:
        streak.restore_uses_month = key
        streak.restore_uses_count = 0
    streak.restore_uses_count += 1