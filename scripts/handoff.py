#!/usr/bin/env python3
"""Перенос незаконченной сессии Claude Code между машинами (Мак <-> телефон/веб).

Сессии Claude Code лежат локально в ~/.claude/projects/<slug>/<session-id>.jsonl
и никуда не синхронизируются. Этот скрипт снимает с последней сессии выжимку
(история диалога, список задач, тронутые файлы, патч незакоммиченных правок),
шифрует её и кладёт одним коммитом в служебную ветку claude-handoff.
На другой машине SessionStart-хук её забирает и подкладывает в контекст.

Всё общение с git идёт через plumbing (hash-object/mktree/commit-tree), поэтому
рабочее дерево и индекс пользователя не трогаются никогда.

Зависимостей нет — только стандартная библиотека.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HANDOFF_REF = "claude-handoff"
PAYLOAD_FILE = "handoff.enc"
PREV_PAYLOAD_FILE = "handoff.prev.enc"
# не висеть на мёртвой сети: обрыв, если меньше 1 КБ/с дольше 20 секунд
SLOW_NET = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20"]
MAGIC = b"TROFIM-HANDOFF-v1"
KDF_ITERS = 200_000

MAX_DIGEST_CHARS = 40_000
MAX_PATCH_CHARS = 200_000
MAX_NEW_FILE_CHARS = 64_000
MAX_NEW_FILES_CHARS = 256_000
MAX_TOOL_OUTPUT = 400
MAX_ASSISTANT_TEXT = 2_000
MAX_USER_TEXT = 4_000
MIN_SAVE_INTERVAL = 45  # секунд между автосохранениями из Stop-хука


# --------------------------------------------------------------------------
# утилиты
# --------------------------------------------------------------------------

def log(msg):
    print(f"[handoff] {msg}", file=sys.stderr)


def run(args, cwd=None, input_bytes=None, check=True):
    kw = {}
    if input_bytes is None:
        # без этого git может зависнуть насмерть, спрашивая пароль в фоне
        kw["stdin"] = subprocess.DEVNULL
    p = subprocess.run(
        args, cwd=cwd, input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} -> {p.returncode}: {p.stderr.decode('utf-8', 'replace').strip()}"
        )
    return p


def git(args, cwd, **kw):
    return run(["git"] + args, cwd=cwd, **kw)


def git_out(args, cwd, default=""):
    p = git(args, cwd, check=False)
    if p.returncode != 0:
        return default
    return p.stdout.decode("utf-8", "replace").strip()


def project_root(start=None):
    start = start or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = git_out(["rev-parse", "--show-toplevel"], start)
    return root or os.path.realpath(start)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human_age(iso):
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "неизвестно когда"
    delta = datetime.now(timezone.utc) - then
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


# --------------------------------------------------------------------------
# шифрование: PBKDF2-SHA256 -> HMAC-SHA256 в режиме счётчика, encrypt-then-MAC
# --------------------------------------------------------------------------

def _subkeys(passphrase, salt):
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, KDF_ITERS, dklen=64)
    return dk[:32], dk[32:]


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _xor(data, stream):
    return bytes(a ^ b for a, b in zip(data, stream))


def encrypt(passphrase, plaintext):
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _subkeys(passphrase, salt)
    ct = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    tag = hmac.new(mac_key, MAGIC + salt + nonce + ct, hashlib.sha256).digest()
    blob = MAGIC + salt + nonce + tag + ct
    armored = base64.b64encode(blob).decode("ascii")
    lines = [armored[i:i + 76] for i in range(0, len(armored), 76)]
    return ("TROFIM-HANDOFF-v1\n" + "\n".join(lines) + "\n").encode("ascii")


def decrypt(passphrase, armored_bytes):
    text = armored_bytes.decode("ascii", "replace").strip()
    if text.startswith("TROFIM-HANDOFF-v1"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    blob = base64.b64decode("".join(text.split()))
    if not blob.startswith(MAGIC):
        raise ValueError("не похоже на файл хендоффа")
    off = len(MAGIC)
    salt, nonce = blob[off:off + 16], blob[off + 16:off + 32]
    tag, ct = blob[off + 32:off + 64], blob[off + 64:]
    enc_key, mac_key = _subkeys(passphrase, salt)
    expect = hmac.new(mac_key, MAGIC + salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        raise ValueError("неверный HANDOFF_KEY или файл повреждён")
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


def key_file_path():
    return os.environ.get("HANDOFF_KEY_FILE") or os.path.expanduser("~/.claude/trofim-handoff.key")


def load_key():
    key = os.environ.get("HANDOFF_KEY", "").strip()
    if key:
        return key
    path = key_file_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    return None


# --------------------------------------------------------------------------
# чистка секретов
# --------------------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"),          # токен телеграм-бота
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
]
ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;]{8,})"
)


REDACTED = "[REDACTED]"


def redact(text):
    if not text:
        return text
    # сначала присваивания (KEY=...), потом отдельно стоящие токены —
    # иначе вторая замена режет уже подставленную заглушку
    text = ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    for pat in SECRET_PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


# --------------------------------------------------------------------------
# поиск и разбор стенограммы
# --------------------------------------------------------------------------

def projects_dir():
    return os.path.expanduser("~/.claude/projects")


def slug_for(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def find_transcript(cwd, exclude_session=None):
    """Самая свежая стенограмма для этого проекта."""
    base = projects_dir()
    if not os.path.isdir(base):
        return None

    candidates = []
    exact = os.path.join(base, slug_for(cwd))
    dirs = [exact] if os.path.isdir(exact) else [
        os.path.join(base, d) for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))
    ]
    for d in dirs:
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(d, name)
            if exclude_session and name.startswith(exclude_session):
                continue
            if d != exact and not _transcript_matches_cwd(path, cwd):
                continue
            candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None
    return max(candidates)[1]


def _transcript_matches_cwd(path, cwd):
    target = os.path.realpath(cwd)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("cwd") and os.path.realpath(rec["cwd"]) == target:
                    return True
    except OSError:
        pass
    return False


def read_records(path):
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _blocks(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _clip(text, limit, inline=False):
    text = (text or "").strip()
    if inline:
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    tail = f"… [обрезано, ещё {len(text) - limit} символов]"
    return text[:limit] + (" " + tail if inline else "\n" + tail)


def _tool_line(name, inp):
    inp = inp or {}
    if name == "Bash":
        return f"`{_clip(inp.get('command', ''), 200, inline=True)}`"
    for key in ("file_path", "path", "notebook_path"):
        if inp.get(key):
            return f"`{inp[key]}`"
    if name == "Skill":
        return f"`{inp.get('skill', '')}`"
    if name in ("Grep", "Glob"):
        return f"`{_clip(inp.get('pattern', ''), 120, inline=True)}`"
    if name in ("Task", "Agent"):
        return _clip(inp.get("description", ""), 120, inline=True)
    return _clip(json.dumps(inp, ensure_ascii=False), 160, inline=True)


def build_digest(records, max_turns=40):
    """Из стенограммы делает markdown-выжимку + метаданные."""
    todos, touched, turns = [], [], []
    tool_names = {}
    session_id = cwd = branch = None
    first_ts = last_ts = None

    for rec in records:
        rtype = rec.get("type")
        session_id = rec.get("sessionId") or session_id
        cwd = rec.get("cwd") or cwd
        branch = rec.get("gitBranch") or branch
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts

        if rtype == "user" and not rec.get("isSidechain"):
            parts = []
            for b in _blocks(rec):
                if b.get("type") == "text":
                    parts.append(_clip(b.get("text", ""), MAX_USER_TEXT))
                elif b.get("type") == "tool_result":
                    name = tool_names.get(b.get("tool_use_id"), "инструмент")
                    content = b.get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            x.get("text", "") for x in content if isinstance(x, dict)
                        )
                    turns.append(
                        ("result", name, _clip(str(content or ""), MAX_TOOL_OUTPUT, inline=True))
                    )
            text = "\n".join(p for p in parts if p).strip()
            if text:
                turns.append(("user", "", text))

        elif rtype == "assistant" and not rec.get("isSidechain"):
            for b in _blocks(rec):
                btype = b.get("type")
                if btype == "text":
                    text = _clip(b.get("text", ""), MAX_ASSISTANT_TEXT)
                    if text:
                        turns.append(("assistant", "", text))
                elif btype == "tool_use":
                    name = b.get("name", "?")
                    inp = b.get("input") or {}
                    tool_names[b.get("id")] = name
                    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        todos = inp["todos"]
                    fp = inp.get("file_path")
                    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit") and fp:
                        # пути хранит относительными: на другой машине корень другой
                        if cwd and fp.startswith(cwd.rstrip("/") + "/"):
                            fp = fp[len(cwd.rstrip("/")) + 1:]
                        if fp not in touched:
                            touched.append(fp)
                    turns.append(("tool", name, _tool_line(name, inp)))

    meta = {
        "session_id": session_id,
        "cwd": cwd,
        "branch": branch,
        "started_at": first_ts,
        "last_activity": last_ts,
        "todos": todos,
        "touched_files": touched,
    }

    # берём хвост диалога, но обязательно с последнего сообщения пользователя
    tail = turns[-max_turns:]
    lines = []
    for kind, name, text in tail:
        if kind == "user":
            lines.append(f"\n### 🧑 Пользователь\n\n{text}\n")
        elif kind == "assistant":
            lines.append(f"\n### 🤖 Claude\n\n{text}\n")
        elif kind == "tool":
            lines.append(f"- 🔧 **{name}** {text}")
        else:
            lines.append(f"  ↳ _{text}_")

    digest = redact("\n".join(lines))
    if len(digest) > MAX_DIGEST_CHARS:
        digest = "… [начало обрезано]\n" + digest[-MAX_DIGEST_CHARS:]
    return digest, meta


# --------------------------------------------------------------------------
# состояние git и патч
# --------------------------------------------------------------------------

def collect_git_state(root):
    state = {
        "branch": git_out(["rev-parse", "--abbrev-ref", "HEAD"], root),
        "head": git_out(["rev-parse", "HEAD"], root),
        "head_subject": git_out(["log", "-1", "--pretty=%s"], root),
        "status": git_out(["status", "--porcelain=v1"], root),
        "stat": git_out(["diff", "HEAD", "--stat"], root),
        "untracked": [],
    }
    patch = git_out(["diff", "HEAD", "--binary"], root)
    if len(patch) > MAX_PATCH_CHARS:
        patch = ""
        state["patch_skipped"] = "патч слишком большой, не переносится"
    state["patch"] = redact(patch) if patch else ""

    # новые файлы git diff не видит — переносим их содержимым,
    # иначе на второй машине не хватало бы ровно того, что начали писать
    state["new_files"] = {}
    budget = MAX_NEW_FILES_CHARS
    listing = git_out(["ls-files", "--others", "--exclude-standard"], root)
    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel or not _safe_relpath(rel):
            continue
        state["untracked"].append(rel)
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > MAX_NEW_FILE_CHARS:
                state.setdefault("new_files_skipped", []).append(f"{rel} (слишком большой)")
                continue
            with open(full, "rb") as fh:
                raw = fh.read()
            if b"\0" in raw:
                state.setdefault("new_files_skipped", []).append(f"{rel} (двоичный)")
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            state.setdefault("new_files_skipped", []).append(f"{rel} (не прочитался)")
            continue
        if len(text) > budget:
            state.setdefault("new_files_skipped", []).append(f"{rel} (не хватило места)")
            continue
        budget -= len(text)
        state["new_files"][rel] = redact(text)
    return state


def _safe_relpath(rel):
    """Путь должен оставаться внутри проекта — файлы из переноса пишутся на диск."""
    if os.path.isabs(rel) or rel.startswith("~"):
        return False
    parts = rel.replace("\\", "/").split("/")
    return ".." not in parts and ".git" not in parts


# --------------------------------------------------------------------------
# транспорт: одна ветка с одним коммитом без истории
# --------------------------------------------------------------------------

def push_payload(root, content_bytes, note, prev_bytes=None):
    def blob_of(data):
        return git(["hash-object", "-w", "--stdin"], root, input_bytes=data).stdout.decode().strip()

    sha = blob_of(content_bytes)
    readme = (
        "Служебная ветка Claude Code: перенос незаконченных сессий между машинами.\n"
        "Содержимое зашифровано (PBKDF2 + HMAC-SHA256, ключ HANDOFF_KEY).\n"
        f"{PAYLOAD_FILE} — последний перенос, handoff.prev.enc — предыдущий.\n"
        "Не мержить в main. Пересоздаётся целиком при каждом сохранении.\n"
    ).encode("utf-8")

    entries = [
        f"100644 blob {blob_of(readme)}\tREADME.md",
        f"100644 blob {sha}\t{PAYLOAD_FILE}",
    ]
    if prev_bytes:
        # предыдущий перенос не пропадает: если его ещё не успели применить,
        # его можно достать через `handoff.py load --prev`
        entries.append(f"100644 blob {blob_of(prev_bytes)}\t{PREV_PAYLOAD_FILE}")
    tree_input = ("\n".join(sorted(entries)) + "\n").encode("utf-8")
    tree = git(["mktree"], root, input_bytes=tree_input).stdout.decode().strip()

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_AUTHOR_NAME", "claude-handoff")
    env.setdefault("GIT_AUTHOR_EMAIL", "claude-handoff@local")
    env.setdefault("GIT_COMMITTER_NAME", "claude-handoff")
    env.setdefault("GIT_COMMITTER_EMAIL", "claude-handoff@local")
    p = subprocess.run(
        ["git", "commit-tree", tree, "-m", note], cwd=root, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    commit = p.stdout.decode().strip()

    last_err = None
    for delay in (0, 2, 4, 8):
        if delay:
            time.sleep(delay)
        push = git(
            SLOW_NET + ["push", "--force", "origin", f"{commit}:refs/heads/{HANDOFF_REF}"],
            root, check=False,
        )
        if push.returncode == 0:
            return commit
        last_err = push.stderr.decode("utf-8", "replace").strip()
    raise RuntimeError(f"push не прошёл: {last_err}")


def fetch_payload(root, which=PAYLOAD_FILE):
    fetched = git(SLOW_NET + ["fetch", "--depth=1", "--quiet", "origin", HANDOFF_REF],
                  root, check=False)
    if fetched.returncode != 0:
        return None
    show = git(["show", f"FETCH_HEAD:{which}"], root, check=False)
    if show.returncode != 0:
        return None
    return show.stdout


# --------------------------------------------------------------------------
# команды
# --------------------------------------------------------------------------

def cmd_keygen(args):
    path = key_file_path()
    if os.path.exists(path) and not args.force:
        with open(path) as fh:
            key = fh.read().strip()
        print(f"Ключ уже есть: {path}")
    else:
        key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        os.chmod(path, 0o600)
        print(f"Ключ записан: {path}")
    print()
    print("Этот же ключ нужно прописать на второй машине как переменную окружения:")
    print()
    print(f"    HANDOFF_KEY={key}")
    print()
    return 0


def cmd_save(args):
    root = project_root()
    key = load_key()
    transcript = args.transcript or find_transcript(root)
    if not transcript or not os.path.exists(transcript):
        log("стенограмма сессии не найдена — нечего сохранять")
        return 1

    records = read_records(transcript)
    digest, meta = build_digest(records)
    payload = {
        "version": 1,
        "saved_at": now_iso(),
        "note": args.note or "",
        "machine": os.uname().nodename,
        "remote": os.environ.get("CLAUDE_CODE_REMOTE") == "true",
        "meta": meta,
        "digest": digest,
        "git": collect_git_state(root),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    local = os.path.join(root, ".claude", "handoff")
    os.makedirs(local, exist_ok=True)
    with open(os.path.join(local, "last-saved.json"), "wb") as fh:
        fh.write(raw)

    if not key:
        log("HANDOFF_KEY не задан — сохранил только локально в .claude/handoff/last-saved.json")
        log("сгенерировать ключ: python3 scripts/handoff.py keygen")
        return 2

    blob = encrypt(key, raw)
    where = "телефон/веб" if payload["remote"] else "локальная машина"
    prev = fetch_payload(root)
    commit = push_payload(root, blob, f"handoff {payload['saved_at']} ({where})", prev_bytes=prev)
    if not args.quiet:
        log(f"сессия сохранена в ветку {HANDOFF_REF} ({commit[:8]}, {len(blob)} байт)")
    return 0


def load_payload(root, prev=False):
    which = PREV_PAYLOAD_FILE if prev else PAYLOAD_FILE
    blob = fetch_payload(root, which)
    if blob is None:
        if prev:
            return None, "предыдущего переноса нет"
        return None, "нет ветки claude-handoff (ещё ничего не сохраняли)"
    key = load_key()
    if not key:
        return None, "HANDOFF_KEY не задан — расшифровать перенос нечем"
    try:
        return json.loads(decrypt(key, blob).decode("utf-8")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"не удалось расшифровать: {exc}"


def render_for_context(payload, current_session=None):
    meta = payload.get("meta") or {}
    gitstate = payload.get("git") or {}
    if current_session and meta.get("session_id") == current_session:
        return None

    lines = []
    lines.append("## Незаконченная сессия с другой машины")
    lines.append("")
    lines.append(
        f"Сохранено **{human_age(payload.get('saved_at'))}** "
        f"({payload.get('saved_at')}) на «{payload.get('machine', '?')}»"
        f"{' (веб/телефон)' if payload.get('remote') else ''}."
    )
    if payload.get("note"):
        lines.append(f"\n**Заметка:** {payload['note']}")
    lines.append("")
    lines.append(
        f"Ветка на той машине: `{gitstate.get('branch', '?')}`, "
        f"HEAD `{(gitstate.get('head') or '')[:8]}` — {gitstate.get('head_subject', '')}"
    )

    todos = meta.get("todos") or []
    if todos:
        lines.append("\n### План работ на момент остановки\n")
        marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        for t in todos:
            lines.append(f"- {marks.get(t.get('status'), '[ ]')} {t.get('content', '')}")

    touched = meta.get("touched_files") or []
    if touched:
        lines.append("\n### Файлы, которые правились\n")
        for f in touched[:25]:
            lines.append(f"- `{f}`")

    if gitstate.get("stat"):
        lines.append("\n### Незакоммиченные изменения на той машине\n")
        lines.append("```")
        lines.append(gitstate["stat"])
        lines.append("```")
    new_files = gitstate.get("new_files") or {}
    if new_files:
        lines.append("\n### Новые файлы (ещё не в git), перенесены целиком\n")
        for path in list(new_files)[:25]:
            lines.append(f"- `{path}`")
    if gitstate.get("patch") or new_files:
        lines.append(
            "\nЧтобы получить эту работу в текущее дерево:\n"
            "`python3 scripts/handoff.py patch --apply`\n"
            "(без флага — только показать; существующие файлы не перезаписываются)."
        )
    if gitstate.get("patch_skipped"):
        lines.append(f"\n⚠️ {gitstate['patch_skipped']}")
    if gitstate.get("new_files_skipped"):
        lines.append(
            "\n⚠️ Не перенеслись: "
            + ", ".join(f"`{f}`" for f in gitstate["new_files_skipped"][:10])
        )

    lines.append("\n### Ход диалога (хвост)\n")
    lines.append(payload.get("digest", ""))
    lines.append(
        "\n---\n"
        "Это перенос из прошлой сессии, а не текущий запрос. Сориентируйся по нему "
        "и дождись, что скажет пользователь; если работа выглядит уже законченной — "
        "просто скажи об этом. Убрать перенос: `python3 scripts/handoff.py clear`."
    )
    return "\n".join(lines)


def cmd_load(args):
    root = project_root()
    payload, err = load_payload(root, prev=args.prev)
    if err:
        log(err)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    text = render_for_context(payload)
    print(text or "перенос относится к текущей сессии")
    return 0


def cmd_patch(args):
    root = project_root()
    payload, err = load_payload(root, prev=args.prev)
    if err:
        log(err)
        return 1
    gitstate = payload.get("git") or {}
    patch = gitstate.get("patch") or ""
    new_files = gitstate.get("new_files") or {}
    if not patch.strip() and not new_files:
        log("в переносе нет незакоммиченной работы (на той машине всё было закоммичено)")
        return 1

    if not args.apply:
        if patch.strip():
            print(patch)
        for path, text in new_files.items():
            print(f"\n=== новый файл: {path} ===\n{text}")
        return 0

    if patch.strip():
        data = patch.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        check = git(["apply", "--check", "-"], root, input_bytes=data, check=False)
        if check.returncode != 0:
            log("патч не накладывается на текущее дерево:")
            log(check.stderr.decode("utf-8", "replace").strip())
            log("посмотреть глазами: python3 scripts/handoff.py patch")
            return 1
        git(["apply", "-"], root, input_bytes=data)
        log("патч наложен в рабочее дерево")

    written, skipped = [], []
    for rel, text in new_files.items():
        if not _safe_relpath(rel):
            skipped.append(f"{rel} (подозрительный путь)")
            continue
        full = os.path.join(root, rel)
        if os.path.exists(full):
            # ничего чужого не перезаписываем — это данные с другой машины
            skipped.append(f"{rel} (уже существует, не тронут)")
            continue
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(rel)
    if written:
        log("новые файлы созданы: " + ", ".join(written))
    if skipped:
        log("пропущены: " + ", ".join(skipped))
    return 0


def cmd_clear(args):
    root = project_root()
    key = load_key()
    if not key:
        log("HANDOFF_KEY не задан")
        return 1
    payload = {
        "version": 1, "saved_at": now_iso(), "note": "cleared",
        "machine": os.uname().nodename, "remote": False,
        "meta": {}, "digest": "", "git": {}, "cleared": True,
    }
    blob = encrypt(key, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    push_payload(root, blob, f"handoff cleared {payload['saved_at']}")
    log("перенос очищен")
    return 0


def _hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def hook_log(root, msg):
    """Хуки не шумят в stderr — всё уходит в лог, чтобы не мешать сессии."""
    try:
        path = os.path.join(root or ".", ".claude", "handoff", "hooks.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {msg}\n")
    except OSError:
        pass


def cmd_hook_session_start(args):
    """Печатает JSON с additionalContext. Никогда не роняет сессию."""
    data = _hook_input()
    root = None
    try:
        root = project_root(data.get("cwd"))
        payload, err = load_payload(root)
        if err:
            if "нет ветки" in err:
                return 0  # тишина: переносов ещё не было
            context = f"⚠️ Перенос сессии найден, но не прочитан: {err}"
        else:
            if payload.get("cleared"):
                return 0
            context = render_for_context(payload, current_session=data.get("session_id"))
            if not context:
                return 0
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        hook_log(root, f"session-start пропущен: {exc}")
    return 0


def cmd_hook_stop(args):
    """Автосохранение в фоне, с троттлингом. Ничего не блокирует."""
    data = _hook_input()
    root = None
    try:
        root = project_root(data.get("cwd"))
        if os.environ.get("HANDOFF_AUTOSAVE", "1") == "0":
            return 0
        stamp = os.path.join(root, ".claude", "handoff", ".last-autosave")
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < MIN_SAVE_INTERVAL:
            return 0
        with open(stamp, "w") as fh:
            fh.write(now_iso())
        cmd = [sys.executable, os.path.abspath(__file__), "save", "--quiet"]
        if data.get("transcript_path"):
            cmd += ["--transcript", data["transcript_path"]]
        logfile = os.path.join(root, ".claude", "handoff", "autosave.log")
        if os.path.exists(logfile) and os.path.getsize(logfile) > 1_000_000:
            os.replace(logfile, logfile + ".1")
        subprocess.Popen(
            cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=open(logfile, "ab"), start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        hook_log(root, f"stop-хук пропущен: {exc}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Перенос сессии Claude Code между машинами")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="создать ключ шифрования")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("save", help="сохранить текущую сессию в ветку переноса")
    p.add_argument("--transcript")
    p.add_argument("--note", help="короткая заметка «на чём остановился»")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("load", help="показать сохранённый перенос")
    p.add_argument("--json", action="store_true")
    p.add_argument("--prev", action="store_true", help="предыдущий перенос, а не последний")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("patch", help="показать/наложить незакоммиченную работу")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--prev", action="store_true", help="из предыдущего переноса")
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("clear", help="очистить перенос")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("hook-session-start")
    p.set_defaults(func=cmd_hook_session_start)

    p = sub.add_parser("hook-stop")
    p.set_defaults(func=cmd_hook_stop)

    # ни один git-вызов не должен требовать ввода с клавиатуры:
    # в фоновом автосохранении это означало бы вечно висящий процесс
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    args = ap.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        # внятная строка вместо traceback: это может писаться в фоновый лог
        text = str(exc).strip()
        log(f"не удалось: {text.splitlines()[0] if text else exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
