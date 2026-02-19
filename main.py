"""
Telegram Infographics Bot — Updated with Krea AI Pipeline
5 шагов: GPT-4o → Krea Previews → Krea Gen → Krea Enhancer → PIL
"""

import os, json, asyncio, base64, io, logging
from typing import Optional

import aiohttp
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont

# ─── Конфиг ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_СЮДА")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY", "ВСТАВЬТЕ_OPENAI_КЛЮЧ_СЮДА")
KREA_API_KEY= os.getenv("KREA_API_KEY", "ВСТАВЬТЕ_KREA_КЛЮЧ_СЮДА")
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379")
SESSION_TTL = 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = FastAPI()
openai = AsyncOpenAI(api_key=OPENAI_KEY)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MP_SIZES = {
    "wb":   (900,  1200, 60),
    "ozon": (1200, 1600, 80),
    "ym":   (800,  800,  50),
}
MP_LABELS = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс.Маркет"}


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks):
    try:
        payload   = await request.json()
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

    # Команды
    if text == "/start":
        await cmd_start(token, chat_id)
        await save_session(chat_id, {"stage": "await_photo"})
        return
    if text in ("/reset", "/clear"):
        await delete_session(chat_id)
        await send_msg(token, chat_id, "🗑 История сброшена. Пришлите фото товара.")
        return

    if is_cb and payload.get("callbackId"):
        await answer_callback(token, payload["callbackId"])

    handlers = {
        "await_photo":           step_photo,
        "await_strategy":        step_strategy,
        "await_background":      step_background,
        "await_marketplace":     step_marketplace,
        "await_qty":             step_qty,
        "await_series":          step_series,
        "generating":            step_generating,
    }

    handler = handlers.get(stage, step_photo)
    await handler(payload, sess, token, chat_id)


# ═══════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(token: str, chat_id: int):
    text = (
        "👋 Привет! Я генерирую WOW-инфографику для маркетплейсов.\n\n"
        "📷 Пришлите фото товара — я создам профессиональную карточку с:\n"
        "  • Креативными фонами через Krea AI\n"
        "  • Реалистичными тенями и отражениями\n"
        "  • Гиперреалистичным апскейлом до 4K\n\n"
        "Начнём? Пришлите фото товара 👇"
    )
    await send_msg(token, chat_id, text)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ A — ФОТО → 3 СТРАТЕГИИ (GPT-4o Vision)
# ═══════════════════════════════════════════════════════════════════════════════

async def step_photo(payload: dict, sess: dict, token: str, chat_id: int):
    photo_id = payload.get("photoFileId")
    if not photo_id:
        await send_msg(token, chat_id, "📷 Пожалуйста, пришлите *фото товара*.", parse_mode="Markdown")
        return

    await send_msg(token, chat_id, "🧠 Анализирую товар и создаю маркетинговые стратегии...")

    try:
        photo_bytes = await download_tg_photo(token, photo_id)
        strategies = await gpt_analyze_strategies(photo_bytes)
    except Exception as e:
        log.error(f"GPT strategies error: {e}")
        await send_msg(token, chat_id, "❌ Не смог проанализировать фото. Попробуйте другое.")
        return

    sess["photo_file_id"] = photo_id
    sess["strategies"]    = strategies
    sess["stage"]         = "await_strategy"
    await save_session(chat_id, sess)

    # Inline кнопки с 3 стратегиями
    kb = {"inline_keyboard": [
        [{"text": f"🎯 {s['title']}", "callback_data": f"strategy:{i}"}]
        for i, s in enumerate(strategies)
    ]}
    
    text = "💡 *Выберите маркетинговую стратегию:*\n\n"
    for i, s in enumerate(strategies, 1):
        text += f"{i}. *{s['title']}*\n_{s['strategy']}_\n\n"
    
    await send_msg(token, chat_id, text, parse_mode="Markdown", reply_markup=kb)


async def gpt_analyze_strategies(photo_bytes: bytes) -> list:
    """GPT-4o Vision: 3 маркетинговые стратегии"""
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
                    "text": """Ты — маркетолог маркетплейсов. Проанализируй фото товара.

Выдай JSON с 3 маркетинговыми концепциями. Каждая должна содержать:
- `title`: название для кнопки (2-4 слова)
- `strategy`: краткое описание стратегии (1 предложение)
- `marketing_hook`: короткий текст для картинки (2-3 слова на русском, без точек)

Примеры стратегий: "Элитный Интерьер", "Природный Лайфстайл", "Техно-Креатив", "Уличный Стиль", "Минимализм-Люкс"

Верни ТОЛЬКО валидный JSON без комментариев:
{
  "strategies": [
    {"title": "...", "strategy": "...", "marketing_hook": "..."},
    {"title": "...", "strategy": "...", "marketing_hook": "..."},
    {"title": "...", "strategy": "...", "marketing_hook": "..."}
  ]
}"""
                }
            ]
        }],
        max_tokens=500,
        response_format={"type": "json_object"}
    )
    
    result = json.loads(resp.choices[0].message.content)
    if "strategies" in result:
        return result["strategies"]
    elif isinstance(result, list):
        return result
    else:
        # Fallback если структура другая
        return [
            {"title": "Элитный", "strategy": "Премиум товар для ценителей", "marketing_hook": "Выбор профи"},
            {"title": "Практичный", "strategy": "Надёжность на каждый день", "marketing_hook": "Просто работает"},
            {"title": "Стильный", "strategy": "Модный дизайн", "marketing_hook": "Будь в тренде"}
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ B — ВЫБОР СТРАТЕГИИ → 3 ПРЕВЬЮ ФОНОВ (Krea Flash)
# ═══════════════════════════════════════════════════════════════════════════════

async def step_strategy(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if not cb.startswith("strategy:"):
        return
    
    strategy_idx = int(cb.split(":")[1])
    strategies = sess.get("strategies", [])
    if strategy_idx >= len(strategies):
        await send_msg(token, chat_id, "❌ Неверная стратегия")
        return
    
    selected = strategies[strategy_idx]
    sess["selected_strategy"] = selected
    sess["stage"] = "await_background"
    await save_session(chat_id, sess)
    
    await send_msg(token, chat_id, f"✅ Выбрано: *{selected['title']}*\n\n⏳ Генерирую 3 варианта фонов...", parse_mode="Markdown")
    
    # GPT-4o создаёт 3 промпта для Krea
    try:
        prompts = await gpt_create_background_prompts(selected)
        previews = await krea_generate_previews(prompts)
    except Exception as e:
        log.error(f"Krea previews error: {e}")
        await send_msg(token, chat_id, "❌ Ошибка генерации превью. Попробуйте другую стратегию.")
        return
    
    sess["background_prompts"] = prompts
    sess["background_previews"] = previews
    await save_session(chat_id, sess)
    
    # Отправляем 3 превью с кнопками
    media_group = []
    for i, (prompt, preview_url) in enumerate(zip(prompts, previews)):
        caption = f"Фон {i+1}" if i == 0 else ""
        media_group.append({
            "type": "photo",
            "media": preview_url,
            "caption": caption
        })
    
    await send_media_group_urls(token, chat_id, media_group)
    
    kb = {"inline_keyboard": [[
        {"text": f"🖼 Фон {i+1}", "callback_data": f"bg:{i}"}
        for i in range(3)
    ]]}
    await send_msg(token, chat_id, "👆 Выберите фон:", reply_markup=kb)


async def gpt_create_background_prompts(strategy: dict) -> list[str]:
    """GPT-4o создаёт 3 промпта для Krea на основе стратегии"""
    resp = await openai.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": f"""На основе маркетинговой концепции "{strategy['title']}" ({strategy['strategy']}), 
напиши 3 детальных промпта для Krea AI на английском.

Требования:
- Фокусируйся на окружении, освещении и стиле
- Промпты в стиле "high-end product photography, 8k, highly detailed textures, studio lighting"
- Каждый промпт должен быть уникальным но в рамках концепции
- Без упоминания конкретного товара (товар добавится автоматически)

Примеры:
- "Luxury marble countertop with soft natural light, elegant interior, bokeh background, 8k"
- "Urban rooftop at golden hour, city skyline, cinematic lighting, photorealistic"
- "Minimalist scandinavian room, white walls, plants, natural daylight, high detail"

Верни ТОЛЬКО JSON массив из 3 промптов:
{{"prompts": ["prompt1", "prompt2", "prompt3"]}}"""
        }],
        max_tokens=300,
        response_format={"type": "json_object"}
    )
    
    result = json.loads(resp.choices[0].message.content)
    if "prompts" in result:
        return result["prompts"][:3]
    else:
        # Fallback
        return [
            "Luxury interior with marble and gold, soft studio lighting, 8k",
            "Modern minimalist setting, white background, professional photography",
            "Natural outdoor scene, bokeh background, golden hour lighting"
        ]


async def krea_generate_previews(prompts: list[str]) -> list[str]:
    """Генерирует 3 быстрых превью через Krea Flash"""
    preview_urls = []
    
    async with aiohttp.ClientSession() as session:
        for prompt in prompts:
            try:
                async with session.post(
                    "https://api.krea.ai/v1/images/generations",
                    headers={
                        "Authorization": f"Bearer {KREA_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "prompt": prompt,
                        "model": "krea-flash",
                        "width": 512,
                        "height": 512,
                        "steps": 4
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        preview_urls.append(data["images"][0]["url"])
                    else:
                        log.error(f"Krea preview error: {await resp.text()}")
                        # Fallback: используем заглушку
                        preview_urls.append("https://via.placeholder.com/512?text=Preview")
            except Exception as e:
                log.error(f"Krea preview exception: {e}")
                preview_urls.append("https://via.placeholder.com/512?text=Error")
            
            await asyncio.sleep(0.5)
    
    return preview_urls


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ C — ВЫБОР ФОНА → МАРКЕТПЛЕЙС
# ═══════════════════════════════════════════════════════════════════════════════

async def step_background(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if not cb.startswith("bg:"):
        return
    
    bg_idx = int(cb.split(":")[1])
    prompts = sess.get("background_prompts", [])
    if bg_idx >= len(prompts):
        return
    
    sess["selected_background_idx"] = bg_idx
    sess["selected_background_prompt"] = prompts[bg_idx]
    sess["stage"] = "await_marketplace"
    await save_session(chat_id, sess)
    
    await send_msg(token, chat_id, f"✅ Фон выбран!\n\n🛒 Теперь выберите маркетплейс:")
    await ask_marketplace(token, chat_id)


async def ask_marketplace(token: str, chat_id: int):
    kb = {"inline_keyboard": [
        [{"text": "🟣 Wildberries (900×1200)",    "callback_data": "mp:wb"}],
        [{"text": "🔵 Ozon (1200×1600)",          "callback_data": "mp:ozon"}],
        [{"text": "🟡 Яндекс.Маркет (800×800)",   "callback_data": "mp:ym"}],
        [{"text": "🌐 Все три сразу",              "callback_data": "mp:all"}],
    ]}
    await send_msg(token, chat_id, "Выберите:", reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ D — МАРКЕТПЛЕЙС
# ═══════════════════════════════════════════════════════════════════════════════

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

    await send_msg(token, chat_id, f"✅ {MP_LABELS.get(mp_key, 'Все три')}\n\n🔢 Сколько картинок сгенерировать? (1-10):")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ E — КОЛИЧЕСТВО
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
    sess["stage"] = "await_series" if qty > 1 else "generating"
    await save_session(chat_id, sess)

    if qty > 1:
        kb = {"inline_keyboard": [[
            {"text": "🔁 Одинаковые",  "callback_data": "mode:series"},
            {"text": "🎲 Разные",       "callback_data": "mode:different"},
        ]]}
        await send_msg(token, chat_id, f"✅ Количество: {qty}\n\nГенерировать одинаковые или разные?", reply_markup=kb)
    else:
        await start_generation(sess, token, chat_id)


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ F — СЕРИЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def step_series(payload: dict, sess: dict, token: str, chat_id: int):
    cb = payload.get("callbackData", "")
    if cb not in ("mode:series", "mode:different"):
        return

    sess["series_mode"] = cb.split(":")[1]
    sess["stage"]       = "generating"
    await save_session(chat_id, sess)

    await start_generation(sess, token, chat_id)


async def step_generating(payload: dict, sess: dict, token: str, chat_id: int):
    await send_msg(token, chat_id, "⏳ Генерация уже идёт...")


# ═══════════════════════════════════════════════════════════════════════════════
# ГЕНЕРАЦИЯ (5 ШАГОВ)
# ═══════════════════════════════════════════════════════════════════════════════

async def start_generation(sess: dict, token: str, chat_id: int):
    qty = sess.get("qty", 1)
    mp_mode = sess.get("mp_mode", "wb")
    total = qty * 3 if mp_mode == "all" else qty

    await send_msg(token, chat_id,
        f"🎨 Запускаю генерацию {total} {'изображения' if total < 5 else 'изображений'}...\n\n"
        f"Это займёт ~{total * 45}–{total * 60} секунд.\n\n"
        f"Этапы:\n"
        f"1️⃣ Krea Background Generation (~30 сек)\n"
        f"2️⃣ Krea Enhancer 4K (~20 сек)\n"
        f"3️⃣ Наложение инфографики\n\n"
        f"Ожидайте... ⏳")

    asyncio.create_task(run_generation(sess.copy(), token, chat_id))


async def run_generation(sess: dict, token: str, chat_id: int):
    try:
        photo_id    = sess["photo_file_id"]
        strategy    = sess["selected_strategy"]
        bg_prompt   = sess["selected_background_prompt"]
        mp_list     = sess["mp"]
        qty         = sess.get("qty", 1)
        series_mode = sess.get("series_mode", "series")

        photo_bytes = await download_tg_photo(token, photo_id)

        all_media = []

        for mp_key in mp_list:
            for i in range(qty):
                log.info(f"[{chat_id}] Генерируем {mp_key} #{i+1}/{qty}")

                # Шаг 3: Krea Background Generation (вживление товара)
                composed_bytes = await krea_background_generation(
                    photo_bytes, bg_prompt, mp_key
                )

                # Шаг 4: Krea Enhancer (апскейл до 4K)
                enhanced_bytes = await krea_enhance(composed_bytes, mp_key)

                # Шаг 5: Наложение инфографики
                final_bytes = await add_infographic_overlay(
                    enhanced_bytes, strategy, mp_key
                )

                all_media.append((final_bytes, mp_key, i + 1))

        await send_results(token, chat_id, all_media, mp_list, qty)

        await save_session(chat_id, {"stage": "await_photo"})
        await send_msg(token, chat_id, "✅ Готово! Пришлите новое фото 📷")

    except Exception as e:
        log.error(f"[{chat_id}] generation error: {e}", exc_info=True)
        await save_session(chat_id, {"stage": "await_photo"})
        await send_msg(token, chat_id, f"❌ Ошибка: {str(e)[:200]}\n\nПопробуйте снова.")


# ═══════════════════════════════════════════════════════════════════════════════
# KREA API
# ═══════════════════════════════════════════════════════════════════════════════

async def krea_background_generation(
    product_photo: bytes,
    background_prompt: str,
    mp_key: str
) -> bytes:
    """Шаг 3: Krea вырезает товар и вплавляет его в фон"""
    w, h, _ = MP_SIZES[mp_key]

    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("image", product_photo, filename="product.jpg")
        form.add_field("prompt", background_prompt)
        form.add_field("width", str(w))
        form.add_field("height", str(h))
        form.add_field("model", "krea-pro")
        form.add_field("steps", "20")

        async with session.post(
            "https://api.krea.ai/v1/images/background-generation",
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            data=form,
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                raise Exception(f"Krea API error: {await resp.text()}")
            data = await resp.json()
            
            if "id" in data:
                result = await _wait_for_krea_result(session, data["id"])
                return result
            else:
                image_url = data["images"][0]["url"]
                async with session.get(image_url) as img_resp:
                    return await img_resp.read()


async def krea_enhance(image_bytes: bytes, mp_key: str) -> bytes:
    """Шаг 4: Krea Enhancer увеличивает до 4K и добавляет гиперреализм"""
    w, h, _ = MP_SIZES[mp_key]
    target_w = w * 2
    target_h = h * 2

    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("image", image_bytes, filename="input.png")
        form.add_field("width", str(target_w))
        form.add_field("height", str(target_h))
        form.add_field("enhance_level", "high")

        async with session.post(
            "https://api.krea.ai/v1/images/enhance",
            headers={"Authorization": f"Bearer {KREA_API_KEY}"},
            data=form,
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                raise Exception(f"Krea Enhance error: {await resp.text()}")
            data = await resp.json()
            
            if "id" in data:
                result = await _wait_for_krea_result(session, data["id"])
                return result
            else:
                image_url = data["images"][0]["url"]
                async with session.get(image_url) as img_resp:
                    return await img_resp.read()


async def _wait_for_krea_result(session: aiohttp.ClientSession, job_id: str) -> bytes:
    """Ждёт результата асинхронной задачи Krea"""
    for _ in range(60):
        await asyncio.sleep(2)
        async with session.get(
            f"https://api.krea.ai/v1/images/{job_id}",
            headers={"Authorization": f"Bearer {KREA_API_KEY}"}
        ) as resp:
            data = await resp.json()
            if data["status"] == "completed":
                image_url = data["images"][0]["url"]
                async with session.get(image_url) as img_resp:
                    return await img_resp.read()
            elif data["status"] == "failed":
                raise Exception("Krea generation failed")
    
    raise Exception("Krea timeout")


# ═══════════════════════════════════════════════════════════════════════════════
# ШАГ 5 — НАЛОЖЕНИЕ ИНФОГРАФИКИ
# ═══════════════════════════════════════════════════════════════════════════════

async def add_infographic_overlay(
    image_bytes: bytes,
    strategy: dict,
    mp_key: str
) -> bytes:
    """Накладываем текст и плашки через PIL"""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    w, h, margin = MP_SIZES[mp_key]
    if img.size != (w, h):
        img = img.resize((w, h), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(img)
    
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 56)
        font_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    except:
        font_title = ImageFont.load_default()
        font_body  = ImageFont.load_default()
    
    hook = strategy["marketing_hook"]
    
    x_text = margin + 30
    y_text = margin + 30
    
    bbox = draw.textbbox((x_text, y_text), hook, font=font_title)
    draw.rectangle(
        [bbox[0] - 20, bbox[1] - 15, bbox[2] + 20, bbox[3] + 15],
        fill=(255, 255, 255, 230)
    )
    
    draw.text((x_text, y_text), hook, fill=(0, 0, 0), font=font_title)
    
    badge_x = w - margin - 180
    badge_y = margin + 30
    draw.rectangle(
        [badge_x, badge_y, badge_x + 170, badge_y + 60],
        fill=(255, 75, 75)
    )
    draw.text((badge_x + 20, badge_y + 15), "НОВИНКА", fill=(255, 255, 255), font=font_body)
    
    output = io.BytesIO()
    img.save(output, format="PNG", quality=95)
    return output.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# ОТПРАВКА РЕЗУЛЬТАТОВ
# ═══════════════════════════════════════════════════════════════════════════════

async def send_results(token: str, chat_id: int, all_media: list, mp_list: list, qty: int):
    if len(all_media) == 1:
        img_bytes, mp_key, _ = all_media[0]
        await send_photo(token, chat_id, img_bytes, caption=f"📦 {MP_LABELS[mp_key]}")
        return

    by_mp = {}
    for img_bytes, mp_key, idx in all_media:
        by_mp.setdefault(mp_key, []).append((img_bytes, idx))

    for mp_key, items in by_mp.items():
        if len(items) == 1:
            await send_photo(token, chat_id, items[0][0], caption=f"📦 {MP_LABELS[mp_key]}")
        else:
            media_group = []
            for i, (img_bytes, idx) in enumerate(items):
                caption = f"📦 {MP_LABELS[mp_key]} #{idx}" if i == 0 else ""
                media_group.append({
                    "type": "photo",
                    "media": f"attach://photo_{idx}",
                    "caption": caption,
                })
            files = {f"photo_{idx}": img_bytes for _, idx in items}
            await send_media_group(token, chat_id, media_group, files)


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM API
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


async def send_photo(token: str, chat_id: int, photo_bytes: bytes, caption: str = ""):
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("caption", caption)
    data.add_field("photo", photo_bytes, filename="image.png", content_type="image/png")

    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data) as r:
            if r.status != 200:
                log.error(f"sendPhoto error: {await r.text()}")


async def send_media_group(token: str, chat_id: int, media: list, files: dict):
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("media", json.dumps(media))
    for name, img_bytes in files.items():
        data.add_field(name, img_bytes, filename=f"{name}.png", content_type="image/png")

    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/sendMediaGroup", data=data) as r:
            if r.status != 200:
                log.error(f"sendMediaGroup error: {await r.text()}")


async def send_media_group_urls(token: str, chat_id: int, media: list):
    """Отправляет media group с URL (для превью)"""
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"https://api.telegram.org/bot{token}/sendMediaGroup",
            json={"chat_id": chat_id, "media": media}
        ) as r:
            if r.status != 200:
                log.error(f"sendMediaGroup URLs error: {await r.text()}")


async def answer_callback(token: str, callback_id: str):
    async with aiohttp.ClientSession() as s:
        await s.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                     json={"callback_query_id": callback_id})


async def download_tg_photo(token: str, file_id: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.telegram.org/bot{token}/getFile",
                          params={"file_id": file_id}) as r:
            data = await r.json()
        file_path = data["result"]["file_path"]
        
        async with s.get(f"https://api.telegram.org/file/bot{token}/{file_path}") as r:
            return await r.read()


# ═══════════════════════════════════════════════════════════════════════════════
# REDIS (с reconnect logic)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_redis():
    """Получает Redis соединение с retry logic"""
    try:
        return await aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        log.error(f"Redis connection error: {e}")
        await asyncio.sleep(0.5)
        return await aioredis.from_url(REDIS_URL, decode_responses=True)


async def load_session(chat_id: int) -> dict:
    try:
        r = await get_redis()
        raw = await r.get(f"session:{chat_id}")
        await r.close()
        return json.loads(raw) if raw else {"stage": "await_photo"}
    except Exception as e:
        log.error(f"load_session error: {e}")
        return {"stage": "await_photo"}


async def save_session(chat_id: int, sess: dict):
    try:
        r = await get_redis()
        await r.setex(f"session:{chat_id}", SESSION_TTL, json.dumps(sess))
        await r.close()
    except Exception as e:
        log.error(f"save_session error: {e}")


async def delete_session(chat_id: int):
    try:
        r = await get_redis()
        await r.delete(f"session:{chat_id}")
        await r.close()
    except Exception as e:
        log.error(f"delete_session error: {e}")
