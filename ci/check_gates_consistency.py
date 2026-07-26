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
  4. гейт не обходится другой записью той же команды (перенос строки,
     слова-обёртки, `-a`, вызов оболочки строкой);
  5. расписку нельзя потратить на файл, которого ревьюер не видел;
  6. переадресация git в чужое дерево отклоняется, а `git -C` — нет;
  7. красная зона не проходит автоматически даже с полным набором расписок;
  8. предполётная проверка блокирует пустое дерево и пропускает непустое;
  9. предупреждение о переделке срабатывает и молчит на фоновом запуске;
 10. запрет разрушительных команд ловит известные обходы и не ругается
     на безобидное (обе стороны обязательны);
 11. ресурсные ограничители и их эвакуационный выход;
 12. гейт плана: нет плана — отказ, статус не выбран — отказ, выбран — проход;
 13. гейт «сначала задача»: без ссылки — отказ, со ссылкой — проход;
 14. защита общей копии, включая обход через символическую ссылку;
 15. в скриптах нет захардкоженных проектных путей — только конфиг.

Каждый пункт с 4 по 6 — это дыра, которая реально работала и была найдена
на ревью этого же набора. Тест на защиту без теста на КОНКРЕТНЫЙ обход
проходит успешно ровно до того дня, когда обход применят.

Чистая стандартная библиотека, никакой сети. Около 5–10 секунд.
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


def make_repo(tmp: Path, name: str = "repo") -> Path:
    """Репозиторий с «удалённой» основной веткой и рабочей веткой поверх."""
    repo = tmp / name
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


def reviewer_payload(cwd: Path, background: bool = False,
                     name: str = "change-reviewer") -> dict:
    return {
        "tool_name": "Agent",
        "cwd": str(cwd),
        "tool_input": {
            "subagent_type": name,
            "description": f"{name} run",
            "prompt": f"You are the `{name}` subagent",
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

        print("\n4. Обход через другую запись той же команды")
        # Каждая строка ниже — реально работавшая дыра, найденная на ревью.
        # Проверяются они здесь, а не в разделе про запреты, потому что
        # ломают именно ГЕЙТ: коммит проходил без ревью.
        evasions = [
            ("многострочная команда", "git status\ngit commit -m x"),
            ("обёртка env", "env git commit -m x"),
            ("обёртка command", "command git commit -m x"),
            ("подмена дерева через env", "env GIT_DIR=/tmp/x git commit -m x"),
            ("команда строкой в оболочке", 'bash -c "git commit -m x"'),
            ("коммит всего без индекса", "git commit -a -m x"),
            ("коммит всего, слитный флаг", "git commit -am x"),
            # Обёртка с флагом, забирающим значение: без таблицы таких флагов
            # «командой» становится значение (`user`, `KILL`), разбор говорит
            # «это не git», и гейт пропускает коммит.
            ("обёртка sudo с флагом-значением", "sudo -u someone git commit -m x"),
            ("обёртка env с флагом-значением", "env -u FOO git commit -m x"),
            ("обёртка env со сменой каталога", "env -C /tmp git commit -m x"),
            ("обёртка timeout с флагом-значением", "timeout -s KILL 10 git commit -m x"),
            ("две обёртки подряд", "sudo -u someone nice -n 5 git commit -m x"),
        ]
        for label, cmd in evasions:
            code, _o, _e = run_hook("commit_gate.py", {
                "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}, repo)
            check(f"гейт не обходится: {label}", code == 2, f"вернул {code} для {cmd!r}")

        # Безобидный коммит впереди не должен снимать гейт со всей строки:
        # проверка обязана дойти до КАЖДОГО коммита в команде.
        harmless = make_repo(tmp, "harmless")
        write_config(harmless)
        (harmless / "one.txt").write_text("x\n")
        git(["add", "one.txt"], harmless)
        code, _o, _e = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": f"git -C {harmless} commit -m ok && git commit -m x"}}, repo)
        check("первый безобидный коммит не снимает гейт со второго", code == 2)

        # `git commit -a` при ПУСТОМ индексе. Проверять его на дереве, где
        # что-то уже добавлено в индекс, бессмысленно: гейт сработает и без
        # разбора флага, и тест окажется зелёным при полностью выключенной
        # починке — ровно тот сорт проверки, против которого §11 SYSTEM.md.
        allflag = make_repo(tmp, "allflag")
        write_config(allflag)
        (allflag / "critical").mkdir()
        (allflag / "critical" / "a.txt").write_text("v1\n")
        git(["add", "critical/a.txt"], allflag)
        git(["commit", "-qm", "add critical"], allflag)
        (allflag / "critical" / "a.txt").write_text("v2\n")   # изменено, но НЕ в индексе
        empty_index = {"tool_name": "Bash", "cwd": str(allflag),
                       "tool_input": {"command": "git commit -m x"}}
        code, _o, _e = run_hook("commit_gate.py", empty_index, allflag)
        check("пустой индекс без -a — гейту нечего проверять", code == 0)
        for form in ("git commit -a -m x", "git commit -am x", "git commit --all -m x",
                     "git commit -S -am x", "git commit \\\n  -a -m x"):
            code, _o, _e = run_hook("commit_gate.py", {
                "tool_name": "Bash", "cwd": str(allflag),
                "tool_input": {"command": form}}, allflag)
            check(f"с пустым индексом {form!r} всё равно проверяется", code == 2)
        # Сообщение, начинающееся с дефиса, — не флаг «взять всё».
        code, _o, _e = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(allflag),
            "tool_input": {"command": "git commit -m '-a быстрая правка'"}}, allflag)
        check("сообщение с дефисом не читается как флаг -a", code == 0)

        print("\n5. Расписку нельзя потратить на файл, которого ревьюер не видел")
        run_hook("subagent_receipt.py", reviewer_payload(repo), repo)
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "git add critical/new.txt && REVIEW_DONE=1 git commit -m x"}},
            repo)
        check("добавление файла в одной команде с обходом отклоняется", code == 2, err[:160])
        # А повторный `git add` файла, который уже в проверенном диффе, ничего
        # к диффу не добавляет — запрещать его значит мешать без выигрыша.
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "git add critical/a.txt && REVIEW_DONE=1 git commit -m x"}},
            repo)
        check("повторное добавление уже проверенного файла разрешено", code == 0, err[:200])
        # …а вот КАТАЛОГ покрытым не считается: рядом с проверенным файлом
        # может лежать новый, которого ревьюер не видел.
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "git add critical && REVIEW_DONE=1 git commit -m x"}}, repo)
        check("добавление каталога не проходит по расписке", code == 2, err[:160])
        # Путь считается от каталога ОБОЛОЧКИ, а не от корня репозитория.
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo / "critical"),
            "tool_input": {"command": "git add a.txt && REVIEW_DONE=1 git commit -m x"}}, repo)
        check("путь из подкаталога приводится к корню дерева", code == 0, err[:200])
        # `-u` берёт только отслеживаемое и новых файлов внести не может.
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "git add -u && REVIEW_DONE=1 git commit -m x"}}, repo)
        check("`git add -u` не требует отдельного круга ревью", code == 0, err[:200])
        # `git commit <путь>` коммитит мимо индекса — тот же случай, что и `-a`.
        code, _o, _e = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(allflag),
            "tool_input": {"command": "git commit -m x critical/a.txt"}}, allflag)
        check("коммит с указанием пути проверяется", code == 2)

        # Имя одного ревьюера — префикс имени другого. Расписка более
        # узкоспециального НЕ должна закрывать требование к общему.
        prefixed = make_repo(tmp, "prefixed")
        write_config(prefixed, {"reviewers": {
            "always": ["change-reviewer"],
            "by_path": [{"pattern": "^никогда-не-совпадёт/", "reviewer": "change-reviewer-deep"}],
            "red_zone": "critical-zone-reviewer"}})
        (prefixed / "critical").mkdir()
        (prefixed / "critical" / "a.txt").write_text("v1\n")
        git(["add", "critical/a.txt"], prefixed)
        run_hook("subagent_receipt.py",
                 reviewer_payload(prefixed, name="change-reviewer-deep"), prefixed)
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(prefixed),
            "tool_input": {"command": "REVIEW_DONE=1 git commit -m x"}}, prefixed)
        check("расписка `change-reviewer-deep` не засчитана за `change-reviewer`",
              code == 2 and "change-reviewer: расписки нет" in err, err[:200])

        # Расписка, написанная руками, — это подделанное ревью. Имя старого
        # формата (без отпечатка в хвосте) не принимается, даже если тело
        # правильное: иначе достаточно создать файл с нужным именем.
        deep = list(Path(home).glob(".claude/state/subagent-receipts/prefixed/**/"
                                    "change-reviewer-deep-*"))
        forged = deep[0].parent / "change-reviewer-9999999999"
        forged.write_text(deep[0].read_text(), encoding="utf-8")
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(prefixed),
            "tool_input": {"command": "REVIEW_DONE=1 git commit -m x"}}, prefixed)
        check("расписка старого формата, созданная руками, не принимается",
              code == 2 and "change-reviewer: расписки нет" in err, err[:200])

        print("\n6. Переадресация дерева отклоняется, `git -C` — поддержана")
        for flag in ("--git-dir=/tmp/elsewhere", "--work-tree=/tmp/elsewhere"):
            code, _o, err = run_hook("commit_gate.py", {
                "tool_name": "Bash", "cwd": str(repo),
                "tool_input": {"command": f"REVIEW_DONE=1 git {flag} commit -m x"}}, repo)
            check(f"флаг переадресации отклонён: {flag}", code == 2, err[:120])
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "GIT_DIR=/tmp/elsewhere git commit -m x"}}, repo)
        check("переменная переадресации отклонена", code == 2, err[:120])

        # `git -C` — законный способ работать в соседней копии, и гейт обязан
        # проверять именно ЕЁ дерево. Иначе он либо мешает без причины, либо
        # (хуже) выносит вердикт по чужому диффу.
        other = make_repo(tmp, "other")
        write_config(other)
        (other / "small.txt").write_text("x\n")
        git(["add", "small.txt"], other)
        code, _o, err = run_hook("commit_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": f"git -C {other} commit -m x"}}, repo)
        check("`git -C` проверяет указанное дерево, а не дерево сессии",
              code == 0, err[:160])

        print("\n7. Красная зона требует человека даже при валидных расписках")
        (repo / "money").mkdir()
        (repo / "money" / "billing.txt").write_text("x\n")
        git(["add", "money/billing.txt"], repo)
        # Расписки нужны от ВСЕХ требуемых ревьюеров, иначе гейт откажет по
        # другой причине, и проверка красной зоны никогда не выполнится —
        # тест отчитывался бы об успехе, ничего не проверив.
        for reviewer in ("change-reviewer", "critical-zone-reviewer"):
            run_hook("subagent_receipt.py", reviewer_payload(repo, name=reviewer), repo)
        red_payload = {"tool_name": "Bash", "cwd": str(repo),
                       "tool_input": {"command": "MERGE_REVIEW_DONE=1 gh pr merge 1 --merge"}}
        code, _o, err = run_hook("merge_gate.py", red_payload, repo)
        check("мерж красной зоны не проходит автоматически", code == 2, err[:200])
        check("отказ ссылается именно на красную зону", "красную зону" in err, err[:200])
        code, _o, err = run_hook("merge_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "gh  pr  merge 1 --merge"}}, repo)
        check("лишние пробелы не проносят мерж мимо гейта", code == 2, err[:120])
        code, _o, err = run_hook("merge_gate.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": 'echo "gh pr merge 1 --merge"'}}, repo)
        check("упоминание мержа в тексте гейт не трогает", code == 0, err[:200])

        print("\n8. Предполётная проверка ревьюера")
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

        print("\n9. Предупреждение о повторных кругах ревью")
        # К этому моменту change-reviewer уже видел ветку в нескольких разных
        # версиях диффа. Предупреждение обязано появиться — и обязано молчать
        # на фоновом запуске, который расписку всё равно не выписывает.
        for marker in ("r1", "r2", "r3"):
            (repo / "critical" / "a.txt").write_text(marker + "\n")
            git(["add", "critical/a.txt"], repo)
            run_hook("subagent_receipt.py", reviewer_payload(repo), repo)
        code, out, _e = run_hook("review_churn_warn.py", reviewer_payload(repo), repo)
        check("предупреждение о переделке показано", "Повторное ревью" in out, out[:160])
        check("предупреждение ничего не блокирует", code == 0)
        code, out, _e = run_hook("review_churn_warn.py",
                                 reviewer_payload(repo, background=True), repo)
        check("на фоновом запуске предупреждение молчит", out.strip() == "", out[:160])
        # Поле может вообще отсутствовать — это самый частый случай, и именно
        # ради него написано предупреждение. Трактовать «нет поля» как фоновый
        # запуск значит замолчать там, где предупреждать обязаны.
        without_flag = reviewer_payload(repo)
        del without_flag["tool_input"]["run_in_background"]
        code, out, _e = run_hook("review_churn_warn.py", without_flag, repo)
        check("без поля о фоне предупреждение всё равно показано",
              "Повторное ревью" in out, out[:160])

        print("\n10. Запрет разрушительных команд: обе стороны")
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
            # Перенос длинной команды: глагол на первой строке, опасный флаг —
            # на следующей. Без склейки продолжений защита слепа ко всему,
            # что записано в две строки, а так пишут постоянно.
            "git add \\\n  -A",
            "git reset \\\n  --hard HEAD~1",
            "rm -r \\\n  -f /tmp/zzz",
            "git commit -m x \\\n  --amend",
            "git push \\\n  --force origin feature",
            # Тело, которое исполняет оболочка, — это скрипт, а не данные.
            "bash <<'EOF'\ngit push --force origin feature\nEOF",
            # `<<` внутри кавычек документа не открывает, и остаток команды
            # обязан остаться видимым.
            'echo "a << b"\ngit rebase main',
            "perl -pi -e 's/a/b/' file.txt",
            "perl -i.bak -pe 's/a/b/' file.txt",
            "timeout 5m git push --force origin feature",
            # Оболочка не обязана стоять первым словом строки.
            "git status && bash <<'EOF'\ngit push --force origin feature\nEOF",
            "env bash <<'EOF'\ngit rebase main\nEOF",
            # Экранированная кавычка не должна «открывать» кавычку до конца
            # строки: иначе весь остаток команды невидим для всех проверок.
            "echo don\\'t; git rebase main",
            # Слитные связки флагов — обычная запись.
            "git push -fu origin feature",
            "git push --all origin",
            "git add -Av",
            "git stage -A",
            # Незакрытый вложенный документ ничего не прячет.
            "cat > f <<'EOF'\ngit rebase main",
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
            "git push -u origin feature",
            # Значение флага — не имя ветки: иначе проверка «отправка без явной
            # ветки» решит, что ветка указана, и молча выключится.
            "git push -o ci.skip origin feature",
            # Тело вложенного документа — это данные, а не команды.
            "cat > note.txt <<'EOF'\ngit rebase main\nrm -rf /tmp/x\nEOF",
            # Обычные однобуквенные флаги интерпретаторов — не правка на месте.
            "perl -Mstrict -e 'print 1'",
            "perl -MList::Util=sum -e 'print 1'",
            "ruby -Ilib -e 'puts 1'",
            "git add -p",
        ]
        # Отправка без явной ветки запрещена только ИЗ основной ветки — а это
        # значит, что на обычной ветке та же команда обязана проходить.
        onmain = make_repo(tmp, "onmain")
        write_config(onmain)
        git(["checkout", "-q", "main"], onmain)
        for cmd, expect in (("git push", 2), ("git push -o ci.skip origin", 2),
                            ("git push origin feature", 0)):
            code, _o, _e = run_hook("guard_bash.py", {
                "tool_name": "Bash", "cwd": str(onmain),
                "tool_input": {"command": cmd}}, onmain)
            check(f"из основной ветки {cmd!r} -> {expect}", code == expect, f"вернул {code}")
        for cmd in blocked:
            code, _o, _e = run_hook("guard_bash.py", {
                "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}, repo)
            check(f"блокируется: {cmd}", code == 2)
        for cmd in allowed:
            code, _o, err = run_hook("guard_bash.py", {
                "tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": cmd}}, repo)
            check(f"пропускается: {cmd}", code == 0, err[:120])

        print("\n11. Ресурсные ограничители")
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
        # Потолок снаружи, команда внутри оболочки — рекомендуемая же форма.
        # Требовать `timeout` ещё и во вложенном сегменте значит блокировать
        # ровно то, что сам ограничитель и советует.
        code, _o, err = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": 'timeout 600 bash -c "pytest tests/unit"'}}, repo)
        check("потолок снаружи покрывает вложенную команду", code == 0, err[:160])
        # …но покрывает только СВОЙ сегмент: соседняя команда своим потолком
        # не обзаводится, и общий поиск «есть ли timeout хоть где-то» снял бы
        # требование с неё.
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "timeout 5 true && pytest tests/"}}, repo)
        check("чужой потолок не покрывает соседнюю команду", code == 2)
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "npm run build"}}, repo)
        check("тяжёлая сборка локально блокируется", code == 2)
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": "npm run build"}}, repo, {"HEAVY_BUILD_OK": "1"})
        check("эвакуационный выход работает", code == 0)
        # Ограничитель обязан различать ЗАПУСК и УПОМИНАНИЕ: иначе он блокирует
        # сообщение коммита и чтение файла с похожим именем, а перехватчик,
        # мешающий обычной работе, выключают.
        for cmd in ('git commit -m "fix: flaky pytest run"',
                    'git commit -m "ci: npm run build moved to CI"',
                    "cat pytest.ini",
                    "grep -rn pytest ."):
            code, _o, err = run_hook("guard_resources.py", {
                "tool_name": "Bash", "cwd": str(repo),
                "tool_input": {"command": cmd}}, repo)
            check(f"упоминание, а не запуск: {cmd[:38]!r}", code == 0, err[:120])
        code, _o, _e = run_hook("guard_resources.py", {
            "tool_name": "Bash", "cwd": str(repo),
            "tool_input": {"command": 'bash -c "timeout 600 pytest tests/"'}}, repo)
        check("потолок внутри оболочки тоже считается", code == 0)

        print("\n12. Гейт плана")
        planned = make_repo(tmp, "planned")
        write_config(planned, {"plan": {
            "enabled": True, "dir": "docs/plans", "min_files": 2,
            "status_patterns": [
                r"^Статус: согласован владельцем \d{4}-\d{2}-\d{2}$",
                "^Статус: технический — согласование не требуется$",
            ]}})
        for i in range(3):
            (planned / f"f{i}.txt").write_text("x\n")
            git(["add", f"f{i}.txt"], planned)
        commit_here = {"tool_name": "Bash", "cwd": str(planned),
                       "tool_input": {"command": "git commit -m x"}}
        code, _o, err = run_hook("plan_gate.py", commit_here, planned)
        check("ветка выросла, плана нет — отказ", code == 2, err[:120])

        plan_dir = planned / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "1-example.md"
        # Заглушка из шаблона — ровно то, что должно НЕ подходить.
        plan_file.write_text("Статус: <выберите один из двух вариантов>\n", encoding="utf-8")
        git(["add", "docs/plans/1-example.md"], planned)
        code, _o, err = run_hook("plan_gate.py", commit_here, planned)
        check("план есть, но статус не выбран — отказ", code == 2, err[:120])

        plan_file.write_text("Статус: технический — согласование не требуется\n", encoding="utf-8")
        git(["add", "docs/plans/1-example.md"], planned)
        code, _o, err = run_hook("plan_gate.py", commit_here, planned)
        check("статус выбран — проход", code == 0, err[:200])

        code, _o, _e = run_hook("plan_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command": "PLAN_OK=1 git commit -m x"}}, planned)
        check("аварийный выход из гейта плана работает", code == 0)

        print("\n13. Гейт «сначала задача»")
        git(["commit", "-qm", "work"], planned)
        pr_no_link = {"tool_name": "Bash", "cwd": str(planned),
                      "tool_input": {"command": 'gh pr create --title t --body "просто текст"'}}
        code, _o, err = run_hook("issue_link_gate.py", pr_no_link, planned)
        check("PR без ссылки на задачу отклонён", code == 2, err[:120])
        code, _o, err = run_hook("issue_link_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command": 'gh  pr  create --title t --body "просто текст"'}}, planned)
        check("лишние пробелы не проносят PR мимо гейта", code == 2, err[:120])
        code, _o, err = run_hook("issue_link_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command": 'echo "gh pr create --body x"'}}, planned)
        check("упоминание команды в тексте гейт не трогает", code == 0, err[:200])
        # Две самые обычные записи длинного описания. Обе должны проходить:
        # ссылка на задачу в описании есть, и гейт обязан её увидеть.
        code, _o, err = run_hook("issue_link_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command": 'gh pr create --title t \\\n  --body "Closes #42"'}}, planned)
        check("перенос строки не скрывает описание PR", code == 0, err[:200])
        code, _o, err = run_hook("issue_link_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command":
                           'gh pr create --title t --body "$(cat <<\'EOF\'\nCloses #42\nEOF\n)"'}},
            planned)
        check("описание из вложенного документа в кавычках читается", code == 0, err[:200])
        code, _o, err = run_hook("issue_link_gate.py", {
            "tool_name": "Bash", "cwd": str(planned),
            "tool_input": {"command": 'gh pr create --title t --body "Closes #42"'}}, planned)
        check("PR со ссылкой на задачу проходит", code == 0, err[:200])
        git(["checkout", "-qb", "hotfix/urgent"], planned)
        code, _o, err = run_hook("issue_link_gate.py", pr_no_link, planned)
        check("горячий фикс освобождён от гейта", code == 0, err[:200])

        print("\n14. Защита общей копии")
        shared = make_repo(tmp, "shared")
        mine = tmp / "mine"
        mine.mkdir()
        write_config(repo, {"shared_checkout": str(shared)})
        code, _o, err = run_hook("worktree_guard.py", {
            "tool_name": "Edit", "cwd": str(mine),
            "tool_input": {"file_path": str(shared / "README.md")}}, repo)
        check("правка в общей копии отклонена", code == 2, err[:120])
        code, _o, _e = run_hook("worktree_guard.py", {
            "tool_name": "Edit", "cwd": str(mine),
            "tool_input": {"file_path": str(mine / "README.md")}}, repo)
        check("правка в своей копии разрешена", code == 0)
        # Символическая ссылка — тот самый случай, ради которого путь
        # разыменовывается: путь выглядит «своим», а файл лежит в общей копии.
        (mine / "link").symlink_to(shared)
        code, _o, _e = run_hook("worktree_guard.py", {
            "tool_name": "Edit", "cwd": str(mine),
            "tool_input": {"file_path": str(mine / "link" / "README.md")}}, repo)
        check("обход через символическую ссылку закрыт", code == 2)
        # Относительный путь считается от каталога сессии, а не от каталога,
        # из которого рантайм запустил сам перехватчик.
        code, _o, _e = run_hook("worktree_guard.py", {
            "tool_name": "Edit", "cwd": str(shared),
            "tool_input": {"file_path": "README.md"}}, repo)
        check("относительный путь считается от каталога сессии", code == 2)

    print("\n15. Регистрация перехватчика переживает свой худший случай")
    # Если рантайм убьёт перехватчик по своему таймауту раньше, чем тот успеет
    # ответить, действие будет РАЗРЕШЕНО. То есть гейт, объявленный падающим
    # закрыто, окажется падающим открыто — и ровно на самых тяжёлых деревьях,
    # где он нужнее всего. Числа в двух файлах обязаны сходиться.
    settings = json.loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
    defaults = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    cheap, heavy = defaults["git_timeout"], defaults["git_diff_timeout"]
    # (дешёвых запросов, полных диффов ветки) на один прогон — считано по коду.
    BUDGET = {
        "guard_bash.py": (2, 0), "guard_resources.py": (0, 0),
        "commit_gate.py": (5, 2), "merge_gate.py": (3, 3),
        "plan_gate.py": (4, 1), "issue_link_gate.py": (5, 2),
        "worktree_guard.py": (0, 0), "preflight_review_guard.py": (3, 1),
        "review_churn_warn.py": (2, 0), "subagent_receipt.py": (3, 1),
    }
    registered = {}
    for group in settings["hooks"].values():
        for entry in group:
            for hook in entry["hooks"]:
                registered[hook["command"].rsplit("/", 1)[-1]] = hook["timeout"]
    for name, (c, h) in BUDGET.items():
        ceiling = c * cheap + h * heavy
        got = registered.get(name)
        check(f"регистрация {name} ≥ потолка {ceiling}с",
              got is not None and got >= ceiling, f"зарегистрирован {got}")
    check("свежесть рабочей копии переживает свой fetch",
          registered.get("worktree_freshness.py", 0) >= 25 + cheap,
          f"зарегистрирован {registered.get('worktree_freshness.py')}")

    print("\n16. В скриптах нет проектных путей — только конфиг")
    hardcoded = re.compile(r"[\"'](/(?:home|root|Users)/[^\"']+)[\"']")
    for script in sorted(HOOKS.glob("*.py")) + sorted((HOOKS / "lib").glob("*.py")):
        text = script.read_text(encoding="utf-8")
        bad = hardcoded.findall(text)
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
