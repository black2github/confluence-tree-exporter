# app/content_extractor.py

import logging
import re
from typing import Dict, List, Optional
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from dataclasses import dataclass, field
from app.utils.style_utils import (
    is_black_color, has_colored_style, normalize_color, is_ignored_color, is_near_black,
)

logger = logging.getLogger(__name__)

# Блочные элементы: пересечение их границы разрывает «примыкание» (ТЗ п. 4.5.2, условие 3).
# Без этого ограничителя эвристика срабатывает на фрагментах из разных абзацев/ячеек,
# между которыми нет содержательной связи — самый опасный класс ошибок (отброшенный ПРОМ).
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "ul", "ol", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "section", "article",
    "header", "footer", "aside", "figure", "figcaption", "dl", "dt", "dd", "hr", "form",
})

# Zero-width символы: невидимы, примыкание не разрывают (ТЗ п. 4.5.2, условие 1).
_ZERO_WIDTH_CHARS = ("​", "﻿", "‌", "‍")


def _normalize_gap_text(text: str) -> str:
    """Нормализация текстового узла в промежутке между фрагментами (ТЗ п. 4.5.2, условие 1).

    Схлопывает пробельные последовательности, приводит `&nbsp;` (\\u00a0) к обычному пробелу,
    выбрасывает zero-width символы, обрезает края. Пустой результат = узел текста не несёт
    и примыкание не разрывает. Сравнение сырых строк здесь давало бы ложное несрабатывание
    признака всегда: между фрагментами почти всегда есть служебная разметка и пробелы.
    """
    s = text.replace(" ", " ")
    for zw in _ZERO_WIDTH_CHARS:
        s = s.replace(zw, "")
    return re.sub(r"\s+", " ", s).strip()


# Паттерны для извлечения page_id из URL Confluence
_CONFLUENCE_PAGE_ID_RE = re.compile(
    r'(?:[?&]pageId=|/pages/viewpage\.action\?pageId=|/wiki/spaces/[^/]+/pages/)(\d+)'
)


def _extract_page_id_from_href(href: str) -> Optional[str]:
    """Извлекает числовой page_id из URL Confluence различных форматов."""
    m = _CONFLUENCE_PAGE_ID_RE.search(href)
    return m.group(1) if m else None


def _escape_link_text(text: str) -> str:
    """Экранирует квадратные скобки в тексте Markdown-ссылки.

    Без экранирования '[' и ']' внутри текста ссылки ломают Markdown-парсер,
    который воспринимает первый ']' как конец текста ссылки.
    """
    return text.replace("[", "\\[").replace("]", "\\]")


def _escape_html_text(text: str) -> str:
    """Экранирует спецсимволы для текстового содержимого HTML-тега."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_html_attr(value: str) -> str:
    """Экранирует значение HTML-атрибута (кавычки и амперсанд)."""
    return value.replace("&", "&amp;").replace('"', "&quot;")


_TAG_OPENER_RE = re.compile(r'<(?=[A-Za-z/!])')


def _escape_stray_tag_openers(text: str) -> str:
    """Экранирует в текстовом узле только те '<', которые HTML-парсер принял бы
    за начало тега: '<' + латинская буква / '/' / '!'.

    BeautifulSoup при разборе уже декодировал сущности (&lt;S&gt; → <S>), и если
    вернуть такой текст в сырую HTML-ячейку как есть, рендер увидит живой тег:
    <S> — незакрытое зачёркивание всего последующего текста, <XS>/<L>/<M>/<XL>
    санитайзер вырезает — токен атрибута тихо исчезает из отображения.

    Точечность намеренная: '<' перед кириллицей, цифрой или пробелом по HTML5 —
    обычный текст, поэтому токены вида <Номер заявки> остаются байт-в-байт как
    прежде (грепаемость и идемпотентность повторного экспорта).
    """
    return _TAG_OPENER_RE.sub("&lt;", text)


_MARGIN_LEFT_RE = re.compile(r'margin-left\s*:\s*([^;]+)')


def _extract_margin_left(style: str) -> Optional[str]:
    """Возвращает значение margin-left из inline-стиля, либо None.

    Confluence использует margin-left (напр. 40px) для визуального отступа
    абзацев-описаний под заголовками шагов. При рендеринге ячейки как HTML
    этот отступ нужно сохранить, иначе структура «заголовок / описание»
    схлопывается.
    """
    m = _MARGIN_LEFT_RE.search(style or "")
    return m.group(1).strip() if m else None


@dataclass
class ExtractionConfig:
    """Конфигурация/настройки для извлечения контента"""
    include_colored: bool = True  # True - все фрагменты, False - только подтвержденные
    preserve_whitespace: bool = True  # Сохранять пробелы
    normalize_spacing: bool = False  # Отключаем агрессивную нормализацию
    clean_brackets: bool = True
    format_tables: bool = True
    format_lists: bool = True
    format_headers: bool = True
    migrate_images: bool = False  # сохранять <ac:image> как HTML <img> с плейсхолдером вложения
    exclude_strikethrough: bool = False  # True - зачёркнутый <s> выкидывается при любом раскладе (в т.ч. под include_colored)
    # Модуль 1, срез 1b: режим эмиссии CriticMarkup. Цветные прогоны оборачиваются в маркеры
    # по карте color_map (нормализованный #rrggbb -> TASK-ID). Неизвестные цвета -> UNKNOWN-<hex>.
    # critic_mode подразумевает include_colored=True (ничего не выбрасываем, всё оборачиваем).
    critic_mode: bool = False
    color_map: Dict[str, str] = field(default_factory=dict)
    critic_status_column: str = "status"  # имя служебного столбца markdown-таблиц (ТЗ п. 4.6)


class ContentExtractor:
    """
    Исправленный экстрактор контента с правильной обработкой порядка заголовков таблиц.
    """

    def __init__(self, config: ExtractionConfig):
        self.config = config
        # Стек активных задач для режима CriticMarkup (срез 1b): вершина — задача текущего
        # цветного региона, чтобы не оборачивать один и тот же цвет повторно.
        self._critic_stack: List[str] = []
        # Подавление inline-обёртки: включается при обработке цельно-цветной строки таблицы,
        # где правка отмечается на уровне строки (status / <tr class>), а не внутри ячеек.
        self._critic_suppress: bool = False
        # Отчёт об автоматически уплощённых вложенных конструкциях (ТЗ п. 4.5): каждая запись —
        # {tasks: [...], html: исходный_фрагмент}. Требует ручной проверки аналитиком.
        self._critic_report: List[dict] = []

    def extract(self, html: str) -> str:
        """Главная точка входа с отладкой HTML"""
        if not html or not html.strip():
            return ""

        from app.history_cleaner import remove_history_sections
        html = remove_history_sections(html)

        self._critic_stack = []  # сброс на каждый вызов (переиспользуемый экстрактор)
        self._critic_suppress = False
        self._critic_report = []

        soup = BeautifulSoup(html, "html.parser")

        self._process_confluence_macros(soup)
        self._remove_empty_paragraphs(soup)

        result_parts = self._process_container(soup)
        result = self._join_parts_preserving_structure(result_parts)

        if self.config.normalize_spacing:
            result = self._apply_minimal_cleanup(result)

        return result

    def _table_needs_html(self, element: Tag) -> bool:
        """
        Определяет, нужно ли рендерить таблицу как HTML вместо Markdown pipe-синтаксиса.

        Markdown pipe-таблица не поддерживает:
        - colspan / rowspan — объединение ячеек
        - блочные элементы внутри ячеек (списки ul/ol, вложенные таблицы, параграфы p)
          так как перевод строки внутри ячейки завершает строку таблицы

        В любом из этих случаев переключаемся на HTML.

        ИСКЛЮЧЕНИЕ: ячейка из нескольких ПРОСТЫХ параграфов (только инлайн-контент:
        текст, ссылки, strong/em) не требует HTML — такие абзацы рендерятся в
        Markdown-ячейке через разделитель <br> (см. _cell_is_simple_multiline /
        _render_simple_multiline_cell). HTML нужен лишь когда внутри есть реально
        блочное содержимое (списки, вложенные таблицы) или объединение ячеек.
        """
        for cell in element.find_all(["td", "th"]):
            # Объединение ячеек
            if int(cell.get("colspan", 1) or 1) > 1:
                return True
            if int(cell.get("rowspan", 1) or 1) > 1:
                return True
            # Блочные элементы внутри ячейки — список или вложенная таблица
            if cell.find(["ul", "ol"]):
                return True
            # Несколько параграфов уводят в HTML только если ячейка НЕ является
            # набором простых абзацев (последние представимы в Markdown через <br>).
            paragraphs = cell.find_all("p", recursive=False)
            if len(paragraphs) > 1 and not self._cell_is_simple_multiline(cell):
                return True
        return False

    def _cell_is_simple_multiline(self, cell: Tag) -> bool:
        """Определяет, можно ли ячейку из нескольких <p> отрендерить в Markdown
        через разделитель <br>, не прибегая к HTML-таблице.

        Условие «простоты»:
        - в ячейке более одного прямого <p>;
        - внутри ячейки нет блочных элементов (вложенных таблиц, списков ul/ol);
        - на верхнем уровне ячейки нет значимого контента вне <p>
          (голого текста или непараграфных тегов с текстом) — иначе при рендеринге
          только по <p> мы бы потеряли часть содержимого, поэтому такую ячейку
          оставляем на HTML-путь.

        colspan/rowspan здесь не проверяются — они отсекаются раньше в
        _table_needs_html и переводят ячейку на HTML независимо от параграфов.
        """
        paragraphs = cell.find_all("p", recursive=False)
        if len(paragraphs) <= 1:
            return False

        # Любое реально блочное содержимое → нельзя в Markdown.
        if cell.find(["table", "ul", "ol"]):
            return False

        # Значимый контент вне прямых <p> → не наш случай (во избежание потери).
        for child in cell.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    return False
            elif isinstance(child, Tag) and child.name != "p":
                if child.get_text(strip=True):
                    return False

        return True

    def _render_simple_multiline_cell(self, cell: Tag, context: str = "table_cell") -> str:
        """Рендерит «простую многоабзацную» ячейку (см. _cell_is_simple_multiline)
        для Markdown-таблицы: каждый абзац обрабатывается обычным конвейером,
        внутренние переводы строк схлопываются в пробел, а абзацы соединяются
        через <br>. Пустые абзацы (визуальные разделители) отбрасываются.

        <br> вставляется здесь, ДО общей нормализации _normalize_cell_text,
        которая работает только с символами перевода строки и теги <br> не трогает.
        Поэтому эффект локален: остальные ячейки таблицы не затрагиваются.
        """
        parts = []
        for p in cell.find_all("p", recursive=False):
            text = self._process_element(p, context) or ""
            # Переводы строк внутри абзаца (например от <br>) → пробел.
            text = re.sub(r"\s*\n\s*", " ", text)
            text = re.sub(r" {2,}", " ", text).strip()
            if text:
                parts.append(text)
        return "<br>".join(parts)

    def _render_cell_for_html_table(self, cell: Tag) -> str:
        """
        Рендерит содержимое ячейки для HTML-таблицы.

        Использует _process_nested_table_cell_content, который сохраняет
        структуру списков и вложенных таблиц в HTML-разметке.
        _normalize_cell_text здесь не применяется — в HTML переводы строк
        внутри <td> не ломают таблицу.

        Контекст "nested_table_cell" сигнализирует _process_bold
        что нужно использовать <strong> вместо **...**
        """
        if not self._should_include_element(cell):
            if not self.config.include_colored:
                return self._extract_black_elements_from_colored_container(cell, "nested_table_cell") or ""
            return ""
        return self._process_nested_table_cell_content(cell)

    def _process_top_level_table_to_html(self, element: Tag) -> str:
        """
        Конвертирует таблицу верхнего уровня в HTML.
        Используется когда таблица не может быть представлена
        Markdown pipe-синтаксисом: содержит colspan/rowspan,
        списки или многострочное содержимое ячеек.
        Содержимое ячеек обрабатывается через _render_cell_for_html_table,
        который сохраняет структуру списков и вложенных таблиц.
        """
        html_parts = ["<table>"]

        # Обрабатываем thead если есть
        thead = element.find("thead")
        if thead:
            html_parts.append("<thead>")
            for row in thead.find_all("tr", recursive=False):
                html_parts.extend(self._html_row_parts(row))
            html_parts.append("</thead>")

        # Обрабатываем tbody если есть; если нет — берём tr прямо из таблицы
        tbody = element.find("tbody")
        rows_source = tbody if tbody else element
        if tbody:
            html_parts.append("<tbody>")
        for row in rows_source.find_all("tr", recursive=False):
            html_parts.extend(self._html_row_parts(row))
        if tbody:
            html_parts.append("</tbody>")

        html_parts.append("</table>")
        return "\n".join(html_parts)

    def _html_row_parts(self, row: Tag) -> List[str]:
        """Рендерит строку HTML-таблицы. В режиме CriticMarkup цельно-цветная строка
        помечается на уровне <tr class="critic-row-ins|critic-row-del" data-task="ID">
        (ТЗ п. 4.7), а её ячейки рендерятся без inline-обёртки (правка уже отмечена строкой).
        Вне critic_mode вывод побайтово совпадает с прежним."""
        tr_attrs = ""
        suppress = False
        if self.config.critic_mode:
            rc = self._row_uniform_critic(row)
            if rc is not None:
                task, kind = rc
                cls = "critic-row-ins" if kind == "ins" else "critic-row-del"
                tr_attrs = f' class="{cls}" data-task="{task}"'
                suppress = True

        parts = [f"<tr{tr_attrs}>"]
        prev = self._critic_suppress
        if suppress:
            self._critic_suppress = True
        try:
            for cell in row.find_all(["td", "th"], recursive=False):
                tag = "th" if cell.name == "th" else "td"
                attrs = self._build_span_attrs(cell)
                cell_text = self._render_cell_for_html_table(cell)
                parts.append(f"<{tag}{attrs}>{cell_text}</{tag}>")
        finally:
            self._critic_suppress = prev
        parts.append("</tr>")
        return parts

    def _build_span_attrs(self, cell: Tag) -> str:
        """Возвращает строку HTML-атрибутов colspan/rowspan для ячейки."""
        attrs = []
        colspan = int(cell.get("colspan", 1) or 1)
        rowspan = int(cell.get("rowspan", 1) or 1)
        if colspan > 1:
            attrs.append(f'colspan="{colspan}"')
        if rowspan > 1:
            attrs.append(f'rowspan="{rowspan}"')
        return (" " + " ".join(attrs)) if attrs else ""

    def _process_table(self, element: Tag, context: str) -> str:
        """
        Обработка таблиц.
        - Вложенные таблицы (context=table_cell) -> HTML всегда.
        - Таблицы с colspan/rowspan -> HTML (Markdown не поддерживает объединение).
        - Простые таблицы -> Markdown pipe-синтаксис.
        """
        if not self.config.format_tables:
            return self._process_text_container(element, context)

        # Если таблица находится внутри ячейки другой таблицы,
        # конвертируем её в HTML вместо Markdown
        if context in ["table_cell", "nested_table_cell"]:
            return self._process_nested_table_to_html(element)

        # Если в таблице есть объединение ячеек — Markdown не справится,
        # переключаемся на HTML-рендеринг
        if self._table_needs_html(element):
            return self._process_top_level_table_to_html(element)

        # Для обычного контекста - создаём Markdown таблицу
        # Собираем строки: (тип, row_data, status). status непуст только для цельно-цветных
        # строк в режиме CriticMarkup (целая добавленная/удалённая строка, ТЗ п. 4.6).
        table_rows = []

        # 1. Обрабатываем ВСЕ строки из thead как заголовки
        # независимо от того, используют они <th> или <td>
        thead = element.find("thead")
        if thead:
            header_rows = thead.find_all("tr", recursive=False)
            for row in header_rows:
                cells = row.find_all(["td", "th"], recursive=False)
                if cells:
                    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Все строки из thead обрабатываем как заголовки
                    row_data = self._process_table_row_cells(cells, context, is_header=True)
                    if row_data:
                        table_rows.append(("header", row_data, ""))

        # 2. ЗАТЕМ обрабатываем тело таблицы из tbody
        tbody = element.find("tbody")
        if tbody:
            body_rows = tbody.find_all("tr", recursive=False)
            for row in body_rows:
                cells = row.find_all(["td", "th"], recursive=False)
                if cells:
                    row_data, status = self._critic_markdown_row(row, cells, context)
                    if row_data:
                        table_rows.append(("body", row_data, status))

        # 3. Если нет явных thead/tbody, берем все tr напрямую
        if not table_rows:
            direct_rows = element.find_all("tr", recursive=False)
            for i, row in enumerate(direct_rows):
                cells = row.find_all(["td", "th"], recursive=False)
                if cells:
                    # Первая строка считается заголовком, если все ячейки - th
                    is_header = (i == 0 and all(cell.name == "th" for cell in cells))
                    if is_header:
                        row_data = self._process_table_row_cells(cells, context, is_header=True)
                        status = ""
                    else:
                        row_data, status = self._critic_markdown_row(row, cells, context)
                    if row_data:
                        row_type = "header" if is_header else "body"
                        table_rows.append((row_type, row_data, status))

        if not table_rows:
            return ""

        # Вычисляем максимальное логическое число колонок по всем строкам,
        # раскрывая colspan: ячейка с colspan=N занимает N логических колонок.
        max_cols = 0
        for _row_type, row_data, _status in table_rows:
            col_count = sum(span for _text, span in row_data)
            if col_count > max_cols:
                max_cols = col_count

        # Служебный столбец status добавляется, только если есть хоть одна размеченная строка
        # (ТЗ п. 4.6: «если в таблице нет ни одной размеченной строки — столбец не добавлять»).
        add_status = self.config.critic_mode and any(st for _rt, _rd, st in table_rows)
        status_name = self.config.critic_status_column
        total_cols = max_cols + (1 if add_status else 0)

        # Имя служебного столбца пишется в строку-ШАПКУ, а «шапкой» до 2026-09-06
        # считался только ряд из <thead>. В выгрузке Confluence заголовки лежат
        # внутри <tbody>, поэтому первая строка тела повышалась до шапки markdown
        # уже со СВОЕЙ (пустой) служебной ячейкой — столбец оставался безымянным,
        # critic такую таблицу не опознавал и пропускал целиком: ни apply, ни
        # reject до строк не добирались, разметка ±ID оставалась в ПРОМ-срезе
        # (на дереве [КК] так уцелело 10 ячеек на 6 страницах).
        header_from_body = add_status and not any(rt == "header" for rt, _rd, _st in table_rows)

        # Формируем таблицу
        table_lines = []
        has_separator = False
        header_labeled = False

        for row_type, row_data, status in table_rows:
            # Нормализуем содержимое и раскрываем colspan:
            # ячейка с colspan=N превращается в N pipe-колонок,
            # где первая содержит текст, остальные пусты.
            pipe_cells: List[str] = []
            for raw_text, span in row_data:
                text = self._normalize_cell_text(raw_text)
                pipe_cells.append(text)
                # Дополнительные пустые колонки для colspan > 1
                for _ in range(span - 1):
                    pipe_cells.append("")

            # Дополняем строку до max_cols если она короче
            while len(pipe_cells) < max_cols:
                pipe_cells.append("")

            # Служебный столбец status добавляется последним (ТЗ п. 4.6).
            if add_status:
                if row_type == "header":
                    pipe_cells.append(status_name if not header_labeled else "")
                else:
                    pipe_cells.append(status)

            # Пропускаем строки, где все ячейки пусты
            if all(c == "" for c in pipe_cells):
                continue

            row_line = "| " + " | ".join(pipe_cells) + " |"
            separator_line = "|" + "|".join([" --- " for _ in range(total_cols)]) + "|"

            if row_type == "header":
                header_labeled = True
                table_lines.append(row_line)
                if not has_separator:
                    table_lines.append(separator_line)
                    has_separator = True
            elif row_type == "body":
                if not has_separator:
                    # Таблица без thead — первая строка становится заголовком
                    if header_from_body and not status:
                        # Шапке нужно имя служебного столбца, иначе critic таблицу
                        # не опознает. Своей разметки у этой строки нет — ячейку
                        # можно занять именем.
                        pipe_cells[-1] = status_name
                        row_line = "| " + " | ".join(pipe_cells) + " |"
                    elif header_from_body and status:
                        # Первая строка сама размечена: её маркер терять нельзя,
                        # поэтому шапку добавляем отдельной строкой, а строку
                        # оставляем телом таблицы.
                        head_cells = [""] * (total_cols - 1) + [status_name]
                        table_lines.append("| " + " | ".join(head_cells) + " |")
                        table_lines.append(separator_line)
                        has_separator = True
                        table_lines.append(row_line)
                        continue
                    table_lines.append(row_line)
                    table_lines.append(separator_line)
                    has_separator = True
                else:
                    table_lines.append(row_line)

        if not table_lines:
            return ""

        return "\n".join(table_lines)

    def _critic_markdown_row(self, row: Tag, cells: List[Tag], context: str):
        """Рендерит строку тела markdown-таблицы и возвращает (row_data, status).

        Цельно-цветная строка (вся под одной задачей) отмечается служебным столбцом status
        значением +TASK (добавлена) / -TASK (удалена); её ячейки рендерятся без inline-обёртки
        — правка отмечена на уровне строки (ТЗ п. 4.6). Обычные строки → status="", а правки
        внутри ячеек размечаются inline через общий механизм.
        """
        status = ""
        suppress = False
        if self.config.critic_mode:
            rc = self._row_uniform_critic(row)
            if rc is not None:
                task, kind = rc
                status = ("+" if kind == "ins" else "-") + task
                suppress = True

        prev = self._critic_suppress
        if suppress:
            self._critic_suppress = True
        try:
            row_data = self._process_table_row_cells(cells, context, is_header=False)
        finally:
            self._critic_suppress = prev
        return row_data, status

    def _process_table_row_cells(self, cells: List[Tag], context: str, is_header: bool = False) -> List[tuple]:
        """
        Обработка ячеек строки таблицы.

        Возвращает список пар (текст_ячейки, colspan), где colspan >= 1.
        Вызывающий код раскрывает colspan в нужное число pipe-колонок.
        """
        row_data = []

        for cell in cells:
            colspan = max(1, int(cell.get("colspan", 1) or 1))

            if not self._should_include_element(cell):
                if not self.config.include_colored:
                    black_content = self._extract_black_elements_from_colored_container(cell, context)
                    cell_text = black_content if black_content else ""
                else:
                    continue
            elif self._cell_is_simple_multiline(cell):
                # Несколько простых абзацев → Markdown с разделителем <br>.
                cell_text = self._render_simple_multiline_cell(cell, "table_cell")
            else:
                cell_text = self._process_table_cell(cell, "table_cell")
                if cell_text is None:
                    cell_text = ""

            row_data.append((cell_text, colspan))

        return row_data

    def _format_table_cell_content(self, content: str, cell: Tag) -> str:
        """
        НОВЫЙ МЕТОД: Форматирование содержимого ячейки с HTML атрибутами
        """
        if not content:
            content = ""

        # Добавляем HTML атрибуты для объединенных ячеек
        html_attrs = []
        if cell.get("rowspan") and int(cell.get("rowspan", 1)) > 1:
            html_attrs.append(f'rowspan="{cell["rowspan"]}"')
        if cell.get("colspan") and int(cell.get("colspan", 1)) > 1:
            html_attrs.append(f'colspan="{cell["colspan"]}"')

        if html_attrs:
            attrs_str = " ".join(html_attrs)
            return f'<td {attrs_str}>{content}</td>' if content else f'<td {attrs_str}></td>'
        else:
            return content

    # Контексты, где страница отдаётся сырым HTML: внутри ячейки таблицы
    # markdown-ограждение не рендерится как код нигде, а для конвейера хуже —
    # содержимое между ``` считается кодом и переносится байт-в-байт, поэтому
    # apply/reject не видят маркеры внутри, и неутверждённое молча остаётся в
    # «чистом ПРОМ» (инцидент 2026-09-06: 204 фрагмента в 8 файлах дерева [КК];
    # уже выгруженные деревья лечит repair_export --unfence-html).
    # В таких контекстах отдаём <pre> — валидный HTML с тем же смыслом.
    _HTML_CONTEXTS = ("table_cell", "nested_table_cell")

    def _code_block(self, code_text: str, context: str) -> str:
        """Блок кода в форме, уместной для текущего контекста."""
        if context in self._HTML_CONTEXTS:
            return f"<pre>{code_text}</pre>"
        return f"\n```\n{code_text}\n```\n"

    def _process_code_block(self, element: Tag, context: str) -> str:
        """
        Обработка блоков кода.

        Поддерживаемые источники:
        - <ac:structured-macro ac:name="code"> — Confluence code macro
        - <ac:structured-macro ac:name="noformat"> — Confluence noformat macro
        - <pre> — HTML preformatted block
        - <code> — HTML inline/block code

        Многострочный текст оборачивается в тройные обратные кавычки.
        Однострочный <code> оборачивается в одиночные обратные кавычки.
        """
        name = element.name

        # Confluence макросы: code и noformat
        if name == "ac:structured-macro":
            plain_body = element.find("ac:plain-text-body")
            if plain_body:
                # BeautifulSoup преобразует CDATA в текст автоматически
                code_text = plain_body.get_text()
            else:
                # Fallback: извлекаем весь текст макроса
                code_text = element.get_text()

            code_text = code_text.strip()
            if not code_text:
                return ""
            return self._code_block(code_text, context)

        # <pre> — всегда многострочный блок
        if name == "pre":
            code_text = element.get_text()
            code_text = code_text.strip()
            if not code_text:
                return ""
            return self._code_block(code_text, context)

        # <code> — inline если однострочный, блок если многострочный
        if name == "code":
            code_text = element.get_text()
            # Однострочный inline code
            if "\n" not in code_text.strip():
                return f"`{code_text.strip()}`"
            # Многострочный
            return self._code_block(code_text.strip(), context)

        return ""

    def _process_bold(self, element: Tag, context: str) -> str:
        """
        Обработка тегов <strong> и <b>.

        В HTML-контексте (table_cell, nested_table_cell) использует <strong>...</strong>,
        потому что Markdown-разметка **...** внутри HTML-тегов не обрабатывается
        рендерерами согласно спецификации CommonMark.

        В Markdown-контексте (default и прочие) использует **...** — стандартный bold.

        Пробелы по краям выносятся за маркеры, чтобы не нарушать синтаксис Markdown:
        корректно: ' **текст** ', некорректно: '** текст **'
        """
        content = self._process_children(element, context)
        stripped = content.strip()
        if not stripped:
            return content  # Только пробелы — возвращаем как есть, без маркеров

        if context == "nested_table_cell":
            # HTML-контекст: используем тег <strong>, так как **...** внутри
            # HTML-тегов не обрабатывается рендерерами (CommonMark)
            leading = content[: len(content) - len(content.lstrip())]
            trailing = content[len(content.rstrip()):]
            return f"{leading}<strong>{stripped}</strong>{trailing}"

        # Markdown-контекст: используем ** маркеры
        leading = content[: len(content) - len(content.lstrip())]
        trailing = content[len(content.rstrip()):]
        return f"{leading}**{stripped}**{trailing}"

    def _process_time(self, element: Tag) -> str:
        """Преобразует элемент <time> в текстовое представление даты/времени.

        Приоритет — атрибут datetime (машинный ISO-формат, напр. '2025-11-06'),
        так как Confluence хранит дату именно там, а тело тега часто пустое
        (<time datetime="2025-11-06" />). Если datetime отсутствует — берём
        видимый текст элемента.

        Обрабатывается во ВСЕХ конвейерах (обычный текст, ячейки HTML-таблиц,
        режим без цветовой фильтрации), чтобы дата не терялась нигде — в
        частности в таблицах "История изменений".
        """
        dt = element.get("datetime")
        if dt and dt.strip():
            return dt.strip()
        return element.get_text(strip=True)

    def _process_image(self, element: Tag) -> str:
        """Преобразует картинку (<ac:image> или <img>) в HTML-тег <img>.

        Поведение зависит от флага config.migrate_images:
        • False (по умолчанию) — картинка выкидывается (пустая строка), как и раньше.
        • True — формируется HTML-тег <img> с сохранением размеров, чтобы соблюсти
          масштабирование. Размеры берутся из ac:width/ac:height (storage) либо
          width/height / data-width/data-height (рендеренный HTML).

        Источник картинки и режим доступа:
        • storage-формат (REST API), тег <ac:image>:
            – <ri:attachment ri:filename="..."> — вложение: в src пишется плейсхолдер
              confluence-attachment://<filename>, который слой миграции разрешает через
              REST (скачивает вложение в img/ и подставляет относительную ссылку).
            – <ri:url ri:value="..."> — внешняя картинка по URL: ссылку оставляем как есть.
        • рендеренный HTML (прямой HTTP, закрытый контур), тег <img>: см.
          _classify_rendered_image — встроенные вложения уходят в плейсхолдер
          confluence-download://<path>, внешние URL остаются, служебная графика
          выкидывается.

        Реальные пути на этом этапе неизвестны (нет ни page_id, ни пути .md), поэтому
        используются плейсхолдеры — тот же приём, что и для ссылок (confluence://).

        HTML-тег используется намеренно (а не Markdown ![]()): только он позволяет
        задать width/height и переживает вставку внутрь сырых HTML-таблиц.
        """
        if not self.config.migrate_images:
            return ""

        # Размеры: storage — ac:width/ac:height; рендеренный HTML — width/height или data-*
        width = element.get("ac:width") or element.get("width") or element.get("data-width")
        height = element.get("ac:height") or element.get("height") or element.get("data-height")
        alt = (element.get("ac:alt") or element.get("alt")
               or element.get("data-linked-resource-default-alias") or "")

        if element.name == "ac:image":
            attachment = element.find("ri:attachment")
            if attachment and attachment.get("ri:filename"):
                src = f"confluence-attachment://{attachment.get('ri:filename')}"
                alt = alt or attachment.get("ri:filename")
            else:
                url_el = element.find("ri:url")
                src = url_el.get("ri:value") if (url_el and url_el.get("ri:value")) else None
        else:  # рендеренный <img>
            src = self._classify_rendered_image(element)

        if not src:
            return ""

        attrs = [f'src="{_escape_html_attr(src)}"']
        if width:
            attrs.append(f'width="{_escape_html_attr(str(width))}"')
        if height:
            attrs.append(f'height="{_escape_html_attr(str(height))}"')
        attrs.append(f'alt="{_escape_html_text(alt)}"')
        return f'<img {" ".join(attrs)}>'

    def _classify_rendered_image(self, element: Tag) -> Optional[str]:
        """Классифицирует <img> из рендеренного HTML Confluence (HTTP-режим).

        • Встроенная картинка-вложение (class confluence-embedded-image, либо атрибут
          data-image-src, либо путь /download/attachments/...) → плейсхолдер
          confluence-download://<path>. Путь сохраняется целиком, включая query
          (?version=...&modificationDate=...), который часто нужен для скачивания.
          Слой миграции скачает картинку браузерным запросом и положит в img/.
        • Внешняя картинка по абсолютному URL → ссылку оставляем как есть.
        • Прочие <img> (иконки, эмотиконы, аватары, /download/resources/...) → None
          (выкидываются), чтобы не засорять вывод служебной графикой.
        """
        raw = element.get("data-image-src") or element.get("src")
        if not raw:
            return None

        classes = element.get("class") or []
        if isinstance(classes, str):
            classes = classes.split()
        is_embedded = (
            "confluence-embedded-image" in classes
            or element.has_attr("data-image-src")
            or "/download/attachments/" in raw
        )
        if not is_embedded:
            return None

        if "/download/attachments/" in raw:
            return f"confluence-download://{raw}"
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw  # внешняя картинка по URL
        return None

    # Остальные методы остаются без изменений (копируем из предыдущей версии)
    def _normalize_cell_text(self, text: str) -> str:
        """
        Нормализует текст ячейки для корректного Markdown-синтаксиса таблицы.

        Markdown-таблица требует, чтобы одна строка таблицы занимала ровно одну
        физическую строку. Перевод строки внутри ячейки завершает таблицу.

        Выполняет:
        - Заменяет переводы строк на пробел
        - Схлопывает несколько пробелов в один
        - Убирает ведущие/завершающие пробелы
        - Экранирует pipe-символ внутри ячейки, чтобы не сломать разметку таблицы
        """
        if not text:
            return ""

        # Схлопываем последовательные переводы строк и пробелы вокруг них
        result = re.sub(r'\s*\n\s*', " ", text)
        # Схлопываем множественные пробелы
        result = re.sub(r" {2,}", " ", result)
        # Экранируем | внутри ячейки
        result = result.replace("|", r"\|")
        return result.strip()

    def _process_element(self, element, context: str = "default") -> Optional[str]:
        """Обёртка диспетчера: в режиме CriticMarkup оборачивает цветной регион в маркер.

        Обёртка — снаружи существующей логики (_dispatch_element), сам обход не меняется.
        Срез 1b-1: оборачиваем только верхнеуровневый цветной регион (стек пуст); вложенные
        цвета (регион в регионе) — отдельный срез 1b-2, здесь маркеры НЕ вкладываются друг
        в друга (это запрещено линтером).
        """
        if (self.config.critic_mode and isinstance(element, Tag)
                and not self._critic_stack and not self._critic_suppress
                and not self._is_ignored_element(element)):
            marker = self._critic_marker_for(element)
            if marker is not None:
                task, kind = marker
                # Вложенность цветов (ТЗ п. 4.5): если внутри есть фрагмент ДРУГОГО цвета —
                # уплощаем в один маркер под внутренней задачей, а не вкладываем маркеры.
                outer_norm = normalize_color(self._element_own_color(element) or "")
                # ПЕРВЫМ делом — виден ли собственный цвет элемента хоть на одном
                # текстовом прогоне (CSS: внутренний цвет перекрывает внешний).
                # Спан-обёртка, весь текст которой перекрашен изнутри в чёрный, —
                # это ПРОМ-текст: маркер на нём привёл бы к его удалению при
                # reject/reject-all (инцидент 2026-08-06, «Получить выписку в
                # КНОСИС»: {++UNKNOWN-ff6600: <Комментарий для Банка> =++} поверх
                # чёрного требования). Проверка обязана стоять ДО уплощения:
                # раньше уплощение перехватывало такие конструкции.
                if not self._element_effectively_colored(element, outer_norm):
                    return self._dispatch_element(element, context)
                if self._has_nested_diff_color(element, outer_norm):
                    return self._flatten_nested_critic(element)
                # Признак 2 (ТЗ п. 4.5.2, confidence: medium): зачёркнутый фрагмент вплотную
                # к вставке другой задачи, без чёрного текста между — на ПРОМ, вероятно, не был
                # (черновик), поэтому отбрасывается. Проверяется ПОСЛЕ признака 1 (тот даёт
                # прямой структурный сигнал) и только для целиком зачёркнутого фрагмента.
                if kind == "del" and self._is_fully_struck(element):
                    foreign = self._adjacent_foreign_insertion(element, outer_norm)
                    if foreign is not None:
                        self._critic_report.append(
                            {"tasks": [task, foreign], "html": str(element),
                             "confidence": "medium"})
                        return ""
                # Внутренний цвет перекрывает внешний (ТЗ, CSS): если ВЕСЬ текст элемента
                # перекрашен изнутри (напр. внешний цветной спан с чёрным текстом внутри) —
                # не оборачиваем целиком, спускаемся внутрь (иначе маркер на ПРОМ-тексте).
                if not self._element_effectively_colored(element, outer_norm):
                    return self._dispatch_element(element, context)
                self._critic_stack.append(task)
                try:
                    inner = self._dispatch_element(element, context)
                finally:
                    self._critic_stack.pop()
                return self._wrap_critic(task, kind, inner)
        return self._dispatch_element(element, context)

    def _element_effectively_colored(self, element: Tag, own_norm: Optional[str]) -> bool:
        """True, если хотя бы у одного текстового прогона внутри element ЭФФЕКТИВНЫЙ
        (ближайший) цвет совпадает с собственным цветом element (own_norm).

        Если весь текст перекрашен вложенными элементами (напр. внешний цветной спан, а
        текст внутри — под чёрным спаном), возвращает False: оборачивать в маркер нечего,
        на экране это чёрный (ПРОМ) текст.
        """
        if not own_norm:
            return False
        for text_node in element.find_all(string=True):
            if not str(text_node).strip():
                continue
            cur = text_node.parent
            eff = None
            while cur is not None and isinstance(cur, Tag):
                own = self._element_own_color(cur)
                if own:
                    eff = own
                    break
                if cur is element:
                    break
                cur = cur.parent
            if eff is not None and normalize_color(eff) == own_norm:
                return True
        return False

    def _element_own_color(self, element: Tag) -> Optional[str]:
        """Собственный цвет элемента (style color: или <font color>), без учёта предков."""
        style = (element.get("style") or "").lower()
        m = re.search(r"color\s*:\s*([^;]+)", style)
        if m:
            return m.group(1).strip()
        if element.name == "font" and element.get("color"):
            return element.get("color").strip()
        return None

    def _is_strikethrough(self, element: Tag) -> bool:
        """Признак зачёркивания: сам тег <s>/<del>/<strike>, line-through в стиле или вложенный <s>."""
        if element.name in ("s", "del", "strike"):
            return True
        if "line-through" in (element.get("style") or "").lower():
            return True
        return element.find(["s", "del", "strike"]) is not None

    def _strike_belongs_to_task(self, element: Tag) -> bool:
        """Зачёркнутое относится к правке ЗАДАЧИ (цветное) — выкидывать нельзя:
        фрагмент станет {--ID: …--} и им управляет critic (apply/reject).

        Цвет ищется у самого элемента, у предков (типовой паттерн: цветной span
        снаружи, <s> внутри) и у потомков (обратный паттерн: <s> снаружи,
        цветной span внутри). Ограничитель удаляющей эвристики (Д-22):
        сомнение трактуется в пользу сохранения."""
        cur = element
        while isinstance(cur, Tag):
            if self._is_edit_color(self._element_own_color(cur)):
                return True
            cur = cur.parent
        for child in element.find_all(True):
            if self._is_edit_color(self._element_own_color(child)):
                return True
        return False

    def _is_edit_color(self, color: Optional[str]) -> bool:
        """Является ли цвет РЕАЛЬНОЙ правкой требования (порядок проверок ТЗ п. 4.3.2).

        Не правка (→ False): нет цвета; точный чёрный; UI-цвет (ignore-список); либо цвет
        перцептивно неотличим от чёрного (near-black) И НЕ сопоставлен задаче в истории.
        Правка (→ True): цвет из карты истории (даже тёмный — это маркер задачи, шаг 3 ПЕРЕД
        шагом 4), либо не-чёрный цвет далеко от чёрного.
        """
        if not color or is_black_color(color) or is_ignored_color(color):
            return False
        norm = normalize_color(color)
        if not norm:
            return False
        if norm in self.config.color_map:   # шаг 3: цвет-маркер задачи — раньше ΔE
            return True
        return not is_near_black(color)      # шаг 4: near-black, не в истории → чёрный

    def _critic_marker_for(self, element: Tag):
        """Возвращает (task_id, kind) для элемента-правки либо None.

        kind: 'del' для зачёркнутого (удаляемого) фрагмента, иначе 'ins'. Неизвестный
        не-чёрный цвет даёт плейсхолдер UNKNOWN-<hex> (ТЗ п. 4.2.ж). Чёрный / near-black /
        UI-цвет / бесцветный маркера не порождают (см. _is_edit_color).
        """
        color = self._element_own_color(element)
        if not self._is_edit_color(color):
            return None
        norm = normalize_color(color)
        # Цвет обязан быть ВИДИМЫМ (CSS: внутренний перекрывает внешний): спан-обёртка,
        # весь текст которой перекрашен изнутри в чёрный, — это ПРОМ-текст, а не правка.
        # Без этой проверки маркер-вставка ложится на утверждённое требование и
        # reject/reject-all удаляет его молча (инцидент 2026-08-06, «Получить выписку
        # в КНОСИС»). Проверка здесь, в общем узле, — её проходят оба пути разметки:
        # обычный обход и HTML-острова таблиц.
        if not self._element_effectively_colored(element, norm):
            return None
        task = self.config.color_map.get(norm) or ("UNKNOWN-" + norm.lstrip("#"))
        kind = "del" if self._is_strikethrough(element) else "ins"
        return task, kind

    def _wrap_critic(self, task: str, kind: str, inner: Optional[str]) -> str:
        """Оборачивает отрендеренный фрагмент в маркер CriticMarkup (ТЗ п. 4.4).

        Пробелы фрагмента сохраняются внутри маркера (значимы: после отбрасывания правки
        не должно остаться двойного пробела). Пустой фрагмент не оборачивается.
        """
        inner = inner or ""
        if not inner.strip():
            return inner
        open_tok, close_tok = ("{++", "++}") if kind == "ins" else ("{--", "--}")

        # Блочные переводы строк на КРАЯХ (границы абзацев/заголовков) выносим наружу маркера,
        # чтобы он не пересекал границу блока (иначе ошибка линтера E5 и битый HTML в
        # pymdownx.critic). Одиночные пробелы на краях значимы (ТЗ п. 4.4) и остаются внутри.
        lead = ""
        mlead = re.match(r"^\s+", inner)
        if mlead and "\n" in mlead.group(0):
            lead = mlead.group(0)
            inner = inner[len(lead):]
        trail = ""
        mtrail = re.search(r"\s+$", inner)
        if mtrail and "\n" in mtrail.group(0):
            trail = mtrail.group(0)
            inner = inner[: len(inner) - len(trail)]

        if not inner:
            return lead + trail

        # Внутренние блочные разрывы (пустые строки) бьют фрагмент на блоки, каждый из которых
        # оборачивается в ОТДЕЛЬНЫЙ маркер: один маркер не должен пересекать границу блока
        # (ТЗ п. 4.5 / ограничение pymdownx.critic). Так «вставка целого раздела» становится
        # набором маркеров по одному на заголовок/абзац.
        parts = re.split(r"(\n[ \t]*\n)", inner)
        out = [lead]
        for part in parts:
            if not part or re.fullmatch(r"\n[ \t]*\n", part) or not part.strip():
                out.append(part)
                continue
            sep = "" if part[:1] == " " else " "
            out.append(f"{open_tok}{task}:{sep}{part}{close_tok}")
        out.append(trail)
        return "".join(out)

    def _critic_colored_descendants(self, element: Tag):
        """Список (normalized_color, depth) для не-чёрных цветных потомков element (и его самого)."""
        found = []
        for tag in [element] + element.find_all(True):
            color = self._element_own_color(tag)
            if self._is_edit_color(color):
                norm = normalize_color(color)
                if norm:
                    found.append((norm, len(list(tag.parents))))
        return found

    def _has_nested_diff_color(self, element: Tag, outer_norm: Optional[str]) -> bool:
        """Есть ли внутри element потомок-правка с ДРУГИМ (не outer) ВИДИМЫМ цветом.

        Учитываются только цвета, эффективные хотя бы для одного текстового прогона:
        спан-обёртка, чей цвет целиком перекрыт изнутри, вложенности не создаёт
        (иначе уплощение затянуло бы под маркер чёрный ПРОМ-текст).
        """
        for tag in element.find_all(True):
            color = self._element_own_color(tag)
            if self._is_edit_color(color):
                norm = normalize_color(color)
                if norm and norm != outer_norm and self._element_effectively_colored(tag, norm):
                    return True
        return False

    def _text_node_style(self, text_node) -> tuple:
        """(эффективный цвет, зачёркнутость) текстового узла — подъём по всем предкам.

        Цвет — ближайший заданный вверх по дереву (внутренний перекрывает внешний, как в CSS);
        зачёркнутость — истинна, если хоть один предок зачёркивает.
        """
        color = None
        struck = False
        cur = text_node.parent
        while cur is not None and isinstance(cur, Tag):
            if color is None:
                own = self._element_own_color(cur)
                if own:
                    color = own
            if (cur.name in ("s", "del", "strike")
                    or "line-through" in (cur.get("style") or "").lower()):
                struck = True
            cur = cur.parent
        return color, struck

    def _nearest_block_ancestor(self, node) -> Optional[Tag]:
        """Ближайший блочный предок узла (ТЗ п. 4.5.2, условие 3). Отсчёт всегда от родителя:
        нас интересует блок, В КОТОРОМ лежит фрагмент, а не сам фрагмент."""
        cur = node.parent
        while cur is not None and isinstance(cur, Tag):
            if cur.name in _BLOCK_TAGS:
                return cur
            cur = cur.parent
        return None

    def _block_tag_between(self, first, last) -> bool:
        """Есть ли блочный тег строго между двумя узлами в порядке обхода документа.

        Ловит случай, который не ловится сравнением блочных предков: пустой заголовок между
        двумя инлайновыми фрагментами одного `<div>` (ТЗ п. 4.5.2, условие 3 — «любой
        заголовок между ними разрывает примыкание»). Предки и потомки границей не считаются.
        """
        skip = {id(x) for x in first.parents} | {id(x) for x in last.parents}
        skip.add(id(first))
        if isinstance(first, Tag):
            skip |= {id(d) for d in first.descendants}
        for node in first.next_elements:
            if node is last:
                return False
            if (isinstance(node, Tag) and id(node) not in skip
                    and node.name in _BLOCK_TAGS):
                return True
        return False

    def _is_fully_struck(self, element: Tag) -> bool:
        """Весь ли видимый текст элемента зачёркнут.

        Ограничитель признака 2: отбрасывать целиком можно только фрагмент, в котором нет
        незачёркнутого текста. Иначе `_is_strikethrough` (истинный и для элемента, лишь
        СОДЕРЖАЩЕГО `<s>`) привёл бы к молчаливой потере соседнего незачёркнутого текста.
        """
        seen = False
        for text_node in element.find_all(string=True):
            if isinstance(text_node, Comment) or not _normalize_gap_text(str(text_node)):
                continue
            seen = True
            _color, struck = self._text_node_style(text_node)
            if not struck:
                return False
        return seen

    def _adjacent_foreign_insertion(self, element: Tag, own_norm: Optional[str]) -> Optional[str]:
        """ТЗ п. 4.5.2, признак 2 (`confidence: medium`): примыкает ли зачёркнутый фрагмент
        вплотную к вставке ДРУГОЙ задачи. Возвращает id этой задачи либо None.

        Обход — по текстовым узлам в порядке документа в обе стороны (при перекраске
        зачёркнутого фрагмента в цвет поздней задачи структурная вложенность разрушается,
        поэтому направление заранее неизвестно). Пустые после нормализации узлы прозрачны;
        первый непустой решает исход:

        * чужая незачёркнутая правка → примыкание установлено (при выполнении условия 3);
        * чёрный текст ПРОМ, свой цвет или зачёркнутое → разрыв (условие 2).

        Приоритет при неопределённости — сохранение текста (ТЗ п. 4.5.2): любое сомнение
        трактуется как отсутствие примыкания, фрагмент остаётся удалением `{--ID: …--}`.
        """
        own_block = self._nearest_block_ancestor(element)
        inside = {id(element)} | {id(d) for d in element.descendants}
        for stream, forward in ((element.next_elements, True),
                                (element.previous_elements, False)):
            for node in stream:
                if id(node) in inside or isinstance(node, Tag):
                    continue
                if not isinstance(node, NavigableString) or isinstance(node, Comment):
                    continue
                if not _normalize_gap_text(str(node)):
                    continue                      # условие 1: узел текста не несёт — прозрачен
                color, struck = self._text_node_style(node)
                if struck or not self._is_edit_color(color):
                    break                         # условие 2: чёрный ПРОМ-текст разрывает
                norm = normalize_color(color)
                if not norm or norm == own_norm:
                    break                         # свой цвет — это не «чужая вставка»
                first, last = (element, node) if forward else (node, element)
                if (self._nearest_block_ancestor(node) is not own_block
                        or self._block_tag_between(first, last)):
                    break                         # условие 3: пересечена граница блока
                return (self.config.color_map.get(norm)
                        or ("UNKNOWN-" + norm.lstrip("#")))
        return None

    def _collect_flattened_text(self, element: Tag) -> str:
        """Текст element для уплощения (ТЗ п. 4.5.3, шаг 2).

        Отбрасывается только зачёркнутый ЦВЕТНОЙ текст (чужой/черновой — на ПРОМ не был).
        Зачёркнутый ЧЁРНЫЙ текст (реальное удаление с ПРОМ) НЕ теряется — сохраняется в
        выводе: молчаливая потеря требования ПРОМ недопустима (приоритет — сохранение текста).
        Пробелы схлопываются — конструкция инлайновая.
        """
        parts = []
        for text_node in element.find_all(string=True):
            cur = text_node.parent
            struck = False
            color = None
            while cur is not None and isinstance(cur, Tag):
                if color is None:
                    own = self._element_own_color(cur)
                    if own:
                        color = own
                if (cur.name in ("s", "del", "strike")
                        or "line-through" in (cur.get("style") or "").lower()):
                    struck = True
                if cur is element:
                    break
                cur = cur.parent
            # Зачёркнутый ЦВЕТНОЙ (правка) черновик другой задачи → отбросить; зачёркнутый
            # чёрный/near-black (реальное удаление с ПРОМ) — сохранить (не терять требование).
            if struck and self._is_edit_color(color):
                continue
            parts.append(str(text_node))
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def _flatten_nested_critic(self, element: Tag) -> str:
        """Уплощает вложенную конструкцию цветов в ОДИН маркер (ТЗ п. 4.5).

        Зачёркнутый черновик отбрасывается; выживший текст оборачивается целиком под
        идентификатор ВНУТРЕННЕЙ (самой глубокой = более поздней) задачи. Конструкция
        записывается в отчёт как черновая — требует ручной проверки аналитиком.
        """
        colored = self._critic_colored_descendants(element)
        # Самый глубокий цвет = внутренняя (поздняя) задача.
        deepest_norm = max(colored, key=lambda c: c[1])[0] if colored else None
        tasks = []
        for norm, _depth in sorted(colored, key=lambda c: c[1]):
            t = self.config.color_map.get(norm) or ("UNKNOWN-" + norm.lstrip("#"))
            if t not in tasks:
                tasks.append(t)
        inner_task = (self.config.color_map.get(deepest_norm)
                      or ("UNKNOWN-" + deepest_norm.lstrip("#"))) if deepest_norm else None

        # confidence=high: сработал признак структурной вложенности (ТЗ п. 4.5.2, признак 1).
        self._critic_report.append(
            {"tasks": tasks, "html": str(element), "confidence": "high"})

        surviving = self._collect_flattened_text(element)
        if not surviving or inner_task is None:
            return ""
        # Всегда вставка: выживший текст — это новое состояние под поздней задачей.
        return f"{{++{inner_task}: {surviving}++}}"

    def _cell_uniform_critic(self, cell: Tag):
        """Возвращает (task, kind), если ВЕСЬ видимый текст ячейки окрашен одной задачей.

        Возвращает None, если в ячейке есть чёрный/бесцветный текст или несколько цветов —
        такая ячейка не является целостной правкой уровня строки (правки внутри неё
        размечаются inline). Используется для определения целых добавленных/удалённых строк
        таблицы (ТЗ п. 4.6/4.7).
        """
        colors = set()
        has_plain = False
        struck = False
        for text_node in cell.find_all(string=True):
            if not str(text_node).strip():
                continue
            color = None
            strike = False
            cur = text_node.parent
            while cur is not None and isinstance(cur, Tag):
                own = self._element_own_color(cur)
                if own and color is None:
                    color = own
                if (cur.name in ("s", "del", "strike")
                        or "line-through" in (cur.get("style") or "").lower()):
                    strike = True
                if cur is cell:
                    break
                cur = cur.parent
            if not self._is_edit_color(color):
                has_plain = True   # чёрный/near-black/UI/бесцветный — не правка
            else:
                colors.add(normalize_color(color))
                if strike:
                    struck = True
        if has_plain or len(colors) != 1:
            return None
        norm = colors.pop()
        if not norm:
            return None
        task = self.config.color_map.get(norm) or ("UNKNOWN-" + norm.lstrip("#"))
        return task, ("del" if struck else "ins")

    def _row_uniform_critic(self, row: Tag):
        """Возвращает (task, kind), если ВСЕ непустые ячейки строки — одна целостная правка."""
        found = None
        for cell in row.find_all(["td", "th"], recursive=False):
            if not cell.get_text(strip=True):
                continue
            cc = self._cell_uniform_critic(cell)
            if cc is None:
                return None
            if found is None:
                found = cc
            elif cc != found:
                return None
        return found

    def _dispatch_element(self, element, context: str = "default") -> Optional[str]:
        """Универсальная рекурсивная обработка элемента"""
        if isinstance(element, NavigableString):
            return self._process_text_node(str(element), context)

        if not isinstance(element, Tag):
            return None

        # ИСПРАВЛЕНО: Проверяем игнорируемые элементы ПЕРВЫМИ (до цветовой фильтрации)
        if self._is_ignored_element(element):
            return None

        # ДОБАВЛЕНО: Обработка <br> тегов
        if element.name == "br":
            return "\n"

        # Проверяем, должен ли элемент быть включен (цветовая фильтрация)
        if not self._should_include_element(element):
            if not self.config.include_colored:
                return self._extract_black_elements_from_colored_container(element, context)
            return None

        # Блоки кода: <pre>, <code>, Confluence code/noformat макросы
        if element.name in ["pre", "code"]:
            return self._process_code_block(element, context)
        if (element.name == "ac:structured-macro" and
                element.get("ac:name") in ("code", "noformat")):
            return self._process_code_block(element, context)

        # Заголовки
        if element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            return self._process_header(element, context)

        # Таблицы
        if element.name == "table":
            return self._process_table(element, context)

        # Списки
        if element.name in ["ul", "ol"]:
            return self._process_list(element, context)

        # Ссылки
        if element.name in ["a", "ac:link"]:
            return self._process_link(element, context)

        # Жирный текст
        if element.name in ["strong", "b"]:
            return self._process_bold(element, context)

        # Время
        if element.name == "time":
            return self._process_time(element)

        # Картинки (вложения и внешние) — под флагом migrate_images
        if element.name in ["ac:image", "img"]:
            return self._process_image(element)

        # Параграфы с добавлением переводов строк
        if element.name == "p":
            return self._process_paragraph(element, context)

        # div/span
        if element.name in ["div", "span"]:
            return self._process_text_container(element, context)

        # Confluence элементы
        if element.name in ["ac:rich-text-body", "ac:layout", "ac:layout-section", "ac:layout-cell"]:
            return self._process_confluence_container(element, context)

        # Ячейки таблицы
        if element.name in ["td", "th"]:
            return self._process_table_cell(element, context)

        # Элементы списка
        if element.name == "li":
            return self._process_list_item(element, context)

        # По умолчанию - обрабатываем как контейнер
        return self._process_text_container(element, context)

    def _process_children(self, element: Tag, context: str) -> str:
        """
        ИСПРАВЛЕНО: Рекурсивная обработка дочерних элементов с правильной обработкой пробелов
        """
        result_parts = []

        for child in element.children:
            if isinstance(child, NavigableString):
                text = str(child)
                # ИСПРАВЛЕНО: Обрабатываем ВСЕ текстовые узлы, включая пробелы
                processed_text = self._process_text_node(text, context)
                result_parts.append(processed_text)
            elif isinstance(child, Tag):
                # ИСПРАВЛЕНО: Проверяем игнорируемые элементы ДО обработки
                if not self._is_ignored_element(child):
                    child_content = self._process_element(child, context)
                    if child_content is not None:
                        result_parts.append(child_content)
                # Если элемент игнорируемый (<s>) - просто пропускаем его

        # Соединяем БЕЗ добавления пробелов
        result = "".join(result_parts)

        # Применяем только очистку треугольных скобок если нужно
        if self.config.clean_brackets:
            result = self._clean_triangular_brackets(result)

        return result

    def _extract_black_elements_from_colored_container(self, element: Tag, context: str) -> str:
        """
        ИСПРАВЛЕНО: НЕ добавляем текстовые узлы из цветных контейнеров
        """
        if not self.config.include_colored:
            approved_parts = []

            for child in element.children:
                if isinstance(child, NavigableString):
                    # ИСПРАВЛЕНО: НЕ добавляем текстовые узлы автоматически
                    # Они будут добавлены только если находятся в черном дочернем элементе
                    continue
                elif isinstance(child, Tag):
                    # Проверяем игнорируемые элементы ПЕРВЫМИ
                    if self._is_ignored_element(child):
                        continue

                    # Проверяем черные цвета напрямую
                    child_style = child.get("style", "").lower()
                    child_is_black = False

                    if "color" in child_style:
                        color_match = re.search(r'color\s*:\s*([^;]+)', child_style)
                        if color_match:
                            color_value = color_match.group(1).strip()
                            child_is_black = is_black_color(color_value)

                    if child_is_black:
                        # ИСПРАВЛЕНО: Черный дочерний элемент - извлекаем БЕЗ цветовой фильтрации
                        child_text = self._process_children_without_color_filter(child, context)
                        if child_text:
                            approved_parts.append(child_text)
                    elif has_colored_style(child):
                        # Цветной дочерний элемент - рекурсивно ищем в нем черные части
                        child_text = self._extract_black_elements_from_colored_container(child, context)
                        if child_text:
                            approved_parts.append(child_text)
                    else:
                        # Элемент без цвета - обрабатываем как обычно
                        if not self._is_ignored_element(child):
                            child_text = self._process_element(child, context)
                            if child_text:
                                approved_parts.append(child_text)

            return "".join(approved_parts)

        return ""

    def _should_include_element(self, element: Tag) -> bool:
        """
        ИСПРАВЛЕНО: Ссылки получают специальный пропуск для анализа соседей
        """
        if self.config.include_colored:
            return True

        # Ссылки всегда пропускаем для анализа соседей в _process_link
        if element.name in ['a', 'ac:link']:
            return True

        # Для остальных элементов применяем цветовую фильтрацию
        if has_colored_style(element):
            return False

        if self._is_in_colored_ancestor_chain(element):
            return False

        return True

    def _process_text_node(self, text: str, context: str) -> str:
        """
        ИСПРАВЛЕНО: Обработка текстового узла БЕЗ потери пробелов
        """
        # Заменяем неразрывные пробелы на обычные
        text = text.replace('\u00a0', ' ')

        # Если включена минимальная нормализация, применяем только базовые правила
        if self.config.normalize_spacing:
            # Только критичные случаи - табы на пробелы
            text = text.replace('\t', ' ')

        return text

    def _is_ignored_element(self, element: Tag) -> bool:
        """
        ИСПРАВЛЕНО: Проверяет, должен ли элемент игнорироваться
        """
        if not isinstance(element, Tag):
            return False

        # Зачёркнутый текст.
        # • exclude_strikethrough=True (флаг --drop-strikethrough) — выкидываем ВСЕГДА,
        #   даже под --all: сам факт вычеркивания = запланированное удаление фрагмента.
        #   ИСКЛЮЧЕНИЕ — критик-режим (2026-08-07): цветное зачёркнутое — это удаление
        #   ЗАДАЧЕЙ, оно обязано стать {--ID: …--} (им управляет critic: apply применяет,
        #   reject-all возвращает ПРОМ-состав); выкидывание на выгрузке сделало бы
        #   восстановление невозможным. Флаг в критик-режиме действует только на
        #   ЧЁРНОЕ зачёркнутое (применённый мусор ПРОМ) — ровно то, что approved-режим
        #   выкидывает этим же флагом.
        # • Иначе в approved-режиме выкидываем, под --all (include_colored) сохраняем
        #   как обычный текст (вычеркивание без цвета/контекста само по себе ценности
        #   не несёт, а текст требования нужен).
        if element.name == "s":
            if self.config.exclude_strikethrough:
                if self.config.critic_mode and self._strike_belongs_to_task(element):
                    return False
                return True
            return not self.config.include_colored

        # Jira макросы
        if element.name == "ac:structured-macro" and element.get("ac:name") == "jira":
            return True

        if (element.name == "ac:parameter" and element.parent and
                element.parent.name == "ac:structured-macro" and
                element.parent.get("ac:name") == "jira"):
            return True

        return False

    # ОСТАЛЬНЫЕ МЕТОДЫ БЕЗ ИЗМЕНЕНИЙ (копируем из предыдущей версии)
    def _join_parts_preserving_structure(self, parts: List[str]) -> str:
        """Соединяет части с сохранением структуры"""
        if not parts:
            return ""

        non_empty_parts = [part for part in parts if part]

        if not non_empty_parts:
            return ""

        result_parts = []

        for i, part in enumerate(non_empty_parts):
            if i == 0:
                result_parts.append(part)
            else:
                prev_part = non_empty_parts[i - 1]
                current_part = part

                needs_blank_line = (
                    self._is_block_element(prev_part) or
                    self._is_block_element(current_part)
                )

                if needs_blank_line:
                    # Блочный элемент требует пустой строки-разделителя.
                    # Убираем trailing whitespace у предыдущей части и
                    # гарантируем ровно две новые строки перед текущей.
                    joined = "".join(result_parts).rstrip("\n")
                    result_parts = [joined + "\n\n"]
                    result_parts.append(current_part.lstrip("\n"))
                elif prev_part.endswith("\n"):
                    result_parts.append(current_part)
                else:
                    result_parts.append(current_part)

        return "".join(result_parts)

    def _is_block_element(self, content: str) -> bool:
        """Проверяет, является ли содержимое блочным элементом"""
        if not content:
            return False

        content_start = content.lstrip()
        return (content_start.startswith('#') or      # Заголовки Markdown
                content_start.startswith('|') or      # Таблицы Markdown
                content_start.startswith('<table') or # Таблицы HTML
                content_start.startswith('<thead') or # Фрагменты HTML-таблиц
                content_start.startswith('<tbody') or
                content_start.startswith('-') or      # Ненумерованные списки
                content_start.startswith('*') or
                content_start.startswith('+') or
                re.match(r'^\d+\.', content_start)) # Нумерованные списки

    def _process_container(self, container) -> List[str]:
        """
        Рекурсивная обработка контейнера
        """
        result_parts = []

        # Обрабатываем ВСЕ дочерние элементы, включая NavigableString
        for i, child in enumerate(container.children):
            if isinstance(child, NavigableString):
                # Обрабатываем текстовые узлы (включая пробелы)
                text = str(child)
                if text:  # Не пропускаем пробелы!
                    processed_text = self._process_text_node(text, "default")
                    result_parts.append(processed_text)
            elif isinstance(child, Tag):

                # Проверяем, должен ли элемент быть включен
                should_include = self._should_include_element(child)

                if not should_include:
                    if not self.config.include_colored:
                        black_content = self._extract_black_elements_from_colored_container(child, "default")
                        if black_content:
                            result_parts.append(black_content)
                else:
                    processed_content = self._process_element(child, context="default")
                    if processed_content is not None:
                        result_parts.append(processed_content)

        return result_parts

    def _process_paragraph(self, element: Tag, context: str) -> str:
        """Обработка параграфов с добавлением переводов строк.

        Если содержимое параграфа начинается с '{' и заканчивается на '}'
        (после trim), оборачивает в тройные обратные кавычки как код.
        Это покрывает JSON-примеры, набранные в Confluence как обычный текст.
        """
        # Режим CriticMarkup: решение о fenced принимаем по ИСХОДНОМУ тексту абзаца, а не по
        # обёрнутому результату — иначе маркер {++…++} (начинается с '{', кончается на '}')
        # ошибочно принимается за JSON и заворачивается в ```…``` (ТЗ 4.8: маркеры в коде
        # недопустимы). Настоящий JSON-абзац фенсим, но содержимое внутри — без маркеров.
        if self.config.critic_mode:
            original = element.get_text().strip()
            if original.startswith('{') and original.endswith('}'):
                prev = self._critic_suppress
                self._critic_suppress = True
                try:
                    content = self._process_children(element, context)
                finally:
                    self._critic_suppress = prev
                stripped = content.strip()
                return self._code_block(stripped, context) if stripped else ""

            content = self._process_children(element, context)
            if not content:
                return ""
            if not content.endswith('\n'):
                content += '\n'
            return content

        # --- Прежнее поведение (не-критик) без изменений ---
        content = self._process_children(element, context)

        if not content:
            return ""

        # Детекция JSON-блоков: параграф начинается с { и заканчивается на }
        stripped = content.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            return self._code_block(stripped, context)

        # Добавляем перевод строки для всех контекстов
        if context in ["table_cell", "nested_table_cell"]:
            if not content.endswith('\n'):
                content += '\n'
        else:
            if not content.endswith('\n'):
                content += '\n'

        return content

    def _process_list(self, element: Tag, context: str, indent_level: int = 0) -> str:
        """Обработка списков с правильными переводами строк"""
        if not self.config.format_lists:
            return self._process_text_container(element, context)

        list_items = []
        indent = "    " * indent_level

        if element.name == "ul":
            markers = ["-", "*", "+"]
            marker = markers[indent_level % len(markers)]
        else:
            marker = None

        item_counter = 1

        for li in element.find_all("li", recursive=False):
            if not self._should_include_element(li):
                if not self.config.include_colored:
                    black_content = self._extract_black_elements_from_colored_container(li, context)
                    if black_content:
                        if element.name == "ul":
                            list_items.append(f"{indent}{marker} {black_content}")
                        else:
                            list_items.append(f"{indent}{item_counter}. {black_content}")
                            item_counter += 1
                continue

            item_content = self._process_list_item_content(li, context, indent_level)

            # ИСПРАВЛЕНО: Проверяем, что содержимое не пустое после trim
            if item_content and item_content.strip():
                if element.name == "ul":
                    list_items.append(f"{indent}{marker} {item_content}")
                else:
                    list_items.append(f"{indent}{item_counter}. {item_content}")
                    item_counter += 1

            nested_lists = li.find_all(["ul", "ol"], recursive=False)
            for nested_list in nested_lists:
                nested_content = self._process_list(nested_list, context, indent_level + 1)
                if nested_content:
                    list_items.append(nested_content)

        result = "\n".join(list_items)

        if result and context in ["table_cell", "nested_table_cell"]:
            result += "\n"

        return result

    def _process_list_item_content(self, li: Tag, context: str, indent_level: int) -> str:
        """Обработка содержимого элемента списка с правильными переводами"""
        content_parts = []

        for child in li.children:
            if isinstance(child, NavigableString):
                text = str(child)
                processed_text = self._process_text_node(text, context)
                content_parts.append(processed_text)
            elif isinstance(child, Tag):
                if child.name in ["ul", "ol"]:
                    continue
                else:
                    if self._should_include_element(child):
                        child_content = self._process_element(child, context)
                        if child_content is not None:
                            content_parts.append(child_content)
                    elif not self.config.include_colored:
                        black_content = self._extract_black_elements_from_colored_container(child, context)
                        if black_content:
                            content_parts.append(black_content)

        result = "".join(content_parts)
        result = result.rstrip('\n')

        return result

    def _apply_minimal_cleanup(self, content: str) -> str:
        """Применяет только минимальную очистку контента"""
        if not content:
            return content

        content = content.replace('\u00a0', ' ')

        if self.config.normalize_spacing:
            content = content.replace('\t', ' ')
            content = re.sub(r' {4,}', ' ', content)

        if self.config.clean_brackets:
            content = self._clean_triangular_brackets(content)

        return content

    def _clean_triangular_brackets(self, content: str) -> str:
        """Очистка содержимого треугольных скобок.

        Чистка нормализует пробелы/кавычки внутри <...> для текстовых
        плейсхолдеров-требований вида <Поле "Имя">. Но она ломает настоящие
        HTML-теги с атрибутами (например <img src="..." width="...">), схлопывая
        пробелы между атрибутами. Поэтому теги с синтаксисом атрибутов (name="value")
        пропускаем без изменений.
        """
        def _clean_match(m: re.Match) -> str:
            inner = m.group(1)
            if '="' in inner:
                return m.group(0)  # настоящий HTML-тег — не трогаем
            return f'<{self._clean_bracket_content(inner)}>'

        content = re.sub(r'<\s*([^<>]*?)\s*>', _clean_match, content)
        content = re.sub(r'<\s*>', '<>', content)
        return content

    def _clean_bracket_content(self, content: str) -> str:
        """Умная очистка содержимого треугольных скобок"""
        if not content:
            return ''

        content = content.strip()
        content = re.sub(r'\s+', ' ', content)
        content = re.sub(r'"\s+', '"', content)
        content = re.sub(r'\s+"', '"', content)
        content = re.sub(r'(\w)"', r'\1 "', content)
        content = re.sub(r'\[\s+', '[', content)
        content = re.sub(r'\s+\]', ']', content)

        return content

    def _is_in_colored_ancestor_chain(self, element: Tag) -> bool:
        """Проверяет, есть ли цветные предки у элемента"""
        if self.config.include_colored:
            return False

        current = element.parent
        while current and isinstance(current, Tag):
            if current.name == "ac:rich-text-body":
                break
            if has_colored_style(current):
                return True
            current = current.parent
        return False

    def _process_text_container(self, element: Tag, context: str) -> str:
        """Обработка текстовых контейнеров (div, span)"""
        if element.name == "div":
            inner_headers = element.find_all(["h1", "h2", "h3", "h4", "h5", "h6"], recursive=False)
            if inner_headers:
                return self._process_confluence_container(element, context)

            # Вне ячеек таблиц <div> может содержать блочные элементы (таблицы, списки,
            # абзацы), которые в Markdown разделяются пустой строкой. Плоский join в
            # _process_children её не вставляет — из-за чего, например, чистая Markdown-
            # таблица сразу за абзацем "История изменений:" не распознаётся (частый
            # случай в рендеренном HTML, где контент завёрнут в layout-div'ы). Поэтому
            # в обычном контексте собираем содержимое div с сохранением блочной структуры.
            if context == "default":
                parts = self._process_container(element)
                result = self._join_parts_preserving_structure(parts)
                if self.config.clean_brackets:
                    result = self._clean_triangular_brackets(result)
                return result

        return self._process_children(element, context)

    def _process_confluence_container(self, element: Tag, context: str) -> str:
        """Обработка Confluence контейнеров"""
        nested_parts = self._process_container(element)
        return self._join_parts_preserving_structure(nested_parts)

    def _process_link(self, element: Tag, context: str) -> str:
        """
        Анализ соседей применяется везде одинаково.

        Для внутренних ссылок Confluence генерирует плейсхолдер confluence://ID,
        который при миграции дерева заменяется на относительный путь к файлу.
        Внешние ссылки сохраняются как есть.

        Внутри HTML-таблиц (context=nested_table_cell) Markdown-синтаксис ссылок
        [текст](url) не обрабатывается рендерерами, так как находится внутри
        сырого HTML-блока. Поэтому там генерируется HTML-тег <a href> —
        аналогично тому, как _process_bold переключается на <strong>.
        """
        # В режиме "только подтвержденные" всегда анализируем соседей
        if not self.config.include_colored:
            if not self._analyze_link_neighbors(element):
                return ""

        html_context = context == "nested_table_cell"

        if element.name == "ac:link":
            ri_page = element.find("ri:page")
            if not ri_page:
                # Внешняя ссылка (ri:url) — сохраняем URL как обычную ссылку. Не зависит
                # от migrate_images: внешний адрес доступен без скачивания.
                ri_url = element.find("ri:url")
                if ri_url and ri_url.get("ri:value"):
                    url = ri_url.get("ri:value")
                    return self._format_link(
                        self._link_body_text(element, fallback=url), url, html_context
                    )

                # Ссылка на вложение (ri:attachment) — плейсхолдер confluence-attachment://,
                # который слой миграции (image_migrator) разрешает в скачанный файл (img/)
                # либо в абсолютный URL вложения. Только при migrate_images, как и картинки:
                # без него адрес раскрывать нечем (нет page_id), остаётся текст без ссылки.
                ri_att = element.find("ri:attachment")
                if ri_att and ri_att.get("ri:filename") and self.config.migrate_images:
                    filename = ri_att.get("ri:filename")
                    return self._format_link(
                        self._link_body_text(element, fallback=filename),
                        f"confluence-attachment://{filename}",
                        html_context,
                    )

                # Прочие ac:link без распознанного ресурса — текст без адреса (как было).
                text = element.get_text(strip=True)
                return self._format_link(text, None, html_context) if text else ""

            # Определяем отображаемый текст ссылки
            link_body = element.find("ac:plain-text-link-body")
            if link_body and link_body.get_text(strip=True):
                text = link_body.get_text(strip=True)
            elif ri_page.get("ri:content-title"):
                text = ri_page["ri:content-title"]
            else:
                text = element.get_text(strip=True)

            if not text:
                return ""

            # Строим URL-плейсхолдер. Когда у ссылки есть и ID, и заголовок,
            # заголовок дописываем суффиксом ?title=... к ID-плейсхолдеру: на Pass 2
            # это даёт fallback-резолв по заголовку, если ID не найдётся в реестре
            # (например, ссылка ведёт за пределы текущего поддерева, но страница уже
            # есть на диске и попала в title_registry через seed_registries_from_disk).
            content_id = ri_page.get("ri:content-id")
            content_title = ri_page.get("ri:content-title")
            space_key = ri_page.get("ri:space-key", "")

            title_path = ""
            if content_title:
                # Пробел кодируем как '+'. Литеральный '+' в заголовке (напр. продукт
                # "O2+") предварительно экранируем как %2B — иначе он станет неотличим
                # от закодированного пробела, и Pass 2 не восстановит исходный заголовок
                # (см. _decode_title_path в migrate_confluence_tree).
                encoded = content_title.replace("+", "%2B").replace(" ", "+")
                title_path = f"{space_key}/{encoded}" if space_key else encoded

            if content_id:
                url = f"confluence://{content_id}"
                if title_path:
                    url += f"?title={title_path}"
                return self._format_link(text, url, html_context)

            if content_title:
                return self._format_link(text, f"confluence://title/{title_path}", html_context)

            return self._format_link(text, None, html_context)

        else:  # element.name == "a"
            text = element.get_text(strip=True)
            if not text:
                return ""
            href = element.get("href", "")
            if not href:
                return self._format_link(text, None, html_context)
            page_id = _extract_page_id_from_href(href)
            if page_id:
                return self._format_link(text, f"confluence://{page_id}", html_context)
            return self._format_link(text, href, html_context)

    def _format_link(self, text: str, href: Optional[str], html_context: bool) -> str:
        """Форматирует ссылку под нужный контекст.

        html_context=True  → HTML-тег <a href> (работает внутри HTML-таблиц,
                             где Markdown-синтаксис ссылок не обрабатывается).
        html_context=False → Markdown-ссылка [текст](url) с экранированием
                             квадратных скобок в тексте.

        href=None — ссылка без адреса: в HTML возвращаем только текст,
        в Markdown — текст в квадратных скобках как плейсхолдер.
        """
        if html_context:
            safe_text = _escape_html_text(text)
            if not href:
                return safe_text
            return f'<a href="{_escape_html_attr(href)}">{safe_text}</a>'

        escaped = _escape_link_text(text)
        if not href:
            return f"[{escaped}]"
        return f"[{escaped}]({href})"

    def _link_body_text(self, element: Tag, fallback: str = "") -> str:
        """Отображаемый текст ac:link: тело ссылки (plain-text/rich), иначе текст узла,
        иначе fallback (например, сам URL или имя файла-вложения)."""
        body = element.find("ac:plain-text-link-body") or element.find("ac:link-body")
        text = body.get_text(strip=True) if body else element.get_text(strip=True)
        return text or fallback

    def _analyze_link_neighbors(self, link_element: Tag) -> bool:
        """
        Анализ соседних блоков ссылки для определения её статуса
        """
        if not link_element.parent:
            return True

        parent = link_element.parent
        all_children = list(parent.children)

        try:
            link_index = all_children.index(link_element)
        except ValueError:
            return True

        left_status = self._get_neighbor_block_status(all_children, link_index, -1)
        right_status = self._get_neighbor_block_status(all_children, link_index, 1)

        # Применяем правила анализа
        if left_status is None and right_status is None:
            return True
        elif left_status is None:
            left_status = right_status
        elif right_status is None:
            right_status = left_status

        # Если оба соседних блока цветные - ссылка исключается
        result = not (left_status and right_status)

        return result

    def _get_neighbor_block_status(self, children: list, start_index: int, direction: int) -> Optional[bool]:
        """
        Получает статус соседнего блока, пропуская незначимые пробелы
        """
        step = direction
        for i in range(start_index + step, len(children) if direction > 0 else -1, step):
            if direction < 0 and i < 0:
                break

            child = children[i]

            # ИСПРАВЛЕНИЕ: Пропускаем незначимые текстовые узлы
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if not text:  # Пустой текст (пробелы, переводы строк) - пропускаем
                    continue
                # Значимый текст - анализируем
                status = False  # Текстовые узлы без стиля = подтвержденные
                return status
            else:
                status = self._get_text_block_color_status(child)

                if status is not None:
                    return status

        return None

    def _get_text_block_color_status(self, element) -> Optional[bool]:
        """Определяет статус текстового блока"""
        if isinstance(element, NavigableString):
            text = str(element)
            return False if text else None

        if isinstance(element, Tag):
            if element.name in ["br", "ac:structured-macro"]:
                return None

            text_content = element.get_text()
            if not text_content:
                return None

            return has_colored_style(element)

        return None

    # Остальные методы таблиц (копируем без изменений)
    def _process_header(self, element: Tag, context: str) -> str:
        """Обработка заголовков с префиксами"""
        if not self.config.format_headers:
            return self._process_text_container(element, context)

        level = int(element.name[1])
        prefix = "#" * level
        content = self._process_children(element, context)

        if content:
            return f"{prefix} {content}"
        return ""

    def _process_table_cell(self, element: Tag, context: str) -> str:
        """
        ИСПРАВЛЕНО: Обработка ячейки таблицы - исключает двойную обработку вложенных таблиц
        """
        nested_table = element.find("table")
        if nested_table:
            # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Если есть вложенная таблица, сразу возвращаем результат
            # и НЕ продолжаем дальнейшую обработку через structural_elements
            return self._process_cell_with_nested_table(element, nested_table, context)

        structural_elements = element.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "div", "p"],
                                               recursive=False)

        if len(structural_elements) > 0:
            cell_parts = []

            for child in element.children:
                if isinstance(child, NavigableString):
                    text = str(child)
                    if text:
                        text = text.replace('\u00a0', ' ')
                        cell_parts.append(text)
                elif isinstance(child, Tag):
                    child_content = self._process_element(child, "table_cell")
                    if child_content:
                        cell_parts.append(child_content)

            if cell_parts:
                result = "".join(cell_parts)

                if self.config.clean_brackets:
                    result = self._clean_triangular_brackets(result)

                return result
            else:
                return self._process_children(element, "table_cell")
        else:
            return self._process_children(element, "table_cell")

    def _process_cell_with_nested_table(self, cell: Tag, nested_table: Tag, context: str) -> str:
        """
        ИСПРАВЛЕНО: Обработка ячейки с вложенной таблицей
        Извлекает весь контент до и после таблицы, включая контент из контейнеров
        """
        result_parts = []

        # ИСПРАВЛЕНИЕ: Собираем весь контент до таблицы, включая из контейнеров
        text_before = self._extract_content_before_table(cell, nested_table, context)
        if text_before:
            result_parts.append(text_before)

        # Обрабатываем саму вложенную таблицу
        nested_html = self._process_nested_table_to_html(nested_table)
        if nested_html:
            result_parts.append(f"**Таблица:** {nested_html}")

        # ИСПРАВЛЕНИЕ: Собираем весь контент после таблицы
        text_after = self._extract_content_after_table(cell, nested_table, context)
        if text_after:
            result_parts.append(text_after)

        return " ".join(result_parts)

    def _extract_content_before_table(self, cell: Tag, target_table: Tag, context: str) -> str:
        """
        НОВЫЙ МЕТОД: Извлекает весь контент ДО таблицы, включая из контейнеров
        """
        result_parts = []

        def extract_until_table(element, target):
            """Рекурсивно извлекает контент до таблицы"""
            for child in element.children:
                # Если нашли целевую таблицу - останавливаемся
                if child == target:
                    return True

                if isinstance(child, NavigableString):
                    text = str(child)
                    if text:
                        result_parts.append(text)
                elif isinstance(child, Tag):
                    # Если это таблица (но не наша целевая) - пропускаем
                    if child.name == "table":
                        continue

                    # Если элемент содержит целевую таблицу - рекурсивно обрабатываем
                    if child.find(lambda t: t == target):
                        found = extract_until_table(child, target)
                        if found:
                            return True
                    else:
                        # Элемент не содержит таблицу - обрабатываем полностью
                        content = self._process_element(child, context)
                        if content:
                            result_parts.append(content)

            return False

        extract_until_table(cell, target_table)
        return "".join(result_parts)

    def _extract_content_after_table(self, cell: Tag, target_table: Tag, context: str) -> str:
        """
        НОВЫЙ МЕТОД: Извлекает весь контент ПОСЛЕ таблицы
        """
        result_parts = []
        found_table = False

        def extract_after_table(element, target):
            """Рекурсивно извлекает контент после таблицы"""
            nonlocal found_table

            for child in element.children:
                # Отмечаем, что нашли целевую таблицу
                if child == target:
                    found_table = True
                    continue

                # Если ещё не нашли таблицу
                if not found_table:
                    # Если элемент содержит целевую таблицу - рекурсивно ищем
                    if isinstance(child, Tag) and child.find(lambda t: t == target):
                        extract_after_table(child, target)
                    continue

                # Уже после таблицы - собираем контент
                if isinstance(child, NavigableString):
                    text = str(child)
                    if text:
                        result_parts.append(text)
                elif isinstance(child, Tag):
                    # Если это другая таблица - пропускаем
                    if child.name == "table":
                        continue

                    content = self._process_element(child, context)
                    if content:
                        result_parts.append(content)

        extract_after_table(cell, target_table)
        return "".join(result_parts)

    def _process_nested_table_to_html(self, table: Tag) -> str:
        """
        ИСПРАВЛЕНО: Преобразование вложенной таблицы в HTML с обработкой глубокой вложенности
        """
        rows = table.find_all("tr", recursive=False)
        if not rows:
            tbody = table.find("tbody")
            thead = table.find("thead")
            if tbody:
                rows.extend(tbody.find_all("tr", recursive=False))
            if thead:
                rows.extend(thead.find_all("tr", recursive=False))

        if not rows:
            return ""

        html_parts = ["<table>"]

        for row in rows:
            cells = row.find_all(["td", "th"], recursive=False)
            row_parts = ["<tr>"]

            for cell in cells:
                tag_name = "th" if cell.name == "th" else "td"

                attrs = []
                if cell.get("rowspan") and int(cell.get("rowspan", 1)) > 1:
                    attrs.append(f'rowspan="{cell["rowspan"]}"')
                if cell.get("colspan") and int(cell.get("colspan", 1)) > 1:
                    attrs.append(f'colspan="{cell["colspan"]}"')

                attrs_str = " " + " ".join(attrs) if attrs else ""

                # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Обрабатываем содержимое ячейки специальным методом
                # который конвертирует вложенные таблицы в HTML вместо Markdown
                cell_content = self._process_nested_table_cell_content(cell)
                row_parts.append(f"<{tag_name}{attrs_str}>{cell_content}</{tag_name}>")

            row_parts.append("</tr>")
            html_parts.append("".join(row_parts))

        html_parts.append("</table>")
        return "".join(html_parts)

    def _list_to_html(self, element: Tag) -> str:
        """
        Рекурсивно конвертирует ul/ol список в HTML-разметку.
        Используется когда список находится внутри HTML-таблицы —
        там Markdown-синтаксис списков не работает корректно.
        Цветовая фильтрация и обработка ссылок применяются к каждому пункту.
        """
        tag = element.name  # ul или ol
        parts = [f"<{tag}>"]

        for li in element.find_all("li", recursive=False):
            if not self._should_include_element(li):
                if not self.config.include_colored:
                    black = self._extract_black_elements_from_colored_container(li, "nested_table_cell")
                    if black:
                        parts.append(f"<li>{black}</li>")
                continue

            li_parts = []
            for child in li.children:
                if isinstance(child, NavigableString):
                    text = str(child).replace("\u00a0", " ")
                    if text.strip():
                        # \u041f\u0443\u043d\u043a\u0442 \u0443\u0445\u043e\u0434\u0438\u0442 \u0432 \u0441\u044b\u0440\u043e\u0439 HTML-\u0441\u043f\u0438\u0441\u043e\u043a \u2014 \u043a\u0430\u043a \u0438 \u0432 \u044f\u0447\u0435\u0439\u043a\u0435,
                        # '<' \u043f\u0435\u0440\u0435\u0434 \u043b\u0430\u0442\u0438\u043d\u0438\u0446\u0435\u0439 \u044d\u043a\u0440\u0430\u043d\u0438\u0440\u0443\u0435\u0442\u0441\u044f \u043e\u0431\u0440\u0430\u0442\u043d\u043e.
                        li_parts.append(_escape_stray_tag_openers(text))
                    elif text and li_parts and not li_parts[-1].endswith((" ", "\n")):
                        # Чисто пробельный узел между инлайн-элементами — это
                        # разделитель: границы <strong>/цветных <span> часто
                        # оставляют пробел отдельным узлом, и его отбрасывание
                        # склеивает соседей («Если<a …»). Схлопываем до одного
                        # пробела; в начале пункта и после уже имеющегося
                        # пробела не добавляем.
                        li_parts.append(" ")
                elif isinstance(child, Tag):
                    if self._is_ignored_element(child):
                        continue
                    if child.name in ["ul", "ol"]:
                        # Рекурсивно конвертируем вложенный список
                        li_parts.append(self._list_to_html(child))
                    elif child.name in ["a", "ac:link"]:
                        link = self._process_link(child, "nested_table_cell")
                        if link:
                            li_parts.append(link)
                    elif child.name in ["strong", "b"]:
                        # Прямой жирный потомок пункта: без этой ветки уходил в
                        # общий else и терял обёртку <strong>. Краевые пробелы —
                        # за тег, как в _process_nested_table_cell_content.
                        bold_content = self._process_nested_table_cell_content(child)
                        stripped = bold_content.strip()
                        if stripped:
                            leading = bold_content[: len(bold_content) - len(bold_content.lstrip())]
                            trailing = bold_content[len(bold_content.rstrip()):]
                            li_parts.append(f"{leading}<strong>{stripped}</strong>{trailing}")
                        elif bold_content:
                            li_parts.append(bold_content)
                    else:
                        content = self._process_nested_table_cell_content(child)
                        if content:
                            li_parts.append(content)
            parts.append(f"<li>{''.join(li_parts)}</li>")

        parts.append(f"</{tag}>")
        return "".join(parts)

    def _process_nested_table_cell_content(self, cell: Tag) -> str:
        """
        ИСПРАВЛЕНО: Обработка содержимого ячейки вложенной таблицы.
        Конвертирует вложенные таблицы в HTML, а не в Markdown.
        ДОБАВЛЕНА обработка заголовков h1-h6
        """
        result_parts = []

        for child in cell.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text:
                    text = text.replace('\u00a0', ' ')
                    # \u0422\u0435\u043a\u0441\u0442\u043e\u0432\u044b\u0439 \u0443\u0437\u0435\u043b \u0443\u0445\u043e\u0434\u0438\u0442 \u0432 \u0441\u044b\u0440\u0443\u044e HTML-\u044f\u0447\u0435\u0439\u043a\u0443: \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u0430\u0432\u043b\u0438\u0432\u0430\u0435\u043c
                    # \u044d\u043a\u0440\u0430\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u043e\u0442\u0435\u043d\u0446\u0438\u0430\u043b\u044c\u043d\u044b\u0445 \u0442\u0435\u0433\u043e\u0432 (&lt;S&gt; \u0438\u0441\u0445\u043e\u0434\u043d\u0438\u043a\u0430 \u043d\u0435
                    # \u0434\u043e\u043b\u0436\u0435\u043d \u0441\u0442\u0430\u0442\u044c \u0436\u0438\u0432\u044b\u043c <s> \u0432 \u0440\u0435\u043d\u0434\u0435\u0440\u0435).
                    result_parts.append(_escape_stray_tag_openers(text))
            elif isinstance(child, Tag):
                if self._is_ignored_element(child):
                    continue

                # КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Применяем цветовую фильтрацию
                should_include = self._should_include_element(child)
                if not should_include:
                    if not self.config.include_colored:
                        black_content = self._extract_black_elements_from_colored_container(child, "nested_table_cell")
                        if black_content:
                            result_parts.append(black_content)
                    continue

                # Режим CriticMarkup внутри сырого HTML: цветной фрагмент оборачивается
                # HTML-нотацией <span class="critic-ins|critic-del" data-task="ID"> (ТЗ п. 4.7),
                # а не текстовым маркером — внутри HTML-острова {++..++} не рендерится.
                if (self.config.critic_mode and not self._critic_stack
                        and not self._critic_suppress):
                    marker = self._critic_marker_for(child)
                    if marker is not None:
                        task, kind = marker
                        self._critic_stack.append(task)
                        try:
                            inner = self._process_nested_table_cell_content(child)
                        finally:
                            self._critic_stack.pop()
                        if inner.strip():
                            cls = "critic-ins" if kind == "ins" else "critic-del"
                            result_parts.append(
                                f'<span class="{cls}" data-task="{task}">{inner}</span>')
                        continue

                # Элемент прошел цветовую фильтрацию - обрабатываем
                if child.name == "table":
                    # Таблицу конвертируем в HTML рекурсивно
                    nested_html = self._process_nested_table_to_html(child)
                    if nested_html:
                        result_parts.append(nested_html)
                elif child.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                    # В HTML-контексте заголовок отдаём HTML-тегом <hN>, а не
                    # markdown '#': внутри сырого HTML-блока '#' не рендерится.
                    content = self._process_nested_table_cell_content(child)
                    if content.strip():
                        if self.config.format_headers:
                            result_parts.append(f"<{child.name}>{content.strip()}</{child.name}>")
                        else:
                            result_parts.append(f"<p>{content.strip()}</p>")
                elif child.name in ["a", "ac:link"]:
                    link_content = self._process_link(child, "nested_table_cell")
                    if link_content:
                        result_parts.append(link_content)
                elif child.name in ["ul", "ol"]:
                    # В HTML-контексте конвертируем список в HTML-теги
                    list_content = self._list_to_html(child)
                    if list_content:
                        result_parts.append(list_content)
                elif child.name == "time":
                    result_parts.append(self._process_time(child))
                elif child.name in ["ac:image", "img"]:
                    result_parts.append(self._process_image(child))
                elif child.name == "br":
                    result_parts.append("\n")
                elif child.name in ["pre", "code"]:
                    result_parts.append(self._process_code_block(child, "nested_table_cell"))
                elif (child.name == "ac:structured-macro" and
                        child.get("ac:name") in ("code", "noformat")):
                    result_parts.append(self._process_code_block(child, "nested_table_cell"))
                elif child.name in ["strong", "b"]:
                    bold_content = self._process_nested_table_cell_content(child)
                    stripped = bold_content.strip()
                    if stripped:
                        # Пробелы по краям выносим за <strong>, чтобы не склеить
                        # жирный фрагмент с соседним текстом (напр. 'если ' + 'реквизит').
                        leading = bold_content[: len(bold_content) - len(bold_content.lstrip())]
                        trailing = bold_content[len(bold_content.rstrip()):]
                        result_parts.append(f"{leading}<strong>{stripped}</strong>{trailing}")
                    elif bold_content:
                        # Только пробелы (например <strong> </strong> как разделитель) —
                        # сохраняем пробел, а не пустой тег <strong></strong>.
                        result_parts.append(bold_content)
                elif child.name == "p":
                    # В HTML-контексте абзац оборачиваем в <p>, иначе разрывы
                    # между абзацами теряются (перевод строки в HTML-ячейке —
                    # это просто пробел). Сохраняем margin-left для отступа
                    # абзацев-описаний под заголовками шагов.
                    p_content = self._process_nested_table_cell_content(child)
                    if p_content.strip():
                        margin = _extract_margin_left(child.get("style", ""))
                        if margin:
                            result_parts.append(
                                f'<p style="margin-left: {margin}">{p_content.strip()}</p>'
                            )
                        else:
                            result_parts.append(f"<p>{p_content.strip()}</p>")
                else:
                    # Для остальных элементов рекурсивно обрабатываем содержимое
                    child_content = self._process_nested_table_cell_content(child)
                    if child_content:
                        result_parts.append(child_content)

        return "".join(result_parts)

    def _process_list_item(self, element: Tag, context: str) -> str:
        """Обработка элемента списка"""
        return self._process_children(element, context)

    def _process_confluence_macros(self, soup: BeautifulSoup):
        """
        Обработка Confluence-специфичных макросов <ac:structured-macro>.

        Стратегия по типам:
        • Динамические листинги (Confluence отрисовывает из контекста при показе) —
          удаляются целиком вместе с параметрами:
            children, toc, recently-updated, pagetree, blog-posts,
            content-by-label, labels-list, page-tree-search, spaces-list
        • Контентные обёртки (несут осмысленный текст внутри) — разворачиваются,
          тело сохраняется:
            expand, info, warning, note, tip, panel
        • code / noformat — пропускаем, обрабатываются отдельно в
          _process_text_container и _process_nested_table_cell_content
        • jira — пропускаем, обрабатывается в _is_ignored_element
        • Незнакомые макросы: если несут тело (rich-text-body / plain-text-body) —
          разворачиваются с сохранением содержимого (иначе обёрнутый контент,
          например таблица внутри table-filter, тихо терялся бы); если тела нет
          (как у динамических листингов) — удаляются. В лог пишется WARNING.
        """
        DYNAMIC_LISTING_MACROS = {
            "children", "toc", "recently-updated", "pagetree",
            "blog-posts", "content-by-label", "contentbylabel",
            "labels-list", "page-tree-search", "spaces-list",
        }

        # Контентные обёртки: несут осмысленное тело, его надо сохранить.
        # table-filter / table-plus — макросы плагина «Table Filter and Charts»,
        # оборачивают обычную таблицу в <ac:rich-text-body>.
        UNWRAP_MACROS = {
            "expand", "info", "warning", "note", "tip", "panel",
            "table-filter", "table-plus",
        }

        HANDLED_ELSEWHERE = {"code", "noformat", "jira"}

        for macro in soup.find_all("ac:structured-macro"):
            name = (macro.get("ac:name") or "").lower()

            if name in DYNAMIC_LISTING_MACROS:
                macro.decompose()
            elif name in UNWRAP_MACROS:
                body = macro.find("ac:rich-text-body") or macro.find("ac:plain-text-body")
                if body:
                    macro.replace_with(body)
                else:
                    macro.decompose()
            elif name in HANDLED_ELSEWHERE:
                continue
            else:
                # Неизвестный макрос: сохраняем тело, если оно есть, — иначе
                # рискуем тихо потерять обёрнутый контент (таблицы, текст).
                body = macro.find("ac:rich-text-body") or macro.find("ac:plain-text-body")
                if body:
                    logger.warning(
                        "[_process_confluence_macros] Unknown macro '%s' with body — "
                        "разворачиваем, сохраняя содержимое", name
                    )
                    macro.replace_with(body)
                else:
                    logger.warning(
                        "[_process_confluence_macros] Unknown macro '%s', removing", name
                    )
                    macro.decompose()

    def _remove_empty_paragraphs(self, soup: BeautifulSoup):
        """
        Удаляет пустые параграфы вида <p><br/></p> и <p>&nbsp;</p>,
        которые часто остаются от Confluence-редактора и создают
        лишние пустые строки в Markdown-выводе.

        Сохраняет параграфы, содержащие блочные элементы (таблицы,
        изображения, списки), даже если у них нет собственного текста.

        Также сохраняет параграфы со ссылками (<a>, <ac:link>): у внутренних
        ссылок Confluence на страницу по заголовку отображаемый текст хранится
        в атрибуте ri:content-title, а не как текстовый узел, поэтому
        get_text() для них пуст — без этой проверки такие ссылки терялись бы.

        Аналогично сохраняет параграфы с датой (<time>): Confluence хранит
        дату в атрибуте datetime (<time datetime="2024-07-08" />), тело тега
        пустое, поэтому get_text() пуст. Без этого исключения параграф
        с датой (напр. в таблице "История изменений") считался бы пустым
        и удалялся ДО обработки в _process_time — дата терялась бы.
        """
        for p in soup.find_all("p"):
            if (not p.get_text(strip=True)
                    and not p.find(["table", "img", "ac:image", "ul", "ol", "a", "ac:link", "time"])):
                p.decompose()

    def _process_children_without_color_filter(self, element: Tag, context: str) -> str:
        """
        Обработка дочерних элементов БЕЗ цветовой фильтрации.
        Используется когда мы уже внутри подтвержденного (черного) элемента.
        """
        result_parts = []

        for child in element.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text:
                    processed_text = self._process_text_node(text, context)
                    result_parts.append(processed_text)
            elif isinstance(child, Tag):
                # ВАЖНО: НЕ применяем цветовую фильтрацию, но проверяем игнорируемые
                if not self._is_ignored_element(child):
                    child_content = self._process_element_without_color_filter(child, context)
                    if child_content is not None:
                        result_parts.append(child_content)

        result = "".join(result_parts)

        if self.config.clean_brackets:
            result = self._clean_triangular_brackets(result)

        return result

    def _process_element_without_color_filter(self, element, context: str = "default") -> Optional[str]:
        """
        НОВЫЙ МЕТОД: Обработка элемента БЕЗ цветовой фильтрации.
        """
        if isinstance(element, NavigableString):
            return self._process_text_node(str(element), context)

        if not isinstance(element, Tag):
            return None

        # Проверяем только игнорируемые элементы
        if self._is_ignored_element(element):
            return None

        if element.name == "br":
            return "\n"

        # Обрабатываем элементы БЕЗ цветовых проверок
        if element.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            return self._process_header_without_color_filter(element, context)
        elif element.name in ["a", "ac:link"]:
            return self._process_link(element, context)
        elif element.name in ["strong", "b"]:
            content = self._process_children_without_color_filter(element, context)
            return f"**{content.strip()}**" if content.strip() else ""
        elif element.name == "time":
            return self._process_time(element)
        elif element.name in ["ac:image", "img"]:
            return self._process_image(element)
        elif element.name == "p":
            return self._process_paragraph_without_color_filter(element, context)
        else:
            # Для всех остальных элементов - просто обрабатываем детей
            return self._process_children_without_color_filter(element, context)

    def _process_header_without_color_filter(self, element: Tag, context: str) -> str:
        """Обработка заголовков БЕЗ цветовой фильтрации"""
        if not self.config.format_headers:
            return self._process_children_without_color_filter(element, context)

        level = int(element.name[1])
        prefix = "#" * level
        content = self._process_children_without_color_filter(element, context)

        if content:
            return f"{prefix} {content}"
        return ""

    def _process_paragraph_without_color_filter(self, element: Tag, context: str) -> str:
        """Обработка параграфов БЕЗ цветовой фильтрации"""
        content = self._process_children_without_color_filter(element, context)

        if not content:
            return ""

        # Добавляем перевод строки для всех контекстов
        if context in ["table_cell", "nested_table_cell"]:
            if not content.endswith('\n'):
                content += '\n'
        else:
            # Для обычного контекста тоже добавляем перевод строки
            if not content.endswith('\n'):
                content += '\n'

        return content


# Фабричные функции остаются теми же
def _migrate_images_enabled() -> bool:
    """Читает app.config.MIGRATE_IMAGES динамически, чтобы CLI-флаг --with-images
    мог переопределить значение в рантайме (по аналогии с REMOVE_HISTORY_SECTIONS)."""
    import app.config as _config
    return _config.MIGRATE_IMAGES


def _exclude_strikethrough_enabled() -> bool:
    """Читает app.config.EXCLUDE_STRIKETHROUGH динамически, чтобы CLI-флаг
    --drop-strikethrough мог переопределить значение в рантайме (по аналогии с
    MIGRATE_IMAGES). При True зачёркнутый <s> выкидывается при любом include_colored."""
    import app.config as _config
    return _config.EXCLUDE_STRIKETHROUGH


def create_all_fragments_extractor() -> ContentExtractor:
    """Создает экстрактор для всех фрагментов с сохранением пробелов"""
    config = ExtractionConfig(
        include_colored=True,
        preserve_whitespace=True,
        normalize_spacing=False,
        migrate_images=_migrate_images_enabled(),
        exclude_strikethrough=_exclude_strikethrough_enabled()
    )
    return ContentExtractor(config)


def create_approved_fragments_extractor() -> ContentExtractor:
    """Создает экстрактор для подтвержденных фрагментов с сохранением пробелов"""
    config = ExtractionConfig(
        include_colored=False,
        preserve_whitespace=True,
        normalize_spacing=False,
        migrate_images=_migrate_images_enabled(),
        exclude_strikethrough=_exclude_strikethrough_enabled()
    )
    return ContentExtractor(config)


def create_critic_extractor(color_map: Dict[str, str]) -> ContentExtractor:
    """Создаёт экстрактор режима CriticMarkup (Модуль 1, срез 1b).

    Цветные фрагменты оборачиваются в маркеры по карте color_map (нормализованный
    #rrggbb -> TASK-ID). Режим подразумевает include_colored=True — ничего не выбрасываем,
    всё оборачиваем; ЦВЕТНОЕ зачёркивание не выкидывается никогда (становится {--...--}).
    Флаг --drop-strikethrough в этом режиме действует только на ЧЁРНОЕ зачёркнутое
    (см. _is_ignored_element, 2026-08-07).
    """
    config = ExtractionConfig(
        include_colored=True,
        preserve_whitespace=True,
        normalize_spacing=False,
        migrate_images=_migrate_images_enabled(),
        exclude_strikethrough=_exclude_strikethrough_enabled(),
        critic_mode=True,
        color_map=color_map,
    )
    return ContentExtractor(config)