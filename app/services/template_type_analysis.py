# app/services/template_type_analysis.py

import json
import logging
import os
import re
from typing import List, Optional, Dict, Any
from app.confluence_loader import get_page_content_by_id, get_page_title_by_id
from app.filter_all_fragments import filter_all_fragments

logger = logging.getLogger(__name__)

FEATURES_FILE = "features.json"
MASTER_SYSTEMS_FILE = "master_systems.json"


class TemplateTypeAnalyzer:
    """Анализатор типов шаблонов требований для страниц Confluence"""

    def __init__(self):
        self.features = self._load_features()
        self.master_systems = self._load_master_systems()

    def _load_features(self) -> Dict[str, Any]:
        """Загружает конфигурацию типов шаблонов из features.json"""
        try:
            features_path = os.path.join(os.path.dirname(__file__), "..", "data", FEATURES_FILE)
            with open(features_path, 'r', encoding='utf-8') as f:
                features = json.load(f)
                logger.info("[TemplateTypeAnalyzer] Loaded %d template types from features.json", len(features))
                return features
        except Exception as e:
            logger.error("[TemplateTypeAnalyzer] Failed to load features.json: %s", str(e))
            return {}

    def _load_master_systems(self) -> List[str]:
        """Загружает список master_system из master_systems.json"""
        try:
            master_systems_path = os.path.join(os.path.dirname(__file__), "..", "data", MASTER_SYSTEMS_FILE)
            with open(master_systems_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Извлекаем значения master_system из всех доменов
                systems = [domain.get("master_system", "") for domain in data.get("data_domains", [])]
                # Фильтруем пустые значения и приводим к lowercase для поиска
                systems = [s.lower() for s in systems if s]
                logger.info("[TemplateTypeAnalyzer] Loaded %d master systems: %s", len(systems), systems)
                return systems
        except Exception as e:
            logger.error("[TemplateTypeAnalyzer] Failed to load master_systems.json: %s", str(e))
            return []

    def analyze_content_type(self, page_title: str, page_html: str) -> Optional[str]:
        """
        Определяет тип шаблона для одной страницы Confluence.
        Args:
            page_html: HTML содержимое страницы
            page_title: Наименование страницы
        Returns:
            Название типа шаблона или None если не определен
        """
        logger.info("[analyze_content_type] <- page_title: %s", page_title)

        if not page_title or not page_html:
            logger.warning("[analyze_content_type] -> Page title or content is None")
            return None

        # Получаем текстовое содержимое страницы
        page_content = filter_all_fragments(page_html)

        logger.debug("[analyze_content_type] Page title: '%s'", page_title)
        logger.debug("[analyze_content_type] Page content length: %d chars", len(page_content))

        # Проверяем каждый тип шаблона
        for template_type, template_config in self.features.items():
            logger.debug("[analyze_content_type] Checking template type: '%s'", template_type)

            if self._check_template_match(page_title, page_content, template_config):
                # ИСПРАВЛЕНИЕ: Обрезаем пробелы из ключа features.json
                cleaned_type = template_type.strip()
                logger.info("[analyze_content_type] -> Found match: '%s' (cleaned: '%s')",
                           template_type, cleaned_type)
                return cleaned_type

        # Fallback: пытаемся определить тип из заголовка
        fallback_type = self._guess_type_from_title(page_title)
        if fallback_type:
            logger.info("[analyze_content_type] -> Fallback match from title: '%s'", fallback_type)
            return fallback_type

        logger.info("[analyze_content_type] -> No template match found")
        return None

    def _guess_type_from_title(self, title: str) -> Optional[str]:
        """
        Пытается определить тип требования из заголовка страницы (fallback).

        Используется когда структурная проверка не сработала.
        """
        title_lower = title.lower()

        # Проверка наличия master_system в заголовке (приоритет для интеграций)
        if self.master_systems:
            for system in self.master_systems:
                if system in title_lower:
                    logger.debug("[_guess_type_from_title] Matched 'integration' by master_system '%s'",
                                 system)
                    return "integration"

        # Паттерны для определения типа (эту часть можно решить через features.json)
        # Похоже стоит эту проверку выполнять именно в конце, когда остальные варианты не помогли определить тип
        patterns = {
            "integration": ["rest", "soap", "api", "адаптер"],
            "dataModel": ["модель данных", "сущность", "мд"],
            "function": ["функция", "клиент:", "банк:"],
            "process": ["сквозной бизнес", "процесс", "алгоритм", "жц заявки"],
            "states": ["статус", "состояни"],
            "control": ["контрол"],
            "notification": ["нотификац"],
            "printForm": ["пф", "печатная форма"],
            "screenListForm": ["список", "списка", "журнал", "фильтр", "заявок"],
            "screenItemForm": ["эф клиента", "эф банка", "форма", "страница"]
        }

        for req_type, keywords in patterns.items():
            for keyword in keywords:
                if keyword in title_lower:
                    logger.debug("[_guess_type_from_title] Matched '%s' by keyword '%s'",
                                 req_type, keyword)
                    return req_type

        return None

    def analyze_page_type(self, page_id: str) -> Optional[str]:
        """
        Определяет тип шаблона для одной страницы Confluence.
        Args:
            page_id: Идентификатор страницы
        Returns:
            Название типа шаблона или None если не определен
        """
        logger.info("[analyze_page_type] <- page_id: %s", page_id)

        if not self.features:
            logger.warning("[analyze_page_type] No features loaded, returning None")
            return None

        # Получаем данные страницы (заголовок и содержимое)
        page_title = get_page_title_by_id(page_id)
        page_html = get_page_content_by_id(page_id, clean_html=False)

        template_type = self.analyze_content_type(page_title, page_html)

        # ИСПРАВЛЕНИЕ: Безопасный возврат с проверкой на None
        if template_type:
            logger.info("[analyze_page_type] -> Found match: '%s'", template_type)
            return template_type.strip()
        else:
            logger.info("[analyze_page_type] -> No template match found")
            return None

    def analyze_pages_types(self, page_ids: List[str]) -> List[Optional[str]]:
        """
        Определяет типы шаблонов для списка страниц

        Args:
            page_ids: Список идентификаторов страниц

        Returns:
            Список типов шаблонов (или None для каждой страницы)
        """
        logger.info("[analyze_pages_types] <- pages = '%s'", page_ids)

        results = []
        for page_id in page_ids:
            try:
                template_type = self.analyze_page_type(page_id)
                results.append(template_type)
            except Exception as e:
                logger.error("[analyze_pages_types] Error analyzing page %s: %s", page_id, str(e))
                results.append(None)

        logger.info("[analyze_pages_types] -> Completed analysis of %d pages", len(results))
        return results

    def _check_template_match(self, page_title: str, page_content: str, template_config: Dict) -> bool:
        """
        Проверяет соответствие страницы шаблону

        Args:
            page_title: Заголовок страницы
            page_content: Содержимое страницы
            template_config: Конфигурация шаблона

        Returns:
            True если страница соответствует шаблону
        """
        logger.debug("[check_template_match] <- page_title: '%s'", page_title)
        # 1. Проверка названия страницы
        if not self._check_title_match(page_title, template_config.get("title")):
            logger.debug("[_check_template_match] Title check failed")
            return False

        # 2. Проверка заголовков
        if not self._check_headers_match(page_content, template_config.get("headers")):
            logger.debug("[_check_template_match] Headers check failed")
            return False

        # 3. Проверка содержимого
        if not self._check_content_match(page_content, template_config.get("content")):
            logger.debug("[_check_template_match] Content check failed")
            return False

        logger.debug("[_check_template_match] All checks passed")
        return True

    def _check_title_match(self, page_title: str, title_synonyms: Optional[List[str]]) -> bool:
        """Проверяет соответствие названия страницы"""
        if title_synonyms is None:
            logger.debug("[_check_title_match] Title check skipped (null)")
            return True

        page_title_lower = page_title.lower()
        for synonym in title_synonyms:
            if synonym.lower() in page_title_lower:
                logger.debug("[_check_title_match] Title match found: '%s' in '%s'", synonym, page_title)
                return True

        logger.debug("[_check_title_match] No title match found")
        return False

    def _check_headers_match(self, page_content: str, headers_config: Optional[List[List[str]]]) -> bool:
        """Проверяет соответствие заголовков страницы"""
        if headers_config is None:
            logger.debug("[_check_headers_match] Headers check skipped (null)")
            return True

        # Извлекаем все заголовки из содержимого
        headers = self._extract_headers(page_content)
        headers_lower = [h.lower() for h in headers]

        logger.debug("[_check_headers_match] Found %d headers: %s", len(headers), headers)

        # Для каждого набора синонимов должен найтись хотя бы один заголовок
        for synonym_group in headers_config:
            group_found = False
            for synonym in synonym_group:
                if any(synonym.lower() in header for header in headers_lower):
                    logger.debug("[_check_headers_match] Header group match: '%s'", synonym)
                    group_found = True
                    break

            if not group_found:
                logger.debug("[_check_headers_match] Header group not found: %s", synonym_group)
                return False

        logger.debug("[_check_headers_match] All header groups matched")
        return True

    def _check_content_match(self, page_content: str, content_config: Optional[List[List[str]]]) -> bool:
        """Проверяет соответствие содержимого страницы"""
        if content_config is None:
            logger.debug("[_check_content_match] Content check skipped (null)")
            return True

        page_content_lower = page_content.lower()

        # Для каждого набора синонимов должен найтись хотя бы один в тексте
        for synonym_group in content_config:
            group_found = False
            for synonym in synonym_group:
                if synonym.lower() in page_content_lower:
                    logger.debug("[_check_content_match] Content group match: '%s'", synonym)
                    group_found = True
                    break

            if not group_found:
                logger.debug("[_check_content_match] Content group not found: %s", synonym_group)
                return False

        logger.debug("[_check_content_match] All content groups matched")
        return True

    def _extract_headers(self, content: str) -> List[str]:
        """
        Извлекает заголовки из markdown содержимого
        Ищет строки, начинающиеся с # (markdown заголовки)
        """
        headers = []
        lines = content.split('\n')

        for line in lines:
            line = line.strip()
            # Markdown заголовки
            if line.startswith('#'):
                # Убираем символы # и пробелы
                header_text = re.sub(r'^#+\s*', '', line).strip()
                if header_text:
                    headers.append(header_text)
            # Также ищем строки с **Заголовок:** (жирный текст)
            elif line.startswith('**') and line.endswith('**'):
                header_text = line.strip('*').strip()
                if header_text:
                    headers.append(header_text)

        return headers


# Глобальный экземпляр анализатора
_analyzer = TemplateTypeAnalyzer()


def analyze_content_template_type(page_title: str, page_html) -> Optional[str]:
    """Функция-обертка для анализа одной страницы"""
    return _analyzer.analyze_content_type(page_title, page_html)


def analyze_page_template_type(page_id: str) -> Optional[str]:
    """Функция-обертка для анализа одной страницы"""
    return _analyzer.analyze_page_type(page_id)


def analyze_pages_template_types(page_ids: List[str]) -> List[Optional[str]]:
    """Функция-обертка для анализа нескольких страниц"""
    return _analyzer.analyze_pages_types(page_ids)


def get_template_name_by_type(template_type: str) -> str:
    """
    Получает человекочитаемое название типа шаблона по его коду.

    Args:
        template_type: Код типа шаблона (например, "dataModel")

    Returns:
        Название типа шаблона (например, "Модель данных") или сам код если не найден
    """
    if not template_type:
        return "Неизвестный тип"

    # Используем уже существующий анализатор
    global _analyzer
    if not _analyzer.features:
        logger.warning("[get_template_name_by_type] Features not loaded")
        return template_type.strip() if template_type else template_type

    # ИСПРАВЛЕНИЕ: Обрезаем пробелы при поиске в словаре
    cleaned_type = template_type.strip() if template_type else template_type
    template_config = _analyzer.features.get(cleaned_type)
    if template_config and "name" in template_config:
        return template_config["name"]

    logger.debug("[get_template_name_by_type] No name found for type '%s'", cleaned_type)
    return cleaned_type


def perform_legacy_structure_check(template_html: str, content: str) -> List[str]:
    """
    Выполняет быструю структурную проверку (legacy код для обратной совместимости).
    Ожидает на вход шаблон и проверяемую страницу в формате HTML.
    Проверяет:
        - совпадение заголовков до 3-го уровня включительно;
        - совпадение числа таблиц на странице и шаблоне.
    """
    try:
        logger.debug("[_perform_legacy_structure_check] <- Legacy structure check")
        from markdownify import markdownify
        from bs4 import BeautifulSoup

        template_md = markdownify(template_html, heading_style="ATX")
        content_md = markdownify(content, heading_style="ATX")
        template_soup = BeautifulSoup(template_md, 'html.parser')
        content_soup = BeautifulSoup(content_md, 'html.parser')

        formatting_issues = []

        # Проверка заголовков
        template_headers = [h.get_text().strip() for h in template_soup.find_all(['h1', 'h2', 'h3'])]
        content_headers = [h.get_text().strip() for h in content_soup.find_all(['h1', 'h2', 'h3'])]
        if set(template_headers) != set(content_headers):
            formatting_issues.append(
                f"Несоответствие заголовков: ожидаются {template_headers}, найдены {content_headers}")

        # Проверка таблиц
        template_tables = template_soup.find_all('table')
        content_tables = content_soup.find_all('table')
        if len(template_tables) != len(content_tables):
            formatting_issues.append(
                f"Несоответствие количества таблиц: ожидается {len(template_tables)}, найдено {len(content_tables)}")

        logger.debug("[_perform_legacy_structure_check] -> formatting issues = '%s'", formatting_issues)
        return formatting_issues

    except Exception as e:
        logger.warning("[_perform_legacy_structure_check] Error in legacy check: %s", str(e))
        return [f"Ошибка структурной проверки: {str(e)}"]