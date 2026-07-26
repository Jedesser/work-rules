#!/usr/bin/env python3
"""Хук ДО вызова Bash — «сначала задача».

Блокирует создание PR/MR, если выполнены ВСЕ условия:
  * ветка не входит в список освобождённых (горячий фикс — исключение
    по существу: там скорость важнее учёта, задача заводится после);
  * дифф относительно базовой ветки трогает больше N файлов
    (тривиальную правку заводить в трекер — бюрократия);
  * в описании PR нет ссылки на задачу.

Зачем: трекер задач — единственное общее состояние между сессиями,
агентами и человеком. Память агента персональна и другим не видна.
Работа без ссылки на задачу через неделю не находится вообще никак.

Порог здесь ОДИН И ТОТ ЖЕ с порогом «нужен ли план» (см. plan_gate.py) —
два разных числа для одного и того же вопроса «это уже серьёзно?»
разъедутся и запутают всех.

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
    # Совпадение от начала сегмента: `gh  pr create` с двумя пробелами не
    # должен проходить мимо, а `echo "gh pr create"` — блокироваться.
    hits = G.invocations(command, cfg["pr_create_patterns"], str(data.get("cwd") or ""))
    if not hits:
        return 0

    for segment, seg_dir in hits:
        code = check_tree(segment, G.resolve_work_dir(seg_dir), cfg)
        if code:
            return code
    return 0


def check_tree(segment: str, work_dir: str, cfg: dict) -> int:
    branch = G.current_branch(work_dir)
    if any(branch.startswith(p) for p in cfg["issue_exempt_branch_prefixes"]):
        return 0

    files = G.changed_files(work_dir)
    if len(files) <= cfg["issue_gate_min_files"]:
        return 0

    body = extract_body(segment, work_dir)
    if body is None:
        # Описание не удалось прочитать — падаем ЗАКРЫТО и просим указать
        # ссылку явно. Иначе гейт обходится любой непонятной формой вызова.
        body = ""
    if re.search(cfg["issue_link_regex"], body, re.IGNORECASE):
        return 0

    return G.block(
        "🛑 Нет ссылки на задачу в описании PR.\n\n"
        f"Ветка: {branch}\nФайлов в диффе: {len(files)} (порог {cfg['issue_gate_min_files']})\n\n"
        "Трекер задач — единственное общее состояние между сессиями, агентами и "
        "человеком; память агента другим не видна. Без ссылки эта работа через "
        "неделю не находится никак.\n\n"
        "Как пройти:\n"
        "  1. Добавить в описание строку вида `Closes #<номер>`.\n"
        "  2. Задачи нет — завести её сейчас: цель, зачем, где, критерии приёмки, "
        "«готово, когда» (одно проверяемое условие), заметки.\n"
        "  3. Это настоящий горячий фикс — назвать ветку с префиксом "
        f"{cfg['issue_exempt_branch_prefixes']} и завести задачу сразу после мержа."
    )


CAT_SUBSTITUTION_RE = re.compile(r"\$\(\s*cat\s+([^)|;&]+?)\s*\)")


def extract_body(segment: str, work_dir: str) -> str | None:
    """Достаёт описание PR из команды: --body, --body-file или --fill."""
    tokens, _env = G.peel_wrappers(G.tokenize(segment))
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-b", "--body") and i + 1 < len(tokens):
            return _resolve_body(tokens[i + 1], work_dir)
        if tok.startswith("--body="):
            return _resolve_body(tok.split("=", 1)[1], work_dir)
        if tok in ("-F", "--body-file") and i + 1 < len(tokens):
            return _read(os.path.join(work_dir, tokens[i + 1]), tokens[i + 1])
        if tok.startswith("--body-file="):
            p = tok.split("=", 1)[1]
            return _read(os.path.join(work_dir, p), p)
        if tok == "--fill":
            # Описание берётся из сообщений коммитов ветки.
            return _commit_messages(work_dir)
        i += 1
    # Флага описания нет вовсе — поведение как у --fill.
    return _commit_messages(work_dir)


def _resolve_body(value: str, work_dir: str) -> str:
    """Раскрывает `--body "$(cat файл)"` — самую частую живую запись.

    Подстановка выполняется оболочкой, до перехватчика она не доходит, и без
    этого шага описание выглядит пустым: ссылка на задачу в файле есть, а гейт
    отказывает. Отказ, который человек считает несправедливым, учатся обходить,
    поэтому дешевле прочитать файл.
    """
    def sub(m: re.Match) -> str:
        path = m.group(1).strip().strip("\"'")
        return _read(os.path.join(work_dir, path), path) or m.group(0)

    return CAT_SUBSTITUTION_RE.sub(sub, value)


def _commit_messages(work_dir: str) -> str | None:
    base = G.merge_base(work_dir)
    if not base:
        return None
    _code, out = G.git(["log", "--format=%B", f"{base}..HEAD"], work_dir)
    return out


def _read(*candidates: str) -> str | None:
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Учётный гейт падает ОТКРЫТО: он про порядок, а не про безопасность.
        sys.exit(0)
