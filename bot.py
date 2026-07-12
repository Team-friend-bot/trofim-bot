import asyncio
import os
import json
import logging
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    from datetime import timezone
    KYIV_TZ = timezone(timedelta(hours=3))  # EEST fallback


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
OWNER_ID = int(os.environ["OWNER_ID"])

logger.info(f"Bot starting. OWNER_ID={OWNER_ID}")

db = Database()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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


def parse_task_with_claude(message_text: str, chat_id: int = None) -> list[dict]:
    import time
    now = now_kyiv()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    today = now.date().isoformat()

    team_hint = ""
    if chat_id:
        members = db.get_team(chat_id)
        if members:
            names = ", ".join(f"{m['name']} (@{m['username']})" for m in members)
            team_hint = (
                f"\nСписок команди: {names}\n"
                f"Якщо в повідомленні скорочення або прізвисько — підбери найближче ім'я з команди "
                f"(Віка→Вікторія, Льоша→Олексій тощо). Для assignee використовуй @username з команди."
            )

    system_prompt = f"""Ти асистент менеджера команди. Зараз {now_str} (Київський час).
Розпізнавай делегування задач — будь-якою мовою (укр, рос, англ).
Якщо є кілька виконавців — створи окремий об'єкт для кожного.{team_hint}
Текст може бути з голосового і містити помилки розпізнавання — виправляй очевидні
(«обери мене»→«набери мене», «по боту»→«по борту») і все одно став задачу.
Відповідай ТІЛЬКИ валідним JSON-масивом, БЕЗ ```-обгорток і БЕЗ коментарів:
[{{"has_task": true/false, "assignee": "@username з команди або ім'я", "task": "опис", "deadline": "YYYY-MM-DDTHH:MM:SS"}}]
Правила дедлайну:
- "до 20 травня" → {today[:4]}-05-20T18:00:00
- "завтра до 10:00" → наступний день від {now_str} о 10:00
- "через 15 хвилин" / "в течение 15 минут" → {now_str} + 15 хв
- "за годину" / "через час" → {now_str} + 1 год
- "сьогодні" / "протягом дня" → {today}T18:00:00
- Якщо час не вказано — 18:00
ГОЛОВНЕ ПРАВИЛО has_task: якщо є виконавець (ім'я/@username) І будь-який дедлайн —
has_task ЗАВЖДИ true, навіть якщо формулювання задачі неідеальне. Не суди про «зрозумілість».
has_task: false ТІЛЬКИ якщо реально немає виконавця АБО немає дедлайну."""

    for attempt in range(3):
        try:
            response = claude.messages.create(
                model="claude-sonnet-4-6", max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": message_text}],
            )
            raw = response.content[0].text
            logger.info(f"Claude raw: {raw}")
            try:
                s, e = raw.find("["), raw.rfind("]")
                if s >= 0 and e > s:
                    return json.loads(raw[s:e+1])
            except Exception as ex:
                logger.error(f"Parse error: {ex}")
            return [{"has_task": False}]
        except Exception as e:
            msg = str(e)
            if ("529" in msg or "overloaded" in msg.lower() or "529" in msg) and attempt < 2:
                logger.warning(f"Claude overloaded, retry {attempt + 1}")
                time.sleep(3 * (attempt + 1))
                continue
            raise
    return [{"has_task": False}]


def parse_voice(audio_path: str, chat_id: int = None) -> tuple[str, list[dict]]:
    """Returns (transcription, parsed_tasks). transcription is "" if unrecognizable."""
    import subprocess
    import speech_recognition as sr

    wav_path = audio_path.replace(".ogg", ".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path, "-y"],
            capture_output=True, check=True, timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg_not_installed")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg_error: {e.stderr.decode()[:200]}")

    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_google(audio, language="uk-UA")
    except sr.UnknownValueError:
        return "", [{"has_task": False}]
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    logger.info(f"Voice transcription: {text}")
    return text, parse_task_with_claude(text, chat_id)


SNOOZE_OPTIONS = {"1h": timedelta(hours=1), "3h": timedelta(hours=3), "1d": timedelta(days=1)}
SNOOZE_LABELS = {"1h": "1 годину", "3h": "3 години", "1d": "1 день"}


def done_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Виконано", callback_data=f"done:{task_id}")],
        [
            InlineKeyboardButton("⏰ +1 год", callback_data=f"snooze:{task_id}:1h"),
            InlineKeyboardButton("+3 год", callback_data=f"snooze:{task_id}:3h"),
            InlineKeyboardButton("+1 день", callback_data=f"snooze:{task_id}:1d"),
        ],
    ])


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
    raw_assignee = result["assignee"]
    if raw_assignee.startswith("@"):
        member = db.get_member_by_username(chat_id, raw_assignee)
    else:
        member = db.get_member(chat_id, raw_assignee)
    tag = f"@{member['username']}" if member and member.get("username") else raw_assignee

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


async def can_act_on_task(context, task: dict, user) -> bool:
    """Owner, the assignee themselves, a group admin, or a flagged manager can act on a task."""
    user_tag = f"@{user.username}".lower() if user.username else None
    is_owner = user.id == OWNER_ID
    is_assignee = (user_tag and task["assignee"].lower() == user_tag) or (assignee_user_id(task) == user.id)
    is_admin = await is_chat_admin(context.bot, task["chat_id"], user.id)
    is_manager = is_allowed_to_assign(task["chat_id"], user.id)
    return is_owner or is_assignee or is_admin or is_manager


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
    if not await can_act_on_task(context, task, user):
        await query.answer("Цю задачу може закрити лише виконавець", show_alert=True)
        return

    closed = await close_task(context, task_id, user)
    await query.answer("Задача закрита ✅" if closed else "Вже закрита")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def snooze_callback(update, context):
    query = update.callback_query
    try:
        _, task_id_str, code = query.data.split(":")
        task_id = int(task_id_str)
    except (ValueError, IndexError):
        await query.answer()
        return
    delta = SNOOZE_OPTIONS.get(code)
    if delta is None:
        await query.answer()
        return

    task = db.get_task(task_id)
    if not task:
        await query.answer("Задача не знайдена", show_alert=True)
        return
    if task["is_done"]:
        await query.answer("Задача вже виконана")
        return

    user = query.from_user
    if not await can_act_on_task(context, task, user):
        await query.answer("Перенести дедлайн може лише виконавець", show_alert=True)
        return

    new_deadline = now_kyiv() + delta
    db.update_deadline(task_id, new_deadline.isoformat())
    deadline_fmt = new_deadline.strftime("%d.%m.%Y %H:%M")
    label = SNOOZE_LABELS[code]
    who = f"@{user.username}" if user.username else user.first_name

    await query.answer(f"Перенесено на {label}")
    kb = done_keyboard(task_id)
    text = (
        f"⏰ {who} переніс(ла) дедлайн задачі #{task_id} на {label}\n"
        f"📋 {task['task_text']}\n"
        f"📅 Новий дедлайн: {deadline_fmt}"
    )
    try:
        await context.bot.send_message(chat_id=task["chat_id"], text=text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Group snooze announce failed: {e}")

    uid = assignee_user_id(task)
    if uid and uid != user.id:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"⏰ Дедлайн задачі #{task_id} перенесено на {label}\n\n"
                    f"📋 {task['task_text']}\n"
                    f"📅 Новий дедлайн: {deadline_fmt}"
                ),
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"DM snooze notify failed: {e}")

    try:
        await query.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        pass


async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    """Check if user is a Telegram group admin or creator."""
    if user_id == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


def is_allowed_to_assign(chat_id: int, user_id: int) -> bool:
    """Owner or any member with is_manager=1 can assign tasks."""
    if user_id == OWNER_ID:
        return True
    member = db.get_member_by_user_id(chat_id, user_id)
    return bool(member and member.get("is_manager"))


async def track_member(update, context):
    """Passively learn the roster: register every group member who sends anything."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return
    try:
        db.upsert_seen_member(chat.id, user.username, user.id, user.first_name)
    except Exception as e:
        logger.warning(f"track_member failed: {e}")


async def handle_message(update, context):
    if not update.message or not update.message.text:
        return
    if not is_allowed_to_assign(update.message.chat_id, update.message.from_user.id):
        return
    results = await asyncio.to_thread(parse_task_with_claude, update.message.text, update.message.chat_id)
    for result in results:
        await save_and_reply(update, context, result)


async def handle_voice(update, context):
    if not update.message or not update.message.voice:
        return
    if not is_allowed_to_assign(update.message.chat_id, update.message.from_user.id):
        return
    voice = update.message.voice
    audio_path = f"/tmp/{voice.file_id}.ogg"
    try:
        file = await context.bot.get_file(voice.file_id)
        await file.download_to_drive(audio_path)
        transcription, results = await asyncio.to_thread(parse_voice, audio_path, update.message.chat_id)
        any_saved = False
        for result in results:
            if await save_and_reply(update, context, result, source="з голосу"):
                any_saved = True
        if not any_saved:
            if not transcription:
                await update.message.reply_text(
                    "🎙 Не вдалося розібрати голосове. Спробуй сказати чіткіше або напиши текстом."
                )
            else:
                await update.message.reply_text(
                    f"🎙 Почув: «{transcription}»\n\n"
                    f"⚠️ Не зміг поставити задачу — вкажи виконавця і дедлайн "
                    f"(напр. «Максим, зроби звіт до завтра 10:00»)."
                )
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"❌ Помилка голосу: {type(e).__name__}: {str(e)[:200]}")
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
    if not await is_chat_admin(context.bot, update.message.chat_id, update.message.from_user.id):
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
        connected = "✅" if m.get("user_id") else "⚠️"
        role = " 👔 менеджер" if m.get("is_manager") else ""
        lines.append(f"{connected} {m['name']} → @{m['username']}{role}")
    lines.append("\n✅ — підключений  ⚠️ — ще не написав /start\n👔 — може ставити задачі")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def manager_command(update, context):
    """Grant or revoke manager role. Usage: /manager ім'я  or  /manager ім'я remove"""
    if not await is_chat_admin(context.bot, update.message.chat_id, update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "📝 Призначити менеджера: /manager ім'я\n"
            "❌ Зняти роль: /manager ім'я remove\n\n"
            "Менеджер може ставити задачі голосом і текстом так само як ти."
        )
        return
    name = context.args[0]
    revoke = len(context.args) > 1 and context.args[1].lower() == "remove"
    member = db.get_member(update.message.chat_id, name)
    if not member:
        await update.message.reply_text(f"❌ {name} не знайдений в команді. Спочатку /add {name} @username")
        return
    db.set_manager(update.message.chat_id, name, not revoke)
    if revoke:
        await update.message.reply_text(f"✅ {name} більше не менеджер")
    else:
        await update.message.reply_text(
            f"✅ {name} тепер менеджер — може ставити задачі голосом і текстом.\n\n"
            f"⚠️ Якщо ще не підключений — хай напише /start боту @{context.bot.username}"
        )


async def remove_command(update, context):
    if not await is_chat_admin(context.bot, update.message.chat_id, update.message.from_user.id):
        return
    if not context.args:
        await update.message.reply_text("Формат: /remove ім'я")
        return
    db.remove_member(update.message.chat_id, context.args[0])
    await update.message.reply_text(f"✅ Видалено: {context.args[0]}")


async def task_command(update, context):
    if not await is_chat_admin(context.bot, update.message.chat_id, update.message.from_user.id):
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


async def error_handler(update, context):
    err = context.error
    logger.error(f"Update caused error: {err}", exc_info=err)
    try:
        msg = f"⚠️ Помилка бота:\n{type(err).__name__}: {err}"
        if update and getattr(update, "effective_message", None):
            msg += f"\n\nПовідомлення: {update.effective_message.text or '(не текст)'}"
        await context.bot.send_message(chat_id=OWNER_ID, text=msg[:4000])
    except Exception as e:
        logger.error(f"Could not notify owner: {e}")


def main():
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        logger.warning("Running on Railway — bot moved to the VPS deployment, exiting to avoid a polling conflict.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )
    app.add_error_handler(error_handler)
    # group=1 runs independently of the task handlers below — learns the roster
    # from every message so the team list rebuilds itself passively.
    app.add_handler(MessageHandler(filters.ALL, track_member), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("team", team_command))
    app.add_handler(CommandHandler("manager", manager_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(done_callback, pattern=r"^done:\d+$"))
    app.add_handler(CallbackQueryHandler(snooze_callback, pattern=r"^snooze:\d+:(1h|3h|1d)$"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_deadlines, "interval", minutes=5, args=[app])
    scheduler.start()
    logger.info("trofim_bot started")
    # Explicitly request ALL update types — Telegram otherwise remembers the last
    # allowed_updates (was ["message"]), which silently dropped callback_query and
    # broke every inline button (Виконано / snooze).
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
