"""Собрать состояние проекта из живых источников и напечатать его в Markdown.

Страница состояния, которую пишут руками, устаревает на второй день и начинает
врать — а врущая страница хуже отсутствующей: по ней принимают решения. Поэтому
здесь ни одной константы, которую надо помнить: число тестов берётся из CI,
список источников — из реестра провайдеров, баланс — из ответа поставщика,
состояние сервера — с самого сервера.

    python scripts/state_report.py            # в stdout
    python scripts/state_report.py --out FILE # в файл

Чего скрипт НЕ делает: не выдумывает. Недоступный источник печатается как
«не удалось получить», а не как ноль или прочерк — иначе отчёт о состоянии
повторил бы ровно ту ошибку, против которой построен весь продукт.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "elenaisanewleet/collector-bot"
SITE = "https://proverka-dolga.shop"
BOT = "@collector_search_bot"
SERVER = "193.168.199.122"
NEWDB_URL = "https://api.newdb.net/v2"

UNKNOWN = "не удалось получить"


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    body: str


def _run(*args: str, cwd: Path | None = None, timeout: int = 30) -> str | None:
    """Выполнить команду и вернуть вывод. ``None`` — если не получилось."""
    try:
        done = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- источники


def git_facts() -> dict[str, str]:
    root = _root()
    commits = _run("git", "rev-list", "--count", "HEAD", cwd=root)
    last = _run("git", "log", "-1", "--format=%h %s", cwd=root)
    when = _run("git", "log", "-1", "--format=%ci", cwd=root)
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    return {
        "commits": commits or UNKNOWN,
        "last": last or UNKNOWN,
        "when": when or UNKNOWN,
        "branch": branch or UNKNOWN,
    }


def code_size() -> dict[str, str]:
    root = _root()
    out: dict[str, str] = {}
    for label, folder in (("app", "app"), ("tests", "tests")):
        target = root / folder
        if not target.exists():
            out[label] = UNKNOWN
            continue
        total = sum(
            len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            for path in target.rglob("*.py")
        )
        out[label] = f"{total:,}".replace(",", " ")
    migrations = list((root / "migrations" / "versions").glob("[0-9]*.py"))
    out["migrations"] = str(len(migrations)) if migrations else UNKNOWN
    return out


def ci_facts() -> dict[str, str]:
    """Последний прогон CI и число тестов из его журнала."""
    raw = _run(
        "gh",
        "run",
        "list",
        "--repo",
        REPO,
        "--branch",
        "main",
        "--limit",
        "1",
        "--json",
        "databaseId,conclusion,createdAt",
    )
    if not raw:
        return {"status": UNKNOWN, "tests": UNKNOWN, "when": UNKNOWN}
    try:
        runs = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": UNKNOWN, "tests": UNKNOWN, "when": UNKNOWN}
    if not runs:
        return {"status": "прогонов нет", "tests": UNKNOWN, "when": UNKNOWN}

    run = runs[0]
    log = _run("gh", "run", "view", str(run["databaseId"]), "--repo", REPO, "--log", timeout=120)
    tests = UNKNOWN
    if log:
        for line in log.splitlines():
            if " passed in " in line:
                tests = line.split(" passed")[0].split()[-1]
    return {
        "status": "зелёный" if run.get("conclusion") == "success" else str(run.get("conclusion")),
        "tests": tests,
        "when": str(run.get("createdAt", UNKNOWN))[:16].replace("T", " "),
    }


def site_status() -> str:
    try:
        with urllib.request.urlopen(f"{SITE}/healthz", timeout=15) as response:
            return "отвечает" if response.status == 200 else f"HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError):
        return UNKNOWN


def newdb_balance() -> str:
    """Остаток оплаченных вызовов. Стоит одного вызова, поэтому вызов дешёвый."""
    env = _root() / ".env"
    if not env.exists():
        return UNKNOWN
    key = next(
        (
            line.split("=", 1)[1].strip()
            for line in env.read_text(encoding="utf-8").splitlines()
            if line.startswith("NEWDB_API_KEY=")
        ),
        "",
    )
    if not key:
        return "ключ не задан"

    body = json.dumps(
        {"params": {"method": "pledge_vin", "country": "ru", "vin": "XTA210990S1234567"}}
    ).encode()
    request = urllib.request.Request(
        NEWDB_URL,
        data=body,
        headers={"Content-Type": "application/json", "X-API-KEY": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return UNKNOWN
    balance = payload.get("balance")
    return str(balance) if balance is not None else UNKNOWN


def providers() -> str:
    """Список источников из реестра, а не из памяти пишущего."""
    root = _root()
    script = (
        "from app.config import get_settings;"
        "from app.db.session import Database;"
        "from app.providers.registry import build_registry;"
        "s=get_settings();r=build_registry(s, Database(s.database_url));"
        "print(', '.join(p.title for p in r.external))"
    )
    venv = root / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else "python3"
    out = _run(python, "-c", script, cwd=root)
    if not out:
        return UNKNOWN
    # Приложение пишет журнал в stdout при импорте настроек; нам нужна только
    # последняя строка — сам список источников.
    return out.splitlines()[-1].strip()


def server_status() -> str:
    out = _run(
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
        f"root@{SERVER}",
        "cd /opt/collector-bot && docker compose -f docker-compose.prod.yml ps "
        '--format "{{.Name}} {{.Status}}"',
        timeout=45,
    )
    return out or f"{UNKNOWN} (нужен ключ SSH; по паролю скрипт не ходит намеренно)"


# ---------------------------------------------------------------- сборка


def build() -> str:
    git = git_facts()
    size = code_size()
    ci = ci_facts()

    lines = [
        "# Collector Bot — состояние проекта",
        "",
        f"Собрано автоматически: `scripts/state_report.py`. Последний коммит {git['when']}.",
        "",
        "## Коротко",
        "",
        "| | |",
        "|---|---|",
        f"| Сайт | {SITE} — {site_status()} |",
        f"| Бот | {BOT} |",
        f"| Репозиторий | `{REPO}` (приватный), ветка `{git['branch']}` |",
        f"| Сервер | `{SERVER}`, `/opt/collector-bot` |",
        f"| CI | {ci['status']}, тестов {ci['tests']} ({ci['when']}) |",
        f"| Коммитов | {git['commits']} |",
        f"| Кода / тестов | {size['app']} / {size['tests']} строк |",
        f"| Миграций базы | {size['migrations']} |",
        f"| Остаток оплаченных запросов | {newdb_balance()} |",
        "",
        f"Последний коммит: `{git['last']}`",
        "",
        "## Контейнеры на сервере",
        "",
        "```",
        server_status(),
        "```",
        "",
        "## Подключённые источники",
        "",
        providers(),
        "",
        "## Что это делает",
        "",
        "Оператор вводит телефон, ФИО или ИНН должника. Бот находит человека во",
        "внутренней выгрузке (данные заказчика из 1С), достаёт оттуда недостающие",
        "поля и опрашивает официальные реестры. На выходе — вердикт «иск / судебный",
        "приказ / проверить руками / не подавать» с расчётом госпошлины и ссылка на",
        "веб-отчёт.",
        "",
        "Главный сценарий — не проверка одного человека, а очередь по сотням:",
        "из выгрузки выбираются те, по кому суд окупается.",
        "",
        "## Правило, на котором построен продукт",
        "",
        "«Не проверено» никогда не должно читаться как «ничего не найдено».",
        "Источник, который не отвечал, помечается отдельно и не даёт плюсов в",
        "оценке. Ради этого отчёт различает шесть состояний источника, а не два.",
        "",
        "## Что не сделано",
        "",
        "- нет выгрузки должников от заказчика: массовый прогон ни разу не",
        "  запускался на настоящих людях;",
        "- прямой интеграции с 1С нет, работает импорт выгрузки;",
        "- БКИ не подключено: нужен договор с бюро кредитных историй.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="куда записать; по умолчанию stdout")
    args = parser.parse_args()

    text = build()
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"записано: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
