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

# `-i` у интерпретатора: только среди ОДНОБУКВЕННЫХ флагов и только до
# первого флага, забирающего значение. Наивное «в токене есть буква i»
# запрещало совершенно обычные `perl -Mstrict -e …` и `ruby -Ilib -e …`, а
# перехватчик, мешающий обычной работе, живёт до первого раздражения.
INTERPRETER_INPLACE_RE = re.compile(r"^-[acdlnpsvwx]*i(?:\.[^ ]*)?(?:[acdlnpsvwx]*)$")


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    cfg = G.load_config()
    main_branch = cfg["main_branch"]

    # Каталог считается лениво: он нужен ровно одной проверке (отправка без
    # явной ветки), а его вычисление — это вызов git. Считать его на КАЖДЫЙ
    # вызов Bash значит платить запуском процесса за команды, в которых
    # никакого git нет вовсе.
    session_cwd = str(data.get("cwd") or "")

    # Сегменты идут с каталогом, в котором выполнятся: `cd <другая копия> &&
    # git push` спрашивает про ветку ТОЙ копии.
    for segment, seg_dir in G.segments_with_dirs(command, session_cwd):
        tokens = G.tokenize(segment)
        if not tokens:
            continue
        stripped, _env = G.strip_env_prefix(tokens)
        stripped, _env2 = G.peel_wrappers(stripped)
        if not stripped:
            continue
        exe = os.path.basename(stripped[0])

        # --- git ---
        parsed = G.parse_git(tokens)
        if parsed:
            # `git -C <путь> push` спрашивает про ветку ТОГО дерева: текущая
            # ветка сессии к нему отношения не имеет, и проверка «отправка без
            # явной ветки» по ней дала бы вердикт про чужой репозиторий.
            target = G.git_dash_c_dir(parsed)
            verdict = check_git(
                parsed, main_branch,
                lambda t=target, d=seg_dir: G.resolve_work_dir(
                    t if t and os.path.isdir(t) else d))
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
        # `-i` у интерпретатора почти всегда стоит слитно с другими буквами
        # (`perl -pi -e …`, `perl -i.bak -pe …`), поэтому точное сравнение
        # токена пропускало ровно те формы, которыми правку и делают.
        if exe in {"perl", "ruby"} and any(INTERPRETER_INPLACE_RE.match(t) for t in stripped[1:]):
            return G.block(
                "🛑 Правка файла на месте через интерпретатор запрещена — "
                "по той же причине, что и sed -i: изменение не поддаётся ревью."
            )

    return 0


# Только те, что берут значение ОТДЕЛЬНЫМ словом. `-u` сюда не входит
# намеренно: у `git push -u origin feature` он значения не берёт, и лишний
# пропуск съел бы имя удалённого репозитория.
PUSH_VALUE_FLAGS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _push_refspecs(args: list[str]) -> list[str]:
    """Что именно отправляют: всё позиционное после имени удалённого репозитория.

    Значения флагов приходится пропускать явно: у `git push -o ci.skip` наивный
    отбор «токены без дефиса» принимает `ci.skip` за ветку, решает, что ветка
    указана, и молча снимает проверку «отправка без явной ветки».
    """
    positional: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            i += 2 if tok in PUSH_VALUE_FLAGS and i + 1 < len(args) else 1
            continue
        positional.append(tok)
        i += 1
    return positional[1:]


def _refspec_targets(refspecs: list[str], current: str) -> tuple[list[str], bool]:
    """Во что реально попадёт отправка — и нет ли принудительности в самой записи.

    Одна и та же ветка пишется пятью способами, и запрет, знающий только два из
    них, выглядит рабочим, оставаясь дырявым:

      main · HEAD:main · refs/heads/main · +main (это force!) · HEAD (с main)

    `+` перед источником — полноценный `--force`, только записанный так, что
    флага в команде нет вовсе.
    """
    targets: list[str] = []
    forced = False
    for spec in refspecs:
        s = spec
        if s.startswith("+"):
            forced = True
            s = s[1:]
        dst = s.split(":", 1)[1] if ":" in s else s
        for prefix in ("refs/heads/", "heads/"):
            if dst.startswith(prefix):
                dst = dst[len(prefix):]
                break
        if dst in ("HEAD", "@") and current:
            dst = current
        targets.append(dst)
    return targets, forced


def _has_letter(tok: str, letter: str) -> bool:
    """Есть ли буква среди ОДНОБУКВЕННЫХ флагов связки: `-fu`, `-Av`, `-f`."""
    return tok.startswith("-") and not tok.startswith("--") and letter in tok[1:]


def check_git(parsed: dict, main_branch: str, work_dir) -> str | None:
    """work_dir передаётся ЛЕНИВО (вызываемым объектом): он нужен одной ветке
    проверки, а его вычисление стоит запуска git."""
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
        # Слитные связки (`-fu`) — обычная запись, и точное сравнение токена
        # пропускало именно её. Проверка по буквам, как у `git clean`.
        # `--force-with-lease=<ссылка>` и `--force-if-includes` — те же записи
        # принудительной отправки; точное сравнение их не ловит, а форма с `=`
        # как раз рекомендуется в большинстве руководств.
        if any(a.startswith("--force") or _has_letter(a, "f") for a in args):
            return (
                "🛑 Принудительная отправка запрещена.\n"
                "Она молча уничтожает чужие коммиты в общей ветке."
            )
        # `--all` / `--mirror` отправляют все ветки разом, включая основную.
        if any(a in ("--all", "--mirror") for a in args):
            return (
                f"🛑 `git push {[a for a in args if a in ('--all', '--mirror')][0]}` запрещён — "
                f"он отправляет все ветки разом, в том числе `{main_branch}`.\n"
                "Всё попадает в основную ветку только через PR и его проверки."
            )
        refspecs = _push_refspecs(args)
        if refspecs:
            # Текущая ветка нужна только чтобы раскрыть `HEAD`; в остальных
            # записях она не участвует, и вызывать git ради неё незачем.
            current = G.current_branch(work_dir()) if any(
                r.lstrip("+").split(":")[-1] in ("HEAD", "@") for r in refspecs) else ""
            targets, forced = _refspec_targets(refspecs, current)
            if forced:
                return (
                    "🛑 Принудительная отправка запрещена.\n"
                    "`+` перед ссылкой — это тот же `--force`, только без флага: "
                    "он так же молча уничтожает чужие коммиты."
                )
            if main_branch in targets:
                return (
                    f"🛑 Прямая отправка в `{main_branch}` запрещена.\n"
                    "Всё попадает в основную ветку только через PR и его проверки."
                )
        # Без явной ветки git отправляет текущую — самая частая форма записи,
        # и раньше именно она проходила мимо запрета.
        elif G.current_branch(work_dir()) == main_branch:
            return (
                f"🛑 Отправка без явной ветки из `{main_branch}` запрещена — "
                f"git отправит текущую ветку, то есть прямо в основную.\n"
                "Всё попадает в основную ветку только через PR и его проверки."
            )
    # `:/` — «всё от корня репозитория», `"*"` — глоб, который раскрывает сам
    # git. Обе записи делают ровно то же, что `-A`, и обе встречаются живьём.
    if sub in ("add", "stage") and any(
        a in ("--all", "--no-ignore-removal", ".") or a.startswith(":/")
        or a.strip("'\"") == "*" or _has_letter(a, "A") for a in args
    ):
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
    if sub == "clean" and any(a == "--force" or _has_letter(a, "f") for a in args):
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
