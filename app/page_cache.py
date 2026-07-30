# app/page_cache.py

import logging
import time
import threading
from typing import Dict, Optional, List
from requests.exceptions import ConnectionError, RequestException
from http.client import RemoteDisconnected

import re

import markdownify
from bs4 import BeautifulSoup
from cachetools import TTLCache
from cachetools.keys import hashkey

from app.config import (
    PAGE_CACHE_SIZE,
    PAGE_CACHE_TTL,
    CONFLUENCE_BASE_URL,
    CONFLUENCE_USER,
    CONFLUENCE_PASSWORD,
)
from app.confluence_loader import confluence, extract_approved_fragments
from app.filter_all_fragments import filter_all_fragments
from app.history_cleaner import remove_history_sections
from app.services.template_type_analysis import analyze_content_template_type

logger = logging.getLogger(__name__)

_PAGE_ID_RE = re.compile(
    r'(?:[?&]pageId=|/pages/viewpage\.action\?pageId=|/wiki/spaces/[^/]+/pages/)(\d+)'
)


def _preprocess_confluence_links(html: str) -> str:
    """Заменяет внутренние ссылки Confluence на плейсхолдеры confluence://ID перед markdownify.

    Обрабатывает два вида ссылок из storage-формата Confluence:
    • <ac:link> — нативные внутренние ссылки на страницы
    • <a href="...?pageId=..."> — Confluence-URL с числовым идентификатором
    """
    soup = BeautifulSoup(html, "html.parser")

    for ac_link in soup.find_all("ac:link"):
        ri_page = ac_link.find("ri:page")
        if not ri_page:
            continue

        link_body = ac_link.find("ac:plain-text-link-body")
        text = (link_body.get_text(strip=True) if link_body
                else ri_page.get("ri:content-title") or ac_link.get_text(strip=True) or "")

        content_id = ri_page.get("ri:content-id")
        content_title = ri_page.get("ri:content-title")
        space_key = ri_page.get("ri:space-key", "")

        if content_id:
            href = f"confluence://{content_id}"
        elif content_title:
            # %2B экранирует литеральный '+' до кодирования пробелов как '+'
            # (симметрично _decode_title_path в migrate_confluence_tree).
            encoded = content_title.replace("+", "%2B").replace(" ", "+")
            href = (f"confluence://title/{space_key}/{encoded}"
                    if space_key else f"confluence://title/{encoded}")
        else:
            href = ""

        new_tag = soup.new_tag("a")
        if href:
            new_tag["href"] = href
        new_tag.string = text
        ac_link.replace_with(new_tag)

    for a_tag in soup.find_all("a", href=True):
        m = _PAGE_ID_RE.search(a_tag["href"])
        if m:
            a_tag["href"] = f"confluence://{m.group(1)}"

    return str(soup)

# Создаем TTL кэш: максимум PAGE_CACHE_SIZE элементов, время жизни PAGE_CACHE_TTL секунд
page_cache = TTLCache(maxsize=PAGE_CACHE_SIZE, ttl=PAGE_CACHE_TTL)
cache_lock = threading.RLock()

# Константы для retry-логики
MAX_RETRIES = 3
INITIAL_BACKOFF = 1  # секунды
MAX_BACKOFF = 8  # секунды


def _reconnect_confluence():
    """
    Переинициализация соединения с Confluence.
    Закрывает текущую сессию и создает новую.
    """
    try:
        if hasattr(confluence, 'session') and confluence.session:
            logger.debug("[_reconnect_confluence] Closing existing session")
            confluence.session.close()

        # Если у confluence есть метод переинициализации, используем его
        if hasattr(confluence, 'reinit_session'):
            confluence.reinit_session()
        elif hasattr(confluence, '_create_session'):
            confluence._create_session()
        else:
            # В противном случае просто создаем новую сессию
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            confluence.session = requests.Session()

            # Настройка retry strategy
            retry_strategy = Retry(
                total=2,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
            )

            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=10,
                pool_maxsize=20,
                pool_block=False
            )

            confluence.session.mount("https://", adapter)
            confluence.session.mount("http://", adapter)

            # Keep-alive headers
            if not hasattr(confluence.session, 'headers'):
                confluence.session.headers = {}
            confluence.session.headers.update({
                'Connection': 'keep-alive'
            })

            logger.info("[_reconnect_confluence] Session reconnected successfully")

    except Exception as e:
        logger.warning("[_reconnect_confluence] Failed to reconnect: %s", str(e))


def get_page_data_cached(page_id: str) -> Optional[Dict]:
    """
    Кешированная функция для получения всех данных страницы за один запрос.

    Включает retry-механизм для обработки обрывов соединения.
    Не кешируем None (неудачные результаты), чтобы избежать
    повторяющихся ошибок "нет данных" из-за закешированных неудач.

    Args:
        page_id: Идентификатор страницы

    Returns:
        Словарь с полными данными страницы или None при ошибке
    """
    logger.debug("[get_page_data_cached] <- page_id=%s", page_id)

    # Создаем ключ для кеша
    cache_key = hashkey(page_id)

    # Проверяем наличие в кеше
    with cache_lock:
        if cache_key in page_cache:
            cached_data = page_cache[cache_key]
            logger.debug("[get_page_data_cached] Cache HIT for page_id=%s", page_id)
            return cached_data

    logger.debug("[get_page_data_cached] Cache MISS for page_id=%s", page_id)

    # Загружаем данные с retry-механизмом
    last_error = None
    backoff = INITIAL_BACKOFF

    for attempt in range(MAX_RETRIES):
        try:
            # Единственный запрос к Confluence API
            page = confluence.get_page_by_id(page_id, expand='body.storage,title')

            if not page:
                logger.warning("[get_page_data_cached] Page not found: %s", page_id)
                return None  # НЕ кешируем None

            title = page.get('title', '')
            raw_html = page.get('body', {}).get('storage', {}).get('value', '')

            if not raw_html:
                logger.warning("[get_page_data_cached] No content found for page_id=%s", page_id)
                return None  # НЕ кешируем None

            # Один раз очищаем историю изменений; все дальнейшие обработчики
            # получают уже чистый HTML и не вызывают remove_history_sections повторно.
            cleaned_html = remove_history_sections(raw_html)

            # Все виды обработки HTML выполняем один раз
            full_content = filter_all_fragments(cleaned_html)
            full_markdown = markdownify.markdownify(
                _preprocess_confluence_links(cleaned_html), heading_style="ATX"
            )
            approved_content = extract_approved_fragments(cleaned_html)
            requirement_type = analyze_content_template_type(title, cleaned_html)

            result = {
                'id': page_id,
                'title': title,
                'raw_html': raw_html,
                'full_content': full_content,
                'full_markdown': full_markdown,
                'approved_content': approved_content,
                'requirement_type': requirement_type
            }

            # ТОЛЬКО успешные результаты кешируем
            with cache_lock:
                page_cache[cache_key] = result

            logger.debug("[get_page_data_cached] -> Processed and CACHED page: title='%s', type='%s'",
                         title, requirement_type)

            # Если были повторные попытки, логируем успех
            if attempt > 0:
                logger.info(
                    "[get_page_data_cached] Successfully retrieved page_id=%s after %d attempt(s)",
                    page_id, attempt + 1
                )

            return result

        except (ConnectionError, RemoteDisconnected, RequestException) as e:
            last_error = e

            if attempt < MAX_RETRIES - 1:
                # Не последняя попытка - retry
                logger.warning(
                    "[get_page_data_cached] Connection error for page_id=%s "
                    "(attempt %d/%d): %s. Retrying in %ds...",
                    page_id, attempt + 1, MAX_RETRIES, str(e), backoff
                )

                # Переинициализируем соединение
                _reconnect_confluence()

                # Ждем с экспоненциальной задержкой
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                # Последняя попытка - логируем ошибку
                logger.error(
                    "[get_page_data_cached] Failed to retrieve page_id=%s after %d attempts: %s",
                    page_id, MAX_RETRIES, str(e)
                )

        except Exception as e:
            # Другие ошибки (не связанные с соединением) не retry
            logger.error(
                "[get_page_data_cached] Error processing page_id=%s: %s",
                page_id, str(e)
            )
            return None  # НЕ кешируем ошибки

    # Если все попытки исчерпаны
    logger.error(
        "[get_page_data_cached] All retry attempts exhausted for page_id=%s. Last error: %s",
        page_id, str(last_error)
    )
    return None  # НЕ кешируем ошибки


# ----------------------------------------------------------------------------
# Прямой HTTP-доступ к Confluence (как браузер), в обход REST API.
# Используется, когда в закрытом окружении доступ к Confluence API закрыт,
# но страницы открываются в браузере.
# ----------------------------------------------------------------------------

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _confluence_http_get(url: str):
    """Выполняет браузероподобный HTTP GET к Confluence с basic-auth.

    Возвращает объект requests.Response (raise_for_status уже вызван)
    или None при ошибке запроса.
    """
    try:
        import requests as http_requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = http_requests.get(
            url,
            headers=_BROWSER_HEADERS,
            auth=(CONFLUENCE_USER, CONFLUENCE_PASSWORD),
            timeout=30,
            verify=False,
        )
        response.raise_for_status()
        return response
    except Exception as e:
        logger.error("[_confluence_http_get] HTTP request failed for url=%s: %s", url, str(e))
        return None


def _extract_title_from_soup(soup) -> str:
    """Извлекает заголовок страницы Confluence из HTML (viewpage.action)."""
    title_tag = soup.find(id="title-text")
    if title_tag:
        return title_tag.get_text(strip=True)
    html_title = soup.find("title")
    if html_title:
        # Confluence добавляет суффикс " - Confluence" или " - Space Name"
        return html_title.get_text(strip=True).rsplit(" - ", 1)[0].strip()
    return ""


def fetch_page_data_via_http(page_id: str) -> Optional[Dict]:
    """
    Загружает данные страницы Confluence через обычный HTTP-запрос (как браузер),
    без использования Confluence API.

    Используется как альтернатива get_page_data_cached() когда доступ к API закрыт,
    но страницы доступны через браузер.

    Выполняет те же операции обработки и кеширования, что и get_page_data_cached().

    Args:
        page_id: Идентификатор страницы Confluence

    Returns:
        Словарь с полными данными страницы или None при ошибке.
        Формат аналогичен get_page_data_cached().
    """
    logger.debug("[fetch_page_data_via_http] <- page_id=%s", page_id)

    # Проверяем кеш
    cache_key = hashkey(page_id)
    with cache_lock:
        if cache_key in page_cache:
            logger.debug("[fetch_page_data_via_http] Cache HIT for page_id=%s", page_id)
            return page_cache[cache_key]

    page_url = f"{CONFLUENCE_BASE_URL.rstrip('/')}/pages/viewpage.action?pageId={page_id}"

    response = _confluence_http_get(page_url)
    if response is None:
        logger.error(
            "[fetch_page_data_via_http] HTTP request failed for page_id=%s url=%s",
            page_id, page_url
        )
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # Извлекаем заголовок страницы
    title = _extract_title_from_soup(soup)

    if not title:
        logger.warning("[fetch_page_data_via_http] Could not extract title for page_id=%s", page_id)
        return None

    # Извлекаем содержимое страницы (основной блок wiki-content)
    content_tag = soup.find(class_="wiki-content")
    if not content_tag:
        content_tag = soup.find(id="main-content")
    if not content_tag:
        logger.warning(
            "[fetch_page_data_via_http] Could not find content area for page_id=%s", page_id
        )
        return None

    raw_html = str(content_tag)

    try:
        full_content = filter_all_fragments(raw_html)
        full_markdown = markdownify.markdownify(
            _preprocess_confluence_links(raw_html), heading_style="ATX"
        )
        approved_content = extract_approved_fragments(raw_html)
        requirement_type = analyze_content_template_type(title, raw_html)
    except Exception as e:
        logger.error(
            "[fetch_page_data_via_http] Error processing page content for page_id=%s: %s",
            page_id, str(e)
        )
        return None

    result = {
        'id': page_id,
        'title': title,
        'raw_html': raw_html,
        'full_content': full_content,
        'full_markdown': full_markdown,
        'approved_content': approved_content,
        'requirement_type': requirement_type,
    }

    with cache_lock:
        page_cache[cache_key] = result

    logger.info(
        "[fetch_page_data_via_http] -> Loaded and cached page_id=%s title='%s' type='%s'",
        page_id, title, requirement_type
    )
    return result


def fetch_page_title_via_http(page_id: str) -> Optional[str]:
    """
    Получает ТОЛЬКО заголовок страницы через прямой HTTP-доступ (как браузер).

    HTTP-аналог get_page_title_only() из confluence_loader: используется для
    страниц-контейнеров без собственного тела, когда нужен только заголовок
    (например, для проверки правил исключения при обходе дерева).
    """
    logger.debug("[fetch_page_title_via_http] <- page_id=%s", page_id)

    # Если страница уже в кеше — берём заголовок оттуда, без лишнего запроса.
    cache_key = hashkey(page_id)
    with cache_lock:
        if cache_key in page_cache:
            return page_cache[cache_key].get("title")

    url = f"{CONFLUENCE_BASE_URL.rstrip('/')}/pages/viewpage.action?pageId={page_id}"
    response = _confluence_http_get(url)
    if response is None:
        return None

    title = _extract_title_from_soup(BeautifulSoup(response.text, "html.parser"))
    if not title:
        logger.warning("[fetch_page_title_via_http] Could not extract title for page_id=%s", page_id)
        return None

    logger.debug("[fetch_page_title_via_http] -> title='%s'", title)
    return title


def fetch_child_pages_via_http(page_id: str) -> List[Dict]:
    """
    Возвращает прямых потомков страницы через прямой HTTP-доступ, в обход REST API.

    HTTP-аналог confluence.get_child_pages(): использует браузерный endpoint
    /pages/children.action — тот же, что дерево страниц вызывает в браузере.
    Он отдаёт JSON-массив прямых дочерних страниц с явными pageId и заголовком,
    например:
        [{"text": "...", "href": "/pages/viewpage.action?pageId=NNN",
          "pageId": "NNN", "nodeClass": "closed", ...}, ...]

    Возвращает список словарей [{"id": ..., "title": ...}], совместимый по форме
    с confluence.get_child_pages(). Пустой список — если потомков нет или endpoint
    недоступен. Рекурсию по дереву обеспечивает вызывающий код (migrate_subtree).
    """
    logger.debug("[fetch_child_pages_via_http] <- page_id=%s", page_id)

    base = CONFLUENCE_BASE_URL.rstrip("/")
    url = f"{base}/pages/children.action?pageId={page_id}"

    response = _confluence_http_get(url)
    if response is None:
        logger.warning("[fetch_child_pages_via_http] No response for page_id=%s", page_id)
        return []

    try:
        items = response.json()
    except Exception as e:
        logger.error(
            "[fetch_child_pages_via_http] Failed to parse JSON for page_id=%s: %s",
            page_id, str(e),
        )
        return []

    if not isinstance(items, list):
        logger.warning(
            "[fetch_child_pages_via_http] Unexpected response shape for page_id=%s: %s",
            page_id, type(items).__name__,
        )
        return []

    children = []
    skipped_no_id = 0
    for item in items:
        child_id = str(item.get("pageId") or "").strip()
        if not child_id:
            skipped_no_id += 1
            continue
        children.append({"id": child_id, "title": (item.get("text") or "").strip()})

    if skipped_no_id:
        logger.debug(
            "[fetch_child_pages_via_http] page_id=%s: пропущено записей без pageId=%d",
            page_id, skipped_no_id,
        )
    logger.info(
        "[fetch_child_pages_via_http] -> page_id=%s: найдено потомков=%d",
        page_id, len(children),
    )
    return children


def get_page_data(page_id: str, use_http: bool = False) -> Optional[Dict]:
    """
    Единая точка получения данных страницы Confluence.

    use_http=False — через Confluence REST API (get_page_data_cached);
    use_http=True  — через прямой HTTP-доступ как браузер (fetch_page_data_via_http).

    Обе ветки используют общий page_cache и возвращают словарь одинакового
    формата, поэтому дальнейшая работа со страницей не зависит от источника.
    """
    if use_http:
        return fetch_page_data_via_http(page_id)
    return get_page_data_cached(page_id)


def clear_page_cache():
    """Очистка кеша страниц"""
    with cache_lock:
        page_cache.clear()
    logger.info("[clear_page_cache] Page cache cleared")


def get_cache_info():
    """Информация о состоянии кеша"""
    with cache_lock:
        current_size = len(page_cache)

    logger.info("[get_cache_info] Cache stats: size=%d, max_size=%d",
                current_size, PAGE_CACHE_SIZE)
    return {
        'current_size': current_size,
        'max_size': PAGE_CACHE_SIZE,
        'ttl': PAGE_CACHE_TTL
    }

# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ для /analyze_external
# ============================================================================

def process_and_cache_external_page(page_id: str, title: str, raw_html: str) -> dict:
    """
    Обрабатывает одну внешнюю страницу и помещает её в кеш.

    Выполняет те же операции, что и get_page_data_cached(), но использует
    уже полученные данные вместо запроса к Confluence API.

    Args:
        page_id: Идентификатор страницы
        title: Заголовок страницы
        raw_html: HTML содержимое страницы

    Returns:
        dict с результатом: {'success': bool, 'page_id': str, 'error': str}
    """
    try:
        logger.debug("[process_and_cache_external_page] Processing page_id=%s, title='%s'",
                     page_id, title)

        # Выполняем все виды обработки HTML (как в get_page_data_cached)
        full_content = filter_all_fragments(raw_html)
        full_markdown = markdownify.markdownify(raw_html, heading_style="ATX")

        # Импортируем здесь, чтобы избежать циклических зависимостей
        from app.filter_approved_fragments import filter_approved_fragments
        approved_content = filter_approved_fragments(raw_html)

        # Определяем тип требования
        requirement_type = analyze_content_template_type(title, raw_html)

        # Формируем результат в том же формате, что и get_page_data_cached
        result = {
            'id': page_id,
            'title': title,
            'raw_html': raw_html,
            'full_content': full_content,
            'full_markdown': full_markdown,
            'approved_content': approved_content,
            'requirement_type': requirement_type
        }

        # Помещаем в кеш (перезаписываем если существует)
        cache_key = hashkey(page_id)
        with cache_lock:
            page_cache[cache_key] = result

        logger.debug("[process_and_cache_external_page] Successfully cached page_id=%s, type='%s'",
                     page_id, requirement_type)

        return {
            'success': True,
            'page_id': page_id,
            'error': None
        }

    except Exception as e:
        error_msg = f"Error processing page: {str(e)}"
        logger.error("[process_and_cache_external_page] Failed to process page_id=%s: %s",
                     page_id, error_msg, exc_info=True)

        return {
            'success': False,
            'page_id': page_id,
            'error': error_msg
        }


def process_and_cache_external_pages(pages: List[Dict[str, str]]) -> dict:
    """
    Обрабатывает список внешних страниц и помещает их в кеш.

    Args:
        pages: Список словарей с ключами 'page_id', 'title', 'content'

    Returns:
        dict со статистикой: {
            'total': int,
            'cached': int,
            'failed': int,
            'failed_pages': list
        }
    """
    logger.info("[process_and_cache_external_pages] <- Processing %d pages", len(pages))

    total = len(pages)
    cached = 0
    failed = 0
    failed_pages = []

    for page_data in pages:
        page_id = page_data.get('page_id')
        title = page_data.get('title')
        content = page_data.get('content')

        if not all([page_id, title, content]):
            error_msg = "Missing required fields (page_id, title, or content)"
            logger.warning("[process_and_cache_external_pages] %s for page_id=%s",
                           error_msg, page_id)
            failed += 1
            failed_pages.append({
                'page_id': page_id or 'unknown',
                'error': error_msg
            })
            continue

        result = process_and_cache_external_page(page_id, title, content)

        if result['success']:
            cached += 1
        else:
            failed += 1
            failed_pages.append({
                'page_id': result['page_id'],
                'error': result['error']
            })

    logger.info("[process_and_cache_external_pages] -> Cached: %d/%d, Failed: %d",
                cached, total, failed)

    return {
        'total': total,
        'cached': cached,
        'failed': failed,
        'failed_pages': failed_pages
    }