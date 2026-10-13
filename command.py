"""
command.py  -  every command, button and menu of the bot (an aiogram Router)

  * student commands (/start /class /schedule /now ...) + menu buttons
  * teacher versions of the same commands
  * admin / owner commands
  * hidden owner-only commands (/setteacher, /removeteacher, /teachers)

All logic and data live in schedule.py. main.py includes this router.
"""
import logging

from aiogram import Bot, BaseMiddleware, F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from schedule import *  # noqa: F401,F403  (config, database, timetables, texts, helpers)

router = Router()


# ======================================================================
#  FILTERS  (a command with these filters is invisible to everybody else:
#  for them the message simply falls through to the inbox handler)
# ======================================================================
class IsOwner(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return bool(message.from_user) and message.from_user.id == OWNER_ID


class IsAdmin(Filter):
    async def __call__(self, message: types.Message) -> bool:
        return bool(message.from_user) and is_admin_or_owner(message.from_user.id)


# ======================================================================
#  KEYBOARDS
# ======================================================================
BTN_NOW = "📍 Hozirgi dars"
BTN_LEFT = "⏳ Qancha vaqt qoldi"
BTN_CLASS = "🏫 Sinfni almashtirish"


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Menu buttons under the text field. Students: 3 buttons. Teachers: only the current lesson."""
    if get_user_teacher(user_id):
        rows = [[KeyboardButton(text=BTN_NOW)]]
    else:
        rows = [
            [KeyboardButton(text=BTN_NOW), KeyboardButton(text=BTN_LEFT)],
            [KeyboardButton(text=BTN_CLASS)],
        ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def day_keyboard() -> InlineKeyboardMarkup:
    names = ["Du", "Se", "Ch", "Pa", "Ju"]
    row = [InlineKeyboardButton(text=n, callback_data=f"day:{i}") for i, n in enumerate(names)]
    today_row = [InlineKeyboardButton(text="📍 Bugun", callback_data="day:today")]
    return InlineKeyboardMarkup(inline_keyboard=[row, today_row])


def teacher_day_keyboard() -> InlineKeyboardMarkup:
    names = ["Du", "Se", "Ch", "Pa", "Ju", "Sh"]
    row = [InlineKeyboardButton(text=n, callback_data=f"tday:{i}") for i, n in enumerate(names)]
    today_row = [InlineKeyboardButton(text="📍 Bugun", callback_data="tday:today")]
    return InlineKeyboardMarkup(inline_keyboard=[row, today_row])


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    rows = []
    for key, label in SETTING_LABELS.items():
        mark = "✅" if settings.get(key) else "❌"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"set:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def class_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🏫 {cfg['name']}", callback_data=f"cls:{cid}")]
        for cid, cfg in CLASSES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ======================================================================
#  TEXTS
# ======================================================================
CLASS_PROMPT = (
    "🏫 <b>Sinfingizni tanlang:</b>\n"
    "Tanlaganingizdan keyin shu sinfning jadvali va xabarlarini olasiz.\n"
    "(Keyinroq o'zgartirish: /class)"
)

NEED_CLASS = (
    "⚠️ Sinfni ko'rsating: <code>10a</code> yoki <code>10b</code> "
    "(yoki /class orqali o'z sinfingizni tanlang)."
)


DENIED_CLASS = "⛔ Siz faqat o'z sinfingizni boshqara olasiz."
DENIED_USER = "⛔ Siz faqat o'z sinfingiz o'quvchilarini boshqara olasiz."
GLOBAL_ONLY = "⛔ Bu buyruq faqat asosiy adminlar uchun."


def welcome_text(class_id: str) -> str:
    return (
        "👋 <b>Xush kelibsiz!</b> Bot sizni eslab qoldi.\n"
        f"🏫 Sinfingiz: <b>{esc(class_name(class_id))}</b> (o'zgartirish: /class)\n\n"
        "✅ <b>Endi har kuni darslar va dars tugashi haqida avtomatik xabarlar olasiz!</b>\n\n"
        "• Bugungi jadval: <code>/schedule</code>\n"
        "• Kunlar bo'yicha: <code>/schedule 1</code> (Dushanba) dan <code>/schedule 5</code> (Juma) gacha\n"
        "• Qisqa kodlar: <code>/schedule du</code>, <code>/schedule se</code>, <code>/schedule ch</code>, "
        "<code>/schedule pa</code>, <code>/schedule ju</code>\n"
        "• Hozirgi va keyingi dars: <code>/now</code>, <code>/next</code>\n"
        "• Sozlamalar: <code>/settings</code>\n"
        "• Barcha buyruqlar: <code>/help</code>\n\n"
        "ℹ️ Botga yozgan xabarlaringizni bot egasi ko'rishi mumkin."
    )


def teacher_welcome_text(teacher_key: str) -> str:
    cfg = TEACHERS[teacher_key]
    return (
        f"👋 <b>Xush kelibsiz, {esc(cfg['short'])} ustoz!</b>\n"
        "👨‍🏫 Siz o'qituvchi sifatida ulandingiz.\n\n"
        "☀️ Har kuni soat 08:00 da shu kungi darslaringiz haqida xabar olasiz.\n\n"
        "• Bugungi darslar: <code>/schedule</code>\n"
        "• Ertangi darslar: <code>/tomorrow</code>\n"
        "• Haftalik darslar: <code>/week</code>\n"
        "• Hozirgi va keyingi dars: <code>/now</code>, <code>/next</code>\n"
        "• Sozlamalar: <code>/settings</code>\n"
        "• Barcha buyruqlar: <code>/help</code>"
    )


# /help shows ONLY user commands. Admin / owner commands are in /adminhelp.
USER_HELP = (
    "📖 <b>Buyruqlar</b>\n\n"
    "• <code>/class</code> — sinfni tanlash / almashtirish\n"
    "• <code>/schedule</code> — bugungi jadval (<code>/schedule 1</code> … <code>/schedule 5</code>)\n"
    "• <code>/tomorrow</code> — ertangi jadval\n"
    "• <code>/week</code> — haftalik jadval\n"
    "• <code>/now</code> — hozirgi dars\n"
    "• <code>/left</code> — dars tugashiga qancha vaqt qoldi\n"
    "• <code>/next</code> — keyingi dars\n"
    "• <code>/menu</code> — menyu tugmalarini ko'rsatish\n"
    "• <code>/settings</code> — xabarlarni sozlash\n"
    "• <code>/stop</code> — barcha xabarlarni o'chirish"
)

TEACHER_HELP = (
    "📖 <b>Buyruqlar</b>\n\n"
    "• <code>/schedule</code> — bugungi darslaringiz (<code>/schedule 1</code> … <code>/schedule 6</code>)\n"
    "• <code>/tomorrow</code> — ertangi darslar\n"
    "• <code>/week</code> — haftalik darslar\n"
    "• <code>/now</code> — hozirgi dars\n"
    "• <code>/next</code> — keyingi dars\n"
    "• <code>/menu</code> — menyu tugmasini ko'rsatish\n"
    "• <code>/settings</code> — xabarlarni sozlash\n"
    "• <code>/stop</code> — barcha xabarlarni o'chirish"
)

_CLASS_BROADCASTS = "\n".join(
    f"• <code>/broadcast_{cid} &lt;xabar&gt;</code> — {esc(cfg['name'])}" for cid, cfg in CLASSES.items()
)

ADMIN_HELP = (
    "🛠 <b>Admin buyruqlari</b>\n\n"
    "• <code>/users [10a|10b|none|all]</code> — foydalanuvchilar (har sinf alohida oynada)\n"
    "• <code>/stats</code> — statistika\n"
    "• <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code>\n"
    "• <code>/ban &lt;user_id&gt;</code> / <code>/unban &lt;user_id&gt;</code>\n"
    "• <code>/setclass &lt;user_id&gt; &lt;sinf&gt;</code> — foydalanuvchi sinfini o'zgartirish\n\n"
    "📢 <b>Xabar yuborish</b>\n"
    "• <code>/broadcast &lt;xabar&gt;</code> — hammaga\n"
    "• <code>/broadcast_students &lt;xabar&gt;</code> — barcha talabalarga\n"
    + _CLASS_BROADCASTS + "\n\n"
    "📅 <b>Jadval</b>\n"
    "• <code>/holiday [sinf|all] &lt;sana&gt; [sabab]</code> — dars yo'q kuni\n"
    "• <code>/change [sinf] &lt;sana&gt; &lt;dars#&gt; &lt;matn&gt;</code> — darsga eslatma\n"
    "• <code>/changes [sinf|all]</code> — o'zgarishlar ro'yxati\n"
    "• <code>/clearchange [sinf|all] &lt;sana&gt;</code> — o'zgarishlarni o'chirish\n"
    "• <code>/setlesson [sinf] &lt;kun&gt; &lt;dars#&gt; &lt;fan&gt;; &lt;xona&gt;; &lt;o'qituvchilar&gt;</code>\n"
    "• <code>/dellesson [sinf] &lt;kun&gt;</code> — kunning oxirgi darsini o'chirish\n"
    "Sinf: <code>10a</code> yoki <code>10b</code> (yozilmasa — o'z sinfingiz).\n"
    "Sana: <code>YYYY-MM-DD</code>, <code>bugun</code> yoki <code>ertaga</code>"
)

def class_admin_help(class_id: str) -> str:
    """Help of a class admin: only what he is allowed to do (his own class)."""
    return (
        f"🛠 <b>Sinf admini: {esc(class_name(class_id))}</b>\n\n"
        "Siz faqat o'z sinfingizni boshqara olasiz.\n\n"
        "• <code>/users</code> — sinfingiz o'quvchilari\n"
        f"• <code>/broadcast_{class_id} &lt;xabar&gt;</code> — sinfingizga e'lon\n"
        "• <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code> — o'quvchiga xabar\n"
        "• <code>/ban &lt;user_id&gt;</code> / <code>/unban &lt;user_id&gt;</code>\n"
        f"• <code>/setclass &lt;user_id&gt; {class_id}</code> — sinfsiz o'quvchini sinfingizga qo'shish "
        "(<code>none</code> — sinfdan chiqarish)\n\n"
        "📅 <b>Jadval</b>\n"
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
    "• <code>/makeclassadmin &lt;user_id&gt; &lt;sinf&gt;</code> — faqat bitta sinf admini\n"
    "• <code>/admins</code> — adminlar ro'yxati\n"
    "• <code>/inbox on|off</code> — foydalanuvchi xabarlarini senga yuborish\n"
    "• <code>/users teachers</code> — o'qituvchilar oynasi\n"
    "• <code>/broadcast_teachers &lt;xabar&gt;</code> — faqat o'qituvchilarga\n"
    "• <code>/setteacher &lt;ism&gt; &lt;user_id&gt;</code> — o'qituvchi rejimini yoqish\n"
    "• <code>/removeteacher &lt;user_id&gt;</code> — o'qituvchi rejimini o'chirish\n"
    "• <code>/teachers</code> — o'qituvchilar ro'yxati\n"
    "• <code>/setteacherlesson [ism] &lt;kun&gt; &lt;dars#&gt; &lt;sinf&gt;; [fan]</code> — o'qituvchiga dars qo'shish / almashtirish\n"
    "• <code>/delteacherlesson [ism] &lt;kun&gt; &lt;dars#&gt;</code> — o'qituvchi darsini o'chirish\n"
    "• <code>/teacherweek [ism]</code> — o'qituvchining saqlangan jadvali"
)


# ======================================================================
#  HELPERS
# ======================================================================
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


async def require_global_admin(message: types.Message) -> bool:
    """Owner or a normal (global) admin. Class admins are refused."""
    if not await require_admin(message):
        return False
    if get_admin_class(message.from_user.id):
        await message.answer(GLOBAL_ONLY)
        return False
    return True


async def ensure_class(message: types.Message) -> str | None:
    """Returns the user's class, or shows the class buttons and returns None."""
    class_id = get_user_class(message.from_user.id)
    if class_id:
        return class_id
    await message.answer(CLASS_PROMPT, parse_mode="HTML", reply_markup=class_keyboard())
    return None


def resolve_scope(args: str | None, user_id: int, allow_all: bool = False):
    """For admin commands. The first word may be a class code (10a / 10b) or 'all'.
    Returns (list_of_class_ids | None | 'denied', remaining_text).
    Without a code the admin's own class is used. A class admin may only work with his own class."""
    text = (args or "").strip()
    parts = text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""

    explicit = None
    if parts:
        first = class_token(parts[0])
        if first:
            explicit = [first]
        elif allow_all and parts[0].lower() in ("all", "hamma"):
            explicit = list(CLASSES)
    remainder = rest if explicit is not None else text

    admin_class = get_admin_class(user_id)
    if admin_class:
        if explicit is not None and explicit != [admin_class]:
            return "denied", remainder
        return [admin_class], remainder

    if explicit is not None:
        return explicit, remainder
    own = get_user_class(user_id)
    if own:
        return [own], text
    return None, text


async def get_scope(message: types.Message, args: str | None, allow_all: bool = False):
    """resolve_scope + the error messages. Returns (class_ids, rest) or None (message already sent)."""
    scope, rest = resolve_scope(args, message.from_user.id, allow_all)
    if scope == "denied":
        await message.answer(DENIED_CLASS)
        return None
    if scope is None:
        await message.answer(NEED_CLASS, parse_mode="HTML")
        return None
    return scope, rest


async def safe_edit(call: types.CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def send_funny_sticker(message: types.Message):
    """Funny sticker after 'no lessons left'. Uses FUNNY_STICKER_ID if it is set,
    otherwise a single big emoji (Telegram shows it as an animated emoji)."""
    try:
        if FUNNY_STICKER_ID:
            await message.answer_sticker(FUNNY_STICKER_ID)
            return
    except Exception as e:
        logging.warning(f"Could not send the funny sticker: {e}")
    await message.answer("🤣")


class MenuOnceMiddleware(BaseMiddleware):
    """Shows the menu buttons once to every user who already has a class / is a teacher
    (so people who used the bot before the menu existed get it too)."""

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        try:
            user = event.from_user
            if (
                user
                and event.chat.type == "private"
                and not menu_shown(user.id)
                and (get_user_class(user.id) or get_user_teacher(user.id))
            ):
                mark_menu_shown(user.id)
                await event.answer("👇 Menyu tugmalari yoqildi", reply_markup=main_menu(user.id))
        except Exception as e:
            logging.warning(f"Could not show the menu: {e}")
        return result


router.message.middleware(MenuOnceMiddleware())


# --- error logging (so crashes are never silent) ---
@router.errors()
async def on_error(event: ErrorEvent):
    logging.exception("Handler crashed: %s", event.exception)


# ======================================================================
#  USER + TEACHER COMMANDS
# ======================================================================
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    subscribe_if_stopped(message.from_user.id)
    uid = message.from_user.id

    teacher = get_user_teacher(uid)
    if teacher:
        mark_menu_shown(uid)
        await message.answer(teacher_welcome_text(teacher), parse_mode="HTML", reply_markup=main_menu(uid))
        return

    class_id = get_user_class(uid)
    if not class_id:
        await message.answer(
            "👋 <b>Xush kelibsiz!</b>\n\n" + CLASS_PROMPT,
            parse_mode="HTML",
            reply_markup=class_keyboard(),
        )
        return

    mark_menu_shown(uid)
    await message.answer(welcome_text(class_id), parse_mode="HTML", reply_markup=main_menu(uid))


@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    if not get_user_teacher(uid) and not await ensure_class(message):
        return
    mark_menu_shown(uid)
    await message.answer("👇 Menyu", reply_markup=main_menu(uid))


@router.message(Command("class"))
@router.message(F.text == BTN_CLASS)
async def cmd_class(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id

    if get_user_teacher(uid):
        await message.answer("👨‍🏫 Siz o'qituvchi sifatida ulangansiz — sinf tanlash shart emas.")
        return

    class_id = get_user_class(uid)
    current = f"Hozirgi sinf: <b>{esc(class_name(class_id))}</b>\n\n" if class_id else ""
    await message.answer(current + CLASS_PROMPT, parse_mode="HTML", reply_markup=class_keyboard())


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = TEACHER_HELP if get_user_teacher(message.from_user.id) else USER_HELP
    await message.answer(text, parse_mode="HTML")


@router.message(Command("schedule", "timetable"))
async def cmd_schedule(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    args = message.text.split()
    today = datetime.now(UZB_TZ).date()

    # --- teacher version ---
    teacher = get_user_teacher(uid)
    if teacher:
        if len(args) > 1:
            query_arg = args[1].lower()
            if query_arg not in TEACHER_DAY_CODE_MAP:
                await message.answer(
                    "❌ <b>Noto'g'ri kun kiritildi!</b>\n"
                    "Misol: <code>/schedule 1</code> (Dushanba) … <code>/schedule 6</code> (Shanba)",
                    parse_mode="HTML"
                )
                return
            target = teacher_week_start(today) + timedelta(days=TEACHER_DAY_CODE_MAP[query_arg])
        else:
            target = today
        await message.answer(
            format_teacher_day(teacher, target), parse_mode="HTML", reply_markup=teacher_day_keyboard()
        )
        return

    # --- student version ---
    class_id = await ensure_class(message)
    if not class_id:
        return

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

    await message.answer(format_date_schedule(class_id, target), parse_mode="HTML", reply_markup=day_keyboard())


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    target = datetime.now(UZB_TZ).date() + timedelta(days=1)

    teacher = get_user_teacher(uid)
    if teacher:
        await message.answer(
            format_teacher_day(teacher, target), parse_mode="HTML", reply_markup=teacher_day_keyboard()
        )
        return

    class_id = await ensure_class(message)
    if not class_id:
        return
    await message.answer(format_date_schedule(class_id, target), parse_mode="HTML", reply_markup=day_keyboard())


@router.message(Command("week"))
async def cmd_week(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    today = datetime.now(UZB_TZ).date()

    teacher = get_user_teacher(uid)
    if teacher:
        await message.answer(
            format_teacher_week(teacher, teacher_week_start(today)), parse_mode="HTML"
        )
        return

    class_id = await ensure_class(message)
    if not class_id:
        return
    await message.answer(format_week(class_id, week_start(today)), parse_mode="HTML")


@router.message(Command("now"))
@router.message(F.text == BTN_NOW)
async def cmd_now(message: types.Message):
    """/now and the '📍 Hozirgi dars' button."""
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    now = datetime.now(UZB_TZ)

    teacher = get_user_teacher(uid)
    if teacher:
        await message.answer(teacher_now_text(teacher, now), parse_mode="HTML")
        return

    class_id = await ensure_class(message)
    if not class_id:
        return

    text, finished = class_now_text(class_id, now)
    await message.answer(text, parse_mode="HTML")
    if finished:
        await send_funny_sticker(message)


@router.message(Command("left"))
@router.message(F.text == BTN_LEFT)
async def cmd_left(message: types.Message):
    """/left and the '⏳ Qancha vaqt qoldi' button."""
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    now = datetime.now(UZB_TZ)

    teacher = get_user_teacher(uid)
    if teacher:
        await message.answer(teacher_now_text(teacher, now), parse_mode="HTML")
        return

    class_id = await ensure_class(message)
    if not class_id:
        return
    await message.answer(class_time_left_text(class_id, now), parse_mode="HTML")


@router.message(Command("next"))
async def cmd_next(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    uid = message.from_user.id
    now = datetime.now(UZB_TZ)

    teacher = get_user_teacher(uid)
    if teacher:
        await message.answer(teacher_next_text(teacher, now), parse_mode="HTML")
        return

    class_id = await ensure_class(message)
    if not class_id:
        return
    await message.answer(next_lesson_text(class_id, now), parse_mode="HTML")


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "⚙️ <b>Sozlamalar</b>\nQaysi xabarlarni olishni tanlang (tugmani bosib yoqing/o'chiring):",
        parse_mode="HTML",
        reply_markup=settings_keyboard(get_settings(message.from_user.id)),
    )


@router.message(Command("stop"))
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


# ======================================================================
#  INLINE BUTTON CALLBACKS
# ======================================================================
@router.callback_query(F.data.startswith("cls:"))
async def cb_class(call: types.CallbackQuery):
    class_id = call.data.split(":", 1)[1]
    if class_id not in CLASSES:
        await call.answer()
        return

    register_or_update_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    if get_user_teacher(call.from_user.id):
        await call.answer("Siz o'qituvchisiz — sinf tanlash shart emas.", show_alert=True)
        return

    set_user_class(call.from_user.id, class_id)

    try:
        await call.message.edit_text(welcome_text(class_id), parse_mode="HTML")
    except TelegramBadRequest:
        pass

    mark_menu_shown(call.from_user.id)
    await call.message.answer("👇 Menyu tugmalari yoqildi", reply_markup=main_menu(call.from_user.id))
    await call.answer(f"{CLASSES[class_id]['name']} ✅")


@router.callback_query(F.data.startswith("day:"))
async def cb_day(call: types.CallbackQuery):
    register_or_update_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    class_id = get_user_class(call.from_user.id)
    if not class_id:
        await call.answer("Avval sinfni tanlang: /class", show_alert=True)
        return

    key = call.data.split(":", 1)[1]
    today = datetime.now(UZB_TZ).date()

    if key == "today":
        target = today
    elif key.isdigit() and 0 <= int(key) <= 4:
        target = week_start(today) + timedelta(days=int(key))
    else:
        await call.answer()
        return

    await safe_edit(call, format_date_schedule(class_id, target), day_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("tday:"))
async def cb_teacher_day(call: types.CallbackQuery):
    register_or_update_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    teacher = get_user_teacher(call.from_user.id)
    if not teacher:
        await call.answer()
        return

    key = call.data.split(":", 1)[1]
    today = datetime.now(UZB_TZ).date()

    if key == "today":
        target = today
    elif key.isdigit() and 0 <= int(key) <= 5:
        target = teacher_week_start(today) + timedelta(days=int(key))
    else:
        await call.answer()
        return

    await safe_edit(call, format_teacher_day(teacher, target), teacher_day_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("set:"))
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


# ======================================================================
#  ADMIN / OWNER COMMANDS
# ======================================================================
@router.message(Command("adminhelp"), IsAdmin())
async def cmd_admin_help(message: types.Message):
    """Help for admins and the owner (not listed in the normal /help)."""
    register_or_update_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    admin_class = get_admin_class(message.from_user.id)
    if admin_class:
        await message.answer(class_admin_help(admin_class), parse_mode="HTML")
        return
    text = ADMIN_HELP
    if get_user_role(message.from_user.id) == "owner":
        text += OWNER_HELP
    await message.answer(text, parse_mode="HTML")


# --- /users: a separate window for every class, for teachers and for people without a class ---
USERS_PAGE_SIZE = 15
USERS_SCOPES = ("none", "teachers", "all")


def users_menu_text() -> str:
    return "👥 <b>Foydalanuvchilar</b>\nBo'limni tanlang:"


def users_menu_keyboard(include_teachers: bool) -> InlineKeyboardMarkup:
    class_buttons = [
        InlineKeyboardButton(
            text=f"🏫 {cfg['short']} ({count_users_in_scope(cid, include_teachers)})",
            callback_data=f"usr:{cid}:0",
        )
        for cid, cfg in CLASSES.items()
    ]
    rows = [class_buttons[i:i + 2] for i in range(0, len(class_buttons), 2)]
    rows.append([InlineKeyboardButton(
        text=f"❓ Sinf tanlamagan ({count_users_in_scope('none', include_teachers)})",
        callback_data="usr:none:0",
    )])
    if include_teachers:
        rows.append([InlineKeyboardButton(
            text=f"👨‍🏫 O'qituvchilar ({count_users_in_scope('teachers', True)})",
            callback_data="usr:teachers:0",
        )])
    rows.append([InlineKeyboardButton(
        text=f"👥 Hammasi ({count_users_in_scope('all', include_teachers)})",
        callback_data="usr:all:0",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_users_window(scope: str, page: int, include_teachers: bool, show_back: bool = True):
    """Returns (text, keyboard) of one /users window (one page of one section)."""
    rows = get_users_in_scope(scope, include_teachers)
    total_pages = max(1, math.ceil(len(rows) / USERS_PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    chunk = rows[page * USERS_PAGE_SIZE:(page + 1) * USERS_PAGE_SIZE]

    if scope in CLASSES:
        title = f"🏫 {class_name(scope)}"
    else:
        title = {"none": "❓ Sinf tanlamagan", "teachers": "👨‍🏫 O'qituvchilar", "all": "👥 Hammasi"}[scope]

    text = f"<b>{esc(title)}</b> — {len(rows)} ta\n"
    if total_pages > 1:
        text += f"({page + 1}/{total_pages}-sahifa)\n"
    text += "\n"

    if not chunk:
        text += "Bu bo'limda foydalanuvchi yo'q."
    for u_id, uname, fname, role, is_active, banned, class_id, teacher_key, admin_class in chunk:
        name = esc(fname or "NoName")
        un = f"@{esc(uname)}" if uname and uname != "NoUsername" else "NoUsername"
        flags = (" 🚫" if banned else "") + (" 💤" if not is_active else "")
        role_label = role
        if role == "admin" and admin_class in CLASSES:
            role_label = f"admin·{CLASSES[admin_class]['short']}"   # class admin
        line = f"• <b>{name}</b> ({un}) | ID: <code>{u_id}</code> | <code>{role_label}</code>{flags}"
        if scope == "all":  # mixed window: show where the person belongs
            if include_teachers and teacher_key in TEACHERS:
                line += f" | 👨‍🏫 {esc(TEACHERS[teacher_key]['short'])}"
            else:
                line += " | 🏫 " + (CLASSES[class_id]["short"] if class_id in CLASSES else "—")
        text += line + "\n"
    text += "\n🚫 — ban qilingan, 💤 — botni bloklagan"

    keyboard_rows = []
    if total_pages > 1:
        keyboard_rows.append([
            InlineKeyboardButton(text="◀️", callback_data=f"usr:{scope}:{max(page - 1, 0)}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="usr:noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"usr:{scope}:{min(page + 1, total_pages - 1)}"),
        ])
    if show_back:
        keyboard_rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="usr:menu")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@router.message(Command("users"))
async def cmd_list_users(message: types.Message, command: CommandObject):
    """/users opens a menu; every class (and the teachers, owner only) has its own window.
    Shortcuts: /users 10a, /users 10b, /users none, /users all, /users teachers (owner)."""
    if not await require_admin(message):
        return

    # a class admin sees only the window of his own class
    admin_class = get_admin_class(message.from_user.id)
    if admin_class:
        text, keyboard = render_users_window(admin_class, 0, False, show_back=False)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    include_teachers = get_user_role(message.from_user.id) == "owner"
    arg = (command.args or "").strip().lower()
    scope = class_token(arg) or {"none": "none", "all": "all", "hamma": "all", "teachers": "teachers"}.get(arg)
    if scope == "teachers" and not include_teachers:
        scope = None

    if scope:
        text, keyboard = render_users_window(scope, 0, include_teachers)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    await message.answer(
        users_menu_text(), parse_mode="HTML", reply_markup=users_menu_keyboard(include_teachers)
    )


@router.callback_query(F.data.startswith("usr:"))
async def cb_users(call: types.CallbackQuery):
    if not is_admin_or_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    include_teachers = get_user_role(call.from_user.id) == "owner"
    admin_class = get_admin_class(call.from_user.id)
    parts = call.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if admin_class:
        # a class admin only ever gets his own class (whatever button data is sent)
        page = parse_id(parts[2]) if len(parts) > 2 and action == admin_class else 0
        text, keyboard = render_users_window(admin_class, page or 0, False, show_back=False)
        await safe_edit(call, text, keyboard)
    elif action == "menu":
        await safe_edit(call, users_menu_text(), users_menu_keyboard(include_teachers))
    elif action in CLASSES or action in USERS_SCOPES:
        if action == "teachers" and not include_teachers:
            await call.answer("⛔", show_alert=True)
            return
        page = parse_id(parts[2]) if len(parts) > 2 else 0
        text, keyboard = render_users_window(action, page or 0, include_teachers)
        await safe_edit(call, text, keyboard)
    await call.answer()


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not await require_global_admin(message):
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

    cursor.execute(
        "SELECT class_id, COUNT(*) FROM users "
        "WHERE is_active = 1 AND banned = 0 AND teacher_key IS NULL GROUP BY class_id"
    )
    per_class = dict(cursor.fetchall())
    class_lines = "".join(
        f"• {esc(cfg['name'])}: <b>{per_class.get(cid, 0)}</b>\n" for cid, cfg in CLASSES.items()
    )
    class_lines += f"• Sinf tanlamagan: <b>{per_class.get(None, 0)}</b>\n"

    await message.answer(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total}</b>\n"
        f"✅ Faol: <b>{active}</b>\n"
        f"💤 Botni bloklagan: <b>{inactive}</b>\n"
        f"🚫 Ban qilingan: <b>{banned}</b>\n"
        f"🛡 Adminlar (owner bilan): <b>{admins}</b>\n\n"
        "🏫 <b>Faol foydalanuvchilar sinflar bo'yicha:</b>\n"
        f"{class_lines}\n"
        f"☀️ Ertalabki xabar yoqilgan: <b>{morning}</b>\n"
        f"🔔 Dars tugashi xabari yoqilgan: <b>{lessons}</b>\n"
        f"📢 E'lonlar yoqilgan: <b>{announce}</b>\n\n"
        "📨 <b>Bot ishga tushgandan beri:</b>\n"
        f"• Yuborilgan: <b>{STATS['sent']}</b>\n"
        f"• Bloklangan: <b>{STATS['blocked']}</b>\n"
        f"• Xatolik: <b>{STATS['failed']}</b>",
        parse_mode="HTML"
    )


@router.message(Command("dm"))
async def cmd_direct_message(message: types.Message, command: CommandObject, bot: Bot):
    if not await require_admin(message):
        return

    parts = (command.args or "").split(maxsplit=1)
    target_id = parse_id(parts[0]) if parts else None
    if target_id is None or len(parts) < 2:
        await message.answer("⚠️ Ishlatish: <code>/dm &lt;user_id&gt; &lt;xabar&gt;</code>", parse_mode="HTML")
        return
    if not can_manage_user(message.from_user.id, target_id):
        await message.answer(DENIED_USER)
        return

    try:
        await bot.send_message(target_id, f"📩 <b>Admin Xabari:</b>\n\n{esc(parts[1])}", parse_mode="HTML")
        await message.answer(f"✅ Xabar <code>{target_id}</code> ga yuborildi!", parse_mode="HTML")
    except TelegramForbiddenError:
        mark_inactive(target_id)
        await message.answer("❌ Foydalanuvchi botni bloklagan.")
    except Exception as e:
        await message.answer(f"❌ Yuborib bo'lmadi: {esc(e)}", parse_mode="HTML")


# --- BROADCASTS: a separate command for everybody / students / each class / teachers ---
async def run_broadcast(message: types.Message, command: CommandObject, bot: Bot, audience: str, label: str):
    body = (command.args or "").strip()
    if not body:
        await message.answer(
            f"⚠️ Ishlatish: <code>/{esc(command.command)} &lt;xabar&gt;</code>", parse_mode="HTML"
        )
        return

    text = f"📢 <b>E'lon:</b>\n\n{esc(body)}"
    result = await broadcast_to(bot, text, audience)
    await message.answer(
        f"📢 {esc(label)}\n"
        f"✅ Yuborildi: {result['ok']}\n"
        f"🚫 Bloklagan: {result['blocked']}\n"
        f"❌ Xatolik: {result['failed']}",
        parse_mode="HTML"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject, bot: Bot):
    """/broadcast <xabar>  -  everybody (students and teachers)."""
    if not await require_admin(message):
        return

    own_class = get_admin_class(message.from_user.id)
    if own_class:
        await message.answer(
            f"⚠️ Siz faqat o'z sinfingizga yubora olasiz: <code>/broadcast_{own_class} &lt;xabar&gt;</code>",
            parse_mode="HTML"
        )
        return

    first = (command.args or "").split(maxsplit=1)[0].lower() if command.args else ""
    if class_token(first) or first in ("students", "teachers", "all"):
        # safety: the old syntax '/broadcast 10a text' must not be sent to everybody by mistake
        await message.answer(
            "⚠️ <code>/broadcast</code> hammaga yuboradi. Alohida yuborish uchun:\n"
            "• <code>/broadcast_students</code>\n"
            + "\n".join(f"• <code>/broadcast_{cid}</code>" for cid in CLASSES),
            parse_mode="HTML"
        )
        return

    await run_broadcast(message, command, bot, "all", "Hammaga")


@router.message(Command("broadcast_students"))
async def cmd_broadcast_students(message: types.Message, command: CommandObject, bot: Bot):
    """/broadcast_students <xabar>  -  all students of all classes (no teachers)."""
    if not await require_global_admin(message):
        return
    await run_broadcast(message, command, bot, "students", "Barcha talabalar")


@router.message(Command(*[f"broadcast_{cid}" for cid in CLASSES]))
async def cmd_broadcast_class(message: types.Message, command: CommandObject, bot: Bot):
    """/broadcast_10a <xabar>, /broadcast_10b <xabar>  -  one class only (generated from CLASSES)."""
    if not await require_admin(message):
        return
    class_id = command.command.split("_", 1)[1].lower()
    if class_id not in CLASSES:
        return
    own_class = get_admin_class(message.from_user.id)
    if own_class and own_class != class_id:
        await message.answer(DENIED_CLASS)
        return
    await run_broadcast(message, command, bot, class_id, class_name(class_id))


@router.message(Command("broadcast_teachers"), IsOwner())
async def cmd_broadcast_teachers(message: types.Message, command: CommandObject, bot: Bot):
    """/broadcast_teachers <xabar>  -  teachers only. Hidden: only the owner can use it."""
    await run_broadcast(message, command, bot, "teachers", "O'qituvchilar")


@router.message(Command("makeadmin"))
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


@router.message(Command("removeadmin"))
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


@router.message(Command("makeclassadmin"))
async def cmd_make_class_admin(message: types.Message, command: CommandObject, bot: Bot):
    """/makeclassadmin <user_id> <sinf>  -  an admin who manages ONE class only (owner only)."""
    if not await require_owner(message, "❌ Faqat Owner sinf admini tayinlashi mumkin!"):
        return

    usage = (
        "⚠️ Ishlatish: <code>/makeclassadmin &lt;user_id&gt; &lt;sinf&gt;</code>\n"
        "Masalan: <code>/makeclassadmin 123456789 10a</code>"
    )
    parts = (command.args or "").split()
    target_id = parse_id(parts[0]) if parts else None
    class_id = class_token(parts[1]) if len(parts) > 1 else None
    if target_id is None or class_id is None:
        await message.answer(usage, parse_mode="HTML")
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
    if get_user_teacher(target_id):
        await message.answer("❌ Bu foydalanuvchini sinf admini qilib bo'lmaydi.")
        return

    set_class_admin(target_id, class_id)
    await message.answer(
        f"👑 <code>{target_id}</code> — <b>{esc(class_name(class_id))}</b> admini qilindi!",
        parse_mode="HTML"
    )
    await safe_send(
        bot, target_id,
        f"👑 Siz <b>{esc(class_name(class_id))}</b> sinfining admini etib tayinlandingiz.\n"
        "Buyruqlar: /adminhelp"
    )


@router.message(Command("admins"))
async def cmd_admins(message: types.Message):
    """Owner: who is admin of what."""
    if not await require_owner(message, "❌ Faqat Owner foydalana oladi."):
        return

    cursor.execute(
        "SELECT user_id, username, first_name, admin_class FROM users WHERE role = 'admin' "
        "ORDER BY admin_class, first_name COLLATE NOCASE"
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("ℹ️ Hozircha adminlar yo'q.")
        return

    def line(user_id, username, first_name):
        un = f"@{esc(username)}" if username and username != "NoUsername" else "NoUsername"
        return f"• <b>{esc(first_name or 'NoName')}</b> ({un}) | ID: <code>{user_id}</code>"

    text = "👑 <b>Adminlar</b>\n"
    global_admins = [r for r in rows if r[3] not in CLASSES]
    text += "\n<b>Asosiy adminlar</b>\n" + ("\n".join(line(*r[:3]) for r in global_admins) or "—") + "\n"
    for class_id, cfg in CLASSES.items():
        members = [r for r in rows if r[3] == class_id]
        text += f"\n<b>{esc(cfg['name'])}</b>\n" + ("\n".join(line(*r[:3]) for r in members) or "—") + "\n"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("ban"))
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
    if not can_manage_user(message.from_user.id, target_id):
        await message.answer(DENIED_USER)
        return
    if get_user_role(target_id) == "admin" and get_user_role(message.from_user.id) != "owner":
        await message.answer("❌ Adminni faqat Owner ban qila oladi.")
        return

    set_banned(target_id, True)
    if get_user_role(target_id) == "admin":
        set_user_role(target_id, "user")
    await message.answer(f"🚫 <code>{target_id}</code> ban qilindi.", parse_mode="HTML")


@router.message(Command("unban"))
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
    if not can_manage_user(message.from_user.id, target_id):
        await message.answer(DENIED_USER)
        return

    set_banned(target_id, False)
    await message.answer(f"✅ <code>{target_id}</code> ban dan chiqarildi.", parse_mode="HTML")


@router.message(Command("setclass"))
async def cmd_set_class(message: types.Message, command: CommandObject, bot: Bot):
    """Admin/Owner: move one user to another class (or clear it so they must choose again)."""
    if not await require_admin(message):
        return

    usage = (
        "⚠️ Ishlatish: <code>/setclass &lt;user_id&gt; &lt;sinf&gt;</code>\n"
        "Masalan: <code>/setclass 123456789 10a</code>\n"
        "Sinfni tozalash (qaytadan tanlatish): <code>/setclass 123456789 none</code>"
    )
    parts = (command.args or "").split()
    target_id = parse_id(parts[0]) if parts else None
    if target_id is None or len(parts) < 2:
        await message.answer(usage, parse_mode="HTML")
        return

    wanted = parts[1].lower()
    new_class = class_token(wanted)
    if new_class is None and wanted not in ("none", "reset"):
        await message.answer(usage, parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bunday foydalanuvchi topilmadi.")
        return
    if get_user_teacher(target_id):
        await message.answer("❌ Bu foydalanuvchining sinfini o'zgartirib bo'lmaydi.")
        return

    # a class admin may only take classless students into HIS class or release students of his class
    own_class = get_admin_class(message.from_user.id)
    if own_class:
        target_class = get_user_class(target_id)
        if (
            get_user_role(target_id) in ("admin", "owner")
            or target_class not in (None, own_class)
            or new_class not in (None, own_class)
        ):
            await message.answer(DENIED_USER)
            return

    if new_class:
        set_user_class(target_id, new_class)
        await message.answer(
            f"✅ <code>{target_id}</code> sinfi: <b>{esc(class_name(new_class))}</b>",
            parse_mode="HTML"
        )
        await safe_send(
            bot, target_id,
            f"🏫 Sinfingiz <b>{esc(class_name(new_class))}</b> ga o'zgartirildi. Jadval: /schedule"
        )
    else:
        cursor.execute("UPDATE users SET class_id = NULL WHERE user_id = ?", (target_id,))
        conn.commit()
        await message.answer(f"✅ <code>{target_id}</code> sinfi tozalandi.", parse_mode="HTML")
        await safe_send(bot, target_id, "🏫 Sinfingiz tozalandi. Iltimos, /class orqali qaytadan tanlang.")


# --- SCHEDULE CHANGES / HOLIDAYS (admin, per class) ---
@router.message(Command("holiday"))
async def cmd_holiday(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args, allow_all=True)
    if res is None:
        return
    scope, rest = res

    parts = rest.split(maxsplit=1)
    target = parse_date(parts[0]) if parts else None
    if target is None:
        await message.answer(
            "⚠️ Ishlatish: <code>/holiday [sinf|all] &lt;sana&gt; [sabab]</code>\n"
            "Masalan: <code>/holiday all 2026-10-01 O'qituvchilar kuni</code>\n"
            "yoki: <code>/holiday 10a ertaga</code>",
            parse_mode="HTML"
        )
        return

    reason = parts[1].strip() if len(parts) > 1 else ""
    for class_id in scope:
        cursor.execute(
            "INSERT OR REPLACE INTO class_overrides (class_id, day_date, lesson, kind, note) "
            "VALUES (?, ?, 0, 'off', ?)",
            (class_id, target.isoformat(), reason),
        )
    conn.commit()
    names = ", ".join(class_name(c) for c in scope)
    await message.answer(
        f"🎉 <code>{target.isoformat()}</code> kuni dars yo'q deb belgilandi: <b>{esc(names)}</b>",
        parse_mode="HTML"
    )


@router.message(Command("change"))
async def cmd_change(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args)
    if res is None:
        return
    scope, rest = res
    class_id = scope[0]

    usage = (
        "⚠️ Ishlatish: <code>/change [sinf] &lt;sana&gt; &lt;dars#&gt; &lt;matn&gt;</code>\n"
        "Masalan: <code>/change ertaga 3 Bekor qilindi</code>\n"
        "yoki: <code>/change 10a 2026-10-05 2 Xona 305 ga ko'chdi</code>"
    )
    parts = rest.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(usage, parse_mode="HTML")
        return

    target = parse_date(parts[0])
    lesson = parse_id(parts[1])
    if target is None or lesson is None or not 1 <= lesson <= len(class_bells(class_id)):
        await message.answer(usage, parse_mode="HTML")
        return

    cursor.execute(
        "INSERT OR REPLACE INTO class_overrides (class_id, day_date, lesson, kind, note) "
        "VALUES (?, ?, ?, 'note', ?)",
        (class_id, target.isoformat(), lesson, parts[2].strip()),
    )
    conn.commit()
    await message.answer(
        f"⚠️ {esc(class_name(class_id))}: <code>{target.isoformat()}</code> kuni {lesson}-darsga eslatma qo'shildi.",
        parse_mode="HTML"
    )


@router.message(Command("changes"))
async def cmd_changes(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args, allow_all=True)
    if res is None:
        return
    scope, _ = res

    today = datetime.now(UZB_TZ).date().isoformat()
    lines = []
    for class_id in scope:
        cursor.execute(
            "SELECT day_date, lesson, kind, note FROM class_overrides "
            "WHERE class_id = ? AND day_date >= ? ORDER BY day_date, lesson LIMIT 40",
            (class_id, today),
        )
        rows = cursor.fetchall()
        if not rows:
            continue
        lines.append(f"\n<b>{esc(class_name(class_id))}</b>")
        for day_date, lesson, kind, note in rows:
            if kind == "off":
                lines.append(f"• <code>{day_date}</code> — 🎉 dars yo'q {esc(note)}".rstrip())
            else:
                lines.append(f"• <code>{day_date}</code> — {lesson}-dars: {esc(note)}")

    if not lines:
        await message.answer("ℹ️ Kelgusi o'zgarishlar yo'q.")
        return

    await message.answer("🗓 <b>Kelgusi o'zgarishlar:</b>" + "\n".join(lines), parse_mode="HTML")


@router.message(Command("clearchange"))
async def cmd_clear_change(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args, allow_all=True)
    if res is None:
        return
    scope, rest = res

    words = rest.split()
    target = parse_date(words[0]) if words else None
    if target is None:
        await message.answer(
            "⚠️ Ishlatish: <code>/clearchange [sinf|all] &lt;sana&gt;</code>", parse_mode="HTML"
        )
        return

    removed = 0
    for class_id in scope:
        cursor.execute(
            "DELETE FROM class_overrides WHERE class_id = ? AND day_date = ?",
            (class_id, target.isoformat()),
        )
        removed += cursor.rowcount
    conn.commit()
    names = ", ".join(class_name(c) for c in scope)
    await message.answer(
        f"🧹 {esc(names)}: <code>{target.isoformat()}</code> uchun {removed} ta o'zgarish o'chirildi.",
        parse_mode="HTML"
    )


# --- TIMETABLE EDITING (admin, per class) ---
@router.message(Command("setlesson"))
async def cmd_set_lesson(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args)
    if res is None:
        return
    scope, rest = res
    class_id = scope[0]

    usage = (
        "⚠️ Ishlatish: <code>/setlesson [sinf] &lt;kun&gt; &lt;dars#&gt; &lt;fan&gt;; &lt;xona&gt;; &lt;o'qituvchilar&gt;</code>\n"
        "Masalan: <code>/setlesson 10a 1 3 Ona tili; 210; Barno</code>\n"
        "Kun: 1-5 yoki du/se/ch/pa/ju"
    )
    parts = rest.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(usage, parse_mode="HTML")
        return

    day_idx = DAY_CODE_MAP.get(parts[0].lower())
    pos = parse_id(parts[1])
    if day_idx is None or pos is None or not 1 <= pos <= len(class_bells(class_id)):
        await message.answer(usage, parse_mode="HTML")
        return

    fields = [f.strip() for f in parts[2].split(";")]
    subject = fields[0]
    room = fields[1] if len(fields) > 1 and fields[1] else "N/A"
    teachers = fields[2] if len(fields) > 2 and fields[2] else "-"
    if not subject:
        await message.answer(usage, parse_mode="HTML")
        return

    current_count = len(class_timetable(class_id).get(day_idx, []))
    if pos > current_count + 1:
        await message.answer(
            f"❌ Bu kunda hozir {current_count} ta dars bor. Avval {current_count + 1}-darsni qo'shing."
        )
        return

    cursor.execute(
        "INSERT OR REPLACE INTO class_timetable (class_id, day, pos, subject, room, teachers) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (class_id, day_idx, pos, subject, room, teachers),
    )
    conn.commit()
    load_timetable()
    await message.answer(
        f"✅ {esc(class_name(class_id))} · {esc(DAY_NAMES[day_idx])}, {pos}-dars: "
        f"<b>{esc(subject)}</b> | {esc(room)} | {esc(teachers)}",
        parse_mode="HTML"
    )


@router.message(Command("dellesson"))
async def cmd_del_lesson(message: types.Message, command: CommandObject):
    if not await require_admin(message):
        return

    res = await get_scope(message, command.args)
    if res is None:
        return
    scope, rest = res
    class_id = scope[0]

    words = rest.split()
    day_idx = DAY_CODE_MAP.get(words[0].lower()) if words else None
    if day_idx is None:
        await message.answer(
            "⚠️ Ishlatish: <code>/dellesson [sinf] &lt;kun&gt;</code> (kun: 1-5 yoki du/se/ch/pa/ju)",
            parse_mode="HTML"
        )
        return

    lessons = class_timetable(class_id).get(day_idx, [])
    if not lessons:
        await message.answer("❌ Bu kunda darslar yo'q.")
        return

    last_pos = len(lessons)
    removed = lessons[-1]["subject"]
    cursor.execute(
        "DELETE FROM class_timetable WHERE class_id = ? AND day = ? AND pos = ?",
        (class_id, day_idx, last_pos),
    )
    conn.commit()
    load_timetable()
    await message.answer(
        f"🗑 {esc(class_name(class_id))} · {esc(DAY_NAMES[day_idx])}, {last_pos}-dars "
        f"(<b>{esc(removed)}</b>) o'chirildi.",
        parse_mode="HTML"
    )


# ======================================================================
#  HIDDEN OWNER-ONLY COMMANDS (teacher mode)
#  Not in any help text. For everybody except the owner these commands
#  do not exist (the message just goes to the inbox).
# ======================================================================
@router.message(Command("setteacher"), IsOwner())
async def cmd_set_teacher(message: types.Message, command: CommandObject, bot: Bot):
    """/setteacher Umarbek 123456789  -  the user starts getting teacher lessons instead of a class schedule."""
    usage = (
        "⚠️ Ishlatish: <code>/setteacher &lt;ism&gt; &lt;user_id&gt;</code>\n"
        "Masalan: <code>/setteacher Umarbek 123456789</code>\n"
        f"Mavjud o'qituvchilar: <code>{esc(', '.join(TEACHERS))}</code>"
    )
    target_id = None
    teacher_key = None
    for part in (command.args or "").split():
        if parse_id(part) is not None:
            target_id = int(part)
        else:
            teacher_key = teacher_token(part) or teacher_key

    if target_id is None or teacher_key is None:
        await message.answer(usage, parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bu foydalanuvchi botda yo'q (u avval /start bosishi kerak).")
        return

    set_user_teacher(target_id, teacher_key)
    mark_menu_shown(target_id)

    cfg = TEACHERS[teacher_key]
    await message.answer(
        f"✅ <code>{target_id}</code> endi o'qituvchi: <b>{esc(cfg['name'])}</b>\n"
        "Unga har kuni soat 08:00 da darslari yuboriladi.",
        parse_mode="HTML"
    )
    await safe_send(bot, target_id, teacher_welcome_text(teacher_key), reply_markup=main_menu(target_id))


@router.message(Command("removeteacher"), IsOwner())
async def cmd_remove_teacher(message: types.Message, command: CommandObject, bot: Bot):
    target_id = parse_id(command.args.split()[0]) if command.args else None
    if target_id is None:
        await message.answer("⚠️ Ishlatish: <code>/removeteacher &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    if not user_exists(target_id):
        await message.answer("❌ Bunday foydalanuvchi topilmadi.")
        return
    if not get_user_teacher(target_id):
        await message.answer("❌ Bu foydalanuvchi o'qituvchi emas.")
        return

    clear_user_teacher(target_id)
    await message.answer(f"✅ <code>{target_id}</code> o'qituvchi rejimidan chiqarildi.", parse_mode="HTML")
    await safe_send(
        bot, target_id,
        "👨‍🏫 O'qituvchi rejimi o'chirildi. Davom etish uchun /start ni bosing va sinfingizni tanlang.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("teachers"), IsOwner())
async def cmd_teachers(message: types.Message):
    rows = get_teacher_users()
    if not rows:
        await message.answer("ℹ️ Hozircha o'qituvchi ulanmagan.")
        return

    lines = ["👨‍🏫 <b>O'qituvchilar:</b>\n"]
    for user_id, username, first_name, teacher_key in rows:
        un = f"@{esc(username)}" if username and username != "NoUsername" else "NoUsername"
        lines.append(
            f"• <b>{esc(first_name or 'NoName')}</b> ({un}) | ID: <code>{user_id}</code> | "
            f"{esc(TEACHERS[teacher_key]['name'])}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# --- TEACHER LESSONS (owner only, like /setlesson for students) ---
def resolve_teacher(args: str | None):
    """The first word may be the teacher's name. With only one teacher the name is optional.
    Returns (teacher_key or None, remaining_text)."""
    text = (args or "").strip()
    parts = text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    if parts:
        key = teacher_token(parts[0])
        if key:
            return key, rest
    if len(TEACHERS) == 1:
        return next(iter(TEACHERS)), text
    return None, text


NEED_TEACHER = "⚠️ O'qituvchini ko'rsating: <code>{names}</code>"


@router.message(Command("setteacherlesson", "tlesson"), IsOwner())
async def cmd_set_teacher_lesson(message: types.Message, command: CommandObject):
    """/setteacherlesson umarbek 1 3 9-A aniq; Informatika  -  adds or replaces one lesson slot."""
    teacher_key, rest = resolve_teacher(command.args)
    if teacher_key is None:
        await message.answer(NEED_TEACHER.format(names=esc(", ".join(TEACHERS))), parse_mode="HTML")
        return

    usage = (
        "⚠️ Ishlatish: <code>/setteacherlesson [ism] &lt;kun&gt; &lt;dars#&gt; &lt;sinf&gt;; [fan]</code>\n"
        "Masalan: <code>/setteacherlesson umarbek 1 3 9-A aniq; Informatika</code>\n"
        "Kun: 1-6 yoki du/se/ch/pa/ju/sh. Dars#: 1-8. Fan yozilmasa — o'qituvchining asosiy fani."
    )
    parts = rest.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(usage, parse_mode="HTML")
        return

    day_idx = TEACHER_DAY_CODE_MAP.get(parts[0].lower())
    lesson = parse_id(parts[1])
    if day_idx is None or lesson is None or not 1 <= lesson <= len(TEACHER_BELLS):
        await message.answer(usage, parse_mode="HTML")
        return

    fields = [f.strip() for f in parts[2].split(";")]
    class_label = fields[0]
    subject = fields[1] if len(fields) > 1 and fields[1] else TEACHERS[teacher_key]["subject"]
    if not class_label:
        await message.answer(usage, parse_mode="HTML")
        return

    existed = any(item["lesson"] == lesson for item in TEACHERS[teacher_key]["timetable"].get(day_idx, []))
    cursor.execute(
        "INSERT OR REPLACE INTO teacher_timetable (teacher_key, day, lesson, class_label, class_id, subject) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (teacher_key, day_idx, lesson, class_label, class_id_from_label(class_label), subject),
    )
    conn.commit()
    load_teacher_timetables()

    bell = teacher_bell(lesson)
    status = "🔄 Almashtirildi" if existed else "✅ Qo'shildi"
    await message.answer(
        f"{status}: {esc(TEACHERS[teacher_key]['short'])} · "
        f"{esc(TEACHER_DAY_NAMES[day_idx])}, {lesson}-dars ({bell['start']} - {bell['end']})\n"
        f"🏫 <b>{esc(class_label)}</b> — {esc(subject)}",
        parse_mode="HTML"
    )


@router.message(Command("delteacherlesson", "deltlesson"), IsOwner())
async def cmd_del_teacher_lesson(message: types.Message, command: CommandObject):
    """/delteacherlesson umarbek 1 3  -  removes one lesson slot."""
    teacher_key, rest = resolve_teacher(command.args)
    if teacher_key is None:
        await message.answer(NEED_TEACHER.format(names=esc(", ".join(TEACHERS))), parse_mode="HTML")
        return

    usage = (
        "⚠️ Ishlatish: <code>/delteacherlesson [ism] &lt;kun&gt; &lt;dars#&gt;</code>\n"
        "Masalan: <code>/delteacherlesson umarbek 1 3</code>"
    )
    words = rest.split()
    day_idx = TEACHER_DAY_CODE_MAP.get(words[0].lower()) if words else None
    lesson = parse_id(words[1]) if len(words) > 1 else None
    if day_idx is None or lesson is None:
        await message.answer(usage, parse_mode="HTML")
        return

    cursor.execute(
        "DELETE FROM teacher_timetable WHERE teacher_key = ? AND day = ? AND lesson = ?",
        (teacher_key, day_idx, lesson),
    )
    removed = cursor.rowcount
    conn.commit()
    load_teacher_timetables()

    if not removed:
        await message.answer(f"❌ {esc(TEACHER_DAY_NAMES[day_idx])}, {lesson}-darsda dars yo'q.", parse_mode="HTML")
        return
    await message.answer(
        f"🗑 {esc(TEACHERS[teacher_key]['short'])} · {esc(TEACHER_DAY_NAMES[day_idx])}, "
        f"{lesson}-dars o'chirildi.",
        parse_mode="HTML"
    )


@router.message(Command("teacherweek"), IsOwner())
async def cmd_teacher_week(message: types.Message, command: CommandObject):
    """/teacherweek [ism]  -  shows the saved timetable of a teacher (to check your edits)."""
    teacher_key, _ = resolve_teacher(command.args)
    if teacher_key is None:
        await message.answer(NEED_TEACHER.format(names=esc(", ".join(TEACHERS))), parse_mode="HTML")
        return
    await message.answer(format_teacher_timetable(teacher_key), parse_mode="HTML")


# ======================================================================
#  INBOX: messages people write to the bot are forwarded to the owner
# ======================================================================
@router.message(Command("inbox"))
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


# This handler must stay LAST: it only receives messages that no other handler
# (commands, menu buttons) has already taken.
@router.message()
async def forward_to_owner(message: types.Message, bot: Bot):
    user = message.from_user
    if user is None or message.chat.type != "private":
        return

    register_or_update_user(user.id, user.username, user.first_name)

    if user.id == OWNER_ID:
        # Owner sends a sticker -> the bot shows its file_id (for FUNNY_STICKER_ID)
        if message.sticker:
            await message.answer(
                "🆔 <b>Sticker ID:</b>\n"
                f"<code>{esc(message.sticker.file_id)}</code>\n\n"
                "Buni hostingdagi <code>FUNNY_STICKER_ID</code> o'zgaruvchisiga qo'ying.",
                parse_mode="HTML"
            )
        return

    if get_setting("inbox", "on") != "on":
        return

    uname = f"@{esc(user.username)}" if user.username else "NoUsername"
    teacher = get_user_teacher(user.id)
    if teacher:
        tag = f"👨‍🏫 {esc(TEACHERS[teacher]['short'])}"
    else:
        class_id = get_user_class(user.id)
        tag = "🏫 " + (CLASSES[class_id]["short"] if class_id else "—")

    header = (
        f"📨 <b>{esc(user.first_name or 'NoName')}</b> ({uname}) | {tag} | ID: <code>{user.id}</code>\n"
        f"Javob: <code>/dm {user.id} </code>"
    )
    try:
        await bot.send_message(OWNER_ID, header, parse_mode="HTML")
        # copy_message works for any type: text, photo, sticker, voice, file...
        await bot.copy_message(OWNER_ID, message.chat.id, message.message_id)
    except Exception as e:
        logging.error(f"Could not forward message from {user.id} to owner: {e}")