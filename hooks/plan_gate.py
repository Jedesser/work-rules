#!/usr/bin/env python3
"""Хук ДО вызова Bash — «план до кода».

Блокирует коммит, если ветка выросла больше порога, а плана нет либо в
нём не заполнена строка статуса.

Смысл слоя (подробно — SYSTEM.md §2): ревьюеры ловят ошибки в коде и не
ловят «сделали не то и не так». Проверка замысла должна стоять раньше и
формулироваться на языке того, кто принимает решение, — иначе человек в
цикле не может ничего проверить, кроме диффа, который он не читает.

Строка статуса сверяется ПОБАЙТОВО с одним из разрешённых вариантов.
Заглушка в шаблоне не совпадает ни с одним намеренно: пока человек не
выбрал «согласовано» или «техническое, согласование не требуется»,
проверка падает закрыто.

Порог тот же, что у гейта «сначала задача»: один вопрос — одно число.

Выключено по умолчанию (plan.enabled в gates.config.json).

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    cfg = G.load_config()
    plan_cfg = cfg.get("plan") or {}
    if not plan_cfg.get("enabled"):
        return 0

    # Каталог берётся у сегмента, а не у сессии: `cd <другая копия> &&
    # git commit` коммитит в ту копию, и план надо искать там же.
    shell_cwd = str(data.get("cwd") or "")
    commit_dir = None
    for segment, seg_dir in G.segments_with_dirs(command, shell_cwd):
        parsed = G.parse_git(G.tokenize(segment), seg_dir)
        if parsed and parsed.get("subcommand") == "commit":
            env = parsed.get("env", {})
            if env.get("PLAN_OK") == "1":
                return 0
            if commit_dir is None:
                commit_dir = G.git_dash_c_dir(parsed, seg_dir) or seg_dir
    if commit_dir is None or os.environ.get("PLAN_OK") == "1":
        return 0

    work_dir = G.resolve_work_dir(commit_dir)
    branch = G.current_branch(work_dir)
    if any(branch.startswith(p) for p in cfg["issue_exempt_branch_prefixes"]):
        return 0

    # Файлы ветки + то, что сейчас в индексе: план мог быть закоммичен
    # раньше, а мог добавляться прямо этим коммитом.
    files = set(G.changed_files(work_dir)) | set(G.staged_files(work_dir))
    if len(files) <= plan_cfg.get("min_files", 2):
        return 0

    plan_dir = plan_cfg.get("dir", "docs/plans")
    plans = [f for f in files if f.startswith(plan_dir + "/") and f.endswith(".md")
             and not f.endswith("TEMPLATE.md")]

    if not plans:
        return G.block(
            f"🛑 Нет плана, а ветка уже трогает {len(files)} файлов "
            f"(порог {plan_cfg.get('min_files', 2)}).\n\n"
            f"Заведите {plan_dir}/<номер-задачи>-<короткое-имя>.md из шаблона и "
            "закоммитьте его ПЕРВЫМ, до кода.\n\n"
            "План отвечает на то, чего не проверит ни один ревьюер кода:\n"
            "  • что изменится для пользователя — обычным языком;\n"
            "  • что меняем технически и в каком порядке (вертикальными срезами);\n"
            "  • как поймём, что работает;\n"
            "  • открытые вопросы — одним пакетом до начала работы;\n"
            "  • что рядом может сломаться.\n\n"
            "Продуктовое изменение не начинают, пока владелец не ответил. "
            "Техническое — пишем план, фиксируем допущения и работаем дальше.\n\n"
            "Если план опоздал (задача выросла по ходу) — коммитьте его сейчас и "
            "честно пометьте, что он поздний. Переписывать историю ради красивого "
            "порядка нельзя.\n\n"
            "Аварийный выход, только с причиной в теле коммита: PLAN_OK=1 git commit ..."
        )

    bad = []
    for rel in sorted(plans):
        full = os.path.join(work_dir, rel)
        verdict = check_status_line(full, plan_cfg.get("status_patterns", []))
        if verdict:
            bad.append(f"{rel}: {verdict}")
    if bad:
        return G.block(
            "🛑 План есть, но строка статуса не заполнена:\n  "
            + "\n  ".join(bad)
            + "\n\nРазрешены ровно два варианта, дословно:\n"
            "  Статус: согласован владельцем ГГГГ-ММ-ДД\n"
            "  Статус: технический — согласование не требуется\n\n"
            "Заглушка из шаблона не подходит намеренно: пока не выбрано одно из "
            "двух, непонятно, ждала ли работа решения человека."
        )
    return 0


def check_status_line(path: str, patterns: list[str]) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "файл не читается"
    if text.startswith("﻿"):
        return "файл с BOM — проверка статуса побайтовая, BOM ломает совпадение"
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("Статус: "):
            line = line.rstrip()
            for pat in patterns:
                if re.match(pat, line):
                    return None
            return f"строка «{line[:60]}» не совпадает ни с одним разрешённым вариантом"
    return "строки «Статус: » нет вовсе"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Гейт замысла падает ОТКРЫТО: он про порядок работы, и его
        # собственная ошибка не должна останавливать работу.
        sys.exit(0)
