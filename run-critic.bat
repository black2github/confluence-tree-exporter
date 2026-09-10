@echo off
REM ============================================================================
REM  Работа со статусом требований в разметке CriticMarkup (утилита critic).
REM  Работает с ЛОКАЛЬНЫМИ .md - доступ к Confluence НЕ требуется.
REM
REM  Использование:
REM    run-critic.bat <команда> [аргументы] [ключи]
REM  Команды:
REM    apply <TASK-ID>   принять правки задачи (задача вышла на ПРОМ)
REM    reject <TASK-ID>  откатить правки задачи (задача отменена)
REM    apply-all         принять правки ВСЕХ задач (целевое состояние)
REM    reject-all        откатить правки ВСЕХ задач (текущий ПРОМ)
REM    lint              проверить корректность разметки (для CI)
REM    list              список незавершённых задач в репозитории
REM  Примеры:
REM    run-critic.bat apply GBO-12345 --path conf-requirements\
REM    run-critic.bat lint --path conf-requirements\
REM    run-critic.bat list --path conf-requirements\ --manifest conf-requirements\migration-manifest.yaml
REM
REM  Подробное руководство: app\scripts\CI\critic_manual.md
REM ============================================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: окружение не установлено. Сначала запустите install.bat
    exit /b 1
)
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m app.scripts.CI.critic %*
endlocal
