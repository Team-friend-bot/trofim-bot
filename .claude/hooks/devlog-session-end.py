#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SessionEnd -> дописати сирий слід сесії у журнал розробок TROFIM."""
import json, os, subprocess, sys

COLLECT = os.path.expanduser("~/Desktop/Стратег/devlog/bin/collect.py")

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    cwd = payload.get("cwd") or ""
    if "Стратег" not in cwd:          # журнал ведемо лише по TROFIM
        return
    if not os.path.exists(COLLECT):
        return
    tp = payload.get("transcript_path")
    args = ["--session", tp] if tp and os.path.exists(tp) else ["--all"]
    try:
        subprocess.run([sys.executable, COLLECT] + args,
                       capture_output=True, timeout=50)
    except Exception:
        pass

if __name__ == "__main__":
    main()
