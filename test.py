"""
test.py - BSB va CHSB javoblarini test-uz.ru saytidan olish moduli
"""
import re
import html
import time
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
import urllib.request

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
)

router = Router()

BASE_URL = "https://www.test-uz.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------
# CACHE (Tezlikni oshirish va saytni yuklamaslik uchun)
# ---------------------------------------------------------
_CACHE = {}
CACHE_TTL = 600  # 10 daqiqa


def _get_cache(key: str):
    if key in _CACHE:
        val, expire = _CACHE[key]
        if time.time() < expire:
            return val
        del _CACHE[key]
    return None


def _set_cache(key: str, val, ttl: int = CACHE_TTL):
    _CACHE[key] = (val, time.time() + ttl)


# ---------------------------------------------------------
# WEB SCRAPER LOGIC (test-uz.ru)
# ---------------------------------------------------------
def _fetch_html(url: str) -> str:
    """test-uz.ru saytidan sahifani yuklab oladi va to'g'ri kodirovkada qaytaradi."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
            try:
                return data.decode("windows-1251")
            except UnicodeDecodeError:
                return data.decode("utf-8", errors="ignore")
    except Exception as e:
        logging.error(f"[test.py] fetch_html error for {url}: {e}")
        return ""


def _fetch_bytes(url: str) -> bytes | None:
    """Rasm baytlarini yuklab oladi."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception as e:
        logging.error(f"[test.py] fetch_bytes error for {url}: {e}")
        return None


def get_subjects_sync(kind: str, grade: int) -> list[dict]:
    """Sinf bo'yicha mavjud fanlarni oladi."""
    cache_key = f"subs:{kind}:{grade}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?klass={grade}"
    content = _fetch_html(url)
    if not content:
        return []

    pattern = re.compile(
        rf'<a\s+[^>]*href=["\'](?:{page})?\?cat=(\d+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    subjects = []
    seen = set()
    for m in pattern.finditer(content):
        cat_id = m.group(1)
        raw_name = re.sub(r"<[^<]+?>", "", m.group(2)).strip()
        # "Fizika / Physics" -> "Fizika"
        clean_name = raw_name.split("/")[0].strip()
        clean_name = html.unescape(clean_name)
        if cat_id not in seen and clean_name:
            seen.add(cat_id)
            subjects.append({"id": cat_id, "name": clean_name})

    _set_cache(cache_key, subjects, ttl=1800)
    return subjects


def get_controls_sync(kind: str, cat_id: str) -> list[dict]:
    """Fan bo'yicha barcha nazorat ishlari / variantlar ro'yxatini oladi."""
    cache_key = f"ctrls:{kind}:{cat_id}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?cat={cat_id}"
    content = _fetch_html(url)
    if not content:
        return []

    pattern = re.compile(
        rf'<a\s+[^>]*href=["\'](?:{page})?\?control=(\d+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    controls = []
    seen = set()
    for m in pattern.finditer(content):
        ctrl_id = m.group(1)
        raw_title = re.sub(r"<[^<]+?>", "", m.group(2)).strip()
        clean_title = " ".join(raw_title.split())
        clean_title = html.unescape(clean_title)
        if ctrl_id not in seen and clean_title:
            seen.add(ctrl_id)
            controls.append({"id": ctrl_id, "title": clean_title})

    _set_cache(cache_key, controls, ttl=600)
    return controls


def _fetch_single_question(kind: str, q_id: str) -> dict:
    """Bitta savol sahifasidagi yechim yoki javobni tahlil qiladi."""
    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?id={q_id}"
    q_page = _fetch_html(url)

    ans_text = None
    sol_imgs = []

    if q_page:
        # 1. Test varianti javobi (Masalan: <h2>Answer</h2><p>C</p>)
        ans_m = re.search(r"<h2>Answer</h2>\s*<p>(.*?)</p>", q_page, re.IGNORECASE | re.DOTALL)
        if ans_m:
            cand = re.sub(r"<[^<]+?>", "", ans_m.group(1)).strip()
            if cand:
                ans_text = html.unescape(cand)

        # 2. Rasm yechimlari (soch_sol yoki sor_sol)
        for img in re.findall(r'<img[^>]*src=["\']([^"\']*sol[^"\']*)["\']', q_page, re.IGNORECASE):
            full_img_url = urljoin(BASE_URL, img)
            if full_img_url not in sol_imgs:
                sol_imgs.append(full_img_url)

        # 3. Yechim blokidagi boshqa rasmlar
        if not sol_imgs and not ans_text:
            sol_block = re.search(
                r"<h2>Solution</h2>(.*?)(?:<h2>|<div class=[\"']ad-wrapper|<footer)",
                q_page,
                re.DOTALL | re.IGNORECASE,
            )
            if sol_block:
                for img in re.findall(r'<img[^>]*src=["\']([^"\']+)["\']', sol_block.group(1), re.IGNORECASE):
                    if not any(x in img for x in ["search", "telegram", "yandex", "logo", "organish"]):
                        full_img_url = urljoin(BASE_URL, img)
                        if full_img_url not in sol_imgs:
                            sol_imgs.append(full_img_url)

                if not sol_imgs:
                    cand_text = re.sub(r"<[^<]+?>", " ", sol_block.group(1)).strip()
                    if cand_text and len(cand_text) > 1 and "adsbygoogle" not in cand_text:
                        ans_text = cand_text

    is_answered = bool(ans_text or sol_imgs)
    return {
        "id": q_id,
        "text": ans_text,
        "images": sol_imgs,
        "has_answer": is_answered,
    }


def get_control_answers_sync(kind: str, ctrl_id: str) -> dict:
    """Berilgan nazorat ishidagi barcha savollar va ularning javoblarini oladi."""
    cache_key = f"ans:{kind}:{ctrl_id}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?control={ctrl_id}"
    content = _fetch_html(url)
    if not content:
        return {"has_answers": False, "questions": [], "url": url}

    # Barcha savollar ID sini aniqlash
    pattern = re.compile(
        rf'<a\s+[^>]*href=["\'](?:{page})?\?id=(\d+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    q_matches = pattern.findall(content)
    if not q_matches:
        return {"has_answers": False, "questions": [], "url": url}

    # Savollarni tezkor yuklash uchun oqimlar (ThreadPoolExecutor)
    with ThreadPoolExecutor(max_workers=8) as executor:
        questions = list(executor.map(lambda item: _fetch_single_question(kind, item[0]), q_matches))

    has_any = any(q["has_answer"] for q in questions)
    res = {
        "has_answers": has_any,
        "questions": questions,
        "url": url,
    }
    _set_cache(cache_key, res, ttl=600)
    return res


# ---------------------------------------------------------
# KEYBOARDS
# ---------------------------------------------------------
def kb_type_selection() -> InlineKeyboardMarkup:
    """BSB yoki CHSB tanlash klaviaturasi."""
    rows = [
        [
            InlineKeyboardButton(text="📝 BSB javoblari", callback_data="ans:type:bsb"),
            InlineKeyboardButton(text="📑 CHSB javoblari", callback_data="ans:type:chsb"),
        ],
        [InlineKeyboardButton(text="❌ Yopish", callback_data="ans:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_grade_selection(kind: str) -> InlineKeyboardMarkup:
    """Sinf tanlash klaviaturasi (5-11 sinflar)."""
    rows = [
        [
            InlineKeyboardButton(text="5-sinf", callback_data=f"ans:gr:{kind}:5"),
            InlineKeyboardButton(text="6-sinf", callback_data=f"ans:gr:{kind}:6"),
            InlineKeyboardButton(text="7-sinf", callback_data=f"ans:gr:{kind}:7"),
        ],
        [
            InlineKeyboardButton(text="8-sinf", callback_data=f"ans:gr:{kind}:8"),
            InlineKeyboardButton(text="9-sinf", callback_data=f"ans:gr:{kind}:9"),
            InlineKeyboardButton(text="10-sinf", callback_data=f"ans:gr:{kind}:10"),
        ],
        [
            InlineKeyboardButton(text="11-sinf", callback_data=f"ans:gr:{kind}:11"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="ans:root"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_subjects(kind: str, grade: int, subjects: list[dict]) -> InlineKeyboardMarkup:
    """Fanlar ro'yxati klaviaturasi."""
    rows = []
    # 2 tadan tugma bir qatorda
    for i in range(0, len(subjects), 2):
        row = []
        for s in subjects[i:i + 2]:
            row.append(
                InlineKeyboardButton(
                    text=s["name"],
                    callback_data=f"ans:sub:{kind}:{grade}:{s['id']}",
                )
            )
        rows.append(row)

    rows.append([InlineKeyboardButton(text="⬅️ Sinfni almashtirish", callback_data=f"ans:type:{kind}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_controls(kind: str, grade: int, cat_id: str, controls: list[dict]) -> InlineKeyboardMarkup:
    """Testlar / choraklar ro'yxati klaviaturasi."""
    rows = []
    for c in controls:
        title = c["title"]
        if len(title) > 38:
            title = title[:35] + "..."
        rows.append([
            InlineKeyboardButton(
                text=f"📄 {title}",
                callback_data=f"ans:ctrl:{kind}:{grade}:{cat_id}:{c['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Boshqa fan", callback_data=f"ans:gr:{kind}:{grade}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_answers_menu(kind: str, grade: int, cat_id: str, ctrl_id: str, has_images: bool) -> InlineKeyboardMarkup:
    """Javoblar ko'rsatilgandan keyingi navigatsiya."""
    rows = []
    if has_images:
        rows.append([
            InlineKeyboardButton(
                text="📥 Barcha yechim rasmlarini olish",
                callback_data=f"ans:all:{kind}:{ctrl_id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Boshqa variant",
            callback_data=f"ans:sub:{kind}:{grade}:{cat_id}",
        ),
        InlineKeyboardButton(
            text="📚 Fanlar",
            callback_data=f"ans:gr:{kind}:{grade}",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------
# BOT HANDLERS
# ---------------------------------------------------------
@router.message(Command("javoblar", "answers", "test"))
@router.message(F.text.casefold().in_(["javoblar", "📝 javoblar", "javob", "chsb", "bsb"]))
async def cmd_javoblar(message: types.Message):
    """Javoblar bo'limi bosh menyusi."""
    await message.answer(
        "📚 <b>BSB va CHSB javoblari</b> (test-uz.ru bazasi)\n\n"
        "Kerakli bo'limni tanlang:",
        parse_mode="HTML",
        reply_markup=kb_type_selection(),
    )


@router.callback_query(F.data == "ans:root")
async def cb_ans_root(call: types.CallbackQuery):
    await call.message.edit_text(
        "📚 <b>BSB va CHSB javoblari</b> (test-uz.ru bazasi)\n\n"
        "Kerakli bo'limni tanlang:",
        parse_mode="HTML",
        reply_markup=kb_type_selection(),
    )
    await call.answer()


@router.callback_query(F.data == "ans:close")
async def cb_ans_close(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("ans:type:"))
async def cb_ans_type(call: types.CallbackQuery):
    kind = call.data.split(":")[2]  # bsb yoki chsb
    title = "BSB (СОР)" if kind == "bsb" else "CHSB (СОЧ)"
    await call.message.edit_text(
        f"🏫 <b>{title} uchun sinfni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=kb_grade_selection(kind),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:gr:"))
async def cb_ans_grade(call: types.CallbackQuery):
    _, _, kind, grade_str = call.data.split(":")
    grade = int(grade_str)

    await call.message.edit_text("⏳ <i>Fanlar ro'yxati yuklanmoqda...</i>", parse_mode="HTML")

    subjects = await asyncio.to_thread(get_subjects_sync, kind, grade)
    title = "BSB" if kind == "bsb" else "CHSB"

    if not subjects:
        await call.message.edit_text(
            f"❌ <b>Hozircha bunday javoblar mavjud emas</b>\n\n"
            f"{grade}-sinf uchun {title} fanlari topilmadi.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"ans:type:{kind}")]]
            ),
        )
        await call.answer()
        return

    await call.message.edit_text(
        f"📖 <b>{grade}-sinf {title} — Fanni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=kb_subjects(kind, grade, subjects),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:sub:"))
async def cb_ans_subject(call: types.CallbackQuery):
    _, _, kind, grade_str, cat_id = call.data.split(":")
    grade = int(grade_str)

    await call.message.edit_text("⏳ <i>Mavjud testlar tekshirilmoqda...</i>", parse_mode="HTML")

    controls = await asyncio.to_thread(get_controls_sync, kind, cat_id)

    # Agar testlar ro'yxati umuman bo'sh bo'lsa
    if not controls:
        await call.message.edit_text(
            "❌ <b>Hozircha bunday javoblar mavjud emas</b>\n\n"
            "Ushbu fan bo'yicha saytda hali testlar yoki javoblar e'lon qilinmagan.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ Boshqa fan tanlash", callback_data=f"ans:gr:{kind}:{grade}")
                ]]
            ),
        )
        await call.answer()
        return

    title = "BSB" if kind == "bsb" else "CHSB"
    await call.message.edit_text(
        f"📝 <b>{grade}-sinf {title} — Kerakli variant yoki chorakni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=kb_controls(kind, grade, cat_id, controls),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:ctrl:"))
async def cb_ans_control(call: types.CallbackQuery):
    _, _, kind, grade_str, cat_id, ctrl_id = call.data.split(":")
    grade = int(grade_str)

    await call.message.edit_text("⏳ <i>Javoblar qidirilmoqda va yuklanmoqda...</i>", parse_mode="HTML")

    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)

    # Agar javoblar hali chiqmagan bo'lsa
    if not ans_data["has_answers"] or not ans_data["questions"]:
        await call.message.edit_text(
            "❌ <b>Hozircha bunday javoblar mavjud emas</b>\n\n"
            "Ushbu test bo'yicha saytda faqat savollar e'lon qilingan yoki javoblar hali yuklanmagan.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ Boshqa variant", callback_data=f"ans:sub:{kind}:{grade}:{cat_id}")
                ]]
            ),
        )
        await call.answer()
        return

    # Javoblarni formatlash
    questions = ans_data["questions"]
    answered_count = sum(1 for q in questions if q["has_answer"])
    has_images = any(len(q["images"]) > 0 for q in questions)

    # Matnli javoblarni yig'ish (A, B, C...)
    text_answers = []
    for i, q in enumerate(questions, start=1):
        if q["text"]:
            text_answers.append(f"<b>{i}-savol:</b> {q['text']}")
        elif q["images"]:
            text_answers.append(f"<b>{i}-savol:</b> 📷 Rasm yechim mavjud")
        else:
            text_answers.append(f"<b>{i}-savol:</b> <i>Javob yo'q</i>")

    title_kind = "BSB" if kind == "bsb" else "CHSB"
    msg_lines = [
        f"✅ <b>{grade}-sinf {title_kind} javoblari</b>\n",
        f"📊 Jami {len(questions)} ta savoldan {answered_count} tasining javobi topildi.\n",
    ]

    # Javoblarni 2 ustunda yoki qatorda chiqarish
    if all(q["text"] and len(q["text"]) <= 3 for q in questions if q["has_answer"]):
        # Test savollari (A, B, C...)
        cols = []
        for i, q in enumerate(questions, start=1):
            val = q["text"] if q["text"] else "—"
            cols.append(f"<b>{i}.</b> {val}")
        # Chiroyli 2 ustun qilib ko'rsatish
        for i in range(0, len(cols), 2):
            part = cols[i:i + 2]
            msg_lines.append("        ".join(part))
    else:
        # Kengroq javoblar yoki aralash
        msg_lines.extend(text_answers[:25])

    msg_lines.append(f"\n🔗 <a href='{ans_data['url']}'>Saytda ko'rish (test-uz.ru)</a>")

    text = "\n".join(msg_lines)
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb_answers_menu(kind, grade, cat_id, ctrl_id, has_images),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:all:"))
async def cb_ans_all_images(call: types.CallbackQuery, bot: Bot):
    """Barcha mavjud yechim rasmlarini foydalanuvchiga yuboradi."""
    _, _, kind, ctrl_id = call.data.split(":")
    await call.answer("Rasmlar yuklanmoqda...", show_alert=False)

    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)
    questions = ans_data.get("questions", [])

    sent_any = False
    for i, q in enumerate(questions, start=1):
        for img_url in q["images"]:
            try:
                # Rasmni to'g'ridan-to'g'ri yoki bayt qilib yuborish
                img_bytes = await asyncio.to_thread(_fetch_bytes, img_url)
                if img_bytes:
                    file = BufferedInputFile(img_bytes, filename=f"savol_{i}_yechim.webp")
                    await bot.send_photo(
                        chat_id=call.message.chat.id,
                        photo=file,
                        caption=f"📝 <b>{i}-savol yechimi</b>",
                        parse_mode="HTML",
                    )
                    sent_any = True
                else:
                    await bot.send_photo(
                        chat_id=call.message.chat.id,
                        photo=img_url,
                        caption=f"📝 <b>{i}-savol yechimi</b>",
                        parse_mode="HTML",
                    )
                    sent_any = True
            except Exception as e:
                logging.error(f"[test.py] Rasmni yuborishda xatolik: {e}")

    if not sent_any:
        await call.message.answer("❌ Bu testda yechim rasmlari topilmadi.")
