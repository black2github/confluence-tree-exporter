# app/scripts/CI/normalize_tables.py
#
# Нормализатор сырых HTML-таблиц в markdown-файлах (Д-21, проход 1
# двухпроходного разбора source-parsing).
#
# Архитектура — три слоя по критерию «понимание — LLM, массовое
# переписывание — скрипт»:
#   1. СЕТКА (этот скрипт): раскрытие rowspan/colspan в полную матрицу с
#      протяжкой объединённых значений — чистая грамматика HTML.
#   2. РОЛИ КОЛОНОК (вне скрипта): профиль таблицы — какие колонки образуют
#      иерархию пути, какие переносятся как есть. Профиль пишет LLM/аналитик
#      один раз на макет (--sample выдаёт шапку и строки-образцы как вход
#      для разметки); знакомый макет узнаётся без LLM.
#   3. СБОРКА (этот скрипт): плоская markdown-таблица по профилю, склейка
#      путей, счётный инвариант «строк в результате = строк сетки».
#
# Утилита класса critic: работает по локальным .md многократно, исходный
# файл НЕ изменяет (режимы: stdout / sidecar). Confluence не нужен —
# сырой HTML уже сохранён консервативной миграцией (Д-21).
#
# Формат профиля (JSON):
#   {
#     "name": "integration-params",
#     "header_rows": 1,
#     "hierarchy_cols": [0, 1, 2],       # колонки, образующие путь (по порядку)
#     "path_title": "XML-элемент",       # заголовок колонки пути
#     "path_join": "/",
#     "keep_cols": [[3, "Тип"], [4, "Обяз."]]   # [индекс, целевой заголовок]
#   }
# Без профиля — passthrough: сетка выводится как есть (уже ценно: снята
# нерегулярность rowspan/colspan).

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag


# ---------- слой 1: сетка ----------

def cell_text(cell: Tag) -> str:
    """Текст ячейки для плоской таблицы: ссылки — markdown, переносы — <br>,
    вертикальная черта экранируется (не ломать pipe-таблицу)."""
    parts: List[str] = []

    def walk(node) -> None:
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            parts.append("\n")
            return
        if node.name == "a":
            text = node.get_text(" ", strip=True)
            href = node.get("href", "")
            parts.append(f"[{text}]({href})" if href else text)
            return
        if node.name in ("p", "li", "div", "tr"):
            for ch in node.children:
                walk(ch)
            parts.append("\n")
            return
        for ch in node.children:
            walk(ch)

    for ch in cell.children:
        walk(ch)

    text = "".join(parts).replace(" ", " ")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "<br>".join(lines).replace("|", "\\|")


def expand_grid(table: Tag) -> List[List[str]]:
    """Раскрывает таблицу в полную матрицу: rowspan/colspan разворачиваются,
    значение объединённой ячейки протягивается на все накрытые позиции."""
    grid: List[List[Optional[str]]] = []
    # занятость будущих строк: col -> (оставшийся rowspan, значение)
    carry: Dict[int, List] = {}

    for tr in table.find_all("tr"):
        row: List[Optional[str]] = []
        col = 0

        def fill_carry() -> None:
            nonlocal col
            while col in carry:
                remaining, value = carry[col]
                row.append(value)
                remaining -= 1
                if remaining:
                    carry[col][0] = remaining
                else:
                    del carry[col]
                col += 1

        fill_carry()
        for cell in tr.find_all(["td", "th"], recursive=False):
            fill_carry()
            value = cell_text(cell)
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)
            for _ in range(colspan):
                row.append(value)
                if rowspan > 1:
                    carry[col] = [rowspan - 1, value]
                col += 1
        fill_carry()
        grid.append(row)

    width = max((len(r) for r in grid), default=0)
    return [[("" if v is None else v) for v in r] + [""] * (width - len(r))
            for r in grid]


def find_top_tables(md_text: str) -> List[Tag]:
    """Верхнеуровневые сырые HTML-таблицы файла (вложенные обрабатываются
    в составе родительской ячейки текстом; их структура — отдельной таблицей
    при необходимости, через --table по индексу)."""
    soup = BeautifulSoup(md_text, "html.parser")
    return [t for t in soup.find_all("table") if not t.find_parent("table")]


# ---------- слой 2: профиль ----------

@dataclass
class Profile:
    name: str = "passthrough"
    header_rows: int = 1
    hierarchy_cols: List[int] = field(default_factory=list)
    path_title: str = "Путь"
    path_join: str = "/"
    keep_cols: List[Tuple[int, str]] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Profile":
        data = json.loads(path.read_text(encoding="utf-8"))
        return Profile(
            name=data.get("name", path.stem),
            header_rows=int(data.get("header_rows", 1)),
            hierarchy_cols=list(data.get("hierarchy_cols", [])),
            path_title=data.get("path_title", "Путь"),
            path_join=data.get("path_join", "/"),
            keep_cols=[(int(i), str(t)) for i, t in data.get("keep_cols", [])],
        )


# ---------- слой 3: сборка ----------

def build_flat(grid: List[List[str]], profile: Profile) -> Tuple[List[str], List[List[str]]]:
    """Собирает плоскую таблицу по профилю. Возвращает (заголовки, строки).
    Инвариант нулевых потерь: len(строк) == len(grid) - header_rows —
    проверяется вызывающим кодом (assert_invariant)."""
    data_rows = grid[profile.header_rows:]

    if not profile.hierarchy_cols and not profile.keep_cols:
        headers = grid[0] if grid else []
        return list(headers), [list(r) for r in data_rows]

    headers = [profile.path_title] + [t for _, t in profile.keep_cols]
    out: List[List[str]] = []
    for row in data_rows:
        path_parts: List[str] = []
        for idx in profile.hierarchy_cols:
            value = row[idx] if idx < len(row) else ""
            # протяжка даёт повтор родителя в каждой строке; дубли подряд не клеим
            if value and (not path_parts or path_parts[-1] != value):
                path_parts.append(value)
        path = profile.path_join.join(path_parts)
        out.append([path] + [(row[i] if i < len(row) else "") for i, _ in profile.keep_cols])
    return headers, out


def assert_invariant(grid: List[List[str]], profile: Profile,
                     rows: List[List[str]]) -> str:
    expected = len(grid) - profile.header_rows
    got = len(rows)
    status = "✓" if expected == got else "✗ ПОТЕРЯ СТРОК"
    return (f"инвариант строк: сетка {expected} → выход {got} {status}; "
            f"колонок сетки: {len(grid[0]) if grid else 0}")


def render_markdown(headers: List[str], rows: List[List[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def render_sample(grid: List[List[str]], header_rows: int = 1, n: int = 3) -> str:
    """Вход для LLM-разметки ролей: пронумерованные колонки шапки +
    первые строки сетки. Маленький выход — читается целиком."""
    lines = ["Колонки сетки (для разметки ролей — какие образуют иерархию пути):"]
    for r in grid[:header_rows]:
        lines.append("  шапка: " + " ; ".join(f"[{i}] {v}" for i, v in enumerate(r)))
    for j, r in enumerate(grid[header_rows:header_rows + n]):
        lines.append(f"  строка {j + 1}: " + " ; ".join(f"[{i}] {v[:40]}" for i, v in enumerate(r)))
    return "\n".join(lines)


# ---------- CLI ----------

def normalize_file(md_path: Path, profile: Optional[Profile],
                   table_index: Optional[int]) -> Tuple[str, List[str]]:
    """Возвращает (markdown-выход, отчёт-строки)."""
    text = md_path.read_text(encoding="utf-8")
    tables = find_top_tables(text)
    report: List[str] = [f"файл: {md_path.name}; верхнеуровневых таблиц: {len(tables)}"]
    chunks: List[str] = []
    prof = profile or Profile()

    for i, t in enumerate(tables):
        if table_index is not None and i != table_index:
            continue
        grid = expand_grid(t)
        headers, rows = build_flat(grid, prof)
        inv = assert_invariant(grid, prof, rows)
        report.append(f"таблица {i}: {inv}")
        chunks.append(f"## Таблица {i} (профиль: {prof.name})\n\n"
                      + render_markdown(headers, rows))
    return "\n\n".join(chunks), report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Нормализатор сырых HTML-таблиц в .md (проход 1, Д-21). "
                    "Исходный файл не изменяется.")
    ap.add_argument("file", type=Path, help="markdown-файл с сырыми HTML-таблицами")
    ap.add_argument("--table", type=int, default=None, help="индекс таблицы (по умолчанию все)")
    ap.add_argument("--profile", type=Path, default=None, help="JSON-профиль ролей колонок")
    ap.add_argument("--sample", action="store_true",
                    help="выдать шапку и строки-образцы для LLM-разметки ролей")
    ap.add_argument("--sidecar", action="store_true",
                    help="записать результат в <файл>.tables.md рядом (иначе stdout)")
    args = ap.parse_args()

    profile = Profile.load(args.profile) if args.profile else None

    if args.sample:
        text = args.file.read_text(encoding="utf-8")
        tables = find_top_tables(text)
        for i, t in enumerate(tables):
            if args.table is not None and i != args.table:
                continue
            grid = expand_grid(t)
            print(f"--- таблица {i} ({len(grid)} строк сетки) ---")
            print(render_sample(grid))
        return 0

    out, report = normalize_file(args.file, profile, args.table)
    for line in report:
        print(f"# {line}", file=sys.stderr)
    if args.sidecar:
        target = args.file.with_suffix(args.file.suffix + ".tables.md")
        target.write_text(out + "\n", encoding="utf-8")
        print(f"# записано: {target}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
