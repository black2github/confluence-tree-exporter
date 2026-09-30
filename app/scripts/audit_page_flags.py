# app/scripts/audit_page_flags.py
#
# Сверка страничных флагов заморозки уже выгруженного архива с «Историей
# изменений» страниц в Confluence (2026-09-30). ТОЛЬКО ЧТЕНИЕ: архив не меняется.
#
# Зачем. До версии 1.9.1 экспортёр замораживал страницу целиком
# (`unapproved_jira: <ID>`), если задача из списка --unapproved-jira стояла в ЛЮБОЙ
# чёрной строке истории. Задача в середине истории чужой страницы давала тот же
# результат, и reject-all оставлял от страницы один frontmatter — молчаливая потеря
# требований, давно введённых в ПРОМ. Правило исправлено: форс только когда задача
# из списка создала страницу (первая по дате запись, чёрная). Но в архиве истории
# нет — её вырезает экспорт, — поэтому проверить старую выгрузку можно только
# повторным чтением истории из Confluence. Этим занимается сверка.
#
# Что делает. Для каждой страницы архива с флагом `unapproved_jira` берёт её
# `confluence_page_id`, читает страницу и применяет то же правило, что экспортёр
# (app.color_map.decide_forced_unapproved). Вердикты:
#   подтверждён           — задача флага создала страницу: заморозка верна;
#   ЗАМОРОЖЕНА ОШИБОЧНО   — задача флага есть в чёрных записях истории, но
#                           страницу создала не она: страница потеряна целиком;
#   флаг не по списку     — задачи флага в чёрных записях нет: флаг поставлен
#                           сторожем (вся страница под цветом одной задачи),
#                           ключом --flag-page или руками; правка его не касается;
#   нет данных            — нет page_id, страница не загрузилась или история
#                           не опознана: проверить руками.
#
# Ограничение: сверяется история, какой она стала СЕЙЧАС. Если страницу правили
# после выгрузки, вердикт относится к текущему состоянию.
#
# Использование:
#     python -m app.scripts.audit_page_flags <корень архива>
#     python -m app.scripts.audit_page_flags <корень архива> --out audit.md
#     python -m app.scripts.audit_page_flags <корень архива> --unapproved-jira unapproved.json
#     python -m app.scripts.audit_page_flags <корень архива> --html-dir debug/html
#
#   --unapproved-jira  тот же список, что давали экспорту. Без него правило
#                      проверяется для одной задачи — той, что стоит во флаге
#                      страницы (этого достаточно для вердикта по флагу).
#   --html-dir         офлайн-источник: каталог с файлами <page_id>.html
#                      (отладка и тесты); Confluence не вызывается.
#   --api              читать через REST API вместо HTTP (по умолчанию HTTP,
#                      как у экспорта: API в контуре закрыт).
#   --out              записать отчёт в markdown-файл (иначе только в консоль).
#
# Код возврата: 0 — ошибочных заморозок нет; 1 — есть; 2 — ошибка запуска.

import argparse
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from app.color_map import decide_forced_unapproved, find_history_table
from app.scripts.repair_export import (load_unapproved_ids, page_flag_of,
                                       split_frontmatter)

OK = "подтверждён"
WRONG = "ЗАМОРОЖЕНА ОШИБОЧНО"
FOREIGN = "флаг не по списку"
NO_DATA = "нет данных"
ORDER = (WRONG, NO_DATA, FOREIGN, OK)

_PAGE_ID_RE = re.compile(r"^confluence_page_id:\s*['\"]?(\d+)['\"]?\s*$", re.M)
_TITLE_RE = re.compile(r"^title:\s*(.*?)\s*$", re.M)


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _title(fm_body: str, fallback: str) -> str:
    m = _TITLE_RE.search(fm_body)
    if not m:
        return fallback
    value = m.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].replace("''", "'")
    return value or fallback


def flagged_pages(root: Path) -> List[dict]:
    """Страницы архива со страничным флагом: путь, заголовок, page_id, задача."""
    files = [root] if root.is_file() else sorted(root.rglob("*.md"))
    pages = []
    for path in files:
        parts = split_frontmatter(_read(path))
        if parts is None:
            continue
        fm_body = parts[1]
        task = page_flag_of(fm_body)
        if not task:
            continue
        m = _PAGE_ID_RE.search(fm_body)
        pages.append({"path": path, "title": _title(fm_body, path.stem),
                      "page_id": m.group(1) if m else "", "task": task})
    return pages


def audit_page(page: dict, raw_html: Optional[str], unapproved: Optional[set]) -> dict:
    """Вердикт по одной странице. unapproved=None — правило для задачи флага."""
    task = page["task"]
    result = dict(page)
    if not page["page_id"]:
        result.update(verdict=NO_DATA, detail="в frontmatter нет confluence_page_id")
        return result
    if not raw_html:
        result.update(verdict=NO_DATA, detail="страница не загружена")
        return result

    if find_history_table(BeautifulSoup(raw_html, "html.parser")) is None:
        result.update(verdict=NO_DATA, detail="секция «История изменений» не опознана")
        return result

    ids = set(unapproved) | {task} if unapproved else {task}
    decision = decide_forced_unapproved(raw_html, ids)
    if decision.forced:
        detail = ""
        if decision.forced.task != task:
            # форс верен, но сегодня экспорт пометил бы состав другой задачей
            detail = (f"сейчас состав пометился бы задачей {decision.forced.task} "
                      f"(в истории несколько задач из списка)")
        result.update(verdict=OK, detail=detail)
    elif decision.notes and task in decision.notes[0]["tasks"]:
        result.update(verdict=WRONG, detail=decision.notes[0]["kind"])
    elif decision.notes:
        result.update(verdict=FOREIGN,
                      detail=f"в истории есть другие задачи из списка "
                             f"{decision.notes[0]['tasks']}: {decision.notes[0]['kind']}")
    else:
        result.update(verdict=FOREIGN,
                      detail="задачи флага нет в чёрных записях истории")
    return result


def audit(root: Path, fetch: Callable[[str], Optional[str]],
          unapproved: Optional[set] = None) -> List[dict]:
    """Сверка всех страниц с флагом. fetch(page_id) -> raw_html или None."""
    results = []
    for page in flagged_pages(root):
        raw = fetch(page["page_id"]) if page["page_id"] else None
        results.append(audit_page(page, raw, unapproved))
    return results


def render_report(results: List[dict], root: Path) -> str:
    counts: Dict[str, int] = {v: 0 for v in ORDER}
    for r in results:
        counts[r["verdict"]] += 1
    lines = ["# Сверка страничных флагов заморозки с историей изменений", "",
             f"Архив: `{root}`. Страниц с флагом `unapproved_jira`: {len(results)}.", "",
             "| Вердикт | Страниц |", "|---|---|"]
    lines += [f"| {v} | {counts[v]} |" for v in ORDER]
    lines.append("")

    def _section(verdict: str, title: str, hint: str) -> None:
        rows = [r for r in results if r["verdict"] == verdict]
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.extend(["_нет_", ""])
            return
        lines.extend([hint, ""])
        for r in sorted(rows, key=lambda x: (x["task"], x["title"])):
            tail = f" — {r['detail']}" if r["detail"] else ""
            lines.append(f"- `{r['task']}` «{r['title']}» (page_id "
                         f"{r['page_id'] or '—'}){tail}")
        lines.append("")

    _section(WRONG, "Заморожена ошибочно — страница потеряна целиком",
             "Задача флага есть в истории, но страницу создала не она. После "
             "reject-all от страницы остаётся один frontmatter. Нужна повторная "
             "выгрузка этих страниц версией 1.9.1 или новее.")
    _section(NO_DATA, "Нет данных — проверить руками",
             "Страницу не удалось сверить.")
    _section(FOREIGN, "Флаг не по списку — правка его не касается",
             "Флаг поставлен сторожем заморозки (вся страница под цветом одной "
             "задачи), ключом --flag-page или руками.")
    _section(OK, "Подтверждён", "Задача флага создала страницу — заморозка верна.")
    return "\n".join(lines) + "\n"


def _html_dir_fetch(html_dir: Path) -> Callable[[str], Optional[str]]:
    def fetch(page_id: str) -> Optional[str]:
        path = html_dir / f"{page_id}.html"
        return path.read_text(encoding="utf-8") if path.is_file() else None
    return fetch


def _confluence_fetch(use_http: bool) -> Callable[[str], Optional[str]]:
    def fetch(page_id: str) -> Optional[str]:
        from app.page_cache import get_page_data
        data = get_page_data(page_id, use_http=use_http)
        return (data or {}).get("raw_html") or None
    return fetch


def main(argv=None) -> int:
    from app.version import banner
    print(banner("audit"))

    parser = argparse.ArgumentParser(
        prog="audit_page_flags",
        description="Сверка страничных флагов заморозки архива с историей изменений "
                    "в Confluence. Только чтение.")
    parser.add_argument("root", help="корень архива выгрузки (каталог или один .md)")
    parser.add_argument("--unapproved-jira", metavar="FILE",
                        help="список неутверждённых задач, как у экспорта")
    parser.add_argument("--html-dir", metavar="DIR",
                        help="офлайн-источник: каталог с <page_id>.html")
    parser.add_argument("--api", action="store_true",
                        help="читать через REST API вместо HTTP")
    parser.add_argument("--out", metavar="FILE", help="записать отчёт в markdown-файл")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"ОШИБКА: путь не найден: {root}", file=sys.stderr)
        return 2
    unapproved = None
    if args.unapproved_jira:
        try:
            unapproved = load_unapproved_ids(Path(args.unapproved_jira))
        except (OSError, ValueError) as e:
            print(f"ОШИБКА: {args.unapproved_jira}: {e}", file=sys.stderr)
            return 2
    if args.html_dir:
        html_dir = Path(args.html_dir)
        if not html_dir.is_dir():
            print(f"ОШИБКА: каталог не найден: {html_dir}", file=sys.stderr)
            return 2
        fetch = _html_dir_fetch(html_dir)
    else:
        fetch = _confluence_fetch(use_http=not args.api)

    results = audit(root, fetch, unapproved)
    report = render_report(results, root)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        print(f"отчёт: {args.out}")

    wrong = [r for r in results if r["verdict"] == WRONG]
    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ORDER}
    print(f"[audit] страниц с флагом: {len(results)}; "
          + "; ".join(f"{v}: {counts[v]}" for v in ORDER))
    for r in sorted(wrong, key=lambda x: (x["task"], x["title"])):
        print(f"  ✗ {r['task']} «{r['title']}» (page_id {r['page_id']}): {r['detail']}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
