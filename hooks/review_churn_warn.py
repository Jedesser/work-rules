#!/usr/bin/env python3
"""Хук ДО вызова Agent/Task — предупреждение о «шлифовке».

Ничего не блокирует. Считает, сколько РАЗНЫХ версий диффа этот ревьюер
уже смотрел на этой ветке за последние часы, и, начиная с четвёртой,
показывает человеку короткое предупреждение.

Зачем: гейты умеют требовать наличие расписки, но никто не считает,
сколько раз одну и ту же ветку прогнали по кругу. У нас один PR сжёг
36 прогонов ревьюеров там, где хватало одного финального круга; примерно
две трети — самонаведённые повторы из-за реакции на мелкие замечания
отдельным кругом ревью.

Порог 3 подобран так, чтобы законный сценарий молчал: первый прогон,
повтор после влития основной ветки, ещё один после пакета правок — это
три. Четвёртый — уже повод спросить себя, финальный ли дифф.

Считается по распискам, без единого вызова git: тело расписки и есть
отпечаток той версии, которую ревьюер видел.

Регистрация: PreToolUse, matcher "Agent|Task" (после предполётной проверки).
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

THRESHOLD_DISTINCT_PRIOR = 3
WINDOW_SECONDS = 3 * 3600


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") not in ("Agent", "Task"):
        return 0
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    # Фоновый запуск расписку не выписывает и повторного гейта не стоит —
    # предупреждать о нём значит шуметь.
    if tool_input.get("run_in_background") is not False:
        return 0

    name = G.detect_subagent(tool_input)
    if not name:
        return 0

    work_dir = G.resolve_work_dir(data.get("cwd"))
    branch_dir = G.receipts_dir(G.repo_name(), G.current_branch(work_dir))
    prior = _distinct_prior(branch_dir, name)
    if prior < THRESHOLD_DISTINCT_PRIOR:
        return 0

    return G.advise(
        f"⚠️ Повторное ревью: `{name}` уже смотрел эту ветку в {prior} разных "
        f"версиях за последние {WINDOW_SECONDS // 3600} ч — это будет {prior + 1}-я. "
        "Расписка привязана к отпечатку всего диффа, поэтому каждая правка после "
        "ревью заставляет заново прогонять ВЕСЬ набор ревьюеров, а не только того, "
        "чью область правили. Вопрос перед запуском: дифф точно финальный? "
        "Не блокирую."
    )


def _distinct_prior(branch_dir: str, name: str) -> int:
    cutoff = time.time() - WINDOW_SECONDS
    seen: set[str] = set()
    try:
        entries = os.listdir(branch_dir)
    except OSError:
        return 0
    for fname in entries:
        if not fname.startswith(f"{name}-"):
            continue
        full = os.path.join(branch_dir, fname)
        try:
            if not os.path.isfile(full) or os.path.getmtime(full) < cutoff:
                continue
            with open(full, encoding="utf-8") as f:
                first = f.readline().strip()
        except OSError:
            continue
        if first:
            seen.add(first)
    return len(seen)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
