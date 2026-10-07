import os
import html
import math
import asyncio
import logging
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, BaseMiddleware, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURATION ---
# Optional: load variables from a local .env file (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# The token is NEVER stored in the code. Set it as an environment variable (BOT_TOKEN).
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Add it as an environment variable (see README / host settings).")

OWNER_ID = int(os.getenv("OWNER_ID", 8780228920))
UZB_TZ = ZoneInfo("Asia/Tashkent")

# Ensure the data directory exists
os.makedirs("data", exist_ok=True)

# Save SQLite database inside the persistent volume folder
conn = sqlite3.connect("data/subscribers.db", check_same_thread=False)
cursor = conn.cursor()

# --- DATABASE SETUP ---
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        role TEXT DEFAULT 'user'
    )
""")


def ensure_column(table: str, column: str, ddl: str):
    """Adds a column to an existing table if it is missing (safe migration)."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = [row[1] for row in cursor.fetchall()]
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


ensure_column("users", "is_active", "INTEGER DEFAULT 1")
ensure_column("users", "banned", "INTEGER DEFAULT 0")
ensure_column("users", "morning_alert", "INTEGER DEFAULT 1")
ensure_column("users", "lesson_alerts", "INTEGER DEFAULT 1")
ensure_column("users", "announcements", "INTEGER DEFAULT 1")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS timetable (
        day INTEGER,
        pos INTEGER,
        subject TEXT,
        room TEXT,
        teachers TEXT,
        PRIMARY KEY (day, pos)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS overrides (
        day_date TEXT,
        lesson INTEGER,
        kind TEXT,
        note TEXT,
        PRIMARY KEY (day_date, lesson)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")
conn.commit()

# Notification settings users can toggle in /settings
SETTING_LABELS = {
    "morning_alert": "☀️ Ertalabki xabar",
    "lesson_alerts": "🔔 Dars tugashi xabarlari",
    "announcements": "📢 E'lonlar",
}

# Counters since the bot was started (shown in /stats)
STATS = {"sent": 0, "blocked": 0, "failed": 0}


def esc(value) -> str:
    return html.escape(str(value))


# --- USER HELPERS ---
def register_or_update_user(user_id: int, username: str, first_name: str):
    user_id = int(user_id)
    role = "owner" if user_id == OWNER_ID else "user"
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            is_active=1,
            role=CASE WHEN user_id = ? THEN 'owner' ELSE users.role END
    """, (user_id, username or "NoUsername", first_name or "User", role, OWNER_ID))
    conn.commit()


def get_user_role(user_id: int) -> str:
    user_id = int(user_id)
    if user_id == OWNER_ID:
        return "owner"
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else "user"


def is_admin_or_owner(user_id: int) -> bool:
    return get_user_role(user_id) in ["owner", "admin"]


def get_all_users():
    cursor.execute("SELECT user_id, username, first_name, role, is_active, banned FROM users")
    return cursor.fetchall()


def set_user_role(user_id: int, role: str):
    cursor.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, int(user_id)))
    conn.commit()


def user_exists(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (int(user_id),))
    return cursor.fetchone() is not None


def parse_id(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_banned(user_id: int) -> bool:
    cursor.execute("SELECT banned FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    return bool(row and row[0])


def set_banned(user_id: int, value: bool):
    cursor.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if value else 0, int(user_id)))
    conn.commit()


def mark_inactive(user_id: int):
    cursor.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (int(user_id),))
    conn.commit()


def get_recipients(flag: str | None = None) -> list[int]:
    """Active, non-banned users. If flag is given, only those who have it switched on."""
    query = "SELECT user_id FROM users WHERE is_active = 1 AND banned = 0"
    if flag in SETTING_LABELS:
        query += f" AND {flag} = 1"
    cursor.execute(query)
    return [row[0] for row in cursor.fetchall()]


def get_settings(user_id: int) -> dict:
    cursor.execute(
        "SELECT morning_alert, lesson_alerts, announcements FROM users WHERE user_id = ?",
        (int(user_id),),
    )
    row = cursor.fetchone() or (1, 1, 1)
    return dict(zip(SETTING_LABELS.keys(), row))


def subscribe_if_stopped(user_id: int):
    """/start turns alerts back on for someone who had used /stop."""
    settings = get_settings(user_id)
    if not any(settings.values()):
        cursor.execute(
            "UPDATE users SET morning_alert = 1, lesson_alerts = 1, announcements = 1 WHERE user_id = ?",
            (int(user_id),),
        )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# --- TIMETABLE & BELL SCHEDULE (10-B Aniq) ---
DAY_NAMES = {
    0: "Dushanba (Monday)",
    1: "Seshanba (Tuesday)",
    2: "Chorshanba (Wednesday)",
    3: "Payshanba (Thursday)",
    4: "Juma (Friday)",
}

DAY_CODE_MAP = {
    "1": 0, "du": 0, "dushanba": 0, "mon": 0, "monday": 0,
    "2": 1, "se": 1, "seshanba": 1, "tue": 1, "tuesday": 1,
    "3": 2, "ch": 2, "chorshanba": 2, "wed": 2, "wednesday": 2,
    "4": 3, "pa": 3, "payshanba": 3, "thu": 3, "thursday": 3,
    "5": 4, "ju": 4, "juma": 4, "fri": 4, "friday": 4,
}

# Default timetable. It is copied into the database the FIRST time the bot runs.
# After that the database is the source of truth (edit it with /setlesson and /dellesson).
DEFAULT_TIMETABLE = {
    0: [  # 1 - Dushanba
        {"subject": "Algebra", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "Kelajak soati", "room": "204", "teachers": "Umarbek"},
        {"subject": "Ona tili", "room": "208", "teachers": "Q.Umid"},
        {"subject": "Informatika", "room": "220", "teachers": "Xursand, Umarbek"},
        {"subject": "Ingliz tili", "room": "128", "teachers": "Rufat, Muzaffar"},
        {"subject": "Rus tili", "room": "202", "teachers": "Gulzoda, Sevara"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Ulug'bek"},
    ],
    1: [  # 2 - Seshanba
        {"subject": "O'zbek tarix", "room": "113", "teachers": "Murod"},
        {"subject": "Adabiyot", "room": "208", "teachers": "Q.Umid"},
        {"subject": "Algebra", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "Geometriya", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "CHQBT", "room": "220", "teachers": "To'lqin"},
        {"subject": "Ingliz tili", "room": "220", "teachers": "Rufat, Muzaffar"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Ulug'bek"},
    ],
    2: [  # 3 - Chorshanba
        {"subject": "Ona tili", "room": "208", "teachers": "Q.Umid"},
        {"subject": "Algebra", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "Fizika", "room": "218", "teachers": "O'g'lijon, Ulug'bek"},
        {"subject": "Rus tili", "room": "128", "teachers": "Gulzoda, Sevara"},
        {"subject": "Ingliz tili", "room": "220", "teachers": "Rufat, Muzaffar"},
        {"subject": "CHQBT", "room": "129", "teachers": "To'lqin"},
    ],
    3: [  # 4 - Payshanba
        {"subject": "Tarbiya", "room": "132", "teachers": "Azada"},
        {"subject": "Fizika", "room": "218", "teachers": "O'g'lijon, Ulug'bek"},
        {"subject": "Algebra", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "Geometriya", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "Ingliz tili", "room": "220", "teachers": "Rufat, Muzaffar"},
        {"subject": "Adabiyot", "room": "208", "teachers": "Q.Umid"},
    ],
    4: [  # 5 - Juma
        {"subject": "Jismoniy tarbiya", "room": "Sport zal", "teachers": "Ulug'bek"},
        {"subject": "Geometriya", "room": "218", "teachers": "Umid, Muhammadsodiq"},
        {"subject": "O'zbek tarix", "room": "113", "teachers": "Murod"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Ulug'bek"},
        {"subject": "Jahon tarix", "room": "113", "teachers": "Murod"},
        {"subject": "Informatika", "room": "N/A", "teachers": "Xursand, Umarbek"},
    ],
}

BELL_SCHEDULE = [
    {"lesson": 1, "start": "09:00", "end": "09:45"},
    {"lesson": 2, "start": "09:50", "end": "10:35"},
    {"lesson": 3, "start": "10:40", "end": "11:25"},
    {"lesson": 4, "start": "11:30", "end": "12:15"},
    {"lesson": 5, "start": "12:45", "end": "13:30"},
    {"lesson": 6, "start": "13:35", "end": "14:20"},
    {"lesson": 7, "start": "14:25", "end": "15:10"},
]

# Live timetable used by the whole bot (loaded from the database)
TIMETABLE: dict[int, list[dict]] = {}


def seed_timetable_if_empty():
    cursor.execute("SELECT COUNT(*) FROM timetable")
    if cursor.fetchone()[0] == 0:
        for day, lessons in DEFAULT_TIMETABLE.items():
            for pos, item in enumerate(lessons, start=1):
                cursor.execute(
                    "INSERT INTO timetable (day, pos, subject, room, teachers) VALUES (?, ?, ?, ?, ?)",
                    (day, pos, item["subject"], item["room"], item["teachers"]),
                )
        conn.commit()


def load_timetable():
    """Reloads TIMETABLE from the database (only days that have lessons)."""
    cursor.execute("SELECT day, pos, subject, room, teachers FROM timetable ORDER BY day, pos")
    fresh: dict[int, list[dict]] = {}
    for day, _pos, subject, room, teachers in cursor.fetchall():
        fresh.setdefault(day, []).append({"subject": subject, "room": room, "teachers": teachers})
    TIMETABLE.clear()
    TIMETABLE.update(fresh)


seed_timetable_if_empty()
load_timetable()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- BAN MIDDLEWARE (banned users are silently ignored) ---
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != OWNER_ID and is_banned(user.id):
            if isinstance(event, types.CallbackQuery):
                await event.answer("⛔", show_alert=True)
            return None
        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# --- DATE / OVERRIDE HELPERS ---
def parse_date(raw: str | None) -> date | None:
    """Accepts YYYY-MM-DD, 'bugun' / 'today', 'ertaga' / 'tomorrow'."""
    if not raw:
        return None
    raw = raw.strip().lower()
    today = datetime.now(UZB_TZ).date()
    if raw in ("bugun", "today"):
        return today
    if raw in ("ertaga", "tomorrow"):
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def week_start(today: date) -> date:
    """Monday of this week (on Saturday/Sunday: Monday of the coming week)."""
    wd = today.weekday()
    return today - timedelta(days=wd) if wd < 5 else today + timedelta(days=7 - wd)


def get_off_reason(d: date) -> str | None:
    """Returns the reason if the day is marked 'no school', otherwise None."""
    cursor.execute(
        "SELECT note FROM overrides WHERE day_date = ? AND lesson = 0 AND kind = 'off'",
        (d.isoformat(),),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_notes(d: date) -> dict[int, str]:
    cursor.execute(
        "SELECT lesson, note FROM overrides WHERE day_date = ? AND kind = 'note'",
        (d.isoformat(),),
    )
    return {lesson: note for lesson, note in cursor.fetchall()}


def effective_lessons(d: date) -> list[dict]:
    """Lessons that really take place on a date (no lessons on weekends/holidays)."""
    wd = d.weekday()
    if wd not in TIMETABLE or get_off_reason(d) is not None:
        return []
    return TIMETABLE[wd][:len(BELL_SCHEDULE)]


def lesson_dt(d: date, hm: str) -> datetime:
    hour, minute = map(int, hm.split(":"))
    return datetime.combine(d, time(hour, minute), tzinfo=UZB_TZ)


def fmt_minutes(total: int) -> str:
    if total >= 60:
        hours, mins = divmod(total, 60)
        return f"{hours} soat {mins} daqiqa" if mins else f"{hours} soat"
    return f"{total} daqiqa"


def day_label(d: date, today: date) -> str:
    diff = (d - today).days
    if diff == 0:
        return "Bugun"
    if diff == 1:
        return "Ertaga"
    return DAY_NAMES.get(d.weekday(), d.isoformat()).split(" ")[0]


# --- FORMATTING ---
def format_date_schedule(d: date) -> str:
    wd = d.weekday()
    label = d.strftime("%d.%m.%Y")

    if wd not in TIMETABLE:
        return f"🎉 <b>Dam olish kuni! Darslar yo'q.</b> ({label})"

    day_title = DAY_NAMES.get(wd, "Dars Jadvali")
    off = get_off_reason(d)
    if off is not None:
        text = f"🎉 <b>{esc(day_title)} ({label}) — dars yo'q.</b>"
        if off:
            text += f"\n{esc(off)}"
        return text

    lessons = TIMETABLE[wd][:len(BELL_SCHEDULE)]
    notes = get_notes(d)
    text = f"📅 <b>Dars Jadvali: {esc(day_title)}</b> ({label})\n\n"

    for idx, item in enumerate(lessons):
        bell = BELL_SCHEDULE[idx]
        room_str = f"| 🚪 Xona: {esc(item['room'])}" if item.get("room") else ""
        text += f"<b>{bell['lesson']}-dars ({bell['start']} - {bell['end']}):</b> {esc(item['subject'])}\n"
        text += f"└ 👨‍🏫 <i>O'qituvchi:</i> {esc(item['teachers'])} {room_str}\n"
        if bell["lesson"] in notes:
            text += f"└ ⚠️ <b>{esc(notes[bell['lesson']])}</b>\n"
        text += "\n"

    return text


def format_week(start: date) -> str:
    text = "🗓 <b>Haftalik jadval</b>\n\n"
    for wd in range(5):
        d = start + timedelta(days=wd)
        text += f"<b>{esc(DAY_NAMES[wd])}</b> ({d.strftime('%d.%m')})"
        off = get_off_reason(d)
        if off is not None:
            text += " — 🎉 dars yo'q\n\n"
            continue
        text += "\n"
        notes = get_notes(d)
        for idx, item in enumerate(TIMETABLE.get(wd, [])[:len(BELL_SCHEDULE)]):
            mark = " ⚠️" if (idx + 1) in notes else ""
            text += f"{idx + 1}. {esc(item['subject'])} ({esc(item['room'])}){mark}\n"
        text += "\n"
    return text


def day_keyboard() -> InlineKeyboardMarkup:
    names = ["Du", "Se", "Ch", "Pa", "Ju"]
    row = [InlineKeyboardButton(text=n, callback_data=f"day:{i}") for i, n in enumerate(names)]
    today_row = [InlineKeyboardButton(text="📍 Bugun", callback_data="day:today")]
    return InlineKeyboardMarkup(inline_keyboard=[row, today_row])


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    rows = []
    for key, label in SETTING_LABELS.items():
        mark = "✅" if settings.get(key) else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"set:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- /now and /next LOGIC ---
def find_current(now: datetime):
    lessons = effective_lessons(now.date())
    for idx, item in enumerate(lessons):
        bell = BELL_SCHEDULE[idx]
        start = lesson_dt(now.date(), bell["start"])
        end = lesson_dt(now.date(), bell["end"])
        if start <= now < end:
            return idx, item, end
    return None


def find_next(now: datetime):
    for offset in range(0, 8):
        d = now.date() + timedelta(days=offset)
        for idx, item in enumerate(effective_lessons(d)):
            start = lesson_dt(d, BELL_SCHEDULE[idx]["start"])
            if start > now:
                return d, idx, item, start
    return None


def next_lesson_text(now: datetime) -> str:
    nxt = find_next(now)
    if not nxt:
        return "ℹ️ Yaqin kunlarda dars topilmadi."

    d, idx, item, start = nxt
    bell = BELL_SCHEDULE[idx]
    note = get_notes(d).get(bell["lesson"])

    text = (
        f"➡️ <b>Keyingi dars:</b> {bell['lesson']}-dars — {esc(item['subject'])}\n"
        f"⏰ {day_label(d, now.date())}, {bell['start']}"
    )
    if d == now.date():
        mins = math.ceil((start - now).total_seconds() / 60)
        text += f" ({fmt_minutes(mins)}dan keyin)"
    text += f"\n🚪 Xona: {esc(item['room'])}\n👨‍🏫 O'qituvchi: {esc(item['teachers'])}"
    if note:
        text += f"\n⚠️ <b>{esc(note)}</b>"
    return text


# --- SAFE SENDING (handles blocked users and rate limits) ---
async def safe_send(user_id: int, text: str, parse_mode: str = "HTML") -> str:
    """Returns 'ok', 'blocked' or 'failed'. Users who blocked the bot are marked inactive."""
    for _ in range(2):
        try:
            await bot.send_message(user_id, text, parse_mode=parse_mode)
            STATS["sent"] += 1
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            mark_inactive(user_id)
            STATS["blocked"] += 1
            return "blocked"
        except TelegramBadRequest as e:
            message_text = str(e).lower()
            if "chat not found" in message_text or "deactivated" in message_text:
                mark_inactive(user_id)
                STATS["blocked"] += 1
                return "blocked"
            logging.error(f"Bad request for user {user_id}: {e}")
            STATS["failed"] += 1
            return "failed"
        except Exception as e:
            logging.error(f"Failed to send to user {user_id}: {e}")
            STATS["failed"] += 1
            return "failed"
    STATS["failed"] += 1
    return "failed"


async def broadcast_message(text: str, flag: str | None = None) -> dict:
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for u_id in get_recipients(flag):
        status = await safe_send(u_id, text)
        result[status] += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    return result


# --- PERMISSION HELPERS ---
async def require_admin(message: types.Message) -> bool:
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not is_admin_or_owner(message.from_user.id):
        await message.answer("⛔ Faqat adminlar foydalana oladi.")
        return False
    return True


async def require_owner(message: types.Message, text: str) -> bool:
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if get_user_role(message.from_user.id) != "owner":
        await message.answer(text)
        return False
    return True


async def safe_edit(call: types.CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# --- ERROR LOGGING (so crashes are never silent) ---
@dp.errors()
async def on_error(event: ErrorEvent):
    logging.exception("Handler crashed: %s", event.exception)


# --- HELP TEXTS ---
USER_HELP = (
    "📖 <b>Buyruqlar</b>\n\n"
    "• <code>/schedule</code> — bugungi jadval (<code>/schedule 1</code> … <code>/schedule 5</code>)\n"
    "• <code>/tomorrow</code> — ertangi jadval\n"
    "• <code>/week</code> — haftalik jadval\n"
    "• <code>/now</code> — hozirgi dars\n"
    "• <code>/next</code> — keyingi dars\n"
    "• <code>/settings</code> — xabarlarni sozlash\n"
    "• <code>/stop</code> — barcha xabarlarni o'chirish"
)

ADMIN_HELP = (
    "\n\n🛠 <b>Admin buyruqlari</b>\n\n"
    "• <code>/users</code> — foydalanuvchilar ro'yxati\n"
    "• <code>/stats</code> — statistika\n"
    "• <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code>\n"
    "• <code>/broadcast &lt;xabar&gt;</code>\n"
    "• <code>/ban &lt;user_id&gt;</code> / <code>/unban &lt;user_id&gt;</code>\n"
    "• <code>/holiday &lt;sana&gt; [sabab]</code> — dars yo'q kuni\n"
    "• <code>/change &lt;sana&gt; &lt;dars#&gt; &lt;matn&gt;</code> — darsga eslatma\n"
    "• <code>/changes</code> — o'zgarishlar ro'yxati\n"
    "• <code>/clearchange &lt;sana&gt;</code> — o'zgarishlarni o'chirish\n"
    "• <code>/setlesson &lt;kun&gt; &lt;dars#&gt; &lt;fan&gt;; &lt;xona&gt;; &lt;o'qituvchilar&gt;</code>\n"
    "• <code>/dellesson &lt;kun&gt;</code> — kunning oxirgi darsini o'chirish\n"
    "Sana: <code>YYYY-MM-DD</code>, <code>bugun</code> yoki <code>ertaga</code>"
)

OWNER_HELP = (
    "\n\n👑 <b>Owner buyruqlari</b>\n\n"
    "• <code>/makeadmin &lt;user_id&gt;</code>\n"
    "• <code>/removeadmin &lt;user_id&gt;</code>\n"
    "• <code>/inbox on|off</code> — foydalanuvchi xabarlarini senga yuborish"
)


# --- USER COMMANDS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    subscribe_if_stopped(message.from_user.id)
    await message.answer(
        "👋 <b>Xush kelibsiz!</b> Bot sizni eslab qoldi.\n\n"
        "✅ <b>Endi har kuni darslar va dars tugashi haqida avtomatik xabarlar olasiz!</b>\n\n"
        "• Bugungi jadval: <code>/schedule</code>\n"
        "• Kunlar bo'yicha: <code>/schedule 1</code> (Dushanba) dan <code>/schedule 5</code> (Juma) gacha\n"
        "• Qisqa kodlar: <code>/schedule du</code>, <code>/schedule se</code>, <code>/schedule ch</code>, "
        "<code>/schedule pa</code>, <code>/schedule ju</code>\n"
        "• Hozirgi va keyingi dars: <code>/now</code>, <code>/next</code>\n"
        "• Sozlamalar: <code>/settings</code>\n"
        "• Barcha buyruqlar: <code>/help</code>\n\n"
        "ℹ️ Botga yozgan xabarlaringizni bot egasi ko'rishi mumkin.",
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = USER_HELP
    if is_admin_or_owner(message.from_user.id):
        text += ADMIN_HELP
    if get_user_role(message.from_user.id) == "owner":
        text += OWNER_HELP
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("schedule", "timetable"))
async def cmd_schedule(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    args = message.text.split()
    today = datetime.now(UZB_TZ).date()

    if len(args) > 1:
        query_arg = args[1].lower()
        if query_arg in DAY_CODE_MAP:
            target = week_start(today) + timedelta(days=DAY_CODE_MAP[query_arg])
        else:
            await message.answer(
                "❌ <b>Noto'g'ri kun kiritildi!</b>\n\n"
                "Misol uchun:\n"
                "• <code>/schedule 1</code> (Dushanba)\n"
                "• <code>/schedule 2</code> (Seshanba)\n"
                "• <code>/schedule 5</code> (Juma)\n"
                "• Yoki kodi bilan: <code>/schedule du</code>",
                parse_mode="HTML"
            )
            return
    else:
        target = today

    await message.answer(format_date_schedule(target), parse_mode="HTML", reply_markup=day_keyboard())


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    target = datetime.now(UZB_TZ).date() + timedelta(days=1)
    await message.answer(format_date_schedule(target), parse_mode="HTML", reply_markup=day_keyboard())


@dp.message(Command("week"))
async def cmd_week(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    start = week_start(datetime.now(UZB_TZ).date())
    await message.answer(format_week(start), parse_mode="HTML")


@dp.message(Command("now"))
async def cmd_now(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    now = datetime.now(UZB_TZ)
    current = find_current(now)

    if current:
        idx, item, end = current
        bell = BELL_SCHEDULE[idx]
        left = math.ceil((end - now).total_seconds() / 60)
        text = (
            f"📍 <b>Hozir:</b> {bell['lesson']}-dars — {esc(item['subject'])}\n"
            f"🚪 Xona: {esc(item['room'])}\n"
            f"👨‍🏫 O'qituvchi: {esc(item['teachers'])}\n"
            f"⏳ Tugashiga {fmt_minutes(left)} qoldi\n\n"
        )
    else:
        text = "☕ <b>Hozir dars yo'q.</b>\n\n"

    text += next_lesson_text(now)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("next"))
async def cmd_next(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(next_lesson_text(datetime.now(UZB_TZ)), parse_mode="HTML")


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "⚙️ <b>Sozlamalar</b>\nQaysi xabarlarni olishni tanlang (tugmani bosib yoqing/o'chiring):",
        parse_mode="HTML",
        reply_markup=settings_keyboard(get_settings(message.from_user.id)),
    )


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    cursor.execute(
        "UPDATE users SET morning_alert = 0, lesson_alerts = 0, announcements = 0 WHERE user_id = ?",
        (message.from_user.id,),
    )
    conn.commit()
    await message.answer(
        "🔕 Barcha avtomatik xabarlar o'chirildi.\n"
        "Qayta yoqish uchun /start yoki /settings ni bosing."
    )


# --- INLINE BUTTON CALLBACKS ---
@dp.callback_query(F.data.startswith("day:"))
async def cb_day(call: types.CallbackQuery):
    register_or_update_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    key = call.data.split(":", 1)[1]
    today = datetime.now(UZB_TZ).date()

    if key == "today":
        target = today
    elif key.isdigit() and 0 <= int(key) <= 4:
        target = week_start(today) + timedelta(days=int(key))
    else:
        await call.answer()
        return

    await safe_edit(call, format_date_schedule(target), day_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(call: types.CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in SETTING_LABELS:
        await call.answer()
        return

    register_or_update_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    cursor.execute(f"UPDATE users SET {key} = 1 - {key} WHERE user_id = ?", (call.from_user.id,))
    conn.commit()

    try:
        await call.message.edit_reply_markup(reply_markup=settings_keyboard(get_settings(call.from_user.id)))
    except TelegramBadRequest:
        pass
    await call.answer("Saqlandi ✅")


# --- OWNER / ADMIN COMMANDS ---
@dp.message(Command("users"))
async def cmd_list_users(message: types.Message):
    if not await require_admin(message):
        return

    users = get_all_users()
    if not users:
        await message.answer("👥 Hozircha foydalanuvchilar yo'q.")
        return

    lines = ["👥 <b>Foydalanuvchilar Ro'yxati:</b>\n"]
    for u_id, uname, fname, role, is_active, banned in users:
        name = esc(fname or "NoName")
        un = f"@{esc(uname)}" if uname and uname != "NoUsername" else "NoUsername"
        flags = (" 🚫" if banned else "") + (" 💤" if not is_active else "")
        lines.append(f"• <b>{name}</b> ({un}) | ID: <code>{u_id}</code> | <code>{role}</code>{flags}")
    lines.append("\n🚫 — ban qilingan, 💤 — botni bloklagan")
    text = "\n".join(lines)

    # Telegram limit is 4096 chars per message
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], parse_mode="HTML")


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not await require_admin(message):
        return

    def count(where: str) -> int:
        cursor.execute(f"SELECT COUNT(*) FROM users WHERE {where}")
        return cursor.fetchone()[0]

    total = count("1=1")
    active = count("is_active = 1 AND banned = 0")
    inactive = count("is_active = 0")
    banned = count("banned = 1")
    admins = count("role IN ('admin', 'owner')")
    morning = count("is_active = 1 AND banned = 0 AND morning_alert = 1")
    lessons = count("is_active = 1 AND banned = 0 AND lesson_alerts = 1")
    announce = count("is_active = 1 AND banned = 0 AND announcements = 1")

    await message.answer(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total}</b>\n"
        f"✅ Faol: <b>{active}</b>\n"
        f"💤 Botni bloklagan: <b>{inactive}</b>\n"
        f"🚫 Ban qilingan: <b>{banned}</b>\n"
        f"🛡 Adminlar (owner bilan): <b>{admins}</b>\n\n"
        f"☀️ Ertalabki xabar yoqilgan: <b>{morning}</b>\n"
        f"🔔 Dars tugashi xabari yoqilgan: <b>{lessons}</b>\n"
        f"📢 E'lonlar yoqilgan: <b>{announce}</b>\n\n"
        "📨 <b>Bot ishga tushgandan beri:</b>\n"
        f"• Yuborilgan: <b>{STATS['sent']}</b>\n"
        f"• Bloklangan: <b>{STATS['blocked']}</b>\n"
        f"• Xatolik: <b>{STATS['failed']}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("dm"))
async def cmd_direct_message(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    parts = (command.args or "").split(maxsplit=1)
    target_id = parse_id(parts[0]) if parts else None
    if target_id is None or len(parts) < 2:
        await message.answer("⚠️ Ishlatish: <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code>", parse_mode="HTML")
        return

    try:
        await bot.send_message(target_id, f"📩 <b>Admin Xabari:</b>\n\n{esc(parts[1])}", parse_mode="HTML")
        await message.answer(f"✅ Xabar <code>{target_id}</code> ga yuborildi!", parse_mode="HTML")
    except TelegramForbiddenError:
        mark_inactive(target_id)
        await message.answer("❌ Foydalanuvchi botni bloklagan.")
    except Exception as e:
        await message.answer(f"❌ Yuborib bo'lmadi: {esc(e)}", parse_mode="HTML")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    if not command.args:
        await message.answer("⚠️ Ishlatish: <code>/broadcast &lt;xabar&gt;</code>", parse_mode="HTML")
        return

    text = f"📢 <b>E'lon:</b>\n\n{esc(command.args)}"
    result = await broadcast_message(text, flag="announcements")

    await message.answer(
        f"✅ Yuborildi: {result['ok']}\n"
        f"🚫 Bloklagan: {result['blocked']}\n"
        f"❌ Xatolik: {result['failed']}"
    )


@dp.message(Command("makeadmin"))
async def cmd_make_admin(message: types.Message, command: CommandObject):
    if not await require_owner(message, "❌ Faqat Owner admin tayinlashi mumkin!"):
        return

    target_id = parse_id(command.args.split()[0]) if command.args else None
    if target_id is None:
        await message.answer("⚠️ Ishlatish: <code>/makeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bu foydalanuvchi botda yo'q (u avval /start bosishi kerak).")
        return
    if target_id == OWNER_ID:
        await message.answer("👑 Owner allaqachon eng yuqori rolda.")
        return
    if is_banned(target_id):
        await message.answer("❌ Bu foydalanuvchi ban qilingan. Avval /unban qiling.")
        return

    set_user_role(target_id, "admin")
    await message.answer(f"👑 <code>{target_id}</code> Admin qilindi!", parse_mode="HTML")


@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message, command: CommandObject):
    if not await require_owner(message, "❌ Faqat Owner adminlikni olib tashlashi mumkin!"):
        return

    target_id = parse_id(command.args.split()[0]) if command.args else None
    if target_id is None:
        await message.answer("⚠️ Ishlatish: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bunday foydalanuvchi topilmadi.")
        return
    if target_id == OWNER_ID:
        await message.answer("❌ Ownerni o'zgartirib bo'lmaydi.")
        return

    set_user_role(target_id, "user")
    await message.answer(f"👤 <code>{target_id}</code> dan adminlik olindi.", parse_mode="HTML")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    target_id = parse_id(command.args.split()[0]) if command.args else None
    if target_id is None:
        await message.answer("⚠️ Ishlatish: <code>/ban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    if target_id == OWNER_ID:
        await message.answer("❌ Ownerni ban qilib bo'lmaydi.")
        return
    if target_id == message.from_user.id:
        await message.answer("❌ O'zingizni ban qila olmaysiz.")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bunday foydalanuvchi topilmadi.")
        return
    if get_user_role(target_id) == "admin" and get_user_role(message.from_user.id) != "owner":
        await message.answer("❌ Adminni faqat Owner ban qila oladi.")
        return

    set_banned(target_id, True)
    if get_user_role(target_id) == "admin":
        set_user_role(target_id, "user")
    await message.answer(f"🚫 <code>{target_id}</code> ban qilindi.", parse_mode="HTML")


@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    target_id = parse_id(command.args.split()[0]) if command.args else None
    if target_id is None:
        await message.answer("⚠️ Ishlatish: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bunday foydalanuvchi topilmadi.")
        return

    set_banned(target_id, False)
    await message.answer(f"✅ <code>{target_id}</code> ban dan chiqarildi.", parse_mode="HTML")


# --- SCHEDULE CHANGES / HOLIDAYS (admin) ---
@dp.message(Command("holiday"))
async def cmd_holiday(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    parts = (command.args or "").split(maxsplit=1)
    target = parse_date(parts[0]) if parts else None
    if target is None:
        await message.answer(
            "⚠️ Ishlatish: <code>/holiday &lt;sana&gt; [sabab]</code>\n"
            "Masalan: <code>/holiday 2026-10-01 O'qituvchilar kuni</code>",
            parse_mode="HTML"
        )
        return

    reason = parts[1].strip() if len(parts) > 1 else ""
    cursor.execute(
        "INSERT OR REPLACE INTO overrides (day_date, lesson, kind, note) VALUES (?, 0, 'off', ?)",
        (target.isoformat(), reason),
    )
    conn.commit()
    await message.answer(f"🎉 <code>{target.isoformat()}</code> kuni dars yo'q deb belgilandi.", parse_mode="HTML")


@dp.message(Command("change"))
async def cmd_change(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    usage = (
        "⚠️ Ishlatish: <code>/change &lt;sana&gt; &lt;dars#&gt; &lt;matn&gt;</code>\n"
        "Masalan: <code>/change ertaga 3 Bekor qilindi</code>\n"
        "yoki: <code>/change 2026-10-05 2 Xona 305 ga ko'chdi</code>"
    )
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(usage, parse_mode="HTML")
        return

    target = parse_date(parts[0])
    lesson = parse_id(parts[1])
    if target is None or lesson is None or not 1 <= lesson <= len(BELL_SCHEDULE):
        await message.answer(usage, parse_mode="HTML")
        return

    cursor.execute(
        "INSERT OR REPLACE INTO overrides (day_date, lesson, kind, note) VALUES (?, ?, 'note', ?)",
        (target.isoformat(), lesson, parts[2].strip()),
    )
    conn.commit()
    await message.answer(
        f"⚠️ <code>{target.isoformat()}</code> kuni {lesson}-darsga eslatma qo'shildi.",
        parse_mode="HTML"
    )


@dp.message(Command("changes"))
async def cmd_changes(message: types.Message):
    if not await require_admin(message):
        return

    today = datetime.now(UZB_TZ).date().isoformat()
    cursor.execute(
        "SELECT day_date, lesson, kind, note FROM overrides WHERE day_date >= ? ORDER BY day_date, lesson LIMIT 40",
        (today,),
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("ℹ️ Kelgusi o'zgarishlar yo'q.")
        return

    lines = ["🗓 <b>Kelgusi o'zgarishlar:</b>\n"]
    for day_date, lesson, kind, note in rows:
        if kind == "off":
            lines.append(f"• <code>{day_date}</code> — 🎉 dars yo'q {esc(note)}".rstrip())
        else:
            lines.append(f"• <code>{day_date}</code> — {lesson}-dars: {esc(note)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("clearchange"))
async def cmd_clear_change(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    target = parse_date(command.args.split()[0]) if command.args else None
    if target is None:
        await message.answer("⚠️ Ishlatish: <code>/clearchange &lt;sana&gt;</code>", parse_mode="HTML")
        return

    cursor.execute("DELETE FROM overrides WHERE day_date = ?", (target.isoformat(),))
    removed = cursor.rowcount
    conn.commit()
    await message.answer(f"🧹 <code>{target.isoformat()}</code> uchun {removed} ta o'zgarish o'chirildi.", parse_mode="HTML")


# --- TIMETABLE EDITING (admin) ---
@dp.message(Command("setlesson"))
async def cmd_set_lesson(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    usage = (
        "⚠️ Ishlatish: <code>/setlesson &lt;kun&gt; &lt;dars#&gt; &lt;fan&gt;; &lt;xona&gt;; &lt;o'qituvchilar&gt;</code>\n"
        "Masalan: <code>/setlesson 1 3 Ona tili; 208; Q.Umid</code>\n"
        "Kun: 1-5 yoki du/se/ch/pa/ju"
    )
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(usage, parse_mode="HTML")
        return

    day_idx = DAY_CODE_MAP.get(parts[0].lower())
    pos = parse_id(parts[1])
    if day_idx is None or pos is None or not 1 <= pos <= len(BELL_SCHEDULE):
        await message.answer(usage, parse_mode="HTML")
        return

    fields = [f.strip() for f in parts[2].split(";")]
    subject = fields[0]
    room = fields[1] if len(fields) > 1 and fields[1] else "N/A"
    teachers = fields[2] if len(fields) > 2 and fields[2] else "-"
    if not subject:
        await message.answer(usage, parse_mode="HTML")
        return

    current_count = len(TIMETABLE.get(day_idx, []))
    if pos > current_count + 1:
        await message.answer(
            f"❌ Bu kunda hozir {current_count} ta dars bor. Avval {current_count + 1}-darsni qo'shing."
        )
        return

    cursor.execute(
        "INSERT OR REPLACE INTO timetable (day, pos, subject, room, teachers) VALUES (?, ?, ?, ?, ?)",
        (day_idx, pos, subject, room, teachers),
    )
    conn.commit()
    load_timetable()
    await message.answer(
        f"✅ {esc(DAY_NAMES[day_idx])}, {pos}-dars: <b>{esc(subject)}</b> | {esc(room)} | {esc(teachers)}",
        parse_mode="HTML"
    )


@dp.message(Command("dellesson"))
async def cmd_del_lesson(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    day_idx = DAY_CODE_MAP.get(command.args.split()[0].lower()) if command.args else None
    if day_idx is None:
        await message.answer("⚠️ Ishlatish: <code>/dellesson &lt;kun&gt;</code> (1-5 yoki du/se/ch/pa/ju)", parse_mode="HTML")
        return

    lessons = TIMETABLE.get(day_idx, [])
    if not lessons:
        await message.answer("❌ Bu kunda darslar yo'q.")
        return

    last_pos = len(lessons)
    removed = lessons[-1]["subject"]
    cursor.execute("DELETE FROM timetable WHERE day = ? AND pos = ?", (day_idx, last_pos))
    conn.commit()
    load_timetable()
    await message.answer(
        f"🗑 {esc(DAY_NAMES[day_idx])}, {last_pos}-dars (<b>{esc(removed)}</b>) o'chirildi.",
        parse_mode="HTML"
    )


# --- INBOX: messages people write to the bot are forwarded to the owner ---
@dp.message(Command("inbox"))
async def cmd_inbox(message: types.Message, command: CommandObject):
    if not await require_owner(message, "❌ Faqat Owner foydalana oladi."):
        return

    arg = (command.args or "").strip().lower()
    if arg in ("on", "off"):
        set_setting("inbox", arg)

    enabled = get_setting("inbox", "on") == "on"
    status = "yoqilgan ✅" if enabled else "o'chirilgan ❌"
    await message.answer(
        f"📥 Inbox: <b>{status}</b>\n"
        "Ishlatish: <code>/inbox on</code> yoki <code>/inbox off</code>",
        parse_mode="HTML"
    )


# This handler must stay LAST among the message handlers: it only receives
# messages that no other handler (commands) has already taken.
@dp.message()
async def forward_to_owner(message: types.Message):
    user = message.from_user
    if user is None or message.chat.type != "private":
        return

    register_or_update_user(user.id, user.username, user.first_name)

    if user.id == OWNER_ID or get_setting("inbox", "on") != "on":
        return

    uname = f"@{esc(user.username)}" if user.username else "NoUsername"
    header = (
        f"📨 <b>{esc(user.first_name or 'NoName')}</b> ({uname}) | ID: <code>{user.id}</code>\n"
        f"Javob: <code>/dm {user.id} </code>"
    )
    try:
        await bot.send_message(OWNER_ID, header, parse_mode="HTML")
        # copy_message works for any type: text, photo, sticker, voice, file...
        await bot.copy_message(OWNER_ID, message.chat.id, message.message_id)
    except Exception as e:
        logging.error(f"Could not forward message from {user.id} to owner: {e}")


# --- AUTOMATED SCHEDULER JOBS ---
async def send_morning_alert():
    today = datetime.now(UZB_TZ).date()
    if today.weekday() not in TIMETABLE:
        return

    if get_off_reason(today) is not None:
        msg = "☀️ <b>Xayrli kun!</b>\n\n" + format_date_schedule(today)
    else:
        msg = "☀️ <b>Xayrli kun! Darslar boshlanishiga 1 soat qoldi.</b>\n\n" + format_date_schedule(today)
    await broadcast_message(msg, flag="morning_alert")


async def send_lesson_end_alert(lesson_num: int):
    today = datetime.now(UZB_TZ).date()
    lessons = effective_lessons(today)
    idx = lesson_num - 1

    if idx >= len(lessons):
        return

    current_lesson = lessons[idx]
    bell = BELL_SCHEDULE[idx]

    if idx + 1 < len(lessons):
        next_lesson = lessons[idx + 1]
        next_bell = BELL_SCHEDULE[idx + 1]
        msg = (
            f"🔔 <b>{bell['lesson']}-dars ({esc(current_lesson['subject'])}) tugadi!</b>\n\n"
            f"➡️ <b>Keyingi dars ({next_bell['lesson']}-dars):</b> {esc(next_lesson['subject'])}\n"
            f"⏰ <b>Boshlanishi:</b> {next_bell['start']}\n"
            f"🚪 <b>Xona:</b> {esc(next_lesson['room'])}\n"
            f"👨‍🏫 <b>O'qituvchi:</b> {esc(next_lesson['teachers'])}"
        )
        note = get_notes(today).get(next_bell["lesson"])
        if note:
            msg += f"\n⚠️ <b>{esc(note)}</b>"
    else:
        msg = (
            f"🎉 <b>{bell['lesson']}-dars ({esc(current_lesson['subject'])}) tugadi! "
            f"Bugungi barcha darslar yakunlandi.</b>"
        )

    await broadcast_message(msg, flag="lesson_alerts")


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=UZB_TZ)

    # 1. Morning schedule alert at 08:00 AM (Mon-Fri)
    scheduler.add_job(send_morning_alert, "cron", day_of_week="mon-fri", hour=8, minute=0)

    # 2. Lesson End Alerts (Mon-Fri) matching bell schedule
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=9, minute=45, args=[1])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=10, minute=35, args=[2])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=11, minute=25, args=[3])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=12, minute=15, args=[4])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=13, minute=30, args=[5])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=14, minute=20, args=[6])
    scheduler.add_job(send_lesson_end_alert, "cron", day_of_week="mon-fri", hour=15, minute=10, args=[7])

    return scheduler


async def main():
    logging.basicConfig(level=logging.INFO)
    scheduler = setup_scheduler()
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())