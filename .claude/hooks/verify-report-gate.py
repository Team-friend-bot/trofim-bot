#!/usr/bin/env python3
"""Stop hook: block finishing a turn that delivers an analytical/business report
with concrete figures but no independent verification marker.

See memory: feedback_auto_verify_research.md
"""
import sys
import json
import re
import os

MARKER_RE = re.compile(r"перевір(ено|я[юв])|verified\b", re.IGNORECASE)
MONEY_RE = re.compile(r"(₴|грн\.?|\$\s?\d|€\s?\d|\d[\d\s]{1,}\s?%)")
ANALYSIS_WORD_RE = re.compile(
    r"аналіз|висновок|рекомендац|прогноз|економік|звіт|стратег", re.IGNORECASE
)
MIN_LEN = 400


def last_assistant_text(transcript_path):
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        role = msg.get("role") or obj.get("role")
        if role != "assistant":
            continue

        content = msg.get("content")
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
        if parts:
            return "\n".join(parts)
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    # Safety: never re-block a turn that is itself a continuation from a prior block.
    if data.get("stop_hook_active"):
        return 0

    transcript_path = data.get("transcript_path")
    if not transcript_path or not os.path.exists(transcript_path):
        return 0

    text = last_assistant_text(transcript_path)
    if not text:
        return 0

    if MARKER_RE.search(text):
        return 0

    if len(text) < MIN_LEN:
        return 0

    if MONEY_RE.search(text) and ANALYSIS_WORD_RE.search(text):
        result = {
            "decision": "block",
            "reason": (
                "Щойно відправлена відповідь виглядає як аналітичний звіт/висновок із "
                "конкретними цифрами, але без позначки перевірки. Перед завершенням ходу: "
                "запусти незалежну перевірку ключових тверджень (окрема Agent-підзадача без "
                "твого контексту, або чистий повторний перегляд джерел), і виклади фінальний "
                "текст заново з позначкою '✅ Перевірено' і коротким підсумком — що "
                "підтвердилось, а що варто скоригувати. Якщо це НЕ аналітичний звіт з реальними "
                "рішеннями на кону (а, наприклад, технічний опис або переказ чужого тексту) — "
                "просто додай у відповідь слово 'перевірено не потрібно' і завершуй."
            ),
        }
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
