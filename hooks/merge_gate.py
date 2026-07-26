#!/usr/bin/env python3
"""Хук ДО вызова Bash — гейт мержа.

Тот же принцип, что и у гейта коммита, но на границе попадания кода в
основную ветку и по ВСЕМУ диффу ветки, а не по одному коммиту.

Блокирует команду мержа PR/MR, когда ветка крупная (файлы или строки)
либо трогает критические пути. Обход: MERGE_REVIEW_DONE=1 <команда>,
и только при наличии свежих расписок на текущий дифф.

Важно: списки критических путей и красной зоны берутся из ОДНОГО
конфига вместе с гейтом коммита. Расхождение здесь означает путь,
закрытый на коммите и открытый на мерже, — и это худший вид дыры,
потому что выглядит как работающая защита.

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
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
    # Совпадение — от начала сегмента, а не где угодно в строке: иначе
    # `gh  pr merge` (два пробела) проходит мимо, а безобидное
    # `echo "gh pr merge"` наоборот блокируется.
    shell_cwd = str(data.get("cwd") or "")
    hits = G.invocations(command, cfg["pr_merge_patterns"], shell_cwd)
    if not hits:
        return 0

    # Каталог берётся у самого сегмента: `cd <другая копия> && gh pr merge`
    # мержит ветку ТОЙ копии, и проверять дифф сессионной — значит смотреть
    # не на тот код.
    for _segment, seg_dir in hits:
        code = check_tree(G.resolve_work_dir(seg_dir), command, cfg)
        if code:
            return code
    return 0


def check_tree(work_dir: str, command: str, cfg: dict) -> int:
    files = G.changed_files(work_dir)
    additions = G.added_lines(work_dir)

    if not files:
        return G.block(
            "🛑 Гейт мержа: в текущей рабочей копии нет изменений относительно "
            "точки ветвления, значит проверить нечего.\n"
            f"Проверяемое дерево: {work_dir}\n\n"
            "Почти всегда это значит, что сессия находится не в той копии "
            "репозитория, где живёт этот PR. Перейдите в неё и повторите — "
            "расписки привязаны к дереву, а не к номеру PR."
        )

    big = len(files) >= cfg["merge_gate"]["max_files"]
    many = additions >= cfg["merge_gate"]["max_additions"]
    critical = [f for f in files if G.is_critical(f, cfg)]
    if not (big or many or critical):
        return 0

    bypass = os.environ.get("MERGE_REVIEW_DONE") == "1" or _inline_env(command)
    reviewers = G.required_reviewers(files, cfg)
    red = [f for f in files if G.is_red_zone(f, cfg)]

    if not bypass:
        reasons = []
        if big:
            reasons.append(f"файлов: {len(files)} (порог {cfg['merge_gate']['max_files']})")
        if many:
            reasons.append(f"добавлено строк: {additions} (порог {cfg['merge_gate']['max_additions']})")
        if critical:
            reasons.append("критические пути: " + ", ".join(sorted(critical)[:5]))
        msg = (
            "🛑 Гейт мержа: перед мержем нужно ревью всего диффа ветки.\n\n"
            f"Проверяемое дерево: {work_dir}\n"
            "Причина: " + "; ".join(reasons) + "\n\n"
            "Требуемые ревьюеры: " + ", ".join(reviewers) + "\n\n"
            "Перед повтором:\n"
            "  1. Влить основную ветку СЕЙЧАС, до ревью (влитие после сдвигает базу "
            "и обнуляет все расписки).\n"
            "  2. Запустить ревьюеров синхронно по полному диффу ветки.\n"
            "  3. Убедиться, что у независимого внешнего ревьюера нет блокирующих "
            "замечаний, а неблокирующие либо починены, либо письменно отклонены.\n"
            "  4. MERGE_REVIEW_DONE=1 <команда мержа>\n"
        )
        if red:
            msg += (
                "\n⚠ КРАСНАЯ ЗОНА: " + ", ".join(sorted(red)[:5]) + "\n"
                "Здесь ревьюеров и обхода НЕДОСТАТОЧНО — нужно явное решение человека.\n"
            )
        return G.block(msg)

    ok, problems = G.check_receipts(files, work_dir, cfg)
    if ok:
        if red:
            return G.block(
                "🛑 Гейт мержа: расписки в порядке, но дифф трогает красную зону:\n  "
                + "\n  ".join(sorted(red))
                + "\n\nДля красной зоны требуется явное подтверждение человека — "
                "автоматический обход здесь не предусмотрен намеренно."
            )
        return 0

    return G.block(
        "🛑 Гейт мержа: MERGE_REVIEW_DONE=1 не принят — нет доказательства ревью.\n\n"
        f"Проверенное дерево: {work_dir}\n\n"
        + "\n".join(f"  ✗ {p}" for p in problems)
        + "\n\nЕсли расписки «на другой дифф» — значит после ревью что-то менялось. "
        "Соберите все правки в один пакет и сделайте ровно один повторный прогон."
    )


def _inline_env(command: str) -> bool:
    """MERGE_REVIEW_DONE=1 может стоять прямо перед командой.

    Разбор идёт через снятие обёрток, а не только присваиваний: у
    `env MERGE_REVIEW_DONE=1 gh pr merge` присваивание стоит ПОСЛЕ обёртки, и
    иначе обход просто не был бы распознан — человек написал бы рабочую с виду
    команду и получил отказ без объяснения.
    """
    for segment in G.expand_segments(command):
        _tokens, env = G.peel_wrappers(G.tokenize(segment))
        if env.get("MERGE_REVIEW_DONE") == "1":
            return True
    return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"🛑 Гейт мержа не смог отработать: {exc}\n")
        sys.exit(2)
