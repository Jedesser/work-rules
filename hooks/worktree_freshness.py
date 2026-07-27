#!/usr/bin/env python3
"""Хук ДО вызова Bash — свежесть новой рабочей копии.

Перед созданием рабочей копии подтягивает состояние удалённого
репозитория. Иначе агент ответвляется от того, что лежало на диске в
прошлый раз, и узнаёт об этом только на конфликтах при мерже — то есть
после того, как работа уже сделана поверх устаревшей базы.

Это не запрет, а подготовка. Падает ОТКРЫТО: нет сети — предупредили и
пропустили. Смысл в том, чтобы свежесть не зависела от того, вспомнит ли
агент сделать fetch руками.

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

WORKTREE_ADD_RE = re.compile(r"\bworktree\s+add\b")


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not WORKTREE_ADD_RE.search(command):
        return 0

    project = G.project_dir()
    code, _out = G.git(["fetch", "origin", "--prune"], project, timeout=25, cache=False)
    if code != 0:
        return G.advise(
            "⚠️ Не удалось обновить состояние удалённого репозитория перед созданием "
            "рабочей копии (нет сети или таймаут). Копия может ответвиться от "
            "устаревшей базы — проверьте это до начала работы. Не блокирую."
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
