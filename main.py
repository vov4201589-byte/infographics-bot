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

# ─── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "ВСТАВЬТЕ_OPENAI_КЛЮЧ_СЮДА")
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
        "await_utp_approve":  step_utp_approve,
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
        "👋 Привет! Я генерирую инфографику для маркетплейсов.\n\n"
        "📷 Пришлите фото товара — и я создам карточку для:\n"
        "  • Wildberries (900×1200)\n"
        "  • Ozon (1200×1600)\n"
        "  • Яндекс.Маркет (800×800)\n\n"
        "Начнём? Пришлите фото товара 👇"
    )
    await send_msg(token, chat_id, text)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ A — ФОТО
# ═══════════════════════════════════════════════════════════════════════════════

async def step_photo(payload: dict, sess: dict, token: str, chat_id: int):
    photo_id = payload.get("photoFileId")
    if not photo_id:
        await send_msg(token, chat_id, "📷 Пожалуйста, пришлите *фото товара* (не файл, а картинку).", parse_mode="Markdown")
        return

    await send_msg(token, chat_id, "🔍 Анализирую товар, определяю УТП...")

    try:
        utp = await gpt_extract_utp(token, photo_id)
    except Exception as e:
        log.error(f"GPT UTP error: {e}")
        await send_msg(token, chat_id, "❌ Не смог проанализировать фото. Попробуйте другое изображение.")
        return

    sess["photo_file_id"] = photo_id
    sess["utp"]           = utp
    sess["stage"]         = "await_utp_approve"
    await save_session(chat_id, sess)

    kb = {"inline_keyboard": [[
        {"text": "✅ Согласовать", "callback_data": "utp:ok"},
        {"text": "✏️ Изменить",   "callback_data": "utp:edit"}
    ]]}
    await send_msg(token, chat_id,
        f"💡 УТП: *{utp}*\n\nСогласовать?",
        parse_mode="Markdown", reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ B — УТП СОГЛАСОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_utp_approve(payload: dict, sess: dict, token: str, chat_id: int):
    cb   = payload.get("callbackData", "")
    text = payload.get("text", "").strip()

    if cb == "utp:ok":
        sess["stage"] = "await_marketplace"
        await save_session(chat_id, sess)
        await ask_marketplace(token, chat_id)

    elif cb == "utp:edit":
        sess["stage"] = "await_utp_approve"
        sess["utp_editing"] = True
        await save_session(chat_id, sess)
        await send_msg(token, chat_id, "✏️ Введите УТП (2–3 слова, без кавычек):")

    elif sess.get("utp_editing") and text:
        words = text.split()
        if len(words) < 2 or len(words) > 3:
            await send_msg(token, chat_id, "⚠️ Нужно ровно 2–3 слова. Попробуйте ещё раз:")
            return
        sess["utp"]         = text
        sess["utp_editing"] = False
        sess["stage"]       = "await_marketplace"
        await save_session(chat_id, sess)
        await send_msg(token, chat_id, f"✅ УТП сохранено: *{text}*", parse_mode="Markdown")
        await ask_marketplace(token, chat_id)

    else:
        kb = {"inline_keyboard": [[
            {"text": "✅ Согласовать", "callback_data": "utp:ok"},
            {"text": "✏️ Изменить",   "callback_data": "utp:edit"}
        ]]}
        await send_msg(token, chat_id,
            f"💡 УТП: *{sess.get('utp','?')}*\n\nСогласовать?",
            parse_mode="Markdown", reply_markup=kb)


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
        f"✅ Выбрано: *{mp_label}*\n\n🔢 Сколько картинок сгенерировать?\nНапишите цифрой (1–10):",
        parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ D — КОЛИЧЕСТВО
# ═══════════════════════════════════════════════════════════════════════════════

async def step_qty(payload: dict, sess: dict, token: str, chat_id: int):
    text = payload.get("text", "").strip()
    try:
        qty = int(text)
        if qty < 1 or qty > 10:
            raise ValueError
    except ValueError:
        await send_msg(token, chat_id, "⚠️ Введите целое число от 1 до 10:")
        return

    sess["qty"]   = qty
    sess["stage"] = "await_series" if qty > 1 else "await_style"
    await save_session(chat_id, sess)

    mp_mode = sess.get("mp_mode", "wb")
    total   = qty * 3 if mp_mode == "all" else qty
    note    = f" (итого {total} картинок — по {qty} на каждый маркетплейс)" if mp_mode == "all" else ""

    if qty > 1:
        kb = {"inline_keyboard": [[
            {"text": "🔁 Одинаковая серия",  "callback_data": "mode:series"},
            {"text": "🎲 Каждое разное",     "callback_data": "mode:different"},
        ]]}
        await send_msg(token, chat_id,
            f"✅ Количество: *{qty}*{note}\n\nГенерировать в одном стиле или каждое разное?",
            parse_mode="Markdown", reply_markup=kb)
    else:
        await send_msg(token, chat_id,
            f"✅ Количество: *{qty}*{note}\n\n🎨 В каком цветовом стиле делать фон?\n_(например: пастельные тона, тёмный минимализм, яркий неон)_",
            parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ E — СЕРИЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_series(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if cb not in ("mode:series", "mode:different"):
        kb = {"inline_keyboard": [[
            {"text": "🔁 Одинаковая серия",  "callback_data": "mode:series"},
            {"text": "🎲 Каждое разное",     "callback_data": "mode:different"},
        ]]}
        await send_msg(token, chat_id, "Выберите режим:", reply_markup=kb)
        return

    sess["series_mode"] = cb.split(":")[1]   # "series" или "different"
    sess["stage"]       = "await_style"
    await save_session(chat_id, sess)

    await send_msg(token, chat_id,
        "🎨 В каком цветовом стиле делать фон?\n_(например: пастельные тона, тёмный минимализм, яркий неон)_",
        parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ F — СТИЛЬ → СТАРТ ГЕНЕРАЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_style(payload: dict, sess: dict, token: str, chat_id: int):
    style = payload.get("text", "").strip()
    if not style:
        await send_msg(token, chat_id, "🎨 Напишите стиль фона (любым текстом):")
        return

    sess["style"] = style
    sess["stage"] = "generating"
    await save_session(chat_id, sess)

    qty     = sess.get("qty", 1)
    mp_mode = sess.get("mp_mode", "wb")
    total   = qty * 3 if mp_mode == "all" else qty

    await send_msg(token, chat_id,
        f"⚙️ Начинаю генерацию *{total}* {'картинки' if total < 5 else 'картинок'}...\n\n"
        f"Это займёт ~{total * 30}–{total * 50} секунд. Ожидайте 🕐",
        parse_mode="Markdown")

    # Запускаем генерацию в отдельной задаче
    asyncio.create_task(run_generation(sess.copy(), token, chat_id))


async def step_generating(payload: dict, sess: dict, token: str, chat_id: int):
    await send_msg(token, chat_id, "⏳ Генерация уже идёт, пожалуйста подождите...")


# ═══════════════════════════════════════════════════════════════════════════════
# ГЕНЕРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════════

MP_SIZES = {
    "wb":   (900,  1200, 60),
    "ozon": (1200, 1600, 80),
    "ym":   (800,  800,  50),
}
MP_LABELS = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс.Маркет"}


async def run_generation(sess: dict, token: str, chat_id: int):
    try:
        photo_id    = sess["photo_file_id"]
        utp         = sess["utp"]
        mp_list     = sess["mp"]
        qty         = sess.get("qty", 1)
        style       = sess.get("style", "светлый минималистичный")
        series_mode = sess.get("series_mode", "series")

        # 1. GPT-4o: получаем JSON с контентом инфографики
        log.info(f"[{chat_id}] Запрашиваем контент у GPT-4o")
        content_json = await gpt_infographic_content(utp, style)

        # 2. Скачиваем фото и удаляем фон
        log.info(f"[{chat_id}] Скачиваем фото и удаляем фон")
        photo_bytes = await download_tg_photo(token, photo_id)
        cutout_bytes = await remove_background(photo_bytes)

        # 3. Генерируем картинки для каждого МП
        all_media = []   # [(bytes, mp_key), ...]

        for mp_key in mp_list:
            for i in range(qty):
                log.info(f"[{chat_id}] Генерируем {mp_key} #{i+1}/{qty}")

                vary = (series_mode == "different") or (i == 0 and series_mode == "series")
                img_bytes = await generate_infographic(
                    cutout_bytes, content_json, mp_key, style, i, series_mode
                )
                all_media.append((img_bytes, mp_key, i + 1))

        # 4. Отправляем пользователю
        await send_results(token, chat_id, all_media, mp_list, qty)

        # 5. Сбрасываем сессию
        await save_session(chat_id, {"stage": "await_photo"})
        await send_msg(token, chat_id,
            "✅ Готово! Пришлите новое фото для следующего товара 📷")

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
        await send_photo(token, chat_id, img_bytes,
                         caption=f"📦 {MP_LABELS[mp_key]}")
        return

    # Группируем по маркетплейсу
    by_mp: dict[str, list] = {}
    for img_bytes, mp_key, idx in all_media:
        by_mp.setdefault(mp_key, []).append((img_bytes, idx))

    for mp_key, items in by_mp.items():
        if len(items) == 1:
            await send_photo(token, chat_id, items[0][0],
                             caption=f"📦 {MP_LABELS[mp_key]}")
        else:
            media_group = []
            for i, (img_bytes, idx) in enumerate(items):
                caption = f"📦 {MP_LABELS[mp_key]} #{idx}" if i == 0 else ""
                media_group.append({
                    "type":    "photo",
                    "media":   f"attach://photo_{idx}",
                    "caption": caption,
                })
            files = {f"photo_{idx}": img_bytes for _, idx in items}
            await send_media_group(token, chat_id, media_group, files)


# ═══════════════════════════════════════════════════════════════════════════════
# GPT ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def gpt_extract_utp(token: str, file_id: str) -> str:
    """GPT-4o vision: извлекает УТП товара (2–3 слова на русском)."""
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
                    "text": (
                        "Определи главное УТП (уникальное торговое предложение) этого товара. "
                        "Ответь ТОЛЬКО 2–3 словами на русском языке. "
                        "Без кавычек, без точек, без эмодзи, без названия бренда/модели. "
                        "Пример хорошего ответа: лёгкое и удобное"
                    )
                }
            ]
        }],
        max_tokens=20,
    )
    return resp.choices[0].message.content.strip()


async def gpt_infographic_content(utp: str, style: str) -> dict:
    """GPT-4o: генерирует структуру контента инфографики."""
    prompt = f"""
Ты дизайнер инфографики для маркетплейсов. Сгенерируй JSON для карточки товара.
УТП товара: "{utp}"
Стиль фона: "{style}"

Верни ТОЛЬКО валидный JSON без комментариев:
{{
  "utp": "финальное УТП 2-3 слова",
  "bullets": ["2-4 слова", "2-4 слова", "2-4 слова"],
  "badge": "Новинка",
  "text_zone": "top-left",
  "palette": ["#hex1", "#hex2", "#hex3"],
  "icon_style": "одна фраза про стиль иконок",
  "background_notes": "одна фраза про фон"
}}

Правила:
- bullets: 2-3 штуки, короткие (2-4 слова), без цифр-обещаний
- palette: 3-5 hex цветов, гармоничные, подходящие к стилю "{style}"
- badge всегда "Новинка"
- text_zone: top-left, top-right, bottom-left или bottom-right
"""
    resp = await openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


async def remove_background(photo_bytes: bytes) -> bytes:
    """gpt-image-1: удаляет фон, возвращает PNG с прозрачностью."""
    resp = await openai.images.edit(
        model="gpt-image-1",
        image=("product.jpg", photo_bytes, "image/jpeg"),
        prompt=(
            "Remove the background and return a clean transparent PNG with alpha channel. "
            "Keep product fully visible, centered, not cropped. "
            "Preserve original colors and all details. "
            "No shadows, no new background, no artifacts."
        ),
        n=1,
        size="1024x1024",
    )
    # Декодируем base64 результат
    img_data = resp.data[0].b64_json
    return base64.b64decode(img_data)


MP_PROMPT_TEMPLATE = """
Create a professional marketplace infographic card for {MP_NAME}.
Canvas size: {W}x{H}px. Safe margins: {MARGIN}px on all sides.

Product cutout is provided as input image — keep it exactly as-is, centered on canvas.
DO NOT redraw, modify, distort or replace the product.

Layout:
- Text zone: {TEXT_ZONE} corner
- Badge "Новинка" in contrasting corner
- 2-3 bullet points with small icons (generate icons that match: {ICON_STYLE})

Typography & text (all in Russian):
- Main headline: {UTP}
- Bullets: {BULLETS}

Design:
- Color palette: {PALETTE}
- Background style: {BACKGROUND_NOTES}, {STYLE}
- Clean commercial design, no watermarks, no English text
- All text strictly inside canvas, within safe margins
- Professional marketplace product card aesthetic
"""


async def generate_infographic(
    cutout_bytes: bytes,
    content: dict,
    mp_key: str,
    style: str,
    index: int,
    series_mode: str,
) -> bytes:
    """gpt-image-1: финальная инфографика."""
    w, h, margin = MP_SIZES[mp_key]
    mp_name      = MP_LABELS[mp_key]

    bullets_str  = " | ".join(content.get("bullets", []))
    palette_str  = ", ".join(content.get("palette", ["#ffffff", "#000000"]))

    # Для mode:different меняем акцент в промпте
    variation = ""
    if series_mode == "different" and index > 0:
        variations = [
            "Use completely different background composition and shapes.",
            "Flip layout — move text zone to opposite side, change background geometry.",
            "Use diagonal layout, bold geometric background elements.",
        ]
        variation = variations[index % len(variations)]

    prompt = MP_PROMPT_TEMPLATE.format(
        MP_NAME=mp_name, W=w, H=h, MARGIN=margin,
        TEXT_ZONE=content.get("text_zone", "top-left"),
        UTP=content.get("utp", ""),
        BULLETS=bullets_str,
        PALETTE=palette_str,
        ICON_STYLE=content.get("icon_style", "flat minimal"),
        BACKGROUND_NOTES=content.get("background_notes", "clean gradient"),
        STYLE=style,
    ) + variation

    resp = await openai.images.edit(
        model="gpt-image-1",
        image=("cutout.png", cutout_bytes, "image/png"),
        prompt=prompt.strip(),
        n=1,
        size=f"{w}x{h}" if f"{w}x{h}" in ("1024x1024","1792x1024","1024x1792") else "1024x1024",
    )
    return base64.b64decode(resp.data[0].b64_json)


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
                   filename="infographic.png", content_type="image/png")

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
                       filename=f"{name}.png", content_type="image/png")

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
