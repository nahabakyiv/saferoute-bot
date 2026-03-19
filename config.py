# ═══════════════════════════════════════════════════════════
# НАЛАШТУВАННЯ — заповни перед деплоєм
# ═══════════════════════════════════════════════════════════

import os

# --- Telegram Bot (від @BotFather) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

# --- Telegram API (з my.telegram.org) ---
# Потрібно щоб читати публічні канали
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
PHONE = os.getenv("PHONE", "+380XXXXXXXXX")  # твій номер (потрібен тільки для auth.py)

# --- Telethon session string (генерується через auth.py) ---
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "")

# --- Канали для моніторингу ---
CHANNELS = [
    os.getenv("CHANNEL_1", "@channel_username_1"),
    os.getenv("CHANNEL_2", "@channel_username_2"),
]

# --- Радіус алерту в метрах ---
ALERT_RADIUS_METERS = 500

# --- Місто за замовчуванням (для геокодування) ---
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Київ")
COUNTRY_CODE = "ua"

# --- Інтервал оновлення точок (хвилини) ---
UPDATE_INTERVAL_MINUTES = 3
