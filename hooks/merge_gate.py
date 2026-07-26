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
import re
import subprocess
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
    for segment, seg_dir in hits:
        work_dir = G.resolve_work_dir(seg_dir)
        code = check_target(segment, work_dir)
        if code:
            return code
        code = check_tree(work_dir, command, cfg)
        if code:
            return code
    return 0


# Флаги команды мержа, забирающие значение отдельным словом. Без этого
# `gh pr merge --body 123` принимает `123` за номер PR и отказывает по
# причине, которой в команде нет.
MERGE_VALUE_FLAGS = {"-b", "--body", "-t", "--subject", "--body-file", "-F",
                     "--match-head-commit", "-R", "--repo", "--author-email"}
# Номер PR внутри сырого вызова API: `gh api -X PUT repos/o/r/pulls/7/merge`.
API_PR_RE = re.compile(r"\bpulls/(\d+)/merge\b")
# Нераскрытая подстановка — цель, которую нельзя проверить в принципе.
UNRESOLVED_RE = re.compile(r"[$`]")


def merge_target(tokens: list[str]) -> str | None:
    """Что именно велено слить: номер, ссылка ИЛИ ИМЯ ВЕТКИ.

    Ветка — третья равноправная форма записи у `gh pr merge`, и проверка,
    знающая только номер и ссылку, оставляет дыру ровно того же размера,
    какую закрывает: `gh pr merge чужая-ветка` сливает чужую работу под
    свою расписку.
    """
    for tok in tokens:
        m = API_PR_RE.search(tok)
        if m:
            return m.group(1)
    i = 0
    seen_verb = False
    while i < len(tokens):
        tok = tokens[i]
        if tok in MERGE_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if not seen_verb:
            # `gh pr merge` / `glab mr merge` — сама команда, не цель.
            seen_verb = tok in ("merge",)
            i += 1
            continue
        return tok
    return None


def check_target(segment: str, work_dir: str) -> int:
    """Мержится ли ИМЕННО та ветка, дифф которой проверил гейт.

    `gh pr merge 123` сольёт чужой PR, а гейт посчитает файлы, красную зону и
    расписки по текущей ветке — то есть валидная расписка на свою работу
    авторизует мерж чужой, никем не смотренной. Поэтому явно названная цель
    сверяется с веткой проверенного дерева.

    Проверка падает ЗАКРЫТО: не удалось выяснить, чья это ветка, — отказ. Это
    граница попадания кода в основную ветку, и «наверное, та самая» здесь не
    аргумент.
    """
    tokens, _env = G.peel_wrappers(G.tokenize(segment))
    if not tokens or os.path.basename(tokens[0]) != "gh":
        # Резолвер здесь один — `gh`. Спрашивать им про GitLab бессмысленно:
        # получился бы вечный отказ с советом, не относящимся к делу. Для
        # другого хостинга допишите свой резолвер рядом с _pr_head.
        return 0
    target = merge_target(tokens)
    if target is None:
        return 0        # цель не названа — сольётся PR текущей ветки
    if UNRESOLVED_RE.search(target):
        return G.block(
            f"🛑 Гейт мержа: цель «{target}» подставляется оболочкой и до проверки "
            "не доходит.\n\nГейт не может убедиться, что сливается именно та ветка, "
            "дифф которой он посмотрел. Напишите цель явно или слейте PR из его "
            "собственной рабочей копии, без аргумента."
        )

    branch = G.current_branch(work_dir)
    resolved = _pr_head(target, work_dir)
    head = resolved[0] if resolved else None
    if head is None:
        return G.block(
            f"🛑 Гейт мержа: не удалось выяснить, какая ветка стоит за «{target}».\n\n"
            "Гейт считает файлы, критические пути и расписки по ТЕКУЩЕЙ ветке. Если "
            "команда сливает другой PR, расписка на свою работу авторизовала бы "
            "мерж чужой, никем не смотренной.\n\n"
            "Слейте PR из его собственной рабочей копии — без явного номера."
        )
    if head != branch:
        return G.block(
            f"🛑 Гейт мержа: сливается не та ветка, дифф которой проверен.\n\n"
            f"  Проверено:  {branch} (дерево {work_dir})\n"
            f"  Сливается:  {head} (цель «{target}»)\n\n"
            "Расписка привязана к диффу проверенного дерева и на чужой PR не "
            "распространяется.\n\n"
            "Перейдите в рабочую копию того PR и слейте его оттуда."
        )

    # Ветка та же — но вершина у неё может быть чужая: другая копия успела
    # запушить, и расписка описывает дифф, которого в PR уже нет.
    _code, local = G.git(["rev-parse", "HEAD"], work_dir)
    if local.strip() and resolved[1] and local.strip() != resolved[1]:
        return G.block(
            "🛑 Гейт мержа: удалённая ветка PR ушла вперёд проверенной копии.\n\n"
            f"  Здесь:    {local.strip()[:12]}\n"
            f"  В PR:     {resolved[1][:12]}\n\n"
            "Расписки описывают дифф ЭТОЙ копии, а сольётся то, что лежит в PR. "
            "Заберите чужие коммиты (`git fetch` + `git merge`), прогоните ревью "
            "заново и повторите."
        )
    return 0


def _pr_head(target: str, work_dir: str) -> tuple[str, str] | None:
    """Ветка-источник указанного PR и её вершина. None — выяснить не удалось."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", target, "--json", "headRefName,headRefOid",
             "-q", ".headRefName + \" \" + .headRefOid"],
            cwd=work_dir, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.split()
    return (parts[0], parts[1]) if proc.returncode == 0 and len(parts) == 2 else None


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
