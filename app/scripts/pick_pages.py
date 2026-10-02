# app/scripts/pick_pages.py
#
# Точечная замена страниц архива страницами из свежей полной выгрузки (2026-10-02).
#
# Зачем. После исправлений экспортёра (1.9.1 — страничная заморозка, 1.9.2/1.9.3 —
# смешанное зачёркивание) часть страниц старой выгрузки нужно выгрузить заново.
# Выгрузка страницы поодиночке кладёт её в корень, вне структуры дерева, и её
# ссылки на соседей не разрешаются. Фильтр внутри экспорта потребовал бы правки
# обходчика дерева: путь страницы зависит от всей цепочки предков, а ссылки
# разрешаются по реестру сохранённых файлов. Проще и надёжнее: полная выгрузка
# run-tree во ВРЕМЕННЫЙ каталог, а эта утилита переносит из неё в архив только
# страницы с заданными идентификаторами — по тем же относительным путям, вместе
# с их картинками. Сеть не нужна, экспортёр не меняется.
#
# Что делает для каждого идентификатора из списка:
#   • находит страницу в новой выгрузке по `confluence_page_id` (frontmatter);
#   • копирует файл в архив по тому же относительному пути, байт в байт;
#   • если в архиве эта страница лежала по другому пути (переименована или
#     перенесена в Confluence) — старый файл удаляет, иначе в архиве оказались бы
#     две страницы с одним идентификатором;
#   • копирует локальные файлы, на которые страница ссылается (картинки `img/…`);
#   • проверяет, что md-ссылки новой страницы ведут на существующие файлы архива.
#
# Всё или ничего: сначала строится план по всем идентификаторам. Если хоть один не
# найден в новой выгрузке, целевой путь занят ДРУГОЙ страницей или корни каталогов
# не совмещены — не пишется ничего, код возврата 1. Молча пропущенная страница
# означала бы, что ошибочная версия осталась в архиве.
#
# Не трогает: служебные файлы `migration-*`, страницы не из списка, картинки, на
# которые новая страница больше не ссылается (ими могут пользоваться соседи).
#
# Ограничение: перенесённые страницы несут ТЕКУЩЕЕ содержание Confluence, а не
# состояние на дату исходной выгрузки. Новая выгрузка должна быть сделана той же
# версией экспортёра и с теми же ключами, что исходная.
#
# Использование:
#     python -m app.scripts.pick_pages <новая выгрузка> <архив> --pages ids.txt --dry-run
#     python -m app.scripts.pick_pages <новая выгрузка> <архив> --pages ids.txt
#     python -m app.scripts.pick_pages <новая выгрузка> <архив> --pages ids.txt --out pick.md
#
#   <новая выгрузка>, <архив> — каталоги ОДНОГО уровня дерева: относительный путь
#       страницы от каждого из них должен совпадать (обычно это каталог, в который
#       run-tree положил дерево, и `sources\raw` репозитория сервиса).
#   --pages   файл со списком идентификаторов страниц: по одному в строке либо
#             через пробел/запятую; строки с `#` — комментарии.
#   --dry-run показать план, ничего не записывая.
#   --out     записать отчёт в markdown-файл.
#
# Код возврата: 0 — выполнено (или план без ошибок при --dry-run); 1 — ошибки
# плана, ничего не записано; 2 — ошибка запуска.

import argparse
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from app.scripts.repair_export import split_frontmatter

_PAGE_ID_RE = re.compile(r"^confluence_page_id:\s*['\"]?(\d+)['\"]?\s*$", re.M)
_TITLE_RE = re.compile(r"^title:\s*(.*?)\s*$", re.M)
_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)
_SERVICE_PREFIX = "migration-"

REPLACED, ADDED, MOVED, SAME = "заменена", "добавлена", "перемещена", "без изменений"


class PickError(Exception):
    """Ошибка запуска: продолжать нельзя, план не строится."""


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


def index_tree(root: Path) -> Dict[str, dict]:
    """page_id → {rel: относительный путь (posix), title}. Дубль идентификатора
    в одном дереве — ошибка: неясно, какую из страниц считать настоящей."""
    index: Dict[str, dict] = {}
    for path in sorted(root.rglob("*.md")):
        if path.name.startswith(_SERVICE_PREFIX):
            continue
        parts = split_frontmatter(_read(path))
        if parts is None:
            continue
        m = _PAGE_ID_RE.search(parts[1])
        if not m:
            continue
        pid = m.group(1)
        rel = path.relative_to(root).as_posix()
        if pid in index:
            raise PickError(
                f"в каталоге {root} две страницы с идентификатором {pid}:\n"
                f"  {index[pid]['rel']}\n  {rel}\n"
                "Уберите лишнюю и повторите.")
        index[pid] = {"rel": rel, "title": _title(parts[1], path.stem)}
    return index


def read_ids(path: Path) -> List[str]:
    """Идентификаторы из файла: по одному в строке либо через пробел/запятую/точку
    с запятой; `#` начинает комментарий. Порядок сохраняется, повторы убираются."""
    ids: List[str] = []
    text = path.read_text(encoding="utf-8-sig")
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0]
        for token in re.split(r"[\s,;]+", line.strip()):
            if not token:
                continue
            if not token.isdigit():
                raise PickError(f"{path}, строка {n}: «{token}» — не идентификатор "
                                "страницы (ожидается число)")
            if token not in ids:
                ids.append(token)
    if not ids:
        raise PickError(f"{path}: список идентификаторов пуст")
    return ids


def link_targets(text: str) -> List[str]:
    """Цели markdown-ссылок `[текст](цель)`. Скобки в цели считаются с балансом:
    имена страниц содержат круглые скобки («…-(БлокировкаЗакрытие…).md»), и
    разбор «до первой закрывающей» обрезал бы путь."""
    out: List[str] = []
    i = 0
    while True:
        i = text.find("](", i)
        if i < 0:
            return out
        j = i + 2
        depth = 1
        while j < len(text) and depth:
            ch = text[j]
            if ch == "\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        if depth == 0:
            out.append(text[i + 2:j - 1])
        i += 2


def _local_target(raw: str) -> Optional[str]:
    """Локальный путь из цели ссылки либо None (внешняя ссылка, якорь, плейсхолдер)."""
    t = raw.strip()
    if t.startswith("<") and ">" in t:
        t = t[1:t.index(">")]
    else:
        m = re.match(r'^(.*?)\s+"[^"]*"$', t)      # [текст](цель "подсказка")
        if m:
            t = m.group(1)
    t = t.split("#", 1)[0].strip()
    # Схема (https:, confluence:), протокол-относительный адрес и путь от корня
    # сервера («/download/attachments/…» — вложение, оставшееся ссылкой на
    # Confluence) — не файлы выгрузки.
    if not t or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t) or t.startswith("/"):
        return None
    return t


def referenced_files(text: str) -> Tuple[List[str], List[str]]:
    """(md-цели, прочие локальные файлы) — пути относительно каталога страницы."""
    md: List[str] = []
    other: List[str] = []
    raw_targets = link_targets(text) + [m.group(2) for m in _IMG_SRC_RE.finditer(text)]
    for raw in raw_targets:
        t = _local_target(raw)
        if t is None:
            continue
        bucket = md if t.lower().endswith(".md") else other
        if t not in bucket:
            bucket.append(t)
    return md, other


def _resolve(page_rel: str, target: str) -> Optional[str]:
    """Относительный путь цели от корня дерева; None — цель выходит за корень."""
    parts: List[str] = []
    for part in (PurePosixPath(page_rel).parent / target.replace("\\", "/")).parts:
        if part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def build_plan(new_root: Path, archive_root: Path, ids: List[str]) -> dict:
    """План переноса без записи. Ключи: pages, errors, warnings, assets."""
    new_idx = index_tree(new_root)
    arc_idx = index_tree(archive_root)
    arc_by_rel = {v["rel"]: pid for pid, v in arc_idx.items()}

    # Совмещены ли корни: у страниц, общих для обоих деревьев, пути должны
    # совпадать хотя бы у части. Ни одного совпадения — каталоги разного уровня.
    common = [pid for pid in new_idx if pid in arc_idx]
    if common and not any(new_idx[p]["rel"] == arc_idx[p]["rel"] for p in common):
        sample = common[0]
        raise PickError(
            "каталоги не совмещены: у общих страниц нет ни одного совпадающего "
            "относительного пути. Пример:\n"
            f"  новая выгрузка: {new_idx[sample]['rel']}\n"
            f"  архив:          {arc_idx[sample]['rel']}\n"
            "Укажите каталоги одного уровня дерева.")

    plan = {"pages": [], "errors": [], "warnings": [], "assets": []}
    planned_rels = set()
    removed_rels = set()
    for pid in ids:
        new = new_idx.get(pid)
        if new is None:
            plan["errors"].append(
                f"{pid}: страницы нет в новой выгрузке (удалена, попала под правила "
                "исключения, осталась без собственного текста или идентификатор "
                "указан неверно)")
            continue
        rel = new["rel"]
        old = arc_idx.get(pid)
        occupant = arc_by_rel.get(rel)
        if occupant is not None and occupant != pid:
            plan["errors"].append(
                f"{pid} «{new['title']}»: целевой путь занят другой страницей "
                f"({occupant}): {rel}")
            continue
        src, dst = new_root / rel, archive_root / rel
        if old is None:
            status = ADDED
        elif old["rel"] != rel:
            status = MOVED
            removed_rels.add(old["rel"])
        elif dst.read_bytes() == src.read_bytes():
            status = SAME
        else:
            status = REPLACED
        plan["pages"].append({"id": pid, "title": new["title"], "rel": rel,
                              "old_rel": old["rel"] if old else None, "status": status})
        planned_rels.add(rel)

    # Сопутствующие файлы и проверка ссылок — по состоянию архива ПОСЛЕ переноса.
    assets = {}
    for page in plan["pages"]:
        md_targets, other_targets = referenced_files(_read(new_root / page["rel"]))
        # Ссылки, которые были в прежней версии страницы на том же месте, перенос
        # не меняет: если они уже вели в никуда (другой сервис вне архива, текст,
        # похожий на ссылку), это не новость — сообщаем только о новом.
        known: set = set()
        if page["old_rel"] == page["rel"]:
            old_md, old_other = referenced_files(_read(archive_root / page["rel"]))
            known = set(old_md) | set(old_other)
        md_targets = [t for t in md_targets if t not in known]
        for target in other_targets:
            res = _resolve(page["rel"], target)
            if res is not None and (new_root / res).is_file():
                assets[res] = True
                continue
            # В новой выгрузке файла нет. Если он уже лежит в архиве — остаётся
            # как был; если ссылка была и раньше — не новость.
            if (res is not None and (archive_root / res).is_file()) or target in known:
                continue
            plan["warnings"].append(
                f"{page['id']} «{page['title']}»: файл по ссылке не найден ни в "
                f"новой выгрузке, ни в архиве: {target}")
        for target in md_targets:
            res = _resolve(page["rel"], target)
            exists = (res is not None and res not in removed_rels
                      and (res in planned_rels or (archive_root / res).is_file()))
            if not exists:
                plan["warnings"].append(
                    f"{page['id']} «{page['title']}»: ссылка ведёт на файл, которого "
                    f"нет в архиве: {target}")
    # Перемещённая страница: старый путь удаляется, а страницы архива, которые
    # не перевыгружаются, продолжают на него ссылаться — их ссылки станут битыми.
    if removed_rels:
        for arc_pid, info in arc_idx.items():
            if info["rel"] in planned_rels or info["rel"] in removed_rels:
                continue
            md_targets, _other = referenced_files(_read(archive_root / info["rel"]))
            for target in md_targets:
                if _resolve(info["rel"], target) in removed_rels:
                    plan["warnings"].append(
                        f"{arc_pid} «{info['title']}» (не перевыгружается): ссылка на "
                        f"старый путь перемещённой страницы станет битой: {target}")
    for res in sorted(assets):
        src, dst = new_root / res, archive_root / res
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            continue
        plan["assets"].append({"rel": res, "status": REPLACED if dst.is_file() else ADDED})
    return plan


def apply_plan(new_root: Path, archive_root: Path, plan: dict) -> None:
    """Выполнить план: копирование байт в байт, удаление старого пути у перемещённых."""
    for item in plan["pages"]:
        if item["status"] == SAME:
            continue
        dst = archive_root / item["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(new_root / item["rel"], dst)
        if item["status"] == MOVED:
            old = archive_root / item["old_rel"]
            if old.is_file():
                old.unlink()
    for item in plan["assets"]:
        dst = archive_root / item["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(new_root / item["rel"], dst)


def render_report(plan: dict, new_root: Path, archive_root: Path, dry_run: bool,
                  written: bool) -> str:
    counts = {s: sum(1 for p in plan["pages"] if p["status"] == s)
              for s in (REPLACED, ADDED, MOVED, SAME)}
    if plan["errors"]:
        state = "ОШИБКИ ПЛАНА — ничего не записано"
    elif dry_run:
        state = "пробный прогон — ничего не записано"
    else:
        state = "выполнено" if written else "ничего не записано"
    lines = ["# Перенос страниц из новой выгрузки в архив", "",
             f"Новая выгрузка: `{new_root}`", f"Архив: `{archive_root}`",
             f"Состояние: **{state}**.", "",
             "| Итог | Страниц |", "|---|---|"]
    lines += [f"| {s} | {counts[s]} |" for s in (REPLACED, ADDED, MOVED, SAME)]
    lines += [f"| сопутствующих файлов (картинки) | {len(plan['assets'])} |",
              f"| ошибок | {len(plan['errors'])} |",
              f"| предупреждений | {len(plan['warnings'])} |", ""]

    def _section(title: str, items: List[str]) -> None:
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        lines.extend(items or ["_нет_"])
        lines.append("")

    _section("Ошибки — перенос остановлен", [f"- {e}" for e in plan["errors"]])
    _section("Предупреждения — проверить руками", [f"- {w}" for w in plan["warnings"]])
    rows = []
    for p in plan["pages"]:
        tail = f" (было: `{p['old_rel']}`)" if p["status"] == MOVED else ""
        rows.append(f"- {p['status']}: {p['id']} «{p['title']}» — `{p['rel']}`{tail}")
    _section("Страницы", rows)
    _section("Сопутствующие файлы",
             [f"- {a['status']}: `{a['rel']}`" for a in plan["assets"]])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    from app.version import banner
    print(banner("pick"))

    parser = argparse.ArgumentParser(
        prog="pick_pages",
        description="Перенос выбранных страниц из новой полной выгрузки в архив "
                    "по тем же относительным путям. Всё или ничего.")
    parser.add_argument("new_root", help="каталог новой полной выгрузки")
    parser.add_argument("archive_root", help="каталог архива (того же уровня дерева)")
    parser.add_argument("--pages", required=True, metavar="FILE",
                        help="файл со списком идентификаторов страниц")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать план, ничего не записывая")
    parser.add_argument("--out", metavar="FILE", help="записать отчёт в markdown-файл")
    args = parser.parse_args(argv)

    new_root, archive_root = Path(args.new_root), Path(args.archive_root)
    for label, root in (("новая выгрузка", new_root), ("архив", archive_root)):
        if not root.is_dir():
            print(f"ОШИБКА: каталог не найден ({label}): {root}", file=sys.stderr)
            return 2
    if new_root.resolve() == archive_root.resolve():
        print("ОШИБКА: новая выгрузка и архив — один и тот же каталог.", file=sys.stderr)
        return 2
    try:
        ids = read_ids(Path(args.pages))
        plan = build_plan(new_root, archive_root, ids)
    except OSError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    except PickError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    written = False
    if not plan["errors"] and not args.dry_run:
        apply_plan(new_root, archive_root, plan)
        written = True

    report = render_report(plan, new_root, archive_root, args.dry_run, written)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
        print(f"отчёт: {args.out}")

    counts = {s: sum(1 for p in plan["pages"] if p["status"] == s)
              for s in (REPLACED, ADDED, MOVED, SAME)}
    for e in plan["errors"]:
        print(f"  ✗ {e}")
    for w in plan["warnings"]:
        print(f"  ⚠ {w}")
    mode = ("ОШИБКИ — ничего не записано" if plan["errors"]
            else "пробный прогон" if args.dry_run else "выполнено")
    print(f"[pick] {mode}; в списке: {len(ids)}; "
          + "; ".join(f"{s}: {counts[s]}" for s in (REPLACED, ADDED, MOVED, SAME))
          + f"; картинок: {len(plan['assets'])}; ошибок: {len(plan['errors'])}; "
          f"предупреждений: {len(plan['warnings'])}")
    return 1 if plan["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
