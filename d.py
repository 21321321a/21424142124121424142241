#!/usr/bin/env python3
# d.py — упрощённая версия: один SOCKS5 (первый в proxies.txt) -> сразу send_code_request

import os
import asyncio
import socks
from flask import Flask
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- Конфиг ----------
PROXIES_FILE = "proxies.txt"
CONNECT_TIMEOUT = 15.0
SEND_CODE_TIMEOUT = 15.0

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("Нужно задать BOT_TOKEN, API_ID, API_HASH в окружении (Replit Secrets).")

# ---------- Помощники ----------
def parse_proxy_line(line: str):
    # форматы: ip:port  или ip:port:user:pass
    parts = line.strip().split(":")
    if len(parts) < 2:
        return None
    host = parts[0].strip()
    try:
        port = int(parts[1].strip())
    except Exception:
        return None
    user = None
    pwd = None
    if len(parts) >= 3 and parts[2].strip() != "":
        user = parts[2].strip()
    if len(parts) >= 4 and parts[3].strip() != "":
        pwd = parts[3].strip()
    return (host, port, user, pwd)

def load_first_proxy(filename=PROXIES_FILE):
    if not os.path.exists(filename):
        return None
    with open(filename, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            p = parse_proxy_line(s)
            if p:
                return p
    return None

# ---------- Telethon: отправка через один прокси ----------
async def send_once_via_proxy(phone: str, host: str, port: int, user: str = None, pwd: str = None) -> (bool, str):
    """
    Пытается подключиться через указанный SOCKS5 и сразу вызвать send_code_request(phone).
    Возвращает (success: bool, message: str).
    """
    if user:
        proxy_tuple = (socks.SOCKS5, host, port, True, user, pwd)
    else:
        proxy_tuple = (socks.SOCKS5, host, port)

    session_name = f"session_{host.replace('.', '_')}_{port}"
    client = TelegramClient(session_name, API_ID, API_HASH, proxy=proxy_tuple)

    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except Exception as e:
        try:
            await client.disconnect()
        except:
            pass
        return False, f"Ошибка подключения к прокси {host}:{port} -> {e!r}"

    try:
        # Без проверки is_user_authorized - сразу отправляем код
        try:
            await asyncio.wait_for(client.send_code_request(phone), timeout=SEND_CODE_TIMEOUT)
            return True, f"Код отправлен через {host}:{port}"
        except FloodWaitError as fe:
            return False, f"FloodWait: нужно ждать {fe.seconds} секунд."
        except Exception as e:
            return False, f"Ошибка при send_code_request через {host}:{port} -> {e!r}"
    finally:
        try:
            await client.disconnect()
        except:
            pass

# ---------- Handlers бота ----------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли номер в формате +79998887766 — отправлю запрос через первый SOCKS5 из proxies.txt.")

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await update.message.reply_text("Неверный формат номера. Пример: +79998887766")
        return

    proxy = load_first_proxy()
    if not proxy:
        await update.message.reply_text("Нет доступного proxy: файл proxies.txt пуст или не содержит валидных строк.")
        return

    host, port, user, pwd = proxy
    await update.message.reply_text(f"Использую прокси {host}:{port} (auth={bool(user)}). Выполняю отправку кода...")

    success, msg = await send_once_via_proxy(phone, host, port, user, pwd)
    await update.message.reply_text(msg)

# ---------- Flask healthcheck (опционально) ----------
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)

# ---------- Запуск ----------
def main():
    # запускаем Flask фоново (Replit healthcheck)
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    print("Бот запускается (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
