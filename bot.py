"""
SafeRoute Bot
"""

import asyncio
import logging
import os
import re
from datetime import time as dtime
from math import radians, sin, cos, sqrt, atan2

import aiohttp
from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, JobQueue
)

import config

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

dangerous_points: list[dict] = []
alerted_points: set[str] = set()
subscribed_users: set[int] = set()


def haversine_meters(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp/2)**2 + cos(p1)*cos(p2)*sin(dl/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def maps_link(lat, lng) -> str:
    return f"https://maps.google.com/?q={lat},{lng}"


def extract_addresses(text: str) -> list[str]:
    results = []
    for m in re.finditer(r'\b(4[5-9]\.\d{3,6})[,\s]+(2[5-9]\.\d{3,6})\b', text):
        results.append(f"COORD:{m.group(1)},{m.group(2)}")
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


async def geocode(address: str) -> tuple[float, float] | None:
    query = address
    if config.DEFAULT_CITY.lower() not in address.lower():
        query = f"{address}, {config.DEFAULT_CITY}"
    async with aiohttp.ClientSession() as session:
        try:
            resp = await session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": config.COUNTRY_CODE},
                headers={"User-Agent": "SafeRouteBot/1.0"},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            data = await resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            log.warning(f"Geocode error '{address}': {e}")
    return None


async def fetch_channel_messages(channel: str) -> list[str]:
    session = StringSession(config.TELETHON_SESSION)
    client = TelegramClient(session, config.API_ID, config.API_HASH)
    texts = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Telethon не авторизований.")
            return []
        async for msg in client.iter_messages(channel, limit=100):
            if msg.text:
                texts.append(msg.text)
    except Exception as e:
        log.error(f"Telethon error ({channel}): {e}")
    finally:
        await client.disconnect()
    return texts


async def job_update_points(context: ContextTypes.DEFAULT_TYPE):
    global dangerous_points, alerted_points
    log.info("Оновлення точок...")
    collected: list[dict] = []
    for channel in config.CHANNELS:
        messages = await fetch_channel_messages(channel)
        for msg in messages:
            for addr in extract_addresses(msg):
                if addr.startswith("COORD:"):
                    lat, lng = map(float, addr[6:].split(","))
                    collected.append({"lat": lat, "lng": lng, "address": f"{lat:.4f},{lng:.4f}", "source": channel})
                else:
                    coords = await geocode(addr)
                    if coords:
                        collected.append({"lat": coords[0], "lng": coords[1], "address": addr, "source": channel})
                    await asyncio.sleep(1.1)
    unique: list[dict] = []
    for pt in collected:
        if not any(haversine_meters(pt["lat"], pt["lng"], ex["lat"], ex["lng"]) < 30 for ex in unique):
            unique.append(pt)
    dangerous_points = unique
    alerted_points.clear()
    log.info(f"Точок: {len(dangerous_points)}")
    for uid in subscribed_users:
        try:
            await context.bot.send_message(uid, f"🗺 *Оновлено:* {len(dangerous_points)} точок з каналів", parse_mode="Markdown")
        except Exception:
            pass


async def job_midnight_reset(context: ContextTypes.DEFAULT_TYPE):
    global dangerous_points, alerted_points
    dangerous_points = []
    alerted_points = set()
    log.info("Точки скинуто (нова доба)")
    for uid in subscribed_users:
        try:
            await context.bot.send_message(uid, "🗑️ Нова доба — точки скинуто. Збираю нові дані...")
        except Exception:
            pass
    await job_update_points(context)


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return
    user_id = msg.from_user.id
    user_lat, user_lng = msg.location.latitude, msg.location.longitude
    user_locations[user_id] = (user_lat, user_lng)
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
    for dist, pt in nearby[:3]:
        lines.append(f"📍 *{pt['address']}*\n   Відстань: *{int(dist)}м*\n   [Карта]({maps_link(pt['lat'], pt['lng'])})\n")
    lines.append(f"\n🔀 [Переплануй маршрут](https://www.google.com/maps/@{user_lat},{user_lng},15z)")
    await context.bot.send_message(user_id, "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=False)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribed_users.add(update.effective_user.id)
    kb = ReplyKeyboardMarkup([[KeyboardButton("📍 Поділитись Live Location", request_location=True)]], resize_keyboard=True)
    await update.message.reply_text(
        "👋 *SafeRoute Bot*\n\nЯ стежу за небезпечними адресами з каналів і попереджаю тебе коли ти поруч.\n\n"
        "1️⃣ Натисни кнопку нижче\n2️⃣ Вибери «Поділитися геопозицією»\n3️⃣ Вибери термін — *8 годин*\n\nБільше нічого не треба — бот сам все зробить 🔄",
        parse_mode="Markdown", reply_markup=kb
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 *Статус*\n\nНебезпечних точок: *{len(dangerous_points)}*\nРадіус алерту: *{config.ALERT_RADIUS_METERS}м*\nКанали: {', '.join(config.CHANNELS)}",
        parse_mode="Markdown"
    )


async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not dangerous_points:
        await update.message.reply_text("Точок поки немає.")
        return
    lines = [f"📍 *Активні точки ({len(dangerous_points)}):*\n"]
    for i, pt in enumerate(dangerous_points[:20], 1):
        lines.append(f"{i}. [{pt['address']}]({maps_link(pt['lat'], pt['lng'])})")
    if len(dangerous_points) > 20:
        lines.append(f"\n_...та ще {len(dangerous_points) - 20}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not dangerous_points:
        await update.message.reply_text("Точок поки немає.")
        return
    pts_str = "\n".join(f"• [{pt['address']}]({maps_link(pt['lat'], pt['lng'])})" for pt in dangerous_points[:15])
    await update.message.reply_text(f"🗺 *Всі точки:*\n\n{pts_str}", parse_mode="Markdown", disable_web_page_preview=True)



# Зберігаємо останню відому локацію користувача
user_locations: dict[int, tuple[float, float]] = {}


async def cmd_nearby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not dangerous_points:
        await update.message.reply_text("Точок поки немає — канали ще не зчитані.")
        return

    loc = user_locations.get(user_id)
    if not loc:
        await update.message.reply_text(
            "📍 Спочатку увімкни Live Location щоб я знав де ти.\n"
            "Або відправ разову геопозицію (📎 → Геопозиція)."
        )
        return

    user_lat, user_lng = loc
    with_dist = []
    for pt in dangerous_points:
        dist = haversine_meters(user_lat, user_lng, pt["lat"], pt["lng"])
        with_dist.append((dist, pt))

    with_dist.sort(key=lambda x: x[0])

    lines = ["📊 *Небезпечні точки поруч:*\n"]
    for dist, pt in with_dist[:15]:
        if dist < 500:
            icon = "🔴"
        elif dist < 1500:
            icon = "🟡"
        else:
            icon = "🟢"
        lines.append(f"{icon} *{int(dist)}м* — [{pt['address']}]({maps_link(pt['lat'], pt['lng'])})")

    lines.append(f"\n_Всього точок: {len(dangerous_points)}_")
    lines.append(f"_🔴<500м  🟡<1.5км  🟢далі_")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# ── WEB SERVER для веб-додатку ──────────────────────────────────────────────

async def start_web_server():
    async def handle_points(request):
        return web.json_response(
            {"points": dangerous_points},
            headers={"Access-Control-Allow-Origin": "*"}
        )
    async def handle_health(request):
        return web.Response(text="ok")

    webapp = web.Application()
    webapp.router.add_get("/points", handle_points)
    webapp.router.add_get("/", handle_health)
    runner = web.AppRunner(webapp)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info(f"Web server on port {port}")


# ── MAIN ────────────────────────────────────────────────────────────────────

async def main_async():
    await start_web_server()

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("points", cmd_points))
    app.add_handler(CommandHandler("map", cmd_map))
    app.add_handler(CommandHandler("nearby", cmd_nearby))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_location))

    jq: JobQueue = app.job_queue
    jq.run_repeating(job_update_points, interval=config.UPDATE_INTERVAL_MINUTES * 60, first=10)
    jq.run_daily(job_midnight_reset, time=dtime(hour=21, minute=0))

    log.info("Bot started")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main_async())
