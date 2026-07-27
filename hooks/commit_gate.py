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
import re
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
    # Сегменты идут вместе с каталогом, в котором выполнятся: `cd` внутри
    # команды меняет дерево для всего, что после него, и без этого гейт
    # проверял бы дифф совсем другой рабочей копии.
    shell_cwd = str(data.get("cwd") or "")
    session_dir = G.resolve_work_dir(shell_cwd)
    segments = G.segments_with_dirs(command, shell_cwd or session_dir)

    # `git add` в той же команде, что и коммит с обходом, — отдельная дыра.
    # Перехватчик видит дерево ДО выполнения, а `git diff` не показывает
    # файлы, о которых git ещё не знает. Значит `git add новый_файл &&
    # REVIEW_DONE=1 git commit` пронёс бы непроверенный файл под распиской,
    # выписанной на дифф без него.
    pending_adds = collect_adds(segments, session_dir)

    for segment, seg_dir in segments:
        tokens = G.tokenize(segment)
        parsed = G.parse_git(tokens, seg_dir)
        if not parsed:
            continue
        if parsed.get("subcommand") == G.SHELL_ALIAS:
            # За псевдонимом-оболочкой может стоять коммит, и проверить это
            # нечем. Пропускать значит отдавать гейт за одну строку конфига.
            return G.block(
                "🛑 Гейт коммита: за псевдонимом стоит команда оболочки — что "
                "именно выполнится, проверке не видно.\n"
                "Напишите команду явно, тогда её можно проверить."
            )
        if parsed.get("subcommand") != "commit":
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

        work_dir = parsed_dir(parsed, G.resolve_work_dir(seg_dir) or session_dir, seg_dir)

        # `git commit -a` / `-am` коммитит всё изменённое, ничего не добавляя
        # в индекс заранее. Смотреть только на индекс здесь означает увидеть
        # пустой набор файлов и мирно пропустить коммит любого размера.
        # Берётся дерево против ПОСЛЕДНЕГО коммита, а не против точки
        # ветвления: `-a` заберёт именно это, а всё, что уже закоммичено на
        # ветке, к текущему коммиту отношения не имеет.
        # `git commit <путь>` — та же дыра другой записью: коммитится
        # содержимое дерева по этому пути, индекс при этом пуст.
        args = parsed.get("args", [])
        staged = G.staged_files(work_dir)
        if staged is None:
            # Посмотреть индекс не удалось. Считать, что он пуст, значит
            # превратить сбой git в разрешение на коммит.
            return G.block(
                "🛑 Гейт коммита: не удалось прочитать индекс "
                f"({work_dir}).\n\nПроверить, что именно коммитится, нечем. "
                "Проверьте состояние репозитория и повторите."
            )
        if commits_all(args) or has_pathspec(args):
            files = sorted(set(G.uncommitted_files(work_dir)) | set(staged))
        else:
            files = staged
        # Пустой набор или мелкое некритичное изменение — этот сегмент вопросов
        # не вызывает. Но выйти отсюда насовсем нельзя: в команде может быть
        # ещё один коммит, и первый безобидный снимал бы гейт со всей строки.
        if not files:
            continue

        big = len(files) >= cfg["commit_gate"]["max_files"]
        critical = [f for f in files if G.is_critical(f, cfg)]
        if not big and not critical:
            continue

        bypass = parsed["env"].get("REVIEW_DONE") == "1" or os.environ.get("REVIEW_DONE") == "1"
        reviewers = G.required_reviewers(files, cfg)

        if not bypass:
            return G.block(_why_blocked(work_dir, files, big, critical, reviewers, cfg))

        unseen = unseen_adds(pending_adds.get(work_dir, []), work_dir)
        if unseen:
            return G.block(
                "🛑 Гейт коммита: в одной команде и `git add`, и обход `REVIEW_DONE=1`.\n\n"
                "Добавляется то, чего в проверенном диффе нет: "
                + ", ".join(sorted(unseen)[:5]) + "\n\n"
                "Расписка подтверждает дифф БЕЗ этих файлов: перехватчик видит дерево до "
                "выполнения, а новый файл в дифф не попадает, пока git о нём не знает.\n\n"
                "Разделите: сначала `git add <файлы>` отдельной командой, затем ревью, "
                "затем коммит."
            )

        ok, problems = G.check_receipts(files, work_dir, cfg)
        if ok:
            continue
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


# Только флаги с ОБЯЗАТЕЛЬНЫМ значением. `-S` / `--gpg-sign` сюда не входят:
# у них значение необязательное и пишется слитно (`-Sключ`), а в списке они
# съедали бы следующий токен — и `git commit -S -am x` терял свой `-a`.
COMMIT_VALUE_FLAGS = {"-m", "-F", "-C", "-c", "--message", "--file", "--author",
                      "--date", "--reuse-message", "--reedit-message", "--fixup",
                      "--squash", "--cleanup"}


# Связка коротких флагов, у которой значение забирает ПОСЛЕДНЯЯ буква:
# `-sm "текст"`, `-am "текст"`, `-qF файл`. Буквы до неё — только те, что
# значения не берут; иначе произвольный аргумент вроде `-Sabc` случайно
# съедал бы следующий токен.
BUNDLE_VALUE_RE = re.compile(r"^-[apevqsnoit]*[mFCc]$")


def _advance(args: list[str], i: int) -> int:
    """Индекс следующего токена: флаг вместе со своим значением."""
    tok = args[i]
    if tok in COMMIT_VALUE_FLAGS or BUNDLE_VALUE_RE.match(tok):
        return i + 2
    return i + 1


def commits_all(args: list[str]) -> bool:
    """Есть ли у `git commit` флаг «взять всё изменённое» — в любом написании.

    Слитные формы (`-am`, `-va`) встречаются чаще раздельных, поэтому проверка
    идёт по буквам, а не по точному совпадению токена.

    Значения флагов пропускаются: иначе сообщение коммита, начинающееся с
    дефиса (`git commit -m '-a quick fix'`), читается как флаг `-a`, и гейт
    считает файлы всего дерева вместо индекса. Направление ошибки безопасное,
    но отказ получается необъяснимым, а такие отказы учатся обходить.
    """
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break
        if G.is_opt(tok, "--all"):
            return True
        if tok.startswith("-") and tok != "-" and not tok.startswith("--") and "a" in tok[1:]:
            return True
        if not tok.startswith("-") or tok == "-":
            # Первое непозиционное слово — путь; дальше флагов «взять всё» нет.
            break
        i = _advance(args, i)
    return False


def has_pathspec(args: list[str]) -> bool:
    """Есть ли у `git commit` позиционный путь.

    `git commit -m x файл.py` коммитит содержимое дерева по этому пути мимо
    индекса — для гейта это ровно тот же случай, что и `-a`.

    `--pathspec-from-file=<файл>` делает то же самое, только пути лежат в
    файле: позиционных аргументов нет вовсе, и наивная проверка «начинается
    ли с дефиса» видит команду без путей. С пустым индексом это значило бы
    «коммитить нечего» — и гейт пропускал бы коммит рабочего дерева.
    """
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return i + 1 < len(args)
        if G.is_opt(tok, "--pathspec-from-file", min_len=5):
            return True
        if not tok.startswith("-") or tok == "-":
            return True
        i = _advance(args, i)
    return False


ADD_SCOPE_UNKNOWN = "<весь индекс>"
# Формы `git add`, которые берут только уже отслеживаемые файлы и по
# определению не могут внести в коммит ничего, чего ревьюер не видел.
ADD_TRACKED_ONLY = {"-u", "--update", "-p", "--patch", "-i", "--interactive"}


def collect_adds(segments: list[tuple[str, str]], session_dir: str) -> dict[str, list[str]]:
    """Что каждый `git add` в этой команде собирается положить в индекс.

    Разложено по деревьям: `git -C /другая/копия add x` к текущему гейту
    отношения не имеет, и запрещать из-за него — ложное срабатывание.

    Пути сразу приводятся к виду «от корня репозитория». В команде они
    записаны относительно каталога ОБОЛОЧКИ, а git отдаёт список изменённых
    файлов от корня дерева — без приведения сравнение шло в разных системах
    координат, и `git add a.txt` из подкаталога блокировался, хотя этот файл
    в проверенном диффе есть.
    """
    out: dict[str, list[str]] = {}
    for segment, shell_cwd in segments:
        parsed = G.parse_git(G.tokenize(segment), shell_cwd)
        # `git stage` — синоним `git add`; отдельный список синонимов не заводим,
        # но сам синоним обязан учитываться, иначе им и обходят.
        if not parsed or parsed.get("subcommand") not in ("add", "stage"):
            continue
        work_dir = parsed_dir(parsed, G.resolve_work_dir(shell_cwd) or session_dir, shell_cwd)
        args = parsed.get("args", [])
        if any(a in ADD_TRACKED_ONLY for a in args):
            continue
        base = G.git_dash_c_dir(parsed, shell_cwd) or shell_cwd or work_dir
        paths = [to_repo_path(a, base, work_dir) for a in args if not a.startswith("-")]
        out.setdefault(work_dir, []).extend(paths or [ADD_SCOPE_UNKNOWN])
    return out


def to_repo_path(path: str, base: str, work_dir: str) -> str:
    try:
        absolute = path if os.path.isabs(path) else os.path.join(base, path)
        rel = os.path.relpath(os.path.realpath(absolute), os.path.realpath(work_dir))
    except (OSError, ValueError):
        return os.path.normpath(path)
    return os.path.normpath(rel)


def unseen_adds(paths: list[str], work_dir: str) -> list[str]:
    """Из добавляемого — то, чего в проверенном диффе ещё нет.

    Покрытым считается ТОЛЬКО точное совпадение с уже изменённым файлом:
    повторный `git add` такого файла ничего к диффу не добавляет, и мешать
    ему — трение без выигрыша.

    Каталог покрытым не считается НИКОГДА. Соблазн засчитать его, если внутри
    есть хоть один проверенный файл, велик и ошибочен: `git add критичный/`
    протащит рядом лежащий новый файл, которого ревьюер не видел, — то есть
    ровно то, ради чего проверка написана, только записанное короче.
    """
    if not paths:
        return []
    known = {os.path.normpath(p) for p in G.changed_files(work_dir)}
    unseen = []
    for p in paths:
        if p in known and not os.path.isdir(os.path.join(work_dir, p)):
            continue
        unseen.append(p)
    return unseen


def parsed_dir(parsed: dict, fallback: str, base_dir: str = "") -> str:
    d = G.git_dash_c_dir(parsed, base_dir)
    if d and os.path.isdir(d):
        return G.resolve_work_dir(d)
    return fallback


def _why_blocked(work_dir, files, big, critical, reviewers, cfg) -> str:
    reasons = []
    if big:
        reasons.append(f"файлов в коммите: {len(files)} (порог {cfg['commit_gate']['max_files']})")
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
