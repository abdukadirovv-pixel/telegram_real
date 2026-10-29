"""
test.py - BSB va CHSB javoblarini test-uz.ru saytidan olish moduli
O'quv yillari (2026-2027, 2025-2026) bo'yicha ajratish,
1-savol shartini (matn/rasm) ko'rsatish va skrinshot javoblarni tartib bilan yetkazish.
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
    InputMediaPhoto,
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
CACHE_TTL = 300  # 5 daqiqa kesh


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
        clean_name = raw_name.split("/")[0].strip()
        clean_name = html.unescape(clean_name)
        if cat_id not in seen and clean_name:
            seen.add(cat_id)
            subjects.append({"id": cat_id, "name": clean_name})

    _set_cache(cache_key, subjects, ttl=1800)
    return subjects


def get_controls_by_year_sync(kind: str, cat_id: str) -> dict[str, list[dict]]:
    """
    Fan sahifasidagi testlarni o'quv yillari (2026-2027, 2025-2026 va h.k.)
    bo'yicha ajratib oladi.
    """
    cache_key = f"ctrls_yr:{kind}:{cat_id}"
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?cat={cat_id}"
    content = _fetch_html(url)
    if not content:
        return {}

    # Sahifani h2 teglari bo'yicha bo'laklarga ajratamiz (o'quv yillarini aniqlash uchun)
    parts = re.split(r"(<h2[^>]*>.*?</h2>)", content, flags=re.IGNORECASE | re.DOTALL)

    years: dict[str, list[dict]] = {}
    current_year = "2026-2027"

    for part in parts:
        h_m = re.match(r"<h2[^>]*>(.*?)</h2>", part, flags=re.IGNORECASE | re.DOTALL)
        if h_m:
            text = re.sub(r"<[^<]+?>", "", h_m.group(1)).strip()
            ym = re.search(r"(\d{4}\s*-\s*\d{4})", text)
            if ym:
                current_year = ym.group(1).replace(" ", "")
            continue

        ctrl_pattern = re.compile(
            rf'<a\s+[^>]*href=["\'](?:{page})?\?control=(\d+)["\'][^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        for m in ctrl_pattern.finditer(part):
            c_id = m.group(1)
            raw_title = re.sub(r"<[^<]+?>", "", m.group(2)).strip()
            clean_title = " ".join(raw_title.split())
            clean_title = html.unescape(clean_title)

            if current_year not in years:
                years[current_year] = []
            if not any(x["id"] == c_id for x in years[current_year]):
                years[current_year].append({"id": c_id, "title": clean_title})

    _set_cache(cache_key, years, ttl=600)
    return years


def _fetch_single_question(kind: str, q_id: str, label: str) -> dict:
    """Bitta savol sahifasidagi shart (savol matni/rasmi) va javob/yechimni tahlil qiladi."""
    page = "sor_uz.php" if kind == "bsb" else "soch_uz.php"
    url = f"{BASE_URL}/{page}?id={q_id}"
    q_page = _fetch_html(url)

    ans_text = None
    sol_imgs = []
    q_text = None
    q_imgs = []

    if q_page:
        # A. Savol shartini (Question) ajratib olish
        q_block = re.search(
            r"<h2>Question</h2>(.*?)(?:<h2>|<h2>Solution</h2>|<h2>Answer</h2>|<footer)",
            q_page,
            re.DOTALL | re.IGNORECASE,
        )
        if q_block:
            block_html = q_block.group(1).strip()
            # Savol sharti rasmlari
            for img in re.findall(r'<img[^>]*src=["\']([^"\']+)["\']', block_html, re.IGNORECASE):
                if not any(x in img for x in ["search", "telegram", "yandex", "logo", "organish", "sol"]):
                    full_url = urljoin(BASE_URL, img)
                    if full_url not in q_imgs:
                        q_imgs.append(full_url)

            # Savol sharti matni
            cleaned_html = re.sub(r"<script.*?</script>", "", block_html, flags=re.DOTALL | re.IGNORECASE)
            cleaned_html = re.sub(r"<style.*?</style>", "", cleaned_html, flags=re.DOTALL | re.IGNORECASE)
            cleaned_html = re.sub(r"\(window\.adsbygoogle.*?\);", "", cleaned_html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r"<[^<]+?>", " ", cleaned_html)
            clean_text = html.unescape(" ".join(clean_text.split()))
            if clean_text and len(clean_text) >= 3 and "window.adsbygoogle" not in clean_text:
                q_text = clean_text

        # B. Javob / Yechimni (Answer/Solution) ajratib olish
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
        "label": label,
        "q_text": q_text,
        "q_images": q_imgs,
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
        return {"has_answers": False, "questions": [], "url": url, "title": ""}

    # Sarlavhani olish
    m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL)
    ctrl_title = ""
    if m_h1:
        ctrl_title = " ".join(re.sub(r"<[^<]+?>", "", m_h1.group(1)).split())
        ctrl_title = html.unescape(ctrl_title)

    # Barcha savollar ID va nomlarini aniqlash
    pattern = re.compile(
        rf'<a\s+[^>]*href=["\'](?:{page})?\?id=(\d+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    raw_matches = pattern.findall(content)
    if not raw_matches:
        return {"has_answers": False, "questions": [], "url": url, "title": ctrl_title}

    q_items = []
    for i, (q_id, raw_link_text) in enumerate(raw_matches, start=1):
        cleaned = re.sub(r"<[^<]+?>", " ", raw_link_text)
        cleaned = html.unescape(cleaned)
        cleaned = cleaned.replace("→", "").replace("Question only; solution not yet available", "").strip()
        cleaned = " ".join(cleaned.split())
        label = re.sub(r"^Question\s*", "", cleaned, flags=re.IGNORECASE)
        label = f"{label}-savol" if label.isdigit() else label
        if not label:
            label = f"{i}-savol"
        q_items.append((q_id, label))

    with ThreadPoolExecutor(max_workers=8) as executor:
        questions = list(executor.map(lambda item: _fetch_single_question(kind, item[0], item[1]), q_items))

    has_any = any(q["has_answer"] for q in questions)
    res = {
        "has_answers": has_any,
        "questions": questions,
        "url": url,
        "title": ctrl_title,
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


def kb_years(kind: str, grade: int, cat_id: str, years: list[str]) -> InlineKeyboardMarkup:
    """O'quv yillarini tanlash klaviaturasi."""
    rows = []
    for yr in years:
        label = f"🎓 {yr} o'quv yili (Joriy)" if "2026" in yr else f"📚 {yr} o'quv yili"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"ans:yr:{kind}:{grade}:{cat_id}:{yr}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Fanlar ro'yxati", callback_data=f"ans:gr:{kind}:{grade}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_controls(kind: str, grade: int, cat_id: str, year: str, controls: list[dict]) -> InlineKeyboardMarkup:
    """Tanlangan o'quv yilidagi testlar ro'yxati klaviaturasi."""
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
        InlineKeyboardButton(text="⬅️ Boshqa o'quv yili", callback_data=f"ans:sub:{kind}:{grade}:{cat_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_answers_menu(kind: str, grade: int, cat_id: str, ctrl_id: str, questions: list[dict]) -> InlineKeyboardMarkup:
    """Javoblar va skrinshotlarni tartib bilan ko'rish klaviaturasi."""
    rows = []

    # 1. TESTNI TEKSHIRISH UCHUN 1-SAVOL SHARTINI KO'RISH TUGMASI
    q1 = questions[0] if questions else None
    if q1 and (q1.get("q_images") or q1.get("q_text")):
        rows.append([
            InlineKeyboardButton(
                text="🔍 1-savol shartini ko'rish (Testni tekshirish) 🖼",
                callback_data=f"ans:qcond:{kind}:{ctrl_id}",
            )
        ])

    # 2. Agar savollar yozma/skrinshot bo'lsa, har bir savol uchun yechim tugmasi
    image_questions = [q for q in questions if q["images"]]
    if image_questions:
        q_buttons = []
        for i, q in enumerate(questions):
            if q["images"]:
                lbl = q["label"]
                if len(lbl) > 12:
                    lbl = f"{i+1}-savol"
                q_buttons.append(
                    InlineKeyboardButton(
                        text=f"{lbl} 🖼",
                        callback_data=f"ans:q:{kind}:{ctrl_id}:{i}",
                    )
                )
        for j in range(0, len(q_buttons), 3):
            rows.append(q_buttons[j:j + 3])

        # Barcha yechim rasmlarini albom qilib yuklash
        rows.append([
            InlineKeyboardButton(
                text="📥 Barcha yechimlarni albomda yuklash",
                callback_data=f"ans:all:{kind}:{ctrl_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️ Variantlar ro'yxati",
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
    """Fan tanlanganda uning testlarini o'quv yillari bo'yicha ajratadi."""
    _, _, kind, grade_str, cat_id = call.data.split(":")
    grade = int(grade_str)

    await call.message.edit_text("⏳ <i>O'quv yillari va testlar yuklanmoqda...</i>", parse_mode="HTML")

    years_dict = await asyncio.to_thread(get_controls_by_year_sync, kind, cat_id)

    if not years_dict:
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
    years_list = sorted(list(years_dict.keys()), reverse=True)

    if len(years_list) > 1:
        await call.message.edit_text(
            f"📅 <b>{grade}-sinf {title} — O'quv yilini tanlang:</b>\n"
            "Kerakli o'quv yili variantlarini ko'rish uchun tanlang:",
            parse_mode="HTML",
            reply_markup=kb_years(kind, grade, cat_id, years_list),
        )
    else:
        sole_year = years_list[0]
        controls = years_dict[sole_year]
        await call.message.edit_text(
            f"📝 <b>{grade}-sinf {title} ({sole_year}) — Variantni tanlang:</b>",
            parse_mode="HTML",
            reply_markup=kb_controls(kind, grade, cat_id, sole_year, controls),
        )
    await call.answer()


@router.callback_query(F.data.startswith("ans:yr:"))
async def cb_ans_year(call: types.CallbackQuery):
    """Foydalanuvchi ma'lum bir o'quv yilini tanlaganda uning testlarini ko'rsatadi."""
    _, _, kind, grade_str, cat_id, year = call.data.split(":")
    grade = int(grade_str)

    years_dict = await asyncio.to_thread(get_controls_by_year_sync, kind, cat_id)
    controls = years_dict.get(year, [])

    if not controls:
        await call.message.edit_text(
            f"❌ <b>Hozircha bunday javoblar mavjud emas</b>\n\n"
            f"{year} o'quv yili uchun testlar topilmadi.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ Boshqa o'quv yili", callback_data=f"ans:sub:{kind}:{grade}:{cat_id}")
                ]]
            ),
        )
        await call.answer()
        return

    title = "BSB" if kind == "bsb" else "CHSB"
    await call.message.edit_text(
        f"📝 <b>{grade}-sinf {title} ({year}) — Variantni tanlang:</b>",
        parse_mode="HTML",
        reply_markup=kb_controls(kind, grade, cat_id, year, controls),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:ctrl:"))
async def cb_ans_control(call: types.CallbackQuery):
    """Tanlangan test javoblarini formatlab ko'rsatadi."""
    _, _, kind, grade_str, cat_id, ctrl_id = call.data.split(":")
    grade = int(grade_str)

    await call.message.edit_text("⏳ <i>Javoblar va yechimlar tekshirilmoqda...</i>", parse_mode="HTML")

    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)

    if not ans_data["has_answers"] or not ans_data["questions"]:
        await call.message.edit_text(
            "❌ <b>Hozircha bunday javoblar mavjud emas</b>\n\n"
            "Ushbu test bo'yicha saytda hali yechimlar yoki javoblar yuklanmagan (faqat savollar mavjud bo'lishi mumkin).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="⬅️ Boshqa variant", callback_data=f"ans:sub:{kind}:{grade}:{cat_id}")
                ]]
            ),
        )
        await call.answer()
        return

    questions = ans_data["questions"]
    answered_count = sum(1 for q in questions if q["has_answer"])
    has_images = any(len(q["images"]) > 0 for q in questions)
    title_kind = "BSB" if kind == "bsb" else "CHSB"

    ctrl_name = ans_data.get("title") or f"{grade}-sinf {title_kind}"
    msg_lines = [
        f"✅ <b>{html.escape(ctrl_name)} javoblari</b>",
        f"📊 Jami {len(questions)} ta topshiriqdan {answered_count} tasining javobi mavjud.\n",
    ]

    # --- 1-SAVOL SHARTINI KO'RSATISH (TESTNI SOLISHTIRIB ISHONCH HOSIL QILISH UCHUN) ---
    q1 = questions[0]
    if q1.get("q_text"):
        preview_text = q1["q_text"]
        if len(preview_text) > 180:
            preview_text = preview_text[:177] + "..."
        msg_lines.append(f"🔍 <b>1-savol sharti (Testni tekshirish uchun):</b>\n<i>«{html.escape(preview_text)}»</i>\n")
    elif q1.get("q_images"):
        msg_lines.append("🔍 <b>1-savol sharti:</b> Rasm shaklida berilgan. Qo'lingizdagi test bilan solishtirish uchun quyidagi tugmani bosing.\n")

    # --- JAVOBLAR RO'YXATI ---
    if all(q["text"] and len(q["text"]) <= 4 for q in questions if q["has_answer"]) and not has_images:
        cols = []
        for q in questions:
            val = q["text"] if q["text"] else "—"
            cols.append(f"<b>{q['label']}:</b> {val}")
        for i in range(0, len(cols), 2):
            msg_lines.append("        ".join(cols[i:i + 2]))
    elif has_images:
        msg_lines.append(
            "📌 <b>Ushbu test yozma topshiriqlardan iborat bo'lib, javoblari skrinshot (rasm) shaklida.</b>\n"
            "Har bir savol yechimini ko'rish uchun quyidagi tugmalardan foydalaning:"
        )
    else:
        for q in questions[:15]:
            ans_val = q["text"] if q["text"] else "<i>Javob yo'q</i>"
            msg_lines.append(f"<b>{q['label']}:</b> {ans_val}")

    msg_lines.append(f"\n🔗 <a href='{ans_data['url']}'>test-uz.ru saytida ochish</a>")

    text = "\n".join(msg_lines)
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb_answers_menu(kind, grade, cat_id, ctrl_id, questions),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ans:qcond:"))
async def cb_ans_question_condition(call: types.CallbackQuery, bot: Bot):
    """1-savol shartini (matn yoki rasm) foydalanuvchiga yuboradi (testni tekshirish uchun)."""
    _, _, kind, ctrl_id = call.data.split(":")
    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)
    questions = ans_data.get("questions", [])

    if not questions:
        await call.answer("Savol topilmadi.", show_alert=True)
        return

    q1 = questions[0]
    await call.answer("1-savol sharti yuborilmoqda...")

    caption = "🔍 <b>1-savol sharti (Testni solishtirish uchun):</b>\nUshbu savol orqali qo'lingizdagi test aynan shu variant ekanligiga ishonch hosil qilishingiz mumkin."

    if q1.get("q_images"):
        for num, img_url in enumerate(q1["q_images"], start=1):
            c_text = caption + (f" ({num}/{len(q1['q_images'])})" if len(q1["q_images"]) > 1 else "")
            img_bytes = await asyncio.to_thread(_fetch_bytes, img_url)
            if img_bytes:
                file = BufferedInputFile(img_bytes, filename=f"1_savol_sharti_{num}.webp")
                await bot.send_photo(chat_id=call.message.chat.id, photo=file, caption=c_text, parse_mode="HTML")
            else:
                await bot.send_photo(chat_id=call.message.chat.id, photo=img_url, caption=c_text, parse_mode="HTML")
    elif q1.get("q_text"):
        await call.message.answer(
            f"🔍 <b>1-savol sharti (Testni solishtirish uchun):</b>\n\n<i>«{html.escape(q1['q_text'])}»</i>",
            parse_mode="HTML",
        )
    else:
        await call.answer("1-savol sharti topilmadi.", show_alert=True)


@router.callback_query(F.data.startswith("ans:q:"))
async def cb_ans_question_image(call: types.CallbackQuery, bot: Bot):
    """Foydalanuvchi bitta savolning yechim skrinshotini so'raganda yuboradi."""
    _, _, kind, ctrl_id, q_idx_str = call.data.split(":")
    q_idx = int(q_idx_str)

    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)
    questions = ans_data.get("questions", [])

    if q_idx >= len(questions) or not questions[q_idx]["images"]:
        await call.answer("Bu savol uchun rasm yechim mavjud emas.", show_alert=True)
        return

    q = questions[q_idx]
    await call.answer(f"{q['label']} yechimi yuborilmoqda...")

    for num, img_url in enumerate(q["images"], start=1):
        caption_part = f" ({num}/{len(q['images'])})" if len(q["images"]) > 1 else ""
        caption = f"📝 <b>{q['label']} yechimi{caption_part}</b>"

        img_bytes = await asyncio.to_thread(_fetch_bytes, img_url)
        if img_bytes:
            file = BufferedInputFile(img_bytes, filename=f"savol_{q_idx+1}_yechim_{num}.webp")
            await bot.send_photo(chat_id=call.message.chat.id, photo=file, caption=caption, parse_mode="HTML")
        else:
            await bot.send_photo(chat_id=call.message.chat.id, photo=img_url, caption=caption, parse_mode="HTML")


@router.callback_query(F.data.startswith("ans:all:"))
async def cb_ans_all_images(call: types.CallbackQuery, bot: Bot):
    """
    Barcha mavjud yechim rasmlarini aniq tartibda (1-savol, 2-savol...)
    albom (MediaGroup) qilib yuboradi.
    """
    _, _, kind, ctrl_id = call.data.split(":")
    await call.answer("Barcha rasmlar tartib bilan tayyorlanmoqda...", show_alert=False)

    ans_data = await asyncio.to_thread(get_control_answers_sync, kind, ctrl_id)
    questions = ans_data.get("questions", [])

    ordered_items = []
    for q in questions:
        for num, img_url in enumerate(q["images"], start=1):
            caption_part = f" ({num}/{len(q['images'])})" if len(q["images"]) > 1 else ""
            ordered_items.append({
                "label": f"📝 {q['label']} yechimi{caption_part}",
                "url": img_url,
            })

    if not ordered_items:
        await call.message.answer("❌ Bu testda yechim rasmlari topilmadi.")
        return

    # Telegram send_media_group 10 tagacha rasm qabul qiladi
    for batch_idx in range(0, len(ordered_items), 10):
        batch = ordered_items[batch_idx:batch_idx + 10]
        media_group = []

        for item in batch:
            img_bytes = await asyncio.to_thread(_fetch_bytes, item["url"])
            if img_bytes:
                media_group.append(
                    InputMediaPhoto(
                        media=BufferedInputFile(img_bytes, filename="yechim.webp"),
                        caption=item["label"],
                    )
                )

        if media_group:
            try:
                await bot.send_media_group(chat_id=call.message.chat.id, media=media_group)
            except Exception as e:
                logging.error(f"[test.py] send_media_group xatosi: {e}")
                for m in media_group:
                    await bot.send_photo(chat_id=call.message.chat.id, photo=m.media, caption=m.caption)
