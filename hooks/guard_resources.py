#!/usr/bin/env python3
"""Хук ДО вызова Bash — ресурсные ограничители.

Два запрета, каждый рождён конкретным инцидентом. Их стоит заводить не
«на всякий случай», а ровно под то, что у вас уже один раз сожгло
ресурсы.

1. ПРОГОН ТЕСТОВ БЕЗ ВНЕШНЕГО ПОТОЛКА ПО ВРЕМЕНИ.
   Тест, который лезет в сеть или ждёт блокировку, висит ровно столько,
   сколько ему позволят. У нас такой прогон на брошенном агенте съел
   около 11.5 часов, прежде чем это заметили. Потолок ставится снаружи
   (`timeout N ...`), а не настройкой внутри тестового фреймворка:
   зависший процесс свою собственную настройку уже не прочитает.
   Заодно требуется сузить прогон до изменённой области, а не «все тесты».

2. ТЯЖЁЛАЯ СБОРКА НА МАШИНЕ РАЗРАБОТКИ.
   Место production-сборки — в CI, где она идёт на выделенных ресурсах
   и является обязательной проверкой. Локально она занимает в разы
   больше времени, а две параллельные уводят машину в своп. Оставлен
   эвакуационный выход (переменная окружения) — на случай, когда
   отлаживают саму сборку.

Оба списка пусты по умолчанию и заполняются в gates.config.json под ваш
стек. Пустой список = проверка выключена.

Регистрация: PreToolUse, matcher "Bash".
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gatelib as G  # noqa: E402

TIMEOUT_PREFIX_RE = re.compile(r"^\s*(timeout|gtimeout)\s+(-\S+\s+)*\d+")


def main() -> int:
    data = G.read_hook_input()
    if str(data.get("tool_name") or "") != "Bash":
        return 0
    command = str((data.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    cfg = G.load_config()

    for segment in G.split_segments(command):
        # --- запрещённые локально команды ---
        for pattern in cfg.get("forbidden_local_commands", []):
            if re.search(pattern, segment):
                escape = cfg.get("forbidden_local_escape_env") or ""
                if escape and os.environ.get(escape) == "1":
                    continue
                _tokens, env = G.strip_env_prefix(G.tokenize(segment))
                if escape and env.get(escape) == "1":
                    continue
                return G.block(
                    f"🛑 Эта команда не запускается на машине разработки:\n    {segment.strip()}\n\n"
                    "Её место — в пайплайне, где она идёт на выделенных ресурсах и "
                    "является обязательной проверкой. Локально она занимает кратно "
                    "больше времени, а две параллельные уводят машину в своп.\n"
                    + (f"\nОтладка самой сборки: {escape}=1 <команда>.\n" if escape else "")
                )

        # --- тесты без внешнего потолка ---
        for pattern in cfg.get("require_timeout_for", []):
            if not re.search(pattern, segment):
                continue
            if TIMEOUT_PREFIX_RE.match(segment.strip()):
                continue
            return G.block(
                f"🛑 Прогон тестов без внешнего потолка по времени:\n    {segment.strip()}\n\n"
                "Тест, который ждёт сеть или блокировку, висит столько, сколько ему "
                "позволят — а брошенный агент этого не заметит.\n\n"
                "Запускайте так:\n"
                "    timeout 120 <ваша команда тестов, суженная до изменённой области>\n\n"
                "Потолок ставится СНАРУЖИ: зависший процесс свою внутреннюю "
                "настройку таймаута уже не прочитает."
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Ресурсный ограничитель падает ОТКРЫТО: он бережёт деньги, а не
        # безопасность, и не должен блокировать работу из-за своей ошибки.
        sys.exit(0)
