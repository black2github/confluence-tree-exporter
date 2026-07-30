# app/scripts/migrate_colors.py
#
# Модуль 1 (срез 1a), ТЗ п. 4.9-4.10: режим «только отчёт» миграции цвета.
#
# Строит по набору страниц Confluence (rendered-view HTML) карту «цвет → задача», обходит
# тело каждой страницы, классифицирует все встреченные цвета и формирует:
#   • migration-manifest.yaml   — реестр мигрировавших задач (ТЗ п. 4.9);
#   • migration-colors-report.json + .md — всё, что требует ручного разбора (ТЗ п. 4.10).
#
# Команда `report` НЕ пишет маркеры (ТЗ п. 4.3: «первый прогон — только отчёт»); команда
# `migrate` пишет .md с маркерами (боевой проход).
#
# Основной вход — ПРЯМОЕ чтение страниц Confluence через штатный загрузчик (page_cache):
# `--pages <id,...>` или `--root <id>` (обход поддерева). HTML берётся в память (raw_html),
# промежуточное сохранение файлов НЕ выполняется. Ключ `--html-dir <каталог>` — опциональный
# офлайн-режим для отладки на сохранённых .html (напр. debug/html). Всё ядро (карта, обход,
# critic-экстракция) работает с HTML-строкой и от источника не зависит.

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from app.color_map import build_color_task_map, survey_body_colors
from app.utils.style_utils import to_rgb_notation


def new_accumulator() -> dict:
    """Пустой аккумулятор отчёта/манифеста. Наполняется постранично accumulate_page()."""
    return {
        "pages": 0,
        "pages_without_history": [],
        "collisions": [],
        "jira_unextractable": [],
        "unresolved_placeholders": [],
        "nested": [],                 # уплощённые вложенности (ТЗ 4.5) — заполняет вызывающий
        "tasks": {},                  # task_id -> {color, confidence, pages:set, markers}
        "color_summary": [],          # список {page, color, count, classification, task} — постранично
        "colored_fragments_total": 0,
    }


def accumulate_page(acc: dict, name: str, result, survey: dict) -> None:
    """Добавляет в аккумулятор данные одной страницы (карта истории + обход тела).

    result — HistoryMapResult, survey — результат survey_body_colors(). Вынесено из
    aggregate(), чтобы боевой проход (migrate_confluence_tree) мог наполнять тот же
    аккумулятор картой, уже построенной для критик-экстракции, без повторного разбора.
    """
    acc["pages"] += 1
    if result.no_history:
        acc["pages_without_history"].append(name)
    for col in result.collisions:
        acc["collisions"].append({"page": name, **col})
    for u in result.unresolved_jira:
        acc["jira_unextractable"].append({"page": name, "color": u["color"]})

    for color, info in survey.items():
        cls, cnt = info["classification"], info["count"]
        # Постраничная сводка (ТЗ 4.3): классификация цвета корректна ДЛЯ ЭТОЙ страницы —
        # один цвет на разных страницах может быть task/unknown/black (постраничность 4.2.д).
        acc["color_summary"].append({
            "page": name, "color": color, "count": cnt,
            "classification": cls, "task": info.get("task"),
            "delta_e": info.get("delta_e")})  # ΔE до чёрного — для калибровки порога (4.3.1)

        # 'black'/'near-black' (ПРОМ) и 'ignored' (UI-цвет) — не правка: в задачи не идут.
        if cls in ("black", "near-black", "ignored"):
            continue
        acc["colored_fragments_total"] += cnt

        task_id = info["task"]
        if cls == "unknown":
            acc["unresolved_placeholders"].append({
                "placeholder": task_id, "color": color, "page": name,
                "count": cnt, "reason": info.get("reason")})
            confidence = "unresolved"
        else:
            confidence = result.confidence.get(color, "high")

        entry = acc["tasks"].setdefault(
            task_id, {"color": color, "confidence": confidence, "pages": set(), "markers": 0})
        entry["pages"].add(name)
        entry["markers"] += cnt
        if confidence == "low":  # коллизия приоритетнее 'high'
            entry["confidence"] = "low"


def finalize(acc: dict, service: str, migrated_at: str) -> Tuple[dict, dict]:
    """Строит (manifest, report) из наполненного аккумулятора."""
    manifest_tasks = {}
    for task_id in sorted(acc["tasks"]):
        e = acc["tasks"][task_id]
        manifest_tasks[task_id] = {
            "color": e["color"], "confidence": e["confidence"],
            "pages": sorted(e["pages"]), "markers": e["markers"],
        }
    manifest = {"migrated_at": migrated_at, "service": service, "tasks": manifest_tasks}

    positions = (len(acc["unresolved_placeholders"]) + len(acc["collisions"])
                 + len(acc["jira_unextractable"]) + len(acc["nested"]))
    report = {
        "migrated_at": migrated_at,
        "service": service,
        "stats": {
            "pages_processed": acc["pages"],
            "colored_fragments_total": acc["colored_fragments_total"],
            "positions_manual_review": positions,
            "nested_flattened": len(acc["nested"]),
        },
        "pages_without_history": acc["pages_without_history"],
        "unresolved_placeholders": acc["unresolved_placeholders"],
        "collisions": acc["collisions"],
        "jira_unextractable": acc["jira_unextractable"],
        "nested_flattened": acc["nested"],
        "color_summary": sorted(acc["color_summary"],
                                key=lambda c: (c["color"], -c["count"], c["page"])),
    }
    return manifest, report


def aggregate(pages: List[Tuple[str, str]], service: str, migrated_at: str) -> Tuple[dict, dict]:
    """Строит (manifest, report) по списку (имя, raw_html).

    Карта строится ЗАНОВО для каждой страницы (ТЗ п. 4.2.д). Тонкая обёртка над
    accumulate_page/finalize — используется офлайн-режимом (--html-dir) и тестами.
    """
    acc = new_accumulator()
    for name, raw_html in pages:
        result = build_color_task_map(raw_html)
        accumulate_page(acc, name, result, survey_body_colors(raw_html, result))
    return finalize(acc, service, migrated_at)


def _safe_name(name: str) -> str:
    """Безопасное имя файла .md из имени страницы."""
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "page"


def _render_md(title: str, body: str) -> str:
    """Минимальный .md: frontmatter с заголовком + тело с маркерами CriticMarkup."""
    fm = f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\nsource: CONFLUENCE\n---\n\n"
    return fm + body


def migrate_pages(pages, service, migrated_at, out_dir):
    """Офлайн боевой проход (отладка): пишет минимальный .md с маркерами и собирает отчёт.

    Карта строится ОДИН раз на страницу и используется и для critic-экстракции, и для
    отчёта (через общий аккумулятор). Боевой прод-путь — migrate_confluence_tree.py --tasks
    (там .md пишется с полным frontmatter, картинками и резолвингом ссылок).
    """
    from app.color_map import build_color_task_map, survey_body_colors
    from app.content_extractor import create_critic_extractor
    from app.scripts.CI.critic import postpass_drop_contained_deletions

    out_dir.mkdir(parents=True, exist_ok=True)
    acc = new_accumulator()
    for name, raw_html in pages:
        result = build_color_task_map(raw_html)
        extractor = create_critic_extractor(result.color_to_task)
        body = extractor.extract(raw_html)
        body, dropped = postpass_drop_contained_deletions(body)  # ТЗ 4.5.4
        accumulate_page(acc, name, result, survey_body_colors(raw_html, result))
        for rec in extractor._critic_report:
            acc["nested"].append({"page": name, "tasks": rec["tasks"], "html": rec["html"][:500],
                                  "confidence": rec.get("confidence", "high")})
        for d in dropped:
            acc["nested"].append({"page": name, "tasks": [d["task"], d["insert_task"]],
                                  "html": d["text"][:500], "confidence": "post-pass"})
        (out_dir / (_safe_name(name) + ".md")).write_text(
            _render_md(name, body), encoding="utf-8")

    return finalize(acc, service, migrated_at)


def write_reports(out_dir: Path, manifest: dict, report: dict) -> None:
    """Записывает manifest.yaml + report.json + report.md в каталог (единый писатель)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "migration-manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (out_dir / "migration-colors-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "migration-colors-report.md").write_text(
        render_report_md(report), encoding="utf-8")


def render_report_md(report: dict) -> str:
    """Человекочитаемый markdown-отчёт из JSON (ТЗ п. 4.10)."""
    st = report["stats"]
    lines: List[str] = []
    lines.append(f"# Отчёт миграции цвета — сервис {report['service']}")
    lines.append("")
    lines.append(f"Дата: {report['migrated_at']}")
    lines.append("")
    lines.append("## Статистика")
    lines.append("")
    lines.append(f"- Страниц обработано: {st['pages_processed']}")
    lines.append(f"- Цветных фрагментов найдено: {st['colored_fragments_total']}")
    lines.append(f"- Позиций требует ручного разбора: {st['positions_manual_review']}")
    lines.append("")

    def _section(title: str, items: List[str]):
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        if items:
            lines.extend(items)
        else:
            lines.append("_нет_")
        lines.append("")

    _section("Страницы без секции «История изменений»",
             [f"- {p}" for p in report["pages_without_history"]])
    # Цвет всюду дублируется формой из HTML: аналитик ищет фрагмент на странице по ней.
    _section("Неразрешённые цвета (плейсхолдеры UNKNOWN-*)",
             [f"- `{u['placeholder']}` цвет {to_rgb_notation(u['color']) or ''} "
              f"({u['color']}) на «{u['page']}» ×{u['count']} ({u['reason']})"
              for u in report["unresolved_placeholders"]])
    _section("Коллизии «цвет → несколько задач»",
             [f"- {to_rgb_notation(c['color']) or ''} ({c['color']}) на «{c['page']}»: "
              f"выбран {c['chosen']} из {c['candidates']}" for c in report["collisions"]])
    _section("Ячейки «Задача в Jira» без извлекаемого id",
             [f"- {to_rgb_notation(j['color']) or ''} ({j['color']}) на «{j['page']}»"
              for j in report["jira_unextractable"]])
    nested = report.get("nested_flattened", [])
    _by_conf = lambda c: [f"- задачи {n['tasks']} на «{n['page']}»"
                          for n in nested if n.get("confidence") == c]
    _section("Уплощено: структурная вложенность (confidence: high)", _by_conf("high"))
    _section("Уплощено: примыкание к вставке (confidence: medium) — приоритет проверки",
             _by_conf("medium"))
    _section("Отброшено пост-проходом: текст входит в состав чужой вставки (п. 4.5.4)",
             _by_conf("post-pass"))

    lines.append("## Сводка цветов по страницам (диагностика набора black_colors)")
    lines.append("")
    lines.append("Классификация цвета указана ДЛЯ КАЖДОЙ страницы отдельно: один цвет на "
                 "разных страницах может быть task/unknown/black (постраничность, ТЗ 4.2.д).")
    lines.append("")
    lines.append("Колонка «Цвет в HTML» — форма записи, в которой цвет лежит на странице "
                 "Confluence: искать в исходнике нужно именно её, канонический `#rrggbb` "
                 "там не встречается.")
    lines.append("")
    lines.append("| Цвет в HTML | Цвет | Страница | Частота | Классификация | Задача "
                 "| ΔE до чёрного |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for c in report["color_summary"]:
        de = c.get("delta_e")
        rgb = to_rgb_notation(c["color"]) or ""
        lines.append(f"| {rgb} | {c['color']} | {c['page']} | {c['count']} | "
                     f"{c['classification']} | {c['task'] or ''} | "
                     f"{de if de is not None else ''} |")
    lines.append("")
    return "\n".join(lines)


def _read_html_dir(html_dir: Path) -> List[Tuple[str, str]]:
    """Офлайн-источник (отладка): страницы из сохранённых .html (имя = имя файла)."""
    return [(p.stem, p.read_text(encoding="utf-8"))
            for p in sorted(html_dir.glob("*.html"))]


def _collect_page_ids(root: str, use_http: bool) -> List[str]:
    """Идентификаторы поддерева, начиная с root (сам root + все потомки)."""
    if use_http:
        from app.page_cache import fetch_child_pages_via_http
        ids, seen, stack = [root], {root}, [root]
        while stack:
            for child in fetch_child_pages_via_http(stack.pop()):
                cid = child["id"]
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    stack.append(cid)
        return ids
    from app.confluence_loader import get_child_page_ids
    return [root] + get_child_page_ids(root)


def _pages_from_confluence(page_ids: List[str], use_http: bool) -> List[Tuple[str, str]]:
    """Читает страницы Confluence в память: (заголовок, raw_html). Диск не задействуется."""
    from app.page_cache import get_page_data
    pages = []
    for pid in page_ids:
        data = get_page_data(pid, use_http=use_http)
        if not data:
            print(f"ПРЕДУПРЕЖДЕНИЕ: страница {pid} не загружена — пропущена.", file=sys.stderr)
            continue
        raw = data.get("raw_html")
        if not raw:
            print(f"ПРЕДУПРЕЖДЕНИЕ: у страницы {pid} пустой raw_html — пропущена.",
                  file=sys.stderr)
            continue
        pages.append((data.get("title") or pid, raw))
    return pages


def _resolve_pages(args) -> Optional[List[Tuple[str, str]]]:
    """Возвращает список (имя, raw_html) по выбранному источнику или None при ошибке."""
    use_http = not args.api  # HTTP — канонический вход задачи; --api переключает на REST
    if args.html_dir:
        html_dir = Path(args.html_dir)
        if not html_dir.is_dir():
            print(f"ОШИБКА: каталог не найден: {html_dir}", file=sys.stderr)
            return None
        pages = _read_html_dir(html_dir)
    elif args.root:
        pages = _pages_from_confluence(_collect_page_ids(args.root, use_http), use_http)
    elif args.pages:
        ids = [p.strip() for p in args.pages.split(",") if p.strip()]
        pages = _pages_from_confluence(ids, use_http)
    else:
        print("ОШИБКА: укажите источник — --pages, --root или --html-dir.", file=sys.stderr)
        return None

    if not pages:
        print("ОШИБКА: не получено ни одной страницы.", file=sys.stderr)
        return None
    return pages


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_colors.py",
        description="Миграция цвета Confluence → CriticMarkup (ТЗ Модуль 1).")
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p):
        src = p.add_argument_group("источник страниц (один из)")
        src.add_argument("--pages", help="идентификаторы страниц Confluence через запятую")
        src.add_argument("--root", help="идентификатор корня — обход всего поддерева")
        src.add_argument("--html-dir", help="ОФЛАЙН-режим (отладка): каталог с .html")
        p.add_argument("--api", action="store_true",
                       help="читать через REST API вместо прямого HTTP (по умолчанию — HTTP)")
        p.add_argument("--out", default=".", help="каталог для вывода")
        p.add_argument("--service", default="", help="код сервиса для манифеста")
        p.add_argument("--date", default=date.today().isoformat(),
                       help="значение migrated_at (по умолчанию сегодня)")

    _add_common(sub.add_parser("report", help="отчёт и манифест без записи маркеров (ТЗ 4.3)"))
    _add_common(sub.add_parser("migrate", help="боевой проход: запись .md с маркерами + отчёт"))

    args = parser.parse_args(argv)

    pages = _resolve_pages(args)
    if pages is None:
        return 2

    out = Path(args.out)
    if args.command == "migrate":
        docs_dir = out / "docs"
        manifest, report = migrate_pages(pages, args.service, args.date, docs_dir)
    else:
        manifest, report = aggregate(pages, args.service, args.date)

    write_reports(out, manifest, report)

    st = report["stats"]
    print(f"Команда: {args.command}. Обработано страниц: {st['pages_processed']}; "
          f"цветных фрагментов: {st['colored_fragments_total']}; "
          f"ручной разбор: {st['positions_manual_review']} позиций"
          f" (в т.ч. уплощений: {st.get('nested_flattened', 0)}).")
    if args.command == "migrate":
        print(f"Markdown с маркерами записан в {out / 'docs'}")
    print(f"Отчёт и манифест записаны в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
