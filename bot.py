import os
import sqlite3
import requests
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NOVA_POSHTA_API_KEY = os.getenv("NOVA_POSHTA_API_KEY")
ALLOWED_TELEGRAM_ID = os.getenv("ALLOWED_TELEGRAM_ID")

DB_PATH = os.getenv("DB_PATH", "shipments.db")
NP_API_URL = "https://api.novaposhta.ua/v2.0/json/"


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            number TEXT PRIMARY KEY,
            phone TEXT,
            last_status TEXT,
            status_code TEXT,
            sender TEXT,
            description TEXT,
            warehouse TEXT,
            received INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def is_allowed(update: Update) -> bool:
    if not ALLOWED_TELEGRAM_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_TELEGRAM_ID)


def np_track(number: str, phone: str = ""):
    payload = {
        "apiKey": NOVA_POSHTA_API_KEY,
        "modelName": "TrackingDocument",
        "calledMethod": "getStatusDocuments",
        "methodProperties": {
            "Documents": [
                {
                    "DocumentNumber": number,
                    "Phone": phone
                }
            ]
        }
    }

    response = requests.post(NP_API_URL, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        return None, data

    items = data.get("data", [])
    if not items:
        return None, data

    return items[0], data


def save_or_update_shipment(item, number: str, phone: str = ""):
    status = item.get("Status", "")
    status_code = str(item.get("StatusCode", ""))
    sender = item.get("SenderFullNameEW") or item.get("Sender") or ""
    description = item.get("CargoDescriptionString") or item.get("CargoDescription") or ""
    warehouse = item.get("WarehouseRecipient") or item.get("WarehouseRecipientAddress") or ""
    received = 1 if "отриман" in status.lower() or status_code == "9" else 0

    conn = db()
    conn.execute("""
        INSERT INTO shipments (
            number, phone, last_status, status_code, sender, description, warehouse, received
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(number) DO UPDATE SET
            phone = excluded.phone,
            last_status = excluded.last_status,
            status_code = excluded.status_code,
            sender = excluded.sender,
            description = excluded.description,
            warehouse = excluded.warehouse,
            received = excluded.received
    """, (number, phone, status, status_code, sender, description, warehouse, received))
    conn.commit()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await update.message.reply_text(
        "Бот працює.\n\n"
        f"Твій Telegram ID: {user_id}\n\n"
        "Щоб додати ТТН:\n"
        "/add 20450000000000\n\n"
        "Якщо Нова пошта попросить телефон:\n"
        "/add 20450000000000 380XXXXXXXXX\n\n"
        "Список команд:\n"
        "/list - активні ТТН\n"
        "/check - перевірити зараз"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if len(context.args) < 1:
        await update.message.reply_text("Напиши так: /add 20450000000000")
        return

    number = context.args[0].strip()
    phone = context.args[1].strip() if len(context.args) >= 2 else ""

    await update.message.reply_text("Перевіряю ТТН...")

    try:
        item, raw = np_track(number, phone)
    except Exception as e:
        await update.message.reply_text(f"Не вдалося звернутися до Нової пошти: {e}")
        return

    if not item:
        await update.message.reply_text("Не знайшов цю ТТН. Перевір номер або додай телефон після номера.")
        return

    save_or_update_shipment(item, number, phone)

    status = item.get("Status", "Статус невідомий")
    sender = item.get("SenderFullNameEW") or item.get("Sender") or "Не вказано"
    description = item.get("CargoDescriptionString") or item.get("CargoDescription") or "Не вказано"

    await update.message.reply_text(
        f"Додав ТТН:\n\n"
        f"{number}\n"
        f"Статус: {status}\n"
        f"Відправник: {sender}\n"
        f"Опис: {description}"
    )


async def list_shipments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    conn = db()
    rows = conn.execute("""
        SELECT number, last_status, sender, description, warehouse
        FROM shipments
        WHERE received = 0
        ORDER BY number
    """).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Активних неотриманих ТТН немає.")
        return

    text = "Активні ТТН:\n\n"
    for number, status, sender, description, warehouse in rows:
        text += f"{number}\n"
        text += f"Статус: {status or 'невідомо'}\n"
        if sender:
            text += f"Відправник: {sender}\n"
        if description:
            text += f"Опис: {description}\n"
        if warehouse:
            text += f"Де: {warehouse}\n"
        text += "\n"

    await update.message.reply_text(text[:4000])


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    count = await check_all_shipments(context.application, notify=True)
    await update.message.reply_text(f"Перевірив. Активних ТТН: {count}")


async def check_all_shipments(application: Application, notify: bool = True):
    conn = db()
    rows = conn.execute("""
        SELECT number, phone, last_status
        FROM shipments
        WHERE received = 0
    """).fetchall()
    conn.close()

    chat_id = ALLOWED_TELEGRAM_ID
    changed = 0

    for number, phone, old_status in rows:
        try:
            item, raw = np_track(number, phone or "")
        except Exception:
            continue

        if not item:
            continue

        new_status = item.get("Status", "")
        save_or_update_shipment(item, number, phone or "")

        if notify and chat_id and new_status and new_status != old_status:
            changed += 1

            sender = item.get("SenderFullNameEW") or item.get("Sender") or "Не вказано"
            description = item.get("CargoDescriptionString") or item.get("CargoDescription") or "Не вказано"
            warehouse = item.get("WarehouseRecipient") or item.get("WarehouseRecipientAddress") or ""

            text = (
                f"Оновлення по ТТН:\n\n"
                f"{number}\n"
                f"Статус: {new_status}\n"
                f"Відправник: {sender}\n"
                f"Опис: {description}"
            )

            if warehouse:
                text += f"\nДе: {warehouse}"

            await application.bot.send_message(chat_id=chat_id, text=text)

    return len(rows)


async def daily_waiting_report(application: Application):
    if not ALLOWED_TELEGRAM_ID:
        return

    conn = db()
    rows = conn.execute("""
        SELECT number, last_status, sender, description, warehouse
        FROM shipments
        WHERE received = 0
    """).fetchall()
    conn.close()

    waiting = []
    for number, status, sender, description, warehouse in rows:
        status_lower = (status or "").lower()
        if "прибув" in status_lower or "відділен" in status_lower or "поштомат" in status_lower:
            waiting.append((number, status, sender, description, warehouse))

    if not waiting:
        await application.bot.send_message(
            chat_id=ALLOWED_TELEGRAM_ID,
            text="Зараз немає посилок, які чекають у відділенні або поштоматі."
        )
        return

    text = "Посилки, які чекають на отримання:\n\n"

    for number, status, sender, description, warehouse in waiting:
        text += f"{number}\n"
        text += f"Статус: {status or 'невідомо'}\n"
        if sender:
            text += f"Відправник: {sender}\n"
        if description:
            text += f"Опис: {description}\n"
        if warehouse:
            text += f"Де: {warehouse}\n"
        text += "\n"

    await application.bot.send_message(chat_id=ALLOWED_TELEGRAM_ID, text=text[:4000])


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    await check_all_shipments(context.application, notify=True)


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    await daily_waiting_report(context.application)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Помилка: у файлі .env немає TELEGRAM_BOT_TOKEN")
        return

    if not NOVA_POSHTA_API_KEY:
        print("Помилка: у файлі .env немає NOVA_POSHTA_API_KEY")
        return

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("list", list_shipments))
    app.add_handler(CommandHandler("check", check_now))

    app.job_queue.run_repeating(
        scheduled_check,
        interval=15 * 60,
        first=10
    )

    app.job_queue.run_daily(
        scheduled_report,
        time=time(hour=9, minute=30, tzinfo=ZoneInfo("Europe/Kyiv"))
    )

    app.job_queue.run_daily(
        scheduled_report,
        time=time(hour=15, minute=30, tzinfo=ZoneInfo("Europe/Kyiv"))
    )

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()