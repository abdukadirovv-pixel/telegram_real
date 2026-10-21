"""
main.py  -  starts everything

  schedule.py  = data + logic + scheduled jobs
  command.py   = all commands and buttons
  main.py      = this file: creates the bot, the scheduler and the web server

Run:  python main.py
"""
import os
import asyncio
import logging
from datetime import datetime, timedelta

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, types
from aiohttp import web  # already installed together with aiogram
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from schedule import (
    BOT_TOKEN,
    CLASSES,
    OWNER_ID,
    TEACHER_BELLS,
    TEACHERS,
    UZB_TZ,
    is_banned,
    send_lesson_end_alert,
    send_morning_alert,
    send_teacher_lesson_reminder,
    send_teacher_morning,
)
from command import router


# --- BAN MIDDLEWARE (banned users are silently ignored) ---
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != OWNER_ID and is_banned(user.id):
            if isinstance(event, types.CallbackQuery):
                await event.answer("⛔", show_alert=True)
            return None
        return await handler(event, data)


# --- SCHEDULER (one set of jobs per class + one per teacher) ---
def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    # misfire_grace_time: a job that starts up to 2 minutes late (busy server) is still sent, not skipped
    scheduler = AsyncIOScheduler(
        timezone=UZB_TZ,
        job_defaults={"misfire_grace_time": 120, "coalesce": True},
    )

    for class_id, cfg in CLASSES.items():
        # 1. Morning schedule alert (Mon-Sat; on days without lessons the job simply does nothing,
        #    so classes with Saturday lessons like 10-E work without extra settings)
        m_hour, m_minute = map(int, cfg["morning"].split(":"))
        scheduler.add_job(
            send_morning_alert, "cron", day_of_week="mon-sat",
            hour=m_hour, minute=m_minute, args=[bot, class_id],
        )

        # 2. Lesson end alerts (Mon-Sat) matching the bell schedule of the class
        for bell in cfg["bells"]:
            e_hour, e_minute = map(int, bell["end"].split(":"))
            scheduler.add_job(
                send_lesson_end_alert, "cron", day_of_week="mon-sat",
                hour=e_hour, minute=e_minute, args=[bot, class_id, bell["lesson"]],
            )

    # 3. Teachers (Mon-Sat, teachers also work on Saturday)
    for teacher_key, cfg in TEACHERS.items():
        # 3a. morning message with all lessons of the day
        t_hour, t_minute = map(int, cfg["morning"].split(":"))
        scheduler.add_job(
            send_teacher_morning, "cron", day_of_week="mon-sat",
            hour=t_hour, minute=t_minute, args=[bot, teacher_key],
        )

        # 3b. a reminder a few minutes BEFORE every lesson slot: "you are going to teach class X".
        #     Slots where the teacher has no lesson are skipped inside the job itself.
        lead = cfg.get("remind_before", 5)
        for bell in TEACHER_BELLS:
            remind_at = datetime.strptime(bell["start"], "%H:%M") - timedelta(minutes=lead)
            scheduler.add_job(
                send_teacher_lesson_reminder, "cron", day_of_week="mon-sat",
                hour=remind_at.hour, minute=remind_at.minute, args=[bot, teacher_key, bell["lesson"]],
            )

    return scheduler


# --- HEALTH WEB SERVER (needed by hosts that run the bot as a "Web Service", e.g. Render) ---
async def start_health_server():
    """Opens the port the host gives us in the PORT variable and answers 'OK'.
    The bot itself still uses polling. Does nothing when PORT is not set (e.g. on your PC)."""
    port = os.getenv("PORT")
    if not port:
        return None

    async def handle(request):
        return web.Response(text="Bot is running ✅")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    logging.info(f"Health server is listening on port {port}")
    return runner


async def main():
    logging.basicConfig(level=logging.INFO)

    # --- SUPABASE DATABASE POOL INITIALIZATION ---
    database_url = os.getenv("DATABASE_URL")
    db_pool = None
    if database_url:
        try:
            db_pool = await asyncpg.create_pool(database_url)
            logging.info("Connected to Supabase PostgreSQL successfully!")
        except Exception as e:
            logging.error(f"Failed to connect to the database: {e}")
    else:
        logging.warning("DATABASE_URL environment variable is not set!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    # Store db_pool in dispatcher data so handlers can access it easily via event workflow data
    if db_pool:
        dp["db_pool"] = db_pool

    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(router)

    runner = await start_health_server()
    scheduler = setup_scheduler(bot)
    scheduler.start()
    
    try:
        await dp.start_polling(bot)
    finally:
        # --- CLEANUP ---
        if db_pool:
            await db_pool.close()
            logging.info("Database connection pool closed.")
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
