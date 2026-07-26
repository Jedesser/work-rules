#!/usr/bin/env python3
"""Тест самих защит. Запускать в CI на КАЖДОМ изменении, без фильтра по путям.

Зачем отдельный тест на перехватчики: их поломка молчалива в обе стороны.
Если защита перестала срабатывать — всё выглядит зелёным, а гейта нет.
Если начала срабатывать ложно — её выключат в первый же день. Ни то, ни
другое не заметно по обычным тестам продукта.

Именно так у нас однажды и вышло: расписки стали привязываться к пустому
диффу, гейты продолжали «работать» и пропускали всё подряд. Обнаружилось
случайно и много позже.

Что проверяется (каждый пункт — на реальном временном репозитории):
  1. писатель расписок и гейты согласны, КАКОЕ дерево они описывают;
  2. расписка на текущий дифф принимается, а на устаревший — нет;
  3. фоновый запуск ревьюера расписку НЕ выписывает;
  4. предполётная проверка блокирует пустое дерево и пропускает непустое;
  5. запрет разрушительных команд ловит известные обходы и не ругается
     на безобидное (обе стороны обязательны);
  6. в скриптах нет захардкоженных проектных путей — только конфиг.

Чистая стандартная библиотека, никакой сети. Около 3–5 секунд.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "gate-test",
    "GIT_AUTHOR_EMAIL": "gate-test@example.invalid",
    "GIT_COMMITTER_NAME": "gate-test",
    "GIT_COMMITTER_EMAIL": "gate-test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",  # нейтрализуем настройки разработчика
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

# Домашний каталог теста. Задаётся в main() и ОБЯЗАТЕЛЬНО передаётся в каждый
# запуск хука: расписки живут в ~/.claude/state, и без подмены HOME тест
# писал бы их в настоящий каталог разработчика — сам себе давая ложное «ok»
# от чужих, оставшихся с прошлого раза расписок.
TEST_HOME = ""

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def git(args: list[str], cwd: Path) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV,
                       capture_output=True, text=True, timeout=30)
    return p.stdout.strip()


def run_hook(script: str, payload: dict, cwd: Path, env_extra: dict | None = None) -> tuple[int, str, str]:
    env = {**GIT_ENV, "HOME": TEST_HOME, "CLAUDE_PROJECT_DIR": str(cwd), **(env_extra or {})}
    p = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True, text=True, cwd=cwd, env=env, timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


def make_repo(tmp: Path) -> Path:
    """Репозиторий с «удалённой» основной веткой и рабочей веткой поверх."""
    repo = tmp / "repo"
    repo.mkdir()
    git(["init", "-q", "-b", "main"], repo)
    (repo / "README.md").write_text("hello\n")
    git(["add", "README.md"], repo)
    git(["commit", "-qm", "init"], repo)
    # origin/main как локальная ссылка — точка ветвления должна существовать
    git(["update-ref", "refs/remotes/origin/main", "HEAD"], repo)
    git(["checkout", "-qb", "feature"], repo)
    return repo


def write_config(repo: Path, extra: dict | None = None) -> None:
    cfg = {
        "base_ref": "origin/main",
        "main_branch": "main",
        "commit_gate": {"max_files": 2},
        "critical_paths": ["^critical/"],
        "red_zone_paths": ["^money/"],
        "reviewers": {"always": ["change-reviewer"], "by_path": [], "red_zone": "critical-zone-reviewer"},
        "require_timeout_for": [],
        "forbidden_local_commands": [],
    }
    cfg.update(extra or {})
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "gates.config.json").write_text(json.dumps(cfg), encoding="utf-8")


def reviewer_payload(cwd: Path, background: bool = False) -> dict:
    return {
        "tool_name": "Agent",
        "cwd": str(cwd),
        "tool_input": {
            "subagent_type": "change-reviewer",
            "description": "change-reviewer run",
            "prompt": "You are the `change-reviewer` subagent",
            "run_in_background": background,
        },
        "tool_response": (
            {"agentId": "abc"} if background else {"is_error": False, "content": "no findings"}
        ),
    }


def main() -> int:
    global TEST_HOME
    TEST_HOME = tempfile.mkdtemp(prefix="gate-home-")
    home = TEST_HOME

    with tempfile.TemporaryDirectory(prefix="gate-test-") as tmpdir:
        tmp = Path(tmpdir)
        repo = make_repo(tmp)
        write_config(repo)

        print("\n1. Дерево и отпечаток: писатель и гейт согласны")
        (repo / "critical").mkdir()
        (repo / "critical" / "a.txt").write_text("v1\n")
        git(["add", "critical/a.txt"], repo)

        code, _out, _err = run_hook("subagent_receipt.py", reviewer_payload(repo), repo)
        check("расписка выписывается на непустой дифф", code == 0)
        receipts = list(Path(home).glob(".claude/state/subagent-receipts/**/change-reviewer-*"))
        check("файл расписки создан", len(receipts) == 1, f"найдено {len(receipts)}")

        print("\n2. Гейт коммита принимает расписку только на текущий дифф")
        commit_payload = {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "REVIEW_DONE=1 git commit -m x"},
        }
        code, _o, err = run_hook("commit_gate.py", commit_payload, repo)
        check("свежая расписка на текущий дифф принимается", code == 0, err[:200])

        no_bypass = {"tool_name": "Bash", "cwd": str(repo),
                     "tool_input": {"command": "git commit -m x"}}
        code, _o, err = run_hook("commit_gate.py", no_bypass, repo)
        check("без обхода критический путь блокируется", code == 2)
        check("в тексте отказа названы требуемые ревьюеры", "change-reviewer" in err)

        # меняем содержимое — расписка обязана перестать подходить
        (repo / "critical" / "a.txt").write_text("v2\n")
        git(["add", "critical/a.txt"], repo)
        code, _o, err = run_hook("commit_gate.py", commit_payload, repo)
        check("расписка на устаревший дифф отвергается", code == 2)
        check("отказ объясняет, что дифф изменился", "ДРУГОЙ дифф" in err, err[:200])

        print("\n3. Фоновый запуск ревьюера расписку не выписывает")
        before = len(list(Path(home).glob(".claude/state/subagent-receipts/**/change-reviewer-*")))
        run_hook("subagent_receipt.py", reviewer_payload(repo, background=True), repo)
        after = len(list(Path(home).glob(".claude/state/subagent-receipts/**/change-reviewer-*")))
        check("фоновый запуск не создал расписку", before == after, f"{before} -> {after}")

        print("\n4. Красная зона требует человека даже при валидных расписках")
        (repo / "money").mkdir()
        (repo / "money" / "billing.txt").write_text("x\n")
        git(["add", "money/billing.txt"], repo)
        run_hook("subagent_receipt.py", reviewer_payload(repo), repo)
        red_payload = {"tool_name": "Bash", "cwd": str(repo),
                       "tool_input": {"command": "MERGE_REVIEW_DONE=1 gh pr merge 1 --merge"}}
        code, _o, err = run_hook("merge_gate.py", red_payload, repo)
        check("мерж красной зоны не проходит автоматически", code == 2, err[:200])

        print("\n5. Предполётная проверка ревьюера")
        clean = tmp / "clean"
        clean.mkdir()
        git(["init", "-q", "-b", "main"], clean)
        (clean / "f.txt").write_text("x\n")
        git(["add", "f.txt"], clean)
        git(["commit", "-qm", "init"], clean)
        git(["update-ref", "refs/remotes/origin/main", "HEAD"], clean)
        write_config(clean)
        code, _o, err = run_hook("preflight_review_guard.py", reviewer_payload(clean), clean)
        check("пустое дерево — запуск ревьюера отклонён", code == 2)
        code, _o, _e = run_hook("preflight_review_guard.py", reviewer_payload(repo), repo)
        check("непустое дерево — запуск разрешён", code == 0)

        print("\n6. Запрет разрушительных команд: обе стороны")
        blocked = [
            "git rebase main",
            "FOO=1 git rebase main",
            "/usr/bin/git rebase main",
            "git -C /tmp/x rebase main",
            "git commit --amend -m x",
            "git push --force origin feature",
            "git push origin main",
            "git add -A",
            "git add .",
            "git reset --hard HEAD~1",
            "rm -rf /tmp/x",
            "rm -r -f /tmp/x",
            "find . -name '*.tmp' -exec rm -rf {} +",
            "sed -i 's/a/b/' file.txt",
            "echo ok && git rebase main",
        ]
        allowed = [
            "git merge origin/main",
            "git add path/to/file.py",
            "git commit -m 'fix: something'",
            "git push origin feature",
            "rm /tmp/single-file",
            "rm -r /tmp/dir",
            "echo 'никогда не делай git rebase'",
            "grep -r 'sed' .",
        ]
        for cmd in blocked:
            code, _o, _e = run_hook("guard_bash.py", {
                "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}, repo)
            check(f"блокируется: {cmd}", code == 2)
        for cmd in allowed:
            code, _o, err = run_hook("guard_bash.py", {
                "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}, repo)
            check(f"пропускается: {cmd}", code == 0, err[:120])

        print("\n7. Ресурсные ограничители")
        write_config(repo, {"require_timeout_for": [r"\bpytest\b"],
                            "forbidden_local_commands": [r"\bnpm run build\b"]})
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "pytest tests/"}}, repo)
        check("тесты без потолка по времени блокируются", code == 2)
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "timeout 120 pytest tests/unit"}}, repo)
        check("тесты с потолком пропускаются", code == 0)
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "npm run build"}}, repo)
        check("тяжёлая сборка локально блокируется", code == 2)
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "npm run build"}}, repo, {"HEAVY_BUILD_OK": "1"})
        check("эвакуационный выход работает", code == 0)

    print("\n8. В скриптах нет проектных путей — только конфиг")
    hardcoded = re.compile(r"[\"'](/(home|root|Users)/[^\"']+)[\"']")
    for script in sorted(HOOKS.glob("*.py")) + sorted((HOOKS / "lib").glob("*.py")):
        text = script.read_text(encoding="utf-8")
        # ~/.claude/state — общий каталог рантайма, а не проектный путь
        bad = [m for m in hardcoded.findall(text) if not m.startswith("/dev/")]
        check(f"без захардкоженных путей: {script.name}", not bad, str(bad[:2]))

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
