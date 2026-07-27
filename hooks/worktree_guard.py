#!/usr/bin/env python3
"""Хук ДО правки файла — запрет писать в общую копию репозитория.

Правило: каждый редактирующий агент работает в СВОЕЙ рабочей копии.
Общая копия принадлежит человеку и его IDE.

Почему это не косметика: два агента в одной копии молча переключают друг
другу ветку и теряют неотслеживаемые файлы. Отказ тихий, обнаруживается
через часы, а восстановить потерянное неоткуда.

Тонкость, из-за которой наивная проверка не работает: символические
ссылки. Рабочие копии часто ссылаются на общий каталог зависимостей в
основной копии — правка «по пути внутри своей копии» на самом деле
меняет файл в общей. Поэтому путь сначала разыменовывается.

Включается заданием shared_checkout в gates.config.json. Не задан —
проверка выключена.

Направление отказа — ОТКРЫТОЕ, и это осознанный компромисс: правка файла не
уничтожает работу необратимо (её видно в истории и в diff), а перехватчик,
который ломает редактирование из-за собственной ошибки, будет выключен в тот
же день. Если для вас цена смешения копий выше — поменяйте последний блок на
выход с кодом 2.

Оболочка проверяется наравне с редактором. Иначе изоляция держится на том,
что агент ДОБРОВОЛЬНО выбрал инструмент правки: `cat > файл`, `cp`, `git
checkout` из оболочки пишут в ту же общую копию, а запрет на редактор при
этом создаёт впечатление работающей защиты.

Охват честно неполный: ловятся перенаправления, известные пишущие утилиты и
подкоманды git, меняющие дерево. Форматтер, генератор кода или установщик
зависимостей (`gofmt -w .`, `npm ci`) правят файлы сами, и опознать их по
имени нельзя — список получился бы бесконечным и всё равно дырявым. Это
слой, снижающий вероятность, а не доказательство.

Регистрация: PreToolUse, matcher "Edit|Write|MultiEdit|NotebookEdit|Bash".
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

FILE_FIELDS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def normalize(path: str, base: str | None = None) -> str:
    """Разыменовывает путь. Относительный считается от каталога сессии.

    Каталог берётся из полезной нагрузки хука, а не из собственного текущего
    каталога процесса: перехватчик запускается рантаймом откуда угодно, и
    относительный путь, посчитанный от его каталога, указал бы не туда.
    """
    if not path:
        return ""
    if base and not os.path.isabs(path):
        path = os.path.join(base, path)
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def is_under(target: str, root: str) -> bool:
    if not target or not root:
        return False
    return target == root or target.startswith(root + os.sep)


# Команды, которые пишут в файловую систему. Список нарочно короткий и
# состоит из того, чем правят код и файлы проекта: длинный список из всего,
# что теоретически умеет писать, ловил бы обычное чтение и был бы выключен.
# ВСЕ позиционные аргументы — цели записи.
WRITING_COMMANDS = {"rm", "rmdir", "mkdir", "touch", "truncate", "chmod",
                    "chown", "patch", "tee", "dd"}
# У этих пишется ТОЛЬКО последний позиционный, остальные — источники. Без
# такого разделения `cp <общая>/a.txt /tmp/b.txt` (обычное чтение) получал бы
# отказ, а `ln -s <общая>/node_modules node_modules` — рекомендованный способ
# переиспользовать зависимости — становился невозможен.
WRITING_LAST_ARG = {"cp", "mv", "ln", "rsync", "install"}
# Архиваторы: цель задаётся флагом (`-C` куда распаковывать, `-f` какой файл
# создавать), а позиционные — это, наоборот, что читать.
ARCHIVE_DEST_FLAGS = {"tar": ("-C", "--directory", "-f", "--file"),
                      "unzip": ("-d",), "zip": ("-O", "--out")}

# Подкоманды git, меняющие рабочее дерево. `git commit` сюда не входит: он
# меняет историю, а не файлы, и у него свой гейт.
WRITING_GIT = {"checkout", "switch", "restore", "apply", "stash", "clean",
               "reset", "merge", "pull", "cherry-pick", "revert", "am",
               "rm", "mv", "submodule", "sparse-checkout"}
# Режимы тех же подкоманд, которые ничего не меняют. Без этого `git stash list`
# и `git clean -n` (сухой прогон — как раз способ ПОСМОТРЕТЬ, что удалится)
# получают отказ, и защита начинает мешать обычной работе.
READONLY_GIT_ARGS = {"list", "show", "-n", "--dry-run", "--check", "--stat",
                     "--summary", "status"}

# Перенаправление в файл: `>`, `>>`, `>|`, с необязательным номером потока
# (`2>`, `2>>`) и без пробела перед именем (`cat>файл`). Токенизация здесь не
# помогает — оболочка склеивает это в одно слово, а номер потока делает токен
# непохожим ни на один известный оператор.
REDIRECT_RE = re.compile(
    r"""(?<![-<>=])(?:\d+|&)?(>{1,2}\|?)\s*("[^"]*"|'[^']*'|[^\s;&|=][^\s;&|]*)""")


def _redirect_targets(segment: str, seg_dir: str) -> list[tuple[str, str]]:
    out = []
    # Маска общая с ядром: правка в одном месте, иначе копии разъезжаются.
    masked = G.mask_quoted(segment)
    for m in REDIRECT_RE.finditer(masked):
        op = m.group(1)
        # Имя берётся из исходной строки: в маске оно затёрто.
        raw = segment[m.start(2):m.end(2)]
        path = raw.strip("\"'")
        if path and not path.startswith("&"):   # `2>&1` — это поток, не файл
            out.append((f"перенаправление {op}", normalize(path, seg_dir)))
    return out


def bash_targets(command: str, base: str) -> list[tuple[str, str]]:
    """Что эта команда собирается изменить: пары «описание, путь».

    Смотрятся две вещи: КАТАЛОГ, в котором сегмент выполнится (`cd` учтён),
    и явные пути в перенаправлениях и аргументах пишущих команд. Одного
    каталога мало — писать в общую копию можно и абсолютным путём откуда
    угодно.
    """
    found: list[tuple[str, str]] = []
    for segment, seg_dir in G.segments_with_dirs(command, base):
        tokens = G.tokenize(segment)
        if not tokens:
            continue
        peeled, _env = G.peel_wrappers(tokens)
        if not peeled:
            continue
        name = os.path.basename(peeled[0])
        parsed = G.parse_git(tokens, seg_dir)

        # Перенаправление пишет независимо от того, что за команда слева.
        found.extend(_redirect_targets(segment, seg_dir))

        if parsed:
            sub = parsed.get("subcommand")
            args = parsed.get("args", [])
            if sub not in WRITING_GIT or any(a in READONLY_GIT_ARGS for a in args):
                continue
            git_dir = G.git_dash_c_dir(parsed, seg_dir) or seg_dir
            targets = [a for a in args if not a.startswith("-")]
            if not targets:
                # `git checkout` без путей меняет всё дерево целиком.
                found.append((f"`git {sub}` меняет дерево", normalize(git_dir)))
            found.extend((f"аргумент `git {sub}`", normalize(a, git_dir)) for a in targets)
            continue

        # Проверяются ИМЕННО пути аргументов, а не каталог сегмента: `rm /tmp/x`
        # из общей копии ничего в ней не меняет, и отказ на нём — ложный.
        # Относительный путь normalize() и так привяжет к каталогу сегмента.
        positional = [a for a in peeled[1:] if not a.startswith("-")]
        if name in ARCHIVE_DEST_FLAGS:
            flags = ARCHIVE_DEST_FLAGS[name]
            for i, tok in enumerate(peeled):
                if tok in flags and i + 1 < len(peeled):
                    found.append((f"цель `{name}`", normalize(peeled[i + 1], seg_dir)))
        elif name in WRITING_LAST_ARG:
            if positional:
                found.append((f"цель `{name}`", normalize(positional[-1], seg_dir)))
        elif name in WRITING_COMMANDS:
            found.extend((f"аргумент `{name}`", normalize(a, seg_dir)) for a in positional)
    return found


def check_bash(data: dict, cfg: dict, shared_root: str) -> int:
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0
    base = str(data.get("cwd") or "")
    for what, path in bash_targets(command, base):
        if is_under(path, shared_root):
            return G.block(
                "🛑 Запись в общую копию репозитория из оболочки запрещена.\n"
                f"  Что пишет:     {what}\n"
                f"  Цель:          {path}\n\n"
                "Запрет на редактор без запрета на оболочку — видимость защиты: "
                "`cat > файл`, `cp`, форматтер или генератор кода пишут в ту же "
                "общую копию.\n\n"
                "Заведите свою рабочую копию и работайте в ней:\n"
                f"  git -C {cfg['shared_checkout']} worktree add -b <ветка> "
                f"<путь-рядом> {cfg['base_ref']}"
            )
    return 0


def main() -> int:
    data = G.read_hook_input()
    tool = str(data.get("tool_name") or "")
    field = FILE_FIELDS.get(tool)
    if field is None and tool != "Bash":
        return 0

    cfg = G.load_config()
    shared = cfg.get("shared_checkout")
    if not shared:
        return 0

    if tool == "Bash":
        return check_bash(data, cfg, normalize(shared))

    raw = str((data.get("tool_input") or {}).get(field) or "")
    if not raw:
        return 0

    resolved = normalize(raw, data.get("cwd"))
    if not is_under(resolved, normalize(shared)):
        return 0

    return G.block(
        f"🛑 Правка в общей копии репозитория запрещена.\n"
        f"  Инструмент:      {tool}\n"
        f"  Целевой путь:    {raw}\n"
        f"  Разыменован в:   {resolved}\n\n"
        "Общая копия принадлежит человеку и его IDE. Два агента в одной копии "
        "переключают друг другу ветку и теряют неотслеживаемые файлы — тихо.\n\n"
        "Заведите свою рабочую копию и работайте в ней:\n"
        f"  git -C {shared} worktree add -b <ветка> <путь-рядом> {cfg['base_ref']}\n\n"
        "И удалите её, когда задача завершена: «смержено» ещё не значит «сделано»."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
