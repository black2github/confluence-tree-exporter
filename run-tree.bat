@echo off
chcp 65001 >nul
REM ============================================================================
REM  Выгрузка ДЕРЕВА страниц Confluence в markdown.
REM  Результат пишется в подкаталог conf-requirements\<service>\<subdir>\
REM
REM  Использование:
REM    run-tree.bat <page_id> <service_code> <subdir> [source] [флаги]
REM  Пример:
REM    run-tree.bat 12345 CORP_CARDS лимиты DBOCORPESPLN --tasks
REM    run-tree.bat 12345 CORP_CARDS лимиты --all --http --with-images
REM    run-tree.bat 12345 CORP_CARDS лимиты --http --with-images
REM
REM  Флаги: --http --all/--tasks --keep-history --with-images --with-index --drop-strikethrough
REM         --unapproved-jira <file.json> — список Jira ID неутверждённых задач:
REM         джира из ЧЁРНОЙ строки истории входит в список -> состав страницы
REM         не утверждён (--tasks: обёртка вставками; approved: пропуск; --all: draft)
REM  Пример: run-tree.bat 12345 CORP_CARDS лимиты --tasks --unapproved-jira unapproved.json
REM ============================================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: окружение не установлено. Сначала запустите install.bat
    exit /b 1
)
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m app.scripts.migrate_confluence_tree %*
endlocal
