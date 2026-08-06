# app/history_cleaner.py

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup, Tag, NavigableString

logger = logging.getLogger(__name__)


def remove_history_sections(html_content: str, enabled: Optional[bool] = None) -> str:
    """
    Удаляет все разделы "История изменений" из HTML контента Confluence.

    Поддерживает различные форматы:
    1. Expand блоки с текстом "История изменений"
    2. Заголовки (h1-h6) со словами "История изменений"
    3. Параграфы с "История изменений:" + следующие за ними таблицы
    4. Таблицы с характерными заголовками (Дата, Описание, Автор, Задача в JIRA)

    Args:
        html_content: HTML контент страницы Confluence
        enabled: нужно ли вообще удалять историю. Если None (по умолчанию) —
            значение берётся динамически из app.config.REMOVE_HISTORY_SECTIONS,
            что позволяет скриптам переопределить его в рантайме (флаг --keep-history).
            Если False — HTML возвращается без изменений.

    Returns:
        Очищенный HTML контент без разделов истории изменений (или исходный HTML,
        если удаление отключено).
    """
    if enabled is None:
        # Читаем модуль динамически, чтобы видеть рантайм-переопределение
        # (CLI-флаг --keep-history выставляет app.config.REMOVE_HISTORY_SECTIONS).
        import app.config as _config
        enabled = _config.REMOVE_HISTORY_SECTIONS

    if not enabled:
        logger.debug("[remove_history_sections] Disabled — история не удаляется")
        return html_content

    if not html_content or not html_content.strip():
        return html_content

    logger.info("[remove_history_sections] <- html length: %d", len(html_content))

    soup = BeautifulSoup(html_content, 'html.parser')
    removed_sections = 0

    # 1. УДАЛЯЕМ EXPAND БЛОКИ С "ИСТОРИЯ ИЗМЕНЕНИЙ"
    removed_sections += _remove_expand_history_blocks(soup)

    # 2. УДАЛЯЕМ ЗАГОЛОВКИ "ИСТОРИЯ ИЗМЕНЕНИЙ" + СЛЕДУЮЩИЕ ТАБЛИЦЫ
    removed_sections += _remove_header_history_sections(soup)

    # 3. УДАЛЯЕМ ПАРАГРАФЫ "ИСТОРИЯ ИЗМЕНЕНИЙ:" + СЛЕДУЮЩИЕ ТАБЛИЦЫ
    removed_sections += _remove_paragraph_history_sections(soup)

    # 4. УДАЛЯЕМ ТАБЛИЦЫ С ХАРАКТЕРНЫМИ ЗАГОЛОВКАМИ ИСТОРИИ
    removed_sections += _remove_history_tables_by_headers(soup)

    cleaned_html = str(soup)

    logger.info("[remove_history_sections] -> Removed %d history sections, cleaned length: %d",
                removed_sections, len(cleaned_html))

    return cleaned_html


def _remove_expand_history_blocks(soup: BeautifulSoup) -> int:
    """
    Удаляет expand блоки, содержащие "История изменений".
    Паттерн 1: <div class="expand-container"><div class="expand-control"><span>История изменений</span>
    """
    removed_count = 0

    # Ищем все expand контейнеры
    expand_containers = soup.find_all('div', class_=lambda x: x and 'expand-container' in x)

    for container in expand_containers:
        # Ищем expand-control внутри контейнера
        expand_control = container.find('div', class_=lambda x: x and 'expand-control' in x)
        if not expand_control:
            continue

        # Проверяем текст в expand-control
        control_text = expand_control.get_text(strip=True).lower()
        if _is_history_title(control_text):
            logger.debug("[_remove_expand_history_blocks] Removing expand container: %s", control_text)
            container.extract()
            removed_count += 1

    return removed_count


def _remove_header_history_sections(soup: BeautifulSoup) -> int:
    """
    Удаляет заголовки (h1-h6) "История изменений" + следующие за ними таблицы.
    Паттерн 3: <h1>История изменений</h1><div class="table-wrap"><table>...
    """
    removed_count = 0

    # Ищем все заголовки
    headers = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])

    for header in headers:
        header_text = header.get_text(strip=True).lower()
        header_id = header.get('id', '').lower()

        # Проверяем текст заголовка или его ID
        if _is_history_title(header_text):  # id-якорь НЕ проверяется: несёт имя страницы (инцидент 2026-08-06)
            logger.debug("[_remove_header_history_sections] Removing header: %s", header_text)

            # Удаляем сам заголовок
            elements_to_remove = [header]

            # Ищем следующие элементы (таблицы, div с таблицами)
            next_elements = _get_following_history_elements(header)
            elements_to_remove.extend(next_elements)

            # Удаляем все найденные элементы
            for element in elements_to_remove:
                element.extract()

            removed_count += 1

    return removed_count


def _remove_paragraph_history_sections(soup: BeautifulSoup) -> int:
    """
    Удаляет параграфы "История изменений:" + следующие за ними таблицы.
    Паттерн 2: <p><strong>История изменений:</strong></p><div class="table-wrap">...
    """
    removed_count = 0

    # Ищем все параграфы
    paragraphs = soup.find_all('p')

    for p in paragraphs:
        p_text = p.get_text(strip=True).lower()

        if _is_history_title(p_text):
            logger.debug("[_remove_paragraph_history_sections] Removing paragraph: %s", p_text)

            # Удаляем сам параграф
            elements_to_remove = [p]

            # Ищем следующие элементы (таблицы, div с таблицами)
            next_elements = _get_following_history_elements(p)
            elements_to_remove.extend(next_elements)

            # Удаляем все найденные элементы
            for element in elements_to_remove:
                element.extract()

            removed_count += 1

    return removed_count


def _remove_history_tables_by_headers(soup: BeautifulSoup) -> int:
    """
    Удаляет таблицы с характерными заголовками истории изменений.
    Ищет таблицы с колонками: Дата, Описание, Автор, Задача в JIRA
    """
    removed_count = 0

    # Ищем все таблицы
    tables = soup.find_all('table')

    for table in tables:
        if _is_history_table(table):
            logger.debug("[_remove_history_tables_by_headers] Removing history table by headers")

            # Удаляем table-wrap контейнер, если он есть
            table_wrap = table.find_parent('div', class_=lambda x: x and 'table-wrap' in x)
            if table_wrap:
                table_wrap.extract()
            else:
                table.extract()

            removed_count += 1

    return removed_count


def _is_history_title(text: str) -> bool:
    """
    Строгий детектор заголовка секции истории: ТОЧНОЕ совпадение
    нормализованного текста со словарём канонических названий.

    Ни подстрока, ни startswith здесь недопустимы — два инцидента:
    • 2026-08-05, [БлокН2Н]: «…создание записи в сущности История изменений
      сообщения о блокировках Н2Н» в абзаце-требовании → удалён абзац и
      следующий div со всеми таблицами процесса (подстрока);
    • 2026-08-06, страница сущности «История изменений сообщения о
      блокировках Н2Н»: её имя входит в id-якоря ВСЕХ заголовков страницы
      (Confluence строит якорь как <Страница>-<Заголовок>) и в начало
      названий — страница выпотрошена целиком (подстрока по id, и startswith
      не спас бы).
    Удаляющая эвристика обязана иметь ограничитель на НЕсрабатывание
    (асимметрия: неудалённая история — шум; потерянное требование —
    невосстановимо). Цена точности безопасна: нераспознанная вариация
    названия оставит историю в выгрузке, а не потеряет содержимое.
    """
    if not text:
        return False

    text = re.sub(r"\s+", " ", text.lower()).strip().rstrip(":").strip()
    return text in (
        'история изменений',
        'история изменений требований',
        'история изменения',
        'change history',
        'revision history',
    )


def _is_history_text(text: str) -> bool:
    """
    Проверяет, содержит ли текст признаки истории изменений.

    ВНИМАНИЕ: подстрочный поиск — применять только к коротким «заголовочным»
    текстам (expand-контрол, h1–h6, ID якорей); для абзацев использовать
    строгий _is_history_heading_paragraph (см. его докстринг об инциденте).
    """
    if not text:
        return False

    text = text.lower().strip()

    # Точные совпадения
    exact_matches = [
        'история изменений',
        'история изменений:',
        'история изменений требований',
        'change history',
        'revision history'
    ]

    for match in exact_matches:
        if match in text:
            return True

    # Проверяем ID заголовков (может содержать дополнительные символы)
    if 'историяизменений' in text.replace(' ', '').replace('-', '').replace('_', ''):
        return True

    return False


def _is_history_table(table: Tag) -> bool:
    """
    Проверяет, является ли таблица таблицей истории изменений по её заголовкам.
    """
    # Ищем заголовки таблицы
    headers = []

    # Проверяем в thead
    thead = table.find('thead')
    if thead:
        header_cells = thead.find_all(['th', 'td'])
        headers.extend([cell.get_text(strip=True).lower() for cell in header_cells])

    # Если нет thead, проверяем первую строку tbody
    if not headers:
        tbody = table.find('tbody')
        if tbody:
            first_row = tbody.find('tr')
            if first_row:
                header_cells = first_row.find_all(['th', 'td'])
                headers.extend([cell.get_text(strip=True).lower() for cell in header_cells])

    # Если и tbody нет, проверяем первую строку таблицы
    if not headers:
        first_row = table.find('tr')
        if first_row:
            header_cells = first_row.find_all(['th', 'td'])
            headers.extend([cell.get_text(strip=True).lower() for cell in header_cells])

    if not headers:
        return False

    # Характерные заголовки истории изменений
    history_headers = {
        'дата', 'date',
        'описание', 'description', 'desc',
        'автор', 'author',
        'задача в jira', 'jira', 'ticket', 'issue',
        'версия', 'version',
        'изменения', 'changes'
    }

    # Считаем совпадения
    matches = 0
    for header in headers:
        header = header.strip()
        if any(hist_header in header for hist_header in history_headers):
            matches += 1

    # Если найдено 3+ характерных заголовка, считаем это таблицей истории
    is_history = matches >= 3

    if is_history:
        logger.debug("[_is_history_table] Detected history table with headers: %s (matches: %d)",
                     headers, matches)

    return is_history


def _get_following_history_elements(element: Tag) -> list:
    """
    Получает элементы, следующие за заголовком/параграфом истории изменений.
    Обычно это div с class="table-wrap" и/или table элементы.
    """
    following_elements = []

    # Ищем следующие элементы-соседи
    next_sibling = element.next_sibling

    while next_sibling:
        if isinstance(next_sibling, NavigableString):
            # Пропускаем текстовые узлы (пробелы, переносы)
            if next_sibling.strip():
                break  # Если есть значимый текст, останавливаемся
            next_sibling = next_sibling.next_sibling
            continue

        if isinstance(next_sibling, Tag):
            # Проверяем, относится ли элемент к истории изменений
            if _is_history_related_element(next_sibling):
                following_elements.append(next_sibling)
                next_sibling = next_sibling.next_sibling
            else:
                # Если элемент не относится к истории, останавливаемся
                break
        else:
            break

    return following_elements


def _is_history_related_element(element: Tag) -> bool:
    """
    Проверяет, относится ли элемент к истории изменений.
    """
    if not element or not element.name:
        return False

    # Div с table-wrap - обычно содержит таблицу истории
    if element.name == 'div':
        classes = element.get('class', [])
        if any('table-wrap' in str(cls) for cls in classes):
            return True

    # Прямая таблица
    if element.name == 'table':
        return True

    # Проверяем, содержит ли элемент таблицу истории
    tables = element.find_all('table')
    for table in tables:
        if _is_history_table(table):
            return True

    return False