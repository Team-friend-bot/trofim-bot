import asyncio
import os
import json
import logging
from datetime import date, datetime

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])

logger.info(f"Bot starting. OWNER_ID={OWNER_ID}")

db = Database()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def parse_task_with_claude(message_text: str) -> dict:
    today = date.today().isoformat()
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=f"""Ти аналізуєш повідомлення менеджера команди. Сьогодні {today}.
Визнач чи є у повідомленні задача для конкретного члена команди з дедлайном.
Поверни ТІЛЬКИ JSON без пояснень:
{{
  "has_task": true/false,
  "assignee": "ім'я виконавця або null",
  "task": "опис задачі або null",
  "deadline": "YYYY-MM-DD або null"
}}
Якщо дедлайн відносний ("завтра", "п'ятниця", "до кінця тижня") — конвертуй у дату.
Якщо немає чіткого виконавця АБО дедлайну — has_task: false.""",
        messages=[{"role": "user", "content": message_text}],
    )
    try:
        return json.loads(response.content[0].text)
    except Exception:
        return {"has_task": False}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.message.from_user.id
    logger.info(f"Message from user_id={user_id}, OWNER_ID={OWNER_ID}")

    if user_id != OWNER_ID:
        return

    text = update.message.text
    chat_id = update.message.chat_id
    logger.info(f"Processing message: {text[:50]}")

    result = await asyncio.to_thread(parse_task_with_claude, text)
    logger.info(f"Claude result: {result}")

    if result.get("has_task") and result.get("deadline") and result.get("assignee"):
        task_id = db.add_task(
            chat_id=chat_id,
            task_text=result["task"],
            assignee=result["assignee"],
            deadline=result["deadline"],
            created_by=update.message.from_user.first_name,
        )
        deadline_fmt = datetime.strptime(result["deadline"], "%Y-%m-%d").strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"✅ Задача #{task_id} зафіксована\n"
            f"👤 {result['assignee']}\n"
            f"📋 {result['task']}\n"
            f"📅 Дедлайн: {deadline_fmt}"
        )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = db.get_active_tasks(update.message.chat_id)
    if not tasks:
        await update.message.reply_text("Активних задач немає ✨")
        return

    lines = ["📋 *Активні задачі:*\n"]
    for t in tasks:
        deadline = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        days_left = (deadline - date.today()).days
        if days_left < 0:
            icon = "🔴"
        elif days_left == 0:
            icon = "🟡"
        elif days_left <= 1:
            icon = "🟠"
        else:
            icon = "🟢"
        lines.append(
            f"{icon} #{t['id']} | {t['assignee']} | {t['task']} | {deadline.strftime('%d.%m.%Y')}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Використання: /done <id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID має бути числом")
        return
    db.mark_done(task_id)
    await update.message.reply_text(f"✅ Задача #{task_id} виконана!")


async def check_deadlines(application: Application):
    tasks = db.get_tasks_for_reminder()
    today = date.today()

    for t in tasks:
        chat_id = t["chat_id"]
        deadline = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        days_left = (deadline - today).days
        deadline_fmt = deadline.strftime("%d.%m.%Y")

        if days_left == 1 and not t["reminded_24h"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Нагадування!*\n\n"
                    f"Завтра дедлайн для *{t['assignee']}*\n"
                    f"📋 {t['task']}\n"
                    f"📅 {deadline_fmt}\n\n"
                    f"Виконано? → /done {t['id']}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded_24h(t["id"])

        elif days_left == 0 and not t["reminded_0h"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚨 *Сьогодні дедлайн!*\n\n"
                    f"*{t['assignee']}*, твоя задача:\n"
                    f"📋 {t['task']}\n\n"
                    f"Виконано? → /done {t['id']}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded_0h(t["id"])

        elif days_left < 0 and not t["reminded_overdue"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔴 *Прострочено!*\n\n"
                    f"*{t['assignee']}* не виконав задачу:\n"
                    f"📋 {t['task']}\n"
                    f"📅 Дедлайн був: {deadline_fmt}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded_overdue(t["id"])


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_deadlines, "interval", hours=1, args=[app])
    scheduler.start()

    logger.info("team_friend_bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
