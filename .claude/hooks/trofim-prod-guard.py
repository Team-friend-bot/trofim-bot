#!/usr/bin/env python3
"""Стоп-кран: гейтить (ask) команди, що МУТУЮТЬ бойовий борд TROFIM.
Прод = сервіс manager_tracker + шлях /opt/manager_tracker/ (НЕ *_staging) + бойова БД.
Staging, читання (grep/cat/curl/ls/sqlite SELECT), .backup — пропускаються вільно.
Вивід ask → harness питає дозвіл користувача перед виконанням."""
import sys, json, re

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if data.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (data.get("tool_input") or {}).get("command", "") or ""

# прибираємо staging-токени, щоб решта посилань на manager_tracker = прод
scrub = cmd.replace("manager_tracker_staging", "«STAGING»")

block = False
reasons = []

# 1) рестарт/стоп/старт прод-сервісу
if re.search(r"systemctl\s+(restart|stop|start|reload)\b", cmd) and re.search(r"\bmanager_tracker\b", scrub):
    block = True; reasons.append("рестарт бойового сервісу manager_tracker")

# 2) запис у прод-код /opt/manager_tracker/
if re.search(r"/opt/manager_tracker\b", scrub) and re.search(
        r"(sed\s+-i|\btee\b|>\s*/opt/manager_tracker|>>\s*/opt/manager_tracker|\bcp\b|\bscp\b|\brsync\b|\bmv\b|\brm\b|patch_?\w*\.py)", cmd):
    block = True; reasons.append("запис у бойовий код /opt/manager_tracker/")

# 3) мутація бойової БД
if re.search(r"/opt/manager_tracker/[^\s]*\.db\b", scrub) and re.search(
        r"\b(UPDATE|INSERT|DELETE|DROP|REPLACE|ALTER|CREATE\s+TABLE)\b", cmd, re.I):
    block = True; reasons.append("зміна бойової БД")

if block:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                "⛔ ЖИВИЙ БОРД TROFIM: " + "; ".join(reasons) +
                ". Правило: спершу staging (/opt/manager_tracker_staging), потім showcase, потім прод. "
                "Підтверди лише якщо свідомо деплоїш на прод."
            ),
        }
    }
    print(json.dumps(out, ensure_ascii=False))
sys.exit(0)
