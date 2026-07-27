#!/usr/bin/env python3
"""Общее ядро всех гейтов и перехватчиков комплекта.

Единственный источник истины для трёх вещей, которые ОБЯЗАНЫ совпадать
между «писателем расписок» и «читателями» (гейт коммита, гейт мержа,
предполётная проверка, предупреждение о повторных ревью):

  1. какое рабочее дерево описывает проверка      -> resolve_work_dir()
  2. как считается отпечаток изменений            -> compute_diff_sha()
  3. кто считается ревьюером и что он покрывает   -> detect_subagent(),
                                                     required_reviewers()

Если эти три вещи разъедутся хотя бы на байт, вся система расписок
превращается в театр: расписки будут выписываться про одно дерево, а
гейты проверять другое — и молча всё пропускать. Именно так у нас и
случилось однажды, поэтому здесь один модуль, а не три копии логики.

Никаких сторонних зависимостей: только стандартная библиотека и git.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# sha256 от пустой строки. Отпечаток дерева, в котором нет изменений
# относительно точки ветвления. Знать его нужно, чтобы отличать «нечего
# ревьюить» от «ревью прошло».
# Отпечаток пустого диффа зависит от точки ветвления (она входит в него),
# поэтому «пусто» определяется отдельной функцией, а не сравнением с
# константой: константа молча перестала бы совпадать.

DEFAULT_CONFIG: dict = {
    # Ветка, относительно которой считается «что я изменил».
    # Отпечаток берётся против ТОЧКИ ВЕТВЛЕНИЯ (merge-base), а не против
    # конца этой ветки: конец двигается при каждом fetch и обнулял бы
    # расписки от чужой активности.
    "base_ref": "origin/main",
    "main_branch": "main",

    # Абсолютный путь к общей копии репозитория, которую нельзя править
    # агентам (та, что открыта у человека в IDE). null — проверка выключена.
    "shared_checkout": None,

    # Сколько живёт расписка. Подгоняется под длину типичной сессии.
    "receipt_fresh_seconds": 7200,

    # Когда гейт коммита вообще просыпается.
    "commit_gate": {"max_files": 5},
    # Когда просыпается гейт мержа.
    "merge_gate": {"max_files": 5, "max_additions": 200},

    # Критические пути: их правка требует ревью независимо от размера диффа.
    # ОДИН список на оба гейта — расхождение здесь означает путь, закрытый
    # на коммите и открытый на мерже.
    "critical_paths": [
        r"^\.github/workflows/",
        r"(^|/)k8s/",
        r"(^|/)(Dockerfile|docker-compose\.ya?ml)$",
        r"(^|/)migrations?/",
        r"(^|/)(main|app|server)\.(py|go|ts|js)$",
    ],

    # Красная зона: подмножество критических путей, где цена пропущенной
    # ошибки максимальна (доступ, деньги, права, начисления, подписи).
    # Требует отдельного, более глубокого ревьюера и человеческого решения.
    "red_zone_paths": [],

    # Кто и когда обязан отревьюить.
    "reviewers": {
        # Запускается всегда, когда гейт сработал.
        "always": ["change-reviewer"],
        # Дополнительные ревьюеры по совпадению пути.
        "by_path": [
            # {"pattern": "\\.go$", "reviewer": "language-reviewer"},
        ],
        # Ревьюер красной зоны (пусто — выключено).
        "red_zone": "critical-zone-reviewer",
    },

    # Команды, для которых обязателен внешний потолок по времени.
    # Список должен покрывать НЕ МЕНЬШЕ, чем образцовый конфиг: при ошибке в
    # пользовательском конфиге проверки откатываются сюда, и «умолчания
    # слабее примера» означало бы, что одна лишняя запятая тихо снимает слой.
    "require_timeout_for": [r"\bgo test\b", r"\bpytest\b", r"python[0-9.]* -m pytest\b",
                            r"(poetry|pipenv|uv|pdm|hatch) run .*\bpytest\b",
                            r"\bnpm (run )?test\b", r"\bcargo test\b"],
    # Команды, запрещённые на машине разработки (их место — в CI).
    "forbidden_local_commands": [],
    # Переменная-эвакуационный выход для предыдущего пункта.
    "forbidden_local_escape_env": "HEAVY_BUILD_OK",

    # Как выглядит создание PR и его мерж в вашем CLI.
    "pr_create_patterns": [r"\bgh pr create\b", r"\bglab mr create\b"],
    "pr_merge_patterns": [r"\bgh pr merge\b", r"\bglab mr merge\b",
                          r"\bgh api\b.*\bpulls/\d+/merge\b"],
    # Ветки, освобождённые от требования «сначала задача».
    "issue_exempt_branch_prefixes": ["hotfix/"],
    "issue_link_regex": r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?:\s*:|\s+)\s*(?:[\w.-]+/[\w.-]+)?#\d+",
    "issue_gate_min_files": 2,

    # План до кода (см. SYSTEM.md §2). null — выключено.
    "plan": {
        "enabled": False,
        "dir": "docs/plans",
        "min_files": 2,
        # Строка статуса сверяется ПОБАЙТОВО. Заглушка в шаблоне не
        # совпадает ни с одним вариантом — это и есть «падать закрыто».
        "status_patterns": [
            r"^Статус: согласован владельцем \d{4}-\d{2}-\d{2}$",
            r"^Статус: технический — согласование не требуется$",
        ],
    },

    # Потолок на ОДИН вызов git. Держите его согласованным с таймаутом, с
    # которым перехватчик зарегистрирован в рантайме: если рантайм убьёт хук
    # раньше, чем тот успеет ответить, действие будет РАЗРЕШЕНО — и гейт,
    # объявленный «падающим закрыто», на деле окажется падающим открыто.
    # Потолок считается по числу РАЗЛИЧНЫХ запросов: повторы бесплатны, их
    # кеширует git(). Дешёвые запросы (корень дерева, ветка, точка ветвления,
    # `diff --cached`, `diff HEAD`) идут по git_timeout, полные диффы ветки —
    # по git_diff_timeout. Соотношение «регистрация ≥ потолка» проверяется в
    # ci/check_gates_consistency.py по явной таблице бюджетов: без неё числа
    # здесь и в settings.example.json разъезжаются молча, а расплата —
    # убитый по таймауту гейт, то есть РАЗРЕШЁННОЕ действие.
    "git_timeout": 5,
    # Полный дифф ветки кратно дороже остальных запросов: на большой ветке
    # общий потолок превратился бы в «не удалось посчитать отпечаток».
    "git_diff_timeout": 15,
}

_CONFIG_CACHE: dict | None = None


def project_dir() -> str:
    """Корень проекта, как его видит агент."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def config_path() -> str:
    return os.path.join(project_dir(), ".claude", "gates.config.json")


def load_config() -> dict:
    """Читает gates.config.json поверх дефолтов (мелкое слияние по ключам).

    Отсутствующий или битый файл — не ошибка: работают дефолты. Гейт,
    который падает из-за опечатки в конфиге, выключат в первый же день.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия
    try:
        with open(config_path(), encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as exc:
        # Молча откатываться к значениям по умолчанию нельзя: в них пуст
        # список красной зоны и не задана общая копия, то есть одна лишняя
        # запятая тихо выключает два слоя защиты, а снаружи всё выглядит
        # работающим. Работу не останавливаем, но говорим вслух.
        sys.stderr.write(f"⚠️ Конфигурация проверок не прочитана ({exc}); "
                         "действуют значения по умолчанию — красная зона пуста.\n")
    _CONFIG_CACHE = cfg
    return cfg


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


_GIT_CACHE: dict[tuple, tuple[int, str]] = {}


def git(args: list[str], cwd: str, timeout: int | None = None, cache: bool = True) -> tuple[int, str]:
    """Запускает git и возвращает (код возврата, stdout).

    Никогда не бросает исключение: любая беда превращается в (1, "") —
    решение о том, «падать открыто» или «падать закрыто», принимает
    вызывающий, а не эта функция.

    Результат кешируется на время одного запуска перехватчика — но только у
    ЧИТАЮЩИХ запросов. Изменяющие (единственный такой в комплекте — `fetch`)
    передают cache=False явно: кеш здесь держится на том, что повтор запроса
    даёт тот же ответ, и молча распространить это на запись значит подложить
    мину следующему, кто добавит сюда вызов. Смысл не в скорости: без него число вызовов git росло
    ЛИНЕЙНО по числу команд в строке (четыре коммита в одной строке — под
    полтора десятка вызовов), а общее время упиралось в таймаут, с которым
    перехватчик зарегистрирован. Рантайм убивает его по таймауту — и
    действие РАЗРЕШАЕТСЯ: гейт, объявленный падающим закрыто, на деле
    оказывается падающим открыто ровно на самых длинных командах.
    """
    if not cache:
        return _git_uncached(args, cwd, timeout)
    key = (tuple(args), os.path.realpath(cwd) if cwd else "", timeout)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    result = _git_uncached(args, cwd, timeout)
    _GIT_CACHE[key] = result
    return result


def _git_uncached(args: list[str], cwd: str, timeout: int | None = None) -> tuple[int, str]:
    cfg_timeout = timeout if timeout is not None else load_config()["git_timeout"]
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=cfg_timeout,
        )
        return p.returncode, p.stdout
    except Exception:
        return 1, ""


def resolve_work_dir(cwd: str | None = None) -> str:
    """Дерево, о котором идёт речь — то, в котором СЕЙЧАС сидит сессия.

    Это самое важное место всей системы. Соблазн взять корень проекта
    огромен, и он же — источник тихой поломки: общая копия репозитория
    всегда чистая, поэтому все расписки получат отпечаток пустого диффа
    и подойдут ко всему подряд.
    """
    start = cwd or os.getcwd()
    code, out = git(["rev-parse", "--show-toplevel"], start)
    if code == 0 and out.strip():
        return out.strip()
    return project_dir()


def merge_base(work_dir: str, base_ref: str | None = None) -> str | None:
    """Точка ветвления текущей ветки от базовой. None — если не вышло.

    Повторные вызовы бесплатны: кеш живёт в git() (см. там же — почему это
    не оптимизация, а условие того, что гейт вообще успеет ответить).
    """
    ref = base_ref or load_config()["base_ref"]
    code, out = git(["merge-base", ref, "HEAD"], work_dir)
    if code != 0 or not out.strip():
        return None
    return out.strip()


def compute_diff_sha(work_dir: str, base_ref: str | None = None) -> str | None:
    """Отпечаток всего, что отличает дерево от точки ветвления.

    ОДИН вызов `git diff <merge-base>` без второго ref. Так он видит
    закоммиченное, добавленное в индекс и просто лежащее на диске —
    одной строкой байтов.

    Почему именно так, а не «индекс отдельно, рабочая копия отдельно»:
    такой составной отпечаток менялся бы в момент обычного `git commit`,
    хотя содержимое дерева не поменялось ни на байт — байты просто
    переехали из одной части в другую. Все расписки обнулялись бы ровно
    в тот момент, когда они нужны.

    None означает «посчитать не удалось». Возвращать вместо этого
    какую-нибудь строку-заглушку нельзя: она совпадёт у писателя и у
    читателя и пропустит обход гейта на непроверенном коде.
    """
    base = merge_base(work_dir, base_ref)
    if base is None:
        return None
    code, out = git(["diff", base], work_dir, load_config()["git_diff_timeout"])
    if code != 0:
        return None
    # В отпечаток входит и сама точка ветвления: основная ветка могла уйти
    # вперёд, дифф ветки при этом не изменится, а сольётся уже другой
    # результат — расписка на старую точку его не описывает.
    payload = base + "\n" + out
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def changed_files(work_dir: str, base_ref: str | None = None) -> list[str]:
    """Файлы, отличающие дерево от точки ветвления (включая нестейдженные)."""
    base = merge_base(work_dir, base_ref)
    if base is None:
        return []
    code, out = git(["diff", "--name-only", base], work_dir,
                    load_config()["git_diff_timeout"])
    if code != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def staged_files(work_dir: str) -> list[str]:
    """Файлы в индексе. None — посмотреть не удалось.

    Пустой список и «не смогли посмотреть» — разные вещи: первое значит
    «коммитить нечего», и гейт на этом пропускает команду. Возвращать его
    при сбое git значит превращать сбой в разрешение.
    """
    code, out = git(["diff", "--cached", "--name-only"], work_dir)
    if code != 0:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def uncommitted_files(work_dir: str) -> list[str]:
    """Отслеживаемые файлы, изменённые относительно последнего коммита.

    Ровно то, что заберёт `git commit -a`. Точка ветвления здесь не годится:
    она включает всё, что уже закоммичено на ветке, и гейт начинает считать
    чужие файлы своими — отказ выглядит абсурдно и его перестают уважать.
    """
    code, out = git(["diff", "--name-only", "HEAD"], work_dir)
    if code != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def added_lines(work_dir: str, base_ref: str | None = None) -> int:
    base = merge_base(work_dir, base_ref)
    if base is None:
        return 0
    code, out = git(["diff", "--numstat", base], work_dir,
                    load_config()["git_diff_timeout"])
    if code != 0:
        return 0
    total = 0
    for ln in out.splitlines():
        parts = ln.split("\t")
        if parts and parts[0].isdigit():
            total += int(parts[0])
    return total


def current_branch(work_dir: str) -> str:
    code, out = git(["rev-parse", "--abbrev-ref", "HEAD"], work_dir)
    if code != 0 or not out.strip():
        return "detached"
    return out.strip()


def repo_name() -> str:
    return os.path.basename(os.path.normpath(project_dir())) or "repo"


# ---------------------------------------------------------------------------
# Пути и ревьюеры
# ---------------------------------------------------------------------------


def _match_any(patterns: list[str], path: str) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, path):
                return True
        except re.error:
            continue
    return False


def is_critical(path: str, cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return _match_any(cfg["critical_paths"], path) or is_red_zone(path, cfg)


def is_red_zone(path: str, cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return _match_any(cfg["red_zone_paths"], path)


def required_reviewers(files: list[str], cfg: dict | None = None) -> list[str]:
    """Кто обязан отревьюить этот набор файлов.

    Порядок стабилен, чтобы сообщения гейтов не прыгали между запусками.
    """
    cfg = cfg or load_config()
    rv = cfg["reviewers"]
    out: list[str] = list(rv.get("always", []))
    for rule in rv.get("by_path", []):
        pat, name = rule.get("pattern"), rule.get("reviewer")
        if not pat or not name or name in out:
            continue
        if any(_match_any([pat], f) for f in files):
            out.append(name)
    red = rv.get("red_zone")
    if red and any(is_red_zone(f, cfg) for f in files) and red not in out:
        out.append(red)
    return out


def all_reviewer_names(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_config()
    rv = cfg["reviewers"]
    names = list(rv.get("always", []))
    names += [r["reviewer"] for r in rv.get("by_path", []) if r.get("reviewer")]
    if rv.get("red_zone"):
        names.append(rv["red_zone"])
    return sorted(set(names))


# ---------------------------------------------------------------------------
# Расписки
# ---------------------------------------------------------------------------


def safe(name: str) -> str:
    """Обратимое кодирование имени для использования в пути.

    Именно обратимое: схлопывание всех «плохих» символов в подчёркивание
    склеивает разные ветки в один каталог, и расписка одной ветки начинает
    отвечать за другую.
    """
    return quote(name, safe="")


def receipts_root() -> str:
    return os.path.expanduser("~/.claude/state/subagent-receipts")


def receipts_dir(repo: str, branch: str) -> str:
    return os.path.join(receipts_root(), safe(repo), safe(branch))


def detect_subagent(tool_input: dict, cfg: dict | None = None) -> str | None:
    """Определяет, что запущен именно ревьюер — строго.

    Два пути распознавания:
      1) точный тип субагента;
      2) дословная сигнатурная фраза В НАЧАЛЕ промпта.

    Слабее нельзя: если засчитывать простое упоминание имени ревьюера
    где-нибудь в тексте промпта, расписку выпишет любой вызов, в котором
    это слово попалось. Это ровно та дыра, через которую «ревью» можно
    имитировать одной строкой.
    """
    cfg = cfg or load_config()
    names = all_reviewer_names(cfg)
    st = str(tool_input.get("subagent_type") or "").strip()
    if st in names:
        return st
    prompt = str(tool_input.get("prompt") or "").lstrip()
    desc = str(tool_input.get("description") or "").strip()
    for name in names:
        sig = f"You are the `{name}` subagent"
        if prompt.startswith(sig) and desc.startswith(name):
            return name
    return None


def is_background_launch(tool_response) -> bool:
    """Фоновый запуск субагента — это НЕ состоявшееся ревью.

    Хук по завершении инструмента срабатывает сразу же, а в ответе лежит
    лишь подтверждение «агент запущен». Выписать по нему расписку —
    поручиться за ревью, которого не было: его находки придут отдельным
    уведомлением, которое этот хук уже не разбудит.
    """
    if isinstance(tool_response, dict):
        if tool_response.get("agentId") and not tool_response.get("content"):
            return True
        blob = json.dumps(tool_response, ensure_ascii=False)
    else:
        blob = str(tool_response)
    return "Async agent launched" in blob


RECEIPT_TAIL_RE = re.compile(r"^\d+-[0-9a-f]{6,64}$")


def receipt_belongs_to(fname: str, name: str) -> bool:
    """Расписка ли это ИМЕННО этого ревьюера.

    Проверка суффикса обязана быть строгой: простое «имя файла начинается с
    `<имя>-`» означает, что расписка ревьюера `change-reviewer-deep`
    удовлетворит требование к `change-reviewer`. Любая конфигурация, где одно
    имя является префиксом другого, тихо понижала бы более строгое требование
    до более слабого — а заметить это можно только по последствиям.

    Хвост — метка времени и начало отпечатка диффа: `<имя>-<секунды>-<хеш>`.
    Обе части обязательны: с необязательным хешем расписка ревьюера с именем
    `reviewer-2` («вторая полоса») засчиталась бы ревьюеру `reviewer` — тот же
    класс тихого понижения требований, ради которого проверка и написана.
    """
    if not fname.startswith(f"{name}-"):
        return False
    return bool(RECEIPT_TAIL_RE.match(fname[len(name) + 1:]))


def write_receipt(name: str, work_dir: str, diff_sha: str) -> str:
    """Кладёт расписку в файл `<имя ревьюера>-<секунды>-<начало отпечатка>`.

    Отпечаток в имени не украшение: без него две расписки одного ревьюера,
    выписанные в одну и ту же секунду, — это один и тот же путь, и вторая молча
    затирает первую. Гейт от этого не страдает (ему хватает любой подходящей),
    а вот счётчик повторных кругов ревью недосчитывается версий и молчит там,
    где обязан предупредить.
    """
    branch = current_branch(work_dir)
    d = receipts_dir(repo_name(), branch)
    os.makedirs(d, exist_ok=True)
    ts = int(time.time())
    stem = f"{name}-{ts}-{diff_sha[:12]}"
    path = os.path.join(d, stem)
    tmp = os.path.join(d, f".{stem}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(diff_sha + "\n")
    os.replace(tmp, path)  # атомарно: гейт не увидит недописанный файл
    return path


def find_receipt(name: str, diff_sha: str, cfg: dict | None = None) -> str | None:
    """Ищет свежую расписку ревьюера, привязанную ИМЕННО к этому диффу.

    Просматриваются все каталоги веток репозитория: расписка выписывается
    из рабочей копии агента, а гейт может спрашивать из другой — ветка в
    пути это удобство для чтения человеком, а не ключ поиска. Ключ —
    отпечаток диффа.
    """
    cfg = cfg or load_config()
    cutoff = time.time() - cfg["receipt_fresh_seconds"]
    root = os.path.join(receipts_root(), safe(repo_name()))
    if not os.path.isdir(root):
        return None
    stale_found = False
    for branch_dir, _dirs, files in os.walk(root):
        for fname in files:
            if not receipt_belongs_to(fname, name):
                continue
            full = os.path.join(branch_dir, fname)
            try:
                if os.path.getmtime(full) < cutoff:
                    continue
                with open(full, encoding="utf-8") as f:
                    body = f.readline().strip()
            except OSError:
                continue
            if body == diff_sha:
                return full
            stale_found = True
    return "__STALE__" if stale_found else None


def check_receipts(files: list[str], work_dir: str, cfg: dict | None = None) -> tuple[bool, list[str]]:
    """(можно ли пропускать, список проблем в человеческом виде)."""
    cfg = cfg or load_config()
    diff_sha = compute_diff_sha(work_dir)
    if diff_sha is None:
        return False, [
            "не удалось посчитать отпечаток изменений (git недоступен, нет базовой "
            "ветки или таймаут). Гейт в такой ситуации падает ЗАКРЫТО."
        ]
    if not changed_files(work_dir):
        return False, [
            "дерево пустое относительно точки ветвления — ревьюить нечего. "
            "Скорее всего сессия сидит не в той рабочей копии, либо новые файлы "
            "ещё не добавлены в индекс (git diff их не видит до `git add`)."
        ]
    problems: list[str] = []
    for name in required_reviewers(files, cfg):
        got = find_receipt(name, diff_sha, cfg)
        if got is None:
            problems.append(f"{name}: расписки нет — ревьюер не запускался")
        elif got == "__STALE__":
            problems.append(
                f"{name}: расписка есть, но на ДРУГОЙ дифф — с тех пор код менялся, "
                f"нужен повторный прогон по финальному диффу"
            )
    return (not problems), problems


# ---------------------------------------------------------------------------
# Разбор команд оболочки
# ---------------------------------------------------------------------------

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Глобальные флаги git, забирающие СЛЕДУЮЩИЙ токен как значение. Без них
# разбор `git -C /path commit` примет путь за подкоманду и пропустит вызов.
GIT_FLAGS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--exec-path", "--config-env", "--attr-source", "--super-prefix",
}


SEGMENT_SEPARATORS = ";|&\n\r"

# Открытие вложенного документа: `<<EOF`, `<<-'EOF'`, `<< "EOF"`.
# Тройной `<<<` — это строка-аргумент, а не документ, и сюда не попадает.
HEREDOC_RE = re.compile(r"(?<!<)<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?!<)")

# Перенос длинной команды на следующую строку. Склеивается ДО всего
# остального: иначе строка режется по переводу строки прямо посреди команды,
# и на первой строке остаётся глагол, а все флаги — на следующих. Тогда
# `git push \<перенос> --force` выглядит как безобидный `git push`, и это
# не теоретическая лазейка, а обычный способ записи длинной команды.
LINE_CONTINUATION_RE = re.compile(r"\\\r?\n[ \t]*")


def _heredoc_openers(line: str, in_s: bool, in_d: bool) -> tuple[list[tuple[str, bool]], bool, bool]:
    """Открытия вложенных документов В ЭТОЙ строке — только вне кавычек.

    Возвращает список (ограничитель, тело_исполняется) и состояние кавычек
    на конец строки: кавычка может открыться в одной строке и закрыться в
    другой, и без переноса состояния разбор поедет.
    """
    found: list[tuple[str, bool]] = []
    # Оболочку ищем в ЛЮБОЙ команде строки, а не только в первой: у
    # `git status && bash <<'EOF'` первое слово строки — `git`, и проверка по
    # нему объявила бы тело данными. Обёртки снимаются по той же причине
    # (`env bash <<'EOF'`).
    executable = False
    for part in _raw_segments(line):
        peeled, _env = peel_wrappers(tokenize(part))
        if peeled and os.path.basename(peeled[0]) in SHELL_COMMANDS:
            executable = True
            break
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and not in_s and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            m = HEREDOC_RE.match(line, i)
            if m:
                found.append((m.group(2), executable))
                i = m.end()
                continue
        i += 1
    return found, in_s, in_d


def strip_heredocs(cmd: str) -> str:
    """Убирает ТЕЛА вложенных документов, оставляя открывающие их команды.

    Без этого тело документа режется по переводу строки наравне с кодом, и
    каждая его строка становится «командой». Обычная запись файла

        cat > f <<'EOF'
        git commit -m x
        EOF

    превращалась в отдельный сегмент `git commit -m x` и ловилась гейтом —
    ложное срабатывание на совершенно рутинном приёме.

    Три тонкости, каждая из которых меняла вердикт на противоположный:

    * `<<` ВНУТРИ кавычек документа не открывает. Иначе самый обычный
      `gh pr create --body "$(cat <<'EOF' … EOF)"` терял всё описание, и гейт
      требовал ссылку на задачу, которая в описании есть;
    * тело, которое исполняет оболочка (`bash <<'EOF' … EOF`), — это скрипт,
      а не данные, и проверять его надо наравне с остальным;
    * незакрытый документ ничего не выбрасывает: одна опечатка в ограничителе
      иначе делает невидимым весь остаток команды.
    """
    if "<<" not in cmd:
        return cmd
    lines = cmd.split("\n")
    kept: list[str] = []
    idx = 0
    in_s = in_d = False
    while idx < len(lines):
        line = lines[idx]
        kept.append(line)
        openers, in_s, in_d = _heredoc_openers(line, in_s, in_d)
        idx += 1
        for delim, executable in openers:
            end = next((j for j in range(idx, len(lines)) if lines[j].strip() == delim), None)
            if end is None:
                continue
            if executable:
                kept.extend(lines[idx:end])
            idx = end + 1
    return "\n".join(kept)


def split_segments(cmd: str) -> list[str]:
    """Режет строку по операторам ; && || | & И ПО ПЕРЕВОДУ СТРОКИ, вне кавычек.

    Нужно, потому что `echo ok && git commit …` — это два вызова, и
    проверять надо каждый. Наивный поиск подстроки здесь ошибается в обе
    стороны.

    Перевод строки в списке разделителей — не мелочь. Агенты сплошь и рядом
    посылают многострочные команды, и без него весь скрипт выглядит как один
    сегмент, чьё первое слово — что-нибудь безобидное вроде `echo`. Тогда
    любая проверка, разбирающая первое слово, слепа ко всему остальному:

        git status\\ngit commit -m x     -> проверка видела только `git status`

    Это была не экзотическая лазейка, а поведение по умолчанию.
    """
    return _raw_segments(strip_heredocs(LINE_CONTINUATION_RE.sub(" ", cmd)))


def _raw_segments(cmd: str) -> list[str]:
    """Разрез по операторам без предварительной обработки строки.

    Экранирование обратной косой чертой учитывается: без него один
    `echo don\\'t; git rebase main` оставляет разбор с «открытой» кавычкой до
    конца строки, и весь остаток команды становится невидимым для всех
    проверок сразу.
    """
    out: list[str] = []
    cur: list[str] = []
    in_s = in_d = False
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\" and not in_s and i + 1 < n:
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 2
            continue
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        if not in_s and not in_d and ch in SEGMENT_SEPARATORS:
            out.append("".join(cur))
            cur = []
            while i < n and cmd[i] in SEGMENT_SEPARATORS:
                i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def tokenize(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


# Слова-обёртки: стоят перед настоящей командой и не меняют её сути.
# Без их снятия `env git commit` и `command git rebase` выглядят как вызовы
# `env` и `command` — то есть как что-то, чего проверка не знает, и потому
# пропускает. Это снимало защиту целиком, включая ту, что специально ловит
# подмену рабочего дерева (`env GIT_DIR=… git commit`).
WRAPPER_COMMANDS = {
    "env", "command", "builtin", "exec", "nice", "ionice", "nohup",
    "time", "stdbuf", "sudo", "doas", "xargs", "timeout", "gtimeout",
}

# Флаги обёрток, забирающие СЛЕДУЮЩЕЕ слово как своё значение. Без этой
# таблицы снятие обёртки съедает не флаг, а саму команду: у `sudo -u user git
# commit` первым «настоящим» словом окажется `user`, разбор вернёт «это не
# git» — и гейт пропустит коммит. Дыра ровно того же семейства, ради которого
# обёртки вообще начали сниматься, просто в форме с флагом.
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-n", "-c", "-p", "-P", "-u"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--replace",
              "--max-lines", "--max-args", "--max-procs", "--delimiter"},
    "nohup": set(),
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "time": {"-f", "-o", "--format", "--output"},
}

# Оболочки, принимающие команду строкой: `bash -c "git commit …"`.
SHELL_COMMANDS = {"bash", "sh", "zsh", "dash", "ksh"}

# Флаг «команда строкой» у оболочки, включая слитные формы `-lc`, `-ec`.
SHELL_C_RE = re.compile(r"^-[a-zA-Z]*c$")

# Подстановка команд: $(…) и `…`.
SUBSHELL_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

MAX_EXPAND_DEPTH = 3

# Длительность-аргумент обёртки: `30`, `0.5`, `10s`, `5m`, `2h`.
DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")


# Служебные слова оболочки, после которых идёт обычная команда. Без них
# первым словом сегмента оказывается `do` / `then` / `(git`, разбор отвечает
# «это не git», и мимо проходит ВСЯ защита — включая запрет переписывания
# истории. Записи вида `(git commit …)`, `{ git commit …; }`,
# `for … do git commit … done`, `if …; then git commit …; fi` — не экзотика.
# `time` сюда НЕ входит: он уже разбирается как обёртка вместе со своими
# флагами (WRAPPER_COMMANDS), а простое выбрасывание слова оставляло бы
# головой сегмента флаг `-p`, и `time -p git push --force` проходил бы мимо
# всех запретов разом.
SHELL_KEYWORDS = {"if", "then", "elif", "else", "fi", "do", "done", "while",
                  "until", "for", "case", "esac", "select", "function", "!"}
GROUPING_CHARS = "({!"


def strip_shell_syntax(tokens: list[str]) -> list[str]:
    """Снимает группировку и служебные слова, оставляя саму команду.

    Скобка часто слипается с командой (`(git`), поэтому чистится и префикс
    токена, а не только отдельно стоящие символы. Хвостовая `)` снимается
    ТОЛЬКО если открывающая была снята здесь же: иначе пострадала бы обычная
    скобка внутри аргумента.
    """
    out = list(tokens)
    opened = 0
    while out:
        head = out[0]
        if head in SHELL_KEYWORDS:
            out = out[1:]
            continue
        trimmed = head.lstrip(GROUPING_CHARS)
        if trimmed == head:
            break
        opened += len(head) - len(trimmed)
        out = ([trimmed] if trimmed else []) + out[1:]
    while opened > 0 and out:
        tail = out[-1]
        trimmed = tail.rstrip(");}")
        if trimmed == tail:
            break
        opened -= len(tail) - len(trimmed)
        out = out[:-1] + ([trimmed] if trimmed else [])
    return out


def peel_wrappers(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Снимает слова-обёртки и их флаги, оставляя настоящую команду.

    `env -i FOO=1 git commit` -> `git commit`
    `sudo -u user nice -n 5 git rebase` -> `git rebase`

    Возвращает ещё и присваивания переменных, встреченные на любом уровне:
    `env GIT_DIR=/tmp git commit` прячет присваивание ПОСЛЕ обёртки, и
    потерять его нельзя — именно оно переадресует git в другое дерево.

    Флаг, забирающий следующее слово, снимается ВМЕСТЕ со значением — иначе
    значение и станет «командой» (`sudo -u user git commit` → `user`), разбор
    ответит «это не git», и гейт пропустит коммит. Список таких флагов задан
    явно, по обёрткам (WRAPPER_VALUE_FLAGS).

    Незнакомый флаг значение НЕ забирает — намеренно: у `xargs -0 git commit`
    предположение обратного съело бы саму команду. Плата за выбор — обёртка с
    незнакомым флагом-значением останется не разобранной; при добавлении
    обёртки в WRAPPER_COMMANDS заполняйте и её таблицу флагов.
    """
    out = list(tokens)
    collected: dict[str, str] = {}
    # Верхняя граница — длина списка, а не MAX_EXPAND_DEPTH: каждый проход
    # либо возвращает результат, либо укорачивает список хотя бы на слово,
    # так что зациклиться нельзя. Ограничение тремя проходами означало, что
    # `timeout 600 nice -n 10 env FOO=1 git push --force` остаётся
    # неразобранным — то есть проходит мимо ВСЕХ запретов сразу.
    for _ in range(len(tokens) + 1):
        out = strip_shell_syntax(out)
        out, env = strip_env_prefix(out)
        collected.update(env)
        if not out:
            return out, collected
        name = os.path.basename(out[0])
        if name not in WRAPPER_COMMANDS:
            return out, collected
        value_flags = WRAPPER_VALUE_FLAGS.get(name, set())
        rest = out[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("--") and "=" in tok:
                i += 1
                continue
            if tok.startswith("-") and tok != "-":
                # `-u user`, `-s KILL`, а также слитные формы вроде `-n5`.
                if tok in value_flags and i + 1 < len(rest):
                    i += 2
                else:
                    i += 1
                continue
            # Длительность у `timeout`: `30`, `0.5`, `10s`, `5m`. Только
            # `isdigit()` — и `timeout 5m git push --force` остаётся неразобранным,
            # то есть просто проходит мимо запрета.
            if DURATION_RE.match(tok):
                i += 1
                continue
            break
        out = rest[i:]
    return out, collected


def expand_segments(cmd: str, depth: int = 0) -> list[str]:
    """Все команды внутри строки, включая спрятанные в оболочке и подстановке.

    Разворачивает:
      * операторы и переводы строк                 (split_segments)
      * `bash -c "…"` / `sh -c "…"` / `eval "…"`   (команда как строка)
      * `$(…)` и обратные кавычки                  (подстановка команд)

    Смысл: проверка должна видеть команду, которая РЕАЛЬНО выполнится, а не
    ту, что стоит первым словом. Иначе любой из этих способов записи — не
    экзотика, а обычный приём — снимает защиту.
    """
    segments = split_segments(cmd)
    if depth >= MAX_EXPAND_DEPTH:
        return segments

    out: list[str] = []
    for seg in segments:
        out.append(seg)
        tokens = tokenize(seg)
        peeled, _env = peel_wrappers(tokens)
        if not peeled:
            continue
        exe = os.path.basename(peeled[0])

        if exe in SHELL_COMMANDS:
            # `-c`, но и слитные формы вроде `-lc`, `-ec`: команда-строкой
            # пишется и так, и так, а разбор только точного `-c` означает,
            # что вторая форма проходит мимо всех проверок.
            for idx, tok in enumerate(peeled[1:], start=1):
                if SHELL_C_RE.match(tok) and idx + 1 < len(peeled):
                    out.extend(expand_segments(peeled[idx + 1], depth + 1))
                    break
        elif exe == "eval":
            out.extend(expand_segments(" ".join(peeled[1:]), depth + 1))

        for m in SUBSHELL_RE.finditer(seg):
            inner = m.group(1) or m.group(2) or ""
            if inner.strip():
                out.extend(expand_segments(inner, depth + 1))
    return out


def segments_with_dirs(command: str, base_dir: str) -> list[tuple[str, str]]:
    """Каждый сегмент вместе с каталогом, в котором он РЕАЛЬНО выполнится.

    `cd` внутри команды меняет дерево для всего, что идёт после него. Гейт,
    который берёт каталог только из полезной нагрузки хука, этого не видит —
    и `cd ../соседняя-копия && git commit` он проверяет по дереву сессии,
    то есть выносит вердикт про совсем другой дифф. А `cd <копия> && git
    commit` — обычный способ добраться до соседней рабочей копии, не экзотика.

    Разворачивание вложенного тоже каталого-осведомлённое: `bash -c "cd
    <копия> && git commit"` — это тот же переход, просто записанный через
    строку-команду, и считать его выполненным в каталоге сессии значит
    оставить дыру ровно там, где её закрывали снаружи.
    """
    return _walk_with_dirs(command, base_dir, 0)


def _walk_with_dirs(command: str, base_dir: str, depth: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    current = base_dir
    for top in split_segments(command):
        peeled, _env = peel_wrappers(tokenize(top))
        if peeled and os.path.basename(peeled[0]) == "cd":
            args = [a for a in peeled[1:] if not a.startswith("-")]
            if args:
                target = args[0] if os.path.isabs(args[0]) else os.path.join(current, args[0])
                if os.path.isdir(target):
                    current = os.path.realpath(target)
            continue
        out.append((top, current))
        if depth >= MAX_EXPAND_DEPTH or not peeled:
            continue
        exe = os.path.basename(peeled[0])
        if exe in SHELL_COMMANDS:
            for idx, tok in enumerate(peeled[1:], start=1):
                if SHELL_C_RE.match(tok) and idx + 1 < len(peeled):
                    out.extend(_walk_with_dirs(peeled[idx + 1], current, depth + 1))
                    break
        elif exe == "eval":
            out.extend(_walk_with_dirs(" ".join(peeled[1:]), current, depth + 1))
        for m in SUBSHELL_RE.finditer(top):
            inner = m.group(1) or m.group(2) or ""
            if inner.strip():
                out.extend(_walk_with_dirs(inner, current, depth + 1))
    return out


def invocations(command: str, patterns: list[str], base_dir: str = "") -> list[tuple[str, str]]:
    """Сегменты, в которых команда РЕАЛЬНО вызывается, а не просто упомянута.

    Совпадение ищется от начала сегмента (после снятия присваиваний и слов-
    обёрток), а не где угодно в строке. Иначе проверка ошибается в обе
    стороны сразу: `gh  pr merge` с двумя пробелами проходит мимо, а
    безобидное `echo "gh pr merge"` — блокируется. Первое опаснее, второе
    быстрее приводит к тому, что защиту выключают.
    """
    hits: list[tuple[str, str]] = []
    for seg, seg_dir in segments_with_dirs(command, base_dir):
        peeled, _env = peel_wrappers(tokenize(seg))
        if not peeled:
            continue
        head = " ".join([os.path.basename(peeled[0]), *peeled[1:]])
        if any(re.match(p, head) for p in patterns):
            hits.append((seg, seg_dir))
    return hits


def strip_env_prefix(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Отделяет присваивания переменных перед командой.

    `FOO=1 git rebase` — это git rebase. Регистр имени переменной любой:
    оболочка разрешает и строчные, и этим уже пытались обходить запрет.
    """
    env: dict[str, str] = {}
    i = 0
    while i < len(tokens) and ENV_ASSIGN_RE.match(tokens[i]):
        k, _, v = tokens[i].partition("=")
        env[k] = v
        i += 1
    return tokens[i:], env


def parse_git(tokens: list[str], cwd: str = "") -> dict | None:
    """Разбирает вызов git: подкоманда, её аргументы, глобальные флаги.

    Имя бинарника берётся как basename — иначе `/usr/bin/git rebase`
    проходит мимо запрета, который ищет ровно строку "git".
    Слова-обёртки снимаются (`env git …`, `sudo git …`), а присваивания
    переменных собираются из ОБЕИХ позиций: перед обёрткой и после неё
    (`env GIT_DIR=/tmp git commit` — присваивание стоит после `env`).

    Псевдоним раскрывается в настоящую подкоманду: `git ci -m x` при
    `alias.ci = commit` — это коммит, и гейт, знающий только слово `commit`,
    пропустил бы его. Псевдоним задаётся и прямо в команде (`git -c
    alias.x=commit x`), поэтому смотрится и то, и другое.
    """
    head, env = strip_env_prefix(tokens)
    head, env2 = peel_wrappers(head)
    env = {**env, **env2}
    if not head or os.path.basename(head[0]) != "git":
        return None
    i, globals_seen = 1, []
    while i < len(head):
        tok = head[i]
        if not tok.startswith("-"):
            sub, extra = resolve_alias(tok, globals_seen, cwd)
            return {
                "subcommand": sub,
                "args": extra + head[i + 1:],
                "globals": globals_seen,
                "env": env,
            }
        globals_seen.append(tok)
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in GIT_FLAGS_WITH_VALUE and i + 1 < len(head):
            globals_seen.append(head[i + 1])
            i += 2
            continue
        i += 1
    return {"subcommand": None, "args": [], "globals": globals_seen, "env": env}


# Псевдонимы, заданные прямо в команде: `git -c alias.co=checkout co main`.
INLINE_ALIAS_RE = re.compile(r"^alias\.([^=]+)=(.*)$")


# Подкоманда, за которой стоит команда оболочки. Имя намеренно не может
# совпасть с настоящей подкомандой git — гейты сверяются именно с ним.
SHELL_ALIAS = "!shell-alias"

# Глубина разворачивания цепочки псевдонимов. Своё число, а не общий предел
# обёрток: цена ступени здесь — вызов `git config`, и он дороже.
MAX_ALIAS_DEPTH = 10

# Подстановки и позиционные параметры в теле псевдонима: что именно
# выполнится, зависит от аргументов вызова, и разобрать это заранее нельзя.
# Перевод строки здесь так же важен, как `;`: тело из двух строк — это две
# команды, и разобрать его как одну значит увидеть только первую.
UNPARSEABLE_ALIAS_RE = re.compile(r"[$`;|&\n\r<>()#]")


def mask_quoted(text: str) -> str:
    """Содержимое кавычек заменено на «x» — длина и сами кавычки сохранены.

    Нужно там, где ищут значащие знаки оболочки: `(`, `>` и `#` внутри
    строки формата (`--pretty=format:"%h (%an)"`) — обычный текст, и
    отказывать по ним значит ломать безобидные команды.

    Двойные кавычки НЕ обезвреживают подстановку: `"$(…)"` и обратные
    кавычки внутри них выполняются, поэтому там они остаются видимыми. И
    незакрытая кавычка не маскирует остаток строки — иначе ею можно было бы
    спрятать что угодно; такая строка возвращается как есть.
    """
    out: list[str] = []
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            out.append("x")
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            out.append(ch)
            escaped = True
            continue
        if quote:
            if ch == quote:
                out.append(ch)
                quote = ""
            elif quote == '"' and ch in "$`":
                out.append(ch)
            else:
                out.append("x")
            continue
        if ch in "\"'":
            quote = ch
        out.append(ch)
    return text if quote else "".join(out)


def _skip_git_globals(tokens: list[str]) -> tuple[str | None, list[str], list[str]]:
    """Подкоманда после глобальных флагов git, сами флаги и остаток за ней.

    Флаги возвращаются, а не выбрасываются: `-C`, `--git-dir` и `-c
    alias.x=…` из тела псевдонима определяют, В КАКОМ дереве выполнится
    команда и что за ней стоит. Потерять их значит проверить не то дерево.
    """
    i = 0
    skipped: list[str] = []
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return tok, skipped, tokens[i + 1:]
        skipped.append(tok)
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in GIT_FLAGS_WITH_VALUE and i + 1 < len(tokens):
            skipped.append(tokens[i + 1])
            i += 2
            continue
        i += 1
    return None, skipped, []


def resolve_alias(sub: str, globals_seen: list[str], cwd: str) -> tuple[str, list[str]]:
    """Настоящая подкоманда за псевдонимом и его собственные аргументы.

    Возвращает исходное имя, если это не псевдоним, — тогда ничего не меняется.

    Псевдоним-команда оболочки (`!sh -c …`) не раскрывается: за ним может быть
    что угодно, и притворяться, что мы это разобрали, хуже, чем не разбирать.
    Но «не разобрали» не значит «пропустили»: возвращается маркер
    SHELL_ALIAS, и гейт отказывает — иначе `git -c alias.x='!git push
    --force' x` снимал бы разом все запреты одной строкой.
    """
    extra: list[str] = []
    seen: set[str] = set()
    # Псевдоним может ссылаться на псевдоним — git разворачивает цепочку до
    # конца, и одна лишняя ступень (`qq` → `pp` → `push`) вернула бы все
    # запреты обратно. `seen` защищает от кольца, которое git ловит сам.
    for _ in range(MAX_ALIAS_DEPTH):
        if sub in KNOWN_GIT_SUBCOMMANDS or sub in seen:
            return sub, extra
        seen.add(sub)
        expansion = ""
        for tok in globals_seen:
            m = INLINE_ALIAS_RE.match(tok)
            if m and m.group(1) == sub:
                expansion = m.group(2)
        if not expansion and cwd:
            code, out = git(["config", "--get", f"alias.{sub}"], cwd)
            if code == 0:
                expansion = out.strip()
        if not expansion:
            return sub, extra
        if expansion.startswith("!"):
            # Тело — команда оболочки. Если это простой вызов git без
            # подстановок, его можно разобрать как обычную команду: иначе
            # безобидные сокращения вроде `!git log --oneline` блокировали бы
            # работу, а такие отказы первым делом выключают. Всё остальное
            # честно помечается непроверяемым.
            body = expansion[1:].strip()
            parts = tokenize(body)
            if (parts and os.path.basename(parts[0]) == "git"
                    and not UNPARSEABLE_ALIAS_RE.search(mask_quoted(body))):
                # Подкоманду ищем так же, как в обычном вызове: перед ней могут
                # стоять глобальные флаги, и `!git -C /tmp push --force`
                # позиционным `parts[1]` читался бы как подкоманда `-C`.
                nxt, skipped, rest = _skip_git_globals(parts[1:])
                if nxt is None:
                    return SHELL_ALIAS, []
                # Флаги тела попадают в тот же список, по которому гейты ищут
                # `-C` и `-c alias.x=…`: иначе псевдоним прячет и дерево, и
                # объявленный внутри себя псевдоним. В КОНЕЦ, а не в начало:
                # у git побеждает последний флаг, и тело — последнее слово.
                globals_seen.extend(skipped)
                sub, extra = nxt, rest + extra
                continue
            return SHELL_ALIAS, []
        parts = tokenize(expansion)
        if not parts:
            return sub, extra
        sub, extra = parts[0], parts[1:] + extra
    return sub, extra


# Подкоманды, которые точно не псевдонимы: для них вызывать git незачем.
# Список неполный намеренно — незнакомое слово просто проверяется через
# `git config`, и цена этого — один дешёвый локальный вызов.
KNOWN_GIT_SUBCOMMANDS = {
    "add", "am", "apply", "bisect", "blame", "branch", "checkout", "cherry-pick",
    "clean", "clone", "commit", "config", "diff", "fetch", "grep", "init", "log",
    "merge", "mv", "pull", "push", "rebase", "reset", "restore", "revert", "rm",
    "show", "stash", "stage", "status", "submodule", "switch", "tag", "worktree",
    "rev-parse", "ls-files", "describe", "remote", "reflog", "shortlog",
    "sparse-checkout", "cat-file", "for-each-ref", "symbolic-ref", "update-ref",
}


def git_dash_c_dir(parsed: dict, base_dir: str = "") -> str | None:
    """Каталог из `git -C <path>` — гейт должен смотреть именно туда.

    Относительный путь считается от каталога СЕГМЕНТА (куда успел перейти
    `cd`), а не от каталога процесса хука. Процесс хука запускается из
    корня проекта, а сессия часто сидит в отдельной рабочей копии, поэтому
    `git -C . commit` при наивном разборе указывал бы на чужое дерево —
    как правило чистое, то есть гейт молча пропускал бы коммит.

    Несколько `-C` подряд git применяет накопительно, каждый следующий —
    относительно предыдущего; повторяем это же правило.
    """
    g = parsed.get("globals", [])
    current = base_dir or ""
    found = False
    for idx, tok in enumerate(g):
        if tok == "-C" and idx + 1 < len(g):
            path = g[idx + 1]
            current = path if os.path.isabs(path) else os.path.join(current, path)
            found = True
    if not found:
        return None
    return os.path.normpath(current) if current else None


# ---------------------------------------------------------------------------
# Ввод/вывод хука
# ---------------------------------------------------------------------------


def read_hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def block(message: str) -> int:
    """Запретить действие и объяснить агенту, как починить самому."""
    sys.stderr.write(message.rstrip() + "\n")
    return 2


def advise(message: str) -> int:
    """Ненавязчиво предупредить человека, действие не трогать."""
    try:
        print(json.dumps({"systemMessage": message}, ensure_ascii=False))
    except Exception:
        pass
    return 0
