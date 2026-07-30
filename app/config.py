# app/config.py

import os
from dotenv import load_dotenv
import anyio

# Устанавливаем лимиты для anyio только если в async контексте
try:
    anyio.to_thread.current_default_thread_limiter().total_tokens = 50
except Exception:
    # Скрипты запускаются синхронно - пропускаем
    pass

load_dotenv()

APP_VERSION = os.getenv("APP_VERSION", "0.91.0")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL")
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
KIMI_API_URL = os.getenv("KIMI_API_URL")
KIMI_API_KEY = os.getenv("KIMI_API_KEY")
GEMINI_API_URL = os.getenv("GEMINI_API_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_API_URL = os.getenv("XAI_API_URL")
QWEN_API_KEY = os.getenv("QWEN_API_KEY")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL")

CONFLUENCE_API_TOKEN = os.getenv("CONFLUENCE_API_TOKEN")
CONFLUENCE_USER = os.getenv("CONFLUENCE_USER")
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL")
CONFLUENCE_PASSWORD = os.getenv("CONFLUENCE_PASSWORD")

# Если доступ к Confluence через REST API закрыт (закрытое окружение), но страницы
# доступны через браузер, переключаемся на прямой HTTP-доступ (как в браузере).
# Скрипты миграции/дампа также понимают флаг командной строки --http,
# который переопределяет это значение.
# Можно глобально включить HTTP-режим в закрытом окружении. Флаг --http переопределяет конфиг.
CONFLUENCE_USE_HTTP = os.getenv("CONFLUENCE_USE_HTTP", "false").lower() in ("1", "true", "yes")

# При миграции в Markdown по умолчанию сохраняются только подтверждённые ("чёрные")
# фрагменты. Если включить — сохраняется ВСЁ содержимое страниц (все фрагменты
# независимо от цвета), с теми же правилами преобразования. Скрипты миграции также
# понимают флаг командной строки --all, который переопределяет это значение.
MIGRATE_INCLUDE_UNAPPROVED = os.getenv("MIGRATE_INCLUDE_UNAPPROVED", "false").lower() in ("1", "true", "yes")

# По умолчанию из начала документов вычищается таблица "История изменений".
# Если выключить — раздел истории сохраняется в выгрузке. Скрипты миграции также
# понимают флаг --keep-history, который переопределяет это значение на время запуска.
# ВНИМАНИЕ: значение читается динамически (через app.config.REMOVE_HISTORY_SECTIONS)
# в remove_history_sections(), поэтому CLI-флаг может переопределить его в рантайме.
REMOVE_HISTORY_SECTIONS = os.getenv("REMOVE_HISTORY_SECTIONS", "true").lower() in ("1", "true", "yes")

# По умолчанию картинки (<ac:image>) из текста страниц НЕ мигрируются — выкидываются.
# Если включить — вложения-картинки скачиваются в подкаталог img/ рядом с .md, а в
# тексте остаётся HTML-тег <img> с относительной ссылкой и сохранёнными размерами.
# Скрипты миграции также понимают флаг --with-images, переопределяющий это значение.
# ВНИМАНИЕ: значение читается динамически (через app.config.MIGRATE_IMAGES) в фабриках
# экстракторов content_extractor, поэтому CLI-флаг может переопределить его в рантайме.
MIGRATE_IMAGES = os.getenv("MIGRATE_IMAGES", "false").lower() in ("1", "true", "yes")

# По умолчанию зачёркнутый (<s>) текст следует общей логике: в approved-режиме
# выкидывается, под --all — сохраняется как часть неутверждённого содержимого.
# Если включить — зачёркнутый текст исключается ПРИ ЛЮБОМ раскладе (в т.ч. под --all).
# Скрипты миграции также понимают флаг --drop-strikethrough, переопределяющий это.
# ВНИМАНИЕ: значение читается динамически (через app.config.EXCLUDE_STRIKETHROUGH) в
# фабриках экстракторов content_extractor, поэтому CLI-флаг переопределяет его в рантайме.
EXCLUDE_STRIKETHROUGH = os.getenv("EXCLUDE_STRIKETHROUGH", "false").lower() in ("1", "true", "yes")

# ДОБАВЛЯЕМ конфигурацию JIRA
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://jira.gboteam.ru")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")  # Альтернатива паролю

LLM_PROVIDER = os.getenv("LLM_PROVIDER")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4") # gpt-3.5-turbo, gpt-3.5-turbo-16k, gpt-4-32k...
LLM_TEMPERATURE = os.getenv("LLM_TEMPERATURE", "0.2")
AGENT_MODEL = os.getenv("AGENT_MODEL")
AGENT_TEMPERATURE = os.getenv("AGENT_TEMPERATURE", "0.2")
LLM_CONTEXT_SIZE = 128000

# ============================================================================
# НАСТРОЙКИ ЭМБЕДДИНГОВ
# ============================================================================
# Провайдер: openai | huggingface
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "huggingface")

# Модель эмбеддингов.
# sentence-transformers/all-MiniLM-L6-v2 - стандартная и тупая.
# deepvk/USER2-base — специализированная модель для русского языка с поддержкой
# контекста до 8192 токенов. Требует task-specific префиксы при кодировании
# (обрабатывается автоматически в llm_interface.get_embeddings_model).
# При смене модели необходима полная переиндексация ChromaDB.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "deepvk/USER2-base")

# Модели с асимметричными префиксами для retrieval (search_query / search_document).
# Перечислены через запятую. get_embeddings_model() проверяет принадлежность
# текущей EMBEDDING_MODEL к этому списку и включает поддержку префиксов.
EMBEDDING_MODELS_WITH_PREFIXES = os.getenv(
    "EMBEDDING_MODELS_WITH_PREFIXES",
    "deepvk/USER2-base,deepvk/USER2-large,intfloat/multilingual-e5-base,intfloat/multilingual-e5-large"
)

# Размерность векторов. Для USER2-base = 768.
# При использовании Matryoshka Representation Learning (MRL) можно задать
# меньшее значение из ряда [32, 64, 128, 256, 384, 512, 768] —
# это сократит размер индекса ChromaDB с минимальной потерей качества.
# ВАЖНО: при изменении размерности необходима полная переиндексация!!!.
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "768"))
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "cpu")
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "64"))
# Максимальная длина последовательности в токенах при векторизации.
# USER2-base поддерживает до 8192 токенов, но attention mask растёт как seq_len^2:
#   seq_len=128,  batch=32: ~24 MB   (title — заголовки редко > 80 токенов)
#   seq_len=512,  batch=16: ~185 MB  (summary — первые 500 символов контента)
#   seq_len=2048, batch=8:  ~1.5 GB  (content/chunk — полный текст требования)
# Значения по умолчанию рассчитаны под L4 GPU (24 GB) в Google Colab.
# Для CPU-режима все три значения автоматически ограничены до EMBEDDING_MAX_SEQ_LENGTH.
EMBEDDING_MAX_SEQ_LENGTH = int(os.getenv("EMBEDDING_MAX_SEQ_LENGTH", "512"))
EMBEDDING_MAX_SEQ_LENGTH_TITLE = int(os.getenv("EMBEDDING_MAX_SEQ_LENGTH_TITLE", "128"))
EMBEDDING_MAX_SEQ_LENGTH_SUMMARY = int(os.getenv("EMBEDDING_MAX_SEQ_LENGTH_SUMMARY", "512"))
EMBEDDING_MAX_SEQ_LENGTH_CONTENT = int(os.getenv("EMBEDDING_MAX_SEQ_LENGTH_CONTENT", "2048"))
# Размер батча подобран под соответствующий seq_len на T4 GPU (14.5 GB).
# Память на attention растёт как batch * seq_len^2, поэтому при меньшем seq_len
# можно использовать значительно больший батч — это ускоряет векторизацию.
EMBEDDING_BATCH_SIZE_TITLE = int(os.getenv("EMBEDDING_BATCH_SIZE_TITLE", "128"))
EMBEDDING_BATCH_SIZE_SUMMARY = int(os.getenv("EMBEDDING_BATCH_SIZE_SUMMARY", "32"))
EMBEDDING_BATCH_SIZE_CONTENT = int(os.getenv("EMBEDDING_BATCH_SIZE_CONTENT", "8"))

CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./store/chroma_db")

PAGE_ANALYSIS_PROMPT_FILE = os.getenv("PAGE_ANALYSIS_PROMPT_FILE", "page_prompt_template.txt")
TEMPLATE_ANALYSIS_PROMPT_FILE = os.getenv("TEMPLATE_ANALYSIS_PROMPT_FILE", "template-analysis-prompt.txt")

# Название единого хранилища
UNIFIED_STORAGE_NAME = "unified_requirements"

SERVICES_REGISTRY_FILE = os.getenv("SERVICES_REGISTRY_FILE", "services.json")
TEMPLATES_REGISTRY_FILE = os.getenv("TEMPLATES_REGISTRY_FILE", "templates.json")
PAGE_EXCLUSION_RULES_FILE = os.getenv("PAGE_EXCLUSION_RULES_FILE", "app/data/page_exclusion_rules.json")

PAGE_CACHE_TTL = int(os.getenv("PAGE_CACHE_TTL", "60"))  # по умолчанию 5 минут
PAGE_CACHE_SIZE = int(os.getenv("PAGE_CACHE_SIZE", "1000")) # по умолчанию 1000 странниц

# Chunking нужен, только если:
# - Страницы > 2-3k токенов
# - Используем маленькую LLM с малым контекстом
# - Нужен очень точный поиск специфичных деталей
# CHUNK_MODE: # "none", "fixed", "adaptive"
#   "none" - целые страницы,
#   "fixed" - разбиение на фрагменты,
#   "adaptive" - Целая страница + фрагменты с высоким overlap
#              Для точного поиска используем фрагменты
#              Для анализа подтягиваем целую страницу по page_id
CHUNK_MODE="none"
CHUNK_MAX_PAGE_SIZE=15000  # Количество символов. Максимальный размер страницы, после которого она разбивается на
# чанки для адаптивной стратегии
CHUNK_SIZE=1500     # символов
CHUNK_OVERLAP=200   # символов

#
# Настройки построения контекста
#
# ТОЧНЫЕ совпадения по именам сущностей по всему хранилищу
IS_ENTITY_NAMES_CONTEXT="True"
# Сервисные документы
IS_SERVICE_DOCS_CONTEXT="True"
# Платформенные (за исключение dataModel)
IS_PLATFORM_DOCS_CONTEXT="False"
# Ссылки из требования
IS_SERVICE_LINKS_CONTEXT="True"

#
# Настройки Context Retrieval Agent
#
# Обязательный sub-запрос по integration-страницам: сколько страниц извлекать
# независимо от результатов основного поиска. 0 — отключить sub-запрос.
CONTEXT_INTEGRATION_TOP_K = int(os.getenv("CONTEXT_INTEGRATION_TOP_K", "10"))

# TRANSFORMERS_OFFLINE=1 и HF_HUB_OFFLINE=1 - полный OFF-LINE режим
# влияют НА БИБЛИОТЕКИ, а не код !!!
# ЧТО ПРОИЗОЙДЕТ:
# 1. Проверит ТОЛЬКО ~/.cache/huggingface/
# 2. Если модель есть - загрузит мгновенно (1-3 сек)
# 3. Если модели нет - ОШИБКА (безопасный fail)
# 4. Никаких сетевых запросов
#
# Библиотека: transformers (от HuggingFace)
# Что контролирует: Работу моделей Transformer (BERT, GPT и т.д.)
# Эффект: Отключает онлайн-проверки конфигураций, метаданных модели, обновлений
# ВАЖНО: os.environ.setdefault устанавливает переменную окружения для текущего
# процесса и всех дочерних библиотек (transformers, huggingface_hub).
# Простое присвоение Python-переменной НЕ работает — библиотеки читают os.environ,
# а не Python namespace.
# setdefault не перезаписывает значение если переменная уже задана в .env или shell.
os.environ.setdefault("TRANSFORMERS_OFFLINE", os.getenv("TRANSFORMERS_OFFLINE", "1"))
os.environ.setdefault("HF_HUB_OFFLINE", os.getenv("HF_HUB_OFFLINE", "1"))

# Для совместимости — оставляем Python-переменные для использования в коде
TRANSFORMERS_OFFLINE = os.environ["TRANSFORMERS_OFFLINE"]
HF_HUB_OFFLINE = os.environ["HF_HUB_OFFLINE"]

# ============================================================================
# РЕЖИМ ИНДЕКСАЦИИ
# ============================================================================
# "legacy"       — старый формат (doc_type=requirement, без vector_type).
#                  Использует LEGACY_EMBEDDING_MODEL (all-MiniLM-L6-v2).
#                  Быстрый импорт из Confluence, подходит как source для миграции.
# "multi_vector" — новый формат (title + summary + content/chunk векторы).
#                  Использует EMBEDDING_MODEL (deepvk/USER2-base).
#                  Используется для поиска в CRA и ревью требований.
INDEXING_MODE = os.getenv("INDEXING_MODE", "multi_vector")

# Модель для legacy-режима. all-MiniLM-L6-v2 — быстрая (CPU), 384-мерные векторы,
# не требует task-specific префиксов. Используется только при INDEXING_MODE=legacy.
LEGACY_EMBEDDING_MODEL = os.getenv("LEGACY_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
# Сумма весов не обязана равняться 1.0 — RRF нормализует результат сам.
# Title-вектор точен для коротких запросов по названию сущности.
# Summary-вектор отражает суть страницы без шума перекрёстных ссылок.
# Content-вектор даёт детальный поиск по телу документа.
# Начинали с весов 0.5, 0.3, 0.2
MV_TITLE_WEIGHT = float(os.getenv("MV_TITLE_WEIGHT", "0.7"))
MV_SUMMARY_WEIGHT = float(os.getenv("MV_SUMMARY_WEIGHT", "0.25"))
MV_CONTENT_WEIGHT = float(os.getenv("MV_CONTENT_WEIGHT", "0.05"))