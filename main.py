import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8741655203:AAHMqMozxrl-qYsbkG_RKOPrwRSH512gNT8")
UZB_TZ = ZoneInfo("Asia/Tashkent")

# --- DATABASE SETUP (Automatically remembers subscribers) ---
conn = sqlite3.connect("subscribers.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS subscribers (
        user_id INTEGER PRIMARY KEY
    )
""")
conn.commit()

def register_user(user_id: int):
    cursor.execute("INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)", (user_id,))
    conn.commit()

def get_all_subscribers():
    cursor.execute("SELECT user_id FROM subscribers")
    return [row[0] for row in cursor.fetchall()]

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

TIMETABLE = {
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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def format_day_schedule(weekday_idx: int) -> str:
    if weekday_idx not in TIMETABLE:
        return "🎉 **Dam olish kuni! Darslar yo'q.**"

    day_title = DAY_NAMES.get(weekday_idx, "Dars Jadvali")
    lessons = TIMETABLE[weekday_idx]
    text = f"📅 **Dars Jadvali: {day_title}**\n\n"

    for idx, item in enumerate(lessons):
        bell = BELL_SCHEDULE[idx]
        room_str = f"| 🚪 Xona: {item['room']}" if item.get("room") else ""
        text += f"**{bell['lesson']}-dars ({bell['start']} - {bell['end']}):** {item['subject']}\n"
        text += f"└ 👨‍🏫 *O'qituvchi:* {item['teachers']} {room_str}\n\n"

    return text

async def broadcast_message(text: str):
    subscribers = get_all_subscribers()
    for u_id in subscribers:
        try:
            await bot.send_message(u_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Failed to send alert to user {u_id}: {e}")

# --- COMMAND HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_user(message.from_user.id)
    await message.answer(
        "👋 **Xush kelibsiz!** Bot sizni eslab qoldi.\n\n"
        "✅ **Endi har kuni darslar va dars tugashi haqida avtomatik xabarlar olasiz!**\n\n"
        "• Bugungi jadval: `/schedule`\n"
        "• Kunlar bo'yicha: `/schedule 1` (Dushanba) dan `/schedule 5` (Juma) gacha\n"
        "• Qisqa kodlar: `/schedule du`, `/schedule se`, `/schedule ch`, `/schedule pa`, `/schedule ju`",
        parse_mode="Markdown"
    )

@dp.message(Command("schedule", "timetable"))
async def cmd_schedule(message: types.Message):
    register_user(message.from_user.id)  # Auto-register user on command
    args = message.text.split()

    if len(args) > 1:
        query_arg = args[1].lower()
        if query_arg in DAY_CODE_MAP:
            target_day = DAY_CODE_MAP[query_arg]
        else:
            await message.answer(
                "❌ **Noto'g'ri kun kiritildi!**\n\n"
                "Misol uchun:\n"
                "• `/schedule 1` (Dushanba)\n"
                "• `/schedule 2` (Seshanba)\n"
                "• `/schedule 5` (Juma)\n"
                "• Yoki kodi bilan: `/schedule du`",
                parse_mode="Markdown"
            )
            return
    else:
        target_day = datetime.now(UZB_TZ).weekday()

    text = format_day_schedule(target_day)
    await message.answer(text, parse_mode="Markdown")

# --- AUTOMATED SCHEDULER JOBS ---
async def send_morning_alert():
    now = datetime.now(UZB_TZ)
    weekday = now.weekday()
    if weekday in TIMETABLE:
        msg = f"☀️ **Xayrli kun! Darslar boshlanishiga 1 soat qoldi.**\n\n" + format_day_schedule(weekday)
        await broadcast_message(msg)

async def send_lesson_end_alert(lesson_num: int):
    now = datetime.now(UZB_TZ)
    weekday = now.weekday()

    if weekday in TIMETABLE:
        lessons = TIMETABLE[weekday]
        idx = lesson_num - 1

        if idx < len(lessons):
            current_lesson = lessons[idx]
            bell = BELL_SCHEDULE[idx]

            if idx + 1 < len(lessons):
                next_lesson = lessons[idx + 1]
                next_bell = BELL_SCHEDULE[idx + 1]
                msg = (
                    f"🔔 **{bell['lesson']}-dars ({current_lesson['subject']}) tugadi!**\n\n"
                    f"➡️ **Keyingi dars ({next_bell['lesson']}-dars):** {next_lesson['subject']}\n"
                    f"⏰ **Boshlanishi:** {next_bell['start']}\n"
                    f"🚪 **Xona:** {next_lesson['room']}\n"
                    f"👨‍🏫 **O'qituvchi:** {next_lesson['teachers']}"
                )
            else:
                msg = f"🎉 **{bell['lesson']}-dars ({current_lesson['subject']}) tugadi! Bugungi barcha darslar yakunlandi.**"

            await broadcast_message(msg)

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