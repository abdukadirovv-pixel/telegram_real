"""
schedule.py  -  the "brain" of the bot (no Telegram handlers here)

  * configuration (token, owner, timezone)
  * database + users
  * class timetables (10-A, 10-B) and teacher timetables (Umarbek)
  * schedule logic: what lesson is now / next / how much time is left
  * ready-made message texts
  * the jobs that the scheduler runs (morning alerts, lesson-end alerts)

command.py  imports this file and holds all commands / buttons.
main.py     imports both and starts the bot.
"""
import os
import html
import math
import asyncio
import logging
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

# ======================================================================
#  CONFIGURATION
# ======================================================================
# Optional: load variables from a local .env file (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# The token is NEVER stored in the code. Set it as an environment variable (BOT_TOKEN).
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Add it as an environment variable (see host settings).")

OWNER_ID = int(os.getenv("OWNER_ID", 8780228920))
UZB_TZ = ZoneInfo("Asia/Tashkent")

# Optional: file_id of the funny sticker shown when there are no lessons left today.
# (Send any sticker to the bot as the owner and it replies with its file_id.)
FUNNY_STICKER_ID = os.getenv("FUNNY_STICKER_ID", "").strip()

# Ensure the data directory exists
os.makedirs("data", exist_ok=True)

# Save SQLite database inside the persistent volume folder
conn = sqlite3.connect("data/subscribers.db", check_same_thread=False)
cursor = conn.cursor()

# ======================================================================
#  DATABASE SETUP
# ======================================================================
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
ensure_column("users", "class_id", "TEXT")
ensure_column("users", "teacher_key", "TEXT")          # set only by the owner (/setteacher)
ensure_column("users", "menu_shown", "INTEGER DEFAULT 0")

# Every class has its OWN timetable and its OWN overrides (nothing is shared or merged)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS class_timetable (
        class_id TEXT,
        day INTEGER,
        pos INTEGER,
        subject TEXT,
        room TEXT,
        teachers TEXT,
        PRIMARY KEY (class_id, day, pos)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS class_overrides (
        class_id TEXT,
        day_date TEXT,
        lesson INTEGER,
        kind TEXT,
        note TEXT,
        PRIMARY KEY (class_id, day_date, lesson)
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
""")
# Teacher timetables (one row = one lesson slot of one teacher; slots may have gaps)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS teacher_timetable (
        teacher_key TEXT,
        day INTEGER,
        lesson INTEGER,
        class_label TEXT,
        class_id TEXT,
        subject TEXT,
        PRIMARY KEY (teacher_key, day, lesson)
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


# ======================================================================
#  SETTINGS (key / value)
# ======================================================================
def get_setting(key: str, default: str = "") -> str:
    cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# ======================================================================
#  DAYS
# ======================================================================
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

# Teachers also work on Saturday
TEACHER_DAY_NAMES = {**DAY_NAMES, 5: "Shanba (Saturday)"}
TEACHER_DAY_CODE_MAP = {**DAY_CODE_MAP, "6": 5, "sh": 5, "shanba": 5, "sat": 5, "saturday": 5}

# ======================================================================
#  CLASSES  (add a new class = add one block here, nothing else changes)
# ======================================================================

# --- 10-B aniq (the original schedule, unchanged) ---
BELLS_10B = [
    {"lesson": 1, "start": "09:00", "end": "09:45"},
    {"lesson": 2, "start": "09:50", "end": "10:35"},
    {"lesson": 3, "start": "10:40", "end": "11:25"},
    {"lesson": 4, "start": "11:30", "end": "12:15"},
    {"lesson": 5, "start": "12:45", "end": "13:30"},
    {"lesson": 6, "start": "13:35", "end": "14:20"},
    {"lesson": 7, "start": "14:25", "end": "15:10"},
]

DEFAULT_TIMETABLE_10B = {
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

# --- 10-A aniq ---
# Same bell times as 10-B for lessons 1-7. Monday has an 8th lesson, so it gets one
# extra slot right after the 7th (5-minute break, 45-minute lesson, like the rest).
BELLS_10A = [dict(b) for b in BELLS_10B] + [
    {"lesson": 8, "start": "15:15", "end": "16:00"},
]

DEFAULT_TIMETABLE_10A = {
    0: [  # 1 - Dushanba
        {"subject": "Kelajak soati", "room": "207", "teachers": "Ollanazar"},
        {"subject": "Ingliz tili", "room": "222", "teachers": "Inobat, Muzaffar"},
        {"subject": "Ona tili", "room": "210", "teachers": "Barno"},
        {"subject": "Algebra", "room": "215", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Geometriya", "room": "214", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "CHQBT", "room": "N/A", "teachers": "To'lqin"},
        {"subject": "Jismoniy tarbiya", "room": "Sport zal", "teachers": "Ulug'bek"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Bekzod"},
    ],
    1: [  # 2 - Seshanba
        {"subject": "Rus tili", "room": "128", "teachers": "Gulzoda, Shoxista"},
        {"subject": "Ingliz tili", "room": "222", "teachers": "Inobat, Muzaffar"},
        {"subject": "Tarbiya", "room": "132", "teachers": "Azada"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Bekzod"},
        {"subject": "Algebra", "room": "215", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Informatika", "room": "Informatika", "teachers": "Xursand, Feruza"},
    ],
    2: [  # 3 - Chorshanba
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Bekzod"},
        {"subject": "Ona tili", "room": "210", "teachers": "Barno"},
        {"subject": "O'zbek tarix", "room": "112", "teachers": "Shoira"},
        {"subject": "Algebra", "room": "215", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Geometriya", "room": "214", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Adabiyot", "room": "210", "teachers": "Barno"},
    ],
    3: [  # 4 - Payshanba
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Bekzod"},
        {"subject": "Ingliz tili", "room": "222", "teachers": "Inobat, Muzaffar"},
        {"subject": "Adabiyot", "room": "210", "teachers": "Barno"},
        {"subject": "Informatika", "room": "Informatika", "teachers": "Xursand, Feruza"},
        {"subject": "Jahon tarix", "room": "112", "teachers": "Shoira"},
        {"subject": "CHQBT", "room": "129", "teachers": "To'lqin"},
    ],
    4: [  # 5 - Juma
        {"subject": "Ingliz tili", "room": "222", "teachers": "Inobat, Muzaffar"},
        {"subject": "O'zbek tarix", "room": "112", "teachers": "Shoira"},
        {"subject": "Algebra", "room": "215", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Geometriya", "room": "214", "teachers": "Gulnoza, Ollanazar"},
        {"subject": "Rus tili", "room": "128", "teachers": "Gulzoda, Shoxista"},
        {"subject": "Fizika", "room": "202", "teachers": "O'g'lijon, Bekzod"},
    ],
}

# "morning" = time of the morning alert
CLASSES = {
    "10a": {
        "name": "10-A aniq",
        "short": "10-A",
        "morning": "08:00",
        "bells": BELLS_10A,
        "default": DEFAULT_TIMETABLE_10A,
    },
    "10b": {
        "name": "10-B aniq",
        "short": "10-B",
        "morning": "08:00",
        "bells": BELLS_10B,
        "default": DEFAULT_TIMETABLE_10B,
    },
}

# ======================================================================
#  TEACHERS  (add a new teacher = add one block here)
#  Teacher lessons use the SAME bell times as the classes.
# ======================================================================
TEACHER_BELLS = [dict(b) for b in BELLS_10A]  # 8 slots


def _tl(lesson: int, klass: str, class_id: str | None = None, subject: str = "Informatika") -> dict:
    """One teacher lesson. class_id is set only for classes that exist in the bot (10a / 10b)
    so a holiday of that class also cancels the lesson for the teacher."""
    return {"lesson": lesson, "class": klass, "class_id": class_id, "subject": subject}


# Madrimov Umarbek (ICT teacher)
TIMETABLE_UMARBEK = {
    0: [  # Dushanba
        _tl(2, "10-B aniq", "10b", "Kelajak soati"),
        _tl(3, "11-A aniq"),
        _tl(4, "10-B aniq", "10b"),
        _tl(5, "9-A aniq"),
        _tl(6, "8-B aniq"),
        _tl(7, "7-D tabiiy"),
    ],
    1: [  # Seshanba
        _tl(2, "11-A aniq"),
        _tl(3, "10-D umumta'lim"),
        _tl(5, "5-B"),
        _tl(7, "8-D tabiiy"),
    ],
    2: [  # Chorshanba
        _tl(1, "11-B aniq"),
        _tl(4, "9-A aniq"),
        _tl(7, "10-G tabiiy"),
    ],
    3: [  # Payshanba
        _tl(6, "8-A aniq"),
    ],
    4: [  # Juma
        _tl(1, "7-B aniq"),
        _tl(2, "11-B aniq"),
        _tl(3, "9-A aniq"),
        _tl(4, "5-A"),
        _tl(5, "6-B aniq"),
        _tl(6, "10-B aniq", "10b"),
    ],
    5: [  # Shanba
        _tl(2, "10-D umumta'lim"),
    ],
}

TEACHERS = {
    "umarbek": {
        "name": "Madrimov Umarbek",
        "short": "Umarbek",
        "subject": "Informatika",
        "morning": "08:00",
        "default": TIMETABLE_UMARBEK,   # copied into the database on the first run only
        "timetable": {},                # live timetable, loaded from the database
    },
}

# ======================================================================
#  CLASS TIMETABLES: migration, seeding, loading
# ======================================================================
# Live timetables used by the whole bot: TIMETABLES[class_id][weekday] -> list of lessons
TIMETABLES: dict[str, dict[int, list[dict]]] = {}


def table_exists(name: str) -> bool:
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return cursor.fetchone() is not None


def migrate_to_multiclass():
    """One-time: the old single-class data (10-B) becomes the data of class 10b,
    and all existing users stay on 10-B so nothing changes for them."""
    if get_setting("multiclass_migrated") == "1":
        return
    if table_exists("timetable"):
        cursor.execute(
            "INSERT OR IGNORE INTO class_timetable (class_id, day, pos, subject, room, teachers) "
            "SELECT '10b', day, pos, subject, room, teachers FROM timetable"
        )
    if table_exists("overrides"):
        cursor.execute(
            "INSERT OR IGNORE INTO class_overrides (class_id, day_date, lesson, kind, note) "
            "SELECT '10b', day_date, lesson, kind, note FROM overrides"
        )
    cursor.execute("UPDATE users SET class_id = '10b' WHERE class_id IS NULL")
    conn.commit()
    set_setting("multiclass_migrated", "1")


def seed_class_if_needed(class_id: str):
    """Copies the default timetable of a class into the database (only once per class)."""
    if get_setting(f"seeded_{class_id}") == "1":
        return
    cursor.execute("SELECT COUNT(*) FROM class_timetable WHERE class_id = ?", (class_id,))
    if cursor.fetchone()[0] == 0:
        for day, lessons in CLASSES[class_id]["default"].items():
            for pos, item in enumerate(lessons, start=1):
                cursor.execute(
                    "INSERT INTO class_timetable (class_id, day, pos, subject, room, teachers) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (class_id, day, pos, item["subject"], item["room"], item["teachers"]),
                )
        conn.commit()
    set_setting(f"seeded_{class_id}", "1")


def load_timetable():
    """Reloads TIMETABLES from the database (only days that have lessons)."""
    fresh: dict[str, dict[int, list[dict]]] = {cid: {} for cid in CLASSES}
    cursor.execute(
        "SELECT class_id, day, pos, subject, room, teachers FROM class_timetable ORDER BY class_id, day, pos"
    )
    for class_id, day, _pos, subject, room, teachers in cursor.fetchall():
        if class_id in fresh:
            fresh[class_id].setdefault(day, []).append({"subject": subject, "room": room, "teachers": teachers})
    TIMETABLES.clear()
    TIMETABLES.update(fresh)


migrate_to_multiclass()
for _cid in CLASSES:
    seed_class_if_needed(_cid)
load_timetable()


def seed_teacher_if_needed(teacher_key: str):
    """Copies the default lessons of a teacher into the database (only once per teacher)."""
    if get_setting(f"seeded_teacher_{teacher_key}") == "1":
        return
    cursor.execute("SELECT COUNT(*) FROM teacher_timetable WHERE teacher_key = ?", (teacher_key,))
    if cursor.fetchone()[0] == 0:
        for day, lessons in TEACHERS[teacher_key]["default"].items():
            for item in lessons:
                cursor.execute(
                    "INSERT INTO teacher_timetable (teacher_key, day, lesson, class_label, class_id, subject) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (teacher_key, day, item["lesson"], item["class"], item["class_id"], item["subject"]),
                )
        conn.commit()
    set_setting(f"seeded_teacher_{teacher_key}", "1")


def load_teacher_timetables():
    """Reloads the lessons of every teacher from the database."""
    fresh: dict[str, dict[int, list[dict]]] = {key: {} for key in TEACHERS}
    cursor.execute(
        "SELECT teacher_key, day, lesson, class_label, class_id, subject FROM teacher_timetable "
        "ORDER BY teacher_key, day, lesson"
    )
    for key, day, lesson, class_label, class_id, subject in cursor.fetchall():
        if key in fresh:
            fresh[key].setdefault(day, []).append(
                {"lesson": lesson, "class": class_label, "class_id": class_id, "subject": subject}
            )
    for key in TEACHERS:
        TEACHERS[key]["timetable"] = fresh[key]


for _tkey in TEACHERS:
    seed_teacher_if_needed(_tkey)
load_teacher_timetables()


def class_bells(class_id: str) -> list[dict]:
    return CLASSES[class_id]["bells"]


def class_timetable(class_id: str) -> dict[int, list[dict]]:
    return TIMETABLES.get(class_id, {})


def class_name(class_id: str | None) -> str:
    return CLASSES[class_id]["name"] if class_id in CLASSES else "—"


def class_token(raw: str | None) -> str | None:
    """'10a', '10-A', '10B' -> '10a' / '10b' (None if it is not a class code)."""
    if not raw:
        return None
    token = raw.lower().replace("-", "")
    return token if token in CLASSES else None


def class_id_from_label(label: str) -> str | None:
    """'10-B aniq' / '10b' -> '10b' when it is a class of the bot (so that class holidays also
    cancel the teacher's lesson), otherwise None (e.g. '9-A aniq')."""
    words = label.split()
    return class_token(words[0]) if words else None


def teacher_token(raw: str | None) -> str | None:
    """'Umarbek', 'umarbek', 'Madrimov' -> 'umarbek' (None if unknown)."""
    if not raw:
        return None
    token = raw.lower()
    for key, cfg in TEACHERS.items():
        names = [key, cfg["short"].lower()] + cfg["name"].lower().split()
        if token in names:
            return key
    return None


# ======================================================================
#  USERS
# ======================================================================
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
    cursor.execute(
        "SELECT user_id, username, first_name, role, is_active, banned, class_id, teacher_key FROM users"
    )
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


def get_user_class(user_id: int) -> str | None:
    cursor.execute("SELECT class_id FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    class_id = row[0] if row else None
    return class_id if class_id in CLASSES else None


def set_user_class(user_id: int, class_id: str):
    cursor.execute("UPDATE users SET class_id = ? WHERE user_id = ?", (class_id, int(user_id)))
    conn.commit()


# --- teachers (assigned only by the owner) ---
def get_user_teacher(user_id: int) -> str | None:
    cursor.execute("SELECT teacher_key FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    key = row[0] if row else None
    return key if key in TEACHERS else None


def set_user_teacher(user_id: int, teacher_key: str):
    """Makes the user a teacher: no class, new (teacher) menu."""
    cursor.execute(
        "UPDATE users SET teacher_key = ?, class_id = NULL, menu_shown = 0 WHERE user_id = ?",
        (teacher_key, int(user_id)),
    )
    conn.commit()


def clear_user_teacher(user_id: int):
    cursor.execute("UPDATE users SET teacher_key = NULL, menu_shown = 0 WHERE user_id = ?", (int(user_id),))
    conn.commit()


def get_teacher_users():
    cursor.execute(
        "SELECT user_id, username, first_name, teacher_key FROM users WHERE teacher_key IS NOT NULL"
    )
    return cursor.fetchall()


def menu_shown(user_id: int) -> bool:
    cursor.execute("SELECT menu_shown FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    return bool(row and row[0])


def mark_menu_shown(user_id: int, value: bool = True):
    cursor.execute("UPDATE users SET menu_shown = ? WHERE user_id = ?", (1 if value else 0, int(user_id)))
    conn.commit()


# --- recipients ---
def get_recipients(flag: str | None = None, class_id: str | None = None) -> list[int]:
    """Active, non-banned users. Optionally only one class / only those with a setting switched on."""
    query = "SELECT user_id FROM users WHERE is_active = 1 AND banned = 0"
    params: list = []
    if flag in SETTING_LABELS:
        query += f" AND {flag} = 1"
    if class_id:
        query += " AND class_id = ?"
        params.append(class_id)
    cursor.execute(query, params)
    return [row[0] for row in cursor.fetchall()]


def get_teacher_recipients(teacher_key: str, flag: str | None = None) -> list[int]:
    query = "SELECT user_id FROM users WHERE is_active = 1 AND banned = 0 AND teacher_key = ?"
    if flag in SETTING_LABELS:
        query += f" AND {flag} = 1"
    cursor.execute(query, (teacher_key,))
    return [row[0] for row in cursor.fetchall()]


def get_audience_recipients(audience: str, flag: str | None = "announcements") -> list[int]:
    """Who gets a broadcast.
    audience = 'all' (everybody) | 'students' (everybody except teachers) | 'teachers' | a class id ('10a')."""
    query = "SELECT user_id FROM users WHERE is_active = 1 AND banned = 0"
    params: list = []
    if audience == "students":
        query += " AND teacher_key IS NULL"
    elif audience == "teachers":
        query += " AND teacher_key IS NOT NULL"
    elif audience in CLASSES:
        query += " AND teacher_key IS NULL AND class_id = ?"
        params.append(audience)
    elif audience != "all":
        return []
    if flag in SETTING_LABELS:
        query += f" AND {flag} = 1"
    cursor.execute(query, params)
    return [row[0] for row in cursor.fetchall()]


def users_scope_where(scope: str, include_teachers: bool):
    """SQL filter of a /users window. scope = a class id | 'none' | 'teachers' | 'all'.
    Teachers are only visible when include_teachers is True (= the viewer is the owner)."""
    if scope == "teachers":
        return ("teacher_key IS NOT NULL", []) if include_teachers else None
    if scope == "none":
        return "class_id IS NULL AND teacher_key IS NULL", []
    if scope == "all":
        return ("1 = 1" if include_teachers else "teacher_key IS NULL"), []
    if scope in CLASSES:
        return "class_id = ? AND teacher_key IS NULL", [scope]
    return None


def get_users_in_scope(scope: str, include_teachers: bool):
    spec = users_scope_where(scope, include_teachers)
    if spec is None:
        return []
    where, params = spec
    cursor.execute(
        "SELECT user_id, username, first_name, role, is_active, banned, class_id, teacher_key "
        f"FROM users WHERE {where} ORDER BY first_name COLLATE NOCASE",
        params,
    )
    return cursor.fetchall()


def count_users_in_scope(scope: str, include_teachers: bool) -> int:
    spec = users_scope_where(scope, include_teachers)
    if spec is None:
        return 0
    where, params = spec
    cursor.execute(f"SELECT COUNT(*) FROM users WHERE {where}", params)
    return cursor.fetchone()[0]


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


# ======================================================================
#  DATE / OVERRIDE HELPERS
# ======================================================================
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


def teacher_week_start(today: date) -> date:
    """Monday of this week (on Sunday: Monday of the coming week). Saturday still counts."""
    wd = today.weekday()
    return today - timedelta(days=wd) if wd < 6 else today + timedelta(days=1)


def get_off_reason(class_id: str, d: date) -> str | None:
    """Returns the reason if the day is marked 'no school' for this class, otherwise None."""
    cursor.execute(
        "SELECT note FROM class_overrides WHERE class_id = ? AND day_date = ? AND lesson = 0 AND kind = 'off'",
        (class_id, d.isoformat()),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_notes(class_id: str, d: date) -> dict[int, str]:
    cursor.execute(
        "SELECT lesson, note FROM class_overrides WHERE class_id = ? AND day_date = ? AND kind = 'note'",
        (class_id, d.isoformat()),
    )
    return {lesson: note for lesson, note in cursor.fetchall()}


def school_off(d: date) -> bool:
    """True when EVERY class of the bot has a holiday on that date (= school-wide holiday)."""
    return all(get_off_reason(cid, d) is not None for cid in CLASSES)


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
    return TEACHER_DAY_NAMES.get(d.weekday(), d.isoformat()).split(" ")[0]


# ======================================================================
#  CLASS SCHEDULE: lessons, texts
# ======================================================================
def effective_lessons(class_id: str, d: date) -> list[dict]:
    """Lessons that really take place for a class on a date (none on weekends/holidays)."""
    timetable = class_timetable(class_id)
    wd = d.weekday()
    if wd not in timetable or get_off_reason(class_id, d) is not None:
        return []
    return timetable[wd][:len(class_bells(class_id))]


def format_date_schedule(class_id: str, d: date) -> str:
    timetable = class_timetable(class_id)
    bells = class_bells(class_id)
    wd = d.weekday()
    label = d.strftime("%d.%m.%Y")
    cname = esc(class_name(class_id))

    if wd not in timetable:
        return f"🎉 <b>Dam olish kuni! Darslar yo'q.</b> ({label})"

    day_title = DAY_NAMES.get(wd, "Dars Jadvali")
    off = get_off_reason(class_id, d)
    if off is not None:
        text = f"🎉 <b>{esc(day_title)} ({label}) — dars yo'q.</b> · {cname}"
        if off:
            text += f"\n{esc(off)}"
        return text

    lessons = timetable[wd][:len(bells)]
    notes = get_notes(class_id, d)
    text = f"📅 <b>Dars Jadvali: {esc(day_title)}</b> ({label}) · {cname}\n\n"

    for idx, item in enumerate(lessons):
        bell = bells[idx]
        room_str = f"| 🚪 Xona: {esc(item['room'])}" if item.get("room") else ""
        text += f"<b>{bell['lesson']}-dars ({bell['start']} - {bell['end']}):</b> {esc(item['subject'])}\n"
        text += f"└ 👨‍🏫 <i>O'qituvchi:</i> {esc(item['teachers'])} {room_str}\n"
        if bell["lesson"] in notes:
            text += f"└ ⚠️ <b>{esc(notes[bell['lesson']])}</b>\n"
        text += "\n"

    return text


def format_week(class_id: str, start: date) -> str:
    timetable = class_timetable(class_id)
    bells = class_bells(class_id)
    text = f"🗓 <b>Haftalik jadval</b> · {esc(class_name(class_id))}\n\n"
    for wd in range(5):
        d = start + timedelta(days=wd)
        text += f"<b>{esc(DAY_NAMES[wd])}</b> ({d.strftime('%d.%m')})"
        off = get_off_reason(class_id, d)
        if off is not None:
            text += " — 🎉 dars yo'q\n\n"
            continue
        text += "\n"
        notes = get_notes(class_id, d)
        for idx, item in enumerate(timetable.get(wd, [])[:len(bells)]):
            mark = " ⚠️" if (idx + 1) in notes else ""
            text += f"{idx + 1}. {esc(item['subject'])} ({esc(item['room'])}){mark}\n"
        text += "\n"
    return text


# --- now / next / time left (students) ---
NO_LESSONS_LEFT_TEXT = "🏁 <b>Bugun uchun darslar qolmadi.</b>"
JOKE_TEXT = "Shu malli ham adam o'qimi eee 😄"


def find_current(class_id: str, now: datetime):
    bells = class_bells(class_id)
    lessons = effective_lessons(class_id, now.date())
    for idx, item in enumerate(lessons):
        bell = bells[idx]
        start = lesson_dt(now.date(), bell["start"])
        end = lesson_dt(now.date(), bell["end"])
        if start <= now < end:
            return idx, item, end
    return None


def find_next(class_id: str, now: datetime, days: int = 8):
    """Next lesson that starts after 'now'. days=1 means: today only."""
    bells = class_bells(class_id)
    for offset in range(0, days):
        d = now.date() + timedelta(days=offset)
        for idx, item in enumerate(effective_lessons(class_id, d)):
            start = lesson_dt(d, bells[idx]["start"])
            if start > now:
                return d, idx, item, start
    return None


def _next_block(class_id: str, nxt, now: datetime) -> str:
    d, idx, item, start = nxt
    bell = class_bells(class_id)[idx]
    note = get_notes(class_id, d).get(bell["lesson"])

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


def next_lesson_text(class_id: str, now: datetime) -> str:
    nxt = find_next(class_id, now)
    if not nxt:
        return "ℹ️ Yaqin kunlarda dars topilmadi."
    return _next_block(class_id, nxt, now)


def class_now_text(class_id: str, now: datetime) -> tuple[str, bool]:
    """Text for /now. The bool is True when no lessons are left today (the bot then also
    sends the funny sticker)."""
    bells = class_bells(class_id)
    lessons = effective_lessons(class_id, now.date())
    if not lessons:
        return "🎉 <b>Bugun dars yo'q.</b>\nKeyingi dars: /next", False

    current = find_current(class_id, now)
    upcoming = find_next(class_id, now, days=1)  # today only

    if current:
        idx, item, end = current
        bell = bells[idx]
        left = math.ceil((end - now).total_seconds() / 60)
        text = (
            f"📍 <b>Hozir:</b> {bell['lesson']}-dars — {esc(item['subject'])}\n"
            f"🚪 Xona: {esc(item['room'])}\n"
            f"👨‍🏫 O'qituvchi: {esc(item['teachers'])}\n"
            f"⏳ Tugashiga {fmt_minutes(left)} qoldi"
        )
        if upcoming:
            text += "\n\n" + _next_block(class_id, upcoming, now)
        else:
            text += "\n\n🎉 Bu bugungi oxirgi dars!"
        return text, False

    if upcoming:
        first_start = lesson_dt(now.date(), bells[0]["start"])
        head = "⏰ <b>Darslar hali boshlanmagan.</b>" if now < first_start else "☕ <b>Hozir tanaffus.</b>"
        return head + "\n\n" + _next_block(class_id, upcoming, now), False

    return NO_LESSONS_LEFT_TEXT + "\n\n" + JOKE_TEXT, True


def class_time_left_text(class_id: str, now: datetime) -> str:
    """Text for the 'how much time left' button."""
    bells = class_bells(class_id)
    lessons = effective_lessons(class_id, now.date())
    if not lessons:
        return "🎉 <b>Bugun dars yo'q.</b>"

    current = find_current(class_id, now)
    if current:
        idx, item, end = current
        bell = bells[idx]
        left = math.ceil((end - now).total_seconds() / 60)
        return (
            f"⏳ <b>{bell['lesson']}-dars ({esc(item['subject'])})</b> tugashiga "
            f"<b>{fmt_minutes(left)}</b> qoldi (tugash vaqti: {bell['end']})."
        )

    upcoming = find_next(class_id, now, days=1)
    if upcoming:
        d, idx, item, start = upcoming
        bell = bells[idx]
        mins = math.ceil((start - now).total_seconds() / 60)
        return (
            f"☕ Hozir dars yo'q. <b>{bell['lesson']}-dars ({esc(item['subject'])})</b> "
            f"boshlanishiga <b>{fmt_minutes(mins)}</b> qoldi ({bell['start']})."
        )

    return NO_LESSONS_LEFT_TEXT


# ======================================================================
#  TEACHER SCHEDULE
# ======================================================================
def teacher_bell(lesson_num: int) -> dict:
    return TEACHER_BELLS[lesson_num - 1]


def teacher_lessons(teacher_key: str, d: date) -> list[dict]:
    """Lessons the teacher really has on a date. Lessons are cancelled when that class
    has a holiday, and everything is cancelled on a school-wide holiday."""
    lessons = TEACHERS[teacher_key]["timetable"].get(d.weekday(), [])
    if not lessons or school_off(d):
        return []
    result = []
    for item in lessons:
        class_id = item.get("class_id")
        if class_id and get_off_reason(class_id, d) is not None:
            continue
        result.append(item)
    return result


def _teacher_line(teacher_key: str, d: date, item: dict) -> str:
    bell = teacher_bell(item["lesson"])
    line = f"<b>{item['lesson']}-dars ({bell['start']} - {bell['end']}):</b> {esc(item['class'])}"
    if item["subject"] != TEACHERS[teacher_key]["subject"]:
        line += f" — {esc(item['subject'])}"
    line += "\n"
    if item.get("class_id"):
        note = get_notes(item["class_id"], d).get(item["lesson"])
        if note:
            line += f"└ ⚠️ <b>{esc(note)}</b>\n"
    return line


def format_teacher_day(teacher_key: str, d: date) -> str:
    label = d.strftime("%d.%m.%Y")
    day_title = TEACHER_DAY_NAMES.get(d.weekday())
    if day_title is None:
        return f"🎉 <b>Dam olish kuni!</b> ({label}) — darslar yo'q."

    lessons = teacher_lessons(teacher_key, d)
    if not lessons:
        return f"🎉 <b>{esc(day_title)} ({label}):</b> sizda dars yo'q — bekorsiz!"

    text = f"📅 <b>Darslaringiz: {esc(day_title)}</b> ({label})\n\n"
    for item in lessons:
        text += _teacher_line(teacher_key, d, item) + "\n"
    return text


def format_teacher_week(teacher_key: str, start: date) -> str:
    text = "🗓 <b>Haftalik darslaringiz</b>\n\n"
    for wd in range(6):
        d = start + timedelta(days=wd)
        text += f"<b>{esc(TEACHER_DAY_NAMES[wd])}</b> ({d.strftime('%d.%m')})\n"
        lessons = teacher_lessons(teacher_key, d)
        if not lessons:
            text += "—\n\n"
            continue
        for item in lessons:
            text += f"{item['lesson']}. {esc(item['class'])}\n"
        text += "\n"
    return text


def format_teacher_timetable(teacher_key: str) -> str:
    """The saved weekly timetable of a teacher, exactly as stored (no holidays applied)."""
    cfg = TEACHERS[teacher_key]
    text = f"🗓 <b>{esc(cfg['name'])}</b> — dars jadvali\n\n"
    for wd in range(6):
        text += f"<b>{esc(TEACHER_DAY_NAMES[wd])}</b>\n"
        lessons = cfg["timetable"].get(wd, [])
        if not lessons:
            text += "—\n\n"
            continue
        for item in lessons:
            bell = teacher_bell(item["lesson"])
            line = f"{item['lesson']}. ({bell['start']}-{bell['end']}) {esc(item['class'])}"
            if item["subject"] != cfg["subject"]:
                line += f" — {esc(item['subject'])}"
            text += line + "\n"
        text += "\n"
    return text


def find_teacher_current(teacher_key: str, now: datetime):
    for item in teacher_lessons(teacher_key, now.date()):
        bell = teacher_bell(item["lesson"])
        start = lesson_dt(now.date(), bell["start"])
        end = lesson_dt(now.date(), bell["end"])
        if start <= now < end:
            return item, end
    return None


def find_teacher_next(teacher_key: str, now: datetime, days: int = 8):
    for offset in range(0, days):
        d = now.date() + timedelta(days=offset)
        for item in teacher_lessons(teacher_key, d):
            start = lesson_dt(d, teacher_bell(item["lesson"])["start"])
            if start > now:
                return d, item, start
    return None


def _teacher_next_block(teacher_key: str, nxt, now: datetime) -> str:
    d, item, start = nxt
    bell = teacher_bell(item["lesson"])
    text = (
        f"➡️ <b>Keyingi dars:</b> {item['lesson']}-dars — {esc(item['class'])}\n"
        f"⏰ {day_label(d, now.date())}, {bell['start']}"
    )
    if d == now.date():
        mins = math.ceil((start - now).total_seconds() / 60)
        text += f" ({fmt_minutes(mins)}dan keyin)"
    if item["subject"] != TEACHERS[teacher_key]["subject"]:
        text += f"\n📚 {esc(item['subject'])}"
    if item.get("class_id"):
        note = get_notes(item["class_id"], d).get(item["lesson"])
        if note:
            text += f"\n⚠️ <b>{esc(note)}</b>"
    return text


def teacher_next_text(teacher_key: str, now: datetime) -> str:
    nxt = find_teacher_next(teacher_key, now)
    if not nxt:
        return "ℹ️ Yaqin kunlarda darsingiz yo'q."
    return _teacher_next_block(teacher_key, nxt, now)


def teacher_now_text(teacher_key: str, now: datetime) -> str:
    """Current lesson of the teacher: the class if he has a lesson, otherwise
    'sizda hozir dars yo'q - bekorsiz' and the time of the next lesson under it."""
    current = find_teacher_current(teacher_key, now)
    if current:
        item, end = current
        bell = teacher_bell(item["lesson"])
        left = math.ceil((end - now).total_seconds() / 60)
        text = (
            "📍 <b>Hozir sizda dars bor!</b>\n"
            f"🏫 Sinf: <b>{esc(item['class'])}</b>\n"
            f"🕘 {item['lesson']}-dars ({bell['start']} - {bell['end']})\n"
            f"⏳ Tugashiga {fmt_minutes(left)} qoldi"
        )
        if item["subject"] != TEACHERS[teacher_key]["subject"]:
            text += f"\n📚 {esc(item['subject'])}"
        if item.get("class_id"):
            note = get_notes(item["class_id"], now.date()).get(item["lesson"])
            if note:
                text += f"\n⚠️ <b>{esc(note)}</b>"
        upcoming = find_teacher_next(teacher_key, now, days=1)  # today only
        if upcoming:
            text += "\n\n" + _teacher_next_block(teacher_key, upcoming, now)
        else:
            text += "\n\n🎉 Bu bugungi oxirgi darsingiz!"
        return text

    return "😎 <b>Sizda hozir dars yo'q - bekorsiz</b>\n\n" + teacher_next_text(teacher_key, now)


# ======================================================================
#  SAFE SENDING (handles blocked users and rate limits)
# ======================================================================
async def safe_send(bot: Bot, user_id: int, text: str, parse_mode: str = "HTML", reply_markup=None) -> str:
    """Returns 'ok', 'blocked' or 'failed'. Users who blocked the bot are marked inactive."""
    for _ in range(2):
        try:
            await bot.send_message(user_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
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


async def broadcast_message(bot: Bot, text: str, flag: str | None = None, class_id: str | None = None) -> dict:
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for u_id in get_recipients(flag, class_id):
        status = await safe_send(bot, u_id, text)
        result[status] += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    return result


async def broadcast_to(bot: Bot, text: str, audience: str, flag: str | None = "announcements") -> dict:
    """Admin broadcasts: audience = 'all' | 'students' | 'teachers' | a class id."""
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for u_id in get_audience_recipients(audience, flag):
        status = await safe_send(bot, u_id, text)
        result[status] += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    return result


# ======================================================================
#  SCHEDULER JOBS  (main.py registers them; one set per class / teacher)
# ======================================================================
async def send_morning_alert(bot: Bot, class_id: str):
    today = datetime.now(UZB_TZ).date()
    if today.weekday() not in class_timetable(class_id):
        return

    if get_off_reason(class_id, today) is not None:
        msg = "☀️ <b>Xayrli kun!</b>\n\n" + format_date_schedule(class_id, today)
    else:
        msg = (
            "☀️ <b>Xayrli kun! Darslar boshlanishiga 1 soat qoldi.</b>\n\n"
            + format_date_schedule(class_id, today)
        )
    await broadcast_message(bot, msg, flag="morning_alert", class_id=class_id)


async def send_lesson_end_alert(bot: Bot, class_id: str, lesson_num: int):
    today = datetime.now(UZB_TZ).date()
    bells = class_bells(class_id)
    lessons = effective_lessons(class_id, today)
    idx = lesson_num - 1

    if idx >= len(lessons):
        return

    current_lesson = lessons[idx]
    bell = bells[idx]

    if idx + 1 < len(lessons):
        next_lesson = lessons[idx + 1]
        next_bell = bells[idx + 1]
        msg = (
            f"🔔 <b>{bell['lesson']}-dars ({esc(current_lesson['subject'])}) tugadi!</b>\n\n"
            f"➡️ <b>Keyingi dars ({next_bell['lesson']}-dars):</b> {esc(next_lesson['subject'])}\n"
            f"⏰ <b>Boshlanishi:</b> {next_bell['start']}\n"
            f"🚪 <b>Xona:</b> {esc(next_lesson['room'])}\n"
            f"👨‍🏫 <b>O'qituvchi:</b> {esc(next_lesson['teachers'])}"
        )
        note = get_notes(class_id, today).get(next_bell["lesson"])
        if note:
            msg += f"\n⚠️ <b>{esc(note)}</b>"
    else:
        msg = (
            f"🎉 <b>{bell['lesson']}-dars ({esc(current_lesson['subject'])}) tugadi! "
            f"Bugungi barcha darslar yakunlandi.</b>"
        )

    await broadcast_message(bot, msg, flag="lesson_alerts", class_id=class_id)


async def send_teacher_morning(bot: Bot, teacher_key: str):
    """08:00 warning for a teacher: which classes he teaches today (or that he is free)."""
    now = datetime.now(UZB_TZ)
    cfg = TEACHERS[teacher_key]
    text = f"☀️ <b>Xayrli tong, {esc(cfg['short'])} ustoz!</b>\n\n" + format_teacher_day(teacher_key, now.date())
    if not teacher_lessons(teacher_key, now.date()):
        text += "\n\n" + teacher_next_text(teacher_key, now)

    for user_id in get_teacher_recipients(teacher_key, "morning_alert"):
        await safe_send(bot, user_id, text)
        await asyncio.sleep(0.05)