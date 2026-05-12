import asyncio
import os
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

logger.info(f"Bot starting. OWNER_ID={OWNER_ID}")

db = Database()


def parse_deadline(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%d.%m.%Y", "%d.%m"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse: {s}")


async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return

    text = " ".join(context.args)
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        await update.message.reply_text(
            "📝 Формат: /task ім'я | опис | дд.мм.рррр гг:хх\n\n"
            "Приклад:\n"
            "/task Андрій | підготувати звіт | 20.05.2026 17:00"
        )
        return

    assignee, task_text, deadline_str = parts

    try:
        deadline = parse_deadline(deadline_str)
    except ValueError:
        await update.message.reply_text(
            "❌ Невірний формат дати.\nПриклад: 20.05.2026 17:00"
        )
        return

    task_id = db.add_task(
        chat_id=update.message.chat_id,
        task_text=task_text,
        assignee=assignee,
        deadline=deadline.isoformat(),
        created_by=update.message.from_user.first_name,
    )

    await update.message.reply_text(
        f"✅ Задача #{task_id} створена\n"
        f"👤 {assignee}\n"
        f"📋 {task_text}\n"
        f"📅 {deadline.strftime('%d.%m.%Y %H:%M')}"
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = db.get_active_tasks(update.message.chat_id)
    if not tasks:
        await update.message.reply_text("Активних задач немає ✨")
        return

    lines = ["📋 *Активні задачі:*\n"]
    now = datetime.now()
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        hours_left = (deadline - now).total_seconds() / 3600

        if hours_left < 0:
            icon = "🔴"
        elif hours_left <= 2:
            icon = "🟠"
        elif hours_left <= 24:
            icon = "🟡"
        else:
            icon = "🟢"

        lines.append(
            f"{icon} #{t['id']} | {t['assignee']} | {t['task_text']} | {deadline.strftime('%d.%m %H:%M')}"
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats(update.message.chat_id)
    if not stats:
        await update.message.reply_text("Немає даних для статистики")
        return

    lines = ["📊 *Статистика виконання:*\n"]
    for s in stats:
        rate = (s["on_time"] / s["total"] * 100) if s["total"] else 0
        lines.append(
            f"👤 *{s['assignee']}*\n"
            f"   Всього: {s['total']} | Вчасно: {s['on_time']} | "
            f"Запізно: {s['late']} | Прострочено: {s['overdue']}\n"
            f"   Ефективність: {rate:.0f}%\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def check_deadlines(application: Application):
    tasks = db.get_tasks_for_reminder()
    now = datetime.now()

    for t in tasks:
        chat_id = t["chat_id"]
        deadline = datetime.fromisoformat(t["deadline"])
        minutes_left = (deadline - now).total_seconds() / 60
        deadline_fmt = deadline.strftime("%d.%m.%Y %H:%M")

        if 23 * 60 <= minutes_left <= 25 * 60 and not t["reminded_1d"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Нагадування за 24 години*\n\n"
                    f"*{t['assignee']}*, твоя задача:\n"
                    f"📋 {t['task_text']}\n"
                    f"📅 Дедлайн: {deadline_fmt}\n\n"
                    f"Виконано? → /done {t['id']}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_1d")

        elif 110 <= minutes_left <= 130 and not t["reminded_2h"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Нагадування за 2 години!*\n\n"
                    f"*{t['assignee']}*\n"
                    f"📋 {t['task_text']}\n"
                    f"📅 Дедлайн: {deadline_fmt}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_2h")

        elif 10 <= minutes_left <= 20 and not t["reminded_15m"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚨 *15 хвилин до дедлайну!*\n\n"
                    f"*{t['assignee']}*\n"
                    f"📋 {t['task_text']}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_15m")

        elif minutes_left < 0 and not t["reminded_overdue"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔴 *Прострочено!*\n\n"
                    f"*{t['assignee']}* не виконав:\n"
                    f"📋 {t['task_text']}\n"
                    f"📅 Дедлайн був: {deadline_fmt}"
                ),
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_overdue")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("stats", stats_command))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_deadlines, "interval", minutes=5, args=[app])
    scheduler.start()

    logger.info("trofim_bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
