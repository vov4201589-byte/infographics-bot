"""
Telegram Infographics Bot — Python Service
Запуск: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os, json, asyncio, base64, io, logging
from typing import Optional

import aiohttp
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import Image

# ─── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "")
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379")
SESSION_TTL = 3600  # секунды

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = FastAPI()
openai = AsyncOpenAI(api_key=OPENAI_KEY)
redis  = aioredis.from_url(REDIS_URL, decode_responses=True)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE= f"https://api.telegram.org/file/bot{BOT_TOKEN}"


# ═══════════════════════════════════════════════════════════════════════════════
# ПРОМПТ АРТ-ДИРЕКТОРА ДЛЯ АНАЛИЗА ТОВАРА
# ═══════════════════════════════════════════════════════════════════════════════

ART_DIRECTOR_PROMPT = """
Ты — арт-директор маркетплейсов и генератор ТЗ для инфографики. 
Я загружаю изображение товара. Твоя задача — по этой картинке сделать готовое ТЗ/промпт для генерации инфографики.

1) АНАЛИЗ ИЗОБРАЖЕНИЯ
- Определи: что за товар (категория + подвид), комплектацию (что входит), варианты/цвет (если видно).
- Сними с изображения весь текст (OCR) и разложи по смыслу: характеристики, преимущества, гарантия, комплектация, ограничения.
- Выяви структуру: где расположен товар, какой фон/цвета/стиль.

2) КОНТЕНТ ИНФОГРАФИКИ
- Составь заголовок (до 3–5 слов).
- Выбери 4–6 ключевых УТП из того, что реально есть на картинке (ничего не выдумывать).
- Для каждого УТП сделай:
  * короткую строку (1–4 слова),
  * уточнение (до 8–12 слов),
  * подходящую иконку (описанием: "термометр", "капли воды", "лезвия", "Bluetooth", "батарея" и т.п.).
- Если есть бейджи (гарантия, сертификация) — оформи как бейджи и укажи текст.

3) СТИЛЬ НА ОСНОВЕ КАРТИНКИ
Зафиксируй:
- фон (описание + 2–3 варианта),
- палитру (5–7 HEX цветов),
- шрифтовые пары (геометрический гротеск для заголовка + простой гротеск для текста),
- эффекты (свечение, металл, стекло, градиенты, тени),
- общий тон (премиум/техно/зима/эко и т.д.).

4) ВАЖНЫЕ ОГРАНИЧЕНИЯ
- Не придумывай характеристики, цифры, гарантию, материалы — только то, что видно на изображении или в тексте на нём.
- Если данных не хватает — предложи безопасные нейтральные формулировки ("подходит для…", "удобный дизайн") без цифр.
- Не используй упоминания игр/брендов/сертификатов, если их нет на картинке.
- Все тексты на русском, кроме общепринятых обозначений (DPI, mAh, RGB, Bluetooth, Type‑C и т.п.).

Верни результат в формате JSON:
{
  "product_name": "название товара 3-5 слов",
  "category": "категория товара",
  "headline": "заголовок для инфографики 3-5 слов",
  "utp_list": [
    {
      "short": "короткое УТП 1-4 слова",
      "detail": "уточнение до 8-12 слов",
      "icon": "описание иконки"
    }
  ],
  "badges": ["текст бейджа 1", "текст бейджа 2"],
  "style": {
    "background": "описание фона",
    "background_variants": ["вариант 1", "вариант 2", "вариант 3"],
    "palette": ["#hex1", "#hex2", "#hex3", "#hex4", "#hex5"],
    "tone": "общий тон (премиум/техно/эко и т.д.)",
    "effects": "описание эффектов"
  },
  "text_zone": "top-left или top-right или bottom-left или bottom-right"
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# ПРОМПТ ДЛЯ ГЕНЕРАЦИИ ИНФОГРАФИКИ
# ═══════════════════════════════════════════════════════════════════════════════

INFOGRAPHIC_PROMPT_TEMPLATE = """
Create a professional marketplace infographic card for {MP_NAME}.
Canvas size: {W}x{H}px. Safe margins: {MARGIN}px on all sides.

PRODUCT:
- Product cutout is provided as input image — keep it exactly as-is, centered on canvas.
- DO NOT redraw, modify, distort or replace the product.
- Product should occupy 40-60% of the canvas area.

LAYOUT for {MP_NAME} ({W}x{H}):
- Text zone: {TEXT_ZONE} corner
- Product position: opposite to text zone, occupying main visual space
- Badge "{BADGE}" in contrasting corner (small, circular or shield shape)
- 3-4 bullet points with small icons in the text zone
- Leave 40px safe zone from all edges

HEADLINE (Russian):
{HEADLINE}

УТП / BULLET POINTS (Russian, with icons):
{UTP_FORMATTED}

DESIGN STYLE:
- Background: {BACKGROUND}
- Color palette: {PALETTE}
- Tone: {TONE}
- Effects: {EFFECTS}
- Typography: Bold geometric sans-serif for headline, clean sans-serif for body text
- All text in Russian language
- High contrast, readable text
- Professional marketplace aesthetic

STRICT REQUIREMENTS:
- All text strictly inside canvas, within safe margins
- No watermarks, no English text (except technical terms like RGB, USB, mAh)
- No invented specifications or fake certifications
- Clean commercial design
- Product must remain exactly as provided, no modifications
"""


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    """Принимает payload от n8n, отвечает 200 OK мгновенно, логику — в фоне."""
    try:
        payload = await request.json()
        print("=== INCOMING REQUEST ===")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        
        bot_token = request.headers.get("X-Bot-Token", BOT_TOKEN)
        if not payload.get("skip"):
            bg.add_task(handle_update, payload, bot_token)
    except Exception as e:
        log.error(f"webhook parse error: {e}")
    return JSONResponse({"ok": True})


@app.get("/health")
async def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_update(payload: dict, token: str):
    chat_id = payload.get("chatId")
    if not chat_id:
        return

    try:
        sess = await load_session(chat_id)
        await dispatch(payload, sess, token, chat_id)
    except Exception as e:
        log.error(f"[{chat_id}] error: {e}", exc_info=True)
        await send_msg(token, chat_id, "❌ Произошла ошибка. Попробуйте ещё раз или отправьте /start")


async def dispatch(payload: dict, sess: dict, token: str, chat_id: int):
    stage    = sess.get("stage", "await_photo")
    text     = payload.get("text", "")
    is_cb    = payload.get("isCallback", False)
    cb_data  = payload.get("callbackData", "")
    photo_id = payload.get("photoFileId")

    log.info(f"[{chat_id}] stage={stage} text={text!r} cb={cb_data!r} photo={bool(photo_id)}")

    # Команды — работают из любого состояния
    if text == "/start":
        await cmd_start(token, chat_id)
        await save_session(chat_id, {"stage": "await_photo"})
        return
    if text in ("/reset", "/clear"):
        await delete_session(chat_id)
        await send_msg(token, chat_id, "🗑 История сброшена. Пришлите фото товара.")
        return

    # Подтверждаем callback немедленно
    if is_cb and payload.get("callbackId"):
        await answer_callback(token, payload["callbackId"])

    # Маршрутизация по стадии
    handlers = {
        "await_photo":        step_photo,
        "await_analysis_approve": step_analysis_approve,
        "await_marketplace":  step_marketplace,
        "await_qty":          step_qty,
        "await_series":       step_series,
        "await_style":        step_style,
        "generating":         step_generating,
    }

    handler = handlers.get(stage, step_photo)
    await handler(payload, sess, token, chat_id)


# ═══════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(token: str, chat_id: int):
    text = (
        "👋 Привет! Я — арт-директор маркетплейсов.\n\n"
        "📷 Пришлите фото товара — и я:\n"
        "  • Проанализирую товар и найду УТП\n"
        "  • Составлю профессиональное ТЗ\n"
        "  • Сгенерирую инфографику для:\n"
        "    — Wildberries (900×1200)\n"
        "    — Ozon (1200×1600)\n"
        "    — Яндекс.Маркет (800×800)\n\n"
        "🎨 Качество HD, формат JPEG\n\n"
        "Начнём? Пришлите фото товара 👇"
    )
    await send_msg(token, chat_id, text)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ A — ФОТО И АНАЛИЗ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_photo(payload: dict, sess: dict, token: str, chat_id: int):
    photo_id = payload.get("photoFileId")
    if not photo_id:
        await send_msg(token, chat_id, "📷 Пожалуйста, пришлите *фото товара* (не файл, а картинку).", parse_mode="Markdown")
        return

    await send_msg(token, chat_id, "🔍 Анализирую товар как арт-директор...\n\n⏳ Определяю категорию, УТП, стиль и цвета...")

    try:
        analysis = await gpt_analyze_product(token, photo_id)
    except Exception as e:
        log.error(f"GPT analysis error: {e}")
        await send_msg(token, chat_id, "❌ Не смог проанализировать фото. Попробуйте другое изображение.")
        return

    sess["photo_file_id"] = photo_id
    sess["analysis"]      = analysis
    sess["stage"]         = "await_analysis_approve"
    await save_session(chat_id, sess)

    # Форматируем результат анализа для пользователя
    utp_text = "\n".join([f"  • {u['short']}: {u['detail']}" for u in analysis.get("utp_list", [])[:4]])
    badges_text = ", ".join(analysis.get("badges", [])) or "—"
    
    message = (
        f"📊 *Анализ завершён!*\n\n"
        f"📦 *Товар:* {analysis.get('product_name', 'Товар')}\n"
        f"📂 *Категория:* {analysis.get('category', '—')}\n\n"
        f"🎯 *Заголовок:* {analysis.get('headline', '—')}\n\n"
        f"💡 *УТП:*\n{utp_text}\n\n"
        f"🏷 *Бейджи:* {badges_text}\n\n"
        f"🎨 *Стиль:* {analysis.get('style', {}).get('tone', '—')}\n"
        f"🖼 *Фон:* {analysis.get('style', {}).get('background', '—')}\n\n"
        f"Всё верно?"
    )

    kb = {"inline_keyboard": [[
        {"text": "✅ Согласовать", "callback_data": "analysis:ok"},
        {"text": "🔄 Переанализировать", "callback_data": "analysis:retry"}
    ]]}
    await send_msg(token, chat_id, message, parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ B — СОГЛАСОВАНИЕ АНАЛИЗА
# ═══════════════════════════════════════════════════════════════════════════════

async def step_analysis_approve(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")

    if cb == "analysis:ok":
        sess["stage"] = "await_marketplace"
        await save_session(chat_id, sess)
        await ask_marketplace(token, chat_id)

    elif cb == "analysis:retry":
        sess["stage"] = "await_photo"
        await save_session(chat_id, sess)
        await send_msg(token, chat_id, "📷 Пришлите фото товара ещё раз — проанализирую заново.")

    else:
        kb = {"inline_keyboard": [[
            {"text": "✅ Согласовать", "callback_data": "analysis:ok"},
            {"text": "🔄 Переанализировать", "callback_data": "analysis:retry"}
        ]]}
        await send_msg(token, chat_id, "Подтвердите анализ или запросите повторный:", reply_markup=kb)


async def ask_marketplace(token: str, chat_id: int):
    kb = {"inline_keyboard": [
        [{"text": "🟣 Wildberries (900×1200)",    "callback_data": "mp:wb"}],
        [{"text": "🔵 Ozon (1200×1600)",          "callback_data": "mp:ozon"}],
        [{"text": "🟡 Яндекс.Маркет (800×800)",   "callback_data": "mp:ym"}],
        [{"text": "🌐 Все три сразу",              "callback_data": "mp:all"}],
    ]}
    await send_msg(token, chat_id, "🛒 Выберите маркетплейс:", reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ C — МАРКЕТПЛЕЙС
# ═══════════════════════════════════════════════════════════════════════════════

MP_NAMES = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс.Маркет", "all": "Все три"}

async def step_marketplace(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if cb not in ("mp:wb", "mp:ozon", "mp:ym", "mp:all"):
        await ask_marketplace(token, chat_id)
        return

    mp_key = cb.split(":")[1]
    sess["mp_mode"] = mp_key
    sess["mp"]      = ["wb", "ozon", "ym"] if mp_key == "all" else [mp_key]
    sess["stage"]   = "await_qty"
    await save_session(chat_id, sess)

    mp_label = MP_NAMES[mp_key]
    await send_msg(token, chat_id,
        f"✅ Выбрано: *{mp_label}*\n\n🔢 Сколько вариантов дизайна сгенерировать?\nНапишите цифрой (1–5):",
        parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ D — КОЛИЧЕСТВО
# ═══════════════════════════════════════════════════════════════════════════════

async def step_qty(payload: dict, sess: dict, token: str, chat_id: int):
    text = payload.get("text", "").strip()
    try:
        qty = int(text)
        if qty < 1 or qty > 5:
            raise ValueError
    except ValueError:
        await send_msg(token, chat_id, "⚠️ Введите целое число от 1 до 5:")
        return

    sess["qty"]   = qty
    sess["stage"] = "await_series" if qty > 1 else "await_style"
    await save_session(chat_id, sess)

    mp_mode = sess.get("mp_mode", "wb")
    total   = qty * 3 if mp_mode == "all" else qty
    note    = f" (итого {total} картинок — по {qty} на каждый маркетплейс)" if mp_mode == "all" else ""

    if qty > 1:
        kb = {"inline_keyboard": [[
            {"text": "🔁 Единая серия",  "callback_data": "mode:series"},
            {"text": "🎲 Разные стили",  "callback_data": "mode:different"},
        ]]}
        await send_msg(token, chat_id,
            f"✅ Количество: *{qty}*{note}\n\nГенерировать в едином стиле или с разными вариациями?",
            parse_mode="Markdown", reply_markup=kb)
    else:
        # Используем стиль из анализа
        analysis = sess.get("analysis", {})
        style_tone = analysis.get("style", {}).get("tone", "современный минимализм")
        sess["style"] = style_tone
        sess["stage"] = "generating"
        await save_session(chat_id, sess)
        await start_generation(sess, token, chat_id)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ E — СЕРИЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_series(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if cb not in ("mode:series", "mode:different"):
        kb = {"inline_keyboard": [[
            {"text": "🔁 Единая серия",  "callback_data": "mode:series"},
            {"text": "🎲 Разные стили",  "callback_data": "mode:different"},
        ]]}
        await send_msg(token, chat_id, "Выберите режим:", reply_markup=kb)
        return

    sess["series_mode"] = cb.split(":")[1]
    sess["stage"]       = "await_style"
    await save_session(chat_id, sess)

    # Предлагаем варианты стиля из анализа
    analysis = sess.get("analysis", {})
    style_info = analysis.get("style", {})
    bg_variants = style_info.get("background_variants", ["светлый градиент", "тёмный премиум", "яркий акцент"])
    
    kb = {"inline_keyboard": [
        [{"text": f"🎨 {bg_variants[0]}", "callback_data": "style:0"}],
        [{"text": f"🎨 {bg_variants[1]}", "callback_data": "style:1"}] if len(bg_variants) > 1 else [],
        [{"text": f"🎨 {bg_variants[2]}", "callback_data": "style:2"}] if len(bg_variants) > 2 else [],
        [{"text": "✏️ Свой вариант", "callback_data": "style:custom"}],
    ]}
    # Убираем пустые строки
    kb["inline_keyboard"] = [row for row in kb["inline_keyboard"] if row]
    
    await send_msg(token, chat_id,
        f"🎨 Выберите стиль фона или опишите свой:",
        reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ F — СТИЛЬ → СТАРТ ГЕНЕРАЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_style(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    text = payload.get("text", "").strip()
    
    analysis = sess.get("analysis", {})
    style_info = analysis.get("style", {})
    bg_variants = style_info.get("background_variants", ["светлый градиент", "тёмный премиум", "яркий акцент"])
    
    if cb.startswith("style:"):
        style_idx = cb.split(":")[1]
        if style_idx == "custom":
            sess["awaiting_custom_style"] = True
            await save_session(chat_id, sess)
            await send_msg(token, chat_id, "✏️ Опишите желаемый стиль фона:")
            return
        else:
            idx = int(style_idx)
            style = bg_variants[idx] if idx < len(bg_variants) else bg_variants[0]
    elif sess.get("awaiting_custom_style") and text:
        style = text
        sess["awaiting_custom_style"] = False
    elif text:
        style = text
    else:
        await send_msg(token, chat_id, "🎨 Выберите или опишите стиль фона:")
        return

    sess["style"] = style
    sess["stage"] = "generating"
    await save_session(chat_id, sess)
    
    await start_generation(sess, token, chat_id)


async def start_generation(sess: dict, token: str, chat_id: int):
    qty     = sess.get("qty", 1)
    mp_mode = sess.get("mp_mode", "wb")
    total   = qty * 3 if mp_mode == "all" else qty

    await send_msg(token, chat_id,
        f"⚙️ Запускаю генерацию *{total}* {'картинки' if total == 1 else 'картинок'} в HD качестве...\n\n"
        f"🎨 Стиль: {sess.get('style', 'авто')}\n"
        f"📐 Формат: JPEG\n\n"
        f"⏳ Это займёт ~{total * 30}–{total * 60} секунд. Ожидайте 🕐",
        parse_mode="Markdown")

    asyncio.create_task(run_generation(sess.copy(), token, chat_id))


async def step_generating(payload: dict, sess: dict, token: str, chat_id: int):
    await send_msg(token, chat_id, "⏳ Генерация уже идёт, пожалуйста подождите...")


# ═══════════════════════════════════════════════════════════════════════════════
# ГЕНЕРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════════

MP_SIZES = {
    "wb":   (900,  1200, 40),
    "ozon": (1200, 1600, 40),
    "ym":   (800,  800,  40),
}
MP_LABELS = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс.Маркет"}


async def run_generation(sess: dict, token: str, chat_id: int):
    try:
        photo_id    = sess["photo_file_id"]
        analysis    = sess["analysis"]
        mp_list     = sess["mp"]
        qty         = sess.get("qty", 1)
        style       = sess.get("style", "современный минимализм")
        series_mode = sess.get("series_mode", "series")

        # 1. Скачиваем фото и удаляем фон
        log.info(f"[{chat_id}] Скачиваем фото и удаляем фон")
        photo_bytes = await download_tg_photo(token, photo_id)
        cutout_bytes = await remove_background(photo_bytes)

        # 2. Генерируем картинки для каждого МП
        all_media = []

        for mp_key in mp_list:
            for i in range(qty):
                log.info(f"[{chat_id}] Генерируем {mp_key} #{i+1}/{qty}")

                img_bytes = await generate_infographic(
                    cutout_bytes, analysis, mp_key, style, i, series_mode
                )
                all_media.append((img_bytes, mp_key, i + 1))

        # 3. Отправляем пользователю
        await send_results(token, chat_id, all_media, mp_list, qty)

        # 4. Сбрасываем сессию
        await save_session(chat_id, {"stage": "await_photo"})
        await send_msg(token, chat_id,
            "✅ Готово! Все картинки в HD качестве, формат JPEG.\n\n"
            "📷 Пришлите новое фото для следующего товара.")

    except Exception as e:
        log.error(f"[{chat_id}] generation error: {e}", exc_info=True)
        await save_session(chat_id, {"stage": "await_photo"})
        await send_msg(token, chat_id,
            f"❌ Ошибка при генерации: {str(e)[:200]}\n\nПопробуйте снова — пришлите фото.")


async def send_results(token: str, chat_id: int,
                       all_media: list, mp_list: list, qty: int):
    """Отправляет результаты — по маркетплейсу или одной группой."""

    if len(all_media) == 1:
        img_bytes, mp_key, _ = all_media[0]
        w, h, _ = MP_SIZES[mp_key]
        await send_photo(token, chat_id, img_bytes,
                         caption=f"📦 {MP_LABELS[mp_key]} ({w}×{h})")
        return

    # Группируем по маркетплейсу
    by_mp: dict[str, list] = {}
    for img_bytes, mp_key, idx in all_media:
        by_mp.setdefault(mp_key, []).append((img_bytes, idx))

    for mp_key, items in by_mp.items():
        w, h, _ = MP_SIZES[mp_key]
        if len(items) == 1:
            await send_photo(token, chat_id, items[0][0],
                             caption=f"📦 {MP_LABELS[mp_key]} ({w}×{h})")
        else:
            media_group = []
            for i, (img_bytes, idx) in enumerate(items):
                caption = f"📦 {MP_LABELS[mp_key]} #{idx} ({w}×{h})" if i == 0 else ""
                media_group.append({
                    "type":    "photo",
                    "media":   f"attach://photo_{mp_key}_{idx}",
                    "caption": caption,
                })
            files = {f"photo_{mp_key}_{idx}": img_bytes for img_bytes, idx in items}
            await send_media_group(token, chat_id, media_group, files)


# ═══════════════════════════════════════════════════════════════════════════════
# GPT ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def gpt_analyze_product(token: str, file_id: str) -> dict:
    """GPT-4o vision: полный анализ товара как арт-директор."""
    photo_bytes = await download_tg_photo(token, file_id)
    b64 = base64.b64encode(photo_bytes).decode()

    resp = await openai.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                },
                {
                    "type": "text",
                    "text": ART_DIRECTOR_PROMPT
                }
            ]
        }],
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


async def remove_background(photo_bytes: bytes) -> bytes:
    """gpt-image-1 HD: удаляет фон, возвращает PNG с прозрачностью."""
    resp = await openai.images.edit(
        model="gpt-image-1",
        image=("product.jpg", photo_bytes, "image/jpeg"),
        prompt=(
            "Remove the background completely and return a clean transparent PNG with alpha channel. "
            "Keep product fully visible, centered, not cropped. "
            "Preserve original colors, lighting and all details exactly. "
            "No shadows, no new background, no artifacts, no modifications to the product."
        ),
        n=1,
        size="1024x1024",
        quality="hd",
    )
    img_data = resp.data[0].b64_json
    return base64.b64decode(img_data)


async def generate_infographic(
    cutout_bytes: bytes,
    analysis: dict,
    mp_key: str,
    style: str,
    index: int,
    series_mode: str,
) -> bytes:
    """gpt-image-1 HD: финальная инфографика с точным размером для маркетплейса."""
    w, h, margin = MP_SIZES[mp_key]
    mp_name      = MP_LABELS[mp_key]

    # Извлекаем данные из анализа
    headline = analysis.get("headline", analysis.get("product_name", "Товар"))
    utp_list = analysis.get("utp_list", [])
    badges = analysis.get("badges", ["Новинка"])
    style_info = analysis.get("style", {})
    
    # Форматируем УТП для промпта
    utp_formatted = "\n".join([
        f"- {u['short']}: {u['detail']} (icon: {u['icon']})"
        for u in utp_list[:4]
    ])
    
    # Палитра и эффекты
    palette = ", ".join(style_info.get("palette", ["#ffffff", "#000000", "#333333"]))
    background = style_info.get("background", style)
    tone = style_info.get("tone", "современный")
    effects = style_info.get("effects", "чистый минимализм")
    text_zone = analysis.get("text_zone", "top-left")
    badge_text = badges[0] if badges else "Новинка"

    # Для mode:different меняем вариации
    variation = ""
    if series_mode == "different" and index > 0:
        bg_variants = style_info.get("background_variants", [])
        if index < len(bg_variants):
            background = bg_variants[index]
        variations = [
            "Use different background composition with geometric shapes.",
            "Flip layout — move text zone to opposite side.",
            "Use diagonal dynamic layout with bold accents.",
            "Minimalist version with more whitespace.",
        ]
        variation = f"\n\nVARIATION: {variations[index % len(variations)]}"

    prompt = INFOGRAPHIC_PROMPT_TEMPLATE.format(
        MP_NAME=mp_name, 
        W=w, 
        H=h, 
        MARGIN=margin,
        TEXT_ZONE=text_zone,
        HEADLINE=headline,
        UTP_FORMATTED=utp_formatted,
        BADGE=badge_text,
        BACKGROUND=background,
        PALETTE=palette,
        TONE=tone,
        EFFECTS=effects,
    ) + variation

    # Генерируем в ближайшем поддерживаемом размере
    if w > h:
        gen_size = "1792x1024"
    elif h > w:
        gen_size = "1024x1792"
    else:
        gen_size = "1024x1024"

    resp = await openai.images.edit(
        model="gpt-image-1",
        image=("cutout.png", cutout_bytes, "image/png"),
        prompt=prompt.strip(),
        n=1,
        size=gen_size,
        quality="hd",
    )
    
    # Декодируем результат
    img_data = base64.b64decode(resp.data[0].b64_json)
    
    # Изменяем размер до точного размера маркетплейса и конвертируем в JPEG
    img = Image.open(io.BytesIO(img_data))
    img = img.resize((w, h), Image.LANCZOS)
    
    # Конвертируем в RGB (JPEG не поддерживает прозрачность)
    if img.mode in ('RGBA', 'LA', 'P'):
        background_img = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = background_img
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    # Сохраняем в JPEG с высоким качеством
    output = io.BytesIO()
    img.save(output, format='JPEG', quality=95)
    return output.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM API HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def send_msg(token: str, chat_id: int, text: str,
                   parse_mode: Optional[str] = None,
                   reply_markup: Optional[dict] = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json=payload) as r:
            if r.status != 200:
                log.error(f"sendMessage error: {await r.text()}")


async def send_photo(token: str, chat_id: int, photo_bytes: bytes,
                     caption: str = ""):
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("caption", caption)
    data.add_field("photo", photo_bytes,
                   filename="infographic.jpg", content_type="image/jpeg")

    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          data=data) as r:
            if r.status != 200:
                log.error(f"sendPhoto error: {await r.text()}")


async def send_media_group(token: str, chat_id: int,
                            media: list, files: dict):
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("media", json.dumps(media))
    for name, img_bytes in files.items():
        data.add_field(name, img_bytes,
                       filename=f"{name}.jpg", content_type="image/jpeg")

    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/sendMediaGroup",
                          data=data) as r:
            if r.status != 200:
                log.error(f"sendMediaGroup error: {await r.text()}")


async def answer_callback(token: str, callback_id: str):
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                     json={"callback_query_id": callback_id})


async def download_tg_photo(token: str, file_id: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        # 1. Получаем file_path
        async with s.get(f"https://api.telegram.org/bot{token}/getFile",
                          params={"file_id": file_id}) as r:
            data = await r.json()
        file_path = data["result"]["file_path"]

        # 2. Скачиваем файл
        async with s.get(f"https://api.telegram.org/file/bot{token}/{file_path}") as r:
            return await r.read()


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS СЕССИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def load_session(chat_id: int) -> dict:
    raw = await redis.get(f"session:{chat_id}")
    return json.loads(raw) if raw else {"stage": "await_photo"}


async def save_session(chat_id: int, sess: dict):
    await redis.setex(f"session:{chat_id}", SESSION_TTL, json.dumps(sess))


async def delete_session(chat_id: int):
    await redis.delete(f"session:{chat_id}")
