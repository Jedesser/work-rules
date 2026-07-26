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

Регистрация: PreToolUse, matcher "Edit|Write|MultiEdit|NotebookEdit".
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


def main() -> int:
    data = G.read_hook_input()
    tool = str(data.get("tool_name") or "")
    field = FILE_FIELDS.get(tool)
    if field is None:
        return 0

    cfg = G.load_config()
    shared = cfg.get("shared_checkout")
    if not shared:
        return 0

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
