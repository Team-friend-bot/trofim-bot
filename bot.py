import asyncio
import os
import json
import logging
import re
from datetime import datetime, date, time as dtime, timedelta
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
from telegram.helpers import escape_markdown
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


WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PERIOD_WORDS = {
    "щодня": "daily", "щоденно": "daily", "кожен день": "daily",
    "будні": "weekdays", "щобудня": "weekdays", "по будням": "weekdays",
    "пн": "mon", "щопонеділка": "mon", "понеділок": "mon",
    "вт": "tue", "щовівторка": "tue", "вівторок": "tue",
    "ср": "wed", "щосереди": "wed", "середа": "wed",
    "чт": "thu", "щочетверга": "thu", "четвер": "thu",
    "пт": "fri", "щопятниці": "fri", "щоп'ятниці": "fri", "пятниця": "fri", "п'ятниця": "fri",
    "сб": "sat", "щосуботи": "sat", "субота": "sat",
    "нд": "sun", "щонеділі": "sun", "неділя": "sun",
}
PERIOD_LABELS = {
    "daily": "щодня", "weekdays": "щобудня", "mon": "щопонеділка", "tue": "щовівторка",
    "wed": "щосереди", "thu": "щочетверга", "fri": "щоп'ятниці", "sat": "щосуботи",
    "sun": "щонеділі",
}


def parse_period(word: str) -> str | None:
    """'щодня' / 'пн' / '15' (число місяця) -> stored period code."""
    w = word.strip().lower()
    if w in PERIOD_WORDS:
        return PERIOD_WORDS[w]
    if w.isdigit() and 1 <= int(w) <= 31:
        return f"day:{int(w)}"
    return None


def period_label(period: str) -> str:
    if period.startswith("day:"):
        return f"{period[4:]} числа щомісяця"
    return PERIOD_LABELS.get(period, period)


def is_due_today(period: str, day: date) -> bool:
    if period == "daily":
        return True
    if period == "weekdays":
        return day.weekday() < 5
    if period in WEEKDAY_CODES:
        return day.weekday() == WEEKDAY_CODES.index(period)
    if period.startswith("day:"):
        wanted = int(period[4:])
        last_day = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        # 31-е у короткому місяці -> останній день, інакше задача просто зникає
        return day.day == min(wanted, last_day.day)
    return False


TELEGRAM_LIMIT = 3900  # 4096 minus room for the entities Telegram counts


def chunk_lines(lines, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram rejects a message over 4096 chars outright, so a team with a few
    dozen open tasks used to get nothing at all from /tasks — just an error DM to
    the owner. Split on line boundaries instead."""
    chunks, current, size = [], [], 0
    for line in lines:
        line_size = len(line) + 1
        if current and size + line_size > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


async def reply_lines(message, lines, **kwargs):
    for chunk in chunk_lines(lines):
        await message.reply_text(chunk, **kwargs)


async def send_lines(bot_, chat_id, lines, **kwargs):
    for chunk in chunk_lines(lines):
        await bot_.send_message(chat_id=chat_id, text=chunk, **kwargs)


def md(text) -> str:
    """Escape user-supplied text for parse_mode="Markdown".
    Telegram rejects the whole message when an entity is left unclosed, so an
    ordinary username like @andrii_smith or a task text with * silently killed
    the reminder (only a log line was left behind)."""
    return escape_markdown(str(text), version=1)


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


def parse_task_with_claude(message_text: str, chat_id: int = None, reply_context: str = None) -> list[dict]:
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
            content = message_text
            if reply_context:
                # a manager often adds to an earlier message ("і ще прайс до пʼятниці"),
                # which is unparseable on its own
                content = (
                    f"Повідомлення, на яке відповідають (контекст):\n{reply_context}\n\n"
                    f"Нове повідомлення (з нього і став задачу):\n{message_text}"
                )
            response = claude.messages.create(
                model="claude-sonnet-4-6", max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
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


def parse_voice(audio_path: str, chat_id: int = None, reply_context: str = None) -> tuple[str, list[dict]]:
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
    return text, parse_task_with_claude(text, chat_id, reply_context)


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

    now = now_kyiv()
    task_id = db.add_task(
        chat_id=chat_id, task_text=result["task"], assignee=tag,
        deadline=deadline.isoformat(), created_by=update.message.from_user.first_name,
        created_at=now.isoformat(),
    )

    warnings = []
    if not member:
        # Claude could match a name nobody in the roster has — the task would sit
        # there with no DM and no reminders, and nobody would know.
        warnings.append(
            f"⚠️ {raw_assignee} не в команді — нагадувань в особисті не буде.\n"
            f"Додай: /add {raw_assignee.lstrip('@')} @username"
        )
    elif not member.get("started"):
        warnings.append(
            f"⚠️ {tag} ще не натиснув(ла) Start у бота — задача видна лише в групі"
        )
    if deadline < now:
        # already-late deadlines happen ("до 10:00" said at 15:00); say so instead
        # of letting the overdue announcement fire a minute later as a surprise
        warnings.append(f"⚠️ Дедлайн у минулому. Перенести: /deadline {task_id} дд.мм гг:хх")
        db.mark_reminded(task_id, "reminded_overdue", "reminded_1d", "reminded_2h", "reminded_15m")

    deadline_fmt = deadline.strftime("%d.%m.%Y %H:%M")
    suffix = f" ({source})" if source else ""
    kb = done_keyboard(task_id)

    text = f"✅ Задача #{task_id} зафіксована{suffix}\n👤 {tag}\n📋 {result['task']}\n📅 {deadline_fmt}"
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    await update.message.reply_text(text, reply_markup=kb)

    if member and member.get("user_id"):
        try:
            await context.bot.send_message(
                chat_id=member["user_id"],
                text=(
                    f"📌 *Тобі поставлена задача #{task_id}*\n\n"
                    f"📋 {md(result['task'])}\n"
                    f"📅 Дедлайн: {deadline_fmt}\n\n"
                    f"Коли виконаєш — натисни кнопку ⬇️"
                ),
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"DM failed: {e}")
    return True


async def close_task(context, task_id: int, by_user, proof=None) -> bool:
    """Mark task done and announce in the group. `proof` is an (file_id, kind)
    pair sent along with the announcement instead of a plain message, so the
    invoice scan or the photo of the goods lands next to the closed task.
    Returns True if closed now."""
    task = db.get_task(task_id)
    if not task or task["is_done"]:
        return False

    done_at = now_kyiv()
    db.mark_done(task_id, done_at.isoformat())
    deadline = datetime.fromisoformat(task["deadline"])
    status = format_late(deadline, done_at)

    closer = f"@{by_user.username}" if by_user.username else by_user.first_name
    text = f"✅ {closer} закрив(ла) задачу #{task_id} {status}\n📋 {task['task_text']}"

    try:
        if proof:
            file_id, kind = proof
            send = context.bot.send_photo if kind == "photo" else context.bot.send_document
            key = "photo" if kind == "photo" else "document"
            await send(chat_id=task["chat_id"], caption=f"{text}\n📎 з підтвердженням",
                       **{key: file_id})
        else:
            await context.bot.send_message(chat_id=task["chat_id"], text=text)
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


def reply_context_of(message) -> str | None:
    """Text of the message being replied to — skipping the bot's own posts,
    which are confirmations rather than task context."""
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return None
    if getattr(replied.from_user, "is_bot", False):
        return None
    return replied.text or replied.caption or None


async def handle_message(update, context):
    if not update.message or not update.message.text:
        return
    if not is_allowed_to_assign(update.message.chat_id, update.message.from_user.id):
        return
    results = await asyncio.to_thread(
        parse_task_with_claude, update.message.text, update.message.chat_id,
        reply_context_of(update.message),
    )
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
        transcription, results = await asyncio.to_thread(
            parse_voice, audio_path, update.message.chat_id, reply_context_of(update.message)
        )
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


TASK_ID_RE = re.compile(r"#(\d+)")


def task_id_from_message(message) -> int | None:
    """Task number from the caption ("#12 готово") or from the bot message
    the file is a reply to — every bot post carries the task's #id."""
    replied = getattr(message, "reply_to_message", None)
    sources = [message.caption]
    if replied:
        sources += [replied.text, getattr(replied, "caption", None)]
    for text in sources:
        match = TASK_ID_RE.search(text) if text else None
        if match:
            return int(match.group(1))
    return None


def own_open_tasks(user, chat_type, chat_id) -> list[dict]:
    if chat_type == "private":
        return db.get_active_tasks_for_user(user.id)
    tag = f"@{user.username}".lower() if user.username else None
    member = db.get_member_by_user_id(chat_id, user.id)
    wanted = {t for t in (tag, member["name"].lower() if member else None) if t}
    return [t for t in db.get_active_tasks(chat_id) if t["assignee"].lower() in wanted]


async def handle_proof(update, context):
    """A photo or document from the assignee closes the task and stays with it.
    In a group we act only on an explicit #id or a reply to the task message —
    photos fly around a work chat all day and the bot must not react to them all.
    In a DM a single open task is unambiguous enough."""
    msg = update.message
    if not msg or not (msg.photo or msg.document):
        return
    private = msg.chat.type == "private"
    file_id, kind = ((msg.photo[-1].file_id, "photo") if msg.photo
                     else (msg.document.file_id, "document"))

    task_id = task_id_from_message(msg)
    if task_id is None:
        if not private:
            return
        candidates = own_open_tasks(msg.from_user, msg.chat.type, msg.chat_id)
        if not candidates:
            return
        if len(candidates) > 1:
            lines = ["📎 До якої задачі це підтвердження? Вкажи номер у підписі до файлу:\n"]
            for t in candidates[:10]:
                lines.append(f"• #{t['id']} {md(t['task_text'])}")
            await reply_lines(msg, lines, parse_mode="Markdown")
            return
        task_id = candidates[0]["id"]

    task = db.get_task(task_id)
    if not task:
        if private:
            await msg.reply_text(f"❌ Задача #{task_id} не знайдена")
        return
    if not await can_act_on_task(context, task, msg.from_user):
        return

    db.attach_proof(task_id, file_id, kind)
    if task["is_done"]:
        await msg.reply_text(f"📎 Підтвердження додано до вже закритої задачі #{task_id}")
        return

    closed = await close_task(context, task_id, msg.from_user, proof=(file_id, kind))
    if closed:
        deadline = datetime.fromisoformat(task["deadline"])
        status = format_late(deadline, now_kyiv())
        await msg.reply_text(f"✅ Задача #{task_id} закрита {status} — підтвердження збережено")


async def undone_command(update, context):
    """/undone <id> — повернути в роботу задачу, закриту помилково."""
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("Використання: /undone <id>  — повернути задачу в роботу")
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
    if not task["is_done"]:
        await update.message.reply_text(f"Задача #{task_id} і так в роботі")
        return
    if not (is_allowed_to_assign(task["chat_id"], user.id)
            or await is_chat_admin(context.bot, task["chat_id"], user.id)):
        await update.message.reply_text("❌ Повернути задачу в роботу може лише менеджер або адмін")
        return

    db.reopen_task(task_id)
    deadline = datetime.fromisoformat(task["deadline"])
    who = f"@{user.username}" if user.username else user.first_name
    text = (
        f"↩️ {md(who)} повернув(ла) задачу #{task_id} в роботу\n"
        f"📋 {md(task['task_text'])}\n"
        f"📅 Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}"
    )
    if deadline < now_kyiv():
        text += f"\n⚠️ Дедлайн у минулому. Перенести: /deadline {task_id} дд.мм гг:хх"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=done_keyboard(task_id))
    await notify_task_parties(context, task, update.message.chat_id, user.id, text)


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
        connected = "✅" if m.get("started") else "⚠️"
        role = " 👔 менеджер" if m.get("is_manager") else ""
        lines.append(f"{connected} {md(m['name'])} → @{md(m['username'])}{role}")
    lines.append("\n✅ — підключений  ⚠️ — ще не написав /start\n👔 — може ставити задачі")
    await reply_lines(update.message, lines, parse_mode="Markdown")


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
    user = update.message.from_user
    if not (is_allowed_to_assign(update.message.chat_id, user.id)
            or await is_chat_admin(context.bot, update.message.chat_id, user.id)):
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


def deadline_icon(deadline: datetime, now: datetime) -> str:
    hours_left = (deadline - now).total_seconds() / 3600
    return "🔴" if hours_left < 0 else "🟠" if hours_left <= 2 else "🟡" if hours_left <= 24 else "🟢"


async def tasks_command(update, context):
    """In a group — everything open there. In a DM — the caller's own tasks
    from every group, so a colleague can check what's on them without scrolling."""
    private = update.message.chat.type == "private"
    if private:
        tasks = db.get_active_tasks_for_user(update.message.from_user.id)
        header = "📋 *Твої активні задачі:*\n"
        empty = "У тебе немає активних задач ✨"
    else:
        tasks = db.get_active_tasks(update.message.chat_id)
        header = "📋 *Активні задачі:*\n"
        empty = "Активних задач немає ✨"
        if context.args:
            raw = context.args[0]
            member = (db.get_member_by_username(update.message.chat_id, raw) if raw.startswith("@")
                      else db.get_member(update.message.chat_id, raw))
            wanted = {raw.lower()}
            if member:
                wanted.add(member["name"].lower())
                if member.get("username"):
                    wanted.add(f"@{member['username']}".lower())
            tasks = [t for t in tasks if t["assignee"].lower() in wanted]
            who = member["name"] if member else raw
            header = f"📋 *Активні задачі — {md(who)}:*\n"
            empty = f"У {who} немає активних задач ✨"

    if not tasks:
        await update.message.reply_text(empty)
        return

    now = now_kyiv()
    lines = [header]
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        icon = deadline_icon(deadline, now)
        if private:
            lines.append(f"{icon} #{t['id']} | {md(t['task_text'])} | {deadline.strftime('%d.%m %H:%M')}")
        else:
            lines.append(
                f"{icon} #{t['id']} | {md(t['assignee'])} | {md(t['task_text'])} | "
                f"{deadline.strftime('%d.%m %H:%M')}"
            )
    await reply_lines(update.message, lines, parse_mode="Markdown")


def task_status_icon(task: dict, now: datetime) -> str:
    if task.get("is_cancelled"):
        return "🗑"
    if task["is_done"]:
        return "✅"
    return "🔴" if datetime.fromisoformat(task["deadline"]) < now else "🟡"


async def find_command(update, context):
    """/find прайс — знайти задачу за словом, включно з уже закритими."""
    if update.message.chat.type == "private":
        await update.message.reply_text("Пошук працює в групі — шукає по задачах цієї групи")
        return
    query = " ".join(context.args).strip()
    if len(query) < 3:
        await update.message.reply_text("Використання: /find <слово>  (мінімум 3 символи)")
        return

    found = db.search_tasks(update.message.chat_id, query)
    if not found:
        await update.message.reply_text(f"Нічого не знайшов по «{query}»")
        return

    now = now_kyiv()
    lines = [f"🔍 *Знайдено по «{md(query)}»:*\n"]
    for t in found:
        deadline = datetime.fromisoformat(t["deadline"])
        tail = ""
        if t["is_done"] and t.get("done_at") and not t.get("is_cancelled"):
            tail = f" · закрито {datetime.fromisoformat(t['done_at']).strftime('%d.%m')}"
        lines.append(
            f"{task_status_icon(t, now)} #{t['id']} | {md(t['assignee'])} | {md(t['task_text'])} | "
            f"{deadline.strftime('%d.%m %H:%M')}{tail}"
        )
    await reply_lines(update.message, lines, parse_mode="Markdown")


async def overdue_command(update, context):
    """/overdue — що горить прямо зараз, по людях, від найдавнішого."""
    if update.message.chat.type == "private":
        await update.message.reply_text("Команду треба писати в групі — зведення по задачах групи")
        return

    now = now_kyiv()
    tasks = db.get_overdue_tasks(update.message.chat_id, now.isoformat())
    if not tasks:
        await update.message.reply_text("🎉 Прострочених задач немає")
        return

    by_person = {}
    for t in tasks:
        by_person.setdefault(t["assignee"], []).append(t)

    lines = [f"🔴 *Прострочено: {len(tasks)}*\n"]
    for assignee, items in sorted(by_person.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"👤 *{md(assignee)}* — {len(items)}")
        for t in items:
            deadline = datetime.fromisoformat(t["deadline"])
            hours = (now - deadline).total_seconds() / 3600
            age = f"{int(hours)} год" if hours < 48 else f"{int(hours / 24)} дн"
            lines.append(f"   • #{t['id']} {md(t['task_text'])} — висить {age}")
        lines.append("")
    await reply_lines(update.message, lines, parse_mode="Markdown")


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
    if not await can_act_on_task(context, task, update.message.from_user):
        await update.message.reply_text("❌ Цю задачу може закрити лише виконавець або менеджер")
        return
    await close_task(context, task_id, update.message.from_user)


async def cancel_command(update, context):
    """Remove a task created by mistake (misheard voice, wrong assignee).
    Cancelled tasks stop reminders and stay out of /stats."""
    user = update.message.from_user
    if not context.args:
        await update.message.reply_text("Використання: /cancel <id>  — скасувати помилкову задачу")
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
        await update.message.reply_text(f"Задача #{task_id} вже закрита")
        return
    if not (is_allowed_to_assign(task["chat_id"], user.id)
            or await is_chat_admin(context.bot, task["chat_id"], user.id)):
        await update.message.reply_text("❌ Скасувати задачу може лише менеджер або адмін групи")
        return

    db.cancel_task(task_id, now_kyiv().isoformat())
    who = f"@{user.username}" if user.username else user.first_name
    text = f"🗑 {who} скасував(ла) задачу #{task_id}\n📋 {task['task_text']}\n(не рахується в статистиці)"
    await update.message.reply_text(text)
    if update.message.chat_id != task["chat_id"]:
        try:
            await context.bot.send_message(chat_id=task["chat_id"], text=text)
        except Exception as e:
            logger.error(f"Group cancel announce failed: {e}")

    uid = assignee_user_id(task)
    if uid and uid != user.id:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"🗑 Задачу #{task_id} скасовано — виконувати не треба.\n📋 {task['task_text']}",
            )
        except Exception as e:
            logger.warning(f"DM cancel notify failed: {e}")


async def deadline_command(update, context):
    """/deadline <id> дд.мм[.рррр] гг:хх — точна нова дата, коли кнопок +1/+3 год мало."""
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /deadline <id> дд.мм.рррр гг:хх")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID має бути числом")
        return
    try:
        new_deadline = parse_deadline(" ".join(context.args[1:]))
    except ValueError:
        await update.message.reply_text("❌ Невірний формат дати. Приклад: /deadline 42 28.08 15:00")
        return

    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"❌ Задача #{task_id} не знайдена")
        return
    if task["is_done"]:
        await update.message.reply_text(f"Задача #{task_id} вже закрита")
        return
    user = update.message.from_user
    if not await can_act_on_task(context, task, user):
        await update.message.reply_text("❌ Перенести дедлайн може лише виконавець або менеджер")
        return

    db.update_deadline(task_id, new_deadline.isoformat())
    deadline_fmt = new_deadline.strftime("%d.%m.%Y %H:%M")
    who = f"@{user.username}" if user.username else user.first_name
    text = (
        f"📅 {md(who)} переніс(ла) дедлайн задачі #{task_id}\n"
        f"📋 {md(task['task_text'])}\n"
        f"📅 Новий дедлайн: {deadline_fmt}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=done_keyboard(task_id))
    await notify_task_parties(context, task, update.message.chat_id, user.id, text)


async def reassign_command(update, context):
    """/reassign <id> ім'я — коли голосом розпізнало не того виконавця."""
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /reassign <id> ім'я  (або @username)")
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
        await update.message.reply_text(f"Задача #{task_id} вже закрита")
        return
    user = update.message.from_user
    if not (is_allowed_to_assign(task["chat_id"], user.id)
            or await is_chat_admin(context.bot, task["chat_id"], user.id)):
        await update.message.reply_text("❌ Перепризначити задачу може лише менеджер або адмін групи")
        return

    raw = context.args[1]
    member = (db.get_member_by_username(task["chat_id"], raw) if raw.startswith("@")
              else db.get_member(task["chat_id"], raw))
    if not member:
        await update.message.reply_text(
            f"❌ {raw} не знайдений у команді цієї групи. Список: /team"
        )
        return
    new_tag = f"@{member['username']}" if member.get("username") else member["name"]
    old_tag = task["assignee"]
    if new_tag.lower() == old_tag.lower():
        await update.message.reply_text(f"Задача #{task_id} вже на {md(new_tag)}", parse_mode="Markdown")
        return

    old_uid = assignee_user_id(task)
    db.reassign_task(task_id, new_tag)
    who = f"@{user.username}" if user.username else user.first_name
    deadline_fmt = datetime.fromisoformat(task["deadline"]).strftime("%d.%m.%Y %H:%M")
    text = (
        f"🔄 {md(who)} перепризначив(ла) задачу #{task_id}\n"
        f"👤 {md(old_tag)} → {md(new_tag)}\n"
        f"📋 {md(task['task_text'])}\n"
        f"📅 Дедлайн: {deadline_fmt}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=done_keyboard(task_id))
    if update.message.chat_id != task["chat_id"]:
        try:
            await context.bot.send_message(
                chat_id=task["chat_id"], text=text,
                parse_mode="Markdown", reply_markup=done_keyboard(task_id),
            )
        except Exception as e:
            logger.error(f"Group reassign announce failed: {e}")

    if old_uid and old_uid != user.id:
        try:
            await context.bot.send_message(
                chat_id=old_uid,
                text=f"🔄 Задачу #{task_id} передано іншій людині — робити не треба.\n"
                     f"📋 {md(task['task_text'])}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"DM reassign notify (old) failed: {e}")
    if member.get("user_id"):
        try:
            await context.bot.send_message(
                chat_id=member["user_id"],
                text=(
                    f"📌 *Тобі передана задача #{task_id}*\n\n"
                    f"📋 {md(task['task_text'])}\n"
                    f"📅 Дедлайн: {deadline_fmt}\n\n"
                    f"Коли виконаєш — натисни кнопку ⬇️"
                ),
                parse_mode="Markdown", reply_markup=done_keyboard(task_id),
            )
        except Exception as e:
            logger.warning(f"DM reassign notify (new) failed: {e}")


async def edit_command(update, context):
    """/edit <id> новий текст — голос часто перекручує формулювання."""
    if len(context.args) < 2:
        await update.message.reply_text("Використання: /edit <id> новий текст задачі")
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
        await update.message.reply_text(f"Задача #{task_id} вже закрита")
        return
    user = update.message.from_user
    if not (is_allowed_to_assign(task["chat_id"], user.id)
            or await is_chat_admin(context.bot, task["chat_id"], user.id)):
        await update.message.reply_text("❌ Змінити текст задачі може лише менеджер або адмін групи")
        return

    new_text = " ".join(context.args[1:]).strip()
    db.update_task_text(task_id, new_text)
    who = f"@{user.username}" if user.username else user.first_name
    text = (
        f"✏️ {md(who)} уточнив(ла) задачу #{task_id}\n"
        f"було: {md(task['task_text'])}\n"
        f"стало: {md(new_text)}\n"
        f"📅 Дедлайн: {datetime.fromisoformat(task['deadline']).strftime('%d.%m.%Y %H:%M')}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=done_keyboard(task_id))
    await notify_task_parties(context, task, update.message.chat_id, user.id, text)


def tasks_csv(tasks, now: datetime) -> bytes:
    """utf-8-sig so Excel opens Ukrainian text without a mangled first column."""
    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "id", "виконавець", "задача", "дедлайн", "поставив",
        "створено", "статус", "закрито", "запізнення (хв)", "підтвердження",
    ])
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        done_at = datetime.fromisoformat(t["done_at"]) if t.get("done_at") else None
        if t.get("is_cancelled"):
            status, late_min = "скасовано", ""
        elif t["is_done"]:
            late = (done_at - deadline).total_seconds() / 60 if done_at else 0
            status = "вчасно" if late <= 0 else "із запізненням"
            late_min = max(0, int(late))
        else:
            status = "прострочено" if deadline < now else "в роботі"
            late_min = int((now - deadline).total_seconds() / 60) if deadline < now else ""
        writer.writerow([
            t["id"], t["assignee"], t["task_text"], deadline.strftime("%d.%m.%Y %H:%M"),
            t.get("created_by") or "", (t.get("created_at") or "")[:16].replace("T", " "),
            status, done_at.strftime("%d.%m.%Y %H:%M") if done_at else "", late_min,
            "так" if t.get("proof_file_id") else "",
        ])
    return buf.getvalue().encode("utf-8-sig")


async def export_command(update, context):
    """/export — усі задачі групи у CSV для звітності (Excel-friendly)."""
    user = update.message.from_user
    chat_id = update.message.chat_id
    if update.message.chat.type == "private":
        await update.message.reply_text("Команду треба писати в групі — експорт іде по задачах групи")
        return
    if not (is_allowed_to_assign(chat_id, user.id)
            or await is_chat_admin(context.bot, chat_id, user.id)):
        return

    tasks = db.get_all_tasks(chat_id)
    if not tasks:
        await update.message.reply_text("Немає задач для експорту")
        return

    now = now_kyiv()
    payload = tasks_csv(tasks, now)
    filename = f"tasks_{now.strftime('%Y-%m-%d')}.csv"
    try:
        await context.bot.send_document(
            chat_id=chat_id, document=payload, filename=filename,
            caption=f"📄 Експорт задач: {len(tasks)} шт. станом на {now.strftime('%d.%m.%Y %H:%M')}",
        )
    except Exception as e:
        logger.error(f"Export failed: {e}")
        await update.message.reply_text(f"❌ Не вдалося сформувати файл: {type(e).__name__}")


async def repeat_command(update, context):
    """/repeat період гг:хх | ім'я | опис — шаблон повторюваної задачі.
    Без аргументів показує активні шаблони цієї групи."""
    chat_id = update.message.chat_id
    user = update.message.from_user
    if update.message.chat.type == "private":
        await update.message.reply_text("Команду треба писати в групі команди")
        return

    templates = db.get_recurring(chat_id)
    raw = (update.message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        lines = ["🔁 *Повторювані задачі*\n"]
        if templates:
            for r in templates:
                lines.append(
                    f"#{r['id']} | {md(r['assignee'])} | {md(r['task_text'])} | "
                    f"{period_label(r['period'])} до {r['at_time']}"
                )
            lines.append("\nВидалити: /unrepeat <id>")
        else:
            lines.append("Поки жодного шаблону.")
        lines.append(
            "\nДодати: `/repeat період гг:хх | ім'я | опис`\n"
            "Період: щодня, будні, пн…нд або число місяця (1–31)\n"
            "Приклад: `/repeat пн 12:00 | Андрій | звіт по закупівлях за тиждень`"
        )
        await reply_lines(update.message, lines, parse_mode="Markdown")
        return

    if not (is_allowed_to_assign(chat_id, user.id)
            or await is_chat_admin(context.bot, chat_id, user.id)):
        await update.message.reply_text("❌ Створювати повторювані задачі може лише менеджер або адмін")
        return

    parts = [x.strip() for x in raw[1].split("|")]
    if len(parts) != 3:
        await update.message.reply_text(
            "📝 Формат: /repeat період гг:хх | ім'я | опис\n"
            "Приклад: /repeat пн 12:00 | Андрій | звіт по закупівлях"
        )
        return

    when, name, task_text = parts
    when_parts = when.split()
    if len(when_parts) != 2:
        await update.message.reply_text("❌ Вкажи період і час, напр. «пн 12:00» або «щодня 10:00»")
        return
    period = parse_period(when_parts[0])
    if not period:
        await update.message.reply_text(
            "❌ Не розпізнав період. Можна: щодня, будні, пн/вт/ср/чт/пт/сб/нд або число 1–31"
        )
        return
    try:
        at = datetime.strptime(when_parts[1], "%H:%M")
    except ValueError:
        await update.message.reply_text("❌ Час у формі гг:хх, напр. 12:00")
        return

    member = (db.get_member_by_username(chat_id, name) if name.startswith("@")
              else db.get_member(chat_id, name))
    if not member:
        await update.message.reply_text(f"❌ {name} не знайдений у команді. Список: /team")
        return
    tag = f"@{member['username']}" if member.get("username") else member["name"]

    template_id = db.add_recurring(
        chat_id, tag, task_text, period, at.strftime("%H:%M"), user.first_name
    )
    await update.message.reply_text(
        f"🔁 Шаблон #{template_id} створено\n"
        f"👤 {md(tag)}\n"
        f"📋 {md(task_text)}\n"
        f"📅 {period_label(period)} до {at.strftime('%H:%M')}\n\n"
        f"Задача створюватиметься автоматично зранку того дня. Видалити: /unrepeat {template_id}",
        parse_mode="Markdown",
    )


async def unrepeat_command(update, context):
    user = update.message.from_user
    chat_id = update.message.chat_id
    if not context.args:
        await update.message.reply_text("Використання: /unrepeat <id>  (список: /repeat)")
        return
    try:
        template_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID має бути числом")
        return
    if not (is_allowed_to_assign(chat_id, user.id)
            or await is_chat_admin(context.bot, chat_id, user.id)):
        await update.message.reply_text("❌ Видаляти шаблони може лише менеджер або адмін")
        return
    if db.deactivate_recurring(template_id, chat_id):
        await update.message.reply_text(f"✅ Шаблон #{template_id} вимкнено — нових задач не буде")
    else:
        await update.message.reply_text(f"❌ Шаблон #{template_id} не знайдений у цій групі")


async def create_recurring_tasks(application):
    """07:00 Kyiv — розкладає на сьогодні всі шаблони, чий день настав.
    Створюємо зранку, а не в момент дедлайну, щоб нагадування мали за що спрацювати."""
    now = now_kyiv()
    today = now.date()
    for r in db.get_all_recurring():
        if r.get("last_created_date") == today.isoformat():
            continue  # уже створено сьогодні (напр. після перезапуску)
        if not is_due_today(r["period"], today):
            continue
        hour, minute = (int(x) for x in r["at_time"].split(":"))
        deadline = datetime.combine(today, dtime(hour=hour, minute=minute))

        task_id = db.add_task(
            chat_id=r["chat_id"], task_text=r["task_text"], assignee=r["assignee"],
            deadline=deadline.isoformat(), created_by=r.get("created_by") or "бот",
            created_at=now.isoformat(),
        )
        db.mark_recurring_created(r["id"], today.isoformat())

        deadline_fmt = deadline.strftime("%d.%m.%Y %H:%M")
        kb = done_keyboard(task_id)
        try:
            await application.bot.send_message(
                chat_id=r["chat_id"],
                text=(
                    f"🔁 *Повторювана задача #{task_id}*\n"
                    f"👤 {md(r['assignee'])}\n"
                    f"📋 {md(r['task_text'])}\n"
                    f"📅 Дедлайн: {deadline_fmt}"
                ),
                parse_mode="Markdown", reply_markup=kb,
            )
        except Exception as e:
            logger.error(f"Recurring announce failed for template {r['id']}: {e}")

        uid = assignee_user_id({"chat_id": r["chat_id"], "assignee": r["assignee"]})
        if uid:
            try:
                await application.bot.send_message(
                    chat_id=uid,
                    text=(
                        f"🔁 *Задача на сьогодні #{task_id}*\n\n"
                        f"📋 {md(r['task_text'])}\n"
                        f"📅 Дедлайн: {deadline_fmt}\n\n"
                        f"Коли виконаєш — натисни кнопку ⬇️"
                    ),
                    parse_mode="Markdown", reply_markup=kb,
                )
            except Exception as e:
                logger.warning(f"Recurring DM failed: {e}")


async def notify_task_parties(context, task, from_chat_id, actor_id, text):
    """Mirror a task change to the task's group and to its assignee's DM."""
    if from_chat_id != task["chat_id"]:
        try:
            await context.bot.send_message(
                chat_id=task["chat_id"], text=text,
                parse_mode="Markdown", reply_markup=done_keyboard(task["id"]),
            )
        except Exception as e:
            logger.error(f"Group announce failed: {e}")
    uid = assignee_user_id(task)
    if uid and uid != actor_id:
        try:
            await context.bot.send_message(
                chat_id=uid, text=text,
                parse_mode="Markdown", reply_markup=done_keyboard(task["id"]),
            )
        except Exception as e:
            logger.warning(f"DM notify failed: {e}")


async def help_command(update, context):
    await update.message.reply_text(
        "🤖 *Що я вмію*\n\n"
        "Просто напиши або надиктуй у групі: «Максим, зроби звіт до завтра 10:00» — "
        "я сам розпізнаю виконавця й дедлайн.\n\n"
        "Фото або скан у відповідь на задачу (чи з «#12» у підписі) закриває її "
        "й лишається підтвердженням.\n\n"
        "*Команди*\n"
        "/tasks [ім'я] — активні задачі (в особистих — твої власні)\n"
        "/task ім'я | опис | дд.мм.рррр гг:хх — задача вручну\n"
        "/done <id> — закрити задачу\n"
        "/undone <id> — повернути закриту задачу в роботу\n"
        "/cancel <id> — скасувати помилкову задачу\n"
        "/deadline <id> дд.мм гг:хх — перенести дедлайн\n"
        "/reassign <id> ім'я — передати задачу іншому\n"
        "/edit <id> текст — уточнити формулювання\n"
        "/export — усі задачі групи у CSV\n"
        "/repeat — повторювані задачі (щодня/щотижня)\n"
        "/overdue — що горить прямо зараз\n"
        "/find <слово> — знайти задачу, зокрема закриту\n"
        "/stats [ім'я] [дні] — статистика (напр. /stats Андрій 7)\n"
        "/team — склад команди\n"
        "/add ім'я @username — додати людину\n"
        "/manager ім'я [remove] — права ставити задачі\n"
        "/remove ім'я — прибрати з команди",
        parse_mode="Markdown",
    )


def stats_line(s: dict) -> str:
    rate = (s["on_time"] / s["total"] * 100) if s["total"] else 0
    return (
        f"👤 *{md(s['assignee'])}*\n"
        f"   Всього: {s['total']} | Вчасно: {s['on_time']} | "
        f"Запізно: {s['late']} | Прострочено: {s['overdue']}\n"
        f"   Ефективність: {rate:.0f}%\n"
    )


async def stats_command(update, context):
    """/stats — за весь час, /stats 7 — за N днів, /stats Андрій [7] — по людині."""
    days, name = None, None
    for arg in context.args:
        if arg.isdigit():
            days = int(arg)
        else:
            name = arg
    if days is not None and days < 1:
        await update.message.reply_text("Використання: /stats [ім'я] [дні], напр. /stats Андрій 7")
        return

    chat_id = update.message.chat_id
    now = now_kyiv()
    since = (now - timedelta(days=days)).isoformat() if days else None
    stats = db.get_stats(chat_id, now.isoformat(), since)
    period_suffix = f" за {days} дн." if days else ""

    if name:
        member = (db.get_member_by_username(chat_id, name) if name.startswith("@")
                  else db.get_member(chat_id, name))
        wanted = {name.lower()}
        if member:
            wanted.add(member["name"].lower())
            if member.get("username"):
                wanted.add(f"@{member['username']}".lower())
        stats = [s for s in stats if s["assignee"].lower() in wanted]
        who = member["name"] if member else name
        if not stats:
            await update.message.reply_text(f"У {who} немає задач{period_suffix}")
            return

        lines = [f"📊 *{md(who)}{period_suffix}:*\n", stats_line(stats[0])]
        hanging = [
            t for t in db.get_active_tasks(chat_id)
            if t["assignee"].lower() in wanted and datetime.fromisoformat(t["deadline"]) < now
        ]
        if hanging:
            lines.append("🔴 *Висить прострочене:*")
            for t in hanging[:10]:
                deadline = datetime.fromisoformat(t["deadline"])
                lines.append(f"• #{t['id']} {md(t['task_text'])} — з {deadline.strftime('%d.%m %H:%M')}")
            if len(hanging) > 10:
                lines.append(f"…та ще {len(hanging) - 10}")
        await reply_lines(update.message, lines, parse_mode="Markdown")
        return

    if not stats:
        await update.message.reply_text(
            f"За останні {days} дн. задач не було" if days else "Немає даних"
        )
        return
    lines = [f"📊 *Статистика{period_suffix}:*\n"]
    for s in stats:
        lines.append(stats_line(s))
    await reply_lines(update.message, lines, parse_mode="Markdown")


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
        f"❗ Не виконано: {md(task['assignee'])}\n"
        f"📋 #{task['id']} {md(task['task_text'])}\n"
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
                    f"📋 Задача #{task['id']}: {md(task['task_text'])}\n"
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
        f"❗ Не виконав: {md(task['assignee'])}\n"
        f"📋 #{task['id']} {md(task['task_text'])}\n"
        f"📅 Дедлайн був: {deadline.strftime('%d.%m %H:%M')}"
    )
    try:
        await application.bot.send_message(
            chat_id=task["chat_id"], text=text,
            parse_mode="Markdown", reply_markup=done_keyboard(task["id"]),
        )
    except Exception as e:
        logger.error(f"Overdue announce failed: {e}")


# Most urgent first. Each level fires once the deadline is within `minutes`,
# not inside a narrow window: a task set 12 minutes before its deadline, or a
# restart that stepped over the window, used to lose the reminder entirely.
REMINDER_LEVELS = [
    ("reminded_15m", 20, "Залишилось 15 хвилин", True),
    ("reminded_2h", 130, "Залишилось 2 години", True),
    ("reminded_1d", 25 * 60, "Залишилось 24 години", False),
]


async def check_deadlines(application):
    tasks = db.get_tasks_for_reminder()
    now = now_kyiv()
    for t in tasks:
        deadline = datetime.fromisoformat(t["deadline"])
        minutes_left = (deadline - now).total_seconds() / 60

        if minutes_left < 0:
            if not t["reminded_overdue"]:
                await send_overdue(application, t)
                db.mark_reminded(t["id"], "reminded_overdue",
                                 *(flag for flag, *_ in REMINDER_LEVELS))
            continue

        for idx, (flag, minutes, label, urgent) in enumerate(REMINDER_LEVELS):
            if minutes_left > minutes:
                continue
            if not t[flag]:
                await send_reminder(application, t, label, urgent)
                # the gentler reminders are moot now — retire them together
                db.mark_reminded(t["id"], flag, *(f for f, *_ in REMINDER_LEVELS[idx + 1:]))
            break


async def morning_digest(application):
    """09:05 Kyiv — DM everyone their own open tasks: overdue first, then today.
    Silent for people with nothing due, so the digest stays worth reading."""
    now = now_kyiv()
    today = now.date()
    for person in db.get_digest_recipients():
        tasks = db.get_active_tasks_for_user(person["user_id"])
        overdue, due_today = [], []
        for t in tasks:
            deadline = datetime.fromisoformat(t["deadline"])
            if deadline < now:
                overdue.append((t, deadline))
            elif deadline.date() == today:
                due_today.append((t, deadline))
        if not overdue and not due_today:
            continue

        lines = [f"☀️ *Доброго ранку, {md(person['name'])}!*\n"]
        if overdue:
            lines.append("🔴 *Прострочено:*")
            for t, deadline in overdue:
                lines.append(f"• #{t['id']} {md(t['task_text'])} — було до {deadline.strftime('%d.%m %H:%M')}")
            lines.append("")
        if due_today:
            lines.append("📅 *Сьогодні:*")
            for t, deadline in due_today:
                lines.append(f"• #{t['id']} {md(t['task_text'])} — до {deadline.strftime('%H:%M')}")
        lines.append("\nЗакрити задачу: /done <id> або кнопка ✅ під нею.")

        try:
            await send_lines(application.bot, person["user_id"], lines, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Morning digest DM to {person['user_id']} failed: {e}")


async def weekly_report(application):
    """П'ятниця 17:00 — підсумок тижня в кожну групу: хто скільки закрив і що висить."""
    now = now_kyiv()
    since = (now - timedelta(days=7)).isoformat()
    for chat_id in db.get_chat_ids():
        stats = [s for s in db.get_stats(chat_id, now.isoformat(), since) if s["total"]]
        if not stats:
            continue
        stats.sort(key=lambda s: (-s["done"], s["assignee"]))
        total = sum(s["total"] for s in stats)
        done = sum(s["done"] for s in stats)
        lines = [f"📊 *Підсумки тижня*\nЗакрито {done} із {total} задач\n"]
        for s in stats:
            parts = [f"✅ {s['done']}/{s['total']}"]
            if s["late"]:
                parts.append(f"⚠️ із запізненням {s['late']}")
            if s["overdue"]:
                parts.append(f"🔴 прострочено {s['overdue']}")
            lines.append(f"👤 *{md(s['assignee'])}* — " + ", ".join(parts))
        try:
            await send_lines(application.bot, chat_id, lines, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Weekly report to {chat_id} failed: {e}")


async def connect_digest(application):
    """Once a day, DM the owner the list of members who haven't pressed Start yet."""
    pending = []
    for chat_id in db.get_chat_ids():
        pending.extend(db.get_unconnected(chat_id))
    if not pending:
        return  # everyone connected — stay silent

    lines = ["⚠️ *Ще не підключили особисті* (не натиснули Start):\n"]
    for m in pending:
        uname = f"@{m['username']}" if m.get("username") else m["name"]
        lines.append(f"• {md(m['name'])} — {md(uname)}")
    lines.append(
        "\nПопроси їх відкрити @TMO_team_bot і натиснути *Start* — "
        "тоді задачі й нагадування йтимуть їм в особисті. У групі все працює й без цього."
    )
    try:
        await send_lines(application.bot, OWNER_ID, lines, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"connect_digest failed: {e}")


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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_proof))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("team", team_command))
    app.add_handler(CommandHandler("manager", manager_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("task", task_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("deadline", deadline_command))
    app.add_handler(CommandHandler("reassign", reassign_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("undone", undone_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("overdue", overdue_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("repeat", repeat_command))
    app.add_handler(CommandHandler("unrepeat", unrepeat_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(done_callback, pattern=r"^done:\d+$"))
    app.add_handler(CallbackQueryHandler(snooze_callback, pattern=r"^snooze:\d+:(1h|3h|1d)$"))
    scheduler = AsyncIOScheduler(timezone=KYIV_TZ)
    scheduler.add_job(check_deadlines, "interval", minutes=5, args=[app])
    scheduler.add_job(connect_digest, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(morning_digest, "cron", hour=9, minute=5, day_of_week="mon-fri", args=[app])
    scheduler.add_job(weekly_report, "cron", day_of_week="fri", hour=17, minute=0, args=[app])
    scheduler.add_job(create_recurring_tasks, "cron", hour=7, minute=0, args=[app])
    scheduler.start()
    logger.info("trofim_bot started")
    # Explicitly request ALL update types — Telegram otherwise remembers the last
    # allowed_updates (was ["message"]), which silently dropped callback_query and
    # broke every inline button (Виконано / snooze).
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
