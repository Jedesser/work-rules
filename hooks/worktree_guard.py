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
что агент ДОБРОВОЛЬНО выбрал инструмент правки: `cat > файл`, `cp`, любой
форматтер или генератор кода из оболочки пишут в ту же общую копию, а
запрет на редактор при этом создаёт впечатление работающей защиты.

Регистрация: PreToolUse, matcher "Edit|Write|MultiEdit|NotebookEdit|Bash".
"""

from __future__ import annotations

import os
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
WRITING_COMMANDS = {
    "cp", "mv", "rm", "rmdir", "mkdir", "touch", "install", "dd", "truncate",
    "ln", "chmod", "chown", "tee", "patch", "rsync", "unzip", "tar",
}
# Подкоманды git, меняющие рабочее дерево. `git commit` сюда не входит: он
# меняет историю, а не файлы, и у него свой гейт.
WRITING_GIT = {"checkout", "switch", "restore", "apply", "stash", "clean",
               "reset", "merge", "pull", "cherry-pick", "revert", "am"}
REDIRECT_TOKENS = (">", ">>", ">|")


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
        parsed = G.parse_git(tokens)

        writes = name in WRITING_COMMANDS
        if parsed and parsed.get("subcommand") in WRITING_GIT:
            writes = True
            seg_dir = G.git_dash_c_dir(parsed) or seg_dir

        # Перенаправление пишет независимо от того, что за команда слева.
        for i, tok in enumerate(tokens):
            for op in REDIRECT_TOKENS:
                if tok == op and i + 1 < len(tokens):
                    found.append((f"перенаправление {op}", normalize(tokens[i + 1], seg_dir)))
                elif tok.startswith(op) and len(tok) > len(op):
                    found.append((f"перенаправление {op}", normalize(tok[len(op):], seg_dir)))

        if not writes:
            continue
        # Проверяются ИМЕННО пути аргументов, а не каталог сегмента: `rm /tmp/x`
        # из общей копии ничего в ней не меняет, и отказ на нём — ложный.
        # Относительный путь normalize() и так привяжет к каталогу сегмента.
        targets = [a for a in peeled[1:] if not a.startswith("-")]
        if parsed:
            targets = [a for a in parsed.get("args", []) if not a.startswith("-")]
            # `git checkout` без путей меняет всё дерево целиком.
            if not targets:
                found.append((f"`git {parsed['subcommand']}` меняет дерево", normalize(seg_dir)))
        for arg in targets:
            found.append((f"аргумент `{name}`", normalize(arg, seg_dir)))
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
