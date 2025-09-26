# d.py — Replit-ready, SOCKS5 (with auth) support
import os
import asyncio
import threading
import socks
from flask import Flask
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ====== Конфиг ======
PROXIES_FILE = "proxies.txt"       # ip:port or ip:port:user:pass
OK_PROXIES_FILE = "ok_proxies.txt"

CONNECT_TIMEOUT = 15.0
SEND_CODE_TIMEOUT = 15.0
IS_AUTH_TIMEOUT = 5.0
MAX_SEND_PER_REQUEST = 25
SEND_CONCURRENCY = 4
DELAY_BETWEEN_TASKS = 0.2

# Переменные окружения (задать в Replit Secrets)
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("Нужно задать BOT_TOKEN, API_ID, API_HASH в окружении")

# ====== Помощники ======
def parse_proxy_line(line: str):
    """Возвращает (host, port, user, pwd) — user/pwd могут быть None."""
    parts = line.strip().split(":")
    if len(parts) < 2:
        return None
    host = parts[0].strip()
    try:
        port = int(parts[1].strip())
    except:
        return None
    user = parts[2].strip() if len(parts) >= 4 else None
    pwd = parts[3].strip() if len(parts) >= 4 else None
    return (host, port, user, pwd)

def load_proxies(filename=PROXIES_FILE):
    res = []
    if not os.path.exists(filename):
        return res
    with open(filename, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            p = parse_proxy_line(s)
            if p:
                res.append(p)
    return res

# ====== Telethon — попытка отправить код через SOCKS5-прокси ======
async def try_send_via_socks(phone: str, host: str, port: int, user: str = None, pwd: str = None) -> bool:
    proxy_tuple = (socks.SOCKS5, host, port, True, user, pwd) if user else (socks.SOCKS5, host, port)
    session_name = f"session_{host.replace('.', '_')}_{port}"
    client = TelegramClient(session_name, API_ID, API_HASH, proxy=proxy_tuple)

    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except Exception as e:
        try:
            await client.disconnect()
        except: pass
        print(f"[connect fail] {host}:{port} -> {repr(e)}")
        return False

    try:
        try:
            is_auth = await asyncio.wait_for(client.is_user_authorized(), timeout=IS_AUTH_TIMEOUT)
        except Exception:
            is_auth = False

        if not is_auth:
            try:
                await asyncio.wait_for(client.send_code_request(phone), timeout=SEND_CODE_TIMEOUT)
                print(f"[ok] send_code_request via {host}:{port}")
                return True
            except FloodWaitError as fe:
                print(f"[floodwait] {host}:{port} -> wait {fe.seconds}s")
            except Exception as e:
                print(f"[send fail] {host}:{port} -> {repr(e)}")
    finally:
        try:
            await client.disconnect()
        except: pass

    return False

# ====== Handlers бота ======
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли номер в формате +79998887766")

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await update.message.reply_text("Неверный формат. Пример: +79998887766")
        return

    await update.message.reply_text(f"Принял {phone}. Начинаю попытки через SOCKS5...")

    proxies = load_proxies()
    if not proxies:
        await update.message.reply_text("Файл proxies.txt пуст или отсутствует.")
        return

    to_try = proxies[:MAX_SEND_PER_REQUEST]
    sem = asyncio.Semaphore(SEND_CONCURRENCY)
    ok_list = []
    sent = 0

    async def worker(host, port, user, pwd):
        nonlocal sent
        async with sem:
            print(f"Пробую прокси {host}:{port} (user={bool(user)})")
            ok = await try_send_via_socks(phone, host, port, user, pwd)
            if ok:
                sent += 1
                ok_list.append(f"{host}:{port}")

    tasks = []
    for host, port, user, pwd in to_try:
        tasks.append(asyncio.create_task(worker(host, port, user, pwd)))
        await asyncio.sleep(DELAY_BETWEEN_TASKS)

    if tasks:
        await asyncio.gather(*tasks)

    if ok_list:
        with open(OK_PROXIES_FILE, "w", encoding="utf-8") as f:
            for line in ok_list:
                f.write(line + "\n")

    await update.message.reply_text(f"Готово. Попыток отправки кода: {sent}. Успешные прокси: {len(ok_list)}.")

# ====== Flask для healthcheck (фон) ======
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    # Replit м.б. использует 0.0.0.0:10000 — нормально
    flask_app.run(host="0.0.0.0", port=port)

# ====== Запуск бота (главный поток) ======
def main():
    # стартуем Flask в фоне
    threading.Thread(target=run_flask, daemon=True).start()

    # строим и запускаем Telegram-бот (в главном потоке)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    print("Бот запускается (polling)...")
    # run_polling() должен быть в главном потоке — так корректно с сигналами
    app.run_polling()

if __name__ == "__main__":
    main()
