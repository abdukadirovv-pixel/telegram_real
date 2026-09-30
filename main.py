
import os
import html
import asyncio
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
 
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
 
# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8741655203:AAHU1fyYZ3mnPRuOXO35Onp2-QF6tLnntAg")
OWNER_ID = int(os.getenv("OWNER_ID", 8780228920))
UZB_TZ = ZoneInfo("Asia/Tashkent")
 
# Ensure the data directory exists
os.makedirs("data", exist_ok=True)
 
# Save SQLite database inside the persistent volume folder
conn = sqlite3.connect("data/subscribers.db", check_same_thread=False)
cursor = conn.cursor()
 
# Create users table
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        role TEXT DEFAULT 'user'
    )
""")
conn.commit()
 
 
def register_or_update_user(user_id: int, username: str, first_name: str):
    user_id = int(user_id)
    role = "owner" if user_id == OWNER_ID else "user"
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
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
    cursor.execute("SELECT user_id, username, first_name, role FROM users")
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
    users = get_all_users()
    for u_id, _, _, _ in users:
        try:
            await bot.send_message(u_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Failed to send alert to user {u_id}: {e}")
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
 
 
# --- ERROR LOGGING (so crashes are never silent) ---
@dp.errors()
async def on_error(event: ErrorEvent):
    logging.exception("Handler crashed: %s", event.exception)
 
 
# --- COMMAND HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
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
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
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
 
 
# --- OWNER / ADMIN COMMANDS ---
@dp.message(Command("users"))
async def cmd_list_users(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not is_admin_or_owner(message.from_user.id):
        await message.answer("⛔ Faqat adminlar foydalana oladi.")
        return
 
    users = get_all_users()
    if not users:
        await message.answer("👥 Hozircha foydalanuvchilar yo'q.")
        return
 
    lines = ["👥 <b>Foydalanuvchilar Ro'yxati:</b>\n"]
    for u_id, uname, fname, role in users:
        name = html.escape(fname or "NoName")
        un = f"@{html.escape(uname)}" if uname and uname != "NoUsername" else "NoUsername"
        lines.append(f"• <b>{name}</b> ({un}) | ID: <code>{u_id}</code> | <code>{role}</code>")
    text = "\n".join(lines)
 
    # Telegram limit is 4096 chars per message
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], parse_mode="HTML")
 
 
@dp.message(Command("dm"))
async def cmd_direct_message(message: types.Message, command: CommandObject):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not is_admin_or_owner(message.from_user.id):
        await message.answer("⛔ Faqat adminlar foydalana oladi.")
        return
 
    parts = (command.args or "").split(maxsplit=1)
    target_id = parse_id(parts[0]) if parts else None
    if target_id is None or len(parts) < 2:
        await message.answer("⚠️ Ishlatish: <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code>", parse_mode="HTML")
        return
 
    try:
        await bot.send_message(target_id, f"📩 <b>Admin Xabari:</b>\n\n{html.escape(parts[1])}", parse_mode="HTML")
        await message.answer(f"✅ Xabar <code>{target_id}</code> ga yuborildi!", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Yuborib bo'lmadi: {html.escape(str(e))}", parse_mode="HTML")
 
 
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not is_admin_or_owner(message.from_user.id):
        await message.answer("⛔ Faqat adminlar foydalana oladi.")
        return
 
    if not command.args:
        await message.answer("⚠️ Ishlatish: <code>/broadcast &lt;xabar&gt;</code>", parse_mode="HTML")
        return
 
    text = f"📢 <b>E'lon:</b>\n\n{html.escape(command.args)}"
    sent = failed = 0
    for u_id, *_ in get_all_users():
        try:
            await bot.send_message(u_id, text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            logging.error(f"Broadcast failed for {u_id}: {e}")
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
 
    await message.answer(f"✅ Yuborildi: {sent}\n❌ Xatolik: {failed}")
 
 
@dp.message(Command("makeadmin"))
async def cmd_make_admin(message: types.Message, command: CommandObject):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if get_user_role(message.from_user.id) != "owner":
        await message.answer("❌ Faqat Owner admin tayinlashi mumkin!")
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
 
    set_user_role(target_id, "admin")
    await message.answer(f"👑 <code>{target_id}</code> Admin qilindi!", parse_mode="HTML")
 
 
@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message, command: CommandObject):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if get_user_role(message.from_user.id) != "owner":
        await message.answer("❌ Faqat Owner adminlikni olib tashlashi mumkin!")
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
 
 
# --- AUTOMATED SCHEDULER JOBS ---
async def send_morning_alert():
    now = datetime.now(UZB_TZ)
    weekday = now.weekday()
    if weekday in TIMETABLE:
        msg = "☀️ **Xayrli kun! Darslar boshlanishiga 1 soat qoldi.**\n\n" + format_day_schedule(weekday)
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
 