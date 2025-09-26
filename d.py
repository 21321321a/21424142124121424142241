#!/usr/bin/env python3
# d.py — Replit-ready: Telegram bot + Telethon send_code_request via SOCKS5 (with optional auth) + Flask healthcheck

import os
import asyncio
import threading
import socks   # PySocks
from flask import Flask
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ====== Конфиг (можно менять) ======
PROXIES_FILE = "proxies.txt"       # строки: ip:port  или ip:port:user:pass
OK_PROXIES_FILE = "ok_proxies.txt"

CONNECT_TIMEOUT = 15.0
SEND_CODE_TIMEOUT = 15.0
IS_AUTH_TIMEOUT = 5.0
MAX_SEND_PER_REQUEST = 25
SEND_CONCURRENCY = 4
DELAY_BETWEEN_TASKS = 0.2   # секунды между стартами задач

# Переменные окружения (на Replit задайте в Secrets)
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")

# Небольшой fallback (необязательно) - если вы хотите использовать жестко захардкоженные значения,
# раскомментируйте и укажите свои, но лучше через окружение.
# if not BOT_TOKEN:
#     BOT_TOKEN = "123:ABC..."
# if not API_ID:
#     API_ID = 27503668
# if not API_HASH:
#     API_HASH = "f654d14ed2b963765ba629d1352dacf5"

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("Нужно задать BOT_TOKEN, API_ID, API_HASH в окружении (Replit Secrets или env).")

# ====== Помощники ======
def parse_proxy_line(line: str):
    """
    Возвращает (host, port, user, pwd) — user/pwd могут быть None.
    Поддерживается формат:
      ip:port
      ip:port:user:pass
    Игнорирует пустые строки и комментарии (#).
    """
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
    if len(parts) >= 3:
        # если есть хотя бы 3 части, то третья часть может быть user (а четвертая — pwd)
        # поддерживаем оба варианта: ip:port:user  (тогда pwd=None) и ip:port:user:pass
        user = parts[2].strip() if parts[2].strip() != "" else None
    if len(parts) >= 4:
        pwd = parts[3].strip() if parts[3].strip() != "" else None
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
    """
    Возвращает True если send_code_request успешно вызван (т.е. код отправлен).
    Подключается к Telegram через Telethon, затем отключается.
    """
    # Telethon ожидает прокси-формат похожий на (socks.SOCKS5, host, port, rdns, username, password)
    if user:
        proxy_tuple = (socks.SOCKS5, host, port, True, user, pwd)
    else:
        proxy_tuple = (socks.SOCKS5, host, port)

    # Используем уникальное имя сессии по proxy, чтобы не мешать другим
    session_name = f"session_{host.replace('.', '_')}_{port}"
    client = TelegramClient(session_name, API_ID, API_HASH, proxy=proxy_tuple)

    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except Exception as e:
        try:
            await client.disconnect()
        except: 
            pass
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
        else:
            print(f"[already auth] client via {host}:{port} reports already authorized (skipped).")
    finally:
        try:
            await client.disconnect()
        except:
            pass

    return False

# ====== Handlers бота ======
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли номер в формате +79998887766 (только цифры и ведущий '+').")

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await update.message.reply_text("Неверный формат. Пример: +79998887766")
        return

    await update.message.reply_text(f"Принял {phone}. Начинаю попытки через SOCKS5-прокси... (макс {MAX_SEND_PER_REQUEST})")

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
        await sem.acquire()
        try:
            print(f"Пробую прокси {host}:{port} (auth={bool(user)})")
            ok = await try_send_via_socks(phone, host, port, user, pwd)
            if ok:
                sent += 1
                ok_list.append(f"{host}:{port}" + (f":{user}:{pwd}" if user else ""))
        finally:
            sem.release()

    tasks = []
    for host, port, user, pwd in to_try:
        tasks.append(asyncio.create_task(worker(host, port, user, pwd)))
        # небольшая пауза между старта задач, чтобы не запускать все одновременно
        await asyncio.sleep(DELAY_BETWEEN_TASKS)

    if tasks:
        await asyncio.gather(*tasks)

    # сохраняем удачные прокси (без паролей, если хотите — можно изменить)
    if ok_list:
        with open(OK_PROXIES_FILE, "w", encoding="utf-8") as f:
            for line in ok_list:
                f.write(line + "\n")

    await update.message.reply_text(f"Готово. Попыток отправки кода (успешных): {sent}. Успешные прокси: {len(ok_list)}.")

# ====== Flask для healthcheck (фон) ======
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def index():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", "10000"))
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
