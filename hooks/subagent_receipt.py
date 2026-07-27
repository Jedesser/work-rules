#!/usr/bin/env python3
"""Хук ПОСЛЕ вызова инструмента Agent/Task — выписывает расписку.

Что такое расписка: файл, подтверждающий, что ревьюер такой-то видел
ИМЕННО ЭТИ изменения. Это единственное, что превращает «я провёл ревью»
из обещания в проверяемый факт.

  ~/.claude/state/subagent-receipts/<репозиторий>/<ветка>/
      <ревьюер>-<время>-<начало отпечатка>
  тело файла: отпечаток диффа против точки ветвления

Отпечаток в ИМЕНИ файла нужен, чтобы две расписки одного ревьюера,
выписанные в одну и ту же секунду, не оказались одним путём: вторая
затирала бы первую, и счётчик повторных кругов ревью недосчитывался.

Расписка НЕ выписывается, если:
  * это не ревьюер (обычный субагент ничего не гейтит);
  * инструмент вернул ошибку;
  * запуск был фоновым — тогда в ответе лежит только «агент стартовал»,
    а находок ещё нет. Поручиться тут — значит поручиться за ревью,
    которого не было. Ревьюеры запускаются синхронно, и не только ради
    расписки: их находки нужно прочитать до того, как действовать.

Регистрация: PostToolUse, matcher "Agent|Task".
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") not in ("Agent", "Task"):
        return 0

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    resp = data.get("tool_response")
    if isinstance(resp, dict) and resp.get("is_error"):
        return 0
    if G.is_background_launch(resp):
        return 0

    name = G.detect_subagent(tool_input)
    if not name:
        return 0

    work_dir = G.resolve_work_dir(data.get("cwd"))
    diff_sha = G.compute_diff_sha(work_dir)
    # Не удалось посчитать — расписки нет. Подставлять сюда любую
    # строку-заглушку нельзя: она совпадёт и у писателя, и у читателя,
    # и гейт пропустит непроверенный код.
    if diff_sha is None:
        return 0

    try:
        G.write_receipt(name, work_dir, diff_sha)
        _cleanup_old()
    except Exception:
        pass
    return 0


def _cleanup_old(ttl_seconds: int = 24 * 3600) -> None:
    """Убирает расписки старше суток — каталог не должен расти вечно."""
    import time

    root = G.receipts_root()
    cutoff = time.time() - ttl_seconds
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Писатель расписок не имеет права ломать работу агента.
        sys.exit(0)
