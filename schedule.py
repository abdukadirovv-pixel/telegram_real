"""
schedule.py  -  the "brain" of the bot (no Telegram handlers here)

  * configuration (token, owner, timezone)
  * database + users (Supabase PostgreSQL)
  * class timetables (10-A, 10-B) and teacher timetables (Umarbek)
  * schedule logic: what lesson is now / next / how much time is left
  * ready-made message texts
  * the jobs that the scheduler runs (morning alerts, lesson-end alerts)

command.py  imports this file and holds all commands / buttons.
main.py     imports both and starts the bot.
"""
import os
import re
import html
import math
import asyncio
import logging
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import psycopg2
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

# Supabase PostgreSQL Connection URL (reads DATABASE_URL from env, falls back to your link)
DATABASE_URL = os.getenv("DATABASE_URL")


# ======================================================================
#  SUPABASE POSTGRESQL ADAPTER (keeps 100% compatibility with command.py)
# ======================================================================
def _translate_sql(query: str) -> str:
    """Translates SQLite syntax used in schedule.py and command.py into PostgreSQL syntax."""
    q = query.strip()

    # Replace SQLite '?' placeholders with PostgreSQL '%s'
    q = q.replace("?", "%s")

    # Replace SQLite 'COLLATE NOCASE' with PostgreSQL-compatible LOWER()
    q = re.sub(r"(\w+)\s+COLLATE\s+NOCASE", r"LOWER(\1)", q, flags=re.IGNORECASE)

    # Translate INSERT OR IGNORE INTO -> INSERT INTO ... ON CONFLICT DO NOTHING
    if re.match(r"^INSERT\s+OR\s+IGNORE\s+INTO", q, flags=re.IGNORECASE):
        q = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", q, flags=re.IGNORECASE)
        q = q.rstrip(";") + " ON CONFLICT DO NOTHING"

    # Translate INSERT OR REPLACE INTO for each table's primary key
    if re.match(r"^INSERT\s+OR\s+REPLACE\s+INTO", q, flags=re.IGNORECASE):
        q = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", q, flags=re.IGNORECASE).rstrip(";")
        low = q.lower()
        if "into bot_settings" in low:
            q += " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        elif "into class_overrides" in low:
            q += (
                " ON CONFLICT (class_id, day_date, lesson) DO UPDATE SET "
                "kind = EXCLUDED.kind, note = EXCLUDED.note"
            )
        elif "into class_timetable" in low:
            q += (
                " ON CONFLICT (class_id, day, pos) DO UPDATE SET "
                "subject = EXCLUDED.subject, room = EXCLUDED.room, teachers = EXCLUDED.teachers"
            )
        elif "into teacher_timetable" in low:
            q += (
                " ON CONFLICT (teacher_key, day, lesson) DO UPDATE SET "
                "class_label = EXCLUDED.class_label, class_id = EXCLUDED.class_id, "
                "subject = EXCLUDED.subject, room = EXCLUDED.room"
            )

    return q


class SupabaseConnection:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None
        self.connect()

    def connect(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = psycopg2.connect(self.dsn)
        self._conn.autocommit = True

    def cursor(self):
        return SupabaseCursor(self)

    def commit(self):
        try:
            if self._conn and not self._conn.closed:
                self._conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            self.connect()


class SupabaseCursor:
    def __init__(self, db: SupabaseConnection):
        self.db = db
        self._cur = db._conn.cursor()

    def _reset_cursor(self):
        self.db.connect()
        self._cur = self.db._conn.cursor()

    def execute(self, query: str, params=None):
        sql = _translate_sql(query)
        for attempt in range(2):
            try:
                if self.db._conn is None or self.db._conn.closed:
                    self._reset_cursor()
                if params is not None:
                    return self._cur.execute(sql, tuple(params))
                return self._cur.execute(sql)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt == 0:
                    self._reset_cursor()
                else:
                    raise
            except Exception:
                try:
                    self.db._conn.rollback()
                except Exception:
                    pass
                raise

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


conn = SupabaseConnection(DATABASE_URL)
cursor = conn.cursor()

# ======================================================================
#  DATABASE SETUP
# ======================================================================
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        role TEXT DEFAULT 'user'
    )
""")


def ensure_column(table: str, column: str, ddl: str):
    """Adds a column to an existing table if it is missing (safe migration)."""
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
    conn.commit()


ensure_column("users", "is_active", "INTEGER DEFAULT 1")
ensure_column("users", "banned", "INTEGER DEFAULT 0")
ensure_column("users", "morning_alert", "INTEGER DEFAULT 1")
ensure_column("users", "lesson_alerts", "INTEGER DEFAULT 1")
ensure_column("users", "announcements", "INTEGER DEFAULT 1")
ensure_column("users", "class_id", "TEXT")
ensure_column("users", "teacher_key", "TEXT")          # set only by the owner (/setteacher)
ensure_column("users", "admin_class", "TEXT")          # class admin: admin of ONE class only
ensure_column("users", "menu_shown", "INTEGER DEFAULT 0")
ensure_column("users", "inbox_on", "INTEGER DEFAULT 1")       # admin: does he get the students' messages (/inbox)?

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
        room TEXT,
        PRIMARY KEY (teacher_key, day, lesson)
    )
""")
ensure_column("teacher_timetable", "room", "TEXT")   # databases made by an older version get the column
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
    5: "Shanba (Saturday)",
}

DAY_CODE_MAP = {
    "1": 0, "du": 0, "dushanba": 0, "mon": 0, "monday": 0,
    "2": 1, "se": 1, "seshanba": 1, "tue": 1, "tuesday": 1,
    "3": 2, "ch": 2, "chorshanba": 2, "wed": 2, "wednesday": 2,
    "4": 3, "pa": 3, "payshanba": 3, "thu": 3, "thursday": 3,
    "5": 4, "ju": 4, "juma": 4, "fri": 4, "friday": 4,
    "6": 5, "sh": 5, "shanba": 5, "sat": 5, "saturday": 5,
}

# Classes (like 10-E) and teachers can have Saturday lessons, so they share the same day tables
TEACHER_DAY_NAMES = DAY_NAMES
TEACHER_DAY_CODE_MAP = DAY_CODE_MAP

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

# --- 10-E umumta'lim ---
BELLS_10E = [dict(b) for b in BELLS_10B]

DEFAULT_TIMETABLE_10E = {
    0: [  # 1 - Dushanba
        {"subject": "Kelajak soati", "room": "129", "teachers": "Zebo"},
        {"subject": "Fizika", "room": "129", "teachers": "Risolat"},
        {"subject": "Rus tili", "room": "129", "teachers": "Gulnora"},
        {"subject": "Algebra", "room": "129", "teachers": "Durdona"},
        {"subject": "Geometriya", "room": "129", "teachers": "Durdona"},
        {"subject": "Ingliz tili", "room": "129", "teachers": "Muzaffar"},
    ],
    1: [  # 2 - Seshanba
        {"subject": "Kimyo", "room": "129", "teachers": "Umid"},
        {"subject": "Informatika", "room": "203", "teachers": "Feruza"},
        {"subject": "Biologiya", "room": "129", "teachers": "Zebo"},
        {"subject": "Geografiya", "room": "223", "teachers": "Mamura"},
        {"subject": "Tarbiya", "room": "129", "teachers": "Azada"},
    ],
    2: [  # 3 - Chorshanba
        {"subject": "Ona tili", "room": "129", "teachers": "Nazokat"},
        {"subject": "CHQBT", "room": "129", "teachers": "To'lqin"},
        {"subject": "O'zbek tarix", "room": "129", "teachers": "Nazokat"},
        {"subject": "Geografiya", "room": "223", "teachers": "Mamura"},
        {"subject": "Rus tili", "room": "129", "teachers": "Gulnora"},
    ],
    3: [  # 4 - Payshanba
        {"subject": "Ingliz tili", "room": "129", "teachers": "Muzaffar"},
        {"subject": "Algebra", "room": "129", "teachers": "Durdona"},
        {"subject": "Kimyo", "room": "129", "teachers": "Umid"},
        {"subject": "Huquq", "room": "129", "teachers": "Azada"},
        {"subject": "CHQBT", "room": "129", "teachers": "To'lqin"},
        {"subject": "Adabiyot", "room": "N/A", "teachers": "Nazokat"},
    ],
    4: [  # 5 - Juma
        {"subject": "Informatika", "room": "203", "teachers": "Feruza"},
        {"subject": "Fizika", "room": "129", "teachers": "Risolat"},
        {"subject": "Jahon tarix", "room": "129", "teachers": "Nazokat"},
        {"subject": "Biologiya", "room": "129", "teachers": "Zebo"},
        {"subject": "Ona tili", "room": "129", "teachers": "Nazokat"},
        {"subject": "Adabiyot", "room": "N/A", "teachers": "Nazokat"},
    ],
    5: [  # 6 - Shanba
        {"subject": "Algebra", "room": "129", "teachers": "Durdona"},
        {"subject": "Geometriya", "room": "129", "teachers": "Durdona"},
        {"subject": "Jismoniy tarbiya", "room": "Sport zal", "teachers": "Dilshod"},
        {"subject": "Jismoniy tarbiya", "room": "Sport zal", "teachers": "Dilshod"},
    ],
}

# --- 10-01 and 10-02 ---
BELLS_1001 = [dict(b) for b in BELLS_10B]
BELLS_1002 = [dict(b) for b in BELLS_10B]


def _ls(subject: str, teachers: str = "-") -> dict:
    """One lesson without a room."""
    return {"subject": subject, "room": "", "teachers": teachers}


_ENG_1 = "Raximberdiyeva H, Isaqova H"     # English teachers of 10-01
_ENG_2 = "Raximberdiyeva H, Axmedova Z"    # English teachers of 10-02

DEFAULT_TIMETABLE_1001 = {
    0: [  # Dushanba
        _ls("Kelajak soati", "Avazov S"),
        _ls("O'zbekiston tarixi", "Matnazarova D"),
        _ls("Fizika", "Satimov Dilshod, Yusupov F"),
        _ls("Rus tili", "Jumaniyazova Y"),
        _ls("Informatika va AT", "Avazov S"),
        _ls("Dasturlash", "Yusupov Yunus"),
        _ls("Akademik va KM", "Axmedova Z"),
    ],
    1: [  # Seshanba
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Ona tili", "Masharipova M"),
        _ls("Ingliz tili", _ENG_1),
        _ls("CHQBT"),
        _ls("O'zbekiston tarixi", "Matnazarova D"),
        _ls("Tarbiya", "Ro'zmetova D"),
    ],
    2: [  # Chorshanba
        _ls("Algebra", "Olimov O, Sultonmurotova O"),
        _ls("Ingliz tili", _ENG_1),
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Adabiyot", "Masharipova M"),
        _ls("Geometriya", "Olimov O, Sultonmurotova O"),
        _ls("Chiziqli algebra", "Yuldashev Y"),
    ],
    3: [  # Payshanba
        _ls("Informatika va AT (T-f)", "G'oyibnazarov R"),
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Algebra", "Olimov O, Sultonmurotova O"),
        _ls("Ona tili", "Masharipova M"),
        _ls("Dasturlash", "Yusupov Yunus"),
        _ls("CHQBT"),
        _ls("Jaxon tarixi", "Matnazarova D"),
    ],
    4: [  # Juma
        _ls("Adabiyot", "Masharipova M"),
        _ls("Rus tili", "Jumaniyazova Y"),
        _ls("Dasturlash", "Yusupov Yunus"),
        _ls("Akademik va KM", "Axmedova Z"),
        _ls("Informatika va AT (T-f)", "G'oyibnazarov R"),
        _ls("Ingliz tili", _ENG_1),
        _ls("Jismoniy tarbiya", "Husainov R"),
    ],
}

DEFAULT_TIMETABLE_1002 = {
    0: [  # Dushanba
        _ls("Kelajak soati", "Satliqov G'"),
        _ls("Ingliz tili", _ENG_2),
        _ls("Informatika va AT", "Avazov S"),
        _ls("Adabiyot", "Ko'palova Sh"),
        _ls("O'zbekiston tarixi", "Matnazarova D"),
        _ls("Akademik va KM", "Axmedova Z"),
        _ls("Dasturlash", "Yusupov Yunus"),
    ],
    1: [  # Seshanba
        _ls("O'zbekiston tarixi", "Matnazarova D"),
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Algebra", "Satliqov G', Ibragimov S"),
        _ls("Tarbiya", "Ro'zmetova D"),
        _ls("CHQBT"),
        _ls("Fizika", "Yusupov F, Satimov Dilshod"),
    ],
    2: [  # Chorshanba
        _ls("Jaxon tarixi", "Matnazarova D"),
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Adabiyot", "Ko'palova Sh"),
        _ls("Rus tili", "Jumaniyazova Y"),
        _ls("Chiziqli algebra", "Yuldashev Y"),
        _ls("Ona tili", "Ko'palova Sh"),
    ],
    3: [  # Payshanba
        _ls("Matematika I II", "Zaripov Sh"),
        _ls("Ingliz tili", _ENG_2),
        _ls("Informatika va AT (T-f)", "G'oyibnazarov R"),
        _ls("Dasturlash", "Yusupov Yunus"),
        _ls("Algebra", "Satliqov G', Ibragimov S"),
        _ls("Ona tili", "Ko'palova Sh"),
        _ls("CHQBT"),
    ],
    4: [  # Juma
        _ls("Dasturlash", "Yusupov Yunus"),
        _ls("Geometriya", "Satliqov G', Ibragimov S"),
        _ls("Akademik va KM", "Axmedova Z"),
        _ls("Jismoniy tarbiya", "Husainov R"),
        _ls("Ingliz tili", _ENG_2),
        _ls("Rus tili", "Jumaniyazova Y"),
        _ls("Informatika va AT (T-f)", "G'oyibnazarov R"),
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
    "10e": {
        "name": "10-E umumta'lim",
        "short": "10-E",
        "morning": "08:00",
        "bells": BELLS_10E,
        "default": DEFAULT_TIMETABLE_10E,
    },
    "1001": {
        "name": "10-01",
        "short": "10-01",
        "morning": "08:00",
        "bells": BELLS_1001,
        "default": DEFAULT_TIMETABLE_1001,
    },
    "1002": {
        "name": "10-02",
        "short": "10-02",
        "morning": "08:00",
        "bells": BELLS_1002,
        "default": DEFAULT_TIMETABLE_1002,
    },
}

# ======================================================================
#  TEACHERS  (add a new teacher = add one block here)
# ======================================================================
TEACHER_BELLS = [dict(b) for b in BELLS_10A]  # 8 slots


def _tl(lesson: int, klass: str, class_id: str | None = None, subject: str = "Informatika",
        room: str | None = None) -> dict:
    return {"lesson": lesson, "class": klass, "class_id": class_id, "subject": subject, "room": room}


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


# Saidova O'g'iljon (physics) - lessons with rooms
def _ph(lesson: int, klass: str, room: str, class_id: str | None = None) -> dict:
    return _tl(lesson, klass, class_id, "Fizika", room)


TIMETABLE_OGILJON = {
    0: [  # Dushanba
        _ph(1, "11-A aniq", "202"),
        _ph(2, "10-B sinf (1-kichik guruh)", "Dežurka", "10b"),
        _ph(3, "8-A aniq", "206"),
        _ph(5, "7-B aniq", "202"),
        _ph(6, "11-A aniq", "202"),
        _ph(8, "10-A aniq", "202", "10a"),
    ],
    1: [  # Seshanba
        _ph(2, "11-B aniq", "206"),
        _ph(3, "11-A aniq", "202"),
        _ph(4, "10-A aniq", "202", "10a"),
        _ph(5, "8-A aniq", "206"),
        _ph(6, "9-D tabiiy", "202"),
        _ph(7, "10-B aniq", "207", "10b"),
    ],
    2: [  # Chorshanba
        _ph(1, "10-A aniq", "202", "10a"),
        _ph(2, "11-A aniq", "202"),
        _ph(3, "10-B aniq", "207", "10b"),
        _ph(5, "7-B aniq", "207"),
        _ph(8, "11-B aniq", "202"),
    ],
    3: [  # Payshanba
        _ph(1, "10-A aniq", "202", "10a"),
        _ph(2, "10-B aniq", "207", "10b"),
        _ph(3, "8-A aniq", "202"),
        _ph(4, "11-B aniq", "206"),
        _ph(5, "11-B aniq", "206"),
        _ph(6, "11-A aniq", "202"),
    ],
    4: [  # Juma
        _ph(2, "11-A aniq", "202"),
        _ph(4, "10-B aniq", "202", "10b"),
        _ph(5, "7-B aniq", "202"),
        _ph(6, "10-A aniq", "202", "10a"),
        _ph(7, "11-B aniq", "202"),
    ],
}


# Olloyorov Muhammadsodiq (maths)
def _math(lesson: int, klass: str, subject: str, class_id: str | None = None) -> dict:
    return _tl(lesson, klass, class_id, subject, "217")


TIMETABLE_MUHAMMADSODIQ = {
    0: [  # 1-kun: Dushanba
        _math(1, "10-B", "Algebra", "10b"),
        _math(2, "6-A", "Matematika"),
        _math(3, "11-B", "Algebra"),
        _math(4, "11-B", "Geometriya"),
    ],
    1: [  # 2-kun: Seshanba
        _math(1, "6-A", "Matematika"),
        _math(2, "6-A", "Matematika"),
        _math(3, "10-B", "Algebra", "10b"),
        _math(4, "10-B", "Geometriya", "10b"),
    ],
    2: [  # 3-kun: Chorshanba
        _math(1, "6-B", "Matematika"),
        _math(2, "10-B", "Algebra", "10b"),
        _math(3, "11-B", "Algebra"),
        _math(4, "11-B", "Geometriya"),
    ],
    3: [  # 4-kun: Payshanba
        _math(1, "6-A", "Matematika"),
        _math(2, "6-A", "Matematika"),
        _math(3, "10-B", "Algebra", "10b"),
        _math(4, "10-B", "Geometriya", "10b"),
    ],
    4: [  # 5-kun: Juma
        _math(1, "6-A", "Matematika"),
        _math(2, "10-B", "Geometriya", "10b"),
        _math(3, "11-B", "Algebra"),
        _math(4, "11-B", "Geometriya"),
    ],
}

TEACHERS = {
    "umarbek": {
        "name": "Madrimov Umarbek",
        "short": "Umarbek",
        "subject": "Informatika",
        "morning": "08:00",
        "remind_before": 5,
        "default": TIMETABLE_UMARBEK,
        "timetable": {},
    },
    "ogiljon": {
        "name": "Saidova O'g'iljon",
        "short": "O'g'iljon",
        "subject": "Fizika",
        "morning": "08:00",
        "remind_before": 5,
        "aliases": ["o'g'lijon"],
        "default": TIMETABLE_OGILJON,
        "timetable": {},
    },
    "muhammadsodiq": {
        "name": "Olloyorov Muhammadsodiq",
        "short": "Muhammadsodiq",
        "subject": "Matematika",
        "always_show_subject": True,
        "morning": "08:00",
        "remind_before": 5,
        "aliases": ["sodiq"],
        "default": TIMETABLE_MUHAMMADSODIQ,
        "timetable": {},
    },
}

# ======================================================================
#  CLASS TIMETABLES: migration, seeding, loading
# ======================================================================
TIMETABLES: dict[str, dict[int, list[dict]]] = {}


def table_exists(name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
        (name,),
    )
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
                    "INSERT INTO teacher_timetable (teacher_key, day, lesson, class_label, class_id, subject, room) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (teacher_key, day, item["lesson"], item["class"], item["class_id"], item["subject"],
                     item.get("room")),
                )
        conn.commit()
    set_setting(f"seeded_teacher_{teacher_key}", "1")


def load_teacher_timetables():
    """Reloads the lessons of every teacher from the database."""
    fresh: dict[str, dict[int, list[dict]]] = {key: {} for key in TEACHERS}
    cursor.execute(
        "SELECT teacher_key, day, lesson, class_label, class_id, subject, room FROM teacher_timetable "
        "ORDER BY teacher_key, day, lesson"
    )
    for key, day, lesson, class_label, class_id, subject, room in cursor.fetchall():
        if key in fresh:
            fresh[key].setdefault(day, []).append(
                {"lesson": lesson, "class": class_label, "class_id": class_id, "subject": subject, "room": room}
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
    words = label.split()
    return class_token(words[0]) if words else None


def _plain(text: str) -> str:
    """Lower case without apostrophes: O'g'iljon = o’g’iljon = ogiljon."""
    return text.lower().translate(str.maketrans("", "", "'`ʻʼ’‘"))


def teacher_token(raw: str | None) -> str | None:
    """'Umarbek', "O'g'iljon", 'saidova', 'Muhammadsodiq' -> the teacher key (None if unknown)."""
    if not raw:
        return None
    token = _plain(raw)
    for key, cfg in TEACHERS.items():
        names = [key, cfg["short"]] + cfg["name"].split() + cfg.get("aliases", [])
        if token in [_plain(n) for n in names]:
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
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            is_active=1,
            role=CASE WHEN users.user_id = ? THEN 'owner' ELSE users.role END
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
    """Changing the role always clears a class-admin scope (use set_class_admin for class admins)."""
    cursor.execute("UPDATE users SET role = ?, admin_class = NULL WHERE user_id = ?", (role, int(user_id)))
    conn.commit()


# --- class admins: an admin who manages ONE class only ---
def get_admin_class(user_id: int) -> str | None:
    """The class a class-admin is responsible for. None for the owner, global admins and normal users."""
    if int(user_id) == OWNER_ID:
        return None
    cursor.execute("SELECT role, admin_class FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    if row and row[0] == "admin" and row[1] in CLASSES:
        return row[1]
    return None


def is_global_admin(user_id: int) -> bool:
    """Owner or an admin without a class limit."""
    return is_admin_or_owner(user_id) and get_admin_class(user_id) is None


def set_class_admin(user_id: int, class_id: str):
    """Makes the user an admin of ONE class. If he has no class yet he is put into that class."""
    cursor.execute(
        "UPDATE users SET role = 'admin', admin_class = ? WHERE user_id = ?", (class_id, int(user_id))
    )
    cursor.execute(
        "UPDATE users SET class_id = ? WHERE user_id = ? AND class_id IS NULL", (class_id, int(user_id))
    )
    conn.commit()


def can_manage_user(admin_id: int, target_id: int) -> bool:
    admin_class = get_admin_class(admin_id)
    if admin_class is None:
        return is_admin_or_owner(admin_id)
    if get_user_role(target_id) in ("admin", "owner") or get_user_teacher(target_id):
        return False
    return get_user_class(target_id) == admin_class


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


# --- inbox: messages that people write to the bot are forwarded to admins ---
def is_inbox_on(user_id: int) -> bool:
    """Does this admin (or the owner) want the messages? On by default."""
    cursor.execute("SELECT inbox_on FROM users WHERE user_id = ?", (int(user_id),))
    row = cursor.fetchone()
    return True if row is None or row[0] is None else bool(row[0])


def set_inbox(user_id: int, value: bool):
    cursor.execute("UPDATE users SET inbox_on = ? WHERE user_id = ?", (1 if value else 0, int(user_id)))
    conn.commit()


def get_inbox_recipients(sender_id: int) -> list[int]:
    sender_id = int(sender_id)
    if sender_id == OWNER_ID:
        return []

    recipients = set()
    if is_inbox_on(OWNER_ID):
        recipients.add(OWNER_ID)

    is_student = get_user_role(sender_id) != "admin" and not get_user_teacher(sender_id)
    if is_student:
        class_id = get_user_class(sender_id)
        cursor.execute(
            "SELECT user_id, admin_class FROM users "
            "WHERE role = 'admin' AND is_active = 1 AND banned = 0 AND inbox_on = 1"
        )
        for admin_id, admin_class in cursor.fetchall():
            if admin_id == sender_id:
                continue
            if admin_class in CLASSES:
                if class_id == admin_class:
                    recipients.add(admin_id)
            else:
                recipients.add(admin_id)

    recipients.discard(sender_id)
    return sorted(recipients)


def migrate_inbox_setting():
    """One-time: the old global '/inbox off' of the owner becomes the owner's personal setting."""
    if get_setting("inbox_migrated") == "1":
        return
    if get_setting("inbox", "on") == "off":
        cursor.execute("UPDATE users SET inbox_on = 0 WHERE user_id = ?", (OWNER_ID,))
        conn.commit()
    set_setting("inbox_migrated", "1")


migrate_inbox_setting()


# --- recipients ---
def get_recipients(flag: str | None = None, class_id: str | None = None) -> list[int]:
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
        "SELECT user_id, username, first_name, role, is_active, banned, class_id, teacher_key, admin_class "
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
    wd = today.weekday()
    return today - timedelta(days=wd) if wd < 5 else today + timedelta(days=7 - wd)


def teacher_week_start(today: date) -> date:
    wd = today.weekday()
    return today - timedelta(days=wd) if wd < 6 else today + timedelta(days=1)


def class_week_start(class_id: str, today: date) -> date:
    wd = today.weekday()
    has_saturday = 5 in class_timetable(class_id)
    if wd == 6 or (wd == 5 and not has_saturday):
        return today + timedelta(days=7 - wd)
    return today - timedelta(days=wd)


def get_off_reason(class_id: str, d: date) -> str | None:
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
    for wd in range(6 if 5 in timetable else 5):
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
            room = f" ({esc(item['room'])})" if item.get("room") else ""
            text += f"{idx + 1}. {esc(item['subject'])}{room}{mark}\n"
        text += "\n"
    return text


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
    if item.get("room"):
        text += f"\n🚪 Xona: {esc(item['room'])}"
    text += f"\n👨‍🏫 O'qituvchi: {esc(item['teachers'])}"
    if note:
        text += f"\n⚠️ <b>{esc(note)}</b>"
    return text


def next_lesson_text(class_id: str, now: datetime) -> str:
    nxt = find_next(class_id, now)
    if not nxt:
        return "ℹ️ Yaqin kunlarda dars topilmadi."
    return _next_block(class_id, nxt, now)


def class_now_text(class_id: str, now: datetime) -> tuple[str, bool]:
    bells = class_bells(class_id)
    lessons = effective_lessons(class_id, now.date())
    if not lessons:
        return "🎉 <b>Bugun dars yo'q.</b>\nKeyingi dars: /next", False

    current = find_current(class_id, now)
    upcoming = find_next(class_id, now, days=1)

    if current:
        idx, item, end = current
        bell = bells[idx]
        left = math.ceil((end - now).total_seconds() / 60)
        room_line = f"🚪 Xona: {esc(item['room'])}\n" if item.get("room") else ""
        text = (
            f"📍 <b>Hozir:</b> {bell['lesson']}-dars — {esc(item['subject'])}\n"
            f"{room_line}"
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


def _subject_shown(teacher_key: str, item: dict) -> bool:
    cfg = TEACHERS[teacher_key]
    return bool(cfg.get("always_show_subject")) or item["subject"] != cfg["subject"]


def _room_text(item: dict) -> str:
    return f"🚪 Xona: {esc(item['room'])}" if item.get("room") else ""


def _teacher_line(teacher_key: str, d: date, item: dict) -> str:
    bell = teacher_bell(item["lesson"])
    line = f"<b>{item['lesson']}-dars ({bell['start']} - {bell['end']}):</b> {esc(item['class'])}"
    if _subject_shown(teacher_key, item):
        line += f" — {esc(item['subject'])}"
    if item.get("room"):
        line += f" | {_room_text(item)}"
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
            line = f"{item['lesson']}. {esc(item['class'])}"
            if _subject_shown(teacher_key, item):
                line += f" — {esc(item['subject'])}"
            if item.get("room"):
                line += f" ({esc(item['room'])})"
            text += line + "\n"
        text += "\n"
    return text


def format_teacher_timetable(teacher_key: str) -> str:
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
            if _subject_shown(teacher_key, item):
                line += f" — {esc(item['subject'])}"
            if item.get("room"):
                line += f" | 🚪 {esc(item['room'])}"
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
    if _subject_shown(teacher_key, item):
        text += f"\n📚 {esc(item['subject'])}"
    if item.get("room"):
        text += f"\n{_room_text(item)}"
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
        if _subject_shown(teacher_key, item):
            text += f"\n📚 {esc(item['subject'])}"
        if item.get("room"):
            text += f"\n{_room_text(item)}"
        if item.get("class_id"):
            note = get_notes(item["class_id"], now.date()).get(item["lesson"])
            if note:
                text += f"\n⚠️ <b>{esc(note)}</b>"
        upcoming = find_teacher_next(teacher_key, now, days=1)
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
        await asyncio.sleep(0.05)
    return result


async def broadcast_to(bot: Bot, text: str, audience: str, flag: str | None = "announcements") -> dict:
    result = {"ok": 0, "blocked": 0, "failed": 0}
    for u_id in get_audience_recipients(audience, flag):
        status = await safe_send(bot, u_id, text)
        result[status] += 1
        await asyncio.sleep(0.05)
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
        room_line = f"🚪 <b>Xona:</b> {esc(next_lesson['room'])}\n" if next_lesson.get("room") else ""
        msg = (
            f"🔔 <b>{bell['lesson']}-dars ({esc(current_lesson['subject'])}) tugadi!</b>\n\n"
            f"➡️ <b>Keyingi dars ({next_bell['lesson']}-dars):</b> {esc(next_lesson['subject'])}\n"
            f"⏰ <b>Boshlanishi:</b> {next_bell['start']}\n"
            f"{room_line}"
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
    now = datetime.now(UZB_TZ)
    cfg = TEACHERS[teacher_key]
    if now.weekday() == 5 and not cfg["timetable"].get(5):
        return
    text = f"☀️ <b>Xayrli tong, {esc(cfg['short'])} ustoz!</b>\n\n" + format_teacher_day(teacher_key, now.date())
    if not teacher_lessons(teacher_key, now.date()):
        text += "\n\n" + teacher_next_text(teacher_key, now)

    for user_id in get_teacher_recipients(teacher_key, "morning_alert"):
        await safe_send(bot, user_id, text)
        await asyncio.sleep(0.05)


async def send_teacher_lesson_reminder(bot: Bot, teacher_key: str, lesson_num: int):
    today = datetime.now(UZB_TZ).date()
    lessons = teacher_lessons(teacher_key, today)
    item = next((l for l in lessons if l["lesson"] == lesson_num), None)
    if item is None:
        return

    cfg = TEACHERS[teacher_key]
    bell = teacher_bell(lesson_num)
    lead = cfg.get("remind_before", 5)
    text = (
        f"⏰ <b>{fmt_minutes(lead)}dan keyin dars!</b>\n"
        f"🏫 Sinf: <b>{esc(item['class'])}</b>\n"
        f"🕘 {lesson_num}-dars ({bell['start']} - {bell['end']})"
    )
    if _subject_shown(teacher_key, item):
        text += f"\n📚 {esc(item['subject'])}"
    if item.get("room"):
        text += f"\n{_room_text(item)}"
    previous = next((l for l in lessons if l["lesson"] == lesson_num - 1), None)
    if previous and previous["class"] == item["class"]:
        text += "\n🔁 Oldingi dars bilan bir xil sinf"
    if item.get("class_id"):
        note = get_notes(item["class_id"], today).get(lesson_num)
        if note:
            text += f"\n⚠️ <b>{esc(note)}</b>"

    for user_id in get_teacher_recipients(teacher_key, "lesson_alerts"):
        await safe_send(bot, user_id, text)
        await asyncio.sleep(0.05)
