#!/usr/bin/env python3
"""Хук ДО вызова Bash — запрет разрушительных команд.

Самый простой слой и единственный, который работает независимо от того,
что агент помнит и во что верит.

Что блокируется по умолчанию (список правится в gates.config.json):
  * переписывание истории: rebase, commit --amend, push --force
  * push прямо в основную ветку
  * массовое добавление файлов (`git add -A` / `git add .`) — так в
    коммит попадают секреты, мусор и чужие изменения
  * `git reset --hard`, `git clean -f` — молча уничтожают работу
  * `rm` одновременно с -r и -f (в любом написании и через find/xargs)
  * потоковые редакторы файлов «на месте» (sed -i, perl -i): правят файл
    без обозримого дифференциального следа, ревьюить нечего

Устройство разбора важнее самого списка. Все обходы, которые мы реально
встречали, ловятся только токенизацией:

  FOO=1 git rebase main        приставка переменной окружения
  /usr/bin/git rebase main     абсолютный путь вместо имени
  rm -r -f dir                 разнесённые флаги вместо -rf
  find . -name x -exec rm -rf  вызов через другую команду
  echo ok && git rebase main   несколько команд в одной строке

Поиск подстроки не ловит ни один из них — и при этом ложно срабатывает
на безобидном `echo "не делай git rebase"`.

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

SED_NAMES = {"sed", "gsed", "ssed", "minised"}
RM_NAMES = {"rm"}
INPLACE_RE = re.compile(r"^-{1,2}i")


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    cfg = G.load_config()
    main_branch = cfg["main_branch"]

    for segment in G.split_segments(command):
        tokens = G.tokenize(segment)
        if not tokens:
            continue
        stripped, _env = G.strip_env_prefix(tokens)
        if not stripped:
            continue
        exe = os.path.basename(stripped[0])

        # --- git ---
        parsed = G.parse_git(tokens)
        if parsed:
            verdict = check_git(parsed, main_branch)
            if verdict:
                return G.block(verdict)

        # --- rm -rf, в том числе разнесённое и через find/xargs ---
        verdict = check_rm(stripped)
        if verdict:
            return G.block(verdict)

        # --- потоковые редакторы «на месте» ---
        if exe in SED_NAMES and any(INPLACE_RE.match(t) for t in stripped[1:]):
            return G.block(
                "🛑 Правка файла потоковым редактором на месте запрещена.\n"
                "Причина: файл переписывается без обозримого пофайлового дифференциала — "
                "ревьюить нечего, а ошибка обнаруживается уже в проде.\n"
                "Используйте обычные операции чтения/правки/записи файла."
            )
        if exe in {"perl", "ruby"} and "-i" in stripped[1:]:
            return G.block(
                "🛑 Правка файла на месте через интерпретатор запрещена — "
                "по той же причине, что и sed -i: изменение не поддаётся ревью."
            )

    return 0


def check_git(parsed: dict, main_branch: str) -> str | None:
    sub = parsed.get("subcommand")
    args = parsed.get("args", [])

    if sub == "rebase":
        return (
            "🛑 `git rebase` запрещён.\n"
            "Переписанная история ломает уже выданные ссылки на коммиты и чужие копии.\n"
            "Вместо этого: `git merge` основной ветки внутрь своей."
        )
    if sub == "commit" and ("--amend" in args):
        return (
            "🛑 `git commit --amend` запрещён.\n"
            "Он подменяет уже сделанный коммит: доказательства (расписки, ссылки, "
            "порядок работ) начинают описывать то, чего в истории больше нет.\n"
            "Вместо этого: новый коммит поверх."
        )
    if sub == "push":
        if any(a in ("--force", "-f", "--force-with-lease") for a in args):
            return (
                "🛑 Принудительная отправка запрещена.\n"
                "Она молча уничтожает чужие коммиты в общей ветке."
            )
        positional = [a for a in args if not a.startswith("-")]
        # `git push origin main` / `git push origin HEAD:main`
        if any(a == main_branch or a.endswith(f":{main_branch}") for a in positional[1:]):
            return (
                f"🛑 Прямая отправка в `{main_branch}` запрещена.\n"
                "Всё попадает в основную ветку только через PR и его проверки."
            )
    if sub == "add" and any(a in ("-A", "--all", ".") for a in args):
        return (
            "🛑 `git add -A` / `git add .` запрещены.\n"
            "Так в коммит попадают секреты, временные файлы и чужие изменения, "
            "которые вы не видели.\n"
            "Добавляйте файлы поимённо — это заодно заставляет посмотреть на них."
        )
    if sub == "reset" and "--hard" in args:
        return (
            "🛑 `git reset --hard` запрещён — необратимо уничтожает несохранённую работу.\n"
            "Если нужно отступить: `git stash`, либо новый коммит с откатом."
        )
    if sub == "clean" and any(a.startswith("-") and "f" in a for a in args):
        return (
            "🛑 `git clean -f` запрещён — удаляет файлы, о которых git не знает "
            "(в том числе локальные наработки другого агента или человека)."
        )
    return None


def check_rm(tokens: list[str]) -> str | None:
    """Ищет rm одновременно с рекурсией и силой — где бы он ни стоял.

    Проверяются все позиции, потому что rm часто вызывается не первым
    словом: `find . -name '*.tmp' -exec rm -rf {} +`, `xargs rm -rf`.
    """
    for idx, tok in enumerate(tokens):
        if os.path.basename(tok) not in RM_NAMES:
            continue
        flags = [t for t in tokens[idx + 1:] if t.startswith("-")]
        letters = set()
        for f in flags:
            if f.startswith("--"):
                if f == "--recursive":
                    letters.add("r")
                if f == "--force":
                    letters.add("f")
            else:
                letters.update(f[1:].lower())
        if "r" in letters and "f" in letters:
            return (
                "🛑 `rm` с рекурсией и силой одновременно запрещён.\n"
                "Одна опечатка в пути — и стирается то, чего никто не собирался стирать.\n"
                "Удаляйте адресно, а каталоги — после того, как посмотрели, что внутри."
            )
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Запрет разрушительного падает ЗАКРЫТО: непонятная ошибка не
        # должна превращаться в разрешение.
        sys.stderr.write(f"🛑 Проверка команды не отработала: {exc}\n")
        sys.exit(2)
