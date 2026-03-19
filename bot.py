"""
SafeRoute Bot
─────────────
• Кожні 30 хв читає два TG канали, витягує адреси, геокодує їх
• Отримує твою Live Location від Telegram
• Якщо ти ближче 500м до будь-якої точки → надсилає алерт + обхідний маршрут
• О 00:00 скидає всі точки
"""

import asyncio
import logging
import re
from datetime import time as dtime
from math import radians, sin, cos, sqrt, atan2

import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, JobQueue
)

import config

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── Глобальний стан ──────────────────────────────────────────────────────────
dangerous_points: list[dict] = []   # [{lat, lng, address, source}]
alerted_points: set[str] = set()    # щоб не спамити одним і тим самим алертом
subscribed_users: set[int] = set()  # user_id тих хто запустив бота


# ═══════════════════════════════════════════════════════════════════════════════
# УТИЛІТИ
# ═══════════════════════════════════════════════════════════════════════════════

def haversine_meters(lat1, lon1, lat2, lon2) -> float:
    """Відстань між двома точками в метрах."""
    R = 6_371_000
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def maps_link(lat, lng) -> str:
    return f"https://maps.google.com/?q={lat},{lng}"


def avoid_link(user_lat, user_lng, dest_lat=None, dest_lng=None) -> str:
    """Посилання на Google Maps з точкою небезпеки як waypoint-ом що треба обминути."""
    if dest_lat:
        return (
            f"https://www.google.com/maps/dir/{user_lat},{user_lng}/"
            f"{dest_lat},{dest_lng}/@{user_lat},{user_lng},15z"
        )
    return f"https://www.google.com/maps/@{user_lat},{user_lng},15z"


# ═══════════════════════════════════════════════════════════════════════════════
# ПАРСИНГ АДРЕС
# ═══════════════════════════════════════════════════════════════════════════════

def extract_addresses(text: str) -> list[str]:
    results = []

    # Координати: 50.4501, 30.5234
    for m in re.finditer(r'\b(4[5-9]\.\d{3,6})[,\s]+(2[5-9]\.\d{3,6})\b', text):
        results.append(f"COORD:{m.group(1)},{m.group(2)}")

    # Вулиця + номер будинку (укр/рос)
    street_re = re.compile(
        r'(?:вул(?:иця)?\.?\s*|просп(?:ект)?\.?\s*|пров(?:улок)?\.?\s*'
        r'|бульв(?:ар)?\.?\s*|пл(?:оща)?\.?\s*|шосе\s*|набережна\s*)'
        r'([А-ЯҐЄІЇа-яґєії\'\-\s]{3,40}?)'
        r'[,\s]+(\d+[А-ЯҐЄІЇа-яґєії/\-]*)',
        re.IGNORECASE
    )
    for m in street_re.finditer(text):
        results.append(m.group(0).strip())

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# ГЕОКОДУВАННЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def geocode(address: str) -> tuple[float, float] | None:
    query = address
    if config.DEFAULT_CITY.lower() not in address.lower():
        query = f"{address}, {config.DEFAULT_CITY}"

    async with aiohttp.ClientSession() as session:
        try:
            resp = await session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1,
                        "countrycodes": config.COUNTRY_CODE},
                headers={"User-Agent": "SafeRouteBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            data = await resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            log.warning(f"Geocode error '{address}': {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ЧИТАННЯ КАНАЛІВ (Telethon)
# ═══════════════════════════════════════════════════════════════════════════════

async def fetch_channel_messages(channel: str) -> list[str]:
    session = StringSession(config.TELETHON_SESSION)
    client = TelegramClient(session, config.API_ID, config.API_HASH)
    texts = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telethon не авторизований. Запусти auth.py локально.")
            return []
        async for msg in client.iter_messages(channel, limit=100):
            if msg.text:
                texts.append(msg.text)
    except Exception as e:
        log.error(f"Telethon error ({channel}): {e}")
    finally:
        await client.disconnect()
    return texts


# ═══════════════════════════════════════════════════════════════════════════════
# ДЖОБ: оновлення точок кожні 30 хв
# ═══════════════════════════════════════════════════════════════════════════════

async def job_update_points(context: ContextTypes.DEFAULT_TYPE):
    global dangerous_points, alerted_points
    log.info("🔄 Оновлення точок...")

    collected: list[dict] = []

    for channel in config.CHANNELS:
        messages = await fetch_channel_messages(channel)
        for msg in messages:
            for addr in extract_addresses(msg):
                if addr.startswith("COORD:"):
                    lat, lng = map(float, addr[6:].split(","))
                    collected.append({"lat": lat, "lng": lng,
                                      "address": f"{lat:.4f},{lng:.4f}",
                                      "source": channel})
                else:
                    coords = await geocode(addr)
                    if coords:
                        collected.append({"lat": coords[0], "lng": coords[1],
                                          "address": addr, "source": channel})
                    await asyncio.sleep(1.1)  # Nominatim: 1 req/sec

    # Дедуплікація — якщо дві точки ближче 30м вважаємо дублікатом
    unique: list[dict] = []
    for pt in collected:
        if not any(haversine_meters(pt["lat"], pt["lng"],
                                    ex["lat"], ex["lng"]) < 30
                   for ex in unique):
            unique.append(pt)

    dangerous_points = unique
    alerted_points.clear()  # скидаємо щоб при наступному оновленні заново перевірити
    log.info(f"✅ Точок: {len(dangerous_points)}")

    # Повідомляємо підписаних
    for uid in subscribed_users:
        try:
            await context.bot.send_message(
                uid,
                f"🗺 *Оновлено:* {len(dangerous_points)} точок з каналів\n"
                f"Увімкни Live Location щоб я стежив за тобою 👇",
                parse_mode="Markdown"
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# ДЖОБ: скидання о 00:00
# ═══════════════════════════════════════════════════════════════════════════════

async def job_midnight_reset(context: ContextTypes.DEFAULT_TYPE):
    global dangerous_points, alerted_points
    dangerous_points = []
    alerted_points = set()
    log.info("🗑️ Точки скинуто (нова доба)")
    for uid in subscribed_users:
        try:
            await context.bot.send_message(
                uid, "🗑️ Нова доба — точки скинуто. Збираю нові дані...",
            )
        except Exception:
            pass
    # Одразу запускаємо оновлення
    await job_update_points(context)


# ═══════════════════════════════════════════════════════════════════════════════
# ОБРОБКА LIVE LOCATION
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location or (
        update.edited_message.location if update.edited_message else None
    )
    if not loc:
        return

    user_id = (update.message or update.edited_message).from_user.id
    user_lat, user_lng = loc.latitude, loc.longitude

    if not dangerous_points:
        return

    nearby = []
    for pt in dangerous_points:
        dist = haversine_meters(user_lat, user_lng, pt["lat"], pt["lng"])
        if dist <= config.ALERT_RADIUS_METERS:
            key = f"{user_id}:{pt['lat']:.4f}:{pt['lng']:.4f}"
            if key not in alerted_points:
                nearby.append((dist, pt))
                alerted_points.add(key)

    if not nearby:
        return

    nearby.sort(key=lambda x: x[0])

    lines = ["⚠️ *НЕБЕЗПЕЧНА ЗОНА ПОРУЧ!*\n"]
    for dist, pt in nearby[:3]:  # максимум 3 алерти за раз
        lines.append(
            f"📍 *{pt['address']}*\n"
            f"   Відстань: *{int(dist)}м*\n"
            f"   [Подивитись на карті]({maps_link(pt['lat'], pt['lng'])})\n"
        )

    lines.append(
        f"\n🔀 [Переплануй маршрут]"
        f"({avoid_link(user_lat, user_lng)})"
    )

    await context.bot.send_message(
        user_id,
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=False
    )


# ═══════════════════════════════════════════════════════════════════════════════
# КОМАНДИ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subscribed_users.add(user_id)

    # Кнопка для швидкого відправлення локації
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Поділитись Live Location", request_location=True)]],
        resize_keyboard=True
    )
    await update.message.reply_text(
        "👋 *SafeRoute Bot*\n\n"
        "Я стежу за небезпечними адресами з каналів і попереджаю тебе коли ти поруч.\n\n"
        "1️⃣ Натисни кнопку нижче\n"
        "2️⃣ Вибери *«Поділитися геопозицією»*\n"
        "3️⃣ Вибери термін — *8 годин*\n\n"
        "Більше нічого не треба — бот сам все зробить 🔄",
        parse_mode="Markdown",
        reply_markup=kb
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 *Статус*\n\n"
        f"Небезпечних точок: *{len(dangerous_points)}*\n"
        f"Радіус алерту: *{config.ALERT_RADIUS_METERS}м*\n"
        f"Канали: {', '.join(config.CHANNELS)}",
        parse_mode="Markdown"
    )


async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not dangerous_points:
        await update.message.reply_text("Точок поки немає.")
        return

    lines = [f"📍 *Активні точки ({len(dangerous_points)}):*\n"]
    for i, pt in enumerate(dangerous_points[:20], 1):
        lines.append(
            f"{i}. [{pt['address']}]({maps_link(pt['lat'], pt['lng'])})"
        )
    if len(dangerous_points) > 20:
        lines.append(f"\n_...та ще {len(dangerous_points) - 20}_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Надсилає посилання на Google My Maps з усіма точками."""
    if not dangerous_points:
        await update.message.reply_text("Точок поки немає.")
        return

    # Google Maps URL з кількома пінами
    base = "https://www.google.com/maps/search/?api=1&query="
    if len(dangerous_points) == 1:
        pt = dangerous_points[0]
        url = f"{base}{pt['lat']},{pt['lng']}"
    else:
        # Для кількох точок — KML через data URI або просто перша точка + список
        pts_str = "\n".join(
            f"• [{pt['address']}]({maps_link(pt['lat'], pt['lng'])})"
            for pt in dangerous_points[:15]
        )
        await update.message.reply_text(
            f"🗺 *Всі точки на карті:*\n\n{pts_str}",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return

    await update.message.reply_text(f"🗺 [Відкрити на карті]({url})", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(config.BOT_TOKEN).build()

    # Хендлери
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("points", cmd_points))
    app.add_handler(CommandHandler("map", cmd_map))

    # Live Location — і нова і оновлена
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # Оновлення live location приходять як edited_message
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_location))

    jq: JobQueue = app.job_queue

    # Оновлення точок кожні 30 хвилин
    jq.run_repeating(
        job_update_points,
        interval=config.UPDATE_INTERVAL_MINUTES * 60,
        first=10  # перший запуск через 10 секунд після старту
    )

    # Скидання о 00:00 Київ (UTC+2/+3)
    jq.run_daily(
        job_midnight_reset,
        time=dtime(hour=21, minute=0)  # 21:00 UTC = 00:00 Київ (зима), коригуй під сезон
    )

    log.info("🚀 Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
