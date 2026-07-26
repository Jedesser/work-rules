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
EMPTY_DIFF_SHA = hashlib.sha256(b"").hexdigest()

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
    "require_timeout_for": [r"\bgo test\b", r"\bpytest\b", r"\bnpm (run )?test\b"],
    # Команды, запрещённые на машине разработки (их место — в CI).
    "forbidden_local_commands": [],
    # Переменная-эвакуационный выход для предыдущего пункта.
    "forbidden_local_escape_env": "HEAVY_BUILD_OK",

    # Как выглядит создание PR и его мерж в вашем CLI.
    "pr_create_patterns": [r"\bgh pr create\b", r"\bglab mr create\b"],
    "pr_merge_patterns": [r"\bgh pr merge\b", r"\bglab mr merge\b"],
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

    "git_timeout": 10,
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
    except Exception:
        pass
    _CONFIG_CACHE = cfg
    return cfg


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def git(args: list[str], cwd: str, timeout: int | None = None) -> tuple[int, str]:
    """Запускает git и возвращает (код возврата, stdout).

    Никогда не бросает исключение: любая беда превращается в (1, "") —
    решение о том, «падать открыто» или «падать закрыто», принимает
    вызывающий, а не эта функция.
    """
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
    """Точка ветвления текущей ветки от базовой. None — если не вышло."""
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
    code, out = git(["diff", base], work_dir, timeout=30)
    if code != 0:
        return None
    return hashlib.sha256(out.encode("utf-8", "replace")).hexdigest()


def changed_files(work_dir: str, base_ref: str | None = None) -> list[str]:
    """Файлы, отличающие дерево от точки ветвления (включая нестейдженные)."""
    base = merge_base(work_dir, base_ref)
    if base is None:
        return []
    code, out = git(["diff", "--name-only", base], work_dir, timeout=30)
    if code != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def staged_files(work_dir: str) -> list[str]:
    code, out = git(["diff", "--cached", "--name-only"], work_dir)
    if code != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def added_lines(work_dir: str, base_ref: str | None = None) -> int:
    base = merge_base(work_dir, base_ref)
    if base is None:
        return 0
    code, out = git(["diff", "--numstat", base], work_dir, timeout=30)
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


def write_receipt(name: str, work_dir: str, diff_sha: str) -> str:
    branch = current_branch(work_dir)
    d = receipts_dir(repo_name(), branch)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}-{int(time.time())}")
    tmp = os.path.join(d, f".{name}-{int(time.time())}.tmp")
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
            if not fname.startswith(f"{name}-"):
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
    if diff_sha == EMPTY_DIFF_SHA:
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


def split_segments(cmd: str) -> list[str]:
    """Режет строку по операторам ; && || | & вне кавычек.

    Нужно, потому что `echo ok && git commit …` — это два вызова, и
    проверять надо каждый. Наивный поиск подстроки здесь ошибается в обе
    стороны.
    """
    out: list[str] = []
    cur: list[str] = []
    in_s = in_d = False
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        if not in_s and not in_d and ch in ";|&":
            out.append("".join(cur))
            cur = []
            while i < n and cmd[i] in ";|&":
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


def parse_git(tokens: list[str]) -> dict | None:
    """Разбирает вызов git: подкоманда, её аргументы, глобальные флаги.

    Имя бинарника берётся как basename — иначе `/usr/bin/git rebase`
    проходит мимо запрета, который ищет ровно строку "git".
    """
    head, env = strip_env_prefix(tokens)
    if not head or os.path.basename(head[0]) != "git":
        return None
    i, globals_seen = 1, []
    while i < len(head):
        tok = head[i]
        if not tok.startswith("-"):
            return {
                "subcommand": tok,
                "args": head[i + 1:],
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


def git_dash_c_dir(parsed: dict) -> str | None:
    """Каталог из `git -C <path>` — гейт должен смотреть именно туда."""
    g = parsed.get("globals", [])
    for idx, tok in enumerate(g):
        if tok == "-C" and idx + 1 < len(g):
            return g[idx + 1]
    return None


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
