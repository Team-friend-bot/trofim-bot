import asyncio
import os
import json
import logging
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo

import anthropic
import google.generativeai as genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ).replace(tzinfo=None)


def today_kyiv() -> date:
    return now_kyiv().date()
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters,
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
                dt = dt.replace(year=now_kyiv().year)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse: {s}")


def parse_task_with_claude(message_text: str) -> dict:
    today = today_kyiv().isoformat()
    response = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=400,
        system=f"""Ти асистент менеджера команди. Сьогодні {today}.
Розпізнавай делегування задач. Поверни ТІЛЬКИ JSON:
{{"has_task": true/false, "assignee": "ім'я або null", "task": "опис або null", "deadline": "YYYY-MM-DDTHH:MM:SS або null"}}
Час за замовчуванням 18:00. has_task: false якщо немає імені АБО дати.
"Андрій підготуй звіт до 20 травня" → {{"has_task": true, "assignee": "Андрій", "task": "підготувати звіт", "deadline": "{today[:4]}-05-20T18:00:00"}}""",
        messages=[{"role": "user", "content": message_text}],
    )
    raw = response.content[0].text
    logger.info(f"Claude raw: {raw}")
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            return json.loads(raw[s:e+1])
    except Exception as ex:
        logger.error(f"Parse error: {ex}")
    return {"has_task": False}


def parse_voice_with_gemini(audio_path: str) -> dict:
    today = today_kyiv().isoformat()
    prompt = f"""Сьогодні {today}. Прослухай голос і витягни задачу. Поверни ТІЛЬКИ JSON:
{{"has_task": true/false, "assignee": "ім'я", "task": "опис", "deadline": "YYYY-MM-DDTHH:MM:SS"}}
Час за замовчуванням 18:00. Поточний рік {today[:4]}."""
    audio_file = genai.upload_file(audio_path, mime_type="audio/ogg")
    response = gemini_model.generate_content([prompt, audio_file])
    raw = response.text
    logger.info(f"Gemini raw: {raw}")
    try:
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            return json.loads(raw[s:e+1])
    except Exception as ex:
        logger.error(f"Gemini parse error: {ex}")
    return {"has_task": False}


def done_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Виконано", callback_data=f"done:{task_id}")
    ]])


def format_late(deadline: datetime, done_at: datetime) -> str:
    diff = done_at - deadline
    if diff.total_seconds() <= 0:
        return "вчасно ✅"
    total_min = int(diff.total_seconds() / 60)
    if total_min < 60:
        return f"із запізненням {total_min} хв ⚠️"
    hours = total_min // 60
    if hours < 24:
        return f"із запізненням на {hours} год ⚠️"
    days = hours // 24
    return f"із запізненням на {days} дн ⚠️"


def assignee_user_id(task: dict) -> int | None:
    """Try to find user_id of task's assignee."""
    a = task["assignee"]
    if a.startswith("@"):
        m = db.get_member_by_username(task["chat_id"], a)
    else:
        m = db.get_member(task["chat_id"], a)
    return m.get("user_id") if m else None


async def save_and_reply(update, context, result, source=""):
    if not (result.get("has_task") and result.get("deadline") and result.get("assignee")):
        return False
    try:
        deadline = datetime.fromisoformat(result["deadline"])
    except (ValueError, TypeError):
        return False

    chat_id = update.message.chat_id
    member = db.get_member(chat_id, result["assignee"])
    tag = f"@{member['username']}" if member and member.get("username") else result["assignee"]

    task_id = db.add_task(
        chat_id=chat_id, task_text=result["task"], assignee=tag,
        deadline=deadline.isoformat(), created_by=update.message.from_user.first_name,
    )

    deadline_fmt = deadline.strftime("%d.%m.%Y %H:%M")
    suffix = f" ({source})" if source else ""
    kb = done_keyboard(task_id)

    await update.message.reply_text(
        f"✅ Задача #{task_id} зафіксована{suffix}\n👤 {tag}\n📋 {result['task']}\n📅 {deadline_fmt}",
        reply_markup=kb,
    )

    if member and member.get("user_id"):
        try:
            await context.bot.send_message(
                chat_id=member["user_id"],
                text=(
                    f"📌 *Тобі поставлена задача #{task_id}*\n\n"
                    f"📋 {result['task']}\n"
                    f"📅 Дедлайн: {deadline_fmt}\n\n"
                    f"Коли виконаєш — натисни кнопку ⬇️"
                ),
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"DM failed: {e}")
    return True


async def close_task(context, task_id: int, by_user) -> bool:
    """Mark task done and announce in the group. Returns True if closed now."""
    task = db.get_task(task_id)
    if not task or task["is_done"]:
        return False

    db.mark_done(task_id)
    deadline = datetime.fromisoformat(task["deadline"])
    status = format_late(deadline, now_kyiv())

    closer = f"@{by_user.username}" if by_user.username else by_user.first_name

    try:
        await context.bot.send_message(
            chat_id=task["chat_id"],
            text=(
                f"✅ {closer} закрив(ла) задачу #{task_id} {status}\n"
                f"📋 {task['task_text']}"
            ),
        )
    except Exception as e:
        logger.error(f"Group announce failed: {e}")
    return True


async def done_callback(update, context):
    query = update.callback_query
    try:
        task_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        await query.answer()
        return

    task = db.get_task(task_id)
    if not task:
        await query.answer("Задача не знайдена", show_alert=True)
        return
    if task["is_done"]:
        await query.answer("Задача вже виконана")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    user = query.from_user
    user_tag = f"@{user.username}".lower() if user.username else None
    is_owner = user.id == OWNER_ID
    is_assignee = user_tag and task["assignee"].lower() == user_tag
    if not (is_owner or is_assignee):
        await query.answer("Цю задачу може закрити лише виконавець", show_alert=True)
        return

    closed = await close_task(context, task_id, user)
    await query.answer("Задача закрита ✅" if closed else "Вже закрита")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_message(update, context):
    if not update.message or not update.message.text:
        return
    if update.message.from_user.id != OWNER_ID:
        return
    result = await asyncio.to_thread(parse_task_with_claude, update.message.text)
    await save_and_reply(update, context, result)


async def handle_voice(update, context):
    if not update.message or not update.message.voice:
        return
    if update.message.from_user.id != OWNER_ID:
        return
    voice = update.message.voice
    audio_path = f"/tmp/{voice.file_id}.ogg"
    try:
        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(audio_path)
        result = await asyncio.to_thread(parse_voice_with_gemini, audio_path)
        saved = await save_and_reply(update, context, result, source="з голосу")
        if not saved:
            await update.message.reply_text("🎤 Не вдалось розпізнати. Назви ім'я і дедлайн чіткіше.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ Помилка: {e}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


async def start_command(update, context):
    if update.message.chat.type != "private":
        return
    user = update.message.from_user
    rows = 0
    if user.username:
        rows = db.update_user_id_by_username(user.username, user.id)

    if rows > 0:
        await update.message.reply_text(
            f"✅ Привіт, {user.first_name}!\n\n"
            f"Тебе підключено. Тепер задачі від менеджера будуть приходити сюди в особисті.\n\n"
            f"Як виконаєш — натискай кнопку ✅ Виконано під повідомленням."
        )
    else:
        if user.username:
            msg = (
                f"⚠️ Привіт, {user.first_name}!\n\n"
                f"Тебе ще не додано в команду під цим @username: @{user.username}.\n\n"
                f"Попроси менеджера написати в групі:\n"
                f"/add {user.first_name} @{user.username}"
            )
        else:
            msg = (
                f"⚠️ Привіт, {user.first_name}!\n\n"
                f"У тебе не встановлено @username в Telegram. "
                f"Встанови його в налаштуваннях (Settings → Username) і напиши /start знову."
            )
        await update.message.reply_text(msg)


async def add_command(update, context):
    if update.message.from_user.id != OWNER_ID:
        return

    text = update.message.text or ""
    pairs = re.findall(r"/add\s+(\S+)\s+(@?\w+)", text)
    if not pairs:
        await update.message.reply_text(
            "📝 Формат: /add ім'я @username\n\nПриклад: /add Андрій @andrii_smith\n\n"
            "Можна додати кілька — у тому ж повідомленні:\n"
            "/add Андрій @andrii\n/add Олена @olena"
        )
        return

    added = []
    for name, username in pairs:
        username = username.lstrip("@")
        db.add_member(update.message.chat_id, name, username)
        added.append(f"• {name} → @{username}")

    bot_username = context.bot.username
    if len(added) == 1:
        await update.message.reply_text(
            f"✅ Додано:\n{added[0]}\n\n"
            f"⚠️ Скажи учаснику написати /start боту @{bot_username} — "
            f"інакше він не отримає задачі в особисті"
        )
    else:
        await update.message.reply_text(
            f"✅ Додано {len(added)}:\n" + "\n".join(added) +
            f"\n\n⚠️ Кожному треба написати /start боту @{bot_username} — "
            f"інакше задачі в особисті не приходитимуть"
        )


async def team_command(update, context):
    members = db.get_team(update.message.chat_id)
    if not members:
        await update.message.reply_text("Команда порожня.\n\nДодай: /add ім'я @username")
        return
    lines = ["👥 *Команда:*\n"]
    for m in members:
        icon = "✅" if m.get("user_id") else "⚠️"
        lines.append(f"{icon} {m['name']} → @{m['username']}")
    lines.append("\n✅ — отримує задачі в особисті\n⚠️ — ще не написав /start боту")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def remove_command(update, context):
    if update.message.from_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /remove ім'я")
        return
    db.remove_member(update.message.chat_id, context.args[0])
    await update.message.reply_text(f"✅ Видалено: {context.args[0]}")


async def task_command(update, context):
    if update.message.from_user.id != OWNER_ID:
        return
    parts = [p.strip() for p in " ".join(context.args).split("|")]
    if len(parts) != 3:
        await update.message.reply_text("📝 Формат: /task ім'я | опис | дд.мм.рррр гг:хх")
        return
    assignee, task_text, deadline_str = parts
    try:
        deadline = parse_deadline(deadline_str)
    except ValueError:
        await update.message.reply_text("❌ Невірний формат дати")
        return
    result = {"has_task": True, "assignee": assignee, "task": task_text, "deadline": deadline.isoformat()}
    await save_and_reply(update, context, result)


async def tasks_command(update, context):
    tasks = db.get_active_tasks(update.message.chat_id)
    if not tasks:
        await update.message.reply_text("Активних задач немає ✨")
        return
    lines = ["📋 *Активні задачі:*\n"]
    now = now_kyiv()
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        hours_left = (deadline - now).total_seconds() / 3600
        icon = "🔴" if hours_left < 0 else "🟠" if hours_left <= 2 else "🟡" if hours_left <= 24 else "🟢"
        lines.append(
            f"{icon} #{t['id']} | {t['assignee']} | {t['task_text']} | {deadline.strftime('%d.%m %H:%M')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def done_command(update, context):
    if not context.args:
        await update.message.reply_text("Використання: /done <id>")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID має бути числом")
        return
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"❌ Задача #{task_id} не знайдена")
        return
    if task["is_done"]:
        await update.message.reply_text(f"Задача #{task_id} вже виконана")
        return
    await close_task(context, task_id, update.message.from_user)


async def stats_command(update, context):
    stats = db.get_stats(update.message.chat_id)
    if not stats:
        await update.message.reply_text("Немає даних")
        return
    lines = ["📊 *Статистика:*\n"]
    for s in stats:
        rate = (s["on_time"] / s["total"] * 100) if s["total"] else 0
        lines.append(
            f"👤 *{s['assignee']}*\n"
            f"   Всього: {s['total']} | Вчасно: {s['on_time']} | "
            f"Запізно: {s['late']} | Прострочено: {s['overdue']}\n"
            f"   Ефективність: {rate:.0f}%\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def send_reminder(application, task, when_text: str, urgent: bool):
    """Send reminder to group + personal DM. Skips if task already done."""
    fresh = db.get_task(task["id"])
    if not fresh or fresh["is_done"]:
        return

    deadline = datetime.fromisoformat(task["deadline"])
    deadline_fmt = deadline.strftime("%d.%m %H:%M")
    icon = "🚨" if urgent else "⏰"

    group_text = (
        f"{icon} *{when_text}*\n"
        f"❗ Не виконано: {task['assignee']}\n"
        f"📋 #{task['id']} {task['task_text']}\n"
        f"📅 Дедлайн: {deadline_fmt}"
    )
    kb = done_keyboard(task["id"])
    try:
        await application.bot.send_message(
            chat_id=task["chat_id"], text=group_text,
            parse_mode="Markdown", reply_markup=kb,
        )
    except Exception as e:
        logger.error(f"Group reminder failed: {e}")

    uid = assignee_user_id(task)
    if uid:
        try:
            await application.bot.send_message(
                chat_id=uid,
                text=(
                    f"{icon} *{when_text}*\n\n"
                    f"📋 Задача #{task['id']}: {task['task_text']}\n"
                    f"📅 Дедлайн: {deadline_fmt}\n\n"
                    f"Натисни кнопку коли виконаєш ⬇️"
                ),
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"DM reminder failed: {e}")


async def send_overdue(application, task):
    fresh = db.get_task(task["id"])
    if not fresh or fresh["is_done"]:
        return
    deadline = datetime.fromisoformat(task["deadline"])
    text = (
        f"🔴 *ПРОСТРОЧЕНО!*\n"
        f"❗ Не виконав: {task['assignee']}\n"
        f"📋 #{task['id']} {task['task_text']}\n"
        f"📅 Дедлайн був: {deadline.strftime('%d.%m %H:%M')}"
    )
    try:
        await application.bot.send_message(
            chat_id=task["chat_id"], text=text,
            parse_mode="Markdown", reply_markup=done_keyboard(task["id"]),
        )
    except Exception as e:
        logger.error(f"Overdue announce failed: {e}")


async def check_deadlines(application):
    tasks = db.get_tasks_for_reminder()
    now = now_kyiv()
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        minutes_left = (deadline - now).total_seconds() / 60
        if 23 * 60 <= minutes_left <= 25 * 60 and not t["reminded_1d"]:
            await send_reminder(application, t, "Залишилось 24 години", urgent=False)
            db.mark_reminded(t["id"], "reminded_1d")
        elif 110 <= minutes_left <= 130 and not t["reminded_2h"]:
            await send_reminder(application, t, "Залишилось 2 години", urgent=True)
            db.mark_reminded(t["id"], "reminded_2h")
        elif 10 <= minutes_left <= 20 and not t["reminded_15m"]:
            await send_reminder(application, t, "Залишилось 15 хвилин", urgent=True)
            db.mark_reminded(t["id"], "reminded_15m")
        elif minutes_left < 0 and not t["reminded_overdue"]:
            await send_overdue(application, t)
            db.mark_reminded(t["id"], "reminded_overdue")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("team", team_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(done_callback, pattern=r"^done:\d+$"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_deadlines, "interval", minutes=5, args=[app])
    scheduler.start()
    logger.info("trofim_bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
