#!/usr/bin/env python3
"""Хук ДО вызова Bash — гейт коммита.

Блокирует `git commit`, когда изменение крупное ИЛИ трогает критический
путь. Пропускает только по расписке (см. subagent_receipt.py), а не по
честному слову.

Обход:  REVIEW_DONE=1 git commit -m "..."
и он работает ТОЛЬКО если у каждого требуемого ревьюера есть расписка,
свежая и привязанная к текущему диффу.

Отдельно запрещены флаги и переменные, переадресующие git в другое
дерево (--git-dir, --work-tree, --namespace, GIT_DIR и родня): с ними
коммит описывает не то дерево, которое проверял гейт. Правильный способ
закоммитить в соседнюю копию — `git -C <путь> commit`, он разбирается
корректно.

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

REDIRECT_FLAGS = ("--git-dir", "--work-tree", "--namespace")
REDIRECT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE")


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    cfg = G.load_config()
    session_dir = G.resolve_work_dir(data.get("cwd"))

    for segment in G.split_segments(command):
        tokens = G.tokenize(segment)
        parsed = G.parse_git(tokens)
        if not parsed or parsed.get("subcommand") != "commit":
            continue

        # Переадресация дерева — отказ без разбирательств.
        for tok in parsed["globals"]:
            if any(tok == f or tok.startswith(f + "=") for f in REDIRECT_FLAGS):
                return G.block(
                    f"🛑 Гейт коммита: флаг {tok} переадресует git в другое дерево.\n"
                    f"Тогда коммит описывает не то, что проверял гейт.\n"
                    f"Используйте `git -C <путь> commit` — он поддержан корректно."
                )
        for var in REDIRECT_ENV:
            if var in parsed["env"]:
                return G.block(
                    f"🛑 Гейт коммита: переменная {var} переадресует git в другое дерево.\n"
                    f"Используйте `git -C <путь> commit`."
                )

        work_dir = parsed_dir(parsed, session_dir)
        files = G.staged_files(work_dir)
        if not files:
            return 0

        big = len(files) >= cfg["commit_gate"]["max_files"]
        critical = [f for f in files if G.is_critical(f, cfg)]
        if not big and not critical:
            return 0

        bypass = parsed["env"].get("REVIEW_DONE") == "1" or os.environ.get("REVIEW_DONE") == "1"
        reviewers = G.required_reviewers(files, cfg)

        if not bypass:
            return G.block(_why_blocked(work_dir, files, big, critical, reviewers, cfg))

        ok, problems = G.check_receipts(files, work_dir, cfg)
        if ok:
            return 0
        return G.block(
            "🛑 Гейт коммита: REVIEW_DONE=1 не принят — нет доказательства ревью.\n\n"
            f"Проверенное дерево: {work_dir}\n\n"
            + "\n".join(f"  ✗ {p}" for p in problems)
            + "\n\nЧто делать: запустить недостающих ревьюеров СИНХРОННО из этой же "
            "рабочей копии, прочитать их находки, починить критичное — и только потом "
            "коммитить.\nРасписку нельзя написать руками: это то же самое, что "
            "подделать ревью."
        )
    return 0


def parsed_dir(parsed: dict, fallback: str) -> str:
    d = G.git_dash_c_dir(parsed)
    if d and os.path.isdir(d):
        return G.resolve_work_dir(d)
    return fallback


def _why_blocked(work_dir, files, big, critical, reviewers, cfg) -> str:
    reasons = []
    if big:
        reasons.append(f"файлов в индексе: {len(files)} (порог {cfg['commit_gate']['max_files']})")
    if critical:
        reasons.append("затронуты критические пути: " + ", ".join(sorted(critical)[:5]))
    red = [f for f in files if G.is_red_zone(f, cfg)]
    if red:
        reasons.append("⚠ КРАСНАЯ ЗОНА: " + ", ".join(sorted(red)[:5]))
    return (
        "🛑 Гейт коммита: перед этим коммитом нужно ревью.\n\n"
        f"Проверяемое дерево: {work_dir}\n"
        "Причина: " + "; ".join(reasons) + "\n\n"
        "Требуемые ревьюеры: " + ", ".join(reviewers) + "\n\n"
        "Порядок:\n"
        "  1. Довести логическую единицу до конца — дифф должен быть финальным.\n"
        "  2. Прогнать дешёвые локальные проверки (линтер, типы, точечные тесты).\n"
        "  3. Запустить ревьюеров СИНХРОННО из этой рабочей копии и прочитать находки.\n"
        "  4. Починить критичное, собрать все правки в один пакет.\n"
        "  5. REVIEW_DONE=1 git commit -m \"...\"\n\n"
        "Любая правка после ревью обнуляет расписки и требует повторного прогона — "
        "поэтому шаги 1–2 идут ДО ревьюеров, а не после."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Гейт безопасности падает ЗАКРЫТО: непонятная ошибка не должна
        # превращаться в бесплатный пропуск.
        sys.stderr.write(f"🛑 Гейт коммита не смог отработать: {exc}\n")
        sys.exit(2)
