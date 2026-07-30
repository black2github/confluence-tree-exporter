# app/service_registry.py

import json
import logging
import os
from typing import List, Dict
from app.config import SERVICES_REGISTRY_FILE

logger = logging.getLogger(__name__)

def load_services() -> List[Dict]:
    try:
        service_file_path = os.path.join(os.path.dirname(__file__), "data", SERVICES_REGISTRY_FILE)
        logger.debug("[load_services] <- %s", service_file_path)
        with open(service_file_path, encoding="utf-8") as f:
            l = json.load(f)
            logger.debug("[load_services] loaded: %s", l)
            # return json.load(f)
            return l
    except Exception as e:
        logging.exception("Ошибка при чтении services.json: {%s}", e)
        return []


def get_service_by_code(code: str) -> Dict:
    logger.debug("[get_service_by_code] <- code='%s'", code)
    services = load_services()

    for service in services:
        logger.debug("[get_service_by_code] test '%s'", service["code"])
        if service["code"] == code:
            return service
    logger.warning("[get_service_by_code] -> None")
    return {}


def get_platform_services() -> List[dict]:
    return [s for s in load_services() if s.get("platform") is True]


def is_valid_service(code: str) -> bool:
    return get_service_by_code(code) != {}


def is_platform_service(service_code: str) -> bool:
    """
    Проверяет, является ли сервис платформенным по коду.
    Возвращает True, если найден и platform=true, иначе False.
    """
    services = load_services()
    for svc in services:
        if svc.get("code") == service_code:
            return svc.get("platform", False)
    return False


def get_platform_status(service_code: str) -> bool:
    """
    Возвращает статус платформенности сервиса.
    Используется при создании метаданных для единого хранилища.
    """
    return is_platform_service(service_code)


# Заглушка — заменить на авторизацию через текущего пользователя
def resolve_service_code_by_user() -> str:
    # TODO: интеграция с пользователем
    return "CC" # Default

# Проверка, был ли page_id уже ранее сохранен в индекс и имеет ли привязанный сервис
def resolve_service_code_from_pages_or_user(page_ids: List[str]) -> str:
    from app.embedding_store import get_vectorstore
    from app.config import UNIFIED_STORAGE_NAME

    store = get_vectorstore(UNIFIED_STORAGE_NAME)
    for pid in page_ids:
        matches = store.similarity_search("", filter={"page_id": pid})
        if matches:
            metadata = matches[0].metadata
            if "service_code" in metadata:
                return metadata["service_code"]

    return resolve_service_code_by_user()