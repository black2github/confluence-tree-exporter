@echo off
REM ============================================================================
REM  Выгрузка ОДНОЙ страницы Confluence в markdown.
REM
REM  Использование:
REM    run-page.bat <page_id> <service_code> <subdir> [source] [флаги]
REM  Пример:
REM    run-page.bat 12345 CORP_CARDS лимиты
REM
REM  Флаги: --http --all --keep-history --with-images --drop-strikethrough
REM ============================================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: окружение не установлено. Сначала запустите install.bat
    exit /b 1
)
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m app.scripts.migrate_confluence_page %*
endlocal
