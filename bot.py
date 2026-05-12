import asyncio
import os
import json
import logging
from datetime import datetime, date

import anthropic
import google.generativeai as genai
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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])

logger.info(f"Bot starting. OWNER_ID={OWNER_ID}")

db = Database()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")


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


def parse_task_with_claude(message_text: str) -> dict:
    today = date.today().isoformat()
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"""Ти асистент менеджера команди. Сьогодні {today}.

Розпізнавай делегування задач членам команди в повідомленнях.

Поверни ТІЛЬКИ JSON без пояснень:
{{
  "has_task": true/false,
  "assignee": "ім'я або null",
  "task": "опис задачі або null",
  "deadline": "YYYY-MM-DDTHH:MM:SS або null"
}}

ПРАВИЛА:
- Якщо є ім'я + дія + дата/час → has_task: true
- "20 травня" → YYYY-05-20 (поточний рік)
- "завтра" → завтрашня дата
- "п'ятниця" → найближча п'ятниця
- Час за замовчуванням 18:00 якщо не вказано
- has_task: false якщо немає імені АБО немає дати

ПРИКЛАДИ:
"Андрій підготуй звіт до 20 травня" → {{"has_task": true, "assignee": "Андрій", "task": "підготувати звіт", "deadline": "2026-05-20T18:00:00"}}
"Сергій зроби аналіз до п'ятниці 15:00" → {{"has_task": true, "assignee": "Сергій", "task": "зробити аналіз", "deadline": "2026-05-15T15:00:00"}}
"Команда, обговоримо завтра" → {{"has_task": false, "assignee": null, "task": null, "deadline": null}}""",
        messages=[{"role": "user", "content": message_text}],
    )
    raw = response.content[0].text
    logger.info(f"Claude raw: {raw}")
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end+1])
    except Exception as e:
        logger.error(f"Parse error: {e}")
    return {"has_task": False}


def parse_voice_with_gemini(audio_path: str) -> dict:
    today = date.today().isoformat()
    prompt = f"""Сьогодні {today}.
Прослухай голосове повідомлення українською і витягни задачу.

Поверни ТІЛЬКИ JSON без пояснень:
{{
  "has_task": true/false,
  "assignee": "ім'я або null",
  "task": "опис задачі або null",
  "deadline": "YYYY-MM-DDTHH:MM:SS або null"
}}

ПРАВИЛА:
- Якщо є ім'я + дія + дата/час → has_task: true
- "20 травня" → YYYY-05-20 (поточний рік {today[:4]})
- "завтра" → завтрашня дата
- Час за замовчуванням 18:00 якщо не вказано
- has_task: false якщо немає імені АБО немає дати"""

    audio_file = genai.upload_file(audio_path, mime_type="audio/ogg")
    response = gemini_model.generate_content([prompt, audio_file])
    raw = response.text
    logger.info(f"Gemini raw: {raw}")
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end+1])
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
    return {"has_task": False}


async def save_and_reply(update: Update, result: dict, source: str = ""):
    if not (result.get("has_task") and result.get("deadline") and result.get("assignee")):
        return False

    try:
        deadline = datetime.fromisoformat(result["deadline"])
    except (ValueError, TypeError):
        return False

    task_id = db.add_task(
        chat_id=update.message.chat_id,
        task_text=result["task"],
        assignee=result["assignee"],
        deadline=deadline.isoformat(),
        created_by=update.message.from_user.first_name,
    )

    suffix = f" ({source})" if source else ""
    await update.message.reply_text(
        f"✅ Задача #{task_id} зафіксована{suffix}\n"
        f"👤 {result['assignee']}\n"
        f"📋 {result['task']}\n"
        f"📅 {deadline.strftime('%d.%m.%Y %H:%M')}"
    )
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.message.from_user.id != OWNER_ID:
        return

    text = update.message.text
    logger.info(f"Processing: {text[:60]}")
    result = await asyncio.to_thread(parse_task_with_claude, text)
    logger.info(f"Result: {result}")
    await save_and_reply(update, result)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    if update.message.from_user.id != OWNER_ID:
        return

    voice = update.message.voice
    audio_path = f"/tmp/{voice.file_id}.ogg"

    try:
        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(audio_path)
        logger.info(f"Voice downloaded: {audio_path}")

        result = await asyncio.to_thread(parse_voice_with_gemini, audio_path)
        logger.info(f"Voice result: {result}")

        saved = await save_and_reply(update, result, source="з голосу")
        if not saved:
            await update.message.reply_text(
                "🎤 Не вдалось розпізнати задачу. Спробуй чіткіше назвати ім'я і дедлайн."
            )
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ Помилка обробки голосу: {e}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


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
        await update.message.reply_text("❌ Невірний формат дати.\nПриклад: 20.05.2026 17:00")
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
                text=f"⏰ *Нагадування за 24 години*\n\n*{t['assignee']}*\n📋 {t['task_text']}\n📅 Дедлайн: {deadline_fmt}\n\nВиконано? → /done {t['id']}",
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_1d")
        elif 110 <= minutes_left <= 130 and not t["reminded_2h"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ *Нагадування за 2 години!*\n\n*{t['assignee']}*\n📋 {t['task_text']}\n📅 Дедлайн: {deadline_fmt}",
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_2h")
        elif 10 <= minutes_left <= 20 and not t["reminded_15m"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *15 хвилин до дедлайну!*\n\n*{t['assignee']}*\n📋 {t['task_text']}",
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_15m")
        elif minutes_left < 0 and not t["reminded_overdue"]:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"🔴 *Прострочено!*\n\n*{t['assignee']}* не виконав:\n📋 {t['task_text']}\n📅 Дедлайн був: {deadline_fmt}",
                parse_mode="Markdown",
            )
            db.mark_reminded(t["id"], "reminded_overdue")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
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
